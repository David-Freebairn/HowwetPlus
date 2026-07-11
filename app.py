"""
app.py — Soil Water Tracker: fallow through to a following crop
==================================================================
Tracks plant-available soil water continuously from a chosen fallow
start date, through a user-set sowing date, into crop transpiration,
using a single daily PERFECT/HowLeaky-style water balance.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import warnings
warnings.filterwarnings("ignore")

import streamlit as st
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import matplotlib.dates as mdates
import io
import threading
from datetime import date, timedelta

from core.silo import (search_stations, ensure_climate_cached, slice_climate, SiloUnavailableError,
                        fetch_station_met, _load_disk_cache, _save_disk_cache, _FULL_START, _full_end,
                        clear_stale_cache)
from core.soil_xml import read_soil_xml
from core.soil import read_prm
from core.cover_excel import read_cover_excel
from core.cropwater import (run_fallow_to_crop, replay_historical_seasons, pasw_plume_from_replays,
                             historical_phase_percentiles, percentile_rank, FallowCropConfig)
from core.report import build_report_docx

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Howwet? +",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="expanded",
)

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
HISTORY_YEARS = 20


@st.cache_resource
def _startup_cache_cleanup():
    """Runs once per app process (not per rerun/user) via st.cache_resource.
    Deletes disk-cached station climate files older than 7 days so
    .silo_cache/ doesn't grow unbounded as more stations get used."""
    return clear_stale_cache(max_age_days=7)


_startup_cache_cleanup()

C_HIST   = "#A8C4E0"
C_MEAN   = "#7B5EA7"
C_RECENT = "#1A2F6B"
C_BG     = "#F4F6F9"
C_CROP_SHADE = "#E7F3E4"

# Fixed cover/PET assumptions — not exposed as inputs. Applied consistently
# across both bare-fallow and in-crop phases (see core.cropwater.FallowCropConfig).
RESIDUE_COVER = 0.30
CROP_FACTOR   = 1.0


# ── Cached helpers ───────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def _search(query: str):
    return search_stations(query)


def _prefetch_worker(station_id, lat, lon):
    """
    Runs off the main thread. Deliberately uses only the filesystem disk
    cache (fetch_station_met + _save_disk_cache), never st.session_state or
    any other Streamlit API — session_state isn't safe to touch from a
    background thread. The main thread picks the result up naturally next
    time it calls ensure_climate_cached(), which checks the disk cache
    before hitting SILO.
    """
    try:
        df = fetch_station_met(station_id, _FULL_START, _full_end(), lat=lat, lon=lon)
        _save_disk_cache(station_id, df)
    except Exception:
        pass  # non-fatal — the Run button's own fetch will surface any real error


def start_climate_prefetch(station_info):
    """
    Kick off a background download for this station's climate record as
    soon as it's confirmed, so it's likely already cached by the time the
    person finishes picking soil/crop/dates and hits Run. Skips starting a
    thread if a fresh disk cache already exists, or one's already running
    for this station.
    """
    sid = station_info["id"]
    if _load_disk_cache(sid) is not None:
        return  # already fresh on disk, nothing to do
    running = st.session_state.get("prefetch_thread")
    if running is not None and st.session_state.get("prefetch_sid") == sid and running.is_alive():
        return  # already in flight for this station
    t = threading.Thread(target=_prefetch_worker, args=(sid, station_info.get("lat"), station_info.get("lon")),
                          daemon=True)
    st.session_state["prefetch_thread"] = t
    st.session_state["prefetch_sid"] = sid
    t.start()


def load_soil_files():
    for candidate in [DATA_DIR, HERE]:
        if candidate.exists():
            files = (sorted(candidate.glob("*.soil")) +
                     sorted(candidate.glob("*.xml")) +
                     sorted(candidate.glob("*.PRM")))
            if files:
                return files
    return []


def load_profile(soil_path: Path):
    if soil_path.suffix.lower() in (".soil", ".xml"):
        return read_soil_xml(soil_path)
    return read_prm(soil_path)


