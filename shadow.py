"""One shadow-run step: load newly published data, then record FreshCal's verdicts.

1. Load. Fetch the ECB's daily reference rates and the US Treasury's daily par yield
   curve. A publication date not seen before is appended to ``loads/<source>.csv`` with
   the current time as ``_loaded_at`` (naive UTC), the way a real loader stamps rows.
2. Check. Run ``freshcal check --format json`` on ``freshcal.yml`` (the PyPI package).
3. Record. Append a line to ``results/transitions.jsonl`` whenever a source's status
   changes; ``results/state.json`` holds the last status per source. Unchanged runs write
   nothing, so the repository only changes when something happened.

Standard library only. A fetch failure is reported and skipped; it never stops the check.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOADS = ROOT / "loads"
RESULTS = ROOT / "results"
STATE = RESULTS / "state.json"
TRANSITIONS = RESULTS / "transitions.jsonl"
FAILURES = RESULTS / "failures.jsonl"

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={year}&page&_format=csv"
)
USER_AGENT = "freshcal-shadow (+https://github.com/JeremyL691/freshcal-shadow)"
ECB_NS = {"cube": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def ecb_latest() -> tuple[str, str]:
    """(rate_date, USD rate) of the ECB's latest publication."""
    root = ET.fromstring(fetch(ECB_URL))
    day = root.find(".//cube:Cube[@time]", ECB_NS)
    if day is None:
        raise ValueError("no dated Cube element in the ECB file")
    usd = day.find("cube:Cube[@currency='USD']", ECB_NS)
    return day.attrib["time"], usd.attrib["rate"] if usd is not None else ""


def treasury_latest(now: datetime) -> tuple[str, str]:
    """(curve date, 10-year yield) of the Treasury's latest published curve."""
    for year in (now.year, now.year - 1):  # early January: this year's file may be empty
        rows = list(csv.DictReader(io.StringIO(fetch(TREASURY_URL.format(year=year)))))
        if rows:
            latest = max(rows, key=lambda row: datetime.strptime(row["Date"], "%m/%d/%Y"))
            date = datetime.strptime(latest["Date"], "%m/%d/%Y").date().isoformat()
            return date, latest.get("10 Yr", "")
    raise ValueError("no Treasury curve in this year's or last year's file")


def append_if_new(path: Path, header: list[str], date: str, value: str, now: datetime) -> bool:
    """Append ``date`` with ``now`` as its load time unless it is already loaded."""
    if path.exists():
        with path.open(newline="") as handle:
            if any(row[header[0]] == date for row in csv.DictReader(handle)):
                return False
    new_file = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(header)
        writer.writerow([date, value, now.strftime("%Y-%m-%d %H:%M:%S")])
    return True


def log(path: Path, record: dict[str, object]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load(now: datetime) -> None:
    sources = (
        ("ecb", ["rate_date", "usd", "_loaded_at"], ecb_latest),
        ("treasury", ["curve_date", "yield_10y", "_loaded_at"], lambda: treasury_latest(now)),
    )
    for name, header, latest in sources:
        try:
            date, value = latest()
        except Exception as error:  # a source being down is data, not a crash
            print(f"shadow: {name} fetch failed: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        if append_if_new(LOADS / f"{name}.csv", header, date, value, now):
            print(f"shadow: loaded {name} {date}")


def check_and_record() -> int:
    completed = subprocess.run(
        ["freshcal", "check", "-c", "freshcal.yml", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        log(FAILURES, {"exit_code": completed.returncode, "stderr": completed.stderr[-2000:]})
        print(f"shadow: freshcal produced no report (exit {completed.returncode})", file=sys.stderr)
        return 1
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for result in report["results"]:
        source = result["source_id"]
        if state.get(source) == result["status"]:
            continue
        state[source] = result["status"]
        log(
            TRANSITIONS,
            {
                "at": report["evaluated_at"],
                "source": source,
                "status": result["status"],
                "release": (result["release"] or {}).get("instant"),
                "deadline": result["deadline"],
                "observed": (result["observed"] or {}).get("instant"),
                "explanation": result["explanation"],
                "freshcal": report["freshcal_version"],
            },
        )
        print(f"shadow: {source} -> {result['status']}")
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    return 0


def main() -> int:
    LOADS.mkdir(exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    load(datetime.now(UTC).replace(microsecond=0, tzinfo=None))
    return check_and_record()


if __name__ == "__main__":
    raise SystemExit(main())
