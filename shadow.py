"""One shadow-run step: load newly published data, then record FreshCal's verdicts.

1. Load. Fetch six public daily publications: the ECB's reference rates, the US
   Treasury's par yield curve, the New York Fed's SOFR, and the exchange rates of the
   Bank of Canada, the Reserve Bank of Australia and the Czech National Bank. A
   publication date not seen before is appended to ``loads/<source>.csv`` with the
   current time as ``_loaded_at`` (naive UTC), the way a real loader stamps rows.
   The RBA and CNB files are fetched every 15 minutes only on weekdays from shortly
   before their release until the day's data has arrived, and hourly otherwise (the
   CNB asks users not to poll excessively; the RBA file is about 140 KB).
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
from collections.abc import Callable
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

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
SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json"
BOC_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"
RBA_URL = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"
CNB_URL = (
    "https://www.cnb.cz/en/financial-markets/foreign-exchange-market/"
    "central-bank-exchange-rate-fixing/central-bank-exchange-rate-fixing/daily.txt"
)
USER_AGENT = "freshcal-shadow (+https://github.com/JeremyL691/freshcal-shadow)"
ECB_NS = {"cube": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8-sig")


def ecb_latest(now: datetime) -> tuple[str, str]:
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


def sofr_latest(now: datetime) -> tuple[str, str]:
    """(effective date, rate) of the latest SOFR; it is published the next business day."""
    rate = json.loads(fetch(SOFR_URL))["refRates"][0]
    return rate["effectiveDate"], str(rate["percentRate"])


def boc_latest(now: datetime) -> tuple[str, str]:
    """(date, USD/CAD) of the Bank of Canada's latest daily average exchange rate."""
    observation = json.loads(fetch(BOC_URL))["observations"][-1]
    return observation["d"], observation["FXUSDCAD"]["v"]


def rba_latest(now: datetime) -> tuple[str, str]:
    """(date, AUD/USD) of the latest row of the RBA's F11.1 table (the 4 pm Sydney fix)."""
    rows = []
    for row in csv.reader(io.StringIO(fetch(RBA_URL))):
        try:
            rows.append((datetime.strptime(row[0], "%d-%b-%Y").date(), row[1]))
        except (IndexError, ValueError):  # title, metadata and blank lines
            continue
    if not rows:
        raise ValueError("no dated row in the RBA F11.1 table")
    day, usd = max(rows)
    return day.isoformat(), usd


def cnb_latest(now: datetime) -> tuple[str, str]:
    """(date, USD/CZK) of the Czech National Bank's latest exchange-rate fixing."""
    lines = fetch(CNB_URL).splitlines()  # "02 Oct 2026 #190", a header, then the rates
    day = datetime.strptime(lines[0].split("#")[0].strip(), "%d %b %Y").date()
    usd = next((line.split("|")[4] for line in lines[2:] if line.split("|")[3:4] == ["USD"]), "")
    return day.isoformat(), usd


Window = tuple[time, ZoneInfo]

#: loads file stem -> (loads header, fetch, polling window; None means every slot).
SOURCES: dict[str, tuple[list[str], Callable[[datetime], tuple[str, str]], Window | None]] = {
    "ecb": (["rate_date", "usd", "_loaded_at"], ecb_latest, None),
    "treasury": (["curve_date", "yield_10y", "_loaded_at"], treasury_latest, None),
    "sofr": (["effective_date", "rate", "_loaded_at"], sofr_latest, None),
    "boc": (["rate_date", "usd_cad", "_loaded_at"], boc_latest, None),
    "rba": (
        ["rate_date", "aud_usd", "_loaded_at"],
        rba_latest,
        (time(15, 45), ZoneInfo("Australia/Sydney")),
    ),
    "cnb": (
        ["rate_date", "usd_czk", "_loaded_at"],
        cnb_latest,
        (time(14, 0), ZoneInfo("Europe/Prague")),
    ),
}


def latest_loaded(path: Path, column: str) -> str | None:
    if not path.exists():
        return None
    with path.open(newline="") as handle:
        return max((row[column] for row in csv.DictReader(handle)), default=None)


def due(now: datetime, path: Path, column: str, window: Window | None) -> bool:
    """Whether a source is fetched in this slot.

    Without a window: every slot. With one: every slot on weekdays from the window's
    local start time until that day's publication is loaded, and otherwise only in the
    first slot of each hour, so no publication waits more than an hour to be seen.
    """
    if window is None or now.minute < 15:
        return True
    start, zone = window
    local = now.replace(tzinfo=UTC).astimezone(zone)
    if local.weekday() >= 5 or local.time() < start:
        return False
    return latest_loaded(path, column) != local.date().isoformat()


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
    for name, (header, latest, window) in SOURCES.items():
        path = LOADS / f"{name}.csv"
        if not due(now, path, header[0], window):
            continue
        try:
            date, value = latest(now)
        except Exception as error:  # a source being down is data, not a crash
            reason = f"{type(error).__name__}: {error}"[:500]
            # Recorded so the analysis can tell "the publisher was late" from "our loader
            # could not reach it".
            log(FAILURES, {"at": now.isoformat() + "Z", "step": f"fetch {name}", "error": reason})
            print(f"shadow: {name} fetch failed: {reason}", file=sys.stderr)
            continue
        if append_if_new(path, header, date, value, now):
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
        log(
            FAILURES,
            {
                "at": datetime.now(UTC).isoformat(),
                "step": "freshcal check",
                "exit_code": completed.returncode,
                "error": completed.stderr[-2000:],
            },
        )
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
