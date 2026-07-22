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
                             historical_phase_percentiles, percentile_rank, transpiration_percentiles,
                             transp_plume_from_replays, FallowCropConfig)
from core.report import build_report_docx
from core.nitrogen import n_mineralisation_gain, n_plume_from_replays, fallow_n_historical_values
from core.yield_n import in_crop_n_median, yield_n_budget

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
ASSETS_DIR = HERE / "assets"
ICON_PATH = ASSETS_DIR / "howwet_icon.png"
REPORT_ICON_PATH = ASSETS_DIR / "howwet_icon_report.png"

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Howwet? +",
    page_icon=str(ICON_PATH) if ICON_PATH.exists() else "🌱",
    layout="wide",
    initial_sidebar_state="expanded",
)


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

# Yield & Nitrogen calculator defaults — per "Dashboard final" workbook note
# ("Default values (add top of .py file)"). TUE currently only used to
# suggest a starting yield estimate; NUE grosses up N required from grain N.
# Bumped by hand whenever a change ships — shown in the Diagnostics panel so
# you can confirm a deployed instance is actually running the latest push.
APP_VERSION = "2026-07-17"

TUE_KG_PER_MM = 20.0   # transpiration use efficiency, kg grain / mm transpired — default suggestion only, editable live in the calculator
NUE_PCT       = 50.0   # nitrogen use efficiency (%) — default suggestion only, editable live in the calculator
DEFAULT_MINERALISATION_COEFFICIENT = 0.00017   # editable pre-run, overrides the soil file's own value

# Season date defaults
MATURITY_MONTHS_AFTER_PLANT = 5   # suggested maturity date = plant date + this many months, until overridden

# Calculator input defaults (all editable in the UI — these are just the
# starting values shown before the user changes anything)
DEFAULT_START_SOIL_WATER_PCT = 10     # % of PAWC
DEFAULT_PROTEIN_PCT          = 13.0   # %
DEFAULT_START_N              = 10.0   # kg N/ha, soil test or estimate
DEFAULT_FERT_N               = 0.0    # kg N/ha

ABOUT_TEXT = """
**Howwet+** tracks soil water and nitrogen mineralisation through a fallow and following crop using weather records, daily water balance accounting (rain, evaporation, runoff, drainage) to provide an estimate of where we are now in relation to past seasons (since 1995). 

To get started:

-	**Select a location** and **soil type** that best match your situation. It may **take a minute** to load climate data initially **(please be patient)**
-	**Adjust dates:** for **start of fallow, planting** and **crop maturity**. 
-	Select **Run water balance** to updates each analysis. 

Rather than focus on values of soil water and NO3 mineralisation estimates, **focus on where each trace sits** within the blue 20% - 80%ile plume and 50%ile (median) line.

**Results** are presented as:
-	**A graph** showing **soil water** for the current season in relation to a plume that describes the years between the 1in 5 driest and 1 in 5 wettest years. Crop cover is shown once a crop starts.
-	**A graph** showing **nitrate mineralisation** like the soil water graph above it.
-	A **Nitrogen budget table** with average yield, target protein, soil nitrogen test results and fertiliser rates **inputs**. This table is **interactive**.
-	**Water balance details** tab summarising rainfall, evaporation, transpiration, runoff, deep drainage and soil water changes
-	**Download report** button provides a MSWord document summarising soil water and nitrogen behaviour.
-	A **Diagnostics** tab is available to explore further detail in the models used (**not recommended**).
When inputs are edited, hit the **Run water balance** to updates results. 

**Assumptions**

**Default values** provided for most inputs but are easily modified to suit each paddocks. Hit the **Run water balance** button after changes.
**Stubble cover** is set to 30% throughout, reflecting an average condition. Bare soil paddocks will have reduced soil water and higher runoff.
**Starting soil water** is evenly distributed in the soil profile and set at 10%. **Adjust** if you have a better information. The most important indicator is where the **current season sits within longer-term conditions**.This is, is this season better or worse?

**Comments welcomed** David Freebairn: david.freebairn@gmail.com

**Disclosure**

This app aims to demonstrate value adding using recent and long term weather data and well tested water balance models. Output from these analyses should be used for **comparative purposes only** – how does this season compare with the longer term?

**Acknowledgements**

**Weather data:** Queensland Government's SILO database sourced from the Bureau of Meteorology and the many voluntary weather recorders across the Australian continent since the 1890’s 

**Soil water ** estimates use a well-tested water balance model used in cropping system model models (PERFECT, Howwet? SoilWaterApp and ApSim).

**App icon** generated using ChatGPT (GPT-5.5 image generation, OpenAI, 2026) from the developer’s design concept.

**References**

Anthropic. (2026). Claude (Sonnet 4.6) [Large language model]. https://claude.ai

ChatGPT (GPT-5.5 image generation, OpenAI, 2026).

Freebairn, D.M., Ghahramani, A., Robinson, J.B., and McClymont, D. (2018). A tool for monitoring soil water using modelling, on-farm data, and mobile technology Environmental Modelling & Software 104 (2018) 55e63 https://www.sciencedirect.com/science/article/pii/S1364815217312422

Freebairn, D.M., Hamilton, A.H., Cox, P.G. and Holzworth, D. (1994). HOWWET? Estimating the storage of water in your soil using rainfall records: A computer program. Agricultural Production Systems Research Unit, QDPI–CSIRO, Toowoomba,

Littleboy, M., Silburn, D.M., Freebairn, D.M., Woodruff, D.R. and Hammer, G.L. (1989). PERFECT: A simulation model of Productivity Erosion Runoff Functions to Evaluate Conservation Techniques. QDPI Bulletin QB89005. Queensland Department of Primary Industries, Brisbane, Australia.

McCown, R.L., Hammer, G.L., Hargreaves, J.N.G., Holzworth, D. and Freebairn, D.M. (1996). APSIM: A novel software system for model development, model testing, and simulation in agricultural systems research. Agricultural Systems, 50, 255–271.

OpenAI. (2026). Howwet+ app icon [AI-generated image]. ChatGPT (GPT-5.5 with image generation). https://chat.openai.com.

"""


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
def make_pasw_chart(df, profile, plant_date, maturity_date, stn_name, start_date, plume=None):
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

    # Shade the in-crop period (visually self-evident — no legend entry)
    crop_end = min(pd.Timestamp(maturity_date), df.index.max())
    ax.axvspan(pd.Timestamp(plant_date), crop_end, color=C_CROP_SHADE, zorder=0)

    if plume is not None:
        ax.fill_between(plume["dates"], plume["low"], plume["high"], color=C_HIST, alpha=0.35, zorder=1)
        median = plume.get("median")  # older cached sessions may predate this key
        if median is not None:
            ax.plot(plume["dates"], median, color="#8FD3FE", lw=1.6, zorder=2)

        # Anchor labels a little before the right edge (not at the very last
        # point) and right-align them, so they sit inside the plot area
        # rather than fighting the twin cover axis for space in the margin.
        anchor_i = max(0, int(len(plume["dates"]) * 0.50) - 1)
        anchor_x = plume["dates"][anchor_i]
        ax.annotate("80%", xy=(anchor_x, plume["high"][anchor_i]), xytext=(0, 4), textcoords="offset points",
                    fontsize=8, color="#5E7A99", va="bottom", ha="right")
        ax.annotate("20%", xy=(anchor_x, plume["low"][anchor_i]), xytext=(0, -4), textcoords="offset points",
                    fontsize=8, color="#5E7A99", va="top", ha="right")
        if median is not None:
            ax.annotate("50%", xy=(anchor_x, median[anchor_i]), xytext=(0, 4), textcoords="offset points",
                        fontsize=8, color="#4F9FD6", va="bottom", ha="right")

    ax.plot(df.index, df["pasw"], color=C_RECENT, lw=2.4, zorder=4, label="Plant available soil water")
    ax.axhline(pawc, color="#CC4422", lw=0.9, ls="--", alpha=0.6, zorder=2)
    ax.annotate(f"PAWC {pawc:.0f} mm", xy=(pd.Timestamp(start_date), pawc), xytext=(6, 4),
                textcoords="offset points", fontsize=8, color="#CC4422", va="bottom", ha="left")
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
    ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left", bbox_to_anchor=(0.0, 0.90),
              fontsize=9, frameon=True, framealpha=0.9, edgecolor="#CCCCCC")
    plt.tight_layout(pad=1.5)
    fig.subplots_adjust(right=0.90)
    return fig


