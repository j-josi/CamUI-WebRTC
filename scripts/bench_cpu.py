#!/usr/bin/env python3
"""
Systematic CPU / recording-fps benchmark for CamUI-WebRTC.

Goal: comparable numbers from different Raspberry Pi models, cameras and
microphones. For every combination of

    browser clients (0..3)  x  audio (on/off)  x  mode (recording+live / live only)
    x  recording resolution  x  live stream resolution

the script measures, while CamUI is running:
  * CPU of the whole system (all cores = 100 %),
  * CPU of camera_server, MediaMTX, coturn and gunicorn (% of ONE core) and the
    largest foreign CPU users (so disturbances such as an IDE or htop show up),
  * the frame rate of the recorded file: video packets / duration (ffprobe),
    compared with the configured fps (>= 98 % is "OK"),
  * temperature and throttling flags (a throttled cell is not comparable).

Because the number of connected browser clients cannot be set by a script, the
script stops before every client count and asks you to connect exactly that many
browser tabs/devices (live image playing), then waits for Enter.

Result files (default: <repo>/benchmarks/, which is git-ignored):
    bench_cpu_<app version>_<pi model>_<timestamp>.txt    header (timestamp, release, app
                                         version, Pi model, OS/kernel, camera sensor,
                                         microphone, ...) followed by the result table
    bench_cpu_<app version>_<pi model>_<timestamp>.json   the same data, machine readable
Both files are rewritten after every measured cell, so an aborted run keeps its
results. After a run that finished without errors the script asks whether the result
should also be kept as a reference result: it is then copied to docs/benchmarks/
(tracked by git) and a row is added to the comparison table in the README there.

CamUI and its infrastructure: if CamUI (gunicorn + camera_server.py) and MediaMTX/coturn
are not running, the script starts them itself - the same way "make run" does, but always
with LOG_LEVEL=ERROR, one worker and without --reload, so the runs are comparable - and
stops exactly what it started when it finishes (also after Ctrl-C). A CamUI that is already
running is used as it is (the log header says which case applied). Use --no-autostart to
turn this off. Nothing else may use the camera. Close other programs on the Pi (IDE,
htop, ...) for reliable numbers.
The script temporarily changes the recording/live resolution and (for the audio
off/on cells) the microphone setting of the camera; both are restored at the end,
also after Ctrl-C. Recordings are written to the gallery folder under the prefix
"benchcpu_" and deleted right after the measurement, together with their thumbnails.

Usage (from the repo root; CamUI is started automatically if it is not running):
    venv/bin/python scripts/bench_cpu.py                       # default: highest recording res, live res 0 (largest) and 2,
                                                               # 0 and 3 clients, audio on+off  (--clients 0,3 --stream-indexes 0,2)
    venv/bin/python scripts/bench_cpu.py --dry-run             # show header and plan only
    venv/bin/python scripts/bench_cpu.py --clients 0,1,2,3 --stream-indexes all   # full client and live-resolution sweep
    venv/bin/python scripts/bench_cpu.py --audio off           # only without microphone
    venv/bin/python scripts/bench_cpu.py --rec-indexes all     # every recording resolution (long!)
    venv/bin/python scripts/bench_cpu.py --label "Wi-Fi 5 GHz, 2 phones + laptop"
"""
import argparse
import atexit
import datetime
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PREFIX = "benchcpu_"
TARGET_PCT = 98.0           # recording fps must reach this share of the configured fps
SETTLE_S = 6                # after a pipeline change, before anything is measured
TAIL_S = 3                  # recording keeps running after the measuring window
GROUP_ORDER = ("camera_server", "mediamtx", "coturn", "gunicorn")
CPU_TABLE_SECTION = "CPU benchmark results"      # heading above the table in docs/benchmarks/README.md

from bench_common import (  # noqa: E402  (shared helpers live next to this script)
    app_version, busiest_core_percent, core_times, cpu_times, header_value, offer_reference, package_version, read as _read,
    render_header, render_table, result_stem, run as _run, save_results, system_items, throttle_now,
    timestamp_items, version_issue, vc_temp, vc_throttled)


# --------------------------------------------------------------------------
# Pure helpers (unit tested in tests/test_bench_cpu.py)
# --------------------------------------------------------------------------

def parse_index_list(spec, count):
    """'all' -> every index, '0,2' -> [0, 2]. Raises ValueError for bad input."""
    spec = str(spec).strip().lower()
    if spec == "all":
        return list(range(count))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) >= count:
            raise ValueError("index %r out of range 0..%d" % (part, count - 1))
        if int(part) not in out:
            out.append(int(part))
    if not out:
        raise ValueError("empty index list")
    return out