# ── Chart ────────────────────────────────────────────────────────────────────
def make_pasw_chart(df, profile, plant_date, maturity_date, stn_name, start_date, end_date, plume=None):
    plt.rcParams.update({
        "font.family": "sans-serif",
        "axes.facecolor": "#FAFBFC",
        "figure.facecolor": C_BG,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.linewidth": 0.8,
    })
    fig, ax = plt.subplots(figsize=(12, 5))
    fig.patch.set_facecolor(C_BG)

    pawc = profile.pawc_total

    # Shade the in-crop period
    crop_end = min(pd.Timestamp(maturity_date), df.index.max())
    ax.axvspan(pd.Timestamp(plant_date), crop_end,
               color=C_CROP_SHADE, zorder=0, label="In-crop period")

    if plume is not None:
        ax.fill_between(plume["dates"], plume["low"], plume["high"],
                         color=C_HIST, alpha=0.35, zorder=1,
                         label=f"20\u201380%ile ({plume['n_years']} historical seasons)")

    ax.plot(df.index, df["pasw"], color=C_RECENT, lw=2.4, zorder=4, label="Plant available soil water")
    ax.axhline(pawc, color="#CC4422", lw=0.9, ls="--", alpha=0.6, zorder=2,
               label=f"PAWC  {pawc:.0f} mm")
    ax.axvline(pd.Timestamp(plant_date), color="#2E7D32", lw=1.4, ls="-", alpha=0.8, zorder=3,
               label=f"Plant  {plant_date.strftime('%d %b %Y')}")
    ax.axvline(pd.Timestamp(maturity_date), color="#8a4b00", lw=1.2, ls="--", alpha=0.6, zorder=3,
               label=f"Maturity  {maturity_date.strftime('%d %b %Y')}")

    ax.set_ylabel("Plant available soil water (mm)", fontsize=10, color="#333")
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=6, integer=True))
    ax.tick_params(labelsize=9)
    ax.grid(axis="y", color="#E0E4EC", lw=0.6, zorder=0)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax.tick_params(axis="x", labelsize=8.5)

    # Crop cover overlay — soft/muted line on its own right-hand axis, so it
    # reads as context rather than competing with the soil water trace.
    ax2 = ax.twinx()
    ax2.plot(df.index, df["green_cover"] * 100, color="#4a7d2e", lw=1.3, alpha=0.5,
              zorder=1, label="Green cover % (RHS)")
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Cover %", fontsize=9, color="#5c8a4a")
    ax2.tick_params(axis="y", labelsize=8.5, colors="#5c8a4a")
    ax2.grid(False)
    ax2.spines["top"].set_visible(False)

    ax.set_xlim(pd.Timestamp(start_date), pd.Timestamp(maturity_date))

    lines1, labels1 = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9,
              frameon=True, framealpha=0.9, edgecolor="#CCCCCC")
    plt.tight_layout(pad=1.5)
    return fig


def make_et_chart(df, fallow_start, maturity_date):
    fig, ax = plt.subplots(figsize=(12, 3))
    ax.stackplot(df.index, df["soil_evap"], df["transp"],
                 colors=["#C8A464", "#3E8E5A"], labels=["Soil evaporation", "Transpiration"], alpha=0.85)
    ax.set_ylabel("mm/day", fontsize=9.5)
    ax.tick_params(labelsize=8.5)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax.set_xlim(pd.Timestamp(fallow_start), pd.Timestamp(maturity_date))
    ax.legend(loc="upper left", fontsize=8.5, frameon=True, framealpha=0.9)
    ax.grid(axis="y", color="#E0E4EC", lw=0.6)
    for sp in ["top", "right"]:
        ax.spines[sp].set_visible(False)
    plt.tight_layout(pad=1.0)
    return fig


