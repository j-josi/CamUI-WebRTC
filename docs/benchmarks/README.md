# Benchmark results

Reference results of the benchmark scripts for different hardware and releases. They
allow comparing Raspberry Pi models, cameras, microphones and app versions.

| Script | Measures | Needs |
|---|---|---|
| `scripts/bench_cpu.py` | CPU load and recording frame rate of the running app with live stream, audio and connected browsers | camera, optionally a microphone and browsers |
| `scripts/bench_encoders.py` | How many H.264 encodes the hardware runs in parallel in real time | no camera; CamUI must be stopped |

Run both from the repository root. `--help` lists all options of a script.

## Result files

Each run writes two files to the git-ignored `benchmarks/` folder:

```
bench_<script>_<app version>_<pi model>_<timestamp>.txt    readable log
bench_<script>_<app version>_<pi model>_<timestamp>.json   the same data, machine readable
```

The app version is the output of `git describe --tags --dirty` (for example `v0.2.0`
for a clean release, `v0.2.0-3-g1a2b3c4-dirty` for a development state). The `.txt`
file starts with a header (date, release, app version, Pi model, OS and kernel,
hardware, and for `bench_cpu.py` camera sensor, microphone and network type) followed by
the result table.

## Keeping a result as a reference

When a run finishes without errors, the script asks where to keep it:

- **Local only** (default): the files stay in `benchmarks/`, which git ignores.
- **Reference result**: the files are also copied to this folder, and a row is added to
  the table of the script below. Review the files and commit them.

Choose this only if:

- the app version in the header has no `-dirty` suffix, so the measured code can be
  reconstructed from git;
- no result is flagged `THROTTLED`, `SHORT-FILE`, `NO-AUDIO-TRACK`, `FAILED` or `ERROR`;
- nothing else loaded the Pi (for `bench_cpu.py` the `other:` column shows no large
  foreign CPU users, no IDE, no `htop`, and the rows for 0 clients were measured without
  any connected browser);
- the file, including a label if you set one, contains no private information.

The script lists the criteria a run does not meet before it asks.

## CPU and frame rate (`scripts/bench_cpu.py`)

```bash
# Default measurement: recording at the highest resolution, with the largest and the
# third-largest live stream resolution of the camera (1080p and 720p for a Camera
# Module 3), audio on and off, and 0 and 3 connected web clients.
# 16 measurements, run duration about 12 minutes.
venv/bin/python scripts/bench_cpu.py

# The same measurement with the default values written out. All flags of this command
# are the default values, so it has the same effect as the command above.
venv/bin/python scripts/bench_cpu.py --clients 0,3 --stream-indexes 0,2

# Full sweep: 0, 1, 2 and 3 connected web clients, all available live stream
# resolutions, audio on and off.
# 80 measurements, run duration about one hour.
venv/bin/python scripts/bench_cpu.py --clients 0,1,2,3 --stream-indexes all

# Raspberry Pi without a microphone: the default measurement with audio off only.
# 8 measurements, run duration about 6 minutes.
venv/bin/python scripts/bench_cpu.py --audio off
```

The script starts CamUI, MediaMTX and coturn if they are not running and stops what it
started. Before each client count it asks you to connect that many browser tabs or
devices to the live view and waits for Enter. `--label` adds a free-text line to the
header (for example the kind of client devices).

### CPU benchmark results

| File | Pi model | Camera | Microphone | App version | Date | Network |
|---|---|---|---|---|---|---|

## Hardware encoders (`scripts/bench_encoders.py`)

The script runs synthetic 1080p30 encodes with ffmpeg, in four scenarios from one
encode up to a recording plus five live streams, and reports for each scenario the
speed of the slowest encode (1.00x is real time). A scenario counts as real time from
0.98x. It does not use the camera, but it needs the hardware encoder to itself: stop
CamUI first.

```bash
# Default measurement: hardware encoder (h264_v4l2m2m), all four scenarios, 15 seconds
# each. Run duration about 2 minutes.
venv/bin/python scripts/bench_encoders.py

# The same scenarios with the software encoder (libx264) for comparison, for example on
# a Raspberry Pi 5 that has no hardware H.264 encoder.
venv/bin/python scripts/bench_encoders.py --encoder libx264
```

### Encoder benchmark results

| File | Pi model | Encoder | App version | Date | Real-time capacity |
|---|---|---|---|---|---|
