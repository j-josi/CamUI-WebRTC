"""
Virtual MP4 faststart — two complementary strategies for browser-seekable MP4:

1. Standard MP4 (moov at end):
   VirtualFaststartMP4 serves the file with moov moved to front in memory,
   patching stco/co64 offsets on the fly. No disk modification needed.

2. Fragmented MP4 / fMP4 (empty_moov at front, moof+mdat fragments):
   append_mfra() appends a Movie Fragment Random Access (mfra) box to the file.
   The browser uses tfra entries to map timestamps → byte offsets for seeking.
   Only a small index (~20 bytes/fragment) is appended; no temp file needed.
"""

import logging
import os
import struct
from collections import defaultdict
from typing import Optional, List, Tuple, Generator

logger = logging.getLogger(__name__)

# Container box types whose contents must be recursively searched for stco/co64.
_CONTAINER_BOXES = {
    b'moov', b'trak', b'mdia', b'minf', b'stbl', b'dinf',
    b'edts', b'udta', b'meta', b'ilst', b'moof', b'traf', b'mvex',
}


# ---------------------------------------------------------------------------
# Box parsing
# ---------------------------------------------------------------------------

def _parse_top_level_boxes(file_path: str) -> List[Tuple[bytes, int, int]]:
    """Return list of (box_type, file_offset, box_size) for all top-level boxes."""
    boxes = []
    file_size = os.path.getsize(file_path)
    with open(file_path, 'rb') as f:
        pos = 0
        while pos < file_size:
            f.seek(pos)
            header = f.read(8)
            if len(header) < 8:
                break
            size = struct.unpack('>I', header[:4])[0]
            box_type = header[4:8]
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                size = struct.unpack('>Q', ext)[0]
            elif size == 0:
                size = file_size - pos
            if size < 8:
                break
            boxes.append((box_type, pos, size))
            pos += size
    return boxes


# ---------------------------------------------------------------------------
# stco / co64 patching
# ---------------------------------------------------------------------------

def _patch_offsets_in_place(data: bytearray, pos: int, end: int, delta: int) -> None:
    """Recursively walk the box tree in data[pos:end] and patch stco/co64 offsets."""
    while pos < end:
        if pos + 8 > end:
            break
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        box_type = bytes(data[pos + 4:pos + 8])
        header_len = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end + 1:
            break

        content_start = pos + header_len

        if box_type == b'stco':
            # FullBox: version(1) + flags(3) + entry_count(4) + offsets(4*n)
            ec_pos = content_start + 4
            if ec_pos + 4 > pos + size:
                pos += size
                continue
            n = struct.unpack('>I', data[ec_pos:ec_pos + 4])[0]
            for i in range(n):
                ep = ec_pos + 4 + i * 4
                if ep + 4 > pos + size:
                    break
                old = struct.unpack('>I', data[ep:ep + 4])[0]
                new_val = old + delta
                if 0 <= new_val <= 0xFFFFFFFF:
                    struct.pack_into('>I', data, ep, new_val)

        elif box_type == b'co64':
            ec_pos = content_start + 4
            if ec_pos + 4 > pos + size:
                pos += size
                continue
            n = struct.unpack('>I', data[ec_pos:ec_pos + 4])[0]
            for i in range(n):
                ep = ec_pos + 4 + i * 8
                if ep + 8 > pos + size:
                    break
                old = struct.unpack('>Q', data[ep:ep + 8])[0]
                struct.pack_into('>Q', data, ep, old + delta)

        elif box_type in _CONTAINER_BOXES:
            _patch_offsets_in_place(data, content_start, pos + size, delta)

        pos += size


def patch_moov(moov_data: bytes, delta: int) -> bytes:
    """Return a copy of moov_data with all stco/co64 offsets shifted by delta."""
    result = bytearray(moov_data)
    _patch_offsets_in_place(result, 0, len(result), delta)
    return bytes(result)


# ---------------------------------------------------------------------------
# Virtual faststart streamer
# ---------------------------------------------------------------------------

