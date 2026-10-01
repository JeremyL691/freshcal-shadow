# freshcal-shadow

A live shadow run of [FreshCal](https://github.com/JeremyL691/freshcal) on two real public
data sources, to see how its business-calendar-aware freshness verdicts behave outside the
test suite — and how they compare with a fixed freshness threshold.

Every 15 minutes, GitHub Actions ([`shadow.yml`](.github/workflows/shadow.yml)):

1. **Loads** the ECB's daily euro reference rates and the US Treasury's daily par yield
   curve. A publication date not seen before is appended to [`loads/`](loads) with the
   time it was loaded — exactly what a real ingestion job does.
2. **Records** the ECB daily file's `Last-Modified` header in
   [`ecb/publications.csv`](ecb/publications.csv) with FreshCal's own collector.
3. **Checks** both sources with `freshcal check`, installed from PyPI, using
   [`freshcal.yml`](freshcal.yml):
   - ECB: TARGET business days, release 15:45 Europe/Berlin, 2 h 15 m grace.
   - Treasury: US federal business days, release 15:00 America/New_York, 4 h grace.
4. **Commits** only what changed: new loads, new ECB observations, and every change of a
   source's status in [`results/transitions.jsonl`](results).

`python3 analyze.py` turns the data into a report: publication delays, every FreshCal
alert, and what a fixed 26-hour threshold (dbt-style `error_after`) would have reported
over the same timeline. The run started on 2026-09-30 and is planned for about four
weeks, which includes US Columbus Day (2026-10-12), a bond-market holiday on which the
stock exchange is open.

Data commits are made by `github-actions[bot]`. The data are public: ECB reference rates
(© European Central Bank) and US Treasury par yield curve rates (U.S. Department of the
Treasury). Only publication dates, one headline value, and load times are stored.