def make_pasw_chart_interactive(df, profile, plant_date, maturity_date, fallow_start, sim_end, plume=None):
    """
    Interactive companion to make_pasw_chart() — same data, Plotly instead
    of matplotlib, so moving the mouse tracks a vertical line across the
    PASW/plume/median traces with live values. The cover overlay is
    deliberately excluded from that hover tracking (hoverinfo='skip') per
    request — it's still visible on the chart, just not part of the
    tracked tooltip, since it's context rather than the thing being read
    off precisely.
    """
    import plotly.graph_objects as go

    pawc = profile.pawc_total
    fig = go.Figure()

    crop_end = min(pd.Timestamp(maturity_date), df.index.max())
    fig.add_vrect(x0=pd.Timestamp(plant_date), x1=crop_end, fillcolor=C_CROP_SHADE,
                  opacity=0.6, line_width=0, layer="below")

    if plume is not None:
        median = plume.get("median")
        fig.add_trace(go.Scatter(x=plume["dates"], y=plume["high"], mode="lines",
                                  line=dict(width=0), hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter(x=plume["dates"], y=plume["low"], mode="lines",
                                  line=dict(width=0), fill="tonexty", fillcolor="rgba(168,196,224,0.35)",
                                  hoverinfo="skip", showlegend=False, name="20\u201380%ile"))
        if median is not None:
            fig.add_trace(go.Scatter(x=plume["dates"], y=median, mode="lines",
                                      line=dict(color="#8FD3FE", width=1.6), name="Median (historical)",
                                      hovertemplate="Median: %{y:.0f} mm<extra></extra>"))

        anchor_i = max(0, int(len(plume["dates"]) * 0.50) - 1)
        anchor_x = plume["dates"][anchor_i]
        fig.add_annotation(x=anchor_x, y=plume["high"][anchor_i], text="80%", showarrow=False,
                           font=dict(color="#5E7A99", size=11), yanchor="bottom", xanchor="center")
        fig.add_annotation(x=anchor_x, y=plume["low"][anchor_i], text="20%", showarrow=False,
                           font=dict(color="#5E7A99", size=11), yanchor="top", xanchor="center")
        if median is not None:
            fig.add_annotation(x=anchor_x, y=median[anchor_i], text="50%", showarrow=False,
                               font=dict(color="#4F9FD6", size=11), yanchor="bottom", xanchor="center")

    fig.add_trace(go.Scatter(x=df.index, y=df["pasw"], mode="lines",
                              line=dict(color=C_RECENT, width=2.6), name="Plant available soil water",
                              hovertemplate="PASW: %{y:.0f} mm<extra></extra>"))

    # add_shape + add_annotation instead of add_hline/add_vline(annotation_text=...):
    # the latter's annotation-position math calls Python's sum() on the
    # line's x-coordinates, which recent pandas no longer allows for
    # Timestamp objects (TypeError on add/radd) — this sidesteps it entirely.
    fig.add_shape(type="line", xref="paper", x0=0, x1=1, yref="y", y0=pawc, y1=pawc,
                  line=dict(color="#CC4422", width=1, dash="dash"))
    fig.add_annotation(xref="paper", x=0, yref="y", y=pawc, text=f"PAWC {pawc:.0f} mm",
                       showarrow=False, font=dict(color="#CC4422", size=11),
                       xanchor="left", yanchor="bottom")

    plant_x = pd.Timestamp(plant_date)
    fig.add_shape(type="line", xref="x", x0=plant_x, x1=plant_x, yref="paper", y0=0, y1=1,
                  line=dict(color="#2E7D32", width=1.6))
    fig.add_annotation(xref="x", x=plant_x, yref="paper", y=1,
                       text=f"Plant {plant_date.strftime('%d %b %Y')}",
                       showarrow=False, font=dict(color="#2E7D32", size=11), yanchor="bottom")

    maturity_x = pd.Timestamp(maturity_date)
    fig.add_shape(type="line", xref="x", x0=maturity_x, x1=maturity_x, yref="paper", y0=0, y1=1,
                  line=dict(color="#8a4b00", width=1.3, dash="dash"))
    fig.add_annotation(xref="x", x=maturity_x, yref="paper", y=1,
                       text=f"Maturity {maturity_date.strftime('%d %b %Y')}",
                       showarrow=False, font=dict(color="#8a4b00", size=11), yanchor="bottom")

    # Cover overlay — right-hand axis, visible but NOT part of hover tracking
    fig.add_trace(go.Scatter(x=df.index, y=df["green_cover"] * 100, mode="lines",
                              line=dict(color="#4a7d2e", width=1.3), opacity=0.5,
                              name="Green cover % (RHS)", yaxis="y2", hoverinfo="skip"))

    fig.update_layout(
        height=500,
        hovermode="x unified",
        plot_bgcolor="#FAFBFC",
        paper_bgcolor=C_BG,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis=dict(range=[pd.Timestamp(fallow_start), pd.Timestamp(maturity_date)],
                   showgrid=False),
        yaxis=dict(title="Plant available soil water (mm)", rangemode="tozero",
                   gridcolor="#E0E4EC"),
        yaxis2=dict(title="Cover %", overlaying="y", side="right", range=[0, 100],
                    showgrid=False),
    )
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


def make_n_mineralisation_chart(ng: pd.DataFrame, plant_date, maturity_date, fallow_start, n_plume=None):
    fig, ax = plt.subplots(figsize=(12, 4.2))

    if n_plume is not None:
        ax.fill_between(n_plume["dates"], n_plume["low"], n_plume["high"],
                         color=C_HIST, alpha=0.35, zorder=1)
        ax.plot(n_plume["dates"], n_plume["median"], color="#8FD3FE", lw=1.6, zorder=2)
        anchor_i = max(0, int(len(n_plume["dates"]) * 0.50) - 1)
        anchor_x = n_plume["dates"][anchor_i]
        ax.annotate("80%", xy=(anchor_x, n_plume["high"][anchor_i]), xytext=(0, 4), textcoords="offset points",
                    fontsize=8, color="#5E7A99", va="bottom", ha="right")
        ax.annotate("20%", xy=(anchor_x, n_plume["low"][anchor_i]), xytext=(0, -4), textcoords="offset points",
                    fontsize=8, color="#5E7A99", va="top", ha="right")
        ax.annotate("50%", xy=(anchor_x, n_plume["median"][anchor_i]), xytext=(0, 4), textcoords="offset points",
                    fontsize=8, color="#4F9FD6", va="bottom", ha="right")

    ax.plot(ng.index, ng["cum_n_kgha"], color="#2E9E3F", lw=2.4, zorder=4)
    ax.annotate("Nitrate mineralisation", xy=(ng.index[int(len(ng) * 0.5)], ng["cum_n_kgha"].iloc[int(len(ng) * 0.5)]),
                xytext=(0, 8), textcoords="offset points", fontsize=8.5, color="#1E7A2E",
                va="bottom", ha="center", fontweight="bold")

    ax.axvline(pd.Timestamp(plant_date), color="#2E7D32", lw=1.4, ls="-", alpha=0.8, zorder=3,
               label=f"Plant  {plant_date.strftime('%d %b %Y')}")
    ax.axvline(pd.Timestamp(maturity_date), color="#8a4b00", lw=1.2, ls="--", alpha=0.6, zorder=3,
               label=f"Maturity  {maturity_date.strftime('%d %b %Y')}")

    ax.set_ylabel("Nitrate mineralisation (kg/ha)", fontsize=9.5, color="#1E7A2E")
    ax.set_ylim(bottom=0)
    ax.tick_params(axis="y", labelsize=8.5, colors="#1E7A2E")
    ax.tick_params(axis="x", labelsize=8.5)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax.set_xlim(pd.Timestamp(fallow_start), pd.Timestamp(maturity_date))
    ax.grid(axis="y", color="#E0E4EC", lw=0.6, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.legend(loc="upper left", fontsize=9, frameon=True, framealpha=0.9, edgecolor="#CCCCCC")

    plt.tight_layout(pad=1.2)
    return fig


def make_temp_moisture_chart(ng: pd.DataFrame, plant_date, fallow_start, maturity_date):
    fig, ax_t = plt.subplots(figsize=(12, 3))

    ax_t.plot(ng.index, ng["tmean_30d"], color="#D9534F", lw=1.6, zorder=3, label="Temperature (30-day avg)")
    ax_t.set_ylabel("Mean temperature (\u00b0C)", fontsize=9, color="#D9534F")
    ax_t.tick_params(axis="y", labelsize=8.5, colors="#D9534F")
    ax_t.tick_params(axis="x", labelsize=8.5)
    ax_t.xaxis.set_major_locator(mdates.MonthLocator())
    ax_t.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax_t.set_xlim(pd.Timestamp(fallow_start), pd.Timestamp(maturity_date))
    ax_t.grid(axis="y", color="#E0E4EC", lw=0.6, zorder=0)
    ax_t.spines["top"].set_visible(False)

    ax_m = ax_t.twinx()
    ax_m.plot(ng.index, ng["surface_moisture_mm"], color="#3E7CB1", lw=1.3, alpha=0.85, zorder=2,
              label="Surface moisture (rel. to wilting point)")
    ax_m.axhline(0, color="#3E7CB1", lw=0.6, ls=":", alpha=0.5, zorder=1)
    ax_m.set_ylabel("Surface moisture, rel. to wilting point (mm)", fontsize=8.5, color="#3E7CB1")
    ax_m.tick_params(axis="y", labelsize=8, colors="#3E7CB1")
    ax_m.grid(False)
    ax_m.spines["top"].set_visible(False)

    ax_t.axvline(pd.Timestamp(plant_date), color="#2E7D32", lw=1.4, ls="-", alpha=0.8, zorder=4,
                 label=f"Plant  {plant_date.strftime('%d %b %Y')}")

    lines1, labels1 = ax_t.get_legend_handles_labels()
    lines2, labels2 = ax_m.get_legend_handles_labels()
    ax_t.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8.5,
                frameon=True, framealpha=0.9, edgecolor="#CCCCCC")

    plt.tight_layout(pad=1.0)
    return fig



def make_n_factors_chart(ng: pd.DataFrame, fallow_start, maturity_date):
    """
    Diagnostic chart: the raw 0-1 moisture/temperature/limiting factors
    that actually drive daily N mineralisation (not the physical
    temperature/moisture values themselves — see the separate
    Temperature/surface moisture chart for those). All three share the
    same 0-1 scale, so they're directly comparable on one axis — useful
    for seeing which factor is actually constraining mineralisation, and
    whether the apparent low variability in mineralisation reflects one
    factor being pinned near its ceiling most of the season.
    """
    fig, ax = plt.subplots(figsize=(12, 3.2))
    ax.plot(ng.index, ng["moist_factor"], color="#3E7CB1", lw=1.4, label="Moisture factor")
    ax.plot(ng.index, ng["temp_factor"], color="#D9534F", lw=1.4, label="Temperature factor")
    ax.plot(ng.index, ng["limiting_factor"], color="#2E9E3F", lw=2.0, label="Limiting factor (used)")
    ax.set_ylabel("Factor (0\u20131)", fontsize=9.5)
    ax.set_ylim(-0.02, 1.05)
    ax.tick_params(labelsize=8.5)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax.set_xlim(pd.Timestamp(fallow_start), pd.Timestamp(maturity_date))
    ax.grid(axis="y", color="#E0E4EC", lw=0.6)
    for sp in ["top", "right"]:
        ax.spines[sp].set_visible(False)
    ax.legend(loc="upper left", fontsize=8.5, frameon=True, framealpha=0.9)
    plt.tight_layout(pad=1.0)
    return fig


def make_transp_chart(df: pd.DataFrame, plant_date, maturity_date, fallow_start, transp_plume=None):
    fig, ax = plt.subplots(figsize=(12, 4.2))
    cum_transp = df["transp"].cumsum()

    if transp_plume is not None:
        ax.fill_between(transp_plume["dates"], transp_plume["low"], transp_plume["high"],
                         color=C_HIST, alpha=0.35, zorder=1)
        ax.plot(transp_plume["dates"], transp_plume["median"], color="#8FD3FE", lw=1.6, zorder=2)
        anchor_i = max(0, int(len(transp_plume["dates"]) * 0.50) - 1)
        anchor_x = transp_plume["dates"][anchor_i]
        ax.annotate("80%", xy=(anchor_x, transp_plume["high"][anchor_i]), xytext=(0, 4), textcoords="offset points",
                    fontsize=8, color="#5E7A99", va="bottom", ha="right")
        ax.annotate("20%", xy=(anchor_x, transp_plume["low"][anchor_i]), xytext=(0, -4), textcoords="offset points",
                    fontsize=8, color="#5E7A99", va="top", ha="right")
        ax.annotate("50%", xy=(anchor_x, transp_plume["median"][anchor_i]), xytext=(0, 4), textcoords="offset points",
                    fontsize=8, color="#4F9FD6", va="bottom", ha="right")

    ax.plot(df.index, cum_transp, color="#3E8E5A", lw=2.4, zorder=4)
    ax.annotate("Transpiration", xy=(df.index[int(len(df) * 0.5)], cum_transp.iloc[int(len(df) * 0.5)]),
                xytext=(0, 8), textcoords="offset points", fontsize=8.5, color="#2C6B43",
                va="bottom", ha="center", fontweight="bold")

    ax.axvline(pd.Timestamp(plant_date), color="#2E7D32", lw=1.4, ls="-", alpha=0.8, zorder=3,
               label=f"Plant  {plant_date.strftime('%d %b %Y')}")
    ax.axvline(pd.Timestamp(maturity_date), color="#8a4b00", lw=1.2, ls="--", alpha=0.6, zorder=3,
               label=f"Maturity  {maturity_date.strftime('%d %b %Y')}")

    ax.set_ylabel("Cumulative transpiration (mm)", fontsize=9.5, color="#2C6B43")
    ax.set_ylim(bottom=0)
    ax.tick_params(axis="y", labelsize=8.5, colors="#2C6B43")
    ax.tick_params(axis="x", labelsize=8.5)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    ax.set_xlim(pd.Timestamp(fallow_start), pd.Timestamp(maturity_date))
    ax.grid(axis="y", color="#E0E4EC", lw=0.6, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.legend(loc="upper left", fontsize=9, frameon=True, framealpha=0.9, edgecolor="#CCCCCC")

    plt.tight_layout(pad=1.2)
    return fig


def water_balance_table_md(sub_df: pd.DataFrame, hist_phase: dict = None, extra_row=None):
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

    if extra_row is not None:
        label, val, pctile_str = extra_row
        line = f"| {label} | {val:.0f} | \u2014 |"
        if hist_phase:
            line += f" {pctile_str if pctile_str is not None else '\u2014'} |"
        lines.append(line)

    return "\n" + "\n".join(lines) + "\n"


# ── UI ───────────────────────────────────────────────────────────────────────
GENERIC_CROP_PATH = DATA_DIR / "generic_crop.xlsx"

title_col1, title_col2 = st.columns([1, 11], vertical_alignment="center")
with title_col1:
    if ICON_PATH.exists():
        st.image(str(ICON_PATH), width=64)
    else:
        st.markdown("### 🌱")
with title_col2:
    st.markdown('# Howwet? <sup style="font-size:0.55em;">+</sup>', unsafe_allow_html=True)
caption_col, about_col = st.columns([8, 1], vertical_alignment="top")
with caption_col:
    st.caption("*Following soil water and nitrate mineralisation – from fallow start to crop maturity*")
with about_col:
    with st.popover("ℹ️ About"):
        st.markdown(ABOUT_TEXT)
st.caption(f"v{APP_VERSION}")

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
                                value=DEFAULT_START_SOIL_WATER_PCT, step=5, key="init_pct")

# The mineralisation coefficient widget itself lives in Diagnostics ->
# Tuning constants (post-run), so it's visible alongside TUE/NUE. Before
# it's ever been rendered (very first run in a session), fall back to the
# default; once it exists, session_state carries its value forward here
# on every subsequent "Run water balance" click.
mineral_coeff_override = st.session_state.get("mineral_coeff_override", DEFAULT_MINERALISATION_COEFFICIENT)

crop_path = GENERIC_CROP_PATH if GENERIC_CROP_PATH.exists() else None
if crop_path is None:
    st.error(f"Generic crop cover template not found at {GENERIC_CROP_PATH}")

SIM_FLOOR = date(1995, 1, 1)  # earliest allowed simulation start


def default_season_dates(today: date):
    """
    Default fallow start / plant dates: 1 Nov -> 1 Apr, anchored to
    whichever cycle is currently in progress or most recently completed
    relative to today. Maturity's default is handled separately (see
    _add_months / the maturity_date widget below) since it needs to track
    plant_date live rather than being fixed to a calendar date.
    """
    if today.month >= 11:
        fallow_yr = today.year
    else:
        fallow_yr = today.year - 1
    fallow_start = date(fallow_yr, 11, 1)
    plant_date = date(fallow_yr + 1, 4, 1)
    return fallow_start, plant_date


def _add_months(d: date, months: int) -> date:
    """Add a whole number of calendar months to a date, clamping the day
    if the target month is shorter (e.g. 31 Jan + 1 month -> 28/29 Feb)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    days_in_month = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(d.day, days_in_month[month - 1])
    return date(year, month, day)


_default_fallow, _default_plant = default_season_dates(today)

d1, d2, d3 = st.columns(3)
with d1:
    fallow_start = st.date_input("Fallow start", value=_default_fallow,
                                  min_value=SIM_FLOOR, max_value=yesterday,
                                  format="DD/MM/YYYY", key="fallow_start")
with d2:
    plant_date = st.date_input("Plant date", value=_default_plant,
                                min_value=fallow_start, max_value=today + timedelta(days=180),
                                format="DD/MM/YYYY", key="plant_date")
with d3:
    # Maturity's suggested default tracks plant_date (MATURITY_MONTHS_AFTER_PLANT
    # months after) until the user manually sets their own maturity_date —
    # once customised, editing plant_date leaves it alone. Managed via
    # session_state directly rather than a plain value= argument, since
    # Streamlit resets a keyed widget to its value= default when min_value
    # changes in a way that would otherwise invalidate the stored value —
    # which happens here every time plant_date moves, even when the current
    # maturity_date is still perfectly valid.
    auto_maturity = _add_months(plant_date, MATURITY_MONTHS_AFTER_PLANT)
    prev_plant = st.session_state.get("_prev_plant_date")
    if "maturity_date" not in st.session_state:
        st.session_state["maturity_date"] = auto_maturity
    elif prev_plant is not None and prev_plant != plant_date:
        prev_auto_maturity = _add_months(prev_plant, MATURITY_MONTHS_AFTER_PLANT)
        if st.session_state["maturity_date"] == prev_auto_maturity:
            st.session_state["maturity_date"] = auto_maturity   # was still on the auto default — follow plant_date
        # else: user had set their own maturity_date — leave it untouched
    if st.session_state["maturity_date"] < plant_date:
        st.session_state["maturity_date"] = auto_maturity   # safety net if plant_date moved past a custom value
    st.session_state["_prev_plant_date"] = plant_date

    maturity_date = st.date_input("Maturity date", min_value=plant_date, format="DD/MM/YYYY", key="maturity_date")




# ── Run ──────────────────────────────────────────────────────────────────────
missing = []
if not station_info:
    missing.append("a weather station — search and select one above")
if not soil_path:
    missing.append("a soil type")
if not crop_path:
    missing.append("a crop cover template")

input_signature = (
    station_info["id"] if station_info else None,
    str(soil_path) if soil_path else None,
    str(crop_path) if crop_path else None,
    fallow_start, plant_date, maturity_date, init_pct, mineral_coeff_override,
)
auto_run = (not missing) and (st.session_state.get("last_run_signature") != input_signature)

run_clicked = st.button("Run water balance", type="primary", disabled=bool(missing))
if missing:
    st.caption("⚠️ Still need: " + "; ".join(missing) + ".")
elif auto_run and not run_clicked:
    st.caption("🔄 Inputs changed — updating automatically. The button above still works any time you want to force a refresh.")

if (run_clicked or auto_run) and not missing:
    st.session_state["last_run_signature"] = input_signature
    st.session_state.pop("avg_yield", None)   # let the yield suggestion re-derive from current TUE/T50 on this fresh run
    try:
        profile = load_profile(soil_path)
        profile.n_mineralisation_coefficient = mineral_coeff_override
    except Exception as e:
        st.error(f"Could not load soil: {e}")
        st.stop()

    try:
        cover_schedule = read_cover_excel(crop_path)
    except Exception as e:
        st.error(f"Could not load crop cover template: {e}")
        st.stop()

    sid = station_info["id"]
    sim_end = min(yesterday, maturity_date)   # always run to the latest available data, or maturity if sooner
    status = st.empty()

    _t = st.session_state.get("prefetch_thread")
    if _t is not None and st.session_state.get("prefetch_sid") == sid and _t.is_alive():
        status.info("Finishing background climate download...")
        _t.join()

    status.info("Loading climate data… (first load may take 30–60 seconds)")
    diag = {"station_id": sid, "station_name": station_info["name"]}
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

    expected_days = pd.date_range(fallow_start, sim_end, freq="D")
    missing_days = expected_days.difference(met.index)
    diag["climate_rows"] = len(met)
    diag["climate_expected_rows"] = len(expected_days)
    diag["climate_missing_days"] = len(missing_days)

    _DEFAULT_OC, _DEFAULT_CN, _DEFAULT_COEFF = 1.2, 12.0, 0.0003
    diag["soil_chem_is_default"] = (
        abs(profile.organic_carbon_pct - _DEFAULT_OC) < 1e-9 and
        abs(profile.carbon_nitrogen_ratio - _DEFAULT_CN) < 1e-9 and
        abs(profile.n_mineralisation_coefficient - _DEFAULT_COEFF) < 1e-9
    )
    diag["organic_carbon_pct"] = profile.organic_carbon_pct
    diag["carbon_nitrogen_ratio"] = profile.carbon_nitrogen_ratio
    diag["n_mineralisation_coefficient"] = profile.n_mineralisation_coefficient

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
    except Exception as e:
        plume = None
        hist_pct = None
        replays = []
        diag["replay_error"] = str(e)
    diag["n_replay_years"] = len(replays)

    try:
        plant_offset = (plant_date - fallow_start).days
        pasw_at_plant_hist = [float(rr["df"]["pasw"].iloc[plant_offset]) for rr in replays
                               if len(rr["df"]) > plant_offset]
    except Exception:
        pasw_at_plant_hist = []

    try:
        n_gain = n_mineralisation_gain(df, profile, met)
        n_plume = n_plume_from_replays(replays, profile, fallow_start)
        t_pct = transpiration_percentiles(replays)
        transp_plume = transp_plume_from_replays(replays, fallow_start)
        in_crop_n_est = in_crop_n_median(replays, profile)
        fallow_n_hist = fallow_n_historical_values(replays, profile)
    except Exception as e:
        n_gain = None
        n_plume = None
        t_pct = None
        transp_plume = None
        in_crop_n_est = None
        fallow_n_hist = []
        diag["nitrogen_error"] = str(e)
    status.empty()

    st.session_state["result"] = {
        "df": df, "profile": profile, "stn_name": station_info["name"],
        "fallow_start": fallow_start, "plant_date": plant_date,
        "maturity_date": maturity_date, "sim_end": sim_end,
        "plume": plume, "hist_pct": hist_pct, "n_gain": n_gain, "n_plume": n_plume,
        "t_pct": t_pct, "in_crop_n_est": in_crop_n_est, "fallow_n_hist": fallow_n_hist, "transp_plume": transp_plume,
        "diag": diag, "pasw_at_plant_hist": pasw_at_plant_hist,
    }

# ── Output ───────────────────────────────────────────────────────────────────
if st.session_state.get("result"):
    r = st.session_state["result"]
    df, profile = r["df"], r["profile"]
    pawc = profile.pawc_total

    final_pasw = float(df["pasw"].iloc[-1])
    at_plant   = df[df.index.date == r["plant_date"]]
    pasw_at_plant = float(at_plant["pasw"].iloc[0]) if len(at_plant) else None
    pasw_at_plant_pct_pawc = (pasw_at_plant / pawc * 100.0) if (pasw_at_plant is not None and pawc > 0) else None
    pasw_at_plant_pctile = (percentile_rank(pasw_at_plant, r.get("pasw_at_plant_hist"))
                             if (pasw_at_plant is not None and r.get("pasw_at_plant_hist")) else None)

    fallow_df_hdr = df[df["phase"] == "fallow"]
    fallow_efficiency = None
    if not fallow_df_hdr.empty:
        fallow_rain_total = float(fallow_df_hdr["rain"].sum())
        fallow_dsw = float(fallow_df_hdr["pasw"].iloc[-1] - fallow_df_hdr["pasw"].iloc[0])
        if fallow_rain_total > 0:
            fallow_efficiency = fallow_dsw / fallow_rain_total * 100.0

    pasw_at_plant_html = ""
    if pasw_at_plant is not None:
        pctile_str = f" {pasw_at_plant_pctile:.0f}%ile" if pasw_at_plant_pctile is not None else ""
        pawc_str = f" ({pasw_at_plant_pct_pawc:.0f}% of PAWC)" if pasw_at_plant_pct_pawc is not None else ""
        pasw_at_plant_html = (
            f"<span><span style='color:#888'>PASW at planting ({r['plant_date'].strftime('%d %b %Y')})&nbsp;</span>"
            f"<b style='color:#1a3a5c'>{pasw_at_plant:.0f} mm</b>"
            f"<span style='color:#2979c4;'>{pawc_str}{pctile_str}</span></span>"
        )
    fallow_eff_html = (
        f"<span><span style='color:#888'>Fallow efficiency&nbsp;</span><b style='color:#1a3a5c'>{fallow_efficiency:.0f}%</b></span>"
        if fallow_efficiency is not None else ""
    )

    st.markdown(f"""
<div style="background:#f0f6ff; border-radius:10px; padding:18px 22px 14px 22px; margin-bottom:4px;">
  <div style="font-size:1.45rem; font-weight:700; color:#1a3a5c;">Soil water — fallow to crop</div>
  <div style="font-size:0.95rem; color:#444; margin-bottom:10px;">
    <b>{r['stn_name']}</b> &nbsp;·&nbsp; {profile.name} &nbsp;·&nbsp;
    {r['fallow_start'].strftime('%d %b %Y')} → {r['sim_end'].strftime('%d %b %Y')}
  </div>
  <div style="display:flex; gap:26px; flex-wrap:wrap;">
    <span><span style="color:#888">Current PASW&nbsp;</span>
      <b style="color:#1a3a5c; font-size:1.25rem;">{final_pasw:.0f} mm</b></span>
    {pasw_at_plant_html}
    {fallow_eff_html}
  </div>
</div>
""", unsafe_allow_html=True)
    if r["maturity_date"] < yesterday:
        st.caption(f"ℹ️ Maturity ({r['maturity_date'].strftime('%d %b %Y')}) has already passed — "
                   f"simulation stops there, no post-harvest run.")

    fig = make_pasw_chart(df, profile, r["plant_date"], r["maturity_date"], r["stn_name"],
                           r["fallow_start"], plume=r.get("plume"))
    fig_interactive = make_pasw_chart_interactive(df, profile, r["plant_date"], r["maturity_date"],
                                                   r["fallow_start"], r["sim_end"], plume=r.get("plume"))
    st.plotly_chart(fig_interactive, width="stretch")
    if r.get("plume") is None:
        st.caption("ℹ️ Not enough historical seasons with full climate coverage since 1995 to build a 20–80%ile band for this site/date combination.")

    n_gain = r.get("n_gain")
    if n_gain is not None and not n_gain.empty:
        fallow_n_rows_hdr = n_gain[n_gain["phase"] == "fallow"]
        fallow_n_actual = float(fallow_n_rows_hdr["cum_n_kgha"].iloc[-1]) if not fallow_n_rows_hdr.empty else 0.0
        n_pctile_hdr = percentile_rank(fallow_n_actual, r.get("fallow_n_hist")) if r.get("fallow_n_hist") else None
        n_cap_date = min(r["plant_date"], r["sim_end"])

        st.markdown(f"""
<div style="background:#f0f6ff; border-radius:10px; padding:18px 22px 14px 22px; margin-bottom:4px;">
  <div style="font-size:1.45rem; font-weight:700; color:#1a3a5c;">Nitrate mineralisation</div>
  <div style="font-size:0.95rem; color:#444; margin-bottom:10px;">
    <b>{r['stn_name']}</b> &nbsp;·&nbsp; {profile.name} &nbsp;·&nbsp;
    {r['fallow_start'].strftime('%d %b %Y')} → {r['sim_end'].strftime('%d %b %Y')}
  </div>
  <div style="display:flex; gap:26px; flex-wrap:wrap;">
    <span><span style="color:#888">NO3 gain over fallow to {n_cap_date.strftime('%d %b %Y')}&nbsp;</span>
      <b style="color:#1a3a5c; font-size:1.25rem;">{fallow_n_actual:.0f} kgN/ha</b>
      {f"<span style='color:#2979c4;'> ({n_pctile_hdr:.0f}%ile)</span>" if n_pctile_hdr is not None else ""}</span>
  </div>
</div>
""", unsafe_allow_html=True)
        fig_n = make_n_mineralisation_chart(n_gain, r["plant_date"], r["maturity_date"],
                                             r["fallow_start"], n_plume=r.get("n_plume"))
        st.pyplot(fig_n, width="stretch")
        plt.close(fig_n)
        if r.get("n_plume") is None:
            st.caption("ℹ️ Not enough historical seasons with full climate coverage since 1995 to build a 20–80%ile band here.")

        st.caption(
            "ℹ️ Gross NO3 mineralisation only \u2014 crop N-uptake not considered. Read the graph as N "
            "mineralised from the soil in the fallow and crop period, considering organic carbon, "
            "surface moisture and temperature. Mineralisation rates have not been validated and are "
            "indicative only. Relativity between seasons should be robust. If the estimated line is "
            "above the median, it indicates a better than average NO3 gain."
        )

        t_pct = r.get("t_pct")
        in_crop_n_est = r.get("in_crop_n_est")
        fallow_rows = n_gain[n_gain["phase"] == "fallow"]
        fallow_n_actual = float(fallow_rows["cum_n_kgha"].iloc[-1]) if not fallow_rows.empty else 0.0

        if t_pct is not None and in_crop_n_est is not None:
            # TUE/NUE widgets live in Diagnostics -> Tuning constants (further
            # down the page) — read via session_state here so both the yield
            # default suggestion and the budget below always have a value,
            # live-updating on the same rerun as soon as those widgets are
            # touched (no "Run" click needed for these two, unlike the
            # mineralisation coefficient, which does need a fresh Run).
            tue_live = st.session_state.get("tue_live", TUE_KG_PER_MM)
            nue_live = st.session_state.get("nue_live", NUE_PCT)

            st.markdown("**Yield & nitrogen calculator**")
            st.caption("A provisional yield estimate provided to demonstrate calculator function. Update with your estimate.")
            yc1, yc2, yc3, yc4 = st.columns(4)
            with yc1:
                default_yield = round(tue_live * t_pct[50] / 1000.0, 1)
                avg_yield = st.number_input("Average grain yield (t/ha)", min_value=0.0, max_value=20.0,
                                             value=default_yield, step=0.1, format="%.1f", key="avg_yield")
            with yc2:
                protein_pct = st.number_input("Protein target (%)", min_value=5.0, max_value=20.0,
                                               value=DEFAULT_PROTEIN_PCT, step=0.5, format="%.1f", key="protein_pct")
            with yc3:
                start_n = st.number_input("Soil test or start N (kg N/ha)", min_value=0.0, max_value=300.0,
                                           value=DEFAULT_START_N, step=5.0, format="%.0f", key="start_n")
            with yc4:
                fert_n = st.number_input("Fertiliser applied (kg N/ha)", min_value=0.0, max_value=400.0,
                                          value=DEFAULT_FERT_N, step=5.0, format="%.0f", key="fert_n")

            budget = yield_n_budget(avg_yield, protein_pct, start_n, fert_n,
                                     fallow_n_actual, in_crop_n_est,
                                     t_pct[20], t_pct[50], t_pct[80], nue_pct=nue_live)

            st.markdown(f"""
| | 20%ile | 50%ile | 80%ile |
|---|---:|---:|---:|
| Estimated water-limited yield (t/ha) | {budget[20]['yield_t_ha']:.1f} | {budget[50]['yield_t_ha']:.1f} | {budget[80]['yield_t_ha']:.1f} |
| Nitrogen required (kg N/ha) | {budget[20]['n_required']:.0f} | {budget[50]['n_required']:.0f} | {budget[80]['n_required']:.0f} |
| Nitrogen supply (kg N/ha) | {budget[20]['n_supply']:.0f} | {budget[50]['n_supply']:.0f} | {budget[80]['n_supply']:.0f} |
| **Nitrogen deficit/surplus (kg N/ha)** | {budget[20]['n_balance']:+.0f} | {budget[50]['n_balance']:+.0f} | {budget[80]['n_balance']:+.0f} |
""")
            st.caption(
                f"\u2139\ufe0f Yield 20%ile and 80%iles values based on the 50%ile value and scaled using "
                f"historical transpiration values at maturity (T20={t_pct[20]:.0f} mm, T50={t_pct[50]:.0f} mm, "
                f"T80={t_pct[80]:.0f} mm)."
            )
            st.caption(
                f"Nitrogen supply based on: current season fallow mineralisation ({fallow_n_actual:.0f} kg N/ha) "
                f"+ soil test or start N + median in-crop mineralisation ({in_crop_n_est:.0f} kg N/ha)"
            )
        else:
            st.caption("ℹ️ Not enough historical seasons with full climate coverage since 1995 to run the yield & nitrogen calculator here.")

    with st.expander("📊 Water balance details"):
        fallow_df = df[df["phase"] == "fallow"]
        crop_df   = df[df["phase"] == "crop"]
        hist_pct  = r.get("hist_pct")

        n_gain_r = r.get("n_gain")
        fallow_n_extra = None
        if n_gain_r is not None and not n_gain_r.empty:
            fallow_n_rows = n_gain_r[n_gain_r["phase"] == "fallow"]
            if not fallow_n_rows.empty:
                fallow_n_val = float(fallow_n_rows["cum_n_kgha"].iloc[-1])
                fallow_n_hist = r.get("fallow_n_hist") or []
                fallow_n_pct = percentile_rank(fallow_n_val, fallow_n_hist) if fallow_n_hist else None
                fallow_n_extra = (
                    "Gain in nitrate (kg/ha)", fallow_n_val,
                    f"{fallow_n_pct:.0f}th" if fallow_n_pct is not None else None,
                )

        wc1, wc2 = st.columns(2)
        with wc1:
            st.markdown(f"**Fallow**  {r['fallow_start'].strftime('%d %b %Y')} → {r['plant_date'].strftime('%d %b %Y')}")
            tbl = water_balance_table_md(fallow_df, hist_pct["fallow"] if hist_pct else None, extra_row=fallow_n_extra)
            st.markdown(tbl if tbl else "_No fallow days in this run._")
        with wc2:
            st.markdown(f"**In-crop**  {r['plant_date'].strftime('%d %b %Y')} → {r['sim_end'].strftime('%d %b %Y')}")
            tbl2 = water_balance_table_md(crop_df, hist_pct["crop"] if hist_pct else None)
            st.markdown(tbl2 if tbl2 else "_No in-crop days in this run yet._")

        st.caption(f"PAWC: {pawc:.0f} mm" +
                   (f"  |  Historical %ile ranks each total against {r['plume']['n_years']} replayed seasons since 1995"
                    if r.get("plume") else ""))

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
            "Simulated to": r["sim_end"].strftime("%d %b %Y"),
            "Starting soil water": f"{init_pct}% of PAWC",
            "Residue cover (fixed)": f"{RESIDUE_COVER * 100:.0f}%",
            "Crop factor (fixed)": f"{CROP_FACTOR:.2f}",
            "PAWC": f"{pawc:.0f} mm",
        }
        n_gain_report = r.get("n_gain")
        fallow_n_actual_val = None
        if n_gain_report is not None and not n_gain_report.empty:
            fallow_n_rows_report = n_gain_report[n_gain_report["phase"] == "fallow"]
            if not fallow_n_rows_report.empty:
                fallow_n_actual_val = float(fallow_n_rows_report["cum_n_kgha"].iloc[-1])

        t_pct_report = r.get("t_pct")
        in_crop_n_est_report = r.get("in_crop_n_est")
        budget_report = None
        if t_pct_report is not None and in_crop_n_est_report is not None and fallow_n_actual_val is not None:
            tue_report = st.session_state.get("tue_live", TUE_KG_PER_MM)
            nue_report = st.session_state.get("nue_live", NUE_PCT)
            avg_yield_report = st.session_state.get("avg_yield", round(tue_report * t_pct_report[50] / 1000.0, 1))
            protein_report = st.session_state.get("protein_pct", DEFAULT_PROTEIN_PCT)
            start_n_report = st.session_state.get("start_n", DEFAULT_START_N)
            fert_n_report = st.session_state.get("fert_n", DEFAULT_FERT_N)
            budget_report = yield_n_budget(avg_yield_report, protein_report, start_n_report, fert_n_report,
                                            fallow_n_actual_val, in_crop_n_est_report,
                                            t_pct_report[20], t_pct_report[50], t_pct_report[80],
                                            nue_pct=nue_report)

        n_chart_png = None
        if n_gain_report is not None and not n_gain_report.empty:
            fig_n_report = make_n_mineralisation_chart(n_gain_report, r["plant_date"], r["maturity_date"],
                                                        r["fallow_start"], n_plume=r.get("n_plume"))
            n_chart_buf = io.BytesIO()
            fig_n_report.savefig(n_chart_buf, format="png", dpi=150, bbox_inches="tight")
            n_chart_png = n_chart_buf.getvalue()
            plt.close(fig_n_report)

        report_bytes = build_report_docx(report_inputs, df, r.get("hist_pct"), r.get("plume"), chart_png,
                                          icon_path=REPORT_ICON_PATH if REPORT_ICON_PATH.exists() else None,
                                          fallow_n_actual=fallow_n_actual_val, fallow_n_hist=r.get("fallow_n_hist"),
                                          n_chart_png=n_chart_png, budget=budget_report)

        st.download_button(
            "📄 Download report (Word)",
            data=report_bytes,
            file_name=f"Howwet+_Report_{r['stn_name'].replace(' ', '_')}_{r['sim_end']}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as e:
        st.warning(f"Couldn't build the report: {e}")
    finally:
        plt.close(fig)

    diag = r.get("diag") or {}
    with st.expander("🔧 Diagnostics"):
        st.caption(f"App version: {APP_VERSION}")

        with st.expander("\u2699\ufe0f Tuning constants"):
            st.number_input(
                "Mineralisation coefficient", min_value=0.0, max_value=0.01,
                value=st.session_state.get("mineral_coeff_override", DEFAULT_MINERALISATION_COEFFICIENT),
                step=0.00005, format="%.5f", key="mineral_coeff_override")
            st.caption("Overrides the value parsed from the selected soil file. Affects the core "
                       "simulation — click \"Run water balance\" again after changing this for it to take effect.")
            st.caption(f"Soil organic carbon: {diag.get('organic_carbon_pct', '\u2014')}%  |  "
                       f"Soil C:N ratio: {diag.get('carbon_nitrogen_ratio', '\u2014')}  "
                       f"(both read from the selected soil file, not editable)")
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                st.number_input("Transpiration use efficiency (TUE, kg grain / mm)",
                                 min_value=1.0, max_value=50.0,
                                 value=st.session_state.get("tue_live", TUE_KG_PER_MM),
                                 step=1.0, format="%.0f", key="tue_live")
            with tcol2:
                st.number_input("Nitrogen use efficiency (NUE, %)",
                                 min_value=10.0, max_value=100.0,
                                 value=st.session_state.get("nue_live", NUE_PCT),
                                 step=5.0, format="%.0f", key="nue_live")
            st.caption("TUE and NUE recompute live in the calculator above — no need to re-run.")

        if diag.get("climate_missing_days", 0):
            st.warning(f"⚠️ {diag['climate_missing_days']} day(s) of climate data are missing within the simulated "
                       f"period — SILO gap-fills most of these, but very sparse stations can still leave holes.")
        if diag.get("soil_chem_is_default"):
            st.warning("⚠️ This soil's nitrogen chemistry (organic carbon, C:N ratio, mineralisation coefficient) "
                       "exactly matches the generic fallback values — likely means those tags are missing from "
                       "this soil's XML file rather than being genuine site-specific values.")
        if diag.get("replay_error"):
            st.error(f"Historical replay (soil water plume) failed internally: {diag['replay_error']}")
        if diag.get("nitrogen_error"):
            st.error(f"Nitrogen/yield calculations failed internally: {diag['nitrogen_error']}")

        transp_plume_r = r.get("transp_plume")
        st.markdown("**Transpiration** *(sanity-check for the yield calculator's T20/T50/T80)*")
        fig_transp = make_transp_chart(df, r["plant_date"], r["maturity_date"], r["fallow_start"],
                                        transp_plume=transp_plume_r)
        st.pyplot(fig_transp, width="stretch")
        plt.close(fig_transp)
        if transp_plume_r is None:
            st.caption("ℹ️ Not enough historical seasons with full climate coverage since 1995 to build a 20–80%ile band here.")

        st.markdown("**Soil evaporation / transpiration split**")
        fig_et = make_et_chart(df, r["fallow_start"], r["maturity_date"])
        st.pyplot(fig_et, width="stretch")
        plt.close(fig_et)

        if n_gain is not None and not n_gain.empty:
            st.markdown("**Temperature / surface moisture**")
            fig_tm = make_temp_moisture_chart(n_gain, r["plant_date"], r["fallow_start"], r["maturity_date"])
            st.pyplot(fig_tm, width="stretch")
            plt.close(fig_tm)

            st.markdown("**N mineralisation factors** *(what's actually driving the model, day to day)*")
            fig_nf = make_n_factors_chart(n_gain, r["fallow_start"], r["maturity_date"])
            st.pyplot(fig_nf, width="stretch")
            plt.close(fig_nf)
            st.caption(
                "ℹ️ The limiting factor (green) is whichever of moisture or temperature is smaller on a given "
                "day — that's what actually multiplies the potential mineralisation rate. If one factor sits "
                "near 1.0 for long stretches, it isn't constraining mineralisation during that period and the "
                "other factor is doing all the work; if both hover in a narrow band, that's a genuine reason "
                "day-to-day mineralisation variability would look small, not necessarily a modelling issue."
            )