class VirtualFaststartMP4:
    """
    Provides virtual faststart streaming for an MP4 whose moov atom comes after mdat.

    The file on disk is never modified.  Call needs_faststart to check whether the
    file actually requires the reordering (already-faststart files are passed through).
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._total_size: int = os.path.getsize(file_path)
        self._needs_faststart: bool = False

        # Virtual layout regions (only set when needs_faststart is True)
        self._header_data: bytes = b''   # everything before mdat (ftyp etc.)
        self._moov_data: bytes = b''     # patched moov
        self._mdat_file_offset: int = 0  # where mdat starts in the original file
        self._mdat_size: int = 0

        # Offsets in the virtual stream
        self._header_end: int = 0        # end of header region
        self._moov_end: int = 0          # end of moov region (= mdat region start)

        self._analyze()

    def _analyze(self) -> None:
        boxes = _parse_top_level_boxes(self.file_path)

        moov_box: Optional[Tuple[bytes, int, int]] = None
        mdat_box: Optional[Tuple[bytes, int, int]] = None
        for b in boxes:
            if b[0] == b'moov':
                moov_box = b
            elif b[0] == b'mdat':
                mdat_box = b

        if moov_box is None or mdat_box is None:
            return
        if moov_box[1] < mdat_box[1]:
            return  # already faststart

        self._needs_faststart = True

        moov_offset, moov_size = moov_box[1], moov_box[2]
        mdat_offset, mdat_size = mdat_box[1], mdat_box[2]

        # Collect header bytes = all boxes that come before mdat
        header_bytes = b''
        with open(self.file_path, 'rb') as f:
            for box_type, offset, size in boxes:
                if offset >= mdat_offset:
                    break
                f.seek(offset)
                header_bytes += f.read(size)

            # Load moov
            f.seek(moov_offset)
            raw_moov = f.read(moov_size)

        # delta = virtual_mdat_start - original_mdat_start
        # virtual_mdat_start = len(header_bytes) + len(moov)
        virtual_mdat_start = len(header_bytes) + moov_size
        delta = virtual_mdat_start - mdat_offset

        self._header_data = header_bytes
        self._moov_data = patch_moov(raw_moov, delta)
        self._mdat_file_offset = mdat_offset
        self._mdat_size = mdat_size

        self._header_end = len(header_bytes)
        self._moov_end = self._header_end + moov_size

    @property
    def needs_faststart(self) -> bool:
        return self._needs_faststart

    @property
    def total_size(self) -> int:
        return self._total_size

    def stream_range(
        self, start: int, end: int, chunk_size: int = 65536
    ) -> Generator[bytes, None, None]:
        """
        Yield bytes in the virtual stream from position start (inclusive) to end (exclusive).
        If the file does not need faststart, bytes are read directly from disk.
        """
        if not self._needs_faststart:
            with open(self.file_path, 'rb') as f:
                f.seek(start)
                remaining = end - start
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    yield chunk
                    remaining -= len(chunk)
            return

        pos = start
        mdat_f = None
        try:
            while pos < end:
                if pos < self._header_end:
                    # header region (ftyp etc.) — served from memory
                    chunk_end = min(end, self._header_end)
                    yield self._header_data[pos:chunk_end]
                    pos = chunk_end

                elif pos < self._moov_end:
                    # moov region — served from patched in-memory bytes
                    moov_pos = pos - self._header_end
                    chunk_end = min(end, self._moov_end)
                    yield self._moov_data[moov_pos:chunk_end - self._header_end]
                    pos = chunk_end

                else:
                    # mdat region — streamed from original file
                    if mdat_f is None:
                        orig_offset = self._mdat_file_offset + (pos - self._moov_end)
                        mdat_f = open(self.file_path, 'rb')
                        mdat_f.seek(orig_offset)
                    remaining = end - pos
                    chunk = mdat_f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    yield chunk
                    pos += len(chunk)
        finally:
            if mdat_f is not None:
                mdat_f.close()


# ---------------------------------------------------------------------------
# fMP4 mfra (Movie Fragment Random Access) appending
# ---------------------------------------------------------------------------
# After a fragmented MP4 recording completes, append an mfra box so that
# browsers can seek to arbitrary positions using HTTP Range requests.
# Only ~20 bytes per fragment are written; no temp file is required.

# ---------------------------------------------------------------------------
# fMP4 info extraction for MSE (Media Source Extensions) player
# ---------------------------------------------------------------------------

def _find_first(data: bytes, box_type: bytes) -> Optional[bytes]:
    """Return payload of the first occurrence of box_type in data, or None."""
    for bt, payload in _parse_inner_boxes(data):
        if bt == box_type:
            return payload
    return None


def _extract_codec_info(moov_payload: bytes) -> Tuple[int, str]:
    """
    Walk moov payload and extract the video-track timescale plus a codec MIME
    string suitable for MediaSource.addSourceBuffer().

    Returns (timescale, mime_string).
    """
    timescale = 90000
    video_codec = 'avc1.640028'  # H.264 High Profile Level 4.0 fallback
    has_audio = False

    for bt, trak in _parse_inner_boxes(moov_payload):
        if bt != b'trak':
            continue
        mdia = _find_first(trak, b'mdia')
        if mdia is None:
            continue

        # Handler type (b'vide' or b'soun')
        hdlr = _find_first(mdia, b'hdlr')
        if not hdlr or len(hdlr) < 12:
            continue
        handler = hdlr[8:12]  # after version(1)+flags(3)+pre_defined(4)

        if handler == b'vide':
            # Timescale from mdhd
            mdhd = _find_first(mdia, b'mdhd')
            if mdhd and len(mdhd) >= 16:
                ver = mdhd[0]
                ts_off = 20 if ver == 1 else 12
                if len(mdhd) >= ts_off + 4:
                    timescale = struct.unpack('>I', mdhd[ts_off:ts_off + 4])[0]

            # Codec from avcC inside stsd
            stbl = _find_first(_find_first(mdia, b'minf') or b'', b'stbl')
            if stbl:
                stsd = _find_first(stbl, b'stsd')
                if stsd and len(stsd) >= 8:
                    for et, ep in _parse_inner_boxes(stsd[8:]):
                        if et in (b'avc1', b'avc2', b'avc3'):
                            avcc = _find_first(ep[78:], b'avcC')
                            if avcc and len(avcc) >= 4:
                                video_codec = (
                                    f'avc1.{avcc[1]:02X}{avcc[2]:02X}{avcc[3]:02X}'
                                )
                            break

        elif handler == b'soun':
            has_audio = True

    mime = f'video/mp4; codecs="{video_codec}' + (',mp4a.40.2"' if has_audio else '"')
    return timescale, mime


def _get_moov_info(file_path: str) -> Tuple[int, int, str]:
    """Return (moov_end, timescale, codec_mime) by reading the moov box."""
    with open(file_path, 'rb') as f:
        file_size = os.fstat(f.fileno()).st_size
        pos = 0
        while pos < file_size:
            f.seek(pos)
            btype, size, hlen = _read_box_header(f)
            if not btype or size < hlen:
                break
            if btype == b'moov':
                payload = f.read(size - hlen)
                ts, mime = _extract_codec_info(payload)
                return pos + size, ts, mime
            pos += size
    return 0, 90000, 'video/mp4; codecs="avc1.640028,mp4a.40.2"'


def _scan_fragments_from(file_path: str, from_byte: int, timescale: int) -> list:
    """
    Scan moof+mdat pairs starting at from_byte.
    Returns list of {byte_start, byte_end, decode_time, time_seconds}.
    Efficient for polling: skips moov and all earlier fragments.
    """
    file_size = os.path.getsize(file_path)
    fragments = []
    with open(file_path, 'rb') as f:
        f.seek(from_byte)
        while f.tell() < file_size:
            box_pos = f.tell()
            btype, size, hlen = _read_box_header(f)
            if not btype or size < hlen:
                break
            if btype == b'moof':
                moof_start = box_pos
                payload = f.read(size - hlen)
                _, decode_time = _parse_moof_payload(payload)
                # Next box should be mdat
                mdat_pos = f.tell()
                btype2, mdat_size, _ = _read_box_header(f)
                if btype2 == b'mdat':
                    byte_end = mdat_pos + mdat_size
                    frag = {
                        'byte_start':  moof_start,
                        'byte_end':    byte_end,
                        'decode_time': decode_time,
                    }
                    if timescale:
                        frag['time_seconds'] = decode_time / timescale
                    fragments.append(frag)
                    f.seek(byte_end)
                else:
                    f.seek(mdat_pos + mdat_size)
            else:
                f.seek(box_pos + size)
    return fragments


def get_fmp4_info(file_path: str, from_byte: int = 0) -> dict:
    """
    Return metadata and fragment list for a fragmented MP4.

    from_byte=0  → full response: moov_end, timescale, codec_mime, all fragments.
    from_byte>0  → polling response: only fragments starting at from_byte.
                   moov is NOT re-parsed; timescale/codec_mime come from initial call.
    """
    file_size = os.path.getsize(file_path)

    if from_byte == 0:
        moov_end, timescale, codec_mime = _get_moov_info(file_path)
        fragments = _scan_fragments_from(file_path, moov_end, timescale)
        return {
            'file_size':  file_size,
            'moov_end':   moov_end,
            'timescale':  timescale,
            'codec_mime': codec_mime,
            'fragments':  fragments,
        }

    # Polling: client already knows timescale/codec/moov_end.
    # Scan only new fragments; time_seconds omitted (client computes from decode_time).
    fragments = _scan_fragments_from(file_path, from_byte, 0)
    return {
        'file_size': file_size,
        'fragments': fragments,
    }


# ---------------------------------------------------------------------------
# mfra helpers (shared with MSE scan above)
# ---------------------------------------------------------------------------

def _read_box_header(f) -> Tuple[bytes, int, int]:
    """Read one box header from f. Returns (type, total_size, header_len)."""
    start = f.tell()
    raw = f.read(8)
    if len(raw) < 8:
        return b'', 0, 0
    size = struct.unpack('>I', raw[:4])[0]
    btype = raw[4:8]
    header_len = 8
    if size == 1:
        ext = f.read(8)
        if len(ext) < 8:
            return b'', 0, 0
        size = struct.unpack('>Q', ext)[0]
        header_len = 16
    elif size == 0:
        size = os.fstat(f.fileno()).st_size - start
    return btype, size, header_len


def _parse_inner_boxes(data: bytes) -> List[Tuple[bytes, bytes]]:
    """Return list of (box_type, box_payload_bytes) from a flat byte sequence."""
    result = []
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            break
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        btype = data[pos + 4:pos + 8]
        hlen = 8
        if size == 1:
            if pos + 16 > len(data):
                break
            size = struct.unpack('>Q', data[pos + 8:pos + 16])[0]
            hlen = 16
        if size < hlen or pos + size > len(data):
            break
        result.append((btype, data[pos + hlen:pos + size]))
        pos += size
    return result


def _collect_fragments(file_path: str) -> List[Tuple[int, int, int]]:
    """
    Scan a fragmented MP4 and return (track_id, decode_time, moof_offset) for
    every fragment. Only moof boxes are visited; all other boxes are skipped.
    """
    fragments = []
    file_size = os.path.getsize(file_path)
    with open(file_path, 'rb') as f:
        while True:
            pos = f.tell()
            if pos >= file_size:
                break
            btype, size, hlen = _read_box_header(f)
            if not btype or size < hlen:
                break
            if btype == b'moof':
                payload = f.read(size - hlen)
                track_id, decode_time = _parse_moof_payload(payload)
                if track_id is not None:
                    fragments.append((track_id, decode_time, pos))
            else:
                f.seek(pos + size)
    return fragments


def _parse_moof_payload(data: bytes) -> Tuple[Optional[int], int]:
    """Extract (track_id, decode_time) from the payload of a moof box."""
    for btype, payload in _parse_inner_boxes(data):
        if btype == b'traf':
            return _parse_traf_payload(payload)
    return None, 0


def _parse_traf_payload(data: bytes) -> Tuple[Optional[int], int]:
    """Extract (track_id, baseMediaDecodeTime) from a traf payload."""
    track_id: Optional[int] = None
    decode_time: int = 0
    for btype, payload in _parse_inner_boxes(data):
        if btype == b'tfhd' and len(payload) >= 8:
            # FullBox: version(1) + flags(3), then track_ID(4)
            track_id = struct.unpack('>I', payload[4:8])[0]
        elif btype == b'tfdt' and len(payload) >= 5:
            version = payload[0]
            if version == 1 and len(payload) >= 12:
                decode_time = struct.unpack('>Q', payload[4:12])[0]
            elif len(payload) >= 8:
                decode_time = struct.unpack('>I', payload[4:8])[0]
    return track_id, decode_time


def _parse_traf_total_duration(data: bytes) -> Optional[int]:
    """
    Sum all sample durations in a traf box payload.
    Returns total duration in media ticks, or None if undetermined.
    """
    default_sample_duration = 0
    trun_total: Optional[int] = None

    for btype, payload in _parse_inner_boxes(data):
        if btype == b'tfhd' and len(payload) >= 8:
            flags = struct.unpack('>I', b'\x00' + payload[1:4])[0]
            off = 8  # after version(1)+flags(3)+track_ID(4)
            if flags & 0x000001:  # base_data_offset present
                off += 8
            if flags & 0x000002:  # sample_description_index present
                off += 4
            if (flags & 0x000008) and len(payload) >= off + 4:  # default_sample_duration
                default_sample_duration = struct.unpack('>I', payload[off:off + 4])[0]

        elif btype == b'trun' and len(payload) >= 8:
            flags = struct.unpack('>I', b'\x00' + payload[1:4])[0]
            sample_count = struct.unpack('>I', payload[4:8])[0]
            off = 8
            if flags & 0x000001:  # data_offset present
                off += 4
            if flags & 0x000004:  # first_sample_flags present
                off += 4

            if flags & 0x000100:  # per-sample duration present
                total = 0
                per_sample_size = (
                    4 * bool(flags & 0x000100) +
                    4 * bool(flags & 0x000200) +
                    4 * bool(flags & 0x000400) +
                    4 * bool(flags & 0x000800)
                )
                for i in range(sample_count):
                    soff = off + i * per_sample_size
                    if soff + 4 > len(payload):
                        break
                    total += struct.unpack('>I', payload[soff:soff + 4])[0]
                trun_total = total
            else:
                trun_total = default_sample_duration * sample_count

    return trun_total


def get_exact_video_duration(file_path: str) -> Optional[float]:
    """
    Return the exact video duration in seconds by parsing the last moof's trun box.
    More accurate than estimating from fragment timestamps alone.
    """
    try:
        moov_end, timescale, _ = _get_moov_info(file_path)
        if not timescale:
            return None

        file_size = os.path.getsize(file_path)
        last_moof_payload: Optional[bytes] = None
        last_decode_time: int = 0

        with open(file_path, 'rb') as f:
            f.seek(moov_end)
            while f.tell() < file_size:
                box_pos = f.tell()
                btype, size, hlen = _read_box_header(f)
                if not btype or size < hlen:
                    break
                if btype == b'moof':
                    last_moof_payload = f.read(size - hlen)
                    _, last_decode_time = _parse_moof_payload(last_moof_payload)
                else:
                    f.seek(box_pos + size)

        if last_moof_payload is None:
            return None

        traf = _find_first(last_moof_payload, b'traf')
        if traf is None:
            return None

        trun_ticks = _parse_traf_total_duration(traf)
        if trun_ticks is None or trun_ticks == 0:
            return None

        return (last_decode_time + trun_ticks) / timescale
    except Exception:
        logger.debug("get_exact_video_duration failed for %s", file_path, exc_info=True)
        return None


def _build_mfra_box(fragments: List[Tuple[int, int, int]]) -> bytes:
    """
    Build the mfra box from (track_id, decode_time, moof_offset) entries.

    Layout:
        mfra
          tfra  (one per track, version=1 for 64-bit time/offset)
          mfro  (total size of mfra, so browser can locate it from the tail)
    """
    by_track: dict = defaultdict(list)
    for track_id, decode_time, moof_offset in fragments:
        by_track[track_id].append((decode_time, moof_offset))

    tfra_bytes = b''
    for track_id in sorted(by_track):
        entries = by_track[track_id]
        entry_data = b''
        for decode_time, moof_offset in entries:
            # version=1: 64-bit time + 64-bit offset
            entry_data += struct.pack('>QQ', decode_time, moof_offset)
            # traf_number=1, trun_number=1, sample_number=1 (1 byte each, length_size=0)
            entry_data += b'\x01\x01\x01'

        # FullBox header: version=1, flags=0
        tfra_payload = (
            b'\x01\x00\x00\x00'           # version=1, flags=0
            + struct.pack('>I', track_id)  # track_ID
            + b'\x00\x00\x00\x00'         # reserved(26) + length_sizes(6) = all 0
            + struct.pack('>I', len(entries))
            + entry_data
        )
        tfra_size = 8 + len(tfra_payload)
        tfra_bytes += struct.pack('>I', tfra_size) + b'tfra' + tfra_payload

    # mfro is always 12 bytes; its payload is the total size of the mfra box
    mfra_size = 8 + len(tfra_bytes) + 12
    mfro = struct.pack('>I', 12) + b'mfro' + b'\x00\x00\x00\x00' + struct.pack('>I', mfra_size)

    return struct.pack('>I', mfra_size) + b'mfra' + tfra_bytes + mfro


def _mfra_already_present(file_path: str) -> bool:
    """Return True if the file already ends with an mfra box."""
    try:
        with open(file_path, 'rb') as f:
            f.seek(-12, 2)            # mfro is always 12 bytes
            data = f.read(12)
        if len(data) == 12 and data[4:8] == b'mfro':
            return True
    except OSError:
        pass
    return False


def append_mfra(file_path: str) -> bool:
    """
    Append an mfra (Movie Fragment Random Access) box to a completed fMP4.

    The browser reads mfro from the tail, locates mfra, builds a timestamp→offset
    index from tfra, and uses HTTP Range requests to seek to any position.
    No temporary file or extra disk space is required.

    Returns True on success.
    """
    if _mfra_already_present(file_path):
        logger.debug("mfra already present in %s, skipping", file_path)
        return True
    try:
        fragments = _collect_fragments(file_path)
        if not fragments:
            logger.warning("No moof fragments found in %s", file_path)
            return False
        mfra = _build_mfra_box(fragments)
        with open(file_path, 'ab') as f:
            f.write(mfra)
        logger.info("Appended mfra (%d B, %d fragments) to %s",
                    len(mfra), len(fragments), file_path)
        return True
    except Exception:
        logger.exception("Failed to append mfra to %s", file_path)
        return False
