"""
core/nitrogen.py

Daily soil nitrogen mineralisation during fallow, ported from DHM
Environmental Software Engineering's HowWetN engine
(A4_HowWetN_Engine.cs, CalculateN method).

Ported with permission — user has confirmed DHM client/licensee status
authorising this port into RiskAware. Original copyright notice:

    DHM Environmental Software Engineering Pty. Ltd. Copyright 2011.
    Per the original file: "Where permission has been granted to modify
    this file, changes must be clearly identified... and a description of
    the changes (including who has made the changes) must be included."

Change description: ported from C# (CliMate HowWetN engine) to Python by
Claude (Anthropic) for David Freebairn, June 2026, as part of the RiskAware
Season Tracker prototype. Logic translated as directly as possible from the
original CalculateN() method; variable names kept close to the original
for traceability. Operates on layer-1 soil water only, exactly as the
original (comment in source: "All calculated in first layer").

Model summary
-------------
Each day, mineralisable N is released from a potential pool (derived from
soil organic carbon and C:N ratio) at a rate limited by the SLOWER of two
0-1 factors:
  - moistfactor : actual volumetric soil water %, air-dry-offset against
                  field capacity (confirmed against the source workbook's
                  "Algorithms" tab, matching the original Howwet C++
                  Calc_minz — see AIRDRY_OFFSET_FACTOR below)
  - tempfactor  : linear function of mean daily air temperature

  Org_n     = OrganicCarbon / CarbonNitrogenRatio            (if C:N != 0)
  potm      = (Org_n / 100) * ACTIVE_MIN_DEPTH_MM * POTM_UNIT_FACTOR
  moistfactor = clip( (soilwater_pct - airdry_pct*AIRDRY_OFFSET_FACTOR)
                       / (field_capacity_pct - airdry_pct*AIRDRY_OFFSET_FACTOR), 0, 1 )
  tempfactor  = clip( TEMP_FACTOR_SLOPE * avgtemp_C - TEMP_FACTOR_INTERCEPT, 0, 1 )
  multiplier  = min(moistfactor, tempfactor)
  daily_N_release = multiplier * NitrogenMineralisationCoefficient * potm

TotalN accumulates daily_N_release over the period (mg N / unit area per
the original units — see note in calc_fallow_nitrogen_gain below on units,
which were not fully disambiguated in the source and should be checked
against a known CliMate/HowLeaky result before relying on absolute values).
"""

import numpy as np

# ── Tunable constants ─────────────────────────────────────────────────────
# Potential mineralisable N scaling (potm): assumes a 200 mm depth of
# active mineralisation and bulk density 1 g/cm^3 — matches the source
# workbook's "Algorithms" tab exactly. Change ACTIVE_MIN_DEPTH_MM if a
# different active-mineralisation depth should be assumed.
ACTIVE_MIN_DEPTH_MM     = 200.0
POTM_UNIT_FACTOR        = 10.0 * 1000.0   # combined bulk-density/unit conversion

# Moisture factor (air-dry-offset version, matches Howwet C++ Calc_minz):
#   Moisture_factor = max(0, min(1, (Soilwater_% - airdry%*AIRDRY_OFFSET)
#                                     / (FC% - airdry%*AIRDRY_OFFSET)))
AIRDRY_OFFSET_FACTOR    = 0.25

# Temperature factor (linear APSIM approximation, chosen over the original
# Howwet exponential — see "Algorithms" tab comments):
#   Temp_factor = max(0, min(1, TEMP_FACTOR_SLOPE * Temp_degC - TEMP_FACTOR_INTERCEPT))
TEMP_FACTOR_SLOPE       = 0.035
TEMP_FACTOR_INTERCEPT   = 0.1