def water_balance_table_md(sub_df: pd.DataFrame, hist_phase: dict = None):
    if sub_df is None or sub_df.empty:
        return None
    rain_t = sub_df["rain"].sum()
    ro_t   = sub_df["runoff"].sum()
    es_t   = sub_df["soil_evap"].sum()
    tr_t   = sub_df["transp"].sum()
    dr_t   = sub_df["drainage"].sum()
    dsw    = sub_df["sw_total"].iloc[-1] - sub_df["sw_total"].iloc[0]
    pct = (lambda x: x / rain_t * 100.0) if rain_t > 0 else (lambda x: 0.0)

    def pctile_col(key, value):
        if not hist_phase:
            return ""
        p = percentile_rank(value, hist_phase.get(key))
        return f"{p:.0f}th" if p is not None else "\u2014"

    header = "| Component | mm | % of rainfall |"
    sep    = "|---|---:|---:|"
    if hist_phase:
        header += " Historical %ile |"
        sep    += "---:|"

    rows = [
        ("Rainfall", rain_t, 100, "rain"),
        ("Runoff", ro_t, pct(ro_t), "runoff"),
        ("Soil evaporation", es_t, pct(es_t), "soil_evap"),
        ("Transpiration", tr_t, pct(tr_t), "transp"),
        ("Deep drainage", dr_t, pct(dr_t), "drainage"),
        ("**Change in soil water**", dsw, pct(dsw), "dsw"),
    ]
    lines = [header, sep]
    for label, val, pctval, key in rows:
        line = f"| {label} | {val:.0f} | {pctval:.0f} |"
        if hist_phase:
            line += f" {pctile_col(key, val)} |"
        lines.append(line)
    return "\n" + "\n".join(lines) + "\n"


# ── UI ───────────────────────────────────────────────────────────────────────
GENERIC_CROP_PATH = DATA_DIR / "generic_crop.xlsx"

st.markdown('# 🌱 Howwet? <sup style="font-size:0.55em;">+</sup>', unsafe_allow_html=True)
st.caption("*Following soil water – from fallow through to the following crop*")

soil_files = load_soil_files()
today      = date.today()
yesterday  = today - timedelta(days=1)

# ── Site select — collapses to a single line once a station is confirmed ────
if st.session_state.get("site_reset"):
    st.session_state["site_confirmed"] = False
    st.session_state.pop("site_info", None)
    st.session_state["station_query"] = ""
    st.session_state["site_reset"] = False

@st.fragment(run_every=2)
def _prefetch_status(sid):
    _t = st.session_state.get("prefetch_thread")
    if _t is not None and st.session_state.get("prefetch_sid") == sid and _t.is_alive():
        st.caption("⏳ Fetching climate history in the background — carry on setting up below.")
    elif _load_disk_cache(sid) is not None:
        st.caption("✅ Climate data ready.")


with st.container(border=True):
    if st.session_state.get("site_confirmed") and st.session_state.get("site_info"):
        station_info = st.session_state["site_info"]
        sc1, sc2 = st.columns([6, 1])
        with sc1:
            st.markdown(f"📍 **{station_info['label']}**")
        with sc2:
            if st.button("Change", key="site_change", width="stretch"):
                st.session_state["site_reset"] = True
                st.rerun()

        start_climate_prefetch(station_info)
        _prefetch_status(station_info["id"])
    else:
        query = st.text_input("station", label_visibility="collapsed",
                               placeholder="Search station — e.g. Dalby, Emerald  (press Enter)",
                               key="station_query")
        station_info = None
        if query and len(query) >= 3:
            with st.spinner("Searching..."):
                try:
                    stations = _search(query)
                except Exception as e:
                    st.error(f"Search failed: {e}")
                    stations = []
            if stations:
                if len(stations) == 1:
                    st.session_state["site_info"] = stations[0]
                    st.session_state["site_confirmed"] = True
                    st.rerun()
                labels = [s["label"] for s in stations]
                rc1, rc2 = st.columns([5, 1])
                with rc1:
                    chosen = st.selectbox("Station", labels, label_visibility="collapsed", key="station_pick")
                with rc2:
                    if st.button("Select", key="station_select", width="stretch"):
                        st.session_state["site_info"] = next(s for s in stations if s["label"] == chosen)
                        st.session_state["site_confirmed"] = True
                        st.rerun()
            else:
                st.warning("No stations found. Try a shorter search term.")