def parse_clients(spec):
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part.isdigit() or int(part) > 20:
            raise ValueError("client count %r must be a number from 0 to 20" % part)
        if int(part) not in out:
            out.append(int(part))
    if not out:
        raise ValueError("empty client list")
    return out


def build_plan(clients, audio_modes, modes, rec_idxs, live_idxs):
    """Ordered list of cells. Client count is the outermost loop so the user only has
    to change the number of connected browsers once per count."""
    plan = []
    for c in clients:
        for audio in audio_modes:
            for mode in modes:
                recs = rec_idxs if mode == "record" else [None]
                for rec in recs:
                    for live in live_idxs:
                        plan.append({"clients": c, "audio": audio, "mode": mode,
                                     "rec_idx": rec, "live_idx": live})
    return plan


def estimate_seconds(plan, warmup, window):
    total = 0
    for cell in plan:
        if cell["mode"] == "record":
            total += SETTLE_S + warmup + window + TAIL_S + 8    # + stop, ffprobe, cleanup
        else:
            total += SETTLE_S + warmup + window
    return total


def fmt_res(r):
    return "%dx%d" % (int(r[0]), int(r[1]))


def normalize_comm(comm):
    """Merge per-CPU kernel worker names so they aggregate into one line."""
    if comm.startswith("kworker/"):
        tail = comm.split("-", 1)[1].split("/")[0] if "-" in comm else ""
        return "kworker:" + tail if tail else "kworker"
    return comm


def classify(comm, cmdline):
    """Group of an app-related process, or None."""
    if comm.startswith("python") and "camera_server.py" in cmdline:
        return "camera_server"
    if comm.startswith("gunicorn"):
        return "gunicorn"
    if comm == "mediamtx":
        return "mediamtx"
    if comm in ("turnserver", "coturn"):
        return "coturn"
    return None


def summarize(before, after, hz, window_s, top_n=3, min_pct=1.5):
    """before/after: {pid: (comm, group, ticks)} -> ({group: % of one core}, [(comm, %)])."""
    groups = {g: 0.0 for g in GROUP_ORDER}
    others = {}
    for pid, (comm, group, ticks) in after.items():
        prev = before.get(pid)
        delta = max(0, ticks - prev[2]) if prev else 0
        pct = 100.0 * delta / hz / window_s
        if group:
            groups[group] += pct
        else:
            name = normalize_comm(comm)
            others[name] = others.get(name, 0.0) + pct
    top = sorted(((n, p) for n, p in others.items() if p >= min_pct), key=lambda x: -x[1])[:top_n]
    return groups, top


def cpu_percent(b0, t0, b1, t1):
    return 100.0 * (b1 - b0) / max(1, t1 - t0)


def reference_issues(version, cells, client_check_ignored):
    """Reasons why a run does not meet the criteria for a reference result (see docs/benchmarks/README.md)."""
    issues = [i for i in (version_issue(version),) if i]
    bad = sorted({f for c in cells for f in cell_flags(c) if f != "BELOW-TARGET"})
    if bad:
        issues.append("cells are flagged %s" % ", ".join(bad))
    if client_check_ignored:
        issues.append("the 0-client rows were measured with live-view connections still open")
    return issues


def table_row(stem, header):
    """One row for the comparison table in docs/benchmarks/README.md."""
    mic = re.sub(r"\s*\(.*\)$", "", header_value(header, "Microphone", "none"))
    cols = ["[%s](%s.txt)" % (stem, stem), header_value(header, "Raspberry Pi model"),
            header_value(header, "Camera sensor"), mic, header_value(header, "App version (git describe)"),
            header_value(header, "Timestamp (local)")[:10], header_value(header, "Network (default route)")]
    return "| " + " | ".join(c.replace("|", "/") for c in cols) + " |"


def cell_flags(cell):
    flags = []
    if cell.get("status") != "ok":
        flags.append("ERROR")
    if cell.get("throttled_now"):
        flags.append("THROTTLED")
    if cell.get("fps_pct") is not None and cell["fps_pct"] < TARGET_PCT:
        flags.append("BELOW-TARGET")
    if cell.get("short_recording"):
        flags.append("SHORT-FILE")
    if cell.get("audio") == "on" and cell.get("mode") == "record" and cell.get("audio_dur") is None \
            and cell.get("status") == "ok":
        flags.append("NO-AUDIO-TRACK")
    return flags


