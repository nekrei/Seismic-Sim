"""
Flask backend for Seismic-Sim (specs/03-live-archetypes.md, extended by
specs/05-structural-frame-furniture.md).

Serves the static frontend (replacing `python -m http.server` for local
dev) and a POST /compute endpoint that reruns the modal/FFT solver (now
built from real column/beam frame geometry, independently per axis, plus
each axis's furniture secondary-system response) for user-adjustable
building parameters against a cached earthquake record, so index.html's
parameter sliders can recompute the response live instead of only ever
showing the fixed out/ precomputed results.

Run: conda run -p .conda python server.py
"""
import json
import os
import struct

import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from mdof_response import (
    MDOF_ShearBuilding, FURNITURE_CLASSES, BEAM_WIDTH,
    plan_dims_from_area, DEFAULT_AREA_SQFT, SQM_PER_SQFT,
    apply_synthetic_earthquake_scaling,
    DEFAULT_EPICENTER_DISTANCE_KM, DEFAULT_EPICENTER_DEPTH_KM,
    SECTION_STIFFNESS_PRESETS, DEFAULT_SECTION_STIFFNESS_MODE,
    SOFT_STORY_HEIGHT_RATIO, GravityInstabilityError,
    MDOF_Building3N, pair_components, ComponentPairingError,
    tangent_eccentricity, solve_with_detachment, handoff_header,
    finite_or_none, furniture_decimation, accumulated_damage,
    subsample_nearest, DAMAGE_CODES, DAMAGE_CODE_OF_BRANCH,
    DRIFT_LIMIT_IO, DRIFT_LIMIT_LS,
)

# Spec 14 A1: optional per-story damage blocks, in WIRE order -- every
# float32 block before every uint8 block, so the float offsets stay 4-byte
# aligned; each uint8 block is zero-padded to 4 bytes (pad in the header).
DAMAGE_BLOCKS = ("story_drift", "story_shear", "stiffness_ratio", "p_nl",
                 "damage_state", "column_damage")

# Frame-geometry defaults -- MUST match mdof_response.py's __main__
# COLUMN_DEPTH_X/Y/BEAM_DEPTH constants AND index.html's slider defaults
# exactly (see the matching comment at each of the other two sites).
# index.html's buildingParamsAtDefault() uses equality against these to
# decide whether out/'s precomputed static files are still valid for the
# current slider positions.
DEFAULT_COLUMN_DEPTH_X = 1.10  # m
DEFAULT_COLUMN_DEPTH_Y = 1.10  # m
DEFAULT_BEAM_DEPTH = 1.50      # m

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_ROOT, "out")

app = Flask(__name__, static_folder=None)

# Ground motion (acceleration + displacement) doesn't change with building
# parameters, so cache each record's parsed ground_accel.json in memory
# after the first request instead of re-reading it from disk every time.
_ground_cache = {}


def _load_ground(record):
    if record in _ground_cache:
        return _ground_cache[record]
    path = os.path.join(OUT_DIR, record, "ground_accel.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No cached ground motion for record '{record}'")
    with open(path) as f:
        data = json.load(f)
    _ground_cache[record] = data
    return data


def _y_or_none(has_y, building_y, attr):
    """A response-dependent per-story array for the Y axis, or None.

    building_y is constructed unconditionally (its modal results are
    cheap and always valid), but compute_response() is only run on it
    when the record actually has a second horizontal component. The
    quantities that depend on a solved time history therefore do not
    exist otherwise, and reporting X's numbers under a _Y key would be
    worse than reporting nothing."""
    if not has_y:
        return None
    return getattr(building_y, attr).tolist()


from mdof_response import HFTD_DEFAULTS, build_backbones


class ParamError(ValueError):
    """A request parameter that cannot be clamped into sanity -- it has to
    be rejected. Distinct from the clamping the rest of _validate_params
    does, because a per-floor profile of the wrong length is not a value
    that is merely too large: it describes a DIFFERENT BUILDING from the
    one the caller asked for, and truncating or padding it would render
    completely plausibly on screen (spec 10, C2/E)."""


def _validate_profile(body, key, num_stories):
    """A per-floor profile: absent/null, or exactly `num_stories` finite,
    strictly positive floats. Never truncated, never padded."""
    raw = body.get(key)
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ParamError(f"{key} must be a list of {num_stories} numbers "
                         f"or null, got {type(raw).__name__}")
    if len(raw) != num_stories:
        raise ParamError(f"{key} must have exactly num_stories "
                         f"({num_stories}) entries, got {len(raw)}")
    out = []
    for i, v in enumerate(raw):
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ParamError(f"{key}[{i}] is not a number: {v!r}") from None
        if not np.isfinite(f) or f <= 0.0:
            raise ParamError(f"{key}[{i}] must be finite and strictly "
                             f"positive, got {v!r}")
        out.append(f)
    return out


