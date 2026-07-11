"""
core/cropwater.py
==================
Continuous soil-water simulation across a fallow period and into a
following crop, built on top of core.waterbalance.daily_water_balance().

Design
------
The underlying engine (core.waterbalance) is crop-agnostic: it just wants
a green cover fraction, a total cover fraction, a root depth (mm) and a
crop factor for each day. This module supplies those daily values by
switching between two sources at the sowing date, and — new — stretches
a template cover curve to fit whatever plant/maturity dates the user
actually picks, rather than assuming a fixed crop duration.

Template stretching
--------------------
A crop cover template (read with core.cover_excel.read_cover_excel) has
some "typical" green-cover growth window baked into its day axis — e.g.
0% until day 121, ramping up, back to 0% by day 289. Real seasons rarely
match that exact window, so `stretch_cover_schedule()` finds the
template's own growth window (first day green cover rises above a small
threshold, to the last day it falls back below it after peaking) and
linearly stretches/compresses just that window to span the user's actual
plant_date -> maturity_date. The result is a new schedule indexed by
calendar-date ordinal, so it can be queried directly with an actual date
via core.cover_excel.get_cover_state() (no change needed to that function
— it already just interpolates against whatever is in `doy`).

  Before plant_date            -> fixed bare-fallow cover (no roots, no
                                   green cover, light residue)
  plant_date .. maturity_date  -> stretched template
  After maturity_date          -> fixed bare-fallow cover (post-harvest)
"""

from dataclasses import dataclass, replace
from datetime import date
import numpy as np
import pandas as pd

from core.soil import init_sw
from core.waterbalance import daily_water_balance
from core.cover_excel import CoverSchedule, get_cover_state
from core.silo import slice_climate


@dataclass
class FallowCropConfig:
    """
    Cover assumptions that apply throughout the run.

    residue_cover combines with green cover the same way in both phases —
    bare fallow (green=0) shows as pure residue cover; under a growing crop,
    total_cover = green + (1-green)*residue_cover (fractional cover model,
    matching core.cover_excel's own total-cover formula). The crop
    template's own Residue Cover % column is not used — one consistent
    residue assumption applies for the whole run instead.
    """
    residue_cover: float = 0.30   # surface residue/stubble fraction (0-1)
    crop_factor: float = 1.0      # flat PET scalar (engine default)


def _total_cover(green: float, residue_cover: float) -> float:
    return green + (1.0 - green) * residue_cover


def stretch_cover_schedule(template: CoverSchedule, plant_date, maturity_date,
                            green_threshold: float = 0.01) -> CoverSchedule:
    """
    Rescale a cover template's growth window to span plant_date -> maturity_date.

    Finds the template's own green-cover growth window — first breakpoint
    where green_cover rises above `green_threshold`, to the last breakpoint
    where it's still above threshold after peaking — and linearly maps that
    window onto the real dates. Breakpoints outside the window keep their
    relative spacing, stretched by the same factor (so a leading bare
    section in the template becomes a shorter or longer bare lead-in, not a
    discontinuity).

    Returns a new CoverSchedule whose `doy` field holds calendar ordinals
    (date.toordinal()), so get_cover_state(scaled, some_date.toordinal())
    works directly.
    """
    green = template.green_cover
    above = np.where(green > green_threshold)[0]
    if len(above) == 0:
        raise ValueError("Template has no green cover above threshold — nothing to stretch.")
    first_above, last_above = int(above[0]), int(above[-1])
    peak_idx = int(np.argmax(green))

    # Start anchor: the last zero(-ish) point *before* the ramp-up begins,
    # not the first point already above threshold — otherwise the very
    # base of the ramp gets clipped.
    start_idx = first_above - 1 if first_above > 0 else first_above

    # End anchor: the first point *at or below* threshold after the peak
    # (i.e. where the crop actually returns to bare ground), not just the
    # last point still above threshold — otherwise the tail of the decline
    # back to zero gets cut off and maturity_date lands mid-decline.
    end_idx = None
    for j in range(peak_idx, len(green)):
        if green[j] <= green_threshold:
            end_idx = j
            break
    if end_idx is None:
        end_idx = last_above  # template never returns to baseline in the data

    day_start, day_end = float(template.doy[start_idx]), float(template.doy[end_idx])
    template_span = day_end - day_start
    if template_span <= 0:
        raise ValueError("Template growth window has zero length — check the source file.")

    target_span = (maturity_date - plant_date).days
    if target_span <= 0:
        raise ValueError("Maturity date must be after plant date.")

    scale = target_span / template_span
    plant_ord = plant_date.toordinal()
    new_doy = plant_ord + (template.doy - day_start) * scale

    return replace(template, doy=new_doy, name=f"{template.name} (stretched {plant_date}\u2192{maturity_date})")


