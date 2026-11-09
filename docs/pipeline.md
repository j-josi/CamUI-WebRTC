# Media pipeline and CPU cost

This document describes what happens between the camera sensor and the `<video>`
element in the browser while a video is recorded to an MP4 file and streamed live
at the same time, and what each step costs on a Raspberry Pi 4. It is meant for
developers who change the camera pipeline, the encoders or the streaming setup.
The recording always has priority over the live stream: if resources run short,
the live stream gives way.

Reference numbers were measured on a Raspberry Pi Compute Module 4 Rev 1.1
(2 GB, Raspberry Pi OS Trixie, kernel 6.12) with a Camera Module 3 (imx708) and a
USB microphone, app version `v0.1.0-10-g2900347-dirty`, 2026-11-07. Use
[`scripts/bench_cpu.py`](../scripts/bench_cpu.py) to reproduce them on other hardware.

## Overview

```
Sensor imx708
  │ CSI-2 (raw Bayer, 10 bit)
  ▼
[1] Unicam (hardware, DMA)  ──► raw frame in CMA memory
  ▼
[2] libcamera + IPA (CPU: auto exposure, white balance, autofocus)
  ▼
[3] ISP (hardware): raw → YUV 4:2:0, two outputs in parallel
      │ main  (the larger of the two resolutions)
      │ lores (the smaller one)
      │ each is either the recording or the live stream size
      ▼
[4] Picamera2 request loop (Python, inside the camera_server process)
      │ passes each encoder the output of its resolution
      │                                   │
      ▼ live stream                       ▼ recording
[5a] H264Encoder → hardware encoder   [5b] H264Encoder → hardware encoder
     (VPU), delivers finished H.264        (VPU), delivers finished H.264
      ▼                                   ▼
[6a] PyavOutput (libav, in-process)   [6b] PyavOutput (libav, in-process)
     RTSP muxer → RTP packets             MP4 muxer → .mp4 file
     + Opus audio                         + AAC audio
      ▼                                   ▼
[7] RTSP to 127.0.0.1:8554               write to static/gallery/
      ▼
[8] MediaMTX (separate container process)
      │ accepts the stream, repackages it per client (DTLS/SRTP)
      ▼
[9] coturn (TURN relay) and Wi-Fi/Ethernet driver
      ▼
[10] Browser: WebRTC receive, H.264/Opus decode, <video> element

Audio (one reader per encoder, independent of each other):
  microphone → PipeWire/PulseAudio → PyAV reads PCM → Opus (live) or AAC (file)
  → packets go to the same PyavOutput as the video of that encoder
```

## What happens in each step

### Who does the work: dedicated chips versus the CPU

The CPU is the general-purpose processor: it can run any program, but none of them
particularly efficiently. The Pi 4's main chip (the SoC, a BCM2711) therefore also
contains **dedicated circuits that do only one job**, and do it far more efficiently
than the CPU could, with practically no CPU time. This document calls them "hardware" in contrast to
"CPU". They are part of the SoC on the Raspberry Pi board, not of the camera module
(which only delivers raw sensor data). Three of them matter here:

