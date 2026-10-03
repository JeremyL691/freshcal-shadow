"""Summarise the shadow run as Markdown: arrivals, FreshCal's alerts, a fixed-threshold baseline.

    python3 analyze.py [--until 2026-10-30T00:00:00Z] > report.md

- Summary: one row per source with its start, publications, FreshCal alerts and the
  baseline's alerts.
- Arrivals: for each loaded publication, the load time (the first 15-minute run that
  saw it) relative to the configured release time.
- FreshCal: every status episode from ``results/transitions.jsonl``; each OVERDUE episode
  is listed so it can be judged a real delay or a false alarm.
- Baseline: what a fixed freshness threshold (dbt-style ``error_after``, 26 hours since
  the latest load) would have reported over the same timeline, sampled every 15 minutes.

Standard library only; reads only files in this repository. Release times are computed
here independently of FreshCal, so the arrival table does not depend on the tool under
test.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
THRESHOLD = timedelta(hours=26)
STEP = timedelta(minutes=15)

#: US bond-market closures on weekdays in the run's period (SIFMA; the New York Fed
#: publishes no SOFR on them). Listed here rather than taken from FreshCal on purpose.
US_CLOSURES = {date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25)}


def same_day(day: date) -> date:
    return day


def next_us_business_day(day: date) -> date:
    """SOFR for ``day`` is published on the next US bond-market business day."""
    following = day + timedelta(days=1)
    while following.weekday() >= 5 or following in US_CLOSURES:
        following += timedelta(days=1)
    return following


#: source id -> (loads file, date column, release time, release zone, publication day of
#: a data date), as configured in freshcal.yml.
SOURCES: dict[str, tuple[str, str, time, ZoneInfo, Callable[[date], date]]] = {
    "ecb.reference_rates": (
        "ecb.csv",
        "rate_date",
        time(15, 45),
        ZoneInfo("Europe/Berlin"),
        same_day,
    ),
    "us_treasury.par_yield_curve": (
        "treasury.csv",
        "curve_date",
        time(15, 0),
        ZoneInfo("America/New_York"),
        same_day,
    ),
    "nyfed.sofr": (
        "sofr.csv",
        "effective_date",
        time(7, 0),
        ZoneInfo("America/New_York"),
        next_us_business_day,
    ),
    "boc.exchange_rates": (
        "boc.csv",
        "rate_date",
        time(15, 0),
        ZoneInfo("America/Toronto"),
        same_day,
    ),
    "rba.exchange_rates": (
        "rba.csv",
        "rate_date",
        time(16, 0),
        ZoneInfo("Australia/Sydney"),
        same_day,
    ),
    "cnb.exchange_rates": (
        "cnb.csv",
        "rate_date",
        time(14, 15),
        ZoneInfo("Europe/Prague"),
        same_day,
    ),
}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def loads(source: str, until: datetime) -> list[tuple[date, datetime]]:
    """(data date, load time) of every load up to ``until``."""
    name, column, _, _, _ = SOURCES[source]
    path = ROOT / "loads" / name
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        rows = [
            (
                date.fromisoformat(row[column]),
                datetime.fromisoformat(row["_loaded_at"]).replace(tzinfo=UTC),
            )
            for row in csv.DictReader(handle)
        ]
    return [(day, loaded) for day, loaded in rows if loaded <= until]


def transitions(source: str, until: datetime) -> list[dict[str, str]]:
    path = ROOT / "results" / "transitions.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in events if e["source"] == source and utc(e["at"]) <= until]


def hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f} h"


def arrivals(source: str, until: datetime) -> list[tuple[date, datetime, datetime]]:
    """(data date, release, load) for every publication after the source's first load."""
    _, _, release_time, zone, publication_day = SOURCES[source]
    rows = loads(source, until)[1:]  # the first row was loaded when the source was added
    return [
        (day, datetime.combine(publication_day(day), release_time, zone), loaded)
        for day, loaded in rows
    ]