def render_log(header, cells, planned, complete):
    """Plain-text log: header block, then the table. header: [(section, [(key, value)])]."""
    lines = render_header("CamUI-WebRTC CPU benchmark", header)
    lines += ["", "[results]  %d of %d cells%s" % (len(cells), planned, "" if complete else "  (run not finished)"), ""]
    cols = ("#", "clients", "audio", "mode", "recording", "live", "fps", "fps%", "CPU%", "core max",
            "cam_srv", "mediamtx", "coturn", "gunicorn", "temp", "flags / other CPU users")
    rows = []
    for i, c in enumerate(cells, 1):
        g = c.get("groups") or {}

        def num(x, f):
            return (f % x) if x is not None else "-"
        other = ", ".join("%s %.0f%%" % (n, p) for n, p in c.get("others", []))
        extra = " ".join(cell_flags(c))
        if other:
            extra = (extra + "  " if extra else "") + "other: " + other
        rows.append((str(i), str(c["clients"]), c["audio"], c["mode"],
                     c.get("rec_res") or "-", c.get("live_res") or "-",
                     num(c.get("fps"), "%.2f"), num(c.get("fps_pct"), "%.0f"),
                     num(c.get("cpu_total"), "%.1f"), num(c.get("cpu_core_max"), "%.0f"),
                     num(g.get("camera_server"), "%.0f"), num(g.get("mediamtx"), "%.0f"),
                     num(g.get("coturn"), "%.0f"), num(g.get("gunicorn"), "%.0f"),
                     num(c.get("temp_end"), "%.0f"), extra))
    lines += render_table(cols, rows)
    lines += [
        "",
        "Legend",
        "  clients    number of browser clients that were connected to the live view (confirmed by the operator)",
        "  audio      microphone on/off for recording and live stream",
        "  mode       record = recording + live stream, live = live stream only (no recording)",
        "  fps        recorded video packets / file duration (whole file), fps% = share of the configured fps",
        "  CPU%       whole system, all cores = 100 %",
        "  core max   load of the busiest single core (100 % = that core fully busy). Low CPU% with core max near 100 %",
        "             points to a single thread as the limit; low values for both point to hardware (encoder, memory)",
        "  cam_srv, mediamtx, coturn, gunicorn   % of ONE core (100 % = one fully used core)",
        "  temp       SoC temperature at the end of the measuring window (deg C)",
        "  flags      BELOW-TARGET fps < %.0f %% | THROTTLED under-voltage/throttling during the cell |"
        " SHORT-FILE recording shorter than expected | NO-AUDIO-TRACK | ERROR" % TARGET_PCT,
        "  other      largest CPU users that do not belong to CamUI (> 1.5 % of one core), % of one core",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# System information (every collector degrades to "unknown")
# --------------------------------------------------------------------------

def snapshot():
    snap = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % pid) as f:
                s = f.read()
            i = s.rindex(")")
            comm = s[s.index("(") + 1:i]
            fld = s[i + 2:].split()
            ticks = int(fld[11]) + int(fld[12])
            cmdline = ""
            if comm.startswith("python") or comm == "MainThread":
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmdline = f.read().replace(b"\0", b" ").decode(errors="replace")
                if comm == "MainThread" and cmdline:      # Node/Electron apps (IDE servers) name their process like this
                    comm = os.path.basename(cmdline.split(" ", 1)[0]) or comm
        except (OSError, ValueError):
            continue
        snap[int(pid)] = (comm, classify(comm, cmdline), ticks)
    return snap


def default_route():
    out = _run(["ip", "-o", "route", "get", "1.1.1.1"])
    dev = re.search(r"dev (\S+)", out)
    src = re.search(r"src (\S+)", out)
    dev = dev.group(1) if dev else None
    kind = "unknown"
    if dev:
        kind = "Wi-Fi" if os.path.isdir("/sys/class/net/%s/wireless" % dev) else "Ethernet/other"
    return dev, src.group(1) if src else None, kind


def list_microphones():
    """[{name, description, sample_spec, driver, product, vendor}] of non-monitor PulseAudio sources."""
    out = _run(["pactl", "--format=json", "list", "sources"])
    mics = []
    try:
        for s in json.loads(out):
            if s.get("name", "").endswith(".monitor"):
                continue
            props = s.get("properties", {})
            mics.append({"name": s["name"], "description": s.get("description", ""),
                         "sample_spec": s.get("sample_specification", ""), "driver": s.get("driver", ""),
                         "product": props.get("device.product.name", ""),
                         "vendor": props.get("device.vendor.name", "")})
    except (ValueError, KeyError, TypeError):
        pass
    return mics