def daily_n_mineralisation(
    sw_layer1_mm: float,
    layer1_thickness_mm: float,
    airdry_pct: float,
    wilting_point_pct: float,
    field_capacity_pct: float,
    avgtemp_degc: float,
    organic_carbon_pct: float,
    carbon_nitrogen_ratio: float,
    nitrogen_mineralisation_coefficient: float,
) -> float:
    """
    One day's nitrogen mineralisation release, layer 1 only.

    sw_layer1_mm         : layer-1 soil water (mm), ABSOLUTE (not relative
                            to wilting point) — i.e. sw[0] straight from
                            the water balance output, not sw[0] - ll_mm.
    layer1_thickness_mm  : thickness of layer 1 (mm) — used to convert
                            sw_layer1_mm to a %-of-layer-depth (%vol) figure.
    airdry_pct, wilting_point_pct, field_capacity_pct : layer-1 soil
                            parameters as %vol (0-100), straight from the
                            .soil XML (InSituAirDryMoist, WiltingPoint,
                            FieldCapacity for layer 1). wilting_point_pct
                            is accepted for signature stability but not
                            used by the current moisture-factor formula
                            (confirmed against the source workbook's
                            Algorithms tab — see moisture factor below).
    avgtemp_degc          : mean daily air temperature (tmax+tmin)/2.
    organic_carbon_pct, carbon_nitrogen_ratio, nitrogen_mineralisation_coefficient :
                            soil chemistry parameters — OrganicCarbon,
                            CarbonNitrogenRatio, NitrateMineralisationCoefficient
                            from the .soil XML (not currently parsed by
                            core/soil_xml.py — see note below).

    Returns daily N release for layer 1 (same units as the original engine;
    not independently re-derived here — see module docstring caveat).
    """
    # NOTE: soilwater_percent here is the ACTUAL volumetric soil water %
    # (0 to ~saturation), not "above wilting point" — the moisture factor
    # formula below needs the real %vol reading. sw_layer1_mm must be
    # ABSOLUTE layer-1 soil water (mm), not a wilting-point-relative value.
    soilwater_percent = (sw_layer1_mm / layer1_thickness_mm) * 100.0 if layer1_thickness_mm > 0 else 0.0

    org_n = (
        organic_carbon_pct / carbon_nitrogen_ratio
        if abs(carbon_nitrogen_ratio) > 1e-6
        else 0.0
    )
    potm = (org_n / 100.0) * ACTIVE_MIN_DEPTH_MM * POTM_UNIT_FACTOR

    # Moisture factor per the "Algorithms" tab of the source workbook,
    # matching the original Howwet C++ (Calc_minz): an air-dry-offset
    # version, not a plain wilting-point-relative fraction.
    denom = field_capacity_pct - airdry_pct * AIRDRY_OFFSET_FACTOR
    moistfactor = 0.0 if denom == 0 else max(0.0, min(1.0, (soilwater_percent - airdry_pct * AIRDRY_OFFSET_FACTOR) / denom))

    tempfactor = max(0.0, min(1.0, TEMP_FACTOR_SLOPE * avgtemp_degc - TEMP_FACTOR_INTERCEPT))

    multiplier = min(moistfactor, tempfactor)
    daily_release = multiplier * nitrogen_mineralisation_coefficient * potm
    return daily_release


def run_n_mineralisation_series(met_df, sw_layer1_series, layer1_thickness_mm,
                                airdry_pct, wilting_point_pct, field_capacity_pct,
                                organic_carbon_pct, carbon_nitrogen_ratio,
                                nitrogen_mineralisation_coefficient):
    """
    Run the daily mineralisation model over a date-indexed climate frame
    and a matching layer-1 absolute soil-water series, returning a
    cumulative N series (same index).

    met_df            : DataFrame with 'tmean' column, DatetimeIndex
    sw_layer1_series  : array-like, layer-1 soil water, ABSOLUTE (mm) —
                        i.e. sw[0] straight from the water balance output
                        for each day, same length/order as met_df.
    """
    tmean = met_df["tmean"].fillna(met_df["tmean"].mean()).values
    sw1 = np.asarray(sw_layer1_series, dtype=float)
    n = len(tmean)
    cumulative = np.zeros(n)
    total = 0.0
    for i in range(n):
        daily = daily_n_mineralisation(
            sw_layer1_mm=sw1[i],
            layer1_thickness_mm=layer1_thickness_mm,
            airdry_pct=airdry_pct,
            wilting_point_pct=wilting_point_pct,
            field_capacity_pct=field_capacity_pct,
            avgtemp_degc=tmean[i],
            organic_carbon_pct=organic_carbon_pct,
            carbon_nitrogen_ratio=carbon_nitrogen_ratio,
            nitrogen_mineralisation_coefficient=nitrogen_mineralisation_coefficient,
        )
        total += daily
        cumulative[i] = total
    import pandas as pd
    return pd.Series(cumulative, index=met_df.index, name="cumulative_n")


# ---------------------------------------------------------------------------
# Integration wrapper for Howwet+ (new code — not part of the licensed
# HowWetN port above, just plumbing to connect it to run_fallow_to_crop()'s
# output).
# ---------------------------------------------------------------------------

def daily_n_factors(soilwater_percent, airdry_pct, field_capacity_pct, avgtemp_degc):
    """
    Diagnostic-only: the raw 0-1 moisture/temperature factors and the
    resulting limiting factor, mirroring the internal calculation in
    daily_n_mineralisation() via the same named constants (AIRDRY_OFFSET_FACTOR,
    TEMP_FACTOR_SLOPE, TEMP_FACTOR_INTERCEPT) — so you can inspect which
    factor is actually constraining mineralisation day to day. Not part of
    the licensed HowWetN port itself; kept separate so that function's own
    logic never needs touching for this. Vectorised — accepts numpy arrays
    or scalars.
    """
    denom = field_capacity_pct - airdry_pct * AIRDRY_OFFSET_FACTOR
    if denom == 0:
        moistfactor = np.zeros_like(np.asarray(soilwater_percent, dtype=float))
    else:
        moistfactor = np.clip((np.asarray(soilwater_percent, dtype=float) - airdry_pct * AIRDRY_OFFSET_FACTOR) / denom, 0.0, 1.0)
    tempfactor = np.clip(TEMP_FACTOR_SLOPE * np.asarray(avgtemp_degc, dtype=float) - TEMP_FACTOR_INTERCEPT, 0.0, 1.0)
    limiting = np.minimum(moistfactor, tempfactor)
    return moistfactor, tempfactor, limiting