st.markdown("**Set up the soil**")

c1, c2 = st.columns(2)
with c1:
    if soil_files:
        soil_idx = st.selectbox("Soil type", range(len(soil_files)),
                                 format_func=lambda i: soil_files[i].stem, key="soil_pick")
        soil_path = soil_files[soil_idx]
    else:
        st.error(f"No soil (.soil/.xml/.PRM) files found in {DATA_DIR}")
        soil_path = None
with c2:
    init_pct = st.number_input("Start soil water (% PAWC)", min_value=0, max_value=100,
                                value=10, step=5, key="init_pct")

crop_path = GENERIC_CROP_PATH if GENERIC_CROP_PATH.exists() else None
if crop_path is None:
    st.error(f"Generic crop cover template not found at {GENERIC_CROP_PATH}")

SIM_FLOOR = date(1995, 1, 1)  # earliest allowed simulation start


def default_season_dates(today: date):
    """
    Default fallow start / plant / maturity dates: 1 Nov -> 1 Apr -> 1 Nov,
    anchored to whichever cycle is currently in progress or most recently
    completed relative to today.
    """
    if today.month >= 11:
        fallow_yr = today.year
    else:
        fallow_yr = today.year - 1
    fallow_start = date(fallow_yr, 11, 1)
    plant_date = date(fallow_yr + 1, 4, 1)
    maturity_date = date(fallow_yr + 1, 11, 1)
    return fallow_start, plant_date, maturity_date


_default_fallow, _default_plant, _default_maturity = default_season_dates(today)

d1, d2, d3, d4 = st.columns(4)
with d1:
    fallow_start = st.date_input("Fallow start", value=_default_fallow,
                                  min_value=SIM_FLOOR, max_value=yesterday,
                                  format="DD/MM/YYYY", key="fallow_start")
with d2:
    plant_date = st.date_input("Plant date", value=_default_plant,
                                min_value=fallow_start, max_value=today + timedelta(days=180),
                                format="DD/MM/YYYY", key="plant_date")
with d3:
    maturity_date = st.date_input("Maturity date", value=_default_maturity,
                                   min_value=plant_date, format="DD/MM/YYYY", key="maturity_date")
with d4:
    end_date = st.date_input("How are we going as of", value=yesterday,
                              min_value=fallow_start, max_value=yesterday,
                              format="DD/MM/YYYY", key="end_date")


with st.expander("ℹ️ About this analysis"):
    st.markdown("""
This tool runs one continuous daily soil-water balance from your **fallow start**
date through to **today** (or your chosen assessment date), switching automatically
from bare-fallow cover to your crop's cover/root development at **plant date**,
and back to bare fallow after **maturity date**.

- Before planting: bare soil, no transpiration — only surface residue cover and
  soil evaporation, same as tracking a fallow.
- Your crop cover template only needs to represent a *typical* growth shape —
  it's automatically stretched or compressed so its growth window lines up
  exactly with your chosen plant → maturity dates.
- From planting onward, green cover, root depth and residue drive both
  transpiration and how ET is split between the soil surface and the crop.
- Soil water carries through planting with no reset — this is a single
  continuous bucket, not two separate simulations stitched together.
""")

# ── Run ──────────────────────────────────────────────────────────────────────
missing = []
if not station_info:
    missing.append("a weather station — search and select one above")
if not soil_path:
    missing.append("a soil type")
if not crop_path:
    missing.append("a crop cover template")

run_clicked = st.button("Run water balance", type="primary", disabled=bool(missing))
if missing:
    st.caption("⚠️ Still need: " + "; ".join(missing) + ".")

