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
)

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
    except ParamError as e:
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
    try:
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

    accel_x = scaled("X")
    disp_x = scaled("X_disp")
    time_arr, gdisp_x, _, abs_x = building_x.compute_response(accel_x, disp_x, dt)
    furn_x, npts_dec_x, q_x, rate_x = building_x.get_decimated_furniture()

    has_y = ground.get("Y") is not None
    if has_y:
        accel_y = scaled("Y")
        disp_y = scaled("Y_disp")
        _, gdisp_y, _, abs_y = building_y.compute_response(accel_y, disp_y, dt)
        furn_y, npts_dec_y, q_y, rate_y = building_y.get_decimated_furniture()
        npts_dec = min(npts_dec_x, npts_dec_y)
        furn_x = furn_x[:, :, :npts_dec]
        furn_y = furn_y[:, :, :npts_dec]
    else:
        npts_dec = npts_dec_x
        furn_y = None

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
        "theta_demand_X": building_x.theta_demand.tolist(),
        "theta_demand_Y": _y_or_none(has_y, building_y, "theta_demand"),
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
    header_bytes = json.dumps(header).encode("utf-8")
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
    parts.append(accel_x.astype(np.float32).tobytes())
    if has_y:
        parts.append(accel_y.astype(np.float32).tobytes())

    return Response(b"".join(parts), mimetype="application/octet-stream")


@app.route("/")
def index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(PROJECT_ROOT, path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