def bitrates():
    out = {}
    try:
        with open(os.path.join(ROOT, "camera.py")) as f:
            src = f.read()
        for key in ("BITRATE_ENCODER_STREAM", "BITRATE_ENCODER_RECORDING"):
            m = re.search(r"%s\s*=\s*([\d_]+)" % key, src)
            if m:
                out[key] = int(m.group(1).replace("_", ""))
    except OSError:
        pass
    return out


def collect_header(cam, args, mic_used, mics, plan, est_s, notes):
    now = datetime.datetime.now().astimezone()
    dev, ip, kind = default_route()
    try:
        info = cam._attr("camera_info") or {}
    except Exception:
        info = {}
    sensor = info.get("Model", "unknown") if isinstance(info, dict) else "unknown"
    module_name = ""
    try:
        with open(os.path.join(ROOT, "camera_module_info.json")) as f:
            for m in json.load(f).get("camera_modules", []):
                if m.get("sensor_model") == sensor:
                    module_name = m.get("module_name", "")
    except (OSError, ValueError):
        pass
    mic_line = "none (audio cells run without microphone)"
    mic_extra = []
    if mic_used:
        mic_line = "%s (%s)" % (mic_used["description"] or mic_used["name"], mic_used["name"])
        mic_extra = [("Microphone vendor/product", " / ".join(x for x in (mic_used["vendor"], mic_used["product"]) if x) or "unknown"),
                     ("Microphone format", "%s, driver %s" % (mic_used["sample_spec"], mic_used["driver"]))]
    hardware, software = system_items()
    header = [
        ("benchmark", timestamp_items(now) + [
            ("Label", args.label or "-"),
            ("CamUI instance", getattr(args, "instance", "unknown")),
            ("Camera under test", "camera %d" % args.camera),
            ("Configured fps (target)", "%g" % args.fps),
            ("Timing", "settle %d s, warm-up %d s, measuring window %d s, tail %d s" % (SETTLE_S, args.warmup, args.window, TAIL_S)),
            ("Planned cells", "%d (about %d min)" % (len(plan), round(est_s / 60))),
            ("Load average at start", "%.2f %.2f %.2f" % os.getloadavg()),
        ]),
        ("hardware", hardware + [
            ("Camera sensor", "%s%s" % (sensor, " (%s)" % module_name if module_name else "")),
            ("Microphone", mic_line),
        ] + mic_extra + [
            ("Network (default route)", "%s, %s" % (dev or "?", kind)),
        ]),
        ("software", software + [
            ("picamera2 (dpkg)", package_version("python3-picamera2")),
            ("libcamera (dpkg)", package_version("libcamera0*")),
            ("Encoder bitrates (bit/s)", ", ".join("%s=%d" % kv for kv in bitrates().items()) or "unknown"),
        ]),
    ]
    if notes:
        header.append(("notes", [("-", n) for n in notes]))
    return header, now


def listening(port):
    out = _run(["ss", "-Hltn", "( sport = :%d )" % port])
    return bool(out.strip())


def preflight():
    """Abort when the live-stream infrastructure is missing: a benchmark without MediaMTX would silently
    measure a live stream that goes nowhere. Returns a list of warnings."""
    warnings = []
    if not listening(8554):
        sys.exit("error: MediaMTX is not listening on port 8554 - the live stream would go nowhere.\n"
                 "       Start it first, e.g.:  docker compose -f docker/docker-compose.yml up -d mediamtx coturn")
    if not listening(8889):
        sys.exit("error: MediaMTX WebRTC (WHEP) is not listening on port 8889.")
    if not listening(3478):
        warnings.append("coturn is not listening on port 3478: browsers that need the TURN relay cannot connect "
                        "(client cells would not be valid).")
    return warnings


def client_counts():
    """Established TCP connections per service. Informational only: MediaMTX has its API disabled, so the
    number of browser clients cannot be verified exactly."""
    def count(port):
        out = _run(["ss", "-Htn", "state", "established", "( sport = :%d )" % port])
        return len([l for l in out.splitlines() if l.strip()])
    return {"web UI (port 80)": count(80), "TURN relay (3478)": count(3478), "WHEP (8889)": count(8889)}


def client_hints():
    return "established TCP connections: " + ", ".join("%s %d" % kv for kv in client_counts().items())


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

def probe(path):
    """(video packets, video duration s, audio duration s or None) of a media file."""
    def run(sel, entries):
        return subprocess.run(
            ["nice", "-n", "19", "ffprobe", "-v", "error", "-select_streams", sel, "-count_packets",
             "-show_entries", entries, "-of", "default=nw=1", path],
            capture_output=True, text=True).stdout
    d = dict(l.split("=", 1) for l in run("v:0", "stream=nb_read_packets,duration").split() if "=" in l)
    pkts = int(d.get("nb_read_packets", 0))
    dur = float(d["duration"]) if d.get("duration", "N/A") != "N/A" else None
    if dur is None:     # fragmented MP4 has no stream duration -> container duration
        o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.strip()
        dur = float(o) if o else None
    a = run("a:0", "stream=duration").strip()
    adur = float(a.split("=")[1]) if "=" in a and "N/A" not in a else None
    return pkts, dur, adur


