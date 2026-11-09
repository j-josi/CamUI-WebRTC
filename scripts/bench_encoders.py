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
  if it is overloaded, speed/fps drop. A scenario counts as "real time" when the slowest
  encode reaches at least 0.98x. The total CPU load (all cores = 100 %), the SoC
  temperature and the throttling flags are recorded for every scenario.

  The camera is NOT used, but the hardware H.264 encoder is. Stop CamUI (and any other
  stream/recording) before running, otherwise the numbers are wrong and the live stream
  stutters. The script refuses to run while camera_server.py is running (use --force
  to override).

Result files (default: <repo>/benchmarks/, which is git-ignored):
  bench_encoders_<app version>_<pi model>_<timestamp>.txt    header (timestamp, release, app
                                  version, Pi model, OS/kernel, encoder, ...) and result table
  bench_encoders_<app version>_<pi model>_<timestamp>.json   the same data, machine readable
After a run that finished without failed encodes the script asks whether the result should
also be kept as a reference result: it is then copied to docs/benchmarks/ (tracked by git)
and a row is added to the table "Encoder benchmark results" in the README there.

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
import datetime
import os
import subprocess
import sys
import time

from bench_common import (ROOT, cpu_times, header_value, offer_reference, pi_model, render_header, render_table,
                          result_stem, run, save_results, system_items, throttle_now, timestamp_items,
                          vc_temp, vc_throttled, version_issue, app_version)

SOURCE = (1920, 1080)
FPS = 30
BITRATE = "8M"   # same as Camera.BITRATE_ENCODER_STREAM / _RECORDING
REAL_TIME_SPEED = 0.98
TABLE_SECTION = "Encoder benchmark results"      # heading above the table in docs/benchmarks/README.md

SCENARIOS = {
    "single":   [(1920, 1080)],
    "rec_live": [(1920, 1080), (1280, 720)],
    "two_live": [(1920, 1080), (1280, 720), (768, 432)],
    "full":     [(1920, 1080), (1920, 1080), (1536, 864), (1280, 720), (1152, 648), (768, 432)],
}


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


def scenario_result(name, encodes, cpu, temp, throttled, wall_s):
    """encodes: [{size, returncode, fps, speed, error}] -> result dict with worst speed and real-time verdict."""
    speeds = [e["speed"] for e in encodes]
    worst = min(speeds) if speeds and all(s is not None for s in speeds) else None
    failed = any(e["returncode"] != 0 for e in encodes)
    return {"scenario": name, "encodes": encodes, "count": len(encodes), "worst_speed": worst,
            "failed": failed, "real_time": (worst is not None and worst >= REAL_TIME_SPEED and not failed),
            "cpu_total": cpu, "temp_end": temp, "throttled": throttled,
            "throttled_now": throttle_now(throttled), "wall_s": wall_s}


def max_real_time(results):
    """(parallel encodes, scenario) of the largest scenario that ran in real time, or None."""
    ok = [r for r in results if r["real_time"]]
    if not ok:
        return None
    best = max(ok, key=lambda r: r["count"])
    return best["count"], best["scenario"]


def capacity_text(results):
    best = max_real_time(results)
    return "up to %d parallel encodes in real time (%s)" % best if best else "no scenario in real time"


def result_flags(r):
    flags = []
    if r["failed"]:
        flags.append("FAILED")
    if r["throttled_now"]:
        flags.append("THROTTLED")
    if not r["failed"] and not r["real_time"]:
        flags.append("NOT-REAL-TIME")
    return flags


def reference_issues(version, results, forced_with_camera_server):
    """Reasons why a run does not meet the criteria for a reference result (see docs/benchmarks/README.md)."""
    issues = [i for i in (version_issue(version),) if i]
    bad = sorted({f for r in results for f in result_flags(r) if f == "THROTTLED"})
    if bad:
        issues.append("scenarios are flagged %s" % ", ".join(bad))
    if forced_with_camera_server:
        issues.append("camera_server.py was running (--force), which skews the numbers")
    return issues


def table_row(stem, header, results):
    """One row for the table 'Encoder benchmark results' in docs/benchmarks/README.md."""
    cols = ["[%s](%s.txt)" % (stem, stem), header_value(header, "Raspberry Pi model"), header_value(header, "Encoder"),
            header_value(header, "App version (git describe)"), header_value(header, "Timestamp (local)")[:10],
            capacity_text(results)]
    return "| " + " | ".join(c.replace("|", "/") for c in cols) + " |"


def render_log(header, results, planned, complete):
    """Plain-text log: header block, scenario table, per-encode details, legend."""
    lines = render_header("CamUI-WebRTC hardware encoder benchmark", header)
    lines += ["", "[results]  %d of %d scenarios%s" % (len(results), planned, "" if complete else "  (run not finished)"), ""]
    rows = [(r["scenario"], str(r["count"]), "%.2fx" % r["worst_speed"] if r["worst_speed"] is not None else "-",
             "%.0f" % r["cpu_total"], "%.0f" % r["temp_end"] if r["temp_end"] is not None else "-",
             " ".join(result_flags(r))) for r in results]
    lines += render_table(("scenario", "encodes", "worst speed", "CPU%", "temp", "flags"), rows)
    if results:
        lines += ["", "Per encode"]
        for r in results:
            lines.append("  %s" % r["scenario"])
            for e in r["encodes"]:
                lines.append("    %-10s fps %-6s speed %-6s %s" % (
                    e["size"], "%.1f" % e["fps"] if e["fps"] is not None else "?",
                    "%.2fx" % e["speed"] if e["speed"] is not None else "?", e["error"] or "ok"))
    lines += [
        "",
        "Legend",
        "  encodes      number of parallel ffmpeg encodes (the first one stands for the recording)",
        "  worst speed  speed of the slowest encode; 1.00x = real time",
        "  CPU%         whole system, all cores = 100 %",
        "  temp         SoC temperature at the end of the scenario (deg C)",
        "  flags        NOT-REAL-TIME worst speed < %.2fx | THROTTLED under-voltage/throttling during the scenario |"
        " FAILED an ffmpeg process exited with an error" % REAL_TIME_SPEED,
        "  summary      %s" % capacity_text(results),
        "",
    ]
    return "\n".join(lines)