def n_mineralisation_gain(df, profile, met_df):
    """
    Daily surface (layer-1) moisture, mean temperature (raw and a 30-day
    rolling average), and cumulative N mineralisation gain, over the FULL
    run_fallow_to_crop() result (fallow + in-crop, whatever was actually
    simulated) — not just the fallow phase.

    This is gross mineralisation only: there's no crop N-uptake term in
    the underlying model, so once a crop is established the cumulative
    total keeps climbing as if nothing were taking it up. Read it as
    "N mineralised from the soil" rather than "N available to the crop"
    once past the plant date — the plant-date marker on the chart exists
    specifically so that distinction stays visible rather than implied.

    Parameters
    ----------
    df      : output of run_fallow_to_crop() — needs 'phase' and
              'sw_layers' columns
    profile : the SoilProfile used for that run
    met_df  : the climate DataFrame used for that run — needs a 'tmean'
              column, same DatetimeIndex as df

    Returns
    -------
    DataFrame indexed by date with columns 'phase', 'surface_moisture_mm'
    (layer-1 soil water relative to wilting point — can go negative if
    it's dried below wilting point), 'tmean', 'tmean_30d' (30-day rolling
    mean), and 'cum_n_kgha'. Returns None if df is empty.

    Note: the underlying model's absolute units haven't been independently
    verified against a known CliMate/HowLeaky reference case (see this
    module's docstring) — treat magnitudes as indicative until checked.
    """
    import pandas as pd

    if df is None or df.empty:
        return None

    layer0 = profile.layers[0]
    sw1_absolute = np.array([layers[0] for layers in df["sw_layers"]])
    sw1_above_ll = sw1_absolute - layer0.ll_mm   # for display only — see below
    met_aligned = met_df.loc[df.index]

    n_series = run_n_mineralisation_series(
        met_aligned, sw1_absolute, layer0.thickness,
        airdry_pct=layer0.airdry * 100.0,
        wilting_point_pct=layer0.ll * 100.0,
        field_capacity_pct=layer0.dul * 100.0,
        organic_carbon_pct=profile.organic_carbon_pct,
        carbon_nitrogen_ratio=profile.carbon_nitrogen_ratio,
        nitrogen_mineralisation_coefficient=profile.n_mineralisation_coefficient,
    )

    tmean = met_aligned["tmean"]
    soilwater_percent = (sw1_absolute / layer0.thickness) * 100.0 if layer0.thickness > 0 else sw1_absolute * 0.0
    moist_factor, temp_factor, limiting_factor = daily_n_factors(
        soilwater_percent, layer0.airdry * 100.0, layer0.dul * 100.0, tmean.values)

    return pd.DataFrame({
        "phase": df["phase"].values,
        "surface_moisture_mm": sw1_above_ll,   # relative to WP, for intuitive charting only
        "tmean": tmean.values,
        "tmean_30d": tmean.rolling(30, min_periods=1).mean().values,
        "cum_n_kgha": n_series.values,
        "moist_factor": moist_factor,
        "temp_factor": temp_factor,
        "limiting_factor": limiting_factor,
    }, index=df.index)


def fallow_n_historical_values(replays, profile):
    """
    This season's fallow-phase N mineralisation total, one value per
    historical replayed year — the raw distribution to rank the current
    season's actual fallow N gain against (via core.cropwater.percentile_rank).

    Returns an empty list if none of the replays have usable fallow data.
    """
    vals = []
    for r in replays:
        ng_y = n_mineralisation_gain(r["df"], profile, r["met"])
        if ng_y is None:
            continue
        fallow_y = ng_y[ng_y["phase"] == "fallow"]
        if fallow_y.empty:
            continue
        vals.append(float(fallow_y["cum_n_kgha"].iloc[-1]))
    return vals


def n_plume_from_replays(replays, profile, fallow_start, pctiles=(20, 80)):
    """
    Build a day-by-day 20-80%ile (plus median) historical band for
    cumulative N mineralisation gain, from replay_historical_seasons()
    output — same approach as core.cropwater.pasw_plume_from_replays(),
    just for N instead of PASW. This is where the real value is: a single
    season's gross mineralisation number means little on its own, but
    ranked against what the same soil/site has done historically it
    becomes genuinely informative.

    Returns None if fewer than 3 usable historical years.
    """
    if len(replays) < 3:
        return None

    import pandas as pd
    traces = []
    for r in replays:
        ng = n_mineralisation_gain(r["df"], profile, r["met"])
        if ng is not None:
            traces.append(ng["cum_n_kgha"].to_numpy())

    if len(traces) < 3:
        return None

    min_len = min(len(t) for t in traces)
    stacked = np.array([t[:min_len] for t in traces])
    low, high = np.percentile(stacked, pctiles, axis=0)
    median = np.percentile(stacked, 50, axis=0)
    dates = pd.date_range(fallow_start, periods=min_len, freq="D")
    return {"dates": dates, "low": low, "high": high, "median": median, "n_years": len(traces)}