- **Unicam**: receives the sensor data and stores it in memory.
- **ISP** (image signal processor): turns raw sensor data into a normal picture.
- **VPU** (VideoCore, the SoC's video processor) with its **H.264 encoder**: compresses
  pictures into a video stream.

Everything else, including packaging, encrypting and sending, runs on the CPU.

> **Note:** The Raspberry Pi 5 has no H.264 encoder, so its CPU has to do the
> compression in software (`bench_encoders.py --encoder libx264` measures this). All
> older Raspberry Pi models, back to the Raspberry Pi 1, have an H.264 encoder in
> their VPU.

### The video path

1. **Sensor to memory (Unicam).** The camera sensor sends raw pixel data over the
   camera cable (**CSI-2**, the Pi's camera interface). The data is raw "Bayer"
   data: one colour value per pixel, not yet a picture. Unicam writes each frame
   straight into a reserved memory area (**CMA**, contiguous memory) using **DMA**
   (direct memory access: a chip copies data into memory without the CPU).
2. **Image control (libcamera + IPA).** libcamera is the Linux camera library.
   Its **IPA** (image processing algorithms) module looks at every frame and
   decides exposure time, gain, white balance and focus for the next ones
   ("auto exposure, auto white balance, autofocus"). This is light CPU work.
3. **Picture creation (ISP).** The ISP turns the raw data into a viewable picture in
   **YUV 4:2:0** format (brightness for each pixel, colour at half resolution, as
   video encoders expect it). It produces two sizes of every frame at the same time:
   the *main* output (always the larger of the two configured resolutions) and the
   *lores* output (the smaller one). Which of them is the recording and which is the
   live stream depends on the settings: the live stream may also be larger than the
   recording. If both resolutions are equal, both outputs have the same size.
4. **Frame distribution (Picamera2 request loop).** [**Picamera2**](https://deepwiki.com/raspberrypi/picamera2/1-overview)
   is a Python interface to control cameras connected to the Raspberry Pi. It builds on libcamera: the program tells it which resolutions to use,
   sets camera controls (exposure, gain, ...), and receives the finished frames.
   It also contains ready-made classes that compress frames with the hardware
   encoder and write the result to a file or a stream. CamUI runs it in its own process, `camera_server.py` (the "camera
   process"), which the web application controls over a local socket. For each frame,
   libcamera hands Picamera2 a finished "request" (the frame in both sizes plus its
   metadata). The Picamera2 loop passes each encoder the output with its resolution
   and returns the memory buffers afterwards. Most of the CPU load of this
   step comes from the operating system moving these buffers around.
5. **Video compression (H264Encoder → hardware encoder).** Picamera2's `H264Encoder`
   gives each frame to the H.264 encoder chip of the VPU (through the Linux
   `v4l2` video driver). **H.264** is the video format that shrinks the raw
   pictures roughly a hundredfold so they fit into a file or a network stream. The chip returns the compressed
   data; the CPU only passes buffers back and forth. The live stream and the
   recording each have their own encoder. How many encodes run in parallel is
   limited by the chip: on the Pi 4 at most three H.264 encodes at once, and two
   1080p encodes already come close to the limit (measured). For the older models
   no fixed number of encodes is documented. According to a Raspberry Pi engineer
   the encoder runs several encodes at the same time, but their *total* is limited
   to about one 1080p30 stream (for example two 720p30 streams), and the Pi 4 is somewhat faster than the older models
   ([forum thread](https://forums.raspberrypi.com/viewtopic.php?t=352713), not
   measured by us). `scripts/bench_encoders.py` determines the limit of the Pi it runs on.
6. **Packaging (PyavOutput with libav).** Compressed video and audio are still
   separate data. A **muxer** puts them into one file or stream format.
   **FFmpeg** is a widely used media toolkit. It consists of the libraries **libav**
   (`libavformat` with the muxers, `libavcodec` with the audio compression) and the
   command-line program `ffmpeg` that uses them. CamUI only uses the libav libraries:
   **PyAV** is a Python wrapper around the libav libraries, and **`PyavOutput`** is the Picamera2
   class that feeds the encoder's output into PyAV. libav runs as part of the
   camera process; no separate `ffmpeg` program (process) is started.
   - *Recording (6b):* the **MP4 muxer** writes video and AAC audio into an `.mp4` file.
   - *Live (6a):* the **RTSP muxer** cuts video and Opus audio into **RTP** packets
     (small numbered network packets with timing information, the format
     real-time streams use) and sends them over **RTSP** (Real Time Streaming
     Protocol, here used only to push a stream to a server).
7. **Hand-over.** The recording is written to disk (`static/gallery/`). The RTP
   packets of the live stream (the continuous sequence of small packets with video
   and audio) are sent over an RTSP connection on the Pi itself (address
   `127.0.0.1`, port 8554) to MediaMTX.
8. **Distribution (MediaMTX).** MediaMTX is a media server. It runs as a separate
   Docker container and receives the RTSP stream once and offers it to all connected clients/webbrowsers over
   **WebRTC** (a set of network protocols for transmitting live video and audio with very
   low delay (usually well under a second) and high browser compability. A browser starts WebRTC
   playback with an HTTP request called **WHEP**, the WebRTC-HTTP Egress Protocol). It does not re-encode: it re-packs the
   RTP packets and encrypts them separately for every connected browser
   (**DTLS** negotiates the keys, **SRTP** is the encrypted RTP).
9. **Network (coturn, Wi-Fi/Ethernet).** A WebRTC connection first looks for a
   network path between browser and MediaMTX (this search is called *ICE*): the browser
   and MediaMTX each list the addresses they can be reached at, try the pairs and
   use the best one that works. A **TURN relay** is an extra server that both sides
   can reach and that forwards the packets between them, as a fallback when no
   direct path works. **coturn** is such a relay; CamUI runs it as a second Docker
   container on the Pi. The page always offers it to the browser
   (`turn:<pi address>:3478`, over TCP), but the video only travels through coturn if
   ICE chooses that path; otherwise the packets go directly from MediaMTX to the
   browser. CamUI adds the relay because Firefox hides its own addresses behind
   random `.local` names (a privacy feature), which MediaMTX cannot resolve, so
   the direct path often fails there; the address of the relay is reachable and works.
   Chrome-based browsers usually connect directly. Afterwards the network driver sends
   the packets out over Wi-Fi or Ethernet.
10. **Browser.** The browser receives the stream, decodes the H.264 video and the
    Opus audio, and shows them in the `<video>` element.

### The audio path

The microphone is handled separately for the recording and for the live stream
(two readers, two encoders):

1. The sound server (**PipeWire/PulseAudio**, which manages audio devices on the
   Pi) delivers the microphone signal. PyAV reads it as **PCM**, which is raw,
   uncompressed audio samples. In PyAV this reading is called "decoding", but no
   decompression takes place.
2. The CPU compresses the samples: **AAC** for the MP4 file (the audio format
   MP4 players expect) and **Opus** for the live stream (the audio format WebRTC
   uses, designed for low delay).
3. The compressed audio packets go to the same `PyavOutput` as the video of that
   encoder (step 6), which interleaves both.

## Things that are easy to get wrong

1. **The Pi never decodes video.** The ISP delivers raw YUV frames, the VPU's
   encoder chip turns them into H.264. Only the browser decodes. For audio,
   "decode" in PyAV just means reading raw PCM samples from the microphone; the
   compression to Opus/AAC happens afterwards, on the CPU.
2. **Muxing is done by the libav library inside the Python process** (through
   PyAV), not by the `ffmpeg` program. No `ffmpeg` process takes part in a
   recording or in the live stream. The `ffmpeg`/`ffprobe` programs are only used
   for side jobs such as thumbnails and file inspection. Those must not run at
   normal priority during a recording, because they cost the recording frames.
3. **RTSP only runs in one direction.** `PyavOutput` uses libav's RTSP muxer and
   publishes the stream to MediaMTX (loopback). The Python process never reads the
   stream back. MediaMTX is the RTSP server; it does not convert anything that
   `PyavOutput` produced.
4. **MediaMTX does not transcode.** H.264 and Opus are passed through; MediaMTX
   repackages the RTP packets and encrypts them for every WebRTC client (WHEP on
   port 8889). Only paths listed in `config/mediamtx/mediamtx.yml` accept
   publishers: `cam<N>` carries the video, plus audio if a microphone is selected.
5. **Flask/gunicorn never touches media data.** It serves the page, the WHEP URL
   and the controls.
6. **The recording does not depend on MediaMTX.** `PyavOutput` writes the MP4 file
   directly. A recording ran at its full frame rate while MediaMTX was not running (MediaMTX
   down from the start of the stream). What happens when MediaMTX stops in the
   middle of a recording was not tested. After an outage the live stream encoder
   can be left in a broken state until CamUI is restarted.

## CPU cost per step

"CPU cost" means the share of processor time a step uses. Percentages are of
**one core** (100 % = one core, the Pi 4 has four). "Measured"
numbers come from per-thread/per-process sampling of `/proc`; "estimated" numbers
are derived from them. Thread roles come from `py-spy dump`.

| Step | CPU | Source |
|---|---|---|
| 1 Unicam / CSI-2 (DMA) | about 0 | dedicated chip, estimated |
| 2 libcamera + IPA (auto exposure, white balance, autofocus) | 3–6 % | measured (`IPAProxyRPi`, `CameraManager` threads) |
| 3 ISP | about 0 CPU, but memory bandwidth | dedicated chip, estimated |
| 4 Picamera2 request loop | 20–28 %, almost all kernel time | measured (`thread_func` plus `listen`, 10–15 %) |
| 5 H.264 encoding (VPU encoder chip) | about 0 for the compression itself; the buffer handling is counted in 4 and 6 | dedicated chip, estimated |
| 6b MP4 muxer and file write | 3–4 % | measured (recording `thread_poll`) |
| 6a/7 RTSP muxer, RTP packetizing, send | about 9–10 % | measured (live `thread_poll`) |
| Audio: read microphone | 1–3.5 % per reader | measured |
| Audio: AAC compression (recording) | 10–17 % | measured |
| Audio: Opus compression (live) | 5–12 % | measured |
| 8 MediaMTX | about 7 % without clients, +8–10 % per client at 720p | measured |
| 9 coturn | below 1 % | measured |
| 9 Wi-Fi driver (`brcmfmac`) | 2–8 %, grows with the number of clients | measured |
| 10 Browser decode and display | not on the Pi | – |

Whole-system CPU (all four cores = 100 %, idle about 4.5 %), recording 1080p30:

| Setup | CPU total |
|---|---|
| native `rpicam-vid`, recording only | 8.4 % |
| Picamera2, recording only, no audio | about 14–16 % |
| + live stream 1080p, no audio, 0 clients | about 24 % |
| + audio (AAC and Opus), 0 clients | about 37 % |
| + 3 clients | about 60 % |

### Where the CPU load comes from

- The big items are the Picamera2 request loop with its buffer handling (mostly
  kernel time), both audio encoders, and MediaMTX per client. The video
  compression itself is nearly free for the CPU, because the VPU's dedicated
  H.264 encoder chip does it (see "Who does the work").
- Adding the live stream (1080p, no audio, 0 clients) costs +8.5 percentage
  points in total: about 45 % kernel time in the camera process, 32 % user time
  there (libav RTP packetizing, Python), 22 % MediaMTX.
- Audio is the most surprising item. The encoders are cheap in isolation (AAC
  10.6 %, Opus 5.1 % of one core) but cost 17 % and 11.6 % while the camera
  pipeline runs. The same work is slower when ISP, encoder and DMA load the
  memory system; CPU frequency (1.5 GHz in both cases) and throttling are ruled
  out. Whether memory bandwidth or the shared cache is the cause was not isolated.
- `aac_coder=fast` and Opus `compression_level=0` save only 1–1.5 percentage
  points of total CPU, so they are not used.
- The microphone is read twice (once per encoder). Sharing one reader would save
  about 1 percentage point of total CPU and put the recording's audio at risk
  (shared back-pressure, custom audio thread inside Picamera2), so it was
  deliberately not done.

## Recording frame rate under load

Measured with 1080p30 recording, frame rate = video packets ÷ file duration
(`ffprobe -count_packets`), target ≥ 98 % of 30 fps:

| Clients | live 720p, no audio | live 1080p, no audio | live 1080p, with audio |
|---|---|---|---|
| 0 | 29.95 | 29.57 / 29.66 | 29.66 / 29.83 |
| 1 | 30.00 / 29.92 | 29.75 / 29.55 | not measured |
| 2 | 30.00 / 29.97 | 29.63 / 29.92 | not measured |
| 3 | 30.00 | 29.55 / 29.37 | **28.86 / 28.86** |

The critical case (live 1080p, audio, 3 clients, 59–64 % total CPU) misses the
target. A plain Picamera2 recording without any live stream reaches about
29.8–30.0 fps on this hardware.

## Measuring

`scripts/bench_cpu.py` runs the scenarios systematically (recording and live
resolution, audio on/off, any number of browser clients; by default 0 and 3 clients and the live
resolutions 0 and 2) and writes a log file with a header
that identifies the hardware and the app version, so results from different
machines can be compared. See its docstring for usage. Every measured cell reports
the recording frame rate, the CPU use of the whole system, of the busiest single
core (`core max`) and of the camera server, MediaMTX, coturn and gunicorn. If the frame
rate drops while both the total and `core max` stay well below 100 %, the CPU is not the
limit; the encoder chip or the memory bandwidth is the more likely cause.

Raw runs are written to the git-ignored `benchmarks/` folder. Runs that are meant as
reference values are kept in [docs/benchmarks/](benchmarks/README.md).

Notes for reliable comparisons:

- Stop everything else on the Pi (IDE, `htop`, other containers). The script lists
  the largest foreign CPU users per cell.
- Keep the browser tabs visible while a client count is being measured.
- Compare only runs with the same app version, camera, microphone and network
  type (Wi-Fi clients cost more than Ethernet clients).
