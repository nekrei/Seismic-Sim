"""Educational equivalent-static sizing against the existing frame model.

Every code-shaped number is TO VERIFY against final BNBC 2020 Part 6. This is
an instructional generator, never a compliant building design.
"""

from dataclasses import dataclass

import numpy as np

from mdof_response import (E_CONCRETE, GRAVITY_MS2, GravityInstabilityError,
                           MDOF_ShearBuilding, SQM_PER_SQFT,
                           THETA_IGNORE, column_inertia, plan_dims_from_area,
                           story_plastic_shear)


# TO VERIFY: candidate values from a 2017 BNBC chapter draft, not final 2020.
BNBC_ZONE_Z = {1: .12, 2: .20, 3: .28, 4: .36}
BNBC_R = {"OMRF": 3., "IMRF": 5., "SMRF": 8.}
BNBC_CD = {"OMRF": 2.5, "IMRF": 4.5, "SMRF": 5.5}  # TO VERIFY: draft Table 6.2.19
IMPORTANCE = {"I/II": 1., "III": 1.25, "IV": 1.5}
# TO VERIFY: draft Table 6.2.16 (S, TB, TC, TD).
SITE = {"SA": (1., .15, .40, 2.), "SB": (1.2, .15, .50, 2.),
        "SC": (1.15, .20, .60, 2.), "SD": (1.35, .20, .80, 2.),
        "SE": (1.4, .15, .50, 2.)}
DRIFT_ALLOW = {"I/II": .020, "III": .015, "IV": .010}  # TO VERIFY: draft occupancy limits
CAPACITY_RATIO = 1.2  # TO VERIFY: story-strength proxy, NOT ACI joint rule
MAX_DESIGN_ITERATIONS = 10
MIN_DEPTH, MAX_DEPTH = .20, 2.0
RHO_MIN, RHO_MAX = .01, .06  # TO VERIFY: project demonstration bounds
DEAD_KPA, LIVE_KPA = 6., 2.  # TO VERIFY: demonstration surface loads
BACKBONE = {"OMRF": (2.5, 4.), "IMRF": (3.5, 7.),
            "SMRF": (5., 10.)}  # TO VERIFY: model assumptions, not code detailing


class DesignError(ValueError):
    """Invalid or infeasible illustrative design."""


@dataclass(frozen=True)
class DesignInput:
    num_stories: int = 7
    story_height: float = 3.5
    area_sqft: float = 542.501085
    occupancy: str = "I/II"
    zone: int = 2
    site_class: str = "SC"
    ductility: str = "SMRF"
    # These are project assumptions. They are shown in the returned audit.
    dead_kpa: float = DEAD_KPA
    live_kpa: float = LIVE_KPA


def period_exponent(period):
    """TO VERIFY: draft BNBC Sec. 2.5.7.4 vertical-distribution exponent."""
    return 1. if period <= .5 else 2. if period >= 2.5 else 1. + (period - .5) / 2.


def normalized_spectrum(period, site_class):
    """TO VERIFY: dimensionless draft-style 5%-damped spectrum shape.

    Z, I, and R are deliberately outside this function, avoiding double count.
    """
    s, tb, tc, td = SITE[site_class]
    if period <= tb:
        return s * (1. + 1.5 * period / tb)
    if period <= tc:
        return 2.5 * s
    if period <= td:
        return 2.5 * s * tc / period
    return 2.5 * s * tc * td / period**2


def design_demand(inp):
    if not isinstance(inp.num_stories, int) or not 1 <= inp.num_stories <= 30:
        raise DesignError("num_stories must be an integer from 1 to 30")
    if inp.zone not in BNBC_ZONE_Z or inp.site_class not in SITE or inp.ductility not in BNBC_R or inp.occupancy not in IMPORTANCE:
        raise DesignError("unknown zone, site class, occupancy, or ductility class")
    if not 1.5 <= inp.story_height <= 10. or not 200. <= inp.area_sqft <= 2000.:
        raise DesignError("story height or plan area is outside the model range")
    if not 0. < inp.dead_kpa <= 30. or not 0. <= inp.live_kpa <= 10.:
        raise DesignError("surface loads must be finite and inside the demonstration range")
    area_m2 = inp.area_sqft * SQM_PER_SQFT
    plan_x, plan_y = plan_dims_from_area(area_m2)
    floor_weight = area_m2 * (inp.dead_kpa + .25 * inp.live_kpa) * 1000.
    mass_per_floor = floor_weight / GRAVITY_MS2
    height = inp.num_stories * inp.story_height
    period = .0466 * height**.9  # TO VERIFY: draft concrete moment-frame Ct,m
    cs = normalized_spectrum(period, inp.site_class)
    base_shear = BNBC_ZONE_Z[inp.zone] * IMPORTANCE[inp.occupancy] * cs * (inp.num_stories * floor_weight) / BNBC_R[inp.ductility]
    levels = inp.story_height * np.arange(1, inp.num_stories + 1, dtype=float)
    weights = levels**period_exponent(period)
    forces = base_shear * weights / weights.sum()
    # Make the partition exact even after floating-point normalization.
    forces[-1] = base_shear - float(forces[:-1].sum())
    shears = np.cumsum(forces[::-1])[::-1]
    return dict(period_empirical_s=period, spectrum_cs=cs,
                floor_weight_N=floor_weight, mass_per_floor=mass_per_floor,
                plan_span_x=plan_x, plan_span_y=plan_y,
                base_shear_N=base_shear, floor_forces_N=forces,
                story_shears_N=shears)


