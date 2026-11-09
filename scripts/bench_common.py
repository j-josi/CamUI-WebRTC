"""
Helpers shared by the benchmark scripts (bench_cpu.py, bench_encoders.py): system
information for the result header, result file names and writing, and the question
whether a result is kept as a git-tracked reference result in docs/benchmarks/.
"""
import datetime
import json
import os
import platform
import re
import shutil
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DOCS_BENCHMARKS = os.path.join(ROOT, "docs", "benchmarks")


# --------------------------------------------------------------------------
# Pure helpers (unit tested in tests/test_bench_common.py)
# --------------------------------------------------------------------------

def slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "unknown"


def safe_name(text):
    """File-name safe form that keeps dots (versions such as v0.1.0)."""
    return re.sub(r"[^A-Za-z0-9.+_-]+", "-", text).strip("-") or "unknown"


def result_stem(kind, version, model, stamp):
    """bench_<kind>_<app version>_<pi model>_<timestamp>: the version comes first so runs of one release sort together."""
    return "bench_%s_%s_%s_%s" % (kind, safe_name(version), slug(model), stamp)


def header_value(header, key, default="unknown"):
    for _, items in header:
        for k, v in items:
            if k == key:
                return v
    return default


def throttle_now(hexstr):
    """True if the 'currently' bits of vcgencmd get_throttled are set
    (under-voltage 0x1, frequency capped 0x2, throttled 0x4, soft temp limit 0x8)."""
    try:
        return bool(int(hexstr, 16) & 0xF)
    except (TypeError, ValueError):
        return False


def render_header(title, header):
    """Header block of a log file. header: [(section, [(key, value)])]."""
    lines = [title, "=" * len(title)]
    for section, items in header:
        lines.append("")
        lines.append("[%s]" % section)
        width = max((len(k) for k, _ in items), default=0)
        for k, v in items:
            lines.append("  %-*s  %s" % (width, k, v))
    return lines


def render_table(cols, rows):
    """Fixed-width table as a list of lines."""
    widths = [max(len(cols[j]), *(len(r[j]) for r in rows)) if rows else len(cols[j]) for j in range(len(cols))]
    lines = ["  ".join(cols[j].ljust(widths[j]) for j in range(len(cols))).rstrip(),
             "  ".join("-" * widths[j] for j in range(len(cols)))]
    lines += ["  ".join(r[j].ljust(widths[j]) for j in range(len(cols))).rstrip() for r in rows]
    return lines


def version_issue(version):
    """Reason why a version string does not identify the measured code, or None."""
    if version.endswith("-dirty") or version == "unknown":
        return "the app version %r does not identify the measured code (uncommitted changes)" % version
    return None


def append_table_row(readme_text, row, section):
    """Insert row after the last table line below the heading 'section'; unchanged if the file is already listed."""
    lines = readme_text.rstrip("\n").split("\n")
    stem_link = row.split("](", 1)[0] + "]("
    if any(l.startswith("|") and stem_link in l for l in lines):
        return readme_text
    start = next((i for i, l in enumerate(lines) if l.startswith("#") and l.lstrip("# ").strip() == section), None)
    if start is None:
        raise ValueError("no heading %r found" % section)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    rows = [i for i in range(start + 1, end) if lines[i].startswith("|")]
    if not rows:
        raise ValueError("no table found below the heading %r" % section)
    lines.insert(rows[-1] + 1, row)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# System information (every collector degrades to "unknown")
# --------------------------------------------------------------------------

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\0", b"").decode(errors="replace").strip()
    except OSError:
        return ""


def pi_model():
    return read("/proc/device-tree/model") or platform.machine()


def vc_temp():
    out = run(["vcgencmd", "measure_temp"])
    m = re.search(r"([\d.]+)", out)
    if m:
        return float(m.group(1))
    raw = read("/sys/class/thermal/thermal_zone0/temp")
    return float(raw) / 1000 if raw.isdigit() else None


def vc_throttled():
    out = run(["vcgencmd", "get_throttled"])
    return out.split("=", 1)[1] if "=" in out else None


def cpu_times():
    """(busy, total) jiffies over all cores from /proc/stat."""
    with open("/proc/stat") as f:
        p = [int(x) for x in f.readline().split()[1:]]
    return sum(p[:8]) - p[3] - p[4], sum(p[:8])


def parse_core_times(stat_text):
    """[(busy, total)] jiffies per core from the text of /proc/stat (the 'cpu0', 'cpu1', ... lines)."""
    cores = []
    for line in stat_text.splitlines():
        name = line.split(" ", 1)[0]
        if name.startswith("cpu") and name[3:].isdigit():
            p = [int(x) for x in line.split()[1:]]
            cores.append((sum(p[:8]) - p[3] - p[4], sum(p[:8])))
    return cores


def core_times():
    with open("/proc/stat") as f:
        return parse_core_times(f.read())


