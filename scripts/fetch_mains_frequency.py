#!/usr/bin/env python3
"""
Fetches the mains electricity frequency table from Wikipedia and writes
static/data/mains_frequency.json.

Source: https://en.wikipedia.org/wiki/Mains_electricity_by_country
Run from the repo root:  python3 scripts/fetch_mains_frequency.py

No extra dependencies — uses only the Python standard library.

Table columns (as of 2026):
  0  Country or territory
  1  Plug type
  2  National plug standard
  3  Nominal supply voltage
  4  Supply frequency   ← extracted here
  5  Notes
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = "https://en.wikipedia.org/wiki/Mains_electricity_by_country"
OUTPUT     = Path(__file__).resolve().parent.parent / "static" / "data" / "mains_frequency.json"

COL_COUNTRY = 0
COL_FREQ    = 4   # "Supply frequency" column
COL_NOTES   = 5  # "Notes" column (used to label split-frequency countries)

# Countries whose single Wikipedia row carries both 50 Hz and 60 Hz.
# Each entry maps to two display names (50 Hz label, 60 Hz label).
SPLIT_FREQ_COUNTRIES: dict[str, tuple[str, str]] = {
    "Japan": ("Japan (East)", "Japan (West)"),
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CamUI-WebRTC/data-fetch (+https://github.com/j-josi/CamUI-WebRTC)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def clean(text: str) -> str:
    text = strip_tags(text)
    text = text.replace("\xa0", " ")          # non-breaking space
    text = re.sub(r"\[[^\]]*\]", "", text)    # footnote markers like [a], [1]
    return text.strip()


def extract_records(html: str) -> list[dict]:
    # Isolate the first wikitable
    m = re.search(r"(<table[^>]+wikitable[^>]*>.*?</table>)", html, re.DOTALL)
    if not m:
        raise ValueError("wikitable not found — page structure may have changed")

    table_html = m.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)

    records = []
    seen    = set()

    for row in rows:
        # td and th, including those with attributes (colspan, rowspan, etc.)
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        if len(cells) <= max(COL_COUNTRY, COL_FREQ):
            continue

        country = clean(cells[COL_COUNTRY])
        if not country or country.lower().startswith("country"):
            continue   # header row

        freq_text = clean(cells[COL_FREQ])
        # Use negative lookaround instead of \b: "Hz50" has no word boundary before 5
        freqs_in_cell = [int(m2) for m2 in re.findall(r"(?<!\d)(50|60)(?!\d)", freq_text)]
        if not freqs_in_cell:
            continue

        if country in SPLIT_FREQ_COUNTRIES and set(freqs_in_cell) == {50, 60}:
            label_50, label_60 = SPLIT_FREQ_COUNTRIES[country]
            for label, freq in ((label_50, 50), (label_60, 60)):
                key = (label, freq)
                if key not in seen:
                    seen.add(key)
                    records.append({"countryOrTerritory": label, "freq": freq})
        else:
            freq = freqs_in_cell[0]
            key  = (country, freq)
            if key not in seen:
                seen.add(key)
                records.append({"countryOrTerritory": country, "freq": freq})

    records.sort(key=lambda r: r["countryOrTerritory"].casefold())
    return records


def main():
    print(f"Fetching {SOURCE_URL} …")
    try:
        html = fetch_html(SOURCE_URL)
    except Exception as exc:
        print(f"ERROR: Could not fetch page: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        records = extract_records(html)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not records:
        print("ERROR: No records extracted — the page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    sixty = sum(1 for r in records if r["freq"] == 60)
    print(f"Extracted {len(records)} entries ({sixty} at 60 Hz, {len(records) - sixty} at 50 Hz).")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Written to {OUTPUT}")


if __name__ == "__main__":
    main()