def _models(inp, demand, dx, dy, beam_depth, rho):
    common = dict(num_stories=inp.num_stories,
                  mass_per_floor=demand["mass_per_floor"],
                  story_height=inp.story_height,
                  column_depth_x=dx, column_depth_y=dy,
                  beam_depth=beam_depth, rho_longitudinal=rho,
                  plan_span_x=demand["plan_span_x"],
                  plan_span_y=demand["plan_span_y"])
    return (MDOF_ShearBuilding(axis="X", **common),
            MDOF_ShearBuilding(axis="Y", **common))


def _strength_ok(dx, dy, h, shear, rho):
    return all(np.all(story_plastic_shear(dx, dy, axis, h, rho=rho)
                         >= CAPACITY_RATIO * shear) for axis in ("X", "Y"))


def _least_rho(dx, dy, h, shear):
    if _strength_ok(dx, dy, h, shear, RHO_MIN):
        return RHO_MIN
    if not _strength_ok(dx, dy, h, shear, RHO_MAX):
        return RHO_MAX
    lo, hi = RHO_MIN, RHO_MAX
    for _ in range(32):
        mid = (lo + hi) / 2.
        if _strength_ok(dx, dy, h, shear, mid):
            hi = mid
        else:
            lo = mid
    return hi


def generate_design(inp):
    """Return model inputs plus transparent diagnostic values; no model mutation."""
    demand = design_demand(inp)
    n = inp.num_stories
    h = np.full(n, inp.story_height)
    dx = np.full(n, MIN_DEPTH)
    dy = np.full(n, MIN_DEPTH)
    beam = np.full(n, 1.5)
    shear = demand["story_shears_N"]
    # Draft Sec. 2.5.7.7 amplifies elastic displacement by Cd/I before
    # comparing to allowable drift. Final BNBC 2020 text remains TO VERIFY.
    elastic_drift_ratio = DRIFT_ALLOW[inp.occupancy] * IMPORTANCE[inp.occupancy] / BNBC_CD[inp.ductility]
    req_k = shear / (elastic_drift_ratio * h)
    for iteration in range(1, MAX_DESIGN_ITERATIONS + 1):
        rho = _least_rho(dx, dy, h, shear)
        try:
            models = _models(inp, demand, dx, dy, beam, rho)
        except (GravityInstabilityError, ValueError, np.linalg.LinAlgError):
            dx = np.minimum(MAX_DEPTH, dx * 1.25)
            dy = np.minimum(MAX_DEPTH, dy * 1.25)
            continue
        ratios = []
        drifts = []
        for model in models:
            u = np.linalg.solve(model.K_no_pdelta, demand["floor_forces_N"])
            drift = np.diff(np.concatenate(([0.], u)))
            drifts.append(drift)
            ratios.append(np.maximum.reduce([
                req_k / model.k0_profile,
                np.abs(drift) / (elastic_drift_ratio * h),
                CAPACITY_RATIO * shear / model.V_p_profile,
                model.theta_stiffness / THETA_IGNORE,
                model.P_gravity / (4. * model.P_cap_profile),
            ]))
        # Period recheck is reported; empirical period is retained as the
        # design spectrum anchor because a prototype cannot certify Cu*Ta.
        period_actual = max(2 * np.pi / model.omega_n[0] for model in models)
        worst = np.maximum(ratios[0], ratios[1])
        if np.all(worst <= 1.001):
            mu_cap, mu_ult = BACKBONE[inp.ductility]
            shortcut_x = 48 * E_CONCRETE * models[0].cracked_factor_column * column_inertia(dx, dy, "X") / h**3
            shortcut_y = 48 * E_CONCRETE * models[1].cracked_factor_column * column_inertia(dx, dy, "Y") / h**3
            return dict(
                story_height_profile=h.tolist(),
                column_depth_x_profile=dx.tolist(),
                column_depth_y_profile=dy.tolist(),
                beam_depth_profile=beam.tolist(),
                rho_longitudinal=rho,
                mass_per_floor=demand["mass_per_floor"],
                backbone_mu_cap=mu_cap, backbone_mu_ult=mu_ult,
                base_shear_N=demand["base_shear_N"],
                floor_forces_N=demand["floor_forces_N"].tolist(),
                story_shears_N=shear.tolist(),
                required_k_N_per_m=req_k.tolist(),
                k0_X=models[0].k0_profile.tolist(),
                k0_Y=models[1].k0_profile.tolist(),
                shear_shortcut_k_X=shortcut_x.tolist(),
                shear_shortcut_k_Y=shortcut_y.tolist(),
                static_drift_X_m=drifts[0].tolist(),
                static_drift_Y_m=drifts[1].tolist(),
                period_empirical_s=demand["period_empirical_s"],
                period_actual_s=period_actual,
                period_over_draft_2s_limit=bool(period_actual > 2.),
                elastic_drift_ratio=elastic_drift_ratio,
                iterations=iteration,
                status="educational approximation; all code constants TO VERIFY")
        # The frame is coupled: these ratios guide a joint depth update; each
        # next iteration rebuilds the full condensed matrices for both axes.
        scale = np.maximum(1., worst)**.27
        dx = np.minimum(MAX_DEPTH, dx * scale)
        dy = np.minimum(MAX_DEPTH, dy * scale)
    raise DesignError("design did not converge within 10 iterations or 2 m column depth")
