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
    PLAN_SPAN_X, PLAN_SPAN_Y,
    apply_synthetic_earthquake_scaling,
    DEFAULT_EPICENTER_DISTANCE_KM, DEFAULT_EPICENTER_DEPTH_KM,
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


def _validate_params(body, reference_magnitude=6.0):
    """Clamp incoming slider values to sane bounds -- protects against
    pathological compute times or degenerate models, not against malice.
    T1_factor is gone (spec 5): period is now an output of the frame
    dimensions, not an input -- see specs/05-structural-frame-furniture.md
    Part C1. Column/beam depths are clamped strictly positive so K can
    never go singular. Epicenter distance/depth are clamped strictly
    positive so hypocentral distance R can never be zero (spec 7)."""
    num_stories = max(1, min(30, int(body.get("num_stories", 7))))
    mass_per_floor = max(1e3, min(1e8, float(body.get("mass_per_floor", 1000e3))))
    zeta = max(0.005, min(0.5, float(body.get("zeta", 0.05))))
    column_depth_x = max(0.15, min(2.0, float(body.get("column_depth_x", DEFAULT_COLUMN_DEPTH_X))))
    column_depth_y = max(0.15, min(2.0, float(body.get("column_depth_y", DEFAULT_COLUMN_DEPTH_Y))))
    beam_depth = max(0.10, min(3.0, float(body.get("beam_depth", DEFAULT_BEAM_DEPTH))))
    epicenter_distance_km = max(1.0, min(200.0, float(body.get("epicenter_distance_km", DEFAULT_EPICENTER_DISTANCE_KM))))
    epicenter_depth_km = max(1.0, min(100.0, float(body.get("epicenter_depth_km", DEFAULT_EPICENTER_DEPTH_KM))))
    richter_magnitude = max(3.0, min(9.0, float(body.get("richter_magnitude", reference_magnitude))))
    return (num_stories, mass_per_floor, zeta, column_depth_x, column_depth_y,
            beam_depth, epicenter_distance_km, epicenter_depth_km, richter_magnitude)


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
    (num_stories, mass_per_floor, zeta, column_depth_x, column_depth_y,
     beam_depth, epicenter_distance_km, epicenter_depth_km,
     richter_magnitude) = _validate_params(body, reference_magnitude)
    dt = ground["dt"]

    # X and Y each need their own instance -- independent condensed K per
    # axis (spec A1's anisotropy), exactly like the offline __main__
    # pipeline in mdof_response.py (never duplicate that logic here).
    building_x = MDOF_ShearBuilding(
        num_stories, mass_per_floor=mass_per_floor, zeta=zeta,
        column_depth_x=column_depth_x, column_depth_y=column_depth_y,
        beam_depth=beam_depth, axis="X",
    )
    building_y = MDOF_ShearBuilding(
        num_stories, mass_per_floor=mass_per_floor, zeta=zeta,
        column_depth_x=column_depth_x, column_depth_y=column_depth_y,
        beam_depth=beam_depth, axis="Y",
    )

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
        "story_height": building_x.h,
        "npts": len(time_arr),
        "has_y": has_y,
        "reference_magnitude": reference_magnitude,
        "richter_magnitude": richter_magnitude,
        "epicenter_distance_km": epicenter_distance_km,
        "epicenter_depth_km": epicenter_depth_km,

        "elastic_modulus_Pa": building_x.E,
        "column_depth_x": column_depth_x,
        "column_depth_y": column_depth_y,
        "beam_depth": beam_depth,
        "beam_width": BEAM_WIDTH,
        "plan_span_x": PLAN_SPAN_X,
        "plan_span_y": PLAN_SPAN_Y,

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
    # [furn_y(...)] -- the 4-byte alignment established by the header pad
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

    return Response(b"".join(parts), mimetype="application/octet-stream")


@app.route("/")
def index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(PROJECT_ROOT, path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
