"""
core/yield_n.py
================
Target-yield nitrogen budget calculator, replicating the "Dashboard final"
tab of For_Claude_N_calcs_for_Howwetplus.xlsx.

Design, matching the workbook:
  - The grower/agronomist supplies an average (50th %ile) expected yield —
    this is their own judgement call, not something the model predicts.
  - That yield is scaled to a 20/80%ile spread using the ratio of
    historical total-transpiration percentiles (T20/T50, T80/T50) —
    because yield is water-limited, and water availability is what
    actually varies season to season.
  - Nitrogen required for that yield/protein combination is computed from
    a standard grain-N budget (grain N% = protein% / 5.7, a wheat
    convention), grossed up by nitrogen use efficiency (NUE).
  - Nitrogen supply blends known and unknown: this season's ACTUAL
    fallow-phase mineralisation (already happened, real weather) plus the
    LONG-TERM MEDIAN in-crop mineralisation (hasn't happened yet, so the
    historical median is the best available estimate) plus starting
    mineral N and fertiliser applied.
"""

import numpy as np

from core.nitrogen import n_mineralisation_gain

# ── Tunable constants ─────────────────────────────────────────────────────
# Grain N requirement (kg N/ha) = yield(t/ha) * protein% * GRAIN_N_FACTOR / NUE%
# GRAIN_N_FACTOR = 1000 * (grain N% per protein%) = 1000 * (17.5/100) = 175.0
# — the wheat convention (protein = N * 5.7, i.e. N% \u2248 protein% * 0.175),
# matching the source workbook's formula exactly (not the more precise 1/5.7,
# to keep numbers identical to the workbook).
GRAIN_N_FACTOR = 175.0


def in_crop_n_median(replays, profile, pctile=50):
    """
    Historical percentile (default: median) of N mineralised specifically
    during the in-crop phase (plant_date -> maturity_date), one value per
    historical replayed year, for blending with this season's actual
    (already realised) fallow mineralisation in the N supply budget.

    Computed as each replayed year's own (N at maturity - N at plant date)
    difference, then the percentile of those per-year differences — not
    a difference of percentiles, which isn't the same thing for a
    monotonically accumulating series across correlated years.

    Returns None if fewer than 3 usable years.
    """
    diffs = []
    for r in replays:
        ng_y = n_mineralisation_gain(r["df"], profile, r["met"])
        if ng_y is None:
            continue
        crop_y = ng_y[ng_y["phase"] == "crop"]
        fallow_y = ng_y[ng_y["phase"] == "fallow"]
        if crop_y.empty or fallow_y.empty:
            continue
        diffs.append(float(crop_y["cum_n_kgha"].iloc[-1] - fallow_y["cum_n_kgha"].iloc[-1]))

    if len(diffs) < 3:
        return None
    return float(np.percentile(diffs, pctile))


def yield_n_budget(avg_yield_t_ha, protein_pct, start_n, fert_n,
                    fallow_n_actual, in_crop_n_est, t20, t50, t80, nue_pct=50.0):
    """
    Target-yield N budget across the 20/50/80%ile spread.

    Parameters
    ----------
    avg_yield_t_ha   : the 50th-%ile yield estimate — a user judgement call
    protein_pct      : grain protein target (%)
    start_n          : starting mineral N, soil test or estimate (kg N/ha)
    fert_n           : fertiliser N applied (kg N/ha)
    fallow_n_actual  : this season's actual fallow-phase N mineralisation
                       (kg N/ha) — from n_mineralisation_gain() on the
                       current run
    in_crop_n_est    : long-term median (or other pctile) in-crop N
                       mineralisation estimate (kg N/ha) — from
                       in_crop_n_median()
    t20, t50, t80    : historical transpiration percentiles (mm) at
                       maturity — from transpiration_percentiles()
    nue_pct          : nitrogen use efficiency (%) — fraction of supplied
                       N that ends up in the grain; default 50%

    Returns
    -------
    dict keyed by 20/50/80, each {'yield_t_ha', 'n_required', 'n_supply',
    'n_balance'} — n_balance positive means surplus, negative means deficit.
    """
    scalar_80 = (t80 / t50) if t50 else 1.0
    scalar_20 = (t20 / t50) if t50 else 1.0

    yields = {
        20: avg_yield_t_ha * scalar_20,
        50: avg_yield_t_ha,
        80: avg_yield_t_ha * scalar_80,
    }
    n_supply = start_n + fallow_n_actual + in_crop_n_est + fert_n

    out = {}
    for p, y in yields.items():
        # grain N requirement = yield(t/ha) * protein% * GRAIN_N_FACTOR / NUE%
        n_required = y * protein_pct * GRAIN_N_FACTOR / nue_pct
        out[p] = {
            "yield_t_ha": y,
            "n_required": n_required,
            "n_supply": n_supply,
            "n_balance": n_supply - n_required,
        }
    return out
