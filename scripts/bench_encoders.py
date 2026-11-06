#!/usr/bin/env python3
"""
Benchmark: how many H.264 encodes can this Raspberry Pi run in parallel?

Background: a per-client live stream resolution ("auto" mode) would need one
parallel encode per resolution, plus the recording. This script measures what the
hardware really delivers, so the design decision rests on numbers instead of estimates.

How it works
  Every encode is one ffmpeg process fed with a synthetic 1920x1080@30 test pattern
  at real-time speed (-re), scaled (CPU) to the target resolution and encoded with the
  chosen encoder. If the Pi keeps up, each process reports speed ~1.00x and ~30 fps;
  if it is overloaded, speed/fps drop. The total CPU load (all cores = 100 %), the SoC
  temperature and the throttling flags are recorded for every scenario.

  The camera is NOT used, but the hardware H.264 encoder is. Stop CamUI (and any other
  stream/recording) before running, otherwise the numbers are wrong and the live stream
  stutters. The script refuses to run while camera_server.py is running (use --force
  to override).

Usage
  venv/bin/python scripts/bench_encoders.py                  # hardware encoder, all scenarios
  venv/bin/python scripts/bench_encoders.py --encoder libx264 # software encoder
  venv/bin/python scripts/bench_encoders.py --duration 10 --scenario full

Scenarios
  single   one 1080p30 encode (baseline)
  rec_live recording 1080p + one live stream 1280x720
  two_live recording 1080p + two live streams (1280x720, 768x432)
  full     recording 1080p + five live streams (1920x1080, 1536x864, 1280x720,
           1152x648, 768x432)
"""

import argparse
import subprocess
import sys
import time

SOURCE = (1920, 1080)
FPS = 30
BITRATE = "8M"   # same as Camera.BITRATE_ENCODER_STREAM / _RECORDING

SCENARIOS = {
    "single":   [(1920, 1080)],
    "rec_live": [(1920, 1080), (1280, 720)],
    "two_live": [(1920, 1080), (1280, 720), (768, 432)],
    "full":     [(1920, 1080), (1920, 1080), (1536, 864), (1280, 720), (1152, 648), (768, 432)],
}


def cpu_times():
    """(busy, total) jiffies over all cores from /proc/stat."""
    with open("/proc/stat") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    idle = parts[3] + parts[4]          # idle + iowait
    total = sum(parts[:8])              # user..steal
    return total - idle, total


def vcgencmd(*args):
    try:
        return subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return "n/a"


def camera_server_running():
    r = subprocess.run(["pgrep", "-f", "camera_server.py"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def ffmpeg_cmd(size, encoder, duration):
    w, h = size
    vf = ("scale=%d:%d," % (w, h) if size != SOURCE else "") + "format=yuv420p"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1",
           "-re", "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d" % (SOURCE[0], SOURCE[1], FPS),
           "-t", str(duration), "-vf", vf, "-c:v", encoder, "-b:v", BITRATE]
    if encoder == "libx264":
        cmd += ["-preset", "ultrafast", "-tune", "zerolatency"]
    return cmd + ["-f", "null", "-"]


def last_progress(output):
    """Last fps= and speed= values from ffmpeg's -progress output."""
    fps = speed = None
    for line in output.splitlines():
        if line.startswith("fps="):
            try:
                fps = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("speed="):
            try:
                speed = float(line.split("=", 1)[1].rstrip("x "))
            except ValueError:
                pass
    return fps, speed


def run_scenario(name, sizes, encoder, duration):
    print("\n=== %s: %d parallel encode(s) with %s ===" % (name, len(sizes), encoder))
    busy0, total0 = cpu_times()
    t0 = time.time()
    procs = [subprocess.Popen(ffmpeg_cmd(s, encoder, duration), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for s in sizes]
    results = []
    for size, p in zip(sizes, procs):
        out, err = p.communicate()
        fps, speed = last_progress(out)
        results.append((size, p.returncode, fps, speed, err.strip()))
    busy1, total1 = cpu_times()
    cpu = 100.0 * (busy1 - busy0) / max(1, total1 - total0)

    for size, rc, fps, speed, err in results:
        status = "ok" if rc == 0 else "FAILED (%s)" % (err.splitlines()[-1] if err else rc)
        print("  %4dx%-4d  fps=%-6s speed=%-6s %s" % (
            size[0], size[1],
            "%.1f" % fps if fps is not None else "?",
            "%.2fx" % speed if speed is not None else "?", status))
    print("  total CPU: %.0f %% of all cores | %s | throttled: %s | wall: %.0f s" % (
        cpu, vcgencmd("measure_temp"), vcgencmd("get_throttled"), time.time() - t0))
    return results, cpu


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--encoder", default="h264_v4l2m2m", help="ffmpeg encoder (h264_v4l2m2m = Pi 4 hardware, libx264 = software)")
    ap.add_argument("--duration", type=int, default=15, help="seconds per scenario")
    ap.add_argument("--scenario", choices=list(SCENARIOS) + ["all"], default="all")
    ap.add_argument("--force", action="store_true", help="run even if camera_server.py is running")
    args = ap.parse_args()

    if camera_server_running() and not args.force:
        sys.exit("camera_server.py is running - stop CamUI first (or use --force, results will be skewed).")

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    print("Encoder: %s | source %dx%d@%d | bitrate %s | %d s per scenario" % (
        args.encoder, SOURCE[0], SOURCE[1], FPS, BITRATE, args.duration))
    print("Start temperature: %s" % vcgencmd("measure_temp"))

    summary = []
    for n in names:
        results, cpu = run_scenario(n, SCENARIOS[n], args.encoder, args.duration)
        worst = min((s for _, _, _, s, _ in results if s is not None), default=None)
        summary.append((n, len(results), worst, cpu))
        time.sleep(3)   # let the encoder settle between scenarios

    print("\nSummary (worst speed 1.00x = real time, below = cannot keep up):")
    for n, count, worst, cpu in summary:
        print("  %-9s %d encode(s)  worst speed %-6s  CPU %3.0f %%" % (
            n, count, "%.2fx" % worst if worst is not None else "?", cpu))


if __name__ == "__main__":
    main()
