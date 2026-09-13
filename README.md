# FCC ULS weekly pull — Nevada / Clark County

Automates the same thing sites like [Midwest Grant Pages](https://www.midwestgrants.com/)
do: pull the FCC's own public weekly/daily ULS transaction files and filter
them down to a state/region, on a schedule, with no API key and no scraping.

## Why this exists

The FCC ULS "License View" API is defunct. The reliable path is the FCC's
bulk data mirror, which publishes every license transaction (new, renewed,
modified, canceled, expired) as plain pipe-delimited flat files, zipped, per
radio service, updated daily and weekly:

- Full/weekly snapshot: `https://data.fcc.gov/download/pub/uls/complete/`
- Daily transactions:   `https://data.fcc.gov/download/pub/uls/daily/`

No auth, no rate limit — just static files meant for exactly this kind of
bulk consumption. Background/citations:
[FCC: Public Access Files - Database Downloads](https://www.fcc.gov/wireless/data/public-access-files-database-downloads),
[ULS Database Public Access Files intro](https://www.fcc.gov/sites/default/files/pubacc_intro_11122014.pdf),
[Radio service codes](https://www.fcc.gov/node/189710).

## Where this runs

**GitHub Actions**, not a Claude-hosted schedule. The sandbox this was built
in has outbound network access to `fcc.gov` / `data.fcc.gov` blocked by org
policy, so a weekly pull can't run there. GitHub-hosted Actions runners have
normal outbound internet access, which is also what your existing
`nws-forecast-collector` pipeline relies on — this repo follows the same
pattern: `.github/workflows/weekly-pull.yml` runs Sundays at 18:00 UTC (after
FCC's own Sunday-morning refresh) and on manual trigger.

## What it pulls

Configured in `config.yml`:

- **`amateur`** — the Amateur radio service, statewide NV.
- **`land_mobile_private`** — Public Safety Pool + Industrial/Business Pool
  (conventional and trunked: `PW`, `YW`, `IG`, `IK`, `YG`, `YK`), filtered to
  NV and, if `clark_county_cities` is non-empty, further narrowed to that
  city allow-list (ULS records don't carry a clean county field, so this is
  a city-name proxy for Clark County — edit the list in `config.yml` freely,
  including removing it to go statewide).

Each run writes, per target, under `data/<target>/`:
- `<run-date>.json` / `<run-date>.csv` — that run's snapshot
- `latest.json` / `latest.csv` — always the most recent run

Plus `data/last_run_summary.md` with a one-line record count per target.

## Things to verify on the first real run

This was built without a working network path to the FCC, so two things
couldn't be tested against live data and are worth a five-minute sanity
check the first time the Action runs successfully:

1. **`land_mobile_private` filename discovery.** `config.yml`'s
   `file_match` list gets matched against whatever FCC actually publishes
   under `/daily/`. Check the run log for a line like
   `discover_filename: matched 'lmpriv' against 'l_lmpriv_mon.zip'`. If it
   instead logs "none of [...] matched", open
   `https://data.fcc.gov/download/pub/uls/daily/` in a browser, find the
   real filename, and either add the right substring to `file_match.daily`
   or just hardcode `daily_prefix` the same way `amateur` does.
2. **HD.dat / EN.dat field positions.** `scripts/fetch_uls.py` uses the
   FCC's long-stable public ULS schema (unchanged for decades, used by
   basically every third-party ULS tool), but it's worth spot-checking one
   or two output rows against a callsign you recognize on the FCC's own
   [ULS license search](https://wireless2.fcc.gov/UlsApp/UlsSearch/searchLicense.jsp)
   to confirm dates/names/cities are landing in the right columns.

If either needs a tweak, it's a one-line change in `config.yml` or a field
list in `fetch_uls.py` — the pipeline structure itself doesn't need to
change.

## Running locally

```bash
pip install -r scripts/requirements.txt
python scripts/fetch_uls.py                 # daily-transaction pull only
python scripts/fetch_uls.py --include-complete   # also pulls full nationwide snapshots (large)
```

## Extending

- Add another target (e.g. GMRS, or a different state) by adding an entry
  to `config.yml`'s `targets:` list — either with an explicit
  `daily_prefix`/`complete_file`, or a `file_match` block to auto-discover
  it the way `land_mobile_private` does.
- Want a dashboard instead of raw CSV/JSON? The `data/*/latest.json` files
  are stable, so a static HTML page (GitHub Pages) or a separate viewer can
  read straight from them without touching this pipeline.