def arrivals_section(source: str, until: datetime) -> list[str]:
    rows = arrivals(source, until)
    if not rows:
        return ["No publication observed since the start yet.", ""]
    lines = [
        "| Data date | Release (local) | Loaded (UTC) | Loaded after release |",
        "|---|---|---|---|",
    ]
    delays = []
    for day, release, loaded in rows:
        delay = loaded - release
        delays.append(delay)
        lines.append(
            f"| {day} | {release:%a %Y-%m-%d %H:%M %Z} | {loaded:%Y-%m-%d %H:%M} | {hours(delay)} |"
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


def freshcal_episodes(
    source: str, until: datetime
) -> tuple[dict[str, timedelta], list[tuple[datetime, datetime, str]]]:
    """Time in each status, and the OVERDUE episodes (start, end, explanation)."""
    events = transitions(source, until)
    totals: dict[str, timedelta] = {}
    overdue: list[tuple[datetime, datetime, str]] = []
    if not events:
        return totals, overdue
    for event, following in zip(events, [*events[1:], None], strict=True):
        start = utc(event["at"])
        end = utc(following["at"]) if following else until
        totals[event["status"]] = totals.get(event["status"], timedelta()) + (end - start)
        if event["status"] == "OVERDUE":
            overdue.append((start, end, event["explanation"]))
    return totals, overdue


def freshcal_section(source: str, until: datetime) -> list[str]:
    totals, overdue = freshcal_episodes(source, until)
    if not totals:
        return ["No FreshCal result recorded yet.", ""]
    lines = ["| Status | Time in status |", "|---|---|"]
    lines += [f"| {status} | {hours(total)} |" for status, total in sorted(totals.items())]
    lines += ["", f"OVERDUE episodes: {len(overdue)}", ""]
    for start, end, explanation in overdue:
        lines.append(f"- {start:%a %Y-%m-%d %H:%M}Z → {end:%a %Y-%m-%d %H:%M}Z: {explanation}")
    return [*lines, ""]


def baseline_episodes(source: str, until: datetime) -> list[tuple[datetime, datetime]]:
    rows = sorted(loaded for _, loaded in loads(source, until))
    if not rows:
        return []
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
    return episodes


def baseline_section(source: str, until: datetime) -> list[str]:
    if not loads(source, until):
        return ["No loads yet.", ""]
    episodes = baseline_episodes(source, until)
    total = sum((end - start for start, end in episodes), timedelta())
    lines = [
        f"Alert episodes with a fixed {hours(THRESHOLD)} threshold: {len(episodes)} "
        f"({hours(total)} in alert)",
        "",
    ]
    lines += [f"- {start:%a %Y-%m-%d %H:%M}Z → {end:%a %Y-%m-%d %H:%M}Z" for start, end in episodes]
    return [*lines, ""]


def summary_section(until: datetime) -> list[str]:
    lines = [
        "| Source | Since (UTC) | Publications | FreshCal OVERDUE episodes | "
        f"{hours(THRESHOLD)} baseline episodes (time in alert) |",
        "|---|---|---|---|---|",
    ]
    for source in SOURCES:
        rows = loads(source, until)
        if not rows:
            lines.append(f"| {source} | – | 0 | – | – |")
            continue
        _, overdue = freshcal_episodes(source, until)
        episodes = baseline_episodes(source, until)
        total = sum((end - start for start, end in episodes), timedelta())
        lines.append(
            f"| {source} | {rows[0][1]:%Y-%m-%d %H:%M} | {len(arrivals(source, until))} | "
            f"{len(overdue)} | {len(episodes)} ({hours(total)}) |"
        )
    return [
        *lines,
        "",
        "Publications are counted after each source's first load (the load made when the "
        "source was added).",
        "",
    ]


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
    out += ["## Summary", "", *summary_section(until)]
    for source in SOURCES:
        out += [f"## {source}", "", "### Arrivals", "", *arrivals_section(source, until)]
        out += ["### FreshCal", "", *freshcal_section(source, until)]
        out += ["### Fixed-threshold baseline", "", *baseline_section(source, until)]
    out += ["## ECB publication-time collection", "", *ecb_collection_section()]
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