def _validate_nonlinear(body):
    nonlinear = body.get('nonlinear', False)
    if not isinstance(nonlinear, bool):
        raise ParamError('nonlinear must be a boolean')
    limits = dict(backbone_hardening=(0,.5), backbone_softening=(0,2),
        backbone_mu_cap=(1.5,20), backbone_mu_ult=(1.5,50),
        backbone_residual_frac=(0,.5), unload_degrade_exp=(0,1),
        pinch_drift_frac=(0,1), pinch_force_frac=(0,1),
        column_strength_cov=(0,.5), hftd_relaxation=(.05,1),
        hftd_tolerance=(1e-8,1e-2), hftd_max_iterations=(1,200),
        drift_limit_cp=(.002,.20), collapse_mu_cap=(1.5,50),
        intensity_scale=(.05,20))
    p = {}
    for name,(low,high) in limits.items():
        try:
            value=float(body.get(name,HFTD_DEFAULTS[name]))
        except (ValueError,TypeError):
            raise ParamError(f'{name} must be a finite number') from None
        if not np.isfinite(value):
            raise ParamError(f'{name} must be finite')
        p[name]=max(low,min(high,value))
    p['hftd_max_iterations']=int(p['hftd_max_iterations'])
    segment=body.get('hftd_segment_seconds')
    if segment is not None:
        try:
            segment=float(segment)
        except (ValueError,TypeError):
            raise ParamError('hftd_segment_seconds must be finite or null') from None
        if not np.isfinite(segment):
            raise ParamError('hftd_segment_seconds must be finite or null')
        segment=max(1.,segment)
    p['hftd_segment_seconds']=segment
    # Spec 12: default ON for elastic, OFF for nonlinear. The coupled
    # nonlinear solve is ~1.5x the both-axes cost (N=20: 203 s against
    # 131 s, GATE B), past a deploy timeout the per-axis path already
    # strains; elastic 3N is sub-second either way (Gate A).
    torsion = body.get('torsion', not nonlinear)
    if not isinstance(torsion, bool):
        raise ParamError('torsion must be a boolean')
    p['torsion'] = torsion
    if p['backbone_mu_ult'] <= p['backbone_mu_cap']:
        raise ParamError('backbone_mu_ult must exceed backbone_mu_cap')
    if p['backbone_softening'] and (1+p['backbone_hardening']*(p['backbone_mu_cap']-1)
            -p['backbone_softening']*(p['backbone_mu_ult']-p['backbone_mu_cap']) > p['backbone_residual_frac']+1e-14):
        raise ParamError('backbone_softening cannot reach residual strength by mu_ult')
    return nonlinear,p


def _validate_damage_blocks(body):
    """Spec 14 A2: the subset of DAMAGE_BLOCKS to send, in wire order."""
    raw = body.get("damage_blocks") or []
    if not isinstance(raw, list) or any(b not in DAMAGE_BLOCKS for b in raw):
        raise ParamError(f"damage_blocks must be a list drawn from {list(DAMAGE_BLOCKS)}")
    return [b for b in DAMAGE_BLOCKS if b in raw]


def _damage_payload(results, dt, drift_limit_cp):
    """Spec 14 blocks for the per-axis HFTDResults: (header dict, arrays).

    drift/shear/p_nl go at the full rate (the hysteresis loop needs true
    peaks); the tangent ratio and the codes are subsampled nearest-neighbour
    to ~50 Hz -- no filter, so every transmitted value is a real sample.
    """
    q = furniture_decimation(dt)
    arrays = {name: [] for name in DAMAGE_BLOCKS}
    backbones = {}
    for axis, r in results:
        col, story = accumulated_damage(r.column_damage, r.summary["t_fail"], dt)
        arrays["story_drift"].append(r.story_drift)
        arrays["story_shear"].append(r.story_shear)
        arrays["stiffness_ratio"].append(subsample_nearest(r.k_t_ratio, q))
        arrays["p_nl"].append(r.p_nl)
        arrays["damage_state"].append(subsample_nearest(story, q))
        arrays["column_damage"].append(subsample_nearest(col, q))
        # Per-column backbone parameters (N x 4), for the viewer's summed
        # backbone overlay. du is inf on a bilinear backbone -> null.
        bb = r.backbones
        backbones[axis] = {k: bb[k].tolist() for k in ("k0", "vy", "dy", "dc", "vr")}
        backbones[axis]["du"] = [finite_or_none(row) for row in bb["du"]]
        backbones[axis].update(hardening=bb["params"]["backbone_hardening"],
                               softening=bb["params"]["backbone_softening"])
    arrays = {k: np.stack(v).astype(np.float32 if k in DAMAGE_BLOCKS[:4] else np.uint8)
              for k, v in arrays.items()}
    header = {
        "version": 1,
        "codes": {str(k): v for k, v in DAMAGE_CODES.items()},
        "raw_branches": ["ELASTIC", "BACKBONE_POS", "BACKBONE_NEG", "UNLOAD", "RELOAD", "RESIDUAL"],
        "code_of_branch": DAMAGE_CODE_OF_BRANCH.tolist(),
        "drift_limits": {"IO": DRIFT_LIMIT_IO, "LS": DRIFT_LIMIT_LS, "CP": drift_limit_cp},
        "axes": [axis for axis, _ in results],
        "backbones": backbones,
    }
    return header, arrays, 1 / dt, 1 / (dt * q)


