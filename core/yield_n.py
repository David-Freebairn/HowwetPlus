"""
core/yield_n.py
================
Target-yield nitrogen budget calculator, replicating the "Howwet+ N
calculations" workbook (N_budget.xlsx).

Design, matching the workbook:
  - The grower/agronomist supplies their own 20/50/80%ile yield estimates
    directly — these are judgement calls, not something the model
    predicts. The app suggests Y20 = 0.6 x mean and Y80 = 1.5 x mean as a
    starting point (the workbook's own rule of thumb), fully overridable.
    Earlier versions derived the 20/80%ile spread from the ratio of
    historical transpiration percentiles (T20/T50, T80/T50), but that
    band came out narrower than real yield variability — transpiration is
    capped by atmospheric demand as much as by water supply, and it can't
    see frost, heat, disease, or waterlogging, all of which widen the
    real-world spread beyond what a water-balance-only model produces.
  - Nitrogen required for that yield/protein combination is computed from
    a standard grain-N budget (grain N% = protein% / 5.7, a wheat
    convention), grossed up by nitrogen use efficiency (NUE).
  - Nitrogen supply blends known and unknown: this season's ACTUAL
    fallow-phase mineralisation (already happened, real weather) plus the
    LONG-TERM MEDIAN in-crop mineralisation (hasn't happened yet, so the
    historical median is the best available estimate) plus starting
    mineral N and fertiliser applied.
  - Optionally, given a grain price and fertiliser cost, the bottom line
    ($/ha grain return, and $/ha extra fertiliser cost to close any N
    deficit) is also budgeted per percentile.
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

# Suggested Y20/Y80 multipliers of the grower's mean/median yield estimate
# — the workbook's own rule of thumb ("Lowest in 5 years", "Highest in 5
# years"), offered as an editable starting point, not a modelled result.
Y20_SUGGESTED_MULTIPLIER = 0.6
Y80_SUGGESTED_MULTIPLIER = 1.5


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


def yield_n_budget(y20, y50, y80, protein_pct, start_n, fert_n,
                    fallow_n_actual, in_crop_n_est, nue_pct=50.0,
                    grain_price=None, fert_cost_per_kgn=None):
    """
    Target-yield N budget across the 20/50/80%ile spread.

    Parameters
    ----------
    y20, y50, y80    : the grower's own 20/50/80%ile yield estimates
                       (t/ha) — judgement calls, not model predictions.
    protein_pct      : grain protein target (%)
    start_n          : starting mineral N, soil test or estimate (kg N/ha)
    fert_n           : fertiliser N applied (kg N/ha)
    fallow_n_actual  : this season's actual fallow-phase N mineralisation
                       (kg N/ha) — from n_mineralisation_gain() on the
                       current run
    in_crop_n_est    : long-term median (or other pctile) in-crop N
                       mineralisation estimate (kg N/ha) — from
                       in_crop_n_median()
    nue_pct          : nitrogen use efficiency (%) — fraction of supplied
                       N that ends up in the grain; default 50%
    grain_price      : optional grain net return ($/tonne). When given
                       (with fert_cost_per_kgn), adds 'grain_return' and
                       'extra_fert_cost' to each percentile's dict.
    fert_cost_per_kgn : optional fertiliser cost ($/kg N).

    Returns
    -------
    dict keyed by 20/50/80, each {'yield_t_ha', 'n_required', 'n_supply',
    'n_balance'} — n_balance positive means surplus, negative means
    deficit — plus 'grain_return' and 'extra_fert_cost' when grain_price
    and fert_cost_per_kgn are both given.
    """
    yields = {20: y20, 50: y50, 80: y80}
    n_supply = start_n + fallow_n_actual + in_crop_n_est + fert_n
    do_economics = grain_price is not None and fert_cost_per_kgn is not None

    out = {}
    for p, y in yields.items():
        # grain N requirement = yield(t/ha) * protein% * GRAIN_N_FACTOR / NUE%
        n_required = y * protein_pct * GRAIN_N_FACTOR / nue_pct
        n_balance = n_supply - n_required
        entry = {
            "yield_t_ha": y,
            "n_required": n_required,
            "n_supply": n_supply,
            "n_balance": n_balance,
        }
        if do_economics:
            entry["grain_return"] = y * grain_price
            # Cost of extra fertiliser N needed to close a deficit and
            # fully meet this percentile's yield potential; 0 in surplus.
            entry["extra_fert_cost"] = max(0.0, -n_balance) * fert_cost_per_kgn
        out[p] = entry
    return out
