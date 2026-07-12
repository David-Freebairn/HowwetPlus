# Howwet? + — Soil Water Tracker (Fallow → Crop)

A standalone Streamlit app that tracks **plant available soil water (PASW)**
continuously from a chosen fallow start date, through planting, and into
crop transpiration — one bucket, no reset at planting, no reset at harvest.

Built on the same PERFECT/HowLeaky-style water balance engine used in
RiskAware/YieldRisk, with one new module (`core/cropwater.py`) that stitches
a bare-fallow period and a crop growth period into a single daily run, plus
a Streamlit front end (`app.py`) purpose-built for this app.

## What it does

1. Pick a weather station, a soil type, and set **fallow start / plant date
   / maturity date** and a starting soil water %.
2. The app stretches a generic crop cover template to fit your exact
   plant → maturity window, runs one continuous daily water balance from
   fallow start through to your assessment date (or maturity, whichever is
   sooner — it never simulates past harvest), and shows:
   - A soil water (PASW) chart with the crop cover curve overlaid on a
     right-hand axis, plus a 20–80%ile historical band built by replaying
     the same season shape across every year of climate record since 1995.
   - A soil evaporation / transpiration stack chart, same time axis.
   - Two water balance tables (Fallow, In-crop), each showing rainfall,
     runoff, soil evaporation, transpiration, drainage, and change in soil
     water — with a historical percentile column ranking each total
     against the replayed seasons.
3. Download the daily results as CSV, or a Word report summarising the
   inputs, both tables, and the chart.

## Repo layout

```
app.py                  Streamlit UI — the only file you run
core/
  soil.py, soil_xml.py  Soil profile readers (.PRM / HowLeaky .soil / .xml)
  waterbalance.py        Daily PERFECT/HowLeaky water balance engine
                          (runoff, infiltration, two-stage soil evap,
                          transpiration, drainage, erosion)
  cover_excel.py          Reads crop cover Excel templates
  cropwater.py            Fallow→crop stitching, cover-curve stretching,
                          historical replay/percentiles (new for this app)
  silo.py                 SILO station search + climate fetch/cache
  report.py               Word (.docx) report builder
data/
  *.xml                   12 soils — shallow/average/deep phases of
                          sandy loam, clay loam, light clay, heavy clay
  generic_crop.xlsx        The one bundled crop cover template
.silo_cache/              Runtime climate cache (git-ignored)
```

## Core engine (unchanged from RiskAware/YieldRisk)

- `core/soil.py`, `core/soil_xml.py` — soil profile readers
- `core/waterbalance.py` — the daily water balance engine. No nitrogen or
  yield code — that was never in here, so nothing needed stripping out.
  One real bug found and fixed along the way: `infiltrate_and_drain()` was
  silently discarding water that arrived faster than a layer's Ksat could
  pass it on (only shows up on soils with very low Ksat hit by large rain
  events back to back). That water now correctly shows up as extra runoff
  ("add runoff", per the original PERFECT terminology) rather than
  vanishing from the mass balance and getting misattributed as evaporation.