def cleanup_gallery():
    gallery = os.path.join(ROOT, "static", "gallery")
    for folder in (gallery, os.path.join(gallery, "thumbnails")):
        try:
            for name in os.listdir(folder):
                if name.startswith(PREFIX):
                    try:
                        os.remove(os.path.join(folder, name))
                    except OSError:
                        pass
        except OSError:
            pass


# --------------------------------------------------------------------------
# Benchmark
# --------------------------------------------------------------------------

class Audio:
    """Switches the camera's microphone through the system settings and restores it."""

    def __init__(self, mgr, cam, camera_num, mic_name):
        self.mgr, self.cam, self.num = mgr, cam, camera_num
        self.mic_name = mic_name
        self.original = cam._attr("_configured_audio_device") or ""
        self.current = self.original

    def _apply(self, want):
        # Switch the microphone with the stream stopped. Older releases change the audio flag of the
        # running stream encoder when the microphone changes; Picamera2 then fails to stop that encoder and
        # the live stream stays dead until CamUI is restarted. With the stream stopped this works on every release.
        self.cam._rpc("stop_streaming")
        try:
            self.mgr.update_system_settings({"camera_audio_devices": {str(self.num): want}})
        finally:
            self.cam._rpc("start_streaming")
        for _ in range(20):
            if self.cam._attr("states").get("is_video_streaming"):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("live stream did not restart after switching the microphone")
        self.current = want

    def set(self, mode):
        want = self.mic_name if mode == "on" else ""
        if want == self.current:
            return False
        self._apply(want)
        active = self.cam._attr("audio_device")
        if bool(active) != bool(want):
            raise RuntimeError("audio device did not switch to %r (active: %r)" % (want or "none", active))
        return True

    def restore(self):
        if self.current != self.original:
            self._apply(self.original)


def measure_window(seconds):
    s0 = snapshot()
    b0, t0 = cpu_times()
    c0 = core_times()
    w0 = time.monotonic()
    time.sleep(seconds)
    s1 = snapshot()
    b1, t1 = cpu_times()
    c1 = core_times()
    window = time.monotonic() - w0
    groups, others = summarize(s0, s1, os.sysconf("SC_CLK_TCK"), window)
    return cpu_percent(b0, t0, b1, t1), busiest_core_percent(c0, c1), groups, others


def run_cell(cam, cell, args, ctx, seq):
    res = ctx["res"]
    out = dict(cell)
    out["live_res"] = fmt_res(res[cell["live_idx"]])
    rec_idx = cell["rec_idx"] if cell["rec_idx"] is not None else ctx["orig_rec"]
    out["rec_res"] = fmt_res(res[rec_idx]) if cell["mode"] == "record" else None
    out["status"] = "ok"
    out["temp_start"] = vc_temp()
    try:
        ctx["audio"].set(cell["audio"])
        cam.set_config({"recording_resolution": rec_idx, "streaming_resolution": cell["live_idx"]})
        cam.reconfigure_video_pipeline()
        time.sleep(SETTLE_S)
        name = "%s%04d.mp4" % (PREFIX, seq)
        if cell["mode"] == "record":
            if not cam.start_recording(name):
                raise RuntimeError("start_recording refused (a recording may already be running)")
            try:
                time.sleep(args.warmup)
                cpu, core_max, groups, others = measure_window(args.window)
                time.sleep(TAIL_S)
            finally:
                cam.stop_recording()
            time.sleep(3)
            src = os.path.join(ROOT, "static", "gallery", name)
            dst = os.path.join(ctx["tmpdir"], name)
            if not os.path.exists(src):
                raise RuntimeError("recording file %s not found in the gallery" % name)
            shutil.move(src, dst)
            pkts, dur, adur = probe(dst)
            os.remove(dst)
            out.update(packets=pkts, duration=dur, audio_dur=adur)
            if dur:
                out["fps"] = pkts / dur
                out["fps_pct"] = 100.0 * out["fps"] / args.fps
                expected = args.warmup + args.window + TAIL_S
                out["short_recording"] = dur < 0.9 * expected
            cleanup_gallery()
        else:
            time.sleep(args.warmup)
            cpu, core_max, groups, others = measure_window(args.window)
        out.update(cpu_total=cpu, cpu_core_max=core_max, groups=groups, others=others)
    except Exception as exc:    # keep going: a failed cell must not lose the others
        out["status"] = "error: %s" % exc
    out["temp_end"] = vc_temp()
    out["throttled"] = vc_throttled()
    out["throttled_now"] = throttle_now(out["throttled"])
    return out