def run_scenario(name, sizes, encoder, duration):
    print("\n=== %s: %d parallel encode(s) with %s ===" % (name, len(sizes), encoder))
    busy0, total0 = cpu_times()
    t0 = time.time()
    procs = [subprocess.Popen(ffmpeg_cmd(s, encoder, duration), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for s in sizes]
    encodes = []
    for size, p in zip(sizes, procs):
        out, err = p.communicate()
        fps, speed = last_progress(out)
        error = "" if p.returncode == 0 else "FAILED (%s)" % (err.strip().splitlines()[-1] if err.strip() else p.returncode)
        encodes.append({"size": "%dx%d" % size, "returncode": p.returncode, "fps": fps, "speed": speed, "error": error})
    busy1, total1 = cpu_times()
    cpu = 100.0 * (busy1 - busy0) / max(1, total1 - total0)
    result = scenario_result(name, encodes, cpu, vc_temp(), vc_throttled(), time.time() - t0)

    for e in encodes:
        print("  %-10s fps=%-6s speed=%-6s %s" % (
            e["size"], "%.1f" % e["fps"] if e["fps"] is not None else "?",
            "%.2fx" % e["speed"] if e["speed"] is not None else "?", e["error"] or "ok"))
    print("  total CPU: %.0f %% of all cores | %s C | throttled: %s | wall: %.0f s" % (
        cpu, result["temp_end"], result["throttled"], result["wall_s"]))
    return result


def build_header(args, names):
    now = datetime.datetime.now().astimezone()
    hardware, software = system_items()
    ffmpeg = (run(["ffmpeg", "-version"]).splitlines() or ["unknown"])[0]
    header = [
        ("benchmark", timestamp_items(now) + [
            ("Label", args.label or "-"),
            ("Encoder", args.encoder),
            ("Test source", "testsrc2 %dx%d at %d fps, real-time speed (-re)" % (SOURCE[0], SOURCE[1], FPS)),
            ("Bitrate per encode", BITRATE),
            ("Duration per scenario", "%d s" % args.duration),
            ("Scenarios", ", ".join(names)),
            ("camera_server.py running", "yes (--force)" if args.force and camera_server_running() else "no"),
            ("Load average at start", "%.2f %.2f %.2f" % os.getloadavg()),
        ]),
        ("hardware", hardware),
        ("software", software + [("ffmpeg", ffmpeg)]),
    ]
    return header, now


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--encoder", default="h264_v4l2m2m", help="ffmpeg encoder (h264_v4l2m2m = Pi 4 hardware, libx264 = software)")
    ap.add_argument("--duration", type=int, default=15, help="seconds per scenario")
    ap.add_argument("--scenario", choices=list(SCENARIOS) + ["all"], default="all")
    ap.add_argument("--force", action="store_true", help="run even if camera_server.py is running")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "benchmarks"))
    ap.add_argument("--label", default="", help="free text for the header")
    args = ap.parse_args()

    forced = camera_server_running()
    if forced and not args.force:
        sys.exit("camera_server.py is running - stop CamUI first (or use --force, results will be skewed).")

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    header, started = build_header(args, names)
    version = app_version()
    stem = result_stem("encoders", version["describe"], pi_model(), started.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(args.out_dir, exist_ok=True)
    paths = (os.path.join(args.out_dir, stem + ".txt"), os.path.join(args.out_dir, stem + ".json"))
    meta = {"args": vars(args)}

    def save(results, complete):
        save_results(paths, render_log(header, results, len(names), complete),
                     {"header": [{"section": s, "items": dict(i)} for s, i in header], "meta": meta,
                      "complete": complete, "planned_scenarios": len(names), "results": results})

    print("Encoder: %s | source %dx%d@%d | bitrate %s | %d s per scenario" % (
        args.encoder, SOURCE[0], SOURCE[1], FPS, BITRATE, args.duration))
    print("Start temperature: %s C" % vc_temp())

    results = []
    try:
        for n in names:
            results.append(run_scenario(n, SCENARIOS[n], args.encoder, args.duration))
            save(results, len(results) == len(names))
            time.sleep(3)   # let the encoder settle between scenarios
    except KeyboardInterrupt:
        print("\nAborted - saving what was measured so far.")
        save(results, False)

    print("\nSummary (worst speed 1.00x = real time, below = cannot keep up):")
    for r in results:
        print("  %-9s %d encode(s)  worst speed %-6s  CPU %3.0f %%  %s" % (
            r["scenario"], r["count"], "%.2fx" % r["worst_speed"] if r["worst_speed"] is not None else "?",
            r["cpu_total"], " ".join(result_flags(r))))
    print("  -> %s" % capacity_text(results))
    print("Results: %s\n         %s" % paths)

    if len(results) == len(names) and not any(r["failed"] for r in results):
        offer_reference(paths, reference_issues(version["describe"], results, forced),
                        TABLE_SECTION, table_row(stem, header, results))
    else:
        print("The run did not finish cleanly, so the results stay in %s only." % args.out_dir)


if __name__ == "__main__":
    main()