- `core/silo.py` — SILO station search, climate fetch, and a two-layer
  cache (Streamlit session state, then a 24-hour-fresh on-disk parquet
  cache per station). Fetches and caches 1995 → today per station (matches
  this app's simulation floor — see below). Stale cache files (>7 days)
  are cleaned up once per app process at startup.
- `core/cover_excel.py` — reads the "Cover data for Howleaky" Excel format
  (green cover %, residue %, root depth by day)

## What's new: `core/cropwater.py`

- **`stretch_cover_schedule()`** — finds a crop template's own green-cover
  growth window (first day it rises off zero, to the day it's fully back
  down after peaking) and linearly stretches/compresses that window onto
  your actual **plant_date → maturity_date**. The template only needs to
  represent a *typical* growth shape — you don't need a separate template
  per possible season length, and it works with calendar-anchored templates
  as-is (it only cares about the relative shape, not what the day numbers
  mean).
- **`run_fallow_to_crop()`** — the main entry point. Runs one continuous
  daily loop: before `plant_date` → fixed bare cover (no roots, no green
  cover, fixed residue, zero transpiration); `plant_date` → `maturity_date`
  → the stretched crop template; simulation stops dead at `maturity_date`
  even if the requested assessment date is later — it never runs a
  post-harvest phase. Soil water carries through both transitions with no
  reset.
- **`replay_historical_seasons()` / `pasw_plume_from_replays()` /
  `historical_phase_percentiles()`** — replay the same fallow/plant/maturity
  month-day pattern across every year back to 1995 using that station's
  actual climate record, to build the 20–80%ile PASW band and the
  historical percentile columns in the water balance tables. Percentile
  comparisons for a still-in-progress season are truncated to the same
  day-count as what's actually been simulated, so a partial season doesn't
  get unfairly compared against complete historical totals.
- **`cover_preview()`** — cover curve only, no soil or climate needed
  (used earlier in development; the in-app chart now uses the real
  simulated cover data instead).

## Fixed assumptions (not exposed as inputs)

Set as constants near the top of `app.py`:
- `RESIDUE_COVER = 0.30` — applied consistently in both phases via
  `total_cover = green_cover + (1 - green_cover) * RESIDUE_COVER`, not read
  from the crop template's own residue column.
- `CROP_FACTOR = 1.0` — flat PET scalar; green cover already drives most
  of the ET partitioning via the HowLeaky Cover model in
  `waterbalance.partition_et()`.
- `SIM_FLOOR = 1 Jan 1995` — earliest selectable fallow start date, and the
  earliest year the historical replay goes back to.
- Default dates: Fallow start 1 Nov, Plant 1 Apr, Maturity 1 Nov, anchored
  to whichever cycle is current relative to today.
- Starting soil water defaults to 10% of PAWC.

## Crop cover template format

Templates use the same Excel layout `core/cover_excel.py` reads (`Main`
sheet, header on row 3, columns `Day No`, `Green Cover %`, `Residue Cover %`,
`Root Depth mm`, optional TUE/HI rows below — read but unused since this
app doesn't do yield). Only one template is bundled (`data/generic_crop.xlsx`)
and it's fixed, not user-selectable, in the current UI.

## Background climate prefetch

As soon as a station is confirmed (including auto-confirm when a search
only matches one station), a background thread starts downloading that
station's climate record while you finish setting up soil/crop/dates. It
only ever touches the filesystem disk cache — never `st.session_state`,
which isn't safe to write from a background thread — so the main thread
picks the result up naturally the next time it calls `ensure_climate_cached()`.
If you hit "Run water balance" before the background fetch finishes, it
waits on that same thread rather than starting a second, redundant download.

## Report

The "📄 Download report (Word)" button builds a `.docx` via `core/report.py`
(using `python-docx`, since this runs inside the deployed app on demand,
not generated ahead of time) with the run's inputs, both water balance
tables (with historical percentiles), and the soil water chart exactly as
shown in the app.

## Running

```bash
pip install -r requirements.txt
streamlit run app.py
```

No secrets or API keys needed — SILO's endpoint is public.

## Deploying (GitHub + Streamlit Community Cloud)

- `.gitignore` excludes `.silo_cache/`, `__pycache__/`, and
  `.streamlit/secrets.toml` — none of that should be committed.
- Community Cloud's filesystem is **ephemeral**: apps hibernate after 12
  hours with no traffic, and both hibernation-wake and any redeploy rebuild
  the container from GitHub, wiping `.silo_cache/` entirely. The on-disk
  cache still helps while an instance stays warm (shared across everyone
  hitting that instance), but don't expect it to persist for days the way
  it might running locally.
- **Python version**: chosen once, at deploy time, in the "Advanced
  settings" dialog — not changeable afterward without deleting and
  redeploying the app. `runtime.txt` is currently unreliable for this on
  Community Cloud (multiple live reports of it being ignored in favour of
  whatever the newest available Python is). If the app segfaults on
  startup with no Python traceback (just "Segmentation fault" in the
  logs), that's a strong sign the environment landed on a very new Python
  version paired with equally new numpy/pandas/matplotlib builds that
  haven't had their C-extension ABI shaken out yet — delete and redeploy,
  explicitly picking a mature Python version (3.11 or 3.12) in Advanced
  settings. `requirements.txt` also pins upper bounds on numpy/pandas/
  matplotlib for the same reason, rather than leaving them fully open-ended.

## Things you may want to revisit

- **Crop factor** is a flat scalar rather than a staged Kc curve — could
  add as an extra column alongside root depth in the template if the flat
  assumption doesn't hold up against real seasons.
- **Cover stretch assumes uniform scaling** — a template built for a
  150-day crop stretched to 200 days lengthens every stage proportionally,
  including any bare lead-in/residue tail. If particular stages (e.g. grain
  fill) should hold roughly constant length regardless of season length,
  that needs a smarter multi-segment stretch.
- **Only one crop template is bundled.** Adding more (e.g. per crop type)
  just means dropping more `.xlsx` files in `data/` and turning the fixed
  `GENERIC_CROP_PATH` back into a dropdown like soil type already is.
- No sample/offline climate dataset is bundled (unlike RiskAware's Dalby
  fallback) — `core/silo.py`'s `load_sample_data()` is still there and
  callable if you want to bundle a `sample_data/` folder later.