if run_clicked and not missing:
    try:
        profile = load_profile(soil_path)
    except Exception as e:
        st.error(f"Could not load soil: {e}")
        st.stop()

    try:
        cover_schedule = read_cover_excel(crop_path)
    except Exception as e:
        st.error(f"Could not load crop cover template: {e}")
        st.stop()

    sid = station_info["id"]
    sim_end = min(end_date, maturity_date)
    status = st.empty()

    _t = st.session_state.get("prefetch_thread")
    if _t is not None and st.session_state.get("prefetch_sid") == sid and _t.is_alive():
        status.info("Finishing background climate download...")
        _t.join()

    status.info("Loading climate data… (first load may take 30–60 seconds)")
    try:
        full_met = ensure_climate_cached(sid, station_info.get("lat"), station_info.get("lon"),
                                          session_state=st.session_state)
        met = slice_climate(full_met, start=fallow_start, end=sim_end)
        if met.empty:
            raise RuntimeError(f"No climate data between {fallow_start} and {sim_end}.")
    except SiloUnavailableError as e:
        status.empty()
        st.warning(f"⚠️ SILO is currently unavailable ({e}).")
        st.stop()
    except Exception as e:
        status.empty()
        st.error(f"Climate data load failed: {e}")
        st.stop()

    status.info("Running water balance...")
    config = FallowCropConfig(residue_cover=RESIDUE_COVER, crop_factor=CROP_FACTOR)
    try:
        df, sw0, swf = run_fallow_to_crop(
            met, profile, plant_date, maturity_date, cover_schedule,
            config=config, sw_init_frac=init_pct / 100.0,
        )
    except Exception as e:
        status.empty()
        st.error(f"Simulation failed: {e}")
        st.stop()

    status.info("Building historical 20\u201380%ile band...")
    try:
        replays = replay_historical_seasons(
            full_met, profile, fallow_start, plant_date, maturity_date, cover_schedule,
            config=config, sw_init_frac=init_pct / 100.0, first_year=1995,
        )
        plume = pasw_plume_from_replays(replays, fallow_start)
        n_fallow_days = int((df["phase"] == "fallow").sum())
        n_crop_days   = int((df["phase"] == "crop").sum())
        hist_pct = historical_phase_percentiles(
            replays,
            fallow_days=n_fallow_days or None,
            crop_days=n_crop_days or None,
        )
    except Exception:
        plume = None
        hist_pct = None
    status.empty()

    st.session_state["result"] = {
        "df": df, "profile": profile, "stn_name": station_info["name"],
        "fallow_start": fallow_start, "plant_date": plant_date,
        "maturity_date": maturity_date, "end_date": end_date, "sim_end": sim_end,
        "plume": plume, "hist_pct": hist_pct,
    }