def run_fallow_to_crop(met_df: pd.DataFrame, profile, plant_date, maturity_date,
                        cover_template: CoverSchedule, config: FallowCropConfig = None,
                        sw_init_frac: float = 0.5):
    """
    Run a continuous daily water balance spanning fallow -> crop.

    Parameters
    ----------
    met_df        : DataFrame indexed by date with 'rain' and 'epan' columns
                     (e.g. from core.silo.slice_climate), spanning fallow start
                     through to the end of the period of interest.
    profile       : SoilProfile (core.soil_xml.read_soil_xml / core.soil.read_prm)
    plant_date    : date — cover switches from bare fallow to the (stretched)
                     crop template on this day.
    maturity_date : date — cover switches back to bare fallow after this day.
    cover_template: CoverSchedule from core.cover_excel.read_cover_excel(). Its
                     day axis just needs to represent a *typical* growth shape —
                     it's stretched to plant_date/maturity_date automatically.
    config        : FallowCropConfig — bare-fallow cover assumptions.
    sw_init_frac  : initial soil water as a fraction of PAWC (0-1) at the
                     start of met_df (i.e. at fallow start).

    Returns
    -------
    (df, sw0, swf) — daily results DataFrame, initial and final total SW (mm)
    """
    config = config or FallowCropConfig()
    scaled = stretch_cover_schedule(cover_template, plant_date, maturity_date)

    layers = profile.layers
    sw  = init_sw(profile, sw_init_frac)
    sw0 = float(sw.sum())

    sumes1 = sumes2 = dsr = 0.0
    records = []

    for dt, row in met_df.iterrows():
        rain = float(row.get("rain", 0) or 0)
        epan = float(row.get("epan", 0) or 0)
        if np.isnan(rain): rain = 0.0
        if np.isnan(epan): epan = 0.0

        d = pd.Timestamp(dt).date()

        if d < plant_date or d > maturity_date:
            green, root = 0.0, 0.0
            total = _total_cover(green, config.residue_cover)
            phase = "fallow" if d < plant_date else "post-harvest"
        else:
            green, _tmpl_total, root = get_cover_state(scaled, d.toordinal())
            total = _total_cover(green, config.residue_cover)
            phase = "crop"

        sw_before = float(sw.sum())
        out = daily_water_balance(
            sw=sw, layers=layers, soil=profile,
            rain=rain, epan=epan,
            green_cover=green, total_cover=total,
            root_depth_mm=root, crop_factor=config.crop_factor,
            sumes1=sumes1, sumes2=sumes2, t_since_wet=dsr,
        )
        sw     = out["sw"]
        sumes1 = out["sumes1"]
        sumes2 = out["sumes2"]
        dsr    = out["t_since_wet"]

        sw_total = float(sw.sum())
        pasw = sum(max(0.0, float(sw[i]) - layers[i].ll_mm) for i in range(len(layers)))
        # Mass-balance back-calculation of actual soil evap (robust to engine tweaks)
        actual_es = max(0.0, sw_before + rain - out["runoff"] - out["drainage"]
                         - out["transp"] - sw_total)

        records.append({
            "phase"       : phase,
            "rain"        : rain,
            "epan"        : epan,
            "green_cover" : green,
            "total_cover" : total,
            "root_depth"  : root,
            "runoff"      : out["runoff"],
            "soil_evap"   : actual_es,
            "transp"      : out["transp"],
            "drainage"    : out["drainage"],
            "et"          : actual_es + out["transp"],
            "sw_total"    : sw_total,
            "pasw"        : round(pasw, 2),
            "sw_layers"   : sw.copy().tolist(),
        })

    df  = pd.DataFrame(records, index=met_df.index)
    swf = float(df["sw_total"].iloc[-1])
    return df, sw0, swf


