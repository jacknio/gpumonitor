# H100 Rental Track

A local NVIDIA H100 rental price tracker. It runs a small Python web server,
stores observations in SQLite, and serves a browser UI at
`http://127.0.0.1:8787`.

The UI shows current rental offers, source health, and an interactive daily
trend chart. When tracking runs multiple times in a day, the chart groups those
observations by date. The chart can switch between daily median and daily
average USD/GPU-hour price.

## Run

```bash
python3 server.py
```

Open `http://127.0.0.1:8787`.

Start hourly tracking:

```bash
python3 server.py --track-interval --track-every-hours 1
```

This collects once when the server starts, then collects again every hour on
local clock boundaries. That gives 24 samples per day.

Run one collection pass and print JSON:

```bash
python3 server.py --once
```

Use demo data without hitting external sites:

```bash
python3 server.py --once --demo
```

## Historical backfill

Backfill historical rental prices from archived public pricing pages:

```bash
python3 server.py --backfill-year
```

Limit the backfill while testing:

```bash
python3 server.py --backfill-year --backfill-days 30 --backfill-sources Lambda,CoreWeave
```

The backfill uses Internet Archive Wayback Machine snapshots for public rental
pricing pages. It is best-effort: if a historical page layout cannot be parsed,
that snapshot is skipped.

## Sources

Vast.ai is the only source used by default:

- Vast.ai live bundles API: `VAST_API_KEY`

For local runs, the API key can also live outside the repo at
`../gpumonitor_private/vast_api_key.txt`. Keeping it outside `h100price/`
prevents accidental GitHub uploads.

Other live/API-backed sources are disabled by default. Set
`H100_INCLUDE_OTHER_LIVE_SOURCES=1` if you want to include them again:

- AWS Capacity Block offerings API: `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`
- AWS Spot price history API: `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`
- Manual rental quotes: `data/manual_prices.json`

Static public page scrapers are disabled by default because they are mostly
list prices, not live rentable inventory. Set `H100_INCLUDE_STATIC_SOURCES=1`
if you want to include them again:

- Lambda pricing
- Crusoe Cloud pricing
- CoreWeave pricing
- AWS EC2 Capacity Blocks pricing
- RunPod pricing page, best effort

## Useful environment variables

```bash
export H100_QUERY="NVIDIA H100 80GB GPU"
export VAST_API_KEY="..."
export VAST_LIMIT=80
export PORT=8787
export H100_TRACK_INTERVAL=1
export H100_TRACK_EVERY_HOURS=1
export H100_DAILY_AT=09:00
```

Use `--no-track-now` with tracking if you want the first automatic collection to
wait until the next scheduled time. The older daily mode is still available:

```bash
python3 server.py --track-daily --daily-at 09:00
```

## Deploy on Render

This repo includes `render.yaml` for a Render Static Site. Render only serves
the committed files in `static/`; it does not run the crawler or store the
SQLite database.

Keep the Vast.ai API key local. After local data changes, refresh the static
snapshot before committing:

```bash
python3 server.py --export-static
git add static/data static/exports
git commit -m "Update GPU rental snapshot"
git push
```

Hourly local tracking also refreshes the static snapshot after each successful
live collection unless `H100_EXPORT_STATIC=0` is set.

In Render, create a Static Site or Blueprint from the repo. The publish path is
`./static`, and the public page reads committed JSON snapshots from
`static/data/` and committed CSV/XLSX downloads from `static/exports/`.

## Local data

The database is stored locally at `data/h100_prices.sqlite3` and is ignored by
git. It is not regenerated unless you run collection or backfill commands. Each
save also refreshes Excel-readable exports:

- `data/gpu_rental_prices.csv`
- `data/gpu_rental_prices.xlsx`
- `data/h100_rental_prices.csv`
- `data/h100_rental_prices.xlsx`
- `data/h200_rental_prices.csv`
- `data/h200_rental_prices.xlsx`
- `data/a100_rental_prices.csv`
- `data/a100_rental_prices.xlsx`
- `data/b200_rental_prices.csv`
- `data/b200_rental_prices.xlsx`
- `data/b300_rental_prices.csv`
- `data/b300_rental_prices.xlsx`

Set `H100_EXPORT_EXCEL=0` if you want to disable those export files.

Create `data/manual_prices.json` using `data/manual_prices.example.json` as a
template to track private rental quotes or platforms that require a sales
conversation.
