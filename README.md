# freshcal-shadow

A live shadow run of [FreshCal](https://github.com/JeremyL691/freshcal) on six real public
data sources, to see how its business-calendar-aware freshness verdicts behave outside the
test suite — and how they compare with a fixed freshness threshold.

Every 15 minutes, GitHub Actions ([`shadow.yml`](.github/workflows/shadow.yml); each run
works through the 15-minute slots for 5.5 hours and then starts the next run, because
GitHub throttles frequent schedules):

1. **Loads** six daily publications. A publication date not seen before is appended to
   [`loads/`](loads) with the time it was loaded — exactly what a real ingestion job
   does. The RBA and CNB files are polled every 15 minutes only on weekdays from shortly
   before their release until the day's data has arrived, and hourly otherwise (the CNB
   asks users not to poll excessively; the RBA file is about 140 KB).
2. **Records** the ECB daily file's `Last-Modified` header in
   [`ecb/publications.csv`](ecb/publications.csv) with FreshCal's own collector.
3. **Checks** every source with `freshcal check`, installed from PyPI, using
   [`freshcal.yml`](freshcal.yml).
4. **Commits** only what changed: new loads, new ECB observations, and every change of a
   source's status in [`results/transitions.jsonl`](results).

| Source | Business days | Release (local) | Grace | Deadline |
|---|---|---|---|---|
| ECB euro reference rates | TARGET (`financial: XECB`) | 15:45 Europe/Berlin | 2 h 15 m | 18:00 |
| US Treasury par yield curve | US federal (`country: US`) | 15:00 America/New_York | 4 h | 19:00 |
| New York Fed SOFR (published the next business day) | US federal | 07:00 America/New_York | 3 h | 10:00 |
| Bank of Canada USD/CAD | Canadian federal government holidays | 15:00 America/Toronto | 3 h 30 m | 18:30 |
| Reserve Bank of Australia AUD/USD (table F11.1) | New South Wales | 16:00 Australia/Sydney | 2 h 30 m | 18:30 |
| Czech National Bank USD/CZK | Czech Republic | 14:15 Europe/Prague | 2 h 15 m | 16:30 |

FreshCal counts only loads made after a release, so a release time must not be later than
the earliest time the data can be seen. For the four sources added on 2026-10-03 the
release is the fixing time or a time clearly before the publisher's stated publication
time, and the deadline is the stated (or, for the RBA, observed) publication time plus
about two hours. These values were fixed before the sources' first data arrived, each
calendar was checked against the publisher's 2024–2025 data, and [`freshcal.yml`](freshcal.yml)
cites the publishers' statements.

`python3 analyze.py --until 2026-10-30T00:00:00Z` turns the data into a report: a summary
per source, publication delays, every FreshCal alert, and what a fixed 26-hour threshold
(dbt-style `error_after`) would have reported over the same timeline. The analysis period
ends on 2026-10-30 at 00:00 UTC and includes:

- 2026-10-04: Australian daylight saving time starts (16:00 Sydney moves from 06:00 to
  05:00 UTC).
- 2026-10-05: NSW Labour Day, no RBA publication.
- 2026-10-12: US Columbus Day and Canadian Thanksgiving: no Treasury curve, SOFR or Bank
  of Canada rate, while the ECB, the RBA and the CNB publish as usual.
- 2026-10-25: European daylight saving time ends (ECB and CNB releases move one hour
  later in UTC).
- 2026-10-28: Independent Czechoslovak State Day, no CNB fixing.

The ECB collection also gives FreshCal's arrival-time validation its 20 business days
(about 2026-10-23).

Data commits are made by `github-actions[bot]`. The data are public: ECB reference rates
(© European Central Bank), US Treasury par yield curve rates (U.S. Department of the
Treasury), SOFR (Federal Reserve Bank of New York), and the exchange rates of the Bank of
Canada, the Reserve Bank of Australia and the Czech National Bank. Only publication dates,
one headline value, and load times are stored.

## Changes during the run

- 2026-10-03: four sources added (SOFR, Bank of Canada, RBA, CNB) and the analysis
  period extended from two weeks (to about 2026-10-16) to 2026-10-30, so that the run
  covers more calendars, time zones and daylight-saving changes. The ECB and Treasury
  configuration is unchanged; the new sources' periods start when they were added.