def save(paths, header, cells, planned, complete, meta):
    save_results(paths, render_log(header, cells, planned, complete),
                 {"header": [{"section": s, "items": dict(i)} for s, i in header], "meta": meta,
                  "complete": complete, "planned_cells": planned, "cells": cells})


class BenchAbort(Exception):
    """Raised to stop the run early; results measured so far are kept."""


class AppRunner:
    """Starts CamUI / MediaMTX / coturn when they are not running and stops exactly what it started."""

    COMPOSE = os.path.join(ROOT, "docker", "docker-compose.yml")

    def __init__(self, log_dir, autostart=True):
        self.log_dir, self.autostart = log_dir, autostart
        self.proc = None
        self.log_path = None
        self.started_infra = False
        self.infra_was_down = False
        self.instance = "already running (started outside this script; log level and --reload unknown)"
        atexit.register(self.stop)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: sys.exit(1))      # run atexit -> stop what we started

    @staticmethod
    def infra_up():
        return listening(8554) and listening(8889)

    def ensure_infra(self):
        if self.infra_up():
            return
        self.infra_was_down = True
        if not self.autostart:
            return                                          # preflight() reports it
        print("Starting MediaMTX and coturn ...", flush=True)
        r = subprocess.run(["docker", "compose", "-f", self.COMPOSE, "up", "-d", "mediamtx", "coturn"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            sys.exit("error: could not start MediaMTX/coturn: %s" % (r.stderr.strip()[-300:] or "docker failed"))
        self.started_infra = True
        for _ in range(30):
            if self.infra_up() and listening(3478):
                break
            time.sleep(1)

    def connect(self, connect_with_retry):
        """Connect to the camera server, starting CamUI first if needed. Raises RuntimeError on failure."""
        sock = os.path.join(ROOT, "camera.sock")
        try:
            return connect_with_retry(sock, timeout=3)
        except RuntimeError:
            pass
        if _run(["pgrep", "-x", "gunicorn"]):               # starting up or busy: never start a second instance
            return connect_with_retry(sock, timeout=60)
        if not self.autostart:
            raise RuntimeError("CamUI is not running")
        gunicorn = os.path.join(ROOT, "venv", "bin", "gunicorn")
        if not os.path.exists(gunicorn):
            gunicorn = shutil.which("gunicorn")
        if not gunicorn:
            raise RuntimeError("gunicorn not found (create the venv: see README)")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, "bench_cpu_camui.log")
        env = dict(os.environ, LIBCAMERA_LOG_LEVELS="*:ERROR", LOG_LEVEL="ERROR")
        print("Starting CamUI (gunicorn, 1 worker, LOG_LEVEL=ERROR, log: %s) ..." % self.log_path, flush=True)
        with open(self.log_path, "ab") as log:
            self.proc = subprocess.Popen(
                [gunicorn, "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker",
                 "--workers", "1", "--bind", "0.0.0.0:80", "--log-level", "ERROR", "--chdir", ROOT, "app:app"],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        self.instance = "started by this script (gunicorn, 1 worker, LOG_LEVEL=ERROR, no --reload)"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                tail = _run(["tail", "-n", "5", self.log_path])
                raise RuntimeError("CamUI exited right after the start (port 80 in use?):\n%s" % tail)
            try:
                return connect_with_retry(sock, timeout=3)
            except RuntimeError:
                time.sleep(2)
        raise RuntimeError("CamUI did not become ready within 90 s (see %s)" % self.log_path)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            print("\nStopping CamUI ...", flush=True)
            try:
                self.proc.send_signal(signal.SIGINT)        # like Ctrl-C in "make run"
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            subprocess.run(["pkill", "-TERM", "-f", "[c]amera_server.py"])   # we started the only instance
            time.sleep(2)
        self.proc = None
        if self.started_infra:
            print("Stopping MediaMTX and coturn ...", flush=True)
            subprocess.run(["docker", "compose", "-f", self.COMPOSE, "stop", "mediamtx", "coturn"],
                           capture_output=True)
            self.started_infra = False




def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise SystemExit("\nNo interactive input available (stdin closed) - aborting.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--clients", default="0,3", help="comma list of browser client counts to measure (default: 0,3)")
    ap.add_argument("--audio", choices=["both", "on", "off"], default="both", help="microphone on, off or both")
    ap.add_argument("--modes", default="record,live", help="record (recording + live) and/or live (live only)")
    ap.add_argument("--rec-indexes", default="0", help="recording resolution indexes, 'all' or comma list (0 = largest)")
    ap.add_argument("--stream-indexes", default="0,2",
                    help="live resolution indexes, 'all' or comma list (default: 0,2 = largest and third; "
                         "on a typical list that is 1080p and 720p)")
    ap.add_argument("--fps", type=float, default=30.0, help="configured fps to compare the recording with")
    ap.add_argument("--warmup", type=int, default=8, help="seconds between start and the measuring window")
    ap.add_argument("--window", type=int, default=20, help="length of the measuring window in seconds")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "benchmarks"))
    ap.add_argument("--label", default="", help="free text for the header (e.g. network, client devices)")
    ap.add_argument("--no-autostart", action="store_true",
                    help="do not start CamUI / MediaMTX / coturn when they are not running (default: start and stop them)")
    ap.add_argument("--dry-run", action="store_true", help="print header and plan, measure nothing")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation before the first measurement")
    args = ap.parse_args()

    sys.path.insert(0, ROOT)
    from camera_client import connect_with_retry

    try:
        clients = parse_clients(args.clients)
        modes = [m.strip() for m in args.modes.split(",") if m.strip()]
        if not modes or any(m not in ("record", "live") for m in modes):
            raise ValueError("--modes must be 'record', 'live' or 'record,live'")
    except ValueError as exc:
        sys.exit("error: %s" % exc)

    runner = AppRunner(args.out_dir, autostart=not args.no_autostart)
    runner.ensure_infra()
    try:
        mgr = runner.connect(connect_with_retry)
    except RuntimeError as exc:
        sys.exit("error: %s\n       Start CamUI yourself with 'make run' or drop --no-autostart." % exc)
    if runner.infra_was_down and runner.proc is None:
        sys.exit("error: MediaMTX was not running while CamUI was. The live stream encoder may be in a broken state\n"
                 "       after such an outage ('Encoder already stopped'). MediaMTX is up now - restart CamUI\n"
                 "       (stop 'make run', start it again) or let this script start CamUI itself, then run it again.")
    args.instance = runner.instance + ("; MediaMTX/coturn started by this script" if runner.started_infra else "")
    mgr.init_cameras()
    cam = mgr.get_camera(args.camera)
    if cam is None:
        sys.exit("error: camera %d not found" % args.camera)
    res = [tuple(r) for r in cam._attr("video_resolutions_supported")]
    try:
        rec_idxs = parse_index_list(args.rec_indexes, len(res))
        live_idxs = parse_index_list(args.stream_indexes, len(res))
    except ValueError as exc:
        sys.exit("error: %s (available: %s)" % (exc, list(enumerate(fmt_res(r) for r in res))))

    notes = preflight()
    mics = list_microphones()
    configured = cam._attr("_configured_audio_device") or ""
    mic_used = next((m for m in mics if m["name"] == configured), None) or (mics[0] if mics else None)
    if configured and not any(m["name"] == configured for m in mics):
        notes.append("Configured microphone %r is not available; using %s." % (
            configured, mic_used["name"] if mic_used else "none"))
    if args.audio == "on" and not mic_used:
        sys.exit("error: --audio on requested but no microphone was found")
    audio_modes = {"both": ["on", "off"] if mic_used else ["off"], "on": ["on"], "off": ["off"]}[args.audio]
    if args.audio == "both" and not mic_used:
        notes.append("No microphone found: only the audio-off cells are measured.")

    plan = build_plan(clients, audio_modes, modes, rec_idxs, live_idxs)
    est = estimate_seconds(plan, args.warmup, args.window) + 30 * len(clients)
    header, started = collect_header(cam, args, mic_used, mics, plan, est, notes)

    model = _read("/proc/device-tree/model") or platform.machine()
    version = app_version()
    stamp = started.strftime("%Y%m%d-%H%M%S")
    os.makedirs(args.out_dir, exist_ok=True)
    stem = result_stem("cpu", version["describe"], model, stamp)
    base = os.path.join(args.out_dir, stem)
    paths = (base + ".txt", base + ".json")

    print(render_log(header, [], len(plan), False))
    print("Supported resolutions:", ", ".join("%d=%s" % (i, fmt_res(r)) for i, r in enumerate(res)))
    print("Plan: %d cells, clients %s, audio %s, modes %s, recording %s, live %s" % (
        len(plan), clients, audio_modes, modes, [fmt_res(res[i]) for i in rec_idxs], [fmt_res(res[i]) for i in live_idxs]))
    print("Estimated duration: about %d minutes.\n" % round(est / 60))
    if args.dry_run:
        return
    if not args.yes:
        ask("Press Enter to begin (Ctrl-C aborts). Make sure no recording is running and nothing else uses the CPU... ")

    orig = (cam.configs["recording_resolution"], cam.configs["streaming_resolution"])
    ctx = {"res": res, "orig_rec": orig[0], "audio": Audio(mgr, cam, args.camera, mic_used["name"] if mic_used else ""),
           "tmpdir": tempfile.mkdtemp(prefix="benchcpu_")}
    cells, done, seq, errors_in_a_row = [], 0, 0, 0
    aborted = client_check_ignored = False
    meta = {"args": vars(args), "resolutions": [fmt_res(r) for r in res]}
    try:
        for count in clients:
            group = [c for c in plan if c["clients"] == count]
            print("\n" + "=" * 70)
            if count == 0:
                print("CLIENT COUNT 0: close ALL browser tabs/devices that show the live view.")
            else:
                print("CLIENT COUNT %d: connect exactly %d browser tab(s)/device(s) to http://%s/ and make sure the live\n"
                      "image is playing in each of them (keep the tabs visible)." % (count, count, default_route()[1] or "<pi-address>"))
            print("Hint (not a verification): %s" % client_hints())
            ask("Press Enter when %d client(s) are connected... " % count)
            while count == 0:      # this one can be checked: no live-view connection may be left
                seen = client_counts()
                if not (seen["WHEP (8889)"] or seen["TURN relay (3478)"]):
                    break
                print("Still connected: %s" % client_hints())
                print("Close every browser tab/device that shows the live view (tabs reconnect on their own).")
                answer = ask("Press Enter to check again, or type 'ignore' to measure anyway: ").strip().lower()
                if answer == "ignore":
                    client_check_ignored = True
                    print("Measuring with leftover connections - the 0-client rows are NOT valid then.")
                    break
            for cell in group:
                seq += 1
                done += 1
                print("[%d/%d] clients=%d audio=%s mode=%s rec=%s live=%s ..." % (
                    done, len(plan), cell["clients"], cell["audio"], cell["mode"],
                    fmt_res(res[cell["rec_idx"]]) if cell["rec_idx"] is not None else "-", fmt_res(res[cell["live_idx"]])), flush=True)
                r = run_cell(cam, cell, args, ctx, seq)
                cells.append(r)
                errors_in_a_row = errors_in_a_row + 1 if r["status"] != "ok" else 0
                print("        -> %s  CPU %s%%  fps %s  %s" % (
                    r["status"], "%.1f" % r["cpu_total"] if r.get("cpu_total") is not None else "-",
                    "%.2f (%.0f%%)" % (r["fps"], r["fps_pct"]) if r.get("fps") else "-", " ".join(cell_flags(r))), flush=True)
                save(paths, header, cells, len(plan), False, meta)
                if errors_in_a_row >= 2:
                    raise BenchAbort("two cells failed in a row (%s)" % r["status"])
    except KeyboardInterrupt:
        aborted = True
        print("\nAborted - restoring settings and saving what was measured so far.")
    except BenchAbort as exc:
        aborted = True
        print("\nStopping early: %s.\nThe live stream or camera is probably in a broken state; later cells would not "
              "be valid.\nRestart CamUI and run again (use --clients to repeat only the missing client counts)." % exc)
    finally:
        try:
            cam.stop_recording()
        except Exception:
            pass
        for step in (lambda: cam.set_config({"recording_resolution": orig[0], "streaming_resolution": orig[1]}),
                     cam.reconfigure_video_pipeline, ctx["audio"].restore):
            try:
                step()
            except Exception as exc:
                print("warning: could not restore a setting: %s" % exc)
        time.sleep(5)
        cleanup_gallery()
        shutil.rmtree(ctx["tmpdir"], ignore_errors=True)
        save(paths, header, cells, len(plan), len(cells) == len(plan), meta)

    bad = [c for c in cells if "BELOW-TARGET" in cell_flags(c)]
    print("\nDone: %d of %d cells, %d below the %.0f %% fps target." % (len(cells), len(plan), len(bad), TARGET_PCT))
    print("Results: %s\n         %s" % paths)

    runner.stop()
    if not aborted and len(cells) == len(plan) and all(c["status"] == "ok" for c in cells):
        offer_reference(paths, reference_issues(version["describe"], cells, client_check_ignored),
                        CPU_TABLE_SECTION, table_row(stem, header))
    else:
        print("The run did not finish cleanly, so the results stay in %s only." % args.out_dir)


if __name__ == "__main__":
    main()