def _validate_params(body, reference_magnitude=6.0):
    """Clamp incoming slider values to sane bounds -- protects against
    pathological compute times or degenerate models, not against malice.
    T1_factor is gone (spec 5): period is now an output of the frame
    dimensions, not an input -- see specs/05-structural-frame-furniture.md
    Part C1. Column/beam depths are clamped strictly positive so K can
    never go singular. Epicenter distance/depth are clamped strictly
    positive so hypocentral distance R can never be zero (spec 7).
    area_sqft (spec 8) is clamped to 200-2000, the same "sane bounds"
    reasoning as the column/beam depths -- it drives plan_span_x/y via
    plan_dims_from_area(), which in turn is the beam span L fed into K."""
    num_stories = max(1, min(30, int(body.get("num_stories", 7))))
    mass_per_floor = max(1e3, min(1e8, float(body.get("mass_per_floor", 1000e3))))
    zeta = max(0.005, min(0.5, float(body.get("zeta", 0.05))))
    column_depth_x = max(0.15, min(2.0, float(body.get("column_depth_x", DEFAULT_COLUMN_DEPTH_X))))
    column_depth_y = max(0.15, min(2.0, float(body.get("column_depth_y", DEFAULT_COLUMN_DEPTH_Y))))
    beam_depth = max(0.10, min(3.0, float(body.get("beam_depth", DEFAULT_BEAM_DEPTH))))
    epicenter_distance_km = max(1.0, min(200.0, float(body.get("epicenter_distance_km", DEFAULT_EPICENTER_DISTANCE_KM))))
    epicenter_depth_km = max(1.0, min(100.0, float(body.get("epicenter_depth_km", DEFAULT_EPICENTER_DEPTH_KM))))
    richter_magnitude = max(3.0, min(9.0, float(body.get("richter_magnitude", reference_magnitude))))
    area_sqft = max(200.0, min(2000.0, float(body.get("area_sqft", DEFAULT_AREA_SQFT))))

    # --- spec 10 ------------------------------------------------------
    # An unknown section_stiffness_mode falls back to the default rather
    # than 400ing: it is a closed vocabulary the client picks from, so a
    # stale client sending an old name should still get a building.
    mode = body.get("section_stiffness_mode", DEFAULT_SECTION_STIFFNESS_MODE)
    if mode not in SECTION_STIFFNESS_PRESETS:
        mode = DEFAULT_SECTION_STIFFNESS_MODE
    p_delta = bool(body.get("p_delta", True))
    soft_ground_story = bool(body.get("soft_ground_story", False))

    profiles = {k: _validate_profile(body, k, num_stories) for k in (
        "story_height_profile", "column_depth_x_profile",
        "column_depth_y_profile", "beam_depth_profile")}

    # The preset is resolved HERE, server-side, so "soft ground story"
    # means exactly one thing in exactly one place and the client never
    # encodes structural meaning (spec 10, C2). An explicit
    # story_height_profile wins -- it is the more specific request.
    story_height = profiles["story_height_profile"]
    if story_height is None:
        h0 = max(1.5, min(10.0, float(body.get("story_height", 3.5))))
        if soft_ground_story:
            story_height = [SOFT_STORY_HEIGHT_RATIO * h0] + [h0] * (num_stories - 1)
        else:
            story_height = h0

    return {
        "num_stories": num_stories,
        "mass_per_floor": mass_per_floor,
        "zeta": zeta,
        "column_depth_x": profiles["column_depth_x_profile"] or column_depth_x,
        "column_depth_y": profiles["column_depth_y_profile"] or column_depth_y,
        "beam_depth": profiles["beam_depth_profile"] or beam_depth,
        "story_height": story_height,
        "epicenter_distance_km": epicenter_distance_km,
        "epicenter_depth_km": epicenter_depth_km,
        "richter_magnitude": richter_magnitude,
        "area_sqft": area_sqft,
        "section_stiffness_mode": mode,
        "p_delta": p_delta,
        "soft_ground_story": soft_ground_story,
        # The scalar values the header still reports, kept separately from
        # the (possibly array) values fed to the model.
        "column_depth_x_scalar": column_depth_x,
        "column_depth_y_scalar": column_depth_y,
        "beam_depth_scalar": beam_depth,
    }


