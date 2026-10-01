"""Summarise the shadow run as Markdown: arrivals, FreshCal's alerts, a fixed-threshold baseline.

    python3 analyze.py [--until 2026-10-29T00:00:00Z] > report.md

- Arrivals: for each loaded publication date, the load time (the first 15-minute run that
  saw it) relative to the configured release time.
- FreshCal: every status episode from ``results/transitions.jsonl``; each OVERDUE episode
  is listed so it can be judged a real delay or a false alarm.
- Baseline: what a fixed freshness threshold (dbt-style ``error_after``, 26 hours since
  the latest load) would have reported over the same timeline, sampled every 15 minutes.

Standard library only; reads only files in this repository.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
THRESHOLD = timedelta(hours=26)
STEP = timedelta(minutes=15)

#: source id -> (loads file, date column, release time, release zone), as in freshcal.yml.
SOURCES = {
    "ecb.reference_rates": ("ecb.csv", "rate_date", time(15, 45), ZoneInfo("Europe/Berlin")),
    "us_treasury.par_yield_curve": (
        "treasury.csv",
        "curve_date",
        time(15, 0),
        ZoneInfo("America/New_York"),
    ),
}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def loads(source: str) -> list[tuple[date, datetime]]:
    name, column, _, _ = SOURCES[source]
    path = ROOT / "loads" / name
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return [
            (
                date.fromisoformat(row[column]),
                datetime.fromisoformat(row["_loaded_at"]).replace(tzinfo=UTC),
            )
            for row in csv.DictReader(handle)
        ]


def transitions() -> list[dict[str, str]]:
    path = ROOT / "results" / "transitions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f} h"


def arrivals_section(source: str) -> list[str]:
    _, _, release_time, zone = SOURCES[source]
    rows = loads(source)[1:]  # the first row was loaded when the shadow run started
    if not rows:
        return ["No publication observed since the start yet.", ""]
    lines = [
        "| Publication date | Release (local) | Loaded (UTC) | Loaded after release |",
        "|---|---|---|---|",
    ]
    delays = []
    for day, loaded in rows:
        release = datetime.combine(day, release_time, zone)
        delay = loaded - release
        delays.append(delay)
        lines.append(
            f"| {day} | {release:%a %H:%M %Z} | {loaded:%Y-%m-%d %H:%M} | {hours(delay)} |"
        )
    delays.sort()
    lines += [
        "",
        f"{len(delays)} publications; load delay after the release time: "
        f"min {hours(delays[0])}, median {hours(delays[len(delays) // 2])}, "
        f"max {hours(delays[-1])} (includes up to 15 minutes of polling).",
        "",
    ]
    return lines


def freshcal_section(source: str, until: datetime) -> list[str]:
    events = [event for event in transitions() if event["source"] == source]
    if not events:
        return ["No FreshCal result recorded yet.", ""]
    totals: dict[str, timedelta] = {}
    overdue = []
    for event, following in zip(events, [*events[1:], None], strict=True):
        start = utc(event["at"])
        end = utc(following["at"]) if following else until
        totals[event["status"]] = totals.get(event["status"], timedelta()) + (end - start)
        if event["status"] == "OVERDUE":
            overdue.append((start, end, event["explanation"]))
    lines = ["| Status | Time in status |", "|---|---|"]
    lines += [f"| {status} | {hours(total)} |" for status, total in sorted(totals.items())]
    lines += ["", f"OVERDUE episodes: {len(overdue)}", ""]
    for start, end, explanation in overdue:
        lines.append(f"- {start:%a %Y-%m-%d %H:%M}Z → {end:%a %Y-%m-%d %H:%M}Z: {explanation}")
    return [*lines, ""]


def baseline_section(source: str, until: datetime) -> list[str]:
    rows = sorted(loaded for _, loaded in loads(source))
    if not rows:
        return ["No loads yet.", ""]
    episodes: list[tuple[datetime, datetime]] = []
    instant, index, open_since = rows[0], 0, None
    while instant <= until:
        while index + 1 < len(rows) and rows[index + 1] <= instant:
            index += 1
        stale = instant - rows[index] > THRESHOLD
        if stale and open_since is None:
            open_since = instant
        if not stale and open_since is not None:
            episodes.append((open_since, instant))
            open_since = None
        instant += STEP
    if open_since is not None:
        episodes.append((open_since, until))
    total = sum((end - start for start, end in episodes), timedelta())
    lines = [
        f"Alert episodes with a fixed {hours(THRESHOLD)} threshold: {len(episodes)} "
        f"({hours(total)} in alert)",
        "",
    ]
    lines += [f"- {start:%a %Y-%m-%d %H:%M}Z → {end:%a %Y-%m-%d %H:%M}Z" for start, end in episodes]
    return [*lines, ""]


def ecb_collection_section() -> list[str]:
    path = ROOT / "ecb" / "publications.csv"
    with path.open(newline="") as handle:
        days = sorted({row["rate_date"] for row in csv.DictReader(handle)})
    span = f"{days[0]} … {days[-1]}"
    return [f"ECB `Last-Modified` observations: {len(days)} business days ({span}).", ""]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--until", type=utc, default=datetime.now(UTC))
    until = parser.parse_args().until
    out = [f"# FreshCal shadow run — report until {until:%Y-%m-%d %H:%M}Z", ""]
    for source in SOURCES:
        out += [f"## {source}", "", "### Arrivals", "", *arrivals_section(source)]
        out += ["### FreshCal", "", *freshcal_section(source, until)]
        out += ["### Fixed-threshold baseline", "", *baseline_section(source, until)]
    out += ["## ECB publication-time collection", "", *ecb_collection_section()]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