def replay_historical_seasons(met_full: pd.DataFrame, profile, fallow_start, plant_date, maturity_date,
                               cover_template: CoverSchedule, config: FallowCropConfig = None,
                               sw_init_frac: float = 0.5, first_year: int = 1995):
    """
    Replay the same fallow->plant->maturity season shape (same month/day,
    different calendar years) across all prior years of climate record from
    first_year onward. Each historical year is run as a *complete* season
    (fallow_start_Y -> maturity_date_Y in full), unlike the current/selected
    season which may be cut short at an assessment date.

    Shared by historical_pasw_plume() and historical_phase_percentiles() so
    the ~30 simulation runs only happen once per app "Run".

    Returns a list of {'year': Y, 'df': df_y} dicts — one per historical year
    with complete climate coverage for that replayed season.
    """
    config = config or FallowCropConfig()
    yr0 = fallow_start.year
    plant_offset    = plant_date.year - yr0
    maturity_offset = maturity_date.year - yr0

    replays = []
    for y in range(first_year, yr0):
        try:
            fs_y = date(y, fallow_start.month, fallow_start.day)
            pd_y = date(y + plant_offset, plant_date.month, plant_date.day)
            md_y = date(y + maturity_offset, maturity_date.month, maturity_date.day)
        except ValueError:
            continue  # e.g. 29 Feb in a non-leap replay year — skip it

        met_y = slice_climate(met_full, start=fs_y, end=md_y)
        if met_y.empty or met_y.index.min().date() > fs_y or met_y.index.max().date() < md_y:
            continue  # incomplete climate coverage for this replay year

        try:
            df_y, _, _ = run_fallow_to_crop(met_y, profile, pd_y, md_y, cover_template,
                                             config=config, sw_init_frac=sw_init_frac)
        except Exception:
            continue

        replays.append({"year": y, "df": df_y})

    return replays


def pasw_plume_from_replays(replays, fallow_start, pctiles=(20, 80)):
    """
    Build a day-by-day 20-80%ile PASW band from replay_historical_seasons()
    output. Returns None if fewer than 3 usable historical years.
    """
    if len(replays) < 3:
        return None
    traces = [r["df"]["pasw"].to_numpy() for r in replays]
    min_len = min(len(t) for t in traces)
    stacked = np.array([t[:min_len] for t in traces])
    low, high = np.percentile(stacked, pctiles, axis=0)
    dates = pd.date_range(fallow_start, periods=min_len, freq="D")
    return {"dates": dates, "low": low, "high": high, "n_years": len(replays)}


def historical_pasw_plume(met_full, profile, fallow_start, plant_date, maturity_date,
                           cover_template, config=None, sw_init_frac=0.5, first_year=1995,
                           pctiles=(20, 80)):
    """Convenience wrapper: replay + plume in one call (see the two functions above)."""
    replays = replay_historical_seasons(met_full, profile, fallow_start, plant_date, maturity_date,
                                         cover_template, config=config, sw_init_frac=sw_init_frac,
                                         first_year=first_year)
    return pasw_plume_from_replays(replays, fallow_start, pctiles=pctiles)