@app.route("/compute", methods=["POST"])
def compute():
    body = request.get_json(force=True, silent=True) or {}
    record = body.get("record")
    if not record:
        return jsonify({"error": "record is required"}), 400

    try:
        ground = _load_ground(record)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

    reference_magnitude = ground.get("reference_magnitude", 6.0)
    try:
        p = _validate_params(body, reference_magnitude)
        nonlinear, nonlinear_params = _validate_nonlinear(body)
        damage_blocks = _validate_damage_blocks(body)
    except (ParamError, ValueError, TypeError, OverflowError) as e:
        # 400, never a silently-truncated profile -- see ParamError.
        return jsonify({"error": "invalid_parameter", "detail": str(e)}), 400

    num_stories = p["num_stories"]
    mass_per_floor = p["mass_per_floor"]
    zeta = p["zeta"]
    epicenter_distance_km = p["epicenter_distance_km"]
    epicenter_depth_km = p["epicenter_depth_km"]
    richter_magnitude = p["richter_magnitude"]
    area_sqft = p["area_sqft"]
    dt = ground["dt"]

    plan_span_x, plan_span_y = plan_dims_from_area(area_sqft * SQM_PER_SQFT)

    # X and Y each need their own instance -- independent condensed K per
    # axis (spec A1's anisotropy), exactly like the offline __main__
    # pipeline in mdof_response.py (never duplicate that logic here).
    common = dict(
        mass_per_floor=mass_per_floor, zeta=zeta,
        story_height=p["story_height"],
        column_depth_x=p["column_depth_x"], column_depth_y=p["column_depth_y"],
        beam_depth=p["beam_depth"],
        plan_span_x=plan_span_x, plan_span_y=plan_span_y,
        section_stiffness_mode=p["section_stiffness_mode"],
        p_delta=p["p_delta"],
    )
    torsion = nonlinear_params.pop('torsion')
    has_y = ground.get("Y") is not None
    b3 = None
    torsion_fallback = None
    try:
        if torsion:
            # Spec 12 B5: pair_components() is the one place the pairing
            # rules live. A cache written before spec 12 carries no
            # orientation keys and is rejected here, falling back to the
            # per-axis path, rather than guessed.
            try:
                pair_components(ground["X"], ground["X_disp"], dt,
                                ground.get("Y"), ground.get("Y_disp"), dt,
                                x_orient=ground.get("x_orient_deg"),
                                y_orient=ground.get("y_orient_deg"))
                b3 = MDOF_Building3N(num_stories, **common)
            except ComponentPairingError as e:
                torsion_fallback = str(e)
        if b3 is not None:
            # The per-axis buildings the 3N class holds get their DOF block
            # of the coupled solve installed on them, so everything below
            # that reads building_x/_y stays one code path.
            building_x, building_y = b3.bx, b3.by
        else:
            building_x = MDOF_ShearBuilding(num_stories, axis="X", **common)
            building_y = MDOF_ShearBuilding(num_stories, axis="Y", **common)
    except GravityInstabilityError as e:
        # 422: the parameters are well-formed, the building just cannot
        # stand up under its own weight. A physically meaningful result,
        # not a crash -- and critically, a VALID JSON body. Letting the
        # NaN through instead would produce a bare `NaN` token that
        # JSON.parse rejects, surfacing in the browser as a parse error a
        # long way from its cause (spec 10, B3).
        return jsonify({
            "error": "gravity_unstable",
            "story": int(e.story) if e.story is not None else None,
            "detail": str(e),
        }), 422

    # Reshape the real record's ground motion into the requested synthetic
    # earthquake (spec 7). At the default Earthquake Parameters this is an
    # identity transform (check 1), so out/ parity stays exact.
    #
    # The SAME filter is applied to the cached ground *displacement* rather
    # than re-deriving displacement from the scaled acceleration. Two
    # reasons, both load-bearing:
    #   1. Consistency. apply_synthetic_earthquake_scaling() multiplies the
    #      spectrum by a real, non-negative s(f), and the displacement and
    #      acceleration spectra differ only by the factor -1/w^2 -- so
    #      multiplying either by s(f) is the identical operation. The
    #      scaled (accel, disp) pair stays exactly as self-consistent as
    #      the recorded pair was, and stays linear in magnitude_scale.
    #   2. Continuity (bug fix). ground_accel.json's X_disp/Y_disp is
    #      usually the *actually recorded* DT2 displacement, not accel's
    #      double integral, and the two differ substantially (~2.3x in
    #      peak, 0.65 correlation on KOCAELI_ATK -- PEER's own baseline
    #      correction is not reproducible by _integrate_accel's generic
    #      high-pass). The old code re-integrated only when the parameters
    #      left their defaults, so nudging any Earthquake Parameter by a
    #      single step swapped the ground-displacement source underneath
    #      the animation: the whole building jumped to a differently-shaped,
    #      differently-scaled waveform. That was one of the two causes of
    #      "the building visibly moves away from the center."
    def scaled(key):
        return apply_synthetic_earthquake_scaling(
            np.array(ground[key]), dt, magnitude=richter_magnitude,
            distance_km=epicenter_distance_km, depth_km=epicenter_depth_km,
            reference_magnitude=reference_magnitude,
        )

    def respond(building, acceleration, displacement):
        # Elastic only; nonlinear requests go through solve_with_detachment.
        return building.compute_response(acceleration*nonlinear_params['intensity_scale'],
            displacement*nonlinear_params['intensity_scale'], dt)

    try:
        accel_x = scaled("X")
        disp_x = scaled("X_disp")
        if b3 is not None:
            # Scaling happens before pairing: the filter is linear and per
            # component, and pairing only zero-pads from sample 0.
            pc = pair_components(accel_x, disp_x, dt,
                                 scaled("Y"), scaled("Y_disp"), dt,
                                 x_orient=ground["x_orient_deg"],
                                 y_orient=ground["y_orient_deg"])
            scale = nonlinear_params['intensity_scale']
            if nonlinear:
                # Spec 13: the nonlinear solve restarts the surviving
                # structure at each detachment (a no-detachment run is the
                # plain spec-12 solve, untouched).
                solve_with_detachment(b3, pc.accel_x, pc.disp_x,
                    pc.accel_y, pc.disp_y, dt=dt, record=record,
                    **nonlinear_params)
            else:
                b3.compute_response(pc.accel_x*scale, pc.disp_x*scale,
                                    pc.accel_y*scale, pc.disp_y*scale, dt)
            time_arr = b3.time
            gdisp_x, abs_x = building_x.ground_disp, building_x.floor_disp_abs
        elif nonlinear:
            # Spec 13 C-4: detachment is a property of the story, so both
            # axes are solved together and restart at the earliest event.
            solve_with_detachment(building_x, accel_x, disp_x,
                scaled("Y") if has_y else None, scaled("Y_disp") if has_y else None,
                dt=dt, building_y=building_y if has_y else None, record=record,
                **nonlinear_params)
            time_arr, gdisp_x, abs_x = (building_x.time, building_x.ground_disp,
                                        building_x.floor_disp_abs)
        else:
            time_arr, gdisp_x, _, abs_x = respond(building_x, accel_x, disp_x)
        furn_x, npts_dec_x, q_x, rate_x = building_x.get_decimated_furniture()

        if has_y:
            if b3 is not None or nonlinear:
                gdisp_y, abs_y = building_y.ground_disp, building_y.floor_disp_abs
            else:
                accel_y = scaled("Y")
                disp_y = scaled("Y_disp")
                _, gdisp_y, _, abs_y = respond(building_y, accel_y, disp_y)
            furn_y, npts_dec_y, q_y, rate_y = building_y.get_decimated_furniture()
            npts_dec = min(npts_dec_x, npts_dec_y)
            furn_x = furn_x[:, :, :npts_dec]
            furn_y = furn_y[:, :, :npts_dec]
        else:
            npts_dec = npts_dec_x
            furn_y = None
    except (FloatingPointError, OverflowError, np.linalg.LinAlgError) as e:
        return jsonify({
            "error": "numerical_failure",
            "detail": str(e),
        }), 500

    # The metadata (frequencies, mode shapes, frame geometry) is tiny --
    # JSON is fine for it. The time-series arrays are not: at full
    # resolution they're hundreds of thousands of floats, and profiling
    # showed JSON encode+decode of that (not the actual physics, which
    # takes ~20-50ms) is what was blowing the live-recompute latency
    # budget past 2 seconds per request. Binary float32 transfer cuts both
    # the payload size (~5.5x, float32 vs ASCII decimal) and, more
    # importantly, the per-element text formatting/parsing cost that
    # dominated the old all-JSON response.
    header = {
        "num_stories": num_stories,
        # Scalar ground-story height, kept for pre-spec-10 readers; the
        # per-floor profile (spec 10, C1) ships alongside it.
        "story_height": float(building_x.h[0]),
        "npts": len(time_arr),
        "has_y": has_y,
        # Spec 9: the ground acceleration the physics actually ran on --
        # i.e. AFTER apply_synthetic_earthquake_scaling() -- is appended at
        # the payload tail so the time-domain panel can plot the real input.
        # The frontend must never re-derive it by differentiating ground
        # displacement, and must never fall back to out/'s unscaled
        # ground_accel.json once any Earthquake Parameter leaves default.
        # Flagged rather than assumed so a stale deployed backend that
        # doesn't send the block degrades instead of throwing.
        "has_ground_accel": True,
        "reference_magnitude": reference_magnitude,
        "richter_magnitude": richter_magnitude,
        "epicenter_distance_km": epicenter_distance_km,
        "epicenter_depth_km": epicenter_depth_km,

        "elastic_modulus_Pa": building_x.E,
        "column_depth_x": p["column_depth_x_scalar"],
        "column_depth_y": p["column_depth_y_scalar"],
        "beam_depth": p["beam_depth_scalar"],
        "beam_width": BEAM_WIDTH,

        # --- spec 10, Part E -- all additive, all per-story scalars so
        # they belong in the header rather than a new binary block (a
        # block would cost a has_* flag, a takeFloats() read-order change
        # and a seismic-sim-backend staleness hazard; header keys are safe
        # by construction, since the client recomputes the 4-byte pad from
        # the header length it reads back).
        "story_heights": building_x.h.tolist(),
        "section_stiffness_mode": p["section_stiffness_mode"],
        "cracked_factor_column": building_x.cracked_factor_column,
        "cracked_factor_beam": building_x.cracked_factor_beam,
        "p_delta_enabled": p["p_delta"],
        "soft_ground_story": p["soft_ground_story"],
        "gravity_unstable": False,
        "k_g_per_story_N_per_m_X": building_x.k_g.tolist(),
        "k_g_per_story_N_per_m_Y": building_y.k_g.tolist(),
        "k0_per_story_N_per_m_X": building_x.k0_profile.tolist(),
        "k0_per_story_N_per_m_Y": building_y.k0_profile.tolist(),
        "theta_stiffness_X": building_x.theta_stiffness.tolist(),
        "theta_stiffness_Y": building_y.theta_stiffness.tolist(),
        # The three response-dependent quantities only exist once
        # compute_response() has run, and Y is skipped entirely for a
        # record with no second horizontal component -- so they are null
        # there rather than silently mirroring X's numbers.
        # null (never inf) where theta is undefined: zero story shear at
        # the drift peak. See finite_or_none.
        "theta_demand_X": finite_or_none(building_x.theta_demand),
        "theta_demand_Y": (finite_or_none(building_y.theta_demand)
                           if has_y else None),
        "peak_drift_ratio_X": building_x.peak_drift_ratio.tolist(),
        "peak_drift_ratio_Y": _y_or_none(has_y, building_y,
                                         "peak_drift_ratio"),
        "V_p_per_story_N_X": building_x.V_p_profile.tolist(),
        "V_p_per_story_N_Y": building_y.V_p_profile.tolist(),
        "delta_y_per_story_m_X": building_x.delta_y_profile.tolist(),
        "delta_y_per_story_m_Y": building_y.delta_y_profile.tolist(),
        # mu is an ELASTIC-DEMAND indicator, not an achieved ductility
        # (spec 10, D5) -- labelled as such wherever it is displayed.
        "mu_demand_X": building_x.mu_demand.tolist(),
        "mu_demand_Y": _y_or_none(has_y, building_y, "mu_demand"),
        # Axial capacity is axis-independent (it depends on the gross
        # section, not on which way the frame is bending).
        "P_cap_per_story_N": np.broadcast_to(
            np.asarray(building_x.P_cap_profile, dtype=float),
            (num_stories,)).tolist(),
        "plan_span_x": plan_span_x,
        "plan_span_y": plan_span_y,

        "natural_frequencies_Hz_X": (building_x.omega_n / (2 * np.pi)).tolist(),
        "mode_shapes_X": building_x.phi.tolist(),
        "fundamental_period_s_X": float(2 * np.pi / building_x.omega_n[0]),
        "story_stiffness_X_N_per_m": building_x.story_stiffness,
        # Modal participation factors -- the live path's building_data.json
        # equivalent already carries these (save_building_data), but the
        # /compute header didn't, so the frontend's transfer-function panel
        # (spec 6 Part C1) had no live-recompute source for them. Closes
        # that static/live asymmetry.
        "participation_factors_X": building_x.Gamma.tolist(),

        "natural_frequencies_Hz_Y": (building_y.omega_n / (2 * np.pi)).tolist(),
        "mode_shapes_Y": building_y.phi.tolist(),
        "fundamental_period_s_Y": float(2 * np.pi / building_y.omega_n[0]),
        "story_stiffness_Y_N_per_m": building_y.story_stiffness,
        "participation_factors_Y": building_y.Gamma.tolist(),

        # Damping ratio -- same reason as the participation factors above;
        # X and Y share one zeta (spec A1), so no _Y suffix needed.
        "damping_ratio": zeta,

        "furniture": {
            "classes": list(FURNITURE_CLASSES.keys()),
            "class_params": FURNITURE_CLASSES,
            "npts_decimated": npts_dec,
            "decimated_rate_hz": rate_x,
        },
    }
    if nonlinear:
        axes = {'X': building_x.hftd_result.summary,
                'Y': building_y.hftd_result.summary if has_y else None}
        summaries = [summary for summary in axes.values() if summary is not None]
        events = [event for summary in summaries for event in summary['collapse_events']]
        converged = all(summary['converged'] for summary in summaries)
        header['collapse'] = dict(axes=axes, converged=converged,
            iterations=max(summary['iterations'] for summary in summaries),
            reason='' if converged else 'Collapse analysis did not converge; no collapse is inferred.',
            collapse_axis=min(events,key=lambda event:event['time'])['axis'] if events and converged else None,
            collapse_events=events if converged else [])
        # Spec 13: the versioned hand-off contract (1-based `story`, metres,
        # physics frame). Floors above a failure plane HOLD their last
        # structural value in abs_x/abs_y/theta_z from `t_detach` on; these
        # timestamps are the authoritative "no longer structural" signal.
        header['collapse'].update(handoff_header(
            b3 if b3 is not None else building_x,
            None if b3 is not None or not has_y else building_y))
    # Spec 14 R5: the per-story column depths actually built, echoed only
    # when a profile was sent, so a no-profile payload stays byte-identical.
    for axis in ("x", "y"):
        if isinstance(p[f"column_depth_{axis}"], list):
            header[f"column_depth_{axis}_per_story"] = p[f"column_depth_{axis}"]
    # Spec 14: elastic requests never carry blocks -- there is no HFTDResult.
    damage_arrays = {}
    if nonlinear and damage_blocks:
        damage, all_arrays, full_hz, dec_hz = _damage_payload(
            [("X", building_x.hftd_result)]
            + ([("Y", building_y.hftd_result)] if has_y else []),
            dt, nonlinear_params["drift_limit_cp"])
        damage["blocks"] = []
        for name in damage_blocks:
            a = damage_arrays[name] = all_arrays[name]
            header[f"has_{name}"] = True
            damage["blocks"].append(dict(
                name=name, dtype=str(a.dtype), shape=list(a.shape), npts=a.shape[-1],
                rate_hz=full_hz if a.shape[-1] == len(time_arr) else dec_hz,
                pad_bytes=(-a.nbytes) % 4))
        header["damage"] = damage
    # Spec 12 C1. Every new key is emitted ONLY when torsion was requested,
    # so a torsion=false payload is byte-identical to spec 11's, header and
    # pad included (verification check 9, correction V-7) -- not merely
    # identical from the first float block onward.
    if torsion:
        header["torsion_enabled"] = True
        header["has_floor_rotation"] = b3 is not None
        if b3 is None:
            header["torsion_fallback_reason"] = torsion_fallback
    if b3 is not None:
        N = num_stories
        freqs = b3.omega_n / (2 * np.pi)
        dominant = np.argmax(b3.modal_block_share, axis=0)
        if nonlinear:
            r = b3.hftd_result
            with np.errstate(invalid="ignore", divide="ignore"):
                ecc = tangent_eccentricity(r)                # (2, N, npts)
            # The centre of rigidity is a weighted average of the column
            # positions -- and so inside the plan -- only while every
            # tangent is >= 0 with a positive sum. A fully plateaued story
            # gives 0/0, a softening one can put it anywhere; neither is a
            # centre of anything, so those samples are left out, and a story
            # with none left reports null.
            kt = np.stack([r.x.column_tangent, r.y.column_tangent])
            defined = np.all(kt >= 0, axis=2) & (kt.sum(axis=2) > 0)
            masked = np.where(defined, np.abs(ecc), -1.0)
            peak = np.argmax(masked, axis=2)
            ecc = np.take_along_axis(ecc, peak[..., None], axis=2)[..., 0]
            ecc = np.where(np.any(defined, axis=2), ecc, np.nan)
            ecc = [[None if np.isnan(v) else float(v) for v in row] for row in ecc]
        else:
            # Elastic columns all carry k0/4 at symmetric corners, so the
            # centre of rigidity IS the centre of mass: exactly zero.
            ecc = np.zeros((2, N)).tolist()
        header.update({
            "plan_a": plan_span_x,
            "plan_b": plan_span_y,
            # Signed value at each story's peak |e| -- the sign says which
            # side degraded, which a bare magnitude envelope would drop.
            "eccentricity_x": ecc[0],
            "eccentricity_y": ecc[1],
            "omega_theta_over_omega_x": b3.frequency_ratio("X"),
            # The 3N modal set, DOF ordering [u_x, u_y, theta].
            "natural_frequencies_Hz": freqs.tolist(),
            "mode_shapes": b3.phi.tolist(),
            "participation_factors_x": b3.Gamma_x.tolist(),
            "participation_factors_y": b3.Gamma_y.tolist(),
        })
        # The old per-axis keys, repopulated from the modes each DOF block
        # dominates, so a reader that only knows _X/_Y still gets this
        # solve's modes rather than a separate per-axis eigensolve's.
        # ponytail: argmax partition; if the X and Y frequencies ever
        # coincide exactly, eigh may mix the degenerate pair and the split
        # becomes arbitrary -- the 3N keys above stay correct regardless.
        for k, axis, gamma in ((0, "X", b3.Gamma_x), (1, "Y", b3.Gamma_y)):
            sel = dominant == k
            header[f"natural_frequencies_Hz_{axis}"] = freqs[sel].tolist()
            header[f"mode_shapes_{axis}"] = b3.phi[k*N:(k+1)*N][:, sel].tolist()
            header[f"fundamental_period_s_{axis}"] = float(1 / freqs[sel][0])
            header[f"participation_factors_{axis}"] = gamma[sel].tolist()
    header_bytes = json.dumps(header, allow_nan=False).encode("utf-8")
    # Float32Array requires its byte offset to be a multiple of 4, but the
    # JSON header's length isn't guaranteed to be -- pad with zero bytes so
    # the float data that follows always starts 4-byte aligned. The client
    # applies the same padding calculation when it reads header_len back out.
    pad = (-(4 + len(header_bytes))) % 4

    # Binary payload is append-only after the existing spec-3 prefix:
    # time, gdisp_x, abs_x, [gdisp_y, abs_y], furn_x(3xNxnpts_dec),
    # [furn_y(...)], gaccel_x, [gaccel_y] -- the 4-byte alignment from the header pad
    # still holds since every array here is float32 (spec 5, plan section 3).
    parts = [struct.pack("<I", len(header_bytes)), header_bytes, b"\x00" * pad]
    parts.append(time_arr.astype(np.float32).tobytes())
    parts.append(gdisp_x.astype(np.float32).tobytes())
    parts.append(abs_x.astype(np.float32).tobytes())  # (num_stories, npts), floor-major
    if has_y:
        parts.append(gdisp_y.astype(np.float32).tobytes())
        parts.append(abs_y.astype(np.float32).tobytes())
    parts.append(furn_x.tobytes())  # (3, num_stories, npts_dec) float32
    if has_y:
        parts.append(furn_y.tobytes())
    # Ground acceleration goes at the TAIL, after the furniture blocks, so
    # the format stays append-only (spec 9). len(accel_x) == len(time_arr):
    # compute_response() sets npts = len(acceleration) and time = arange(npts)*dt.
    parts.append(building_x.accel.astype(np.float32).tobytes())
    if has_y:
        parts.append(building_y.accel.astype(np.float32).tobytes())
    # Spec 12: floor rotation about +Z, radians, (num_stories, npts)
    # floor-major -- after everything else, gated by has_floor_rotation.
    if b3 is not None:
        parts.append(b3.floor_rot.astype(np.float32).tobytes())
    # Spec 14: the requested damage blocks, already in wire order (float32
    # first, then uint8, each uint8 block zero-padded to 4 bytes).
    for a in damage_arrays.values():
        parts.append(a.tobytes())
        parts.append(b"\x00" * ((-a.nbytes) % 4))

    return Response(b"".join(parts), mimetype="application/octet-stream")


@app.route("/")
def index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(PROJECT_ROOT, path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