# ── Output ───────────────────────────────────────────────────────────────────
if st.session_state.get("result"):
    r = st.session_state["result"]
    df, profile = r["df"], r["profile"]
    pawc = profile.pawc_total

    final_pasw = float(df["pasw"].iloc[-1])
    pawc_pct   = final_pasw / pawc * 100 if pawc > 0 else 0.0
    cum_rain   = float(df["rain"].sum())
    at_plant   = df[df.index.date == r["plant_date"]]
    pasw_at_plant = float(at_plant["pasw"].iloc[0]) if len(at_plant) else None

    st.markdown(f"""
<div style="background:#f0f6ff; border-radius:10px; padding:18px 22px 14px 22px; margin-bottom:4px;">
  <div style="font-size:1.45rem; font-weight:700; color:#1a3a5c;">Soil water — fallow to crop</div>
  <div style="font-size:0.95rem; color:#444; margin-bottom:10px;">
    <b>{r['stn_name']}</b> &nbsp;·&nbsp; {profile.name} &nbsp;·&nbsp;
    {r['fallow_start'].strftime('%d %b %Y')} → {r['sim_end'].strftime('%d %b %Y')}
  </div>
  <div style="display:flex; gap:26px; flex-wrap:wrap;">
    <span><span style="color:#888">Current PASW&nbsp;</span>
      <b style="color:#1a3a5c; font-size:1.25rem;">{final_pasw:.0f} mm</b>
      <span style="color:#2979c4;"> ({pawc_pct:.0f}% of PAWC)</span></span>
    {"<span><span style='color:#888'>PASW at planting&nbsp;</span><b style='color:#1a3a5c'>%.0f mm</b></span>" % pasw_at_plant if pasw_at_plant is not None else ""}
    <span><span style="color:#888">Total rainfall&nbsp;</span><b>{cum_rain:.0f} mm</b></span>
  </div>
</div>
""", unsafe_allow_html=True)
    if r["sim_end"] < r["end_date"]:
        st.caption(f"ℹ️ Your assessment date ({r['end_date'].strftime('%d %b %Y')}) is after maturity "
                   f"({r['maturity_date'].strftime('%d %b %Y')}) — simulation stops at maturity, no post-harvest run.")

    fig = make_pasw_chart(df, profile, r["plant_date"], r["maturity_date"], r["stn_name"],
                           r["fallow_start"], r["sim_end"], plume=r.get("plume"))
    st.pyplot(fig, width="stretch")
    if r.get("plume") is None:
        st.caption("ℹ️ Not enough historical seasons with full climate coverage since 1995 to build a 20–80%ile band for this site/date combination.")

    fig2 = make_et_chart(df, r["fallow_start"], r["maturity_date"])
    st.pyplot(fig2, width="stretch")

    with st.expander("📊 Water balance details"):
        fallow_df = df[df["phase"] == "fallow"]
        crop_df   = df[df["phase"] == "crop"]
        hist_pct  = r.get("hist_pct")

        wc1, wc2 = st.columns(2)
        with wc1:
            st.markdown(f"**Fallow**  {r['fallow_start'].strftime('%d %b %Y')} → {r['plant_date'].strftime('%d %b %Y')}")
            tbl = water_balance_table_md(fallow_df, hist_pct["fallow"] if hist_pct else None)
            st.markdown(tbl if tbl else "_No fallow days in this run._")
        with wc2:
            st.markdown(f"**In-crop**  {r['plant_date'].strftime('%d %b %Y')} → {r['sim_end'].strftime('%d %b %Y')}")
            tbl2 = water_balance_table_md(crop_df, hist_pct["crop"] if hist_pct else None)
            st.markdown(tbl2 if tbl2 else "_No in-crop days in this run yet._")

        st.caption(f"PAWC: {pawc:.0f} mm" +
                   (f"  |  Historical %ile ranks each total against {r['plume']['n_years']} replayed seasons since 1995"
                    if r.get("plume") else ""))

    st.download_button(
        "📥 Download daily results (CSV)",
        data=df.drop(columns=["sw_layers"]).to_csv().encode(),
        file_name=f"SoilWater_{r['stn_name'].replace(' ', '_')}_{r['fallow_start']}_{r['sim_end']}.csv",
        mime="text/csv",
    )

    try:
        chart_buf = io.BytesIO()
        fig.savefig(chart_buf, format="png", dpi=150, bbox_inches="tight")
        chart_png = chart_buf.getvalue()

        report_inputs = {
            "Station": r["stn_name"],
            "Soil type": profile.name,
            "Crop template": GENERIC_CROP_PATH.stem,
            "Fallow start": r["fallow_start"].strftime("%d %b %Y"),
            "Plant date": r["plant_date"].strftime("%d %b %Y"),
            "Maturity date": r["maturity_date"].strftime("%d %b %Y"),
            "Assessment date": r["end_date"].strftime("%d %b %Y"),
            "Simulated to": r["sim_end"].strftime("%d %b %Y"),
            "Starting soil water": f"{init_pct}% of PAWC",
            "Residue cover (fixed)": f"{RESIDUE_COVER * 100:.0f}%",
            "Crop factor (fixed)": f"{CROP_FACTOR:.2f}",
            "PAWC": f"{pawc:.0f} mm",
        }
        report_bytes = build_report_docx(report_inputs, df, r.get("hist_pct"), r.get("plume"), chart_png)

        st.download_button(
            "📄 Download report (Word)",
            data=report_bytes,
            file_name=f"Howwet+_Report_{r['stn_name'].replace(' ', '_')}_{r['sim_end']}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as e:
        st.warning(f"Couldn't build the report: {e}")