_COMPONENTS = ["rain", "runoff", "soil_evap", "transp", "drainage"]


def phase_totals(df: pd.DataFrame, phase: str):
    """Sum each water-balance component over rows matching `phase`, plus the
    change in soil water across that slice. Returns None if the phase has no
    rows in this df."""
    sub = df[df["phase"] == phase]
    if sub.empty:
        return None
    out = {c: float(sub[c].sum()) for c in _COMPONENTS}
    out["dsw"] = float(sub["sw_total"].iloc[-1] - sub["sw_total"].iloc[0])
    return out


def historical_phase_percentiles(replays, fallow_days: int = None, crop_days: int = None):
    """
    From replay_historical_seasons() output, build the historical
    distribution of fallow-phase and crop-phase component totals (rain,
    runoff, soil_evap, transp, drainage, dsw) — one value per historical
    year per component — for ranking the current season's totals against.

    fallow_days / crop_days: if given, each historical year's phase is
    truncated to this many days (from the start of that phase) before
    summing. Pass the current season's own phase day-counts here whenever
    the current run is still in progress (e.g. assessment date is before
    maturity) — otherwise a partial current-season total gets compared
    against *complete* historical season totals and looks artificially low
    just because it covers fewer days, not because conditions differ.

    Returns {'fallow': {'rain': [...], ...}, 'crop': {...}}, using only
    years that had rows for that phase (a replay year always has a fallow
    phase; it only has a crop phase if the season shape includes one).
    """
    out = {"fallow": {c: [] for c in _COMPONENTS + ["dsw"]},
           "crop":   {c: [] for c in _COMPONENTS + ["dsw"]}}
    day_limits = {"fallow": fallow_days, "crop": crop_days}
    for r in replays:
        for phase in ("fallow", "crop"):
            sub = r["df"][r["df"]["phase"] == phase]
            limit = day_limits[phase]
            if limit is not None:
                sub = sub.iloc[:limit]
            if sub.empty:
                continue
            vals = {c: float(sub[c].sum()) for c in _COMPONENTS}
            vals["dsw"] = float(sub["sw_total"].iloc[-1] - sub["sw_total"].iloc[0])
            for c, v in vals.items():
                out[phase][c].append(v)
    return out


def percentile_rank(current_value: float, historical_values):
    """Where current_value ranks among historical_values, as a percentile
    (0-100). Returns None if there's no historical distribution to rank
    against."""
    if not historical_values:
        return None
    arr = np.asarray(historical_values, dtype=float)
    return float(100.0 * np.mean(arr <= current_value))


def cover_preview(cover_template: CoverSchedule, plant_date, maturity_date,
                   residue_cover: float = 0.30, lead_in_days: int = 30):
    """
    Cover-only preview (no soil, no climate) — just the stretched green
    cover and root depth curve across plant_date -> maturity_date, with a
    short bare lead-in for context. Total cover is derived from green cover
    plus a constant residue_cover (same formula run_fallow_to_crop uses),
    not read from the template's own residue column. Useful for
    sanity-checking a template's stretch behaviour before wiring in a full
    water balance run, matching the SeasonAware "Crop cover %" gauge.

    Returns a DataFrame indexed by date with green_cover, total_cover,
    root_depth (fractions/mm).
    """
    scaled  = stretch_cover_schedule(cover_template, plant_date, maturity_date)
    lead_in = plant_date - pd.Timedelta(days=lead_in_days)
    idx     = pd.date_range(lead_in, maturity_date, freq="D")
    rows = []
    for ts in idx:
        d = ts.date()
        if d < plant_date:
            green, root = 0.0, 0.0
        else:
            green, _tmpl_total, root = get_cover_state(scaled, d.toordinal())
        total = _total_cover(green, residue_cover)
        rows.append({"date": ts, "green_cover": green, "total_cover": total, "root_depth": root})
    return pd.DataFrame(rows).set_index("date")