def busiest_core_percent(before, after):
    """Highest load of a single core between two core_times() snapshots (100 % = that core fully busy)."""
    loads = [100.0 * (b1 - b0) / max(1, t1 - t0) for (b0, t0), (b1, t1) in zip(before, after)]
    return max(loads, default=0.0)


def app_version():
    """Version of the code under test: git describe (release tag, commits since it, commit, -dirty)."""
    def git(*cmd):
        return run(["git", "-C", ROOT] + list(cmd)) or "unknown"
    return {"describe": git("describe", "--tags", "--always", "--dirty"),
            "release": git("describe", "--tags", "--abbrev=0"),
            "commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD")}


def package_version(pkg_glob):
    out = run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n", pkg_glob])
    lines = [l.strip() for l in out.splitlines() if len(l.split()) == 2]   # virtual packages have no version
    return "; ".join(lines) if lines else "unknown"


def timestamp_items(now):
    return [("Timestamp (local)", now.isoformat(timespec="seconds")),
            ("Timestamp (UTC)", now.astimezone(datetime.timezone.utc).isoformat(timespec="seconds"))]


def system_items():
    """(hardware items, software items) for the result header."""
    mem = re.search(r"MemTotal:\s+(\d+)", read("/proc/meminfo"))
    rev = re.search(r"Revision\s*:\s*(\S+)", read("/proc/cpuinfo"))
    os_name = re.search(r'PRETTY_NAME="?([^"\n]+)', read("/etc/os-release"))
    cpufreq = "/sys/devices/system/cpu/cpu0/cpufreq/"
    fmax, fcur = read(cpufreq + "cpuinfo_max_freq"), read(cpufreq + "scaling_cur_freq")
    ver = app_version()
    hardware = [
        ("Raspberry Pi model", pi_model()),
        ("Board revision", rev.group(1) if rev else "unknown"),
        ("CPU cores", str(os.cpu_count())),
        ("RAM", "%d MB" % (int(mem.group(1)) // 1024) if mem else "unknown"),
        ("CPU governor", read(cpufreq + "scaling_governor") or "unknown"),
        ("CPU frequency (max / now)", "%s / %s MHz" % (int(fmax) // 1000 if fmax.isdigit() else "?",
                                                      int(fcur) // 1000 if fcur.isdigit() else "?")),
        ("SoC temperature at start", "%s C" % vc_temp()),
        ("Throttle flags at start", vc_throttled() or "unknown"),
    ]
    software = [
        ("Release (latest git tag)", ver["release"]),
        ("App version (git describe)", ver["describe"]),
        ("App commit / branch", "%s / %s" % (ver["commit"], ver["branch"])),
        ("Operating system", os_name.group(1) if os_name else platform.platform()),
        ("Kernel", "%s %s" % (platform.release(), platform.version())),
        ("Architecture", platform.machine()),
        ("Firmware", run(["vcgencmd", "version"]).replace("\n", " | ") or "unknown"),
        ("Python", platform.python_version()),
    ]
    return hardware, software


# --------------------------------------------------------------------------
# Result files
# --------------------------------------------------------------------------

def save_results(paths, text, data):
    """Write the readable log and the JSON file atomically (a half-written file never replaces a good one)."""
    for path, content in ((paths[0], text), (paths[1], json.dumps(data, indent=2, default=str))):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content + "\n")
        os.replace(tmp, path)


def offer_reference(paths, issues, section, row):
    """Ask whether the result is also kept as a git-tracked reference result in docs/benchmarks/,
    and if so copy the files and add 'row' to the table below the heading 'section' of its README."""
    print("\nWhere should this result be kept?")
    print("  l  local only: %s (not tracked by git, never pushed)   [default]" % os.path.dirname(paths[0]))
    print("  r  also as a reference result: copy it to docs/benchmarks/ (tracked by git, pushed with your next\n"
          "     commit) and add a row to the table '%s' in docs/benchmarks/README.md" % section)
    if issues:
        print("Note - this run does not meet the criteria for a reference result:")
        for issue in issues:
            print("  - %s" % issue)
    try:
        choice = input("Choice [l/r]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = ""
    if choice != "r":
        print("Kept locally only.")
        return
    os.makedirs(DOCS_BENCHMARKS, exist_ok=True)
    for path in paths:
        shutil.copy2(path, DOCS_BENCHMARKS)
    readme = os.path.join(DOCS_BENCHMARKS, "README.md")
    try:
        with open(readme) as f:
            text = f.read()
        with open(readme, "w") as f:
            f.write(append_table_row(text, row, section))
        print("Copied to docs/benchmarks/ and added a row to its README.md. Review the files, then commit them.")
    except (OSError, ValueError) as exc:
        print("Copied the files, but the table in docs/benchmarks/README.md was not updated: %s" % exc)
