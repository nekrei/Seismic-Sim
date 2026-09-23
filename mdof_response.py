import numpy as np
from scipy.fft import fft, ifft, fftfreq, next_fast_len, rfft, irfft
from scipy.linalg import eigh as scipy_eigh
from scipy.signal import butter, filtfilt, decimate
import csv
import json
import re
import os
import sys
import copy
from collections import namedtuple
from time import perf_counter
from types import SimpleNamespace

# Constants
G_TO_MS2 = 9.80665

# --- Structural frame constants (spec 5, Part A) --------------------------
# Reinforced concrete elastic modulus. Fixed, not user-adjustable this spec
# (spec A2) -- a material-selection slider is a natural follow-up once the
# dimension-driven sliders below are verified working.
E_CONCRETE = 25e9  # Pa
# Perimeter beam width, uniform for both axes (only "beam depth" is a
# slider -- spec A2 keeps the beam cross-section's other dimension fixed
# to avoid ballooning the control surface).
BEAM_WIDTH = 0.5  # m
# Plan footprint of the idealized single-bay frame. Ratio 8.4/6.0 = 1.4
# matches index.html's existing 2.8x2.0 scene footprint (also ratio 1.4) --
# see specs/05-structural-frame-furniture.md Part A2 and the plan's
# constant-calibration section.
PLAN_SPAN_X = 8.4  # m
PLAN_SPAN_Y = 6.0  # m
# Exact ISO foot -> metre conversion (1 ft = 0.3048 m exactly), used only
# by the Area (sq ft) slider (spec 8) to convert its UI units to the
# metres plan_dims_from_area() works in.
SQM_PER_SQFT = 0.09290304
# Default Area slider value: the exact current footprint
# (PLAN_SPAN_X * PLAN_SPAN_Y) expressed in sq ft, so the slider's default
# position reproduces out/'s existing dimensions bit-for-bit (spec 8
# Part A2). Rounded to 6 decimals -- see the plan's ruling on why this is
# 542.501085, not the spec's rounded prose figure of 542.53. MUST match
# server.py's imported value and index.html's DEFAULT_AREA_SQFT literal
# exactly (comment at each of the other two sites, same convention as
# DEFAULT_COLUMN_DEPTH_X/Y).
DEFAULT_AREA_SQFT = round((PLAN_SPAN_X * PLAN_SPAN_Y) / SQM_PER_SQFT, 6)
# Each axis's condensed stiffness is built from ONE planar frame (2 corner
# columns + 1 beam) and then scaled by this factor to account for the
# second, identical parallel frame on the far side of the building's other
# plan dimension -- see assemble_frame_stiffness's docstring and spec A4.
N_PARALLEL_FRAMES = 2

# --- Cracked-section stiffness (spec 10, Part A) --------------------------
# Effective (cracked-section) stiffness multipliers on the GROSS second
# moments of area. Reinforced concrete cracks long before it yields, so the
# gross section overestimates stiffness substantially under seismic demand.
#
# NOT A CODE CITATION. "TO VERIFY" means exactly that. The research
# documents this project's collapse work is built on assert "0.35 I_g for
# columns and 0.5 I_g for beams per ASCE 41 / ACI 318"; that attribution
# does not survive checking (spec 10, A2):
#   ACI 318-19 Table 6.6.3.1.1(a)  0.70 columns / 0.35 beams  <- opposite ordering
#   ASCE 41-17 Table 10-5          0.3-0.7 E_c*I_g, axial-load interpolated
# Both of those are secondary-sourced; the primary standards are paywalled
# and were not read. 0.35/0.50 is this PROJECT's chosen default, kept
# because the user chose it, and labelled honestly rather than passed off
# as a code value. The code-sourced alternatives are reachable through
# SECTION_STIFFNESS_PRESETS without editing anything.
CRACKED_FACTOR_COLUMN = 0.35   # TO VERIFY -- project default, see spec 10 A2
CRACKED_FACTOR_BEAM = 0.50     # TO VERIFY -- project default, see spec 10 A2

# (column factor, beam factor). Selected by MDOF_ShearBuilding's
# `section_stiffness_mode`.
SECTION_STIFFNESS_PRESETS = {
    "gross":               (1.00, 1.00),  # pre-spec-10 behaviour, regression only
    "project-default":     (CRACKED_FACTOR_COLUMN, CRACKED_FACTOR_BEAM),
    "aci318-19":           (0.70, 0.35),  # TO VERIFY -- Table 6.6.3.1.1(a), secondary-sourced
    "asce41-17-low-axial": (0.30, 0.30),  # TO VERIFY -- Table 10-5 low-axial end, secondary-sourced
}
DEFAULT_SECTION_STIFFNESS_MODE = "project-default"

# Soft-ground-story preset (spec 10, C2): the ground story is this much
# taller than the rest, columns and beams unchanged. The Dhaka
# parking-level typology. Nothing about it is special-cased anywhere --
# the softness is an EMERGENT consequence of k ~ 1/h^3 and k_g = P/h, not
# a hand-applied weakening, which is exactly what verification check 7c
# asserts.
SOFT_STORY_HEIGHT_RATIO = 1.6   # TO VERIFY

# --- P-Delta / gravity (spec 10, Part B) ----------------------------------
# Standard gravity. Numerically the same constant as G_TO_MS2 above -- that
# one is a unit conversion (g -> m/s^2 for PEER records), this one is an
# actual acceleration entering the physics, so it gets its own name rather
# than overloading the converter's.
GRAVITY_MS2 = G_TO_MS2  # 9.80665 m/s^2

# Deflection amplification factor used in theta_demand. This model is not
# code-designed and has no C_d; inventing one would be a fabricated number,
# so it is exactly 1.0 and said so out loud (spec 10, B5).
C_D_DEFLECTION_AMPLIFICATION = 1.0

# ASCE 7-16 Sec 12.8.7 -- verified against multiple secondary readings of
# the clause (the primary standard is paywalled and was not read): theta
# <= 0.10 may be ignored; theta is capped at min(0.5/(beta*Cd), 0.25),
# above which the structure is to be redesigned. The widely-repeated
# "0.33 = instability" figure that both of this project's research
# documents quote is NOT in ASCE 7-16 -- it traces to a consulting blog
# post -- and is deliberately not used here.
THETA_IGNORE = 0.10
THETA_CODE_CAP = 0.25   # TO VERIFY against the primary standard

# --- RC material strengths and capacities (spec 10, Part D) ---------------
# Illustrative RC material properties. Representative, NOT a design
# specification -- this project has no rebar detailing model, and the
# capacities below carry a named simplification (see column_plastic_moment).
# Nothing in Part D affects the elastic solve; it exists so spec 11's
# backbones and spec 15's impact trigger read real capacities off the same
# geometry the stiffness already came from, instead of guessing.
F_Y_STEEL = 420e6          # Pa, Grade 60                        -- TO VERIFY
F_C_CONCRETE = 30e6        # Pa                                  -- TO VERIFY
RHO_LONGITUDINAL = 0.02    # column longitudinal steel ratio;
                           # ACI 318 permits 0.01-0.06           -- TO VERIFY
CONCRETE_COVER = 0.05      # m, to bar centroid                  -- TO VERIFY
PHI_AXIAL = 0.65           # ACI 318 strength reduction, tied    -- TO VERIFY
AXIAL_CAP_FACTOR = 0.80    # ACI 318 max axial, tied columns     -- TO VERIFY
# Furniture time-series are decimated toward this rate before being written
# to out/<record>/furniture_response.bin -- see furniture_decimation's
# docstring for why this is a legitimate sampling-theorem application, not
# just a size fudge.
FURNITURE_TARGET_RATE_HZ = 50.0

# --- Synthetic earthquake constants (spec 7, Part A) ----------------------
# Reference epicenter geometry: the "no reshaping" point where
# apply_synthetic_earthquake_scaling() must be the identity transform, so
# regenerating out/ (which never calls it) stays exactly reproducible.
# MUST match server.py's and index.html's DEFAULT_EPICENTER_DISTANCE_KM/
# DEFAULT_EPICENTER_DEPTH_KM exactly (comment at each of the other sites).
DEFAULT_EPICENTER_DISTANCE_KM = 20.0  # km
DEFAULT_EPICENTER_DEPTH_KM = 10.0     # km
# Anelastic (Q) attenuation constants -- typical crustal shear-wave Q and
# velocity, fixed (not user-adjustable, same reasoning as E_CONCRETE above).
ATTENUATION_Q = 200.0
ATTENUATION_VELOCITY_MPS = 3500.0

# Illustrative furniture-class parameters (spec Part B3) -- representative
# constants, not derived from any real furniture-stiffness database (none
# exists), freely adjustable for visual plausibility. Order matters: it
# fixes the class axis of out/<record>/furniture_response.bin.
FURNITURE_CLASSES = {
    "table": {"f_hz": 8.0, "zeta": 0.02},
    "chair": {"f_hz": 5.0, "zeta": 0.02},
    "fan":   {"f_hz": 3.0, "zeta": 0.02},
}


# --- Frame-assembly / static-condensation math (spec 5, Parts A3/A4) ------
# Kept as pure, module-level functions (not methods) for two reasons: the
# verification script needs to import and exercise them directly against
# the hand-derived closed form, and graphify shows them as distinct nodes
# in the dependency graph instead of burying them inside a class.

def column_inertia(b_x, b_y, axis):
    """
    Second moment of area of a rectangular column cross-section (width
    `b_x` along X, `b_y` along Y) about the bending axis relevant to sway
    in the given direction -- spec A2.

    Bending that resists X-direction sway happens about the column's
    Y-dimension face, so it depends on the X-depth cubed (I_x = b_y *
    b_x^3 / 12); resisting Y-sway is the transpose (I_y = b_x * b_y^3 /
    12). This is what makes the two axes genuinely anisotropic once
    b_x != b_y, instead of reusing one isotropic k for both, as the old
    abstract-spring model did.
    """
    if axis == "X":
        return b_y * b_x ** 3 / 12.0
    elif axis == "Y":
        return b_x * b_y ** 3 / 12.0
    else:
        raise ValueError(f"axis must be 'X' or 'Y', got {axis!r}")


def beam_inertia(depth, width=BEAM_WIDTH):
    """Second moment of area of a rectangular beam cross-section."""
    return width * depth ** 3 / 12.0


def frame_span(axis, plan_span_x, plan_span_y):
    """
    Beam span for the planar frame resisting sway in `axis`. The beam
    connecting the two columns of that frame runs parallel to the sway
    direction, so its span is the plan dimension in that same direction.
    """
    if axis == "X":
        return plan_span_x
    elif axis == "Y":
        return plan_span_y
    else:
        raise ValueError(f"axis must be 'X' or 'Y', got {axis!r}")


def plan_dims_from_area(area_sqm):
    """
    Aspect-ratio-preserving plan dimensions for a target footprint area
    (spec 8, Part A2). The current footprint's aspect ratio
    (PLAN_SPAN_X / PLAN_SPAN_Y = 1.4) is held fixed -- a single area
    slider can't specify two independent dimensions -- so:
        plan_span_y = sqrt(area_sqm / aspect)
        plan_span_x = aspect * plan_span_y
    """
    aspect = PLAN_SPAN_X / PLAN_SPAN_Y
    plan_span_y = np.sqrt(area_sqm / aspect)
    plan_span_x = aspect * plan_span_y
    return plan_span_x, plan_span_y


def apply_synthetic_earthquake_scaling(accel, dt, magnitude, distance_km,
                                        depth_km, reference_magnitude):
    """
    Reshape a real recorded ground acceleration trace into a synthetic
    earthquake at a different Richter magnitude and epicenter geometry, by
    multiplying its FFT spectrum by a real, non-negative, frequency-
    dependent scale factor (spec 7, Part A) -- no phase change, matching
    this project's existing zero-phase filtering convention.

    Three effects, all identity at (magnitude=reference_magnitude,
    distance_km=DEFAULT_EPICENTER_DISTANCE_KM,
    depth_km=DEFAULT_EPICENTER_DEPTH_KM):

    1. magnitude_scale = 10**(magnitude - reference_magnitude) -- the
       literal historical Richter definition (M = log10(A) - log10(A0)),
       applied directly rather than approximated.
    2. spreading_scale = R0 / R -- geometric spreading amplitude decay,
       where R = hypot(distance_km, depth_km) is hypocentral distance and
       R0 is the same hypocentral distance at the reference geometry.
    3. attenuation(f) = exp(-pi * f * max(R - R0, 0) * 1000 / (Q * v)) --
       anelastic attenuation, a genuine per-frequency-bin filter (removes
       high frequencies faster than low ones as R grows past R0), not a
       uniform amplitude scale in disguise. (R - R0) converted km -> m to
       match velocity in m/s. Clamped at R0 -- Q attenuation only ever
       removes energy the recorded trace still has; when R < R0 the
       un-clamped formula would run the exponential in reverse and
       "restore" high-frequency energy the reference recording never had
       in the first place, blowing up without bound as R -> 0. Physically
       there's nothing to restore, so the correct value for R <= R0 is no
       attenuation adjustment at all (factor 1), not amplification.
    """
    n = len(accel)
    freqs = np.fft.rfftfreq(n, dt)

    R0 = np.hypot(DEFAULT_EPICENTER_DISTANCE_KM, DEFAULT_EPICENTER_DEPTH_KM)
    R = np.hypot(distance_km, depth_km)

    magnitude_scale = 10.0 ** (magnitude - reference_magnitude)
    spreading_scale = R0 / R
    attenuation = np.exp(
        -np.pi * freqs * max(R - R0, 0.0) * 1000.0
        / (ATTENUATION_Q * ATTENUATION_VELOCITY_MPS)
    )
    scale = magnitude_scale * spreading_scale * attenuation

    spectrum = np.fft.rfft(accel)
    return np.fft.irfft(spectrum * scale, n=n)


def assemble_frame_stiffness(N, E, I_c, I_b, h, L):
    """
    Assemble the 2N x 2N stiffness matrix for ONE planar frame (N stories,
    single bay, fixed at the base, free at the roof) with DOF ordering
    [Delta_1..Delta_N, theta_1..theta_N]. Delta_i (index i-1) is floor i's
    lateral sway DOF -- the one the rest of the pipeline already expects.
    theta_i (index N+i-1) is floor i's shared beam-column joint rotation:
    both columns at a level rotate together by the frame's left-right
    symmetry (spec A4), so there is only one theta per floor, not two.

    Column element for story i (bottom joint i-1, top joint i), derived
    via slope-deflection (spec A3), with c = 2*E*I_c. The factor of 2
    already accounts for the two columns of THIS one frame sharing the
    same Delta_i/theta_i DOFs -- a standard 4x4 Euler-Bernoulli
    beam-column stiffness matrix, added twice. (The *second*, physically
    parallel frame on the far side of the building -- 4 corner columns
    total -- is handled separately by N_PARALLEL_FRAMES in
    build_condensed_K, not here.) Joint 0 is the fixed base, so any term
    that would touch it is dropped -- story 1's column only contributes
    its "top joint" (floor 1) terms.

    Beam at floor i (span L, EI_b) contributes only to theta_i,theta_i
    (+12*E*I_b/L) -- with the beam assumed axially rigid (both ends move
    together laterally, per spec A3), its only effect on this reduced DOF
    set is rotational restraint at floor i.

    `I_c`, `I_b` and `h` are per-story (spec 10, C1): each may be a scalar
    (broadcast to every story, exactly as before) or a length-N sequence,
    indexed by story. This is what lets damage localise and what makes a
    soft ground story expressible at all. `L` stays scalar -- it is a plan
    dimension, shared by every story of the frame.

    Each story's values are pulled out as plain Python floats before the
    arithmetic, so a uniform per-story array evaluates the *same* expression
    tree as the old scalar code and reproduces it bit-for-bit, not merely
    to within a tolerance (verification check 1).
    """
    size = 2 * N
    K = np.zeros((size, size))

    def per_story(v, name):
        arr = np.broadcast_to(np.asarray(v, dtype=float), (N,)).copy()
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"{name} must be finite per story, got {v!r}")
        return arr

    I_c_arr = per_story(I_c, "I_c")
    I_b_arr = per_story(I_b, "I_b")
    h_arr = per_story(h, "h")

    def dof(i):
        """(delta_index, theta_index) for floor i; both None for the
        fixed base (i == 0)."""
        if i == 0:
            return None, None
        return i - 1, N + i - 1

    def add(row, col, val):
        if row is None or col is None:
            return
        K[row, col] += val

    for i in range(1, N + 1):
        d_bot, t_bot = dof(i - 1)
        d_top, t_top = dof(i)

        # This story's own section/geometry. Story i spans joints i-1..i,
        # so it takes I_c[i-1] and h[i-1]; the beam AT floor i takes
        # I_b[i-1].
        c = 2.0 * E * float(I_c_arr[i - 1])
        h = float(h_arr[i - 1])

        # Lateral-lateral (shear) terms
        add(d_bot, d_bot, 12 * c / h ** 3)
        add(d_top, d_top, 12 * c / h ** 3)
        add(d_bot, d_top, -12 * c / h ** 3)
        add(d_top, d_bot, -12 * c / h ** 3)

        # Rotation-rotation terms
        add(t_bot, t_bot, 4 * c / h)
        add(t_top, t_top, 4 * c / h)
        add(t_bot, t_top, 2 * c / h)
        add(t_top, t_bot, 2 * c / h)

        # Lateral-rotation coupling terms
        add(d_bot, t_bot, 6 * c / h ** 2)
        add(t_bot, d_bot, 6 * c / h ** 2)
        add(d_bot, t_top, 6 * c / h ** 2)
        add(t_top, d_bot, 6 * c / h ** 2)
        add(d_top, t_top, -6 * c / h ** 2)
        add(t_top, d_top, -6 * c / h ** 2)
        add(t_bot, d_top, -6 * c / h ** 2)
        add(d_top, t_bot, -6 * c / h ** 2)

        # Beam at floor i
        _, t_i = dof(i)
        add(t_i, t_i, 12 * E * float(I_b_arr[i - 1]) / L)

    return K


def condense_rotations(K_full, N):
    """
    Static (Guyan/Schur-complement) condensation of the joint-rotation
    DOFs out of the 2N x 2N frame stiffness matrix, leaving the N x N
    matrix in terms of the lateral sway DOFs only -- the same shape
    _modal_analysis() already expects (spec A4):
    K_condensed = K_dd - K_dt @ inv(K_tt) @ K_td.

    Solved via np.linalg.solve rather than an explicit matrix inverse
    (faster and more numerically stable), then symmetrized to kill the
    tiny floating-point asymmetry the solve can introduce.
    """
    K_dd = K_full[:N, :N]
    K_dt = K_full[:N, N:]
    K_td = K_full[N:, :N]
    K_tt = K_full[N:, N:]

    X = np.linalg.solve(K_tt, K_td)
    K_cond = K_dd - K_dt @ X
    return 0.5 * (K_cond + K_cond.T)


def build_condensed_K(N, E, I_c, I_b, h, L):
    """
    Assemble one planar frame, condense out its joint rotations, and scale
    by N_PARALLEL_FRAMES (=2) to account for the second, identical planar
    frame on the far side of the building's other plan dimension -- 4
    corner columns total, 2 per frame. Condensing one frame and doubling
    is exactly equivalent to condensing both together (they're identical
    and uncoupled), so the assembly stays scoped to a single frame -- this
    is also what verify_frame_furniture.py compares against the closed
    form, which is itself a single-frame (2-column) result.

    `I_c`, `I_b` and `h` may each be scalar or per-story -- see
    assemble_frame_stiffness.
    """
    K_full = assemble_frame_stiffness(N, E, I_c, I_b, h, L)
    K_one_frame = condense_rotations(K_full, N)
    return N_PARALLEL_FRAMES * K_one_frame


def column_section_for_axis(b_x, b_y, axis):
    """(width b, bending depth) of a column resisting sway in `axis`.

    Reuses column_inertia's convention exactly: resisting X-sway bends
    about the Y-dimension face, so the bending depth is b_x and the width
    is b_y; Y is the transpose. Kept as one function so the capacity
    formulas below cannot drift from the stiffness formulas above.
    """
    if axis == "X":
        return b_y, b_x
    elif axis == "Y":
        return b_x, b_y
    raise ValueError(f"axis must be 'X' or 'Y', got {axis!r}")


def column_plastic_moment(b_x, b_y, axis, f_y=F_Y_STEEL, f_c=F_C_CONCRETE,
                          rho=RHO_LONGITUDINAL, cover=CONCRETE_COVER):
    """
    Plastic moment capacity of ONE column, bending about the axis relevant
    to sway in `axis` (spec 10, D2):

        d   = depth_bending - cover
        A_s = rho * b * d
        a   = A_s*f_y / (0.85*f_c*b)
        M_p = A_s*f_y*(d - a/2)

    **Named simplification: this is a singly-reinforced FLEXURAL capacity
    and ignores axial load entirely.** Real columns carry large axial
    force, which raises moment capacity up to the balance point and
    reduces it above -- the P-M interaction diagram. Getting that right
    needs a section analysis this project does not have.

    Upgrade path: a simplified P-M interaction using the same `P_i`
    geometric_stiffness_matrix() already computes. Until then M_p must not
    be presented as *the* column capacity without this caveat attached --
    see knowledge/mdof_response.md and the math PDF, which carry it too.

    Scalar or per-story arrays both work (b_x/b_y may be arrays).
    """
    b, depth = column_section_for_axis(b_x, b_y, axis)
    d = depth - cover
    A_s = rho * b * d
    a = A_s * f_y / (0.85 * f_c * b)
    return A_s * f_y * (d - a / 2)


def story_plastic_shear(b_x, b_y, axis, h, **kwargs):
    """
    Story plastic shear capacity (spec 10, D3):

        V_p,i = sum_j (M_p,ij^top + M_p,ij^bot) / h_i

    over the 2*N_PARALLEL_FRAMES = 4 corner columns, each hinging at top
    and bottom. With identical columns and identical top/bottom detailing
    that collapses to 8*M_p/h_i -- the summation form is kept anyway
    because spec 11 makes the columns differ per story.

    Inherits column_plastic_moment's axial-load simplification.
    """
    n_columns = 2 * N_PARALLEL_FRAMES
    M_p = column_plastic_moment(b_x, b_y, axis, **kwargs)
    return n_columns * (M_p + M_p) / np.asarray(h, dtype=float)


def column_axial_capacity(b_x, b_y, f_y=F_Y_STEEL, f_c=F_C_CONCRETE,
                          rho=RHO_LONGITUDINAL, phi=PHI_AXIAL,
                          cap_factor=AXIAL_CAP_FACTOR):
    """
    Total axial capacity of a story's 2*N_PARALLEL_FRAMES corner columns
    (spec 10, D4):

        A_g      = b_x*b_y,   A_st = rho*A_g
        P_cap,ij = cap_factor*phi*[0.85*f_c*(A_g - A_st) + f_y*A_st]
        P_cap,i  = sum_j P_cap,ij

    Spec 15's pancake cascade compares an assumed impact force against
    this. Exported now so that comparison is against a number derived from
    the same section the stiffness came from, rather than a fresh
    invention at animation time.
    """
    A_g = b_x * b_y
    A_st = rho * A_g
    per_column = cap_factor * phi * (
        0.85 * f_c * (A_g - A_st) + f_y * A_st)
    return 2 * N_PARALLEL_FRAMES * per_column


class GravityInstabilityError(Exception):
    """
    Raised when gravity overwhelms lateral stiffness -- the building cannot
    stand up under its own weight at these parameters (spec 10, B3).

    This is a *physically meaningful result*, not a crash, and it exists
    because the alternative is much worse than an exception:
    `scipy.linalg.eigh(K, M)` requires **M** positive definite, not K, and
    M always is. So an indefinite K_L returns a NEGATIVE eigenvalue,
    `np.sqrt` turns it into `nan`, and that `nan` propagates silently
    through `settle_time = 5/(zeta*omega_1)`, the entire FFT solve, and
    into the JSON header -- where `json.dumps` emits a bare `NaN` token
    that `JSON.parse` rejects, surfacing in the browser as a parse error a
    very long way from its cause.

    `story` is the 0-indexed story that gave way first (largest
    k_g,i / k0,i), so callers can say *where*, not just *that*.
    """

    def __init__(self, message, story=None):
        super().__init__(message)
        self.story = story


def geometric_stiffness_matrix(N, m_per_floor, h, g=GRAVITY_MS2):
    """
    Linearised P-Delta geometric stiffness matrix (spec 10, B1).

    Gravity acting through lateral drift produces an overturning moment
    that first-order analysis misses. Per story:

        k_g,i = P_i / h_i,      P_i = g * sum_{j >= i} m_j

    P_i is the gravity load story i's columns carry -- every floor mass at
    or above it. With uniform mass that makes k_g largest at the BASE,
    which is not incidental: it is why the lower stories go unstable first,
    and it is the mechanism the whole collapse roadmap rests on.

    Assembled with shear-building topology (story i contributes
    k_g,i * b_i b_i^T with b_i = e_i - e_{i-1}, e_0 dropped at the fixed
    base):

        K_G[i,i]   = k_g,i + k_g,i+1        (k_g,N+1 == 0)
        K_G[i,i+1] = K_G[i+1,i] = -k_g,i+1

    **K_G is tridiagonal even though the condensed K is not, and that is
    correct, not an inconsistency.** K is full because joint rotations were
    condensed out of a frame; K_G is tridiagonal because the P-Delta moment
    depends only on each story's *relative* drift. Do not "fix" the
    topology mismatch.

    `m_per_floor` and `h` may be scalar or per-story. Returns
    (K_G, k_g, P) so callers do not have to re-derive k_g from the matrix.
    """
    m_arr = np.broadcast_to(np.asarray(m_per_floor, dtype=float), (N,)).copy()
    h_arr = np.broadcast_to(np.asarray(h, dtype=float), (N,)).copy()

    # P_i = g * sum of every floor mass at or above story i.
    P = g * np.cumsum(m_arr[::-1])[::-1]
    k_g = P / h_arr

    return _relative_drift_matrix(k_g), k_g, P


def _relative_drift_matrix(k):
    """Shear-building (relative-drift) assembly of per-story springs `k`.

    Story i contributes k_i * b_i b_i^T with b_i = e_i - e_{i-1} and e_0
    dropped at the fixed base:

        K[i,i]   = k_i + k_{i+1}     (k_{N+1} == 0)
        K[i,i+1] = K[i+1,i] = -k_{i+1}

    Factored out because spec 12's rotational geometric stiffness uses the
    *same* topology on the theta block with a different spring -- one
    assembly, two callers, rather than a copy that can drift. The
    arithmetic is expression-for-expression what geometric_stiffness_matrix
    did inline before spec 12, so its output is unchanged bit for bit.
    """
    N = len(k)
    K = np.zeros((N, N))
    for i in range(N):
        k_above = k[i + 1] if i + 1 < N else 0.0
        K[i, i] = k[i] + k_above
        if i + 1 < N:
            K[i, i + 1] = -k[i + 1]
            K[i + 1, i] = -k[i + 1]
    return K


# --- Spec 12: the 3N torsional kernel -------------------------------------
#
# DOF ordering, stated once and never varied:  [u_x,1..N, u_y,1..N, theta_1..N]
#
# A silent ordering mismatch between the assembler, the pseudo-force
# assembly and the payload writer is the most likely bug in this spec, so
# every function below indexes through that one ordering and nothing
# re-derives it.

def column_plan_positions(plan_span_x, plan_span_y):
    """Plan positions (x_j, y_j) of the four corner columns, j = 0..3.

    **This pins column index `j` to a physical plan position, once, for the
    whole project.** Before spec 12 `j` was an anonymous slot in spec 11's
    (N, 4) per-column arrays; it now has a location, and the strength
    scatter seeded `f'{record}#{axis}#{i}#{j}'` acquires a geometric
    meaning it did not have. That seed string is deliberately left
    byte-for-byte unchanged -- changing it would break spec 11's
    determinism checks and column_strength_scatter's bit-match with the
    viewer's FNV/mulberry32 (spec correction C-1 item 6).

    Counter-clockwise from the +x,+y corner, with a = plan_span_x along X
    and b = plan_span_y along Y:

        j = 0 -> (+a/2, +b/2)
        j = 1 -> (-a/2, +b/2)
        j = 2 -> (-a/2, -b/2)
        j = 3 -> (+a/2, -b/2)

    Sign convention for the whole spec, fixed here: a right-handed frame
    with theta measured about +Z, so a floor rotation theta displaces a
    point at (x, y) by (-y*theta, +x*theta). Verification check 2 asserts
    both this table and that convention.
    """
    a = float(plan_span_x) / 2.0
    b = float(plan_span_y) / 2.0
    return np.array([[+a, +b], [-a, +b], [-a, -b], [+a, -b]])


def frame_transform(N, offset, kind):
    """The N x 3N kinematic transform T_f mapping floor DOFs to one planar
    frame's in-plane drift (spec 12, A1).

    Following column_plan_positions' right-handed convention:

        frame resisting X-sway at plan offset y_f:  d_i = u_x,i - y_f*theta_i
        frame resisting Y-sway at plan offset x_f:  d_i = u_y,i + x_f*theta_i

    `kind` is "X" or "Y" -- the sway direction the frame resists, not the
    axis its offset is measured along.
    """
    T = np.zeros((N, 3 * N))
    eye = np.eye(N)
    if kind == "X":
        T[:, :N] = eye
        T[:, 2 * N:] = -float(offset) * eye
    elif kind == "Y":
        T[:, N:2 * N] = eye
        T[:, 2 * N:] = +float(offset) * eye
    else:
        raise ValueError(f"kind must be 'X' or 'Y', got {kind!r}")
    return T


def building_frames(plan_span_x, plan_span_y):
    """The four planar frames as (offset, kind) pairs -- two resisting
    X-sway at y = +-b/2, two resisting Y-sway at x = +-a/2.

    This is spec 5's N_PARALLEL_FRAMES structure *placed* rather than
    merely counted. build_condensed_K's factor of N_PARALLEL_FRAMES is
    therefore removed here and replaced by explicit placement -- applying
    both would double-count every frame.
    """
    a = float(plan_span_x) / 2.0
    b = float(plan_span_y) / 2.0
    return ((+b, "X"), (-b, "X"), (+a, "Y"), (-a, "Y"))


def assemble_K3N(N, E, I_c_x, I_c_y, I_b, h, plan_span_x, plan_span_y):
    """K_3N = sum_f T_f^T K_f T_f over the four placed planar frames.

    `K_f` is ONE frame's condensed N x N matrix -- assemble_frame_stiffness
    followed by condense_rotations, i.e. build_condensed_K *without* its
    N_PARALLEL_FRAMES factor (see building_frames).

    Do NOT substitute the research document's scalar per-column spring
    formulas (spec A1). They assume each column is an independent lateral
    spring, which throws away the beams' rotational restraint that spec 5's
    condensation bakes into a full N x N matrix.

    This reduces to the per-axis result **bit-identically** for a symmetric
    plan, which is verification check 1 and the gate on the whole spec:
    the u_x block sums to K_f + K_f and IEEE-754 gives x + x == 2*x
    exactly; the u_x-theta block sums to (-b/2)K_f + (+b/2)K_f, two exact
    negations, so it is 0.0 exactly rather than merely small.
    """
    K_x = condense_rotations(
        assemble_frame_stiffness(N, E, I_c_x, I_b, h,
                                 frame_span("X", plan_span_x, plan_span_y)), N)
    K_y = condense_rotations(
        assemble_frame_stiffness(N, E, I_c_y, I_b, h,
                                 frame_span("Y", plan_span_x, plan_span_y)), N)

    K3 = np.zeros((3 * N, 3 * N))
    for offset, kind in building_frames(plan_span_x, plan_span_y):
        T = frame_transform(N, offset, kind)
        K3 += T.T @ (K_x if kind == "X" else K_y) @ T
    return K3


def geometric_stiffness_3N(N, m_per_floor, h, plan_span_x, plan_span_y,
                           g=GRAVITY_MS2):
    """Linearised P-Delta geometric stiffness for the 3N system.

    The two lateral blocks are geometric_stiffness_matrix()'s output
    **verbatim**, and that is load-bearing rather than a convenience: the
    per-column placement sum computes 4*((P_i/4)/h_i) where the existing
    code computes P_i/h_i, and those are not bit-identical in IEEE-754.
    Verification check 1 demands bit-identity with p_delta on.

    The rotational block uses the same relative-drift topology with

        k_gtheta,i = (1/h_i) * sum_j P_ij (x_j^2 + y_j^2)
                   = (P_i/h_i) * (a^2 + b^2)/4

    for four equal corner columns. **This is NOT the (a^2+b^2)/12 of the
    rotational mass**, and the difference is not a typo in either place.
    Mass is the distributed floor plate, so its radius of gyration is the
    plate's. The destabilising gravity torque acts through the axial load,
    and in this model the axial load sits in the four corner columns at
    (+-a/2, +-b/2) -- the same assumption column_axial_capacity() already
    makes. The corner radius is a factor of 3 larger, which makes torsional
    gravity instability *more* likely, not less. Spec A4 originally wrote
    /12 here; spec correction C-1 item 2 records the derivation and
    verification check 3 re-derives it independently.

    Returns (K_G3, k_g, P) with `k_g`/`P` the per-story lateral quantities,
    unchanged in meaning from geometric_stiffness_matrix.
    """
    K_G_lat, k_g, P = geometric_stiffness_matrix(N, m_per_floor, h, g=g)

    pos = column_plan_positions(plan_span_x, plan_span_y)
    r_sq = float(np.mean(pos[:, 0] ** 2 + pos[:, 1] ** 2))
    K_G_rot = _relative_drift_matrix(k_g * r_sq)

    K_G3 = np.zeros((3 * N, 3 * N))
    K_G3[:N, :N] = K_G_lat
    K_G3[N:2 * N, N:2 * N] = K_G_lat
    K_G3[2 * N:, 2 * N:] = K_G_rot
    return K_G3, k_g, P


def mass_matrix_3N(N, m_per_floor, plan_span_x, plan_span_y):
    """diag(m_i, m_i, I_m,i) per floor, with I_m,i = m_i(a^2 + b^2)/12.

    The rigid diaphragm is a uniformly distributed plate of plan a x b, so
    the rotational inertia keeps the plate's radius of gyration -- unlike
    the gravity torque in geometric_stiffness_3N, which acts through the
    corner columns. Both radii are correct; see that docstring.

    `a`, `b` come from spec 8's plan_span_x/y, so I_m tracks the Area
    slider for free.
    """
    m = np.broadcast_to(np.asarray(m_per_floor, dtype=float), (N,))
    I_m = m * (float(plan_span_x) ** 2 + float(plan_span_y) ** 2) / 12.0
    return np.diag(np.concatenate([m, m, I_m]))


def story_stiffness_profile(K_L, M, phi1):
    """
    Per-story lateral stiffness k0,i via a first-mode pushover
    (spec 10, B4):

        s     = M @ phi1          (first-mode-proportional lateral pattern)
        K_L u = s                 (static solve)
        delta_i = u_i - u_{i-1}   (story drift, u_0 = 0)
        V_i     = sum_{j >= i} s_j
        k0,i    = V_i / delta_i

    Downstream this is "the stiffness of story i" everywhere -- the
    stability coefficient below, spec 11's backbone anchors, spec 14's
    colouring, spec 18's drift-based sizing. The condensed K cannot supply
    one: it is full, and its diagonal is not a story spring.

    **This is an approximation for a frame** -- flexural coupling means no
    exact story-spring decomposition exists -- but it has an exact limiting
    case that makes it checkable: at the rigid-beam limit (rho -> infinity)
    the condensed K becomes exactly a shear-building matrix, and then
    V_i/delta_i reproduces its story springs for ANY load pattern, to
    floating-point precision. That is verification check 5.

    Sign-invariant: eigh may return phi1 with either sign, and s, u, delta
    and V all flip together, so k0 does not.

    `MDOF_ShearBuilding.story_stiffness` (K[0,0], spec 5 metadata) is NOT
    this and must not be repurposed.
    """
    s = M @ phi1
    u = np.linalg.solve(K_L, s)
    delta = np.diff(np.concatenate(([0.0], u)))
    V = np.cumsum(s[::-1])[::-1]
    return V / delta


def frame_story_stiffness_closed_form(E, I_c, I_b, h, L):
    """
    A3's hand-derived single-story closed form for ONE planar frame (2
    columns + 1 beam):
        k_story = (12*E*I_c/h^3) * (1 + 6*rho) / (2 + 3*rho)
        rho = (E*I_b/L) / (E*I_c/h)
    Used ONLY by verification (claude_scripts/verify_frame_furniture.py)
    to check assemble_frame_stiffness/condense_rotations at N=1 -- never
    called from the production build_condensed_K path, to keep the check
    independent of the code it's checking.
    """
    rho = (E * I_b / L) / (E * I_c / h)
    return (12 * E * I_c / h ** 3) * (1 + 6 * rho) / (2 + 3 * rho)


# --- Furniture secondary-system frequency response (spec 5, Part B) -------

def furniture_frf(omega, f_Hz, zeta):
    """
    Frequency-response function of a single-DOF furniture oscillator
    riding on its floor (spec Part B1/B2). Relative displacement
    u = x_f - x_b (furniture minus its floor) under floor excitation
    obeys m*u'' + c*u' + k*u = -m*x_b'', the exact same equation FORM as
    the primary structure's own governing equation -- the floor stands in
    for the ground, the furniture item stands in for the building.

    H(jw) = 1 / (wf^2 - w^2 + j*2*zeta*wf*w)

    equivalently, in Laplace form before evaluating on the imaginary axis
    s = j*omega:
        H(s) = 1 / (s^2 + 2*zeta*wf*s + wf^2)
    with poles at s = -zeta*wf +/- j*wf*sqrt(1 - zeta^2) -- directly
    analogous to the primary structure's own modal poles used in
    compute_response().
    """
    wf = 2 * np.pi * f_Hz
    return 1.0 / (wf ** 2 - omega ** 2 + 1j * 2 * zeta * wf * omega)


def furniture_decimation(dt, target_rate=FURNITURE_TARGET_RATE_HZ):
    """
    Integer decimation factor to bring a furniture time-series (native
    rate 1/dt) down toward `target_rate` Hz.

    This is an honest sampling-theorem application, not a size fudge:
    |H(jw)| (furniture_frf) rolls off ~1/w^2 above the furniture's own
    natural frequency, so the furniture response is genuinely band-limited
    -- resampling it with an anti-aliasing filter loses nothing that
    matters (see specs/COURSE-CONCEPTS.md). Reused for both the offline
    out/<record>/furniture_response.bin artifact and the live /compute
    payload.
    """
    native_rate = 1.0 / dt
    return max(1, int(round(native_rate / target_rate)))


def decimate_furniture(u, q):
    """
    Anti-aliased decimation of a furniture response array along its last
    (time) axis. FIR + zero-phase (not the default IIR) so the decimated
    furniture sway isn't phase-shifted relative to the primary structure's
    own already-computed, un-decimated response -- a phase mismatch here
    would show up as furniture visibly leading or lagging its floor by a
    fixed offset instead of oscillating around it.
    """
    if q <= 1:
        return u
    return decimate(u, q, ftype='fir', zero_phase=True, axis=-1)


def get_orientation_from_filename(filename):
    """
    Parse the PEER filename to determine the physical orientation of the component.
    Returns azimuth in degrees (0=N, 90=E, etc.) or None if vertical/unknown.
    Handles both .AT2 and .DT2 files.
    """
    fname = filename.upper()
    
    # ---------- VERTICAL INDICATORS ----------
    # PEER vertical components: UP, UD, VT2, or HLZ
    if any(x in fname for x in ['UP', 'UD', 'VT2']):
        return None
    if 'HLZ' in fname:
        return None
    # If filename ends with Z before extension (e.g., ...Z.AT2 or ...Z.DT2)
    if re.search(r'Z\.(AT2|DT2)$', fname):
        return None
    
    # ---------- NUMERIC AZIMUTHS ----------
    # 3-digit azimuth (e.g., 000, 090, 180, 270) followed by .AT2 or .DT2
    match = re.search(r'(\d{3})\.(AT2|DT2)', fname)
    if match:
        return int(match.group(1))
    # 2-digit azimuth (e.g., 90, 180) followed by .AT2 or .DT2
    match = re.search(r'(\d{2})\.(AT2|DT2)', fname)
    if match:
        val = int(match.group(1))
        if val in [0, 90, 180, 270, 360]:
            return 0 if val == 360 else val
    
    # ---------- CARDINAL / COMMON NAMES ----------
    if 'NS' in fname:     return 0
    if 'EW' in fname:     return 90
    if 'HLN' in fname:    return 0
    if 'HLE' in fname:    return 90
    if 'XTE' in fname:    return 90   # L'Aquila East
    if 'YLN' in fname:    return 0    # L'Aquila North
    
    # ---------- FINAL FALLBACK (only for known patterns) ----------
    # If we still can't determine, log and return None
    print(f"Warning: Could not parse orientation for {filename}. Skipping.")
    return None


class ComponentPairingError(ValueError):
    """The two horizontal components cannot drive one coupled 3N solve.

    Raised rather than patched over: spec 12 B5 makes component pairing a
    correctness issue, because a swapped or mis-timed pairing produces a
    plausible-looking but wrong twist. Every caller's response is the same
    -- fall back to the per-axis elastic path for that record.
    """


def assign_component_axes(orients):
    """(x_orient, y_orient) from a record's horizontal azimuths.

    X is the LOWER azimuth. That is a heuristic, not a compass direction
    (spec correction C-4): it is the sort that decides, not the filename
    parser, so the assignment is recorded in the header by
    `pair_components` and stays detectable after the fact.
    """
    ordered = sorted(orients)
    if len(ordered) < 2:
        raise ComponentPairingError(
            f"a record needs two horizontal components, got {ordered}")
    return ordered[0], ordered[1]


class PairedComponents:
    """Two horizontal components on one time base, plus what was done."""

    __slots__ = ("accel_x", "disp_x", "accel_y", "disp_y", "dt",
                 "npts", "pad_x", "pad_y", "header")

    def __init__(self, accel_x, disp_x, accel_y, disp_y, dt,
                 npts, pad_x, pad_y, header):
        self.accel_x = accel_x
        self.disp_x = disp_x
        self.accel_y = accel_y
        self.disp_y = disp_y
        self.dt = dt
        self.npts = npts
        self.pad_x = pad_x
        self.pad_y = pad_y
        self.header = header


def pair_components(accel_x, disp_x, dt_x, accel_y, disp_y, dt_y,
                    x_orient=None, y_orient=None, label=""):
    """Align a record's two horizontal components for one 3N solve.

    The two `.AT2` files are simultaneous recordings of the same event, so
    they are aligned on sample index 0 and zero-**padded** to a common
    length -- never resampled, which would shift phase and quietly
    invent a different earthquake. Spec 12 B5, verification check 8.

    Raises `ComponentPairingError` (caller falls back to the per-axis
    path) when there is no Y component, when either orientation could not
    be assigned, or when the two components were sampled at different
    rates. That last guard is new: `load_component` returned each
    component's own `dt` and the offline caller discarded Y's, so a
    mismatched pair would have been solved as though it were simultaneous
    (spec correction C-4). Latent while every shipped record has one
    shared `dt`; a correctness defect the moment both components drive one
    solve.
    """
    tag = f"{label}: " if label else ""
    if accel_y is None or disp_y is None:
        raise ComponentPairingError(
            f"{tag}no Y component -- rejected for torsion, use the per-axis "
            f"path")
    if x_orient is None or y_orient is None:
        raise ComponentPairingError(
            f"{tag}orientation could not be assigned (x={x_orient}, "
            f"y={y_orient}) -- rejected for torsion rather than guessed")
    if dt_y != dt_x:
        raise ComponentPairingError(
            f"{tag}the two components were sampled at different rates "
            f"(dt_x={dt_x}, dt_y={dt_y}); they cannot be treated as "
            f"simultaneous -- rejected for torsion")

    arrays = [np.asarray(a, dtype=float)
              for a in (accel_x, disp_x, accel_y, disp_y)]
    npts = max(len(a) for a in arrays)

    def padded(a):
        if len(a) == npts:
            return a
        out = np.zeros(npts)
        out[:len(a)] = a
        return out

    pad_x = npts - min(len(arrays[0]), len(arrays[1]))
    pad_y = npts - min(len(arrays[2]), len(arrays[3]))
    if pad_x or pad_y:
        print(f"  {tag}component length mismatch -- zero-padding X by "
              f"{pad_x} and Y by {pad_y} samples to {npts} (not resampled).")

    return PairedComponents(
        *[padded(a) for a in arrays], dt_x, npts, pad_x, pad_y,
        {"x_orient_deg": x_orient, "y_orient_deg": y_orient,
         "component_pad_x": pad_x, "component_pad_y": pad_y})


def parse_peer_file(filename, convert_to_ms2=True):
    """Parse a PEER .AT2 acceleration file. Returns (acceleration, dt) in m/s²."""
    with open(filename, 'r') as f:
        lines = f.readlines()

    dt = None
    npts = None
    data_start = 0
    unit_is_g = False

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if 'dt=' in line_lower:
            match = re.search(r'dt\s*=\s*([0-9.]+)', line_lower)
            if match:
                dt = float(match.group(1))
        if 'npts=' in line_lower:
            match = re.search(r'npts\s*=\s*([0-9]+)', line_lower)
            if match:
                npts = int(match.group(1))
        if 'units of g' in line_lower or 'in units of g' in line_lower:
            unit_is_g = True
        try:
            float(line.split()[0])
            data_start = i
            break
        except (ValueError, IndexError):
            continue

    accel_vals = []
    for line in lines[data_start:]:
        parts = line.split()
        for part in parts:
            try:
                val = float(part)
                accel_vals.append(val)
            except ValueError:
                pass

    accel = np.array(accel_vals)
    if npts is not None and len(accel) != npts:
        print(f"Warning: Expected {npts} points, found {len(accel)}. Using found length.")
    if dt is None:
        raise ValueError("Could not find DT in file header. Please specify dt manually.")

    if convert_to_ms2 and unit_is_g:
        accel = accel * G_TO_MS2
        print(f"Converted acceleration from g to m/s² (multiplied by {G_TO_MS2})")
    elif convert_to_ms2 and not unit_is_g:
        print("Warning: File does not explicitly state units. Assuming g and converting anyway.")
        accel = accel * G_TO_MS2

    return accel, dt

def parse_peer_displacement_file(filename):
    """Parse a PEER .dt2 displacement file. Returns (displacement, dt) in meters."""
    with open(filename, 'r') as f:
        lines = f.readlines()

    dt = None
    npts = None
    data_start = 0
    unit_is_cm = False

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if 'dt=' in line_lower:
            match = re.search(r'dt\s*=\s*([0-9.]+)', line_lower)
            if match:
                dt = float(match.group(1))
        if 'npts=' in line_lower:
            match = re.search(r'npts\s*=\s*([0-9]+)', line_lower)
            if match:
                npts = int(match.group(1))
        if 'units of cm' in line_lower:
            unit_is_cm = True
        try:
            float(line.split()[0])
            data_start = i
            break
        except (ValueError, IndexError):
            continue

    disp_vals = []
    for line in lines[data_start:]:
        parts = line.split()
        for part in parts:
            try:
                val = float(part)
                disp_vals.append(val)
            except ValueError:
                pass

    disp = np.array(disp_vals)
    if npts is not None and len(disp) != npts:
        print(f"Warning: Expected {npts} points, found {len(disp)}. Using found length.")
    if dt is None:
        raise ValueError("Could not find DT in file header. Please specify dt manually.")

    if unit_is_cm:
        disp = disp / 100.0
        print(f"Converted displacement from cm to meters (divided by 100)")
    else:
        print("Warning: File does not explicitly state units. Assuming cm and converting anyway.")
        disp = disp / 100.0

    return disp, dt

#-----helper functions end here--------

# --- Nonlinear constitutive model (spec 11) -----------------------------
# These are modelling assumptions, not calibrated component properties.
HFTD_DEFAULTS = dict(
    backbone_hardening=0.03, backbone_softening=0.30,
    backbone_mu_cap=4.0, backbone_mu_ult=8.0,
    backbone_residual_frac=0.05, unload_degrade_exp=0.3,
    pinch_drift_frac=0.25, pinch_force_frac=0.25,
    column_strength_cov=0.10, hftd_relaxation=0.4,
    hftd_tolerance=1e-4, hftd_max_iterations=40,
    hftd_segment_seconds=None, drift_limit_cp=0.04,
    collapse_mu_cap=8.0, intensity_scale=1.0)


def column_strength_scatter(record, axis, N, ncol=4, cov=0.10):
    """Mean-one lognormal assumption, using the viewer's FNV/mulberry32.

    `cov` is the actual coefficient of variation, hence log-sigma is
    sqrt(log(1+cov**2)). UTF-16 code units match JavaScript charCodeAt.
    """
    if not np.isfinite(cov) or cov < 0:
        raise ValueError('column_strength_cov must be finite and nonnegative')
    if cov == 0:
        return np.ones((N, ncol))
    sigma = np.sqrt(np.log1p(cov * cov))
    out = np.empty((N, ncol))
    mask = 0xffffffff
    for i in range(N):
        for j in range(ncol):
            seed = 2166136261
            encoded = f'{record}#{axis}#{i}#{j}'.encode('utf-16-le')
            for c in np.frombuffer(encoded, dtype='<u2'):
                seed = ((seed ^ int(c)) * 16777619) & mask
            draws = []
            for _ in range(2):
                seed = (seed + 0x6D2B79F5) & mask
                t = ((seed ^ (seed >> 15)) * (1 | seed)) & mask
                t ^= (t + (((t ^ (t >> 7)) * (61 | t)) & mask)) & mask
                draws.append(((t ^ (t >> 14)) & mask) / 4294967296)
            z = np.sqrt(-2 * np.log(max(draws[0], 2**-32))) * np.cos(2*np.pi*draws[1])
            out[i, j] = np.exp(sigma*z - sigma*sigma/2)
    return out


def build_backbones(building, record='', **overrides):
    """Parallel I-weighted stiffness shares; flexural coupling stays elastic.

    Four identical corner sections give exactly k0/4. This partitions the
    condensed pushover stiffness; it is not member-level condensation.
    Strength is the per-column plastic shear capacity, before scatter.
    """
    p = {**HFTD_DEFAULTS, **overrides}
    a1, a2 = p['backbone_hardening'], p['backbone_softening']
    mc, mu, r = p['backbone_mu_cap'], p['backbone_mu_ult'], p['backbone_residual_frac']
    if not (mu > mc > 1 and a1 >= 0 and a2 >= 0 and 0 <= r <= 1):
        raise ValueError('invalid backbone parameters: require mu_ult > mu_cap > 1')
    # Zero softening deliberately denotes the bilinear validation model.
    if a2 and 1 + a1*(mc-1) - a2*(mu-mc) > r + 1e-14:
        raise ValueError('backbone_softening cannot reach residual strength by mu_ult')
    psi = column_strength_scatter(record, building.axis, building.N, 4, p['column_strength_cov'])
    k0 = np.repeat(building.k0_profile[:, None]/4, 4, axis=1)
    vy = building.V_p_profile[:, None]/4 * psi
    dy = vy/k0
    # Ultimate drift scatter must not make a valid nominal backbone
    # discontinuous: plateau onset follows the backbone, failure threshold
    # is at least that onset (spec correction).
    dc = mc*dy
    du = mu*dy * np.sqrt(psi)
    if a2:
        plateau = dc + (1 + a1*(mc-1)-r)*dy/a2
        du = np.maximum(du, plateau)
    else:
        du = np.full_like(dy, np.inf)
    return dict(k0=k0, vy=vy, dy=dy, dc=dc, du=du, vr=r*vy,
                psi=psi, params=p)


class ColumnHysteresis:
    """Causal peak-oriented springs, vectorised over (story, column).

    Reversal unloads to zero force, then connects continuously to the
    pinch point, then to the opposite peak. This connecting segment is
    necessary: the specified degraded unloading line generally does not
    intersect the specified pinch point. No energy-driven deterioration.
    """
    ELASTIC, BACKBONE_POS, BACKBONE_NEG, UNLOAD, RELOAD, RESIDUAL = range(6)

    def __init__(self, backbone):
        self.bb = backbone
        self.p = backbone['params']
        shape = backbone['k0'].shape
        self.delta = np.zeros(shape)
        self.force = np.zeros(shape)
        self.tangent = backbone['k0'].copy()
        self.direction = np.zeros(shape)
        self.positive = backbone['dy'].copy()
        self.negative = -backbone['dy'].copy()
        self.positive_force = backbone['vy'].copy()
        self.negative_force = -backbone['vy'].copy()
        self.rev_delta = np.zeros(shape)
        self.rev_force = np.zeros(shape)
        self.k_unload = backbone['k0'].copy()
        self.failed_pos = np.zeros(shape, dtype=bool)
        self.failed_neg = np.zeros(shape, dtype=bool)
        self.yielded = np.zeros(shape, dtype=bool)
        self.direct_reload = np.zeros(shape, dtype=bool)
        self.branch = np.zeros(shape, dtype=np.int8)
        self.work = np.zeros(shape)

    def backbone(self, delta):
        b = self.bb
        x = np.abs(delta)
        a1, a2 = self.p['backbone_hardening'], self.p['backbone_softening']
        elastic = x <= b['dy']
        hard = x <= b['dc']
        vc = b['vy'] + a1*b['k0']*(b['dc']-b['dy'])
        soft = vc - a2*b['k0']*(x-b['dc'])
        value = np.where(elastic, b['k0']*x,
                         np.where(hard | (a2 == 0), b['vy']+a1*b['k0']*(x-b['dy']),
                                  np.maximum(b['vr'], soft)))
        tangent = np.where(elastic, b['k0'], np.where(hard | (a2 == 0), a1*b['k0'],
                           np.where(soft > b['vr'], -a2*b['k0'], 0.)))
        failed = np.where(delta >= 0, self.failed_pos, self.failed_neg)
        value = np.where(failed, np.minimum(value, b['vr']), value)
        tangent = np.where(failed & (value >= b['vr']), 0., tangent)
        return np.sign(delta)*value, tangent

    def step(self, delta):
        d = np.asarray(delta, dtype=float)
        if d.shape == (self.bb['k0'].shape[0],):
            d = d[:, None]
        d = np.broadcast_to(d, self.delta.shape)
        increment = d-self.delta
        direction = np.where(increment != 0, np.sign(increment), self.direction)
        reversal = (direction*self.direction < 0) & self.yielded
        self.direct_reload = np.where(reversal, direction*self.force >= 0, self.direct_reload)
        self.rev_delta = np.where(reversal, self.delta, self.rev_delta)
        self.rev_force = np.where(reversal, self.force, self.rev_force)
        peak = np.maximum(self.positive, -self.negative)
        self.k_unload = np.where(reversal, self.bb['k0']*(self.bb['dy']/peak)**self.p['unload_degrade_exp'], self.k_unload)
        self.failed_pos |= d > self.bb['du']
        self.failed_neg |= d < -self.bb['du']
        self.yielded |= np.abs(d) > self.bb['dy']
        target_d = np.where(direction >= 0, self.positive, self.negative)
        target_v = np.where(direction >= 0, self.positive_force, self.negative_force)
        target_v = np.where(np.where(direction >= 0, self.failed_pos, self.failed_neg),
                            np.sign(target_d)*self.bb['vr'], target_v)
        pinch_d = self.p['pinch_drift_frac']*target_d
        pinch_v = self.p['pinch_force_frac']*target_v
        zero_d = self.rev_delta-self.rev_force/self.k_unload
        # For a small nested reversal, its zero can lie beyond the pinch.
        # Join directly to the target in that case, preserving continuity.
        usable_pinch = direction*(pinch_d-zero_d) > 0
        join_d = np.where(usable_pinch, pinch_d, target_d)
        join_v = np.where(usable_pinch, pinch_v, target_v)
        den = join_d-zero_d
        slope_mid = np.divide(join_v, den, out=self.k_unload.copy(), where=den != 0)
        den_end = target_d-pinch_d
        slope_end = np.divide(target_v-pinch_v, den_end, out=self.k_unload.copy(), where=den_end != 0)
        unload = direction*(d-zero_d) <= 0
        middle = direction*(d-join_d) <= 0
        v = np.where(unload, self.rev_force+self.k_unload*(d-self.rev_delta),
                     np.where(middle, slope_mid*(d-zero_d), pinch_v+slope_end*(d-pinch_d)))
        kt = np.where(unload, self.k_unload, np.where(middle, slope_mid, slope_end))
        # A nested reversal can already carry force toward its target.
        # Its zero-force intercept lies behind the reversal, so unload-to-
        # zero would skip immediately to a disconnected reload line.
        direct_den = target_d-self.rev_delta
        direct_slope = np.divide(target_v-self.rev_force, direct_den,
                                 out=self.k_unload.copy(), where=direct_den != 0)
        v = np.where(self.direct_reload, self.rev_force+direct_slope*(d-self.rev_delta), v)
        kt = np.where(self.direct_reload, direct_slope, kt)
        unload = unload & ~self.direct_reload
        envelope = ((direction >= 0) & (d >= self.positive)) | ((direction < 0) & (d <= self.negative))
        virgin = ~self.yielded
        bv, bk = self.backbone(d)
        v = np.where(envelope | virgin, bv, v)
        kt = np.where(envelope | virgin, bk, kt)
        # Bound the opposite envelope only beyond its yield point. Near
        # zero a yielded spring can carry nonzero force: clipping it to
        # the virgin elastic line would create a force jump at zero drift.
        beyond = (direction*v > direction*bv) & (direction*d >= self.bb['dy'])
        v = np.where(beyond, bv, v)
        kt = np.where(beyond, bk, kt)
        new_pos = d >= self.positive
        new_neg = d <= self.negative
        self.positive = np.where(new_pos, d, self.positive)
        self.negative = np.where(new_neg, d, self.negative)
        self.positive_force = np.where(new_pos, v, self.positive_force)
        self.negative_force = np.where(new_neg, v, self.negative_force)
        self.branch = np.where(virgin, self.ELASTIC, np.where(envelope,
            np.where(d >= 0, self.BACKBONE_POS, self.BACKBONE_NEG),
            np.where(unload, self.UNLOAD, self.RELOAD))).astype(np.int8)
        self.branch = np.where((self.failed_pos & (d>0)) | (self.failed_neg & (d<0)), self.RESIDUAL, self.branch)
        self.work += .5*(self.force+v)*increment
        self.delta = d.copy()
        self.force, self.tangent, self.direction = v, kt, direction
        return v.copy(), kt.copy()


def assemble_pseudo_force(s):
    """B.T @ s: story-shear correction, not a decomposition of frame K."""
    p = np.array(s, copy=True)
    p[:-1] -= s[1:]
    return p


def assemble_pseudo_force_3N(s_x, s_y, positions):
    """Spec 12 B3: p_NL = sum_ij T_ij^T [k0,ij*delta_ij - V_ij].

    `s_x`, `s_y` are the per-column constitutive defects, each (N, 4, npts),
    for the two bending axes. `T_ij` is the row of `frame_transform` that
    produced that column's drift, so its transpose places the defect on all
    three DOFs of floors i and i-1:

        u_x block:    sum_j s_x,ij
        u_y block:    sum_j s_y,ij
        theta block:  sum_j [ -y_j*s_x,ij + x_j*s_y,ij ]

    each then carried across the story boundary by the SAME B^T that spec
    11's `assemble_pseudo_force` applies -- B^T factors out of the sum over
    j because every column of story i shares the b_i = e_i - e_{i-1} row.
    Spec 11's tidy `p_i = s_i - s_{i+1}` is this with one more index, and
    the sign here is the single most likely place for the spec to break
    silently: a wrong transpose leaves the ELASTIC defect identically zero,
    so verification check 6 still passes. Check 2's known-direction case is
    what actually exercises it (verification correction V-4).
    """
    x = positions[:, 0][None, :, None]
    y = positions[:, 1][None, :, None]
    N, _, npts = s_x.shape
    p = np.empty((3 * N, npts))
    p[:N] = assemble_pseudo_force(s_x.sum(axis=1))
    p[N:2 * N] = assemble_pseudo_force(s_y.sum(axis=1))
    p[2 * N:] = assemble_pseudo_force((-y * s_x + x * s_y).sum(axis=1))
    return p


def column_drift_3N(u, positions):
    """Spec 12 B2: per-column story drift from the 3N displacement history.

        delta_x,ij = [u_x,i - y_j*theta_i] - [u_x,i-1 - y_j*theta_i-1]
        delta_y,ij = [u_y,i + x_j*theta_i] - [u_y,i-1 + x_j*theta_i-1]

    Every column now has its OWN drift. Spec 11 A4's "a rigid diaphragm
    forces every column to share the same drift" was true of a
    one-lateral-DOF-per-floor model and is false here: the diaphragm is
    rigid *in plane*, which is exactly what permits rotation.

    `u` is (3N, npts) in the `[u_x, u_y, theta]` ordering; returns two
    (N, 4, npts) arrays. The bracketed quantity is the column's own lateral
    displacement, i.e. `frame_transform` evaluated at that column's plan
    position, and the drift is its first difference up the building -- so
    this and `assemble_pseudo_force_3N` are one transform and its
    transpose, not two independently written sign conventions.
    """
    N = u.shape[0] // 3
    u_x, u_y, theta = u[:N], u[N:2 * N], u[2 * N:]
    x = positions[:, 0][None, :, None]
    y = positions[:, 1][None, :, None]
    col_x = u_x[:, None, :] - y * theta[:, None, :]
    col_y = u_y[:, None, :] + x * theta[:, None, :]
    zero = np.zeros((1, *col_x.shape[1:]))
    return (np.diff(col_x, axis=0, prepend=zero),
            np.diff(col_y, axis=0, prepend=zero))


from dataclasses import dataclass, field


@dataclass
class HFTDResult:
    u_rel: np.ndarray
    u_abs: np.ndarray
    story_drift: np.ndarray
    story_shear: np.ndarray
    k_t_ratio: np.ndarray
    p_nl: np.ndarray
    damage_state: np.ndarray
    column_damage: np.ndarray
    converged: bool
    iterations: int
    residual_history: list
    energy: dict
    velocity: np.ndarray
    acceleration: np.ndarray
    column_force: np.ndarray
    column_tangent: np.ndarray
    backbones: dict
    dt: float
    # (N, 4, npts). Equal to `story_drift` broadcast on the per-axis path;
    # genuinely per-column once theta is free (spec 12 B2).
    column_drift: np.ndarray = None
    collapse_events: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    segments: int = 1
    padding_capped: bool = False
    reason: str = ''
    # Spec 13: the pre-downsampling solve-grid histories (and the refined
    # traces, when factor 4) kept so a restart can recover the exact state.
    solve_grid: dict = None


def _hysteresis_history(drift, bb, initial_state=None):
    """Per-column drift history -> column forces, tangents and the defect.

    `drift` is (N, 4, npts): spec 12 B2 gives every column its own drift.
    The pre-spec-12 one-drift-per-story case is that same story drift
    broadcast across the column axis, which is exact rather than merely
    close, so the per-axis path stays bit-identical (verification check 11).

    The returned defect `k0*delta - V` is the RAW per-column array, not
    placed on the structural DOFs. Placement is the caller's, because the
    3N path must sum both bending axes before applying `T_ij^T` -- see
    `assemble_pseudo_force_3N`.
    """
    npts = drift.shape[-1]
    machine = ColumnHysteresis(bb) if initial_state is None else copy.deepcopy(initial_state)
    shape = (*bb['k0'].shape, npts)
    force, tangent = np.empty(shape), np.empty(shape)
    damage = np.empty(shape, dtype=np.int8)
    correction = np.zeros(shape)
    peak = np.max(abs(drift),axis=-1)
    if not np.any(machine.yielded) and np.all(peak <= bb['dy']):
        force = bb['k0'][:,:,None]*drift
        tangent = np.broadcast_to(bb['k0'][:,:,None],shape).copy()
        damage.fill(ColumnHysteresis.ELASTIC)
        machine.step(drift[:,:,-1])
        machine.work = .5*bb['k0']*drift[:,:,-1]**2
        return force,tangent,damage,correction,machine
    # Within a monotonic stretch each sample lies on the same path from
    # the committed reversal. Evaluate those samples as an extra NumPy
    # dimension, then commit only the final state. Split whenever ANY
    # column reverses, preserving vectorisation across every story and
    # column. Reducing over both axes rather than over stories alone is
    # what keeps this exact for the broadcast per-axis case: direction
    # changes are then common to the four columns, and `any` over j of
    # `peak_i > dy_ij` is `peak_i > min_j dy_ij`, the old expression.
    increments=np.diff(drift,axis=-1,prepend=machine.delta[:,:,None])
    directions=np.sign(increments)
    active=machine.yielded | (peak>bb['dy'])
    cuts=np.flatnonzero(np.any((directions[...,1:] != directions[...,:-1]) & active[...,None],axis=(0,1)))+1
    boundaries=np.r_[0,cuts,npts]
    for start,end in zip(boundaries[:-1],boundaries[1:]):
        trial=copy.copy(machine)
        trial.bb={key:(value[:,:,None] if isinstance(value,np.ndarray) else value)
                  for key,value in bb.items()}
        for key,value in vars(machine).items():
            if isinstance(value,np.ndarray):
                setattr(trial,key,np.broadcast_to(value[:,:,None],(*value.shape,end-start)).copy())
        values,slopes=trial.step(drift[:,:,start:end])
        force[:,:,start:end]=values
        tangent[:,:,start:end]=slopes
        damage[:,:,start:end]=trial.branch
        defects=bb['k0'][:,:,None]*drift[:,:,start:end]-values
        defects[~trial.yielded]=0.
        correction[:,:,start:end]=defects
        previous_force=machine.force.copy()
        previous_delta=machine.delta.copy()
        old_work=machine.work.copy()
        for key,value in vars(trial).items():
            if isinstance(value,np.ndarray):
                setattr(machine,key,value[:,:,-1].copy())
        delta_steps=np.diff(np.broadcast_to(drift[:,:,start:end],values.shape),axis=2,prepend=previous_delta[:,:,None])
        left_values=np.concatenate((previous_force[:,:,None],values[:,:,:-1]),axis=2)
        machine.work=old_work+np.sum(.5*(left_values+values)*delta_steps,axis=2)
    return force, tangent, damage, correction, machine


HysteresisAux = namedtuple(
    'HysteresisAux', 'state story_drift column_drift force tangent damage')


def _per_axis_constitutive(bb, initial=None):
    """Spec 11's one-drift-per-story constitutive law, as a callable.

    The drift broadcast across the column axis is a view, not arithmetic,
    so every number downstream is bit-identical to the pre-spec-12 code.
    `initial` is the state a call with no state starts from: None (virgin)
    for a from-rest solve, the frozen state for a spec-13 restart.
    """
    def constitutive(u, state=None):
        state = initial if state is None else state
        story = np.diff(u, axis=0, prepend=np.zeros((1, u.shape[1])))
        drift = np.broadcast_to(story[:, None, :], (*bb['k0'].shape, u.shape[1]))
        force, tangent, damage, defect, machine = _hysteresis_history(drift, bb, state)
        return (assemble_pseudo_force(defect.sum(axis=1)),
                HysteresisAux(machine, story, drift, force, tangent, damage))
    return constitutive


def _torsion_constitutive(bb_x, bb_y, positions, initial=None):
    """Spec 12 B2+B3: two independent per-column state machines, one placement.

    The two bending axes of a column are kept independent -- one state
    machine per (story, column, axis), with NO biaxial interaction surface.
    That is a real approximation, not a simplification of bookkeeping:
    biaxial bending reduces a column's capacity in both directions at once,
    so this model is unconservative for a column driven hard on both axes
    simultaneously. A circular/elliptical interaction is research-grade and
    deliberately out of scope (spec 12 B2).
    """
    def constitutive(u, state=None):
        state = initial if state is None else state
        sx, sy = state if state is not None else (None, None)
        dx, dy = column_drift_3N(u, positions)
        fx, tx, gx, ex, mx = _hysteresis_history(dx, bb_x, sx)
        fy, ty, gy, ey, my = _hysteresis_history(dy, bb_y, sy)
        N = u.shape[0] // 3
        story = (np.diff(u[:N], axis=0, prepend=np.zeros((1, u.shape[1]))),
                 np.diff(u[N:2 * N], axis=0, prepend=np.zeros((1, u.shape[1]))))
        return (assemble_pseudo_force_3N(ex, ey, positions),
                HysteresisAux((mx, my), story, (dx, dy), (fx, fy),
                              (tx, ty), (gx, gy)))
    return constitutive


def evaluate_collapse_criteria(result, building, k_g=None):
    """Physical thresholds evaluated only after the fixed point converges.

    Global drift thresholds are descriptive FEMA 356 commentary values;
    crossing them is a model onset flag, not a validated failure prediction.
    `k_g` optionally overrides `building.k_g` with a per-sample (N, npts)
    history: after a spec-13 restart the survivor carries reduced gravity,
    and the gravity criterion must use the P/h actually in force.
    """
    r, p, bb = result, result.backbones['params'], result.backbones
    drift = r.story_drift
    # Capacity is a per-column property, so ductility and fracture are
    # evaluated on the per-column drift; the descriptive story-level
    # thresholds and the reported residual stay on the story drift, which
    # is the centre-of-mass quantity the viewer and the wire format carry.
    col = r.column_drift if r.column_drift is not None else np.broadcast_to(
        drift[:,None,:], (*bb['dy'].shape, drift.shape[-1]))
    mu = abs(col)/bb['dy'][:,:,None]
    kt = r.column_tangent.sum(axis=1)
    first = lambda mask: float(np.argmax(mask)*r.dt) if np.any(mask) else None
    fail = [[first(abs(col[i,j]) > bb['du'][i,j]) for j in range(4)] for i in range(building.N)]
    times, criteria, directions = [], [], []
    for i in range(building.N):
        masks = [np.max(abs(col[i]),axis=0)/building.h[i] > p['drift_limit_cp'],
                 np.max(mu[i],axis=0) > p['collapse_mu_cap'],
                 kt[i] <= ((building.k_g[i] if k_g is None else k_g[i]) if building.p_delta else 0.)]
        hits = [(int(np.argmax(m)),name) for m,name in zip(masks,('drift','ductility','gravity')) if np.any(m)] if r.converged else []
        if hits:
            t,name = min(hits, key=lambda pair:pair[0])
            times.append(float(t*r.dt)); criteria.append(name); directions.append(int(np.sign(drift[i,t])))
            r.collapse_events.append(dict(story=i, time=float(t*r.dt), criterion=name, direction=directions[-1], axis=building.axis))
        else:
            times.append(None); criteria.append(None); directions.append(None)
    r.summary = dict(converged=r.converged, iterations=r.iterations, reason=r.reason,
        t_fail=fail if r.converged else [[None]*4 for _ in range(building.N)],
        t_collapse=times, collapse_criterion=criteria, collapse_direction=directions,
        collapse_axis=building.axis if r.collapse_events else None,
        residual_drift=drift[:,-1].tolist(), peak_drift_ratio=(np.max(abs(drift),axis=1)/building.h).tolist(),
        peak_mu=np.max(mu,axis=2).tolist(), k_t_min=np.min(kt,axis=1).tolist(),
        collapse_events=r.collapse_events, residual_history=r.residual_history,
        segments=r.segments, padding_capped=r.padding_capped)
    return r.collapse_events


# Spec 14: the damage code a viewer sees. `ColumnHysteresis.branch` is the
# INSTANTANEOUS branch and cycles UNLOAD <-> RELOAD, so it is mapped to a
# severity and accumulated (damage does not heal). RESIDUAL only occurs from
# t_fail on (failed_pos/neg trip on the same |delta| > du test as t_fail), so
# three levels are all the state machine can distinguish.
DAMAGE_CODES = {0: 'elastic', 1: 'yielded', 2: 'failed'}
DAMAGE_CODE_OF_BRANCH = np.array([0, 1, 1, 1, 1, 2], dtype=np.uint8)
# Descriptive FEMA 356 Table C1-3 transient drifts for concrete frames,
# Immediate Occupancy / Life Safety; CP is the `drift_limit_cp` parameter.
DRIFT_LIMIT_IO, DRIFT_LIMIT_LS = 0.01, 0.02


def accumulated_damage(column_damage, t_fail, dt):
    """(N, 4, npts) raw branches -> accumulated codes (column, story max).

    Code 2 is forced from each column's `t_fail` sample on, so the failed
    code and the summary's t_fail can never disagree.
    """
    codes = DAMAGE_CODE_OF_BRANCH[column_damage]
    for i, row in enumerate(t_fail):
        for j, t in enumerate(row):
            if t is not None:
                codes[i, j, int(round(t / dt)):] = 2
    codes = np.maximum.accumulate(codes, axis=-1)
    return codes, codes.max(axis=1)


def subsample_nearest(arr, q):
    """Every q-th sample along time, no filter: each value is a real sample.

    Right for categorical codes (a filtered category is meaningless) and for
    the tangent ratio, which jumps at every branch switch -- an FIR filter
    would ring those jumps past 1.
    """
    return np.ascontiguousarray(arr[..., ::q])


class _HFTDConvolution:
    """Modal overlap-save convolution of a trial pseudo-force history.

    The leading overlap retains the damped kernel memory. Hysteresis is
    evaluated once over the saved, joined history, never independently
    reset at FFT block boundaries. A quiet prefix separates the held
    residual tail from the start of the physical record.
    """

    def __init__(self, building, npts, dt, segment_seconds=None,
                 overlap_seconds=None):
        self.building = building
        self.npts = npts
        # DOF count, not story count: the coupled 3N kernel runs the same
        # modal overlap-save with phi shaped (3N, 3N).
        self.ndof = building.phi.shape[0]
        settle = 5 / (building.zeta * building.omega_n[0])
        requested_guard = int(np.ceil(3 * settle / dt))
        self.guard = min(requested_guard, 200000)
        overlap_seconds = settle if overlap_seconds is None else overlap_seconds
        self.overlap = max(0, int(np.ceil(overlap_seconds / dt)))
        self.block_size = npts if segment_seconds is None else max(1, int(round(segment_seconds / dt)))
        self.segments = int(np.ceil(npts / self.block_size))
        window = min(npts, self.block_size + self.overlap)
        requested_tail = max(self.guard, window)
        tail = min(requested_tail, 200000)
        self.length = next_fast_len(self.guard + window + tail)
        self.padding_capped = requested_guard > self.guard or requested_tail > tail
        self.omega = 2 * np.pi * fftfreq(self.length, dt)
        wn = building.omega_n[:, None]
        self.compliance = 1 / (wn**2 - self.omega**2 + 2j * building.zeta * wn * self.omega)

    def apply(self, force, derivatives=False):
        b = self.building
        displacement = np.empty_like(force)
        velocity = np.empty_like(force) if derivatives else None
        acceleration = np.empty_like(force) if derivatives else None
        for start in range(0, self.npts, self.block_size):
            end = min(start + self.block_size, self.npts)
            left = max(0, start - self.overlap)
            size = end - left
            padded = np.zeros((self.ndof, self.length))
            padded[:, self.guard:self.guard + size] = force[:, left:end]
            padded[:, self.guard + size:] = force[:, end - 1, None]
            q = self.compliance * (b.phi.T @ fft(padded, axis=1))
            saved = slice(self.guard + start - left, self.guard + size)
            displacement[:, start:end] = b.phi @ np.real(ifft(q, axis=1))[:, saved]
            if derivatives:
                velocity[:, start:end] = b.phi @ np.real(ifft(1j * self.omega * q, axis=1))[:, saved]
                acceleration[:, start:end] = b.phi @ np.real(ifft(-self.omega**2 * q, axis=1))[:, saved]
        return displacement, velocity, acceleration



def _causal_fft_predictor(building, base, constitutive, convolution, dt, max_iterations):
    """Causal block predictor for the whole-record FFT fixed point.

    The positive-time samples of IFFT(H) form a Volterra convolution.
    Short blocks make its constitutive feedback contractive; completed
    blocks contribute through FFT convolution, with frozen causal state.
    This predicts forces only. The requested whole-record/overlap-save
    operator must still pass its unchanged residual test afterward.

    The block length is that contraction knob, so a block that runs out of
    iterations HALVES it and retries the same block rather than abandoning
    the whole prediction. Spec 12 is what made this necessary: the coupled
    3N map contracts more slowly than the per-axis one -- measurably
    contracting, just not inside the budget -- and giving up dropped the
    solve back to plain relaxation, which stalls near a residual of 0.6
    and never converges. Completed blocks stay valid when the schedule
    changes, so nothing already solved is recomputed. The per-axis path
    never exhausts the budget at 1.0 s, so it never halves and its numbers
    are unchanged.
    """
    b = building
    n = base.shape[1]
    block = max(2, int(1.0 / dt))
    kernel = np.real(ifft(convolution.compliance, axis=1))[:, :n]
    length = next_fast_len(n + block - 1)
    kernel_fft = rfft(kernel, n=length, axis=1)
    local_length = next_fast_len(2 * block - 1)
    local_kernel = rfft(kernel[:, :block], n=local_length, axis=1)
    past, force = np.zeros_like(base), np.zeros_like(base)
    state = None
    total_iterations = 0
    start = 0
    while start < n:
        end = min(n, start + block)
        size = end - start
        trial = np.repeat(force[:, start-1:start], size, axis=1) if start else np.zeros((base.shape[0], size))
        for _ in range(max_iterations):
            total_iterations += 1
            modal = b.phi.T @ trial
            correction = b.phi @ irfft(local_kernel * rfft(modal, n=local_length, axis=1), n=local_length, axis=1)[:, :size]
            u = base[:, start:end] + past[:, start:end] + correction
            candidate, aux = constitutive(u, state)
            next_state = aux.state
            error = np.linalg.norm(candidate-trial) / max(np.linalg.norm(candidate), 1.)
            if not np.isfinite(error):
                return None, total_iterations
            # This is only a seed for the final whole-record residual test.
            # Solving predictor blocks more tightly than the production
            # fixed-point tolerance repeats expensive hysteresis histories
            # without strengthening the accepted result.
            if error < 1e-4:
                break
            trial = candidate
        else:
            if block <= 2:
                return None, total_iterations
            block = max(2, block // 2)
            local_length = next_fast_len(2 * block - 1)
            local_kernel = rfft(kernel[:, :block], n=local_length, axis=1)
            continue
        state = next_state
        force[:, start:end] = trial
        modal = b.phi.T @ trial
        response = b.phi @ irfft(kernel_fft * rfft(modal, n=length, axis=1), n=length, axis=1)[:, :n-start]
        past[:, end:] += response[:, size:]
        start = end
    return force, total_iterations



def _hftd_fixed_point(building, base, constitutive, convolution, dt, p, dy_min, offset=None):
    """The shared pseudo-force fixed point: causally seeded, Anderson-mixed.

    DOF-agnostic. `base` is the elastic response on whatever DOF set the
    building has -- (N, npts) per axis, (3N, npts) coupled -- and
    `constitutive` maps a displacement history to a pseudo-force on those
    same DOFs. The per-axis and 3N paths differ ONLY in that callable and
    in how the converged history is packaged afterwards, never in the
    iteration itself, so there is still exactly one fixed point in the
    codebase rather than a torsional copy of it.

    Returns the converged force, the displacement history it produced, the
    last constitutive `HysteresisAux`, and the loop's own diagnostics.
    `offset` (spec 13) is a force already accounted for in `base`: the
    iteration solves for the remainder, but convergence is still measured
    relative to the total pseudo-force, exactly as without it.
    """
    n = base.shape[1]
    force = np.zeros_like(base)
    history=[]
    previous_force = previous_residual = None
    force_steps, residual_steps = [], []
    converged=False
    predictor_iterations = 0
    quiet_prefix = 0
    causality_error = 0.
    u=base.copy()
    for iteration in range(1,int(p['hftd_max_iterations'])+1):
        if iteration == 3:
            predicted, predictor_iterations = _causal_fft_predictor(
                building, base, constitutive, convolution, dt, int(p['hftd_max_iterations']))
            if predicted is not None:
                force = predicted
                active = np.any(force != 0, axis=0)
                # Stay away from the first force corner: finite-bandwidth
                # interpolation has a small local pre-ringing there.
                quiet_prefix = int(np.argmax(active)) // 2 if np.any(active) else n
                previous_force = previous_residual = None
                force_steps, residual_steps = [], []
        if np.any(force):
            correction,_,_ = convolution.apply(force)
            u=base+correction
        candidate, aux = constitutive(u)
        residual = candidate-force
        change=p['hftd_relaxation']*residual
        updated=force+change
        norm=float(np.linalg.norm(change)/max(np.linalg.norm(updated if offset is None else updated+offset),1e-30))
        history.append(norm)
        if not np.isfinite(norm) or not np.all(np.isfinite(u)):
            break
        if quiet_prefix:
            causality_error = float(np.max(abs(u[:, :quiet_prefix]-base[:, :quiet_prefix])) / dy_min)
            if causality_error > p['hftd_tolerance']:
                break
        if norm < p['hftd_tolerance']:
            converged=True
            break
        if iteration < p['hftd_max_iterations']:
            # Anderson mixing accelerates the same FFT fixed point; it does
            # not introduce a time integrator or alter the residual test.
            if previous_force is not None:
                force_steps.append((force-previous_force).reshape(-1))
                residual_steps.append((residual-previous_residual).reshape(-1))
                force_steps, residual_steps = force_steps[-20:], residual_steps[-20:]
                D = np.stack(residual_steps,axis=1)
                gram = D.T@D
                regularizer = max(float(np.trace(gram))*1e-12,1e-30)
                weights = np.linalg.solve(gram+regularizer*np.eye(len(force_steps)),D.T@residual.reshape(-1))
                accelerated = updated.reshape(-1)-(np.stack(force_steps,axis=1)+p['hftd_relaxation']*D)@weights
                if np.all(np.isfinite(accelerated)) and np.linalg.norm(accelerated) <= 10*max(np.linalg.norm(force)+np.linalg.norm(candidate),1e-30):
                    updated=accelerated.reshape(force.shape)
            previous_force, previous_residual = force.copy(), residual.copy()
            force=updated
    return force, u, aux, iteration, history, converged, predictor_iterations, causality_error


def solve_hftd(building, accel, disp, dt, params=None):
    """Preserve the elastic grid; resolve yielding on a finer FFT grid.

    Hysteresis introduces corners and harmonics above the input bandwidth.
    Fourfold band-limited input interpolation resolves those transitions;
    output arrays retain the caller's sample times and wire layout.
    This is sampling refinement, not a displacement post-filter.
    """
    p = {**HFTD_DEFAULTS, **(params or {})}
    a = np.asarray(accel, dtype=float) * p['intensity_scale']
    d = np.asarray(disp, dtype=float) * p['intensity_scale']
    if a.ndim != 1 or d.shape != a.shape or len(a) < 2 or not np.all(np.isfinite(a)) or not np.all(np.isfinite(d)) or not np.isfinite(dt) or dt <= 0:
        raise ValueError('ground traces must be finite matching 1-D arrays and dt positive')
    bb = build_backbones(building, **p)
    building.compute_response(a, d, dt, nonlinear=False)
    drift = np.diff(building.floor_disp_rel, axis=0, prepend=np.zeros((1, len(a))))
    if np.all(np.max(abs(drift), axis=1)[:, None] <= bb['dy']):
        result = _solve_hftd_grid(building, accel, disp, dt, p)
        result.summary['sampling_factor'] = 1
        result.solve_grid = _solve_grid(result, 1, dt, None)
        return result

    factor = 4
    fine_a, fine_d = _refine_traces(building, (a, d), dt, factor)
    result = _solve_hftd_grid(building, fine_a, fine_d, dt/factor,
                              {**p, 'intensity_scale': 1.})
    grid = _solve_grid(result, factor, dt/factor, (fine_a, fine_d))
    # Restore the native input/time metadata consumed by furniture and the
    # server; compute_response_nonlinear installs the returned floor arrays.
    building.compute_response(a, d, dt, nonlinear=False)
    for name in ('u_rel', 'u_abs', 'story_drift', 'story_shear', 'k_t_ratio',
                 'p_nl', 'damage_state', 'column_damage', 'velocity',
                 'acceleration', 'column_force', 'column_tangent',
                 'column_drift'):
        setattr(result, name, getattr(result, name)[..., ::factor].copy())
    result.u_abs = result.u_rel + d[None, :]
    result.dt = dt
    result.backbones['params'] = p
    predictor_iterations = result.summary['predictor_iterations']
    causality_error = result.summary['causality_error']
    result.collapse_events = []
    evaluate_collapse_criteria(result, building)
    result.summary.update(sampling_factor=factor,
                          predictor_iterations=predictor_iterations,
                          causality_error=causality_error)
    result.solve_grid = grid
    return result


def _refine_traces(building, traces, dt, factor=4):
    """Band-limited `factor`x interpolation of already-scaled ground traces.

    Shared by both sampling wrappers and by the spec-13 restart, which must
    slice the SAME fine traces the original solve used rather than
    re-resample a segment that starts mid-shake (that would add Gibbs
    ringing at the restart the original solve never had).
    """
    from scipy.signal import resample
    n = len(traces[0])
    settle = 5 / (building.zeta * building.omega_n[0])
    length = min(4*n, n + int(np.ceil(settle/dt)))
    count = (n-1)*factor + 1
    return [resample(np.pad(t, (0, length-n)), length*factor)[:count]
            for t in traces]


def _solve_grid(result, factor, dt, traces):
    """References (not copies) to a result's solve-grid histories."""
    views = (result.x, result.y) if isinstance(result, HFTD3NResult) else (result,)
    return dict(factor=factor, dt=dt, traces=traces, u=result.u_rel,
                velocity=result.velocity,
                column_drift=[v.column_drift for v in views],
                column_force=[v.column_force for v in views])


def _base_velocity(building, a, dt):
    """Velocity of the elastic base on compute_response's frequency grid."""
    n, N = len(a), building.N
    settle = 5/(building.zeta*building.omega_n[0])
    old_len = min(4*n,n+int(np.ceil(settle/dt)))
    old_w = 2*np.pi*fftfreq(old_len,dt)
    af = fft(a,n=old_len)
    base_vel=np.zeros((N, n))
    for i in range(N):
        old_den=building.omega_n[i]**2-old_w**2+2j*building.zeta*building.omega_n[i]*old_w
        q=(-building.Gamma[i]/old_den)*af
        base_vel+=np.outer(building.phi[:,i],np.real(ifft(1j*old_w*q))[:n])
    return base_vel


def _solve_hftd_grid(building, accel, disp, dt, params=None):
    """Modal FFT pseudo-force fixed point; no time-marching structural solve.

    The original elastic expression is untouched. Its response is the base
    solution; an FFT of the constitutive force defect adds the correction.
    Leading quiet padding prevents a held residual tail wrapping to t=0.
    The frame's non-story-spring flexural coupling remains elastic.
    """
    p = {**HFTD_DEFAULTS, **(params or {})}
    scale = p['intensity_scale']
    a, d = np.asarray(accel,dtype=float)*scale, np.asarray(disp,dtype=float)*scale
    if a.ndim != 1 or d.shape != a.shape or len(a)<2 or not np.all(np.isfinite(a)) or not np.all(np.isfinite(d)) or not np.isfinite(dt) or dt<=0:
        raise ValueError('ground traces must be finite matching 1-D arrays and dt positive')
    bb = build_backbones(building, **p)
    building.compute_response(a,d,dt,nonlinear=False)
    constitutive = _per_axis_constitutive(bb)
    base = building.floor_disp_rel.copy()
    base_acc = building.floor_accel_rel.copy()
    n, N = len(a), building.N
    convolution = _HFTDConvolution(building,n,dt,p['hftd_segment_seconds'])
    # Velocity of the unchanged elastic base on its original frequency grid.
    base_vel = _base_velocity(building, a, dt)
    force, u, aux, iteration, history, converged, predictor_iterations, causality_error = \
        _hftd_fixed_point(building, base, constitutive, convolution, dt, p,
                          float(np.min(bb['dy'])))
    drift, vf, kt, damage, machine = (aux.story_drift, aux.force, aux.tangent,
                                      aux.damage, aux.state)
    velocity=base_vel.copy(); acceleration=base_acc.copy()
    if np.any(force):
        _,correction_velocity,correction_acceleration=convolution.apply(force,True)
        velocity=base_vel+correction_velocity
        acceleration=base_acc+correction_acceleration
    # Independently integrated work terms. Constitutive work is separated
    # from recoverable secant spring energy; coupling and gravity stay elastic.
    B=np.eye(N)-np.eye(N,k=-1)
    coupling=building.K-B.T@np.diag(building.k0_profile)@B
    elastic_energy=.5*np.einsum('it,ij,jt->t',u,coupling,u)
    spring_energy=np.sum(.5*vf*vf/bb['k0'][:,:,None],axis=(0,1))
    hysteretic_work=float(np.sum(machine.work)-spring_energy[-1])
    if not np.any(machine.yielded): hysteretic_work=0.
    modal_velocity=building.phi.T@building.M@velocity
    damp=float(np.trapezoid(np.sum(2*building.zeta*building.omega_n[:,None]*modal_velocity**2,axis=0),dx=dt))
    input_energy=float(np.trapezoid(-a*np.sum(building.M@velocity,axis=0),dx=dt))
    kinetic=float(.5*velocity[:,-1]@building.M@velocity[:,-1])
    strain=float(elastic_energy[-1]+spring_energy[-1])
    energy=dict(input=input_energy, kinetic=kinetic,strain=strain,damping=damp,hysteretic=hysteretic_work,
                closure_error=(kinetic+strain+damp+hysteretic_work-input_energy)/max(abs(input_energy),1e-30))
    result=HFTDResult(u,u+d[None,:],drift,vf.sum(axis=1),kt.sum(axis=1)/building.k0_profile[:,None],
        force,np.max(damage,axis=1),damage,converged,iteration,history,energy,velocity,acceleration+a[None,:],vf,kt,bb,dt,
        column_drift=aux.column_drift,
        segments=convolution.segments, padding_capped=convolution.padding_capped,
        reason='' if converged else 'Pseudo-force iteration did not converge; no collapse is inferred.')
    evaluate_collapse_criteria(result,building)
    if causality_error > p['hftd_tolerance']:
        result.reason = 'FFT tail dynamic range contaminated the causal prefix; no collapse is inferred.'
        result.summary['reason'] = result.reason
    result.summary['causality_error'] = causality_error
    result.summary['predictor_iterations'] = predictor_iterations
    return result


def _column_spring_stiffness_3N(k0_x, k0_y, positions):
    """sum_ij T_ij^T k0,ij T_ij -- the per-column story springs placed on 3N.

    The same `T_ij` as `column_drift_3N` / `assemble_pseudo_force_3N`,
    written once as a matrix so the energy balance's "everything that is
    NOT a yielding story spring" term is the exact complement of what the
    pseudo-force replaces. `B = I - shift` turns a column's lateral
    displacement into its story drift.
    """
    N = k0_x.shape[0]
    B = np.eye(N) - np.eye(N, k=-1)
    K = np.zeros((3 * N, 3 * N))
    for j, (xj, yj) in enumerate(positions):
        for kind, off, k0 in (("X", yj, k0_x[:, j]), ("Y", xj, k0_y[:, j])):
            T = B @ frame_transform(N, off, kind)
            K += T.T @ (k0[:, None] * T)
    return K


@dataclass
class HFTD3NResult:
    """The coupled nonlinear result: 3N histories plus two per-axis views.

    `x` and `y` are ordinary `HFTDResult`s carrying that bending axis's
    per-column arrays, so `evaluate_collapse_criteria` runs on them
    unchanged -- drift limits, ductility and the gravity tangent test are
    genuinely per-axis quantities and there is no second copy of them here.
    The `energy` dict is the COUPLED balance and is therefore the same
    object on both views; it is not separable per axis.
    """
    u_rel: np.ndarray
    p_nl: np.ndarray
    velocity: np.ndarray
    acceleration: np.ndarray
    x: HFTDResult
    y: HFTDResult
    converged: bool
    iterations: int
    residual_history: list
    energy: dict
    dt: float
    positions: np.ndarray
    collapse_events: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    segments: int = 1
    padding_capped: bool = False
    reason: str = ''
    solve_grid: dict = None

    @property
    def theta(self):
        """Floor rotation history (N, npts), radians about +Z."""
        return self.u_rel[2 * (self.u_rel.shape[0] // 3):]


def tangent_eccentricity(result):
    """Tangent centre-of-rigidity offset per story, (2, N, npts) (spec A3).

    e_x,i(t) = sum_j kt_x,ij*y_j / sum_j kt_x,ij  -- the offset, along y, of
    the centre that resists X-sway; e_y,i(t) is its x-offset counterpart.
    `build_backbones` gives all four columns k0/4 exactly, so before any
    yielding every kt is equal and both are 0.0 exactly -- that is a
    property of the code, not a hope (verification correction V-3).
    """
    x, y = result.positions[:, 0], result.positions[:, 1]
    out = []
    for kt, coord in ((result.x.column_tangent, y), (result.y.column_tangent, x)):
        out.append(np.einsum('ijt,j->it', kt, coord) / kt.sum(axis=1))
    return np.array(out)


def _solve_hftd_grid_3N(building, ax, dx, ay, dy, dt, params=None):
    """Spec 12 B2/B3: the pseudo-force fixed point on the coupled 3N system.

    Identical in structure to `_solve_hftd_grid` -- same elastic base, same
    modal FFT convolution, same Anderson-mixed fixed point, shared through
    `_hftd_fixed_point`. What changes is the constitutive callable: every
    column now has its own drift on each of two bending axes, and the
    defect is placed through `T_ij^T` instead of the single-index
    `p_i = s_i - s_{i+1}`.

    `K_3N`, `Phi` and `omega_n` are built once by the building's own
    constructor and never touched inside the iteration; re-forming them per
    iteration is the thing the performance gate is watching for.
    """
    p = {**HFTD_DEFAULTS, **(params or {})}
    scale = p['intensity_scale']
    ax, dx = np.asarray(ax, dtype=float)*scale, np.asarray(dx, dtype=float)*scale
    ay, dy = np.asarray(ay, dtype=float)*scale, np.asarray(dy, dtype=float)*scale
    for trace in (ax, dx, ay, dy):
        if trace.ndim != 1 or trace.shape != ax.shape or len(ax) < 2 or not np.all(np.isfinite(trace)):
            raise ValueError('ground traces must be finite matching 1-D arrays')
    if not np.isfinite(dt) or dt <= 0:
        raise ValueError('dt must be finite and positive')

    bb_x = build_backbones(building.bx, **p)
    bb_y = build_backbones(building.by, **p)
    positions = building.column_positions
    constitutive = _torsion_constitutive(bb_x, bb_y, positions)

    building.compute_response(ax, dx, ay, dy, dt, nonlinear=False)
    base = building.floor_disp_rel.copy()
    base_acc = building.floor_accel_rel.copy()
    base_vel = building.floor_vel_rel.copy()
    n, N = len(ax), building.N
    convolution = _HFTDConvolution(building, n, dt, p['hftd_segment_seconds'])

    force, u, aux, iteration, history, converged, predictor_iterations, causality_error = \
        _hftd_fixed_point(building, base, constitutive, convolution, dt, p,
                          float(min(np.min(bb_x['dy']), np.min(bb_y['dy']))))
    (mx, my) = aux.state
    velocity, acceleration = base_vel.copy(), base_acc.copy()
    if np.any(force):
        _, correction_velocity, correction_acceleration = convolution.apply(force, True)
        velocity = base_vel + correction_velocity
        acceleration = base_acc + correction_acceleration

    # Same energy decomposition as the per-axis solver, summed over the two
    # bending axes. Everything the per-column springs do NOT represent --
    # the frames' flexural coupling and P-Delta -- stays elastic and is
    # carried by `coupling`.
    springs = _column_spring_stiffness_3N(bb_x['k0'], bb_y['k0'], positions)
    coupling = building.K - springs
    elastic_energy = .5*np.einsum('it,ij,jt->t', u, coupling, u)
    spring_energy = (np.sum(.5*aux.force[0]**2/bb_x['k0'][:,:,None], axis=(0,1))
                     + np.sum(.5*aux.force[1]**2/bb_y['k0'][:,:,None], axis=(0,1)))
    hysteretic_work = float(np.sum(mx.work)+np.sum(my.work)) - spring_energy[-1]
    if not (np.any(mx.yielded) or np.any(my.yielded)):
        hysteretic_work = 0.
    modal_velocity = building.phi.T@building.M@velocity
    damp = float(np.trapezoid(np.sum(2*building.zeta*building.omega_n[:,None]*modal_velocity**2, axis=0), dx=dt))
    momentum = building.M@velocity
    input_energy = float(np.trapezoid(
        -ax*np.sum(momentum[:N], axis=0) - ay*np.sum(momentum[N:2*N], axis=0), dx=dt))
    kinetic = float(.5*velocity[:,-1]@building.M@velocity[:,-1])
    strain = float(elastic_energy[-1]+spring_energy[-1])
    energy = dict(input=input_energy, kinetic=kinetic, strain=strain, damping=damp,
                  hysteretic=hysteretic_work,
                  closure_error=(kinetic+strain+damp+hysteretic_work-input_energy)/max(abs(input_energy),1e-30))

    reason = '' if converged else 'Pseudo-force iteration did not converge; no collapse is inferred.'
    views = []
    for k, (sub, bb, ground, accel_g) in enumerate((
            (building.bx, bb_x, dx, ax), (building.by, bb_y, dy, ay))):
        block = slice(k*N, (k+1)*N)
        vf, kt, damage = aux.force[k], aux.tangent[k], aux.damage[k]
        views.append(HFTDResult(
            u[block], u[block]+ground[None,:], aux.story_drift[k],
            vf.sum(axis=1), kt.sum(axis=1)/sub.k0_profile[:,None],
            force[block], np.max(damage, axis=1), damage, converged, iteration,
            history, energy, velocity[block], acceleration[block]+accel_g[None,:],
            vf, kt, bb, dt, column_drift=aux.column_drift[k],
            segments=convolution.segments,
            padding_capped=convolution.padding_capped, reason=reason))
    rx, ry = views

    result = HFTD3NResult(
        u, force, velocity, acceleration, rx, ry, converged, iteration, history,
        energy, dt, positions, segments=convolution.segments,
        padding_capped=convolution.padding_capped, reason=reason)
    evaluate_collapse_criteria(rx, building.bx)
    evaluate_collapse_criteria(ry, building.by)
    result.collapse_events = rx.collapse_events + ry.collapse_events
    result.summary = dict(x=rx.summary, y=ry.summary, converged=converged,
                          iterations=iteration, reason=reason,
                          residual_history=history, segments=convolution.segments,
                          padding_capped=convolution.padding_capped,
                          collapse_events=result.collapse_events,
                          peak_rotation=float(np.max(abs(result.theta))),
                          residual_rotation=result.theta[:,-1].tolist())
    if causality_error > p['hftd_tolerance']:
        result.reason = 'FFT tail dynamic range contaminated the causal prefix; no collapse is inferred.'
        result.summary['reason'] = result.reason
    result.summary['causality_error'] = causality_error
    result.summary['predictor_iterations'] = predictor_iterations
    return result


def solve_hftd_3N(building, ax, dx, ay, dy, dt, params=None):
    """`solve_hftd`'s adaptive 4x sampling wrapper, on the coupled system.

    DOF-agnostic by construction -- the refinement is of the ground traces
    and of the output slicing, neither of which knows how many DOFs a floor
    has. Both components are resampled on the same grid so the pairing
    `pair_components` established survives the refinement.
    """
    p = {**HFTD_DEFAULTS, **(params or {})}
    a_x = np.asarray(ax, dtype=float)*p['intensity_scale']
    a_y = np.asarray(ay, dtype=float)*p['intensity_scale']
    bb_x = build_backbones(building.bx, **p)
    bb_y = build_backbones(building.by, **p)
    building.compute_response(a_x, np.asarray(dx, dtype=float)*p['intensity_scale'],
                              a_y, np.asarray(dy, dtype=float)*p['intensity_scale'],
                              dt, nonlinear=False)
    cdx, cdy = column_drift_3N(building.floor_disp_rel, building.column_positions)
    if (np.all(np.max(abs(cdx), axis=-1) <= bb_x['dy'])
            and np.all(np.max(abs(cdy), axis=-1) <= bb_y['dy'])):
        result = _solve_hftd_grid_3N(building, ax, dx, ay, dy, dt, p)
        result.summary['sampling_factor'] = 1
        result.solve_grid = _solve_grid(result, 1, dt, None)
        return result

    factor = 4
    fine = _refine_traces(building, [np.asarray(t, dtype=float)*p['intensity_scale']
                                     for t in (ax, dx, ay, dy)], dt, factor)
    result = _solve_hftd_grid_3N(building, *fine, dt/factor,
                                 {**p, 'intensity_scale': 1.})
    result.solve_grid = _solve_grid(result, factor, dt/factor, tuple(fine))
    building.compute_response(a_x, np.asarray(dx, dtype=float)*p['intensity_scale'],
                              a_y, np.asarray(dy, dtype=float)*p['intensity_scale'],
                              dt, nonlinear=False)
    for holder in (result, result.x, result.y):
        for name in ('u_rel', 'u_abs', 'story_drift', 'story_shear', 'k_t_ratio',
                     'p_nl', 'damage_state', 'column_damage', 'velocity',
                     'acceleration', 'column_force', 'column_tangent',
                     'column_drift'):
            value = getattr(holder, name, None)
            if value is not None:
                setattr(holder, name, value[..., ::factor].copy())
        holder.dt = dt
    result.x.u_abs = result.x.u_rel + np.asarray(dx, dtype=float)[None,:]*p['intensity_scale']
    result.y.u_abs = result.y.u_rel + np.asarray(dy, dtype=float)[None,:]*p['intensity_scale']
    result.x.backbones['params'] = p
    result.y.backbones['params'] = p
    predictor_iterations = result.summary['predictor_iterations']
    causality_error = result.summary['causality_error']
    result.collapse_events = []
    result.x.collapse_events, result.y.collapse_events = [], []
    evaluate_collapse_criteria(result.x, building.bx)
    evaluate_collapse_criteria(result.y, building.by)
    result.collapse_events = result.x.collapse_events + result.y.collapse_events
    result.summary.update(x=result.x.summary, y=result.y.summary,
                          collapse_events=result.collapse_events,
                          peak_rotation=float(np.max(abs(result.theta))),
                          residual_rotation=result.theta[:,-1].tolist(),
                          sampling_factor=factor,
                          predictor_iterations=predictor_iterations,
                          causality_error=causality_error)
    return result


# --- Spec 13: post-detachment re-solve --------------------------------------
#
# Story indices below are 0-based, like every spec-11 array. The 1-based
# `story` of the hand-off contract is produced once, in its builder.

def free_vibration(building, u0, v0, tau):
    """Closed-form zero-input response of each mode (spec 13 C-3).

        q_n(tau) = e^{-zeta w_n tau} [q_n(0) cos w_d tau
                   + (q'_n(0) + zeta w_n q_n(0))/w_d sin w_d tau]

    with q_n(0) = phi_n^T M u0 and q'_n(0) = phi_n^T M v0 (phi mass-
    normalised). Returns physical (u, v, a), each (ndof, len(tau)), on the
    building's own DOF ordering -- one function for both building classes.
    """
    z = building.zeta
    if not 0 <= z < 1:
        raise ValueError('free_vibration needs an underdamped modal set')
    w = building.omega_n[:, None]
    wd = w * np.sqrt(1 - z * z)
    q0 = (building.phi.T @ (building.M @ np.asarray(u0, dtype=float)))[:, None]
    qd0 = (building.phi.T @ (building.M @ np.asarray(v0, dtype=float)))[:, None]
    t = np.asarray(tau, dtype=float)[None, :]
    e, c, s = np.exp(-z * w * t), np.cos(wd * t), np.sin(wd * t)
    q = e * (q0 * c + (qd0 + z * w * q0) / wd * s)
    qd = e * (qd0 * c - (w * w * q0 + z * w * qd0) / wd * s)
    qdd = -2 * z * w * qd - w * w * q
    return building.phi @ q, building.phi @ qd, building.phi @ qdd


def ground_velocity(disp, dt):
    """Ground velocity as j*omega times the transform of the displacement.

    The trace is extended evenly (d, reversed d) first, so the periodic
    signal the DFT sees has no jump at the wrap. Never a time-domain
    difference of the displacement (spec 13 C-2, AGENTS.md).
    """
    d = np.asarray(disp, dtype=float)
    ext = np.concatenate([d, d[::-1]])
    w = 2 * np.pi * fftfreq(len(ext), dt)
    return np.real(ifft(1j * w * fft(ext)))[:len(d)]


def survivor_building(building, stories):
    """The building with only its lowest `stories` stories, RE-ASSEMBLED.

    Every per-floor array is sliced and the frame is condensed again from
    geometry; `K` is never sliced -- the original condensation baked the
    removed stories' restraint into the rows below (spec 13 A2). Mass and
    P-Delta loads follow from the sliced masses, so gravity is reduced.
    `k0_profile` is overridden to the original rows: surviving columns keep
    their frozen backbone, and the energy/coupling split must use the same
    k0 as the pseudo-force (C-5). Works for both building classes.
    """
    s = int(stories)
    if not 1 <= s < building.N:
        raise ValueError(f'survivor needs 1..{building.N - 1} stories, got {s}')
    src = building.bx if isinstance(building, MDOF_Building3N) else building
    m = np.asarray(building.m, dtype=float)
    kw = dict(mass_per_floor=m if m.ndim == 0 else m[:s].copy(), zeta=building.zeta,
              story_height=src.h[:s], column_depth_x=src.column_depth_x[:s],
              column_depth_y=src.column_depth_y[:s], beam_depth=src.beam_depth[:s],
              E=src.E, plan_span_x=src.plan_span_x, plan_span_y=src.plan_span_y,
              section_stiffness_mode=src.section_stiffness_mode,
              cracked_factor_column=src.cracked_factor_column,
              cracked_factor_beam=src.cracked_factor_beam, p_delta=src.p_delta,
              f_y=src.f_y, f_c=src.f_c, rho_longitudinal=src.rho_longitudinal,
              concrete_cover=src.concrete_cover, phi_axial=src.phi_axial,
              axial_cap_factor=src.axial_cap_factor)
    if isinstance(building, MDOF_Building3N):
        out = MDOF_Building3N(s, **kw)
        out.bx.k0_profile = building.bx.k0_profile[:s].copy()
        out.by.k0_profile = building.by.k0_profile[:s].copy()
        out.k0_profile_x, out.k0_profile_y = out.bx.k0_profile, out.by.k0_profile
    else:
        out = MDOF_ShearBuilding(s, axis=building.axis, **kw)
        out.k0_profile = building.k0_profile[:s].copy()
    return out


def slice_backbones(bb, stories):
    """Row slice of every (N, 4) backbone array -- the frozen backbone."""
    return {k: (v[:stories].copy() if isinstance(v, np.ndarray) else v)
            for k, v in bb.items()}


class _RestartConvolution:
    """The survivor's convolution operator, zero-input-corrected at tau=0.

    A pseudo-force placed after the restart is convolved on the FULL-record
    grid (so its band-limited response joins the fixed prefix without a
    truncation jump), and the closed-form free vibration of its own value
    and velocity at tau=0 is subtracted. Every iterate therefore starts from
    exactly the handed-over state: continuity is a property of the operator,
    not a patch on the stitched sample. Still linear, so the fixed point is
    unchanged.
    """

    def __init__(self, convolution, start, building, tau):
        self.convolution, self.start = convolution, start
        self.building, self.tau = building, tau
        self.compliance = convolution.compliance
        self.segments = convolution.segments
        self.padding_capped = convolution.padding_capped

    def apply(self, force, derivatives=False):
        full = np.zeros((force.shape[0], self.convolution.npts))
        full[:, self.start:] = force
        d, v, a = (x[:, self.start:] for x in self.convolution.apply(full, True))
        fu, fv, fa = free_vibration(self.building, -d[:, 0], -v[:, 0], self.tau)
        return d + fu, (v + fv if derivatives else None), (a + fa if derivatives else None)


def _restart_solve(survivor, constitutive, base_full, vel_full, acc_full, start,
                   prefix, u0, v0, dt, p, dy_min):
    """Survivor response from sample `start`: zero-state + zero-input (C-3).

    The particular (zero-state) part runs over the whole record on the
    survivor: its elastic response to the full ground traces, plus the
    convolution of `prefix`, the surviving columns' actual pseudo-force
    before the restart. Both are continuous across `start`, so nothing is
    truncated there. The closed-form free vibration of the survivor's modes
    then carries the state difference `u0 - w(start)`, `v0 - w'(start)`.
    Restricted to tau >= 0 that is exactly "free vibration from (u0, v0) +
    convolution of the forcing after the restart". A from-rest FFT solve
    that starts at the restart instead half-counts the first sample (a
    band-limited pulse centred on tau=0), which breaks velocity continuity
    by about a0*dt/2. The pseudo-force after `start` is found by the shared
    fixed point.
    """
    nf = base_full.shape[1]
    convolution = _HFTDConvolution(survivor, nf, dt, p['hftd_segment_seconds'])
    if prefix is not None and np.any(prefix):
        full = np.zeros_like(base_full)
        full[:, :start] = prefix
        d, v, a = convolution.apply(full, True)
        base_full, vel_full, acc_full = base_full + d, vel_full + v, acc_full + a
    tau = np.arange(nf - start) * dt
    fu, fv, fa = free_vibration(survivor, u0 - base_full[:, start],
                                v0 - vel_full[:, start], tau)
    operator = _RestartConvolution(convolution, start, survivor, tau)
    base = base_full[:, start:] + fu
    velocity = vel_full[:, start:] + fv
    acceleration = acc_full[:, start:] + fa
    # Continuity fixes u(0) = u0, so with the frozen state the pseudo-force
    # at tau=0 is already determined. Solve for the remainder g = p - p0,
    # which is 0 at tau=0: the operator's tau=0 correction then nearly
    # vanishes, so the plain causal predictor seeds it consistently (without
    # this the coupled survivor's seed missed by ~1e-3 and the outer
    # iteration stalled there). A change of variables, not of the problem.
    p0 = constitutive(np.asarray(u0, dtype=float)[:, None])[0]
    hold = None
    if np.any(p0):
        hold = np.repeat(p0, nf - start, axis=1)
        d, v, a = operator.apply(hold, True)
        base, velocity, acceleration = base + d, velocity + v, acceleration + a

    def remainder(u, state=None):
        candidate, aux = constitutive(u, state)
        return candidate - p0, aux

    force, u, aux, iteration, history, converged, predictor_iterations, causality_error = \
        _hftd_fixed_point(survivor, base, remainder, operator, dt, p, dy_min, offset=hold)
    if np.any(force):
        _, cv, ca = operator.apply(force, True)
        velocity, acceleration = velocity + cv, acceleration + ca
    if hold is not None:
        force = force + hold
    return dict(u=u, velocity=velocity, acceleration=acceleration, aux=aux,
                force=force, iterations=iteration, residual_history=history,
                converged=converged, predictor_iterations=predictor_iterations,
                causality_error=causality_error, segments=convolution.segments,
                padding_capped=convolution.padding_capped)


def _restart_axis(survivor, bb, state, traces, dt, start, prefix_defect, u0, v0, p):
    """Per-axis restart. `prefix_defect` is the story defect (s, start).
    All returned histories are relative to the ground and start at `start`."""
    a, d = traces
    survivor.compute_response(a, d, dt, nonlinear=False)
    prefix = None if prefix_defect is None else assemble_pseudo_force(prefix_defect)
    out = _restart_solve(survivor, _per_axis_constitutive(bb, state),
                         survivor.floor_disp_rel.copy(), _base_velocity(survivor, a, dt),
                         survivor.floor_accel_rel.copy(), start, prefix, u0, v0, dt, p,
                         float(np.min(bb['dy'])))
    return out


def _restart_3N(survivor, bb_x, bb_y, state, traces, dt, start, prefix_defect, u0, v0, p):
    """Coupled restart. `prefix_defect` is the per-column (ex, ey), (s, 4, start)."""
    ax, dx, ay, dy = traces
    survivor.compute_response(ax, dx, ay, dy, dt, nonlinear=False)
    positions = survivor.column_positions
    prefix = None if prefix_defect is None else assemble_pseudo_force_3N(*prefix_defect, positions)
    out = _restart_solve(survivor, _torsion_constitutive(bb_x, bb_y, positions, state),
                         survivor.floor_disp_rel.copy(), survivor.floor_vel_rel.copy(),
                         survivor.floor_accel_rel.copy(), start, prefix, u0, v0, dt, p,
                         float(min(np.min(bb_x['dy']), np.min(bb_y['dy']))))
    return out


MAX_DETACHMENT_EVENTS = 4   # TO VERIFY -- a budget, not physics (spec 13 A4)

# Per-story histories (first axis = story) that freeze for a detached story.
_STORY_ARRAYS = ('story_drift', 'story_shear', 'k_t_ratio', 'damage_state',
                 'column_damage', 'column_force', 'column_tangent', 'column_drift')


def _find_detachment(views, kg_hist, p_delta, stories, start):
    """Earliest (sample, story, axes) at which a story detaches (spec 13 A1).

    Every column of the story has failed -- |delta| > du at some sample so
    far, in global time, so columns that failed before a restart count --
    AND the summed tangent is <= P/h: spec 11's gravity criterion, with the
    P/h actually in force (reduced after a restart). With p_delta off that
    test degenerates to <= 0, which is stricter, not equivalent. Ties go to
    the lowest story; `axes` lists every axis tripping at that sample.
    """
    hits = {}
    for axis, v in views:
        broke = np.abs(v.column_drift[:stories]) > v.backbones['du'][:stories, :, None]
        failed = np.logical_or.accumulate(broke, axis=-1).all(axis=1)[:, start:]
        kt = v.column_tangent[:stories, :, start:].sum(axis=1)
        mask = failed & (kt <= (kg_hist[:stories, start:] if p_delta else 0.))
        for i in np.flatnonzero(mask.any(axis=1)):
            hits.setdefault((int(mask[i].argmax()) + start, int(i)), []).append(axis)
    if not hits:
        return None
    k, story = min(hits)
    return k, story, hits[(k, story)]


def _state_at(segment, k):
    """Hysteresis state after a segment's own samples before native `k`.

    Re-runs the constitutive law on the retained solve-grid history (4x
    when refined), not on downsampled drift, which would miss peaks
    between samples (plan R5).
    """
    local = (k - segment['k0']) * segment['factor']
    if local == 0:
        return segment['state0']
    return segment['law'](segment['u'][:, :local])[1].state


def _hold_rows(arrays, rows, k):
    """Floors/stories `rows` keep their value at sample k from then on."""
    for a in arrays:
        a[rows, ..., k + 1:] = a[rows, ..., k:k + 1]


def solve_with_detachment(building, accel_x, disp_x, accel_y=None, disp_y=None,
                          dt=None, building_y=None,
                          max_events=MAX_DETACHMENT_EVENTS, **params):
    """Nonlinear solve that restarts the surviving structure at detachment.

    `building` is an MDOF_Building3N (coupled, both components), or the X
    MDOF_ShearBuilding with an optional `building_y` for Y. The first pass
    is exactly `compute_response_nonlinear`; a run that never detaches is
    returned untouched, bit for bit.

    At the earliest detachment across axes, BOTH axes restart on the
    re-assembled survivor (C-4). Floors above the failure plane HOLD their
    absolute position (and theta) from the detachment sample on, with zero
    absolute velocity and acceleration. They are no longer structural, and
    a hold is bounded where NaN would poison every consumer (C3). Survivor
    segments are always solved on the 4x grid, with the fine traces of the
    original solve. Repeats until nothing detaches, story 1 detaches
    (nothing left to solve) or `max_events` binds (`cap_reached`). The
    result is installed on the buildings; the event list (0-based stories)
    is returned and kept as `.detachment`.
    """
    import dataclasses
    torsional = isinstance(building, MDOF_Building3N)
    F = 4
    started = perf_counter()
    if torsional:
        building.compute_response_nonlinear(accel_x, disp_x, accel_y, disp_y, dt, **params)
        first = building.hftd_result
        per = [('X', building.bx, first.x, accel_x, disp_x),
               ('Y', building.by, first.y, accel_y, disp_y)]
        owners = [building]
    else:
        building.compute_response_nonlinear(accel_x, disp_x, dt, **params)
        per = [('X', building, building.hftd_result, accel_x, disp_x)]
        if building_y is not None:
            building_y.compute_response_nonlinear(accel_y, disp_y, dt, **params)
            per.append(('Y', building_y, building_y.hftd_result, accel_y, disp_y))
        owners = [b for _, b, _, _, _ in per]
    p = {**HFTD_DEFAULTS, **building.nonlinear_params, **params}
    N, n = building.N, len(accel_x)
    converged = all(v.summary['converged'] for _, _, v, _, _ in per)
    info = dict(events=[], cap_reached=False, surviving_stories=N,
                converged=converged, reason='', segments=[],
                first_pass_seconds=perf_counter() - started)
    for b in owners:
        b.detachment = info
    kg_hist = np.repeat(np.asarray(per[0][1].k_g, dtype=float)[:, None], n, axis=1)
    p_delta = building.p_delta
    hit = _find_detachment([(a, v) for a, _, v, _, _ in per], kg_hist, p_delta, N, 0) \
        if converged else None
    if hit is None:
        return info

    scale = p['intensity_scale']
    ground = {a: dict(a=np.asarray(ag, dtype=float)*scale, d=np.asarray(dg, dtype=float)*scale)
              for a, _, _, ag, dg in per}
    for g in ground.values():
        g['v'] = ground_velocity(g['d'], dt)
    info['ground'] = ground

    # Stitched native histories: independent copies of the first pass.
    S = {}
    for a, b, v, _, _ in per:
        S[a] = {name: np.array(getattr(v, name)) for name in
                ('u_rel', 'u_abs', 'velocity', 'acceleration', 'p_nl') + _STORY_ARRAYS}
    # Fine-grid per-column defect k0*delta - V: the survivor's fixed prefix.
    grids = [first.solve_grid] if torsional else [v.solve_grid for _, _, v, _, _ in per]
    defect = {}
    for j, (a, b, v, _, _) in enumerate(per):
        g = grids[0] if torsional else grids[j]
        c = j if torsional else 0
        nf = (n - 1) * F + 1
        defect[a] = (v.backbones['k0'][:, :, None] * g['column_drift'][c] - g['column_force'][c]
                     if g['factor'] == F else np.zeros((N, 4, nf)))
    if torsional:
        g = first.solve_grid
        fine = g['traces'] if g['factor'] == F else tuple(_refine_traces(
            building, [ground[a][q] for a in 'XY' for q in 'ad'], dt, F))
        S3 = dict(u=np.array(first.u_rel), v=np.array(first.velocity),
                  a=np.array(first.acceleration), p=np.array(first.p_nl))
        positions = building.column_positions
        segments = {'3N': dict(k0=0, factor=g['factor'], u=g['u'], state0=None,
                               law=_torsion_constitutive(first.x.backbones, first.y.backbones, positions))}
    else:
        fine, segments = {}, {}
        for (a, b, v, _, _), g in zip(per, grids):
            fine[a] = g['traces'] if g['factor'] == F else tuple(_refine_traces(
                b, (ground[a]['a'], ground[a]['d']), dt, F))
            segments[a] = dict(k0=0, factor=g['factor'], u=g['u'], state0=None,
                               law=_per_axis_constitutive(v.backbones))

    def fresh_or(state, bb, s):
        return ColumnHysteresis(slice_backbones(bb, s)) if state is None \
            else slice_hysteresis_state(state, s)

    s_cur = N
    while hit is not None:
        k, story, axes = hit
        if len(info['events']) >= max_events:
            info['cap_reached'] = True
            break
        info['events'].append(dict(story=story, k=k, time=float(k * dt), axis=axes[0],
                                   axes_tripped=list(axes), stories_before=s_cur))
        s_cur = story
        info['surviving_stories'] = story
        rows = slice(story, N)
        # Hold: absolute position (and theta) frozen, absolute velocity and
        # acceleration zero; per-story histories of detached stories frozen.
        for a, _, _, _, _ in per:
            A, g = S[a], ground[a]
            A['u_abs'][rows, k + 1:] = A['u_abs'][rows, k:k + 1]
            A['u_rel'][rows, k + 1:] = A['u_abs'][rows, k + 1:] - g['d'][None, k + 1:]
            A['velocity'][rows, k + 1:] = -g['v'][None, k + 1:]
            A['acceleration'][rows, k + 1:] = 0.
            _hold_rows([A['p_nl']] + [A[x] for x in _STORY_ARRAYS], rows, k)
        if torsional:
            for c, a in enumerate(('X', 'Y')):
                blk = slice(c * N + story, (c + 1) * N)
                S3['u'][blk, k + 1:] = S[a]['u_rel'][rows, k + 1:]
                S3['v'][blk, k + 1:] = S[a]['velocity'][rows, k + 1:]
                S3['a'][blk, k + 1:] = -ground[a]['a'][None, k + 1:]
            th = slice(2 * N + story, 3 * N)
            _hold_rows([S3['u']], th, k)
            _hold_rows([S3['p']], np.r_[story:N, N + story:2 * N, 2 * N + story:3 * N], k)
            S3['v'][th, k + 1:] = 0.
            S3['a'][th, k + 1:] = 0.
        if story == 0:
            break

        K = F * k
        seg_log = dict(k=k, stories=story)
        tick = perf_counter()
        if torsional:
            surv = survivor_building(building, story)
            bbx = slice_backbones(first.x.backbones, story)
            bby = slice_backbones(first.y.backbones, story)
            st = _state_at(segments['3N'], k)
            state = (fresh_or(None if st is None else st[0], first.x.backbones, story),
                     fresh_or(None if st is None else st[1], first.y.backbones, story))
            idx = np.r_[0:story, N:N + story, 2 * N:2 * N + story]
            out = _restart_3N(surv, bbx, bby, state, fine, dt / F, K,
                              (defect['X'][:story, :, :K], defect['Y'][:story, :, :K]),
                              S3['u'][idx, k], S3['v'][idx, k], p)
            outs = {'3N': out}
            S3['u'][idx, k:] = out['u'][:, ::F]
            S3['v'][idx, k:] = out['velocity'][:, ::F]
            S3['a'][idx, k:] = out['acceleration'][:, ::F]
            S3['p'][idx, k:] = out['force'][:, ::F]
            for c, (a, b, v, _, _) in enumerate(per):
                A, blk = S[a], slice(c * story, (c + 1) * story)
                A['u_rel'][:story, k:] = out['u'][blk, ::F]
                A['velocity'][:story, k:] = out['velocity'][blk, ::F]
                A['acceleration'][:story, k:] = (out['acceleration'][blk] + fine[2 * c][None, K:])[:, ::F]
                A['p_nl'][:story, k:] = out['force'][blk, ::F]
            aux_of = {a: tuple(getattr(out['aux'], f)[c] for f in
                               ('story_drift', 'column_drift', 'force', 'tangent', 'damage'))
                      for c, a in enumerate(('X', 'Y'))}
            segments['3N'] = dict(k0=k, factor=F, u=out['u'], state0=state,
                                  law=_torsion_constitutive(bbx, bby, surv.column_positions, state))
            kg_new = surv.k_g
        else:
            outs, aux_of = {}, {}
            for a, b, v, _, _ in per:
                surv = survivor_building(b, story)
                bb = slice_backbones(v.backbones, story)
                state = fresh_or(_state_at(segments[a], k), v.backbones, story)
                A = S[a]
                out = _restart_axis(surv, bb, state, fine[a], dt / F, K,
                                    defect[a][:story, :, :K].sum(axis=1),
                                    A['u_rel'][:story, k], A['velocity'][:story, k], p)
                outs[a] = out
                A['u_rel'][:story, k:] = out['u'][:, ::F]
                A['velocity'][:story, k:] = out['velocity'][:, ::F]
                A['acceleration'][:story, k:] = (out['acceleration'] + fine[a][0][None, K:])[:, ::F]
                A['p_nl'][:story, k:] = out['force'][:, ::F]
                x = out['aux']
                aux_of[a] = (x.story_drift, x.column_drift, x.force, x.tangent, x.damage)
                segments[a] = dict(k0=k, factor=F, u=out['u'], state0=state,
                                   law=_per_axis_constitutive(bb, state))
                kg_new = surv.k_g
        seg_log['seconds'] = perf_counter() - tick
        seg_log['iterations'] = {a: o['iterations'] for a, o in outs.items()}
        seg_log['converged'] = all(o['converged'] for o in outs.values())
        seg_log['residual_history'] = {a: o['residual_history'] for a, o in outs.items()}
        seg_log['predictor_iterations'] = {a: o['predictor_iterations'] for a, o in outs.items()}
        info['segments'].append(seg_log)
        for a, b, v, _, _ in per:
            A = S[a]
            drift, col, force, tangent, damage = aux_of[a]
            A['u_abs'][:story, k:] = A['u_rel'][:story, k:] + ground[a]['d'][None, k:]
            A['story_drift'][:story, k:] = drift[:, ::F]
            A['column_drift'][:story, :, k:] = col[..., ::F]
            A['column_force'][:story, :, k:] = force[..., ::F]
            A['column_tangent'][:story, :, k:] = tangent[..., ::F]
            A['column_damage'][:story, :, k:] = damage[..., ::F]
            A['story_shear'][:story, k:] = force.sum(axis=1)[:, ::F]
            A['k_t_ratio'][:story, k:] = (tangent.sum(axis=1) / b.k0_profile[:story, None])[:, ::F]
            A['damage_state'][:story, k:] = np.max(damage, axis=1)[:, ::F]
            defect[a][:story, :, K:] = v.backbones['k0'][:story, :, None] * col - force
        kg_hist[:story, k:] = np.asarray(kg_new, dtype=float)[:, None]
        if not seg_log['converged']:
            info['converged'] = False
            info['reason'] = ('Post-detachment re-solve did not converge; '
                              'no further detachment is inferred.')
            break
        views = [(a, SimpleNamespace(column_drift=S[a]['column_drift'],
                                     column_tangent=S[a]['column_tangent'],
                                     backbones=v.backbones)) for a, _, v, _, _ in per]
        hit = _find_detachment(views, kg_hist, p_delta, story, k)

    held = [None] * N
    for e in info['events']:
        for i in range(e['story'], N):
            held[i] = e['k'] if held[i] is None else held[i]
    for _, b, _, _, _ in per:
        b.held_from = held
    # Package: the stitched histories replace the first pass on each view,
    # and spec 11's criteria are re-evaluated on them with the P/h in force.
    results = {}
    for a, b, v, _, _ in per:
        r = dataclasses.replace(v, **S[a], collapse_events=[], summary={},
                                converged=info['converged'], solve_grid=None,
                                reason=info['reason'] or v.reason)
        evaluate_collapse_criteria(r, b, k_g=kg_hist)
        for key in ('sampling_factor', 'predictor_iterations', 'causality_error'):
            if key in v.summary:
                r.summary[key] = v.summary[key]
        results[a] = r
    if torsional:
        rx, ry = results['X'], results['Y']
        r3 = dataclasses.replace(first, u_rel=S3['u'], velocity=S3['v'], acceleration=S3['a'],
                                 p_nl=S3['p'], x=rx, y=ry, converged=info['converged'],
                                 reason=info['reason'] or first.reason, solve_grid=None,
                                 collapse_events=rx.collapse_events + ry.collapse_events)
        r3.summary = {**first.summary, 'x': rx.summary, 'y': ry.summary,
                      'converged': info['converged'], 'reason': r3.reason,
                      'collapse_events': r3.collapse_events,
                      'peak_rotation': float(np.max(abs(r3.theta))),
                      'residual_rotation': r3.theta[:, -1].tolist()}
        building._install_nonlinear(r3)
    else:
        for a, b, _, _, _ in per:
            b._install_nonlinear(results[a])
    return info


HANDOFF_VERSION = 1

HANDOFF_EVENT_FIELDS = (
    'story', 't_detach', 'axis', 'axes_tripped', 'direction', 'criterion',
    'failed_columns', 'surviving_columns', 'hinge_column', 'floor_state_frame',
    'floor_state', 'ground_state', 'upper_mass', 'upper_cm_height',
    'upper_inertia_cm', 'story_height_at_failure', 'P_cap_below',
    'residual_drift_below', 'surviving_stories')


def build_detachment_events(building, building_y=None):
    """The versioned hand-off contract, one dict per event (spec 13 C2).

    Everything the animation may use, in metres / seconds / radians in the
    physics frame (never scene units). This is the ONE place where 0-based
    story indices become the contract's 1-based `story` / `floor`; `column`
    stays 0-based, indexing `column_plan_positions()` (CCW from +x,+y).

    - `floor_state` is ABSOLUTE (`floor_state_frame`), matching the payload's
      floor arrays; `ground_state` beside it lets a consumer change frame.
      `z` is 0.0 (no vertical DOF); `theta_z`/`omega_z` are 0.0 off the
      torsional path.
    - `surviving_columns` is empty by definition: detachment requires every
      column of the story to have failed (A1). What the simulation does
      determine is failure ORDER, so `hinge_column` names the column that
      failed last on the tripped axis (held longest), and each failed
      column carries `t_fail_x`/`t_fail_y` (null where that axis had not
      failed by `t_detach`).
    - `upper_cm_height` is measured from the failure plane (the top of floor
      m-1, where the block lands after dropping `story_height_at_failure`).
    - `P_cap_below` / `residual_drift_below` are null only for story 1.
    """
    info = getattr(building, 'detachment', None)
    if not info or not info['converged'] or not info['events']:
        return []
    torsional = isinstance(building, MDOF_Building3N)
    N = building.N
    if torsional:
        r = building.hftd_result
        views, src = {'X': r.x, 'Y': r.y}, building.bx
        theta, omega = r.theta, r.velocity[2 * N:]
    else:
        views, src = {'X': building.hftd_result}, building
        if building_y is not None:
            views['Y'] = building_y.hftd_result
        theta = omega = None
    ground = info['ground']
    h = np.asarray(src.h, dtype=float)
    z = np.cumsum(h)
    m = np.broadcast_to(np.asarray(building.m, dtype=float), (N,))
    a, b = float(src.plan_span_x), float(src.plan_span_y)
    positions = column_plan_positions(a, b)
    P_cap = np.broadcast_to(np.asarray(src.P_cap_profile, dtype=float), (N,))
    events = []
    for e in info['events']:
        s, k, t_d, ax = e['story'], e['k'], e['time'], e['axis']
        v = views[ax]
        tf = {A: views[A].summary['t_fail'][s] for A in views}

        def by_detach(t):
            return None if t is None or t > t_d else float(t)

        failed = [dict(story=s + 1, column=j, t_fail=float(tf[ax][j]),
                       t_fail_x=by_detach(tf['X'][j]),
                       t_fail_y=by_detach(tf['Y'][j]) if 'Y' in tf else None,
                       plan_x=float(positions[j, 0]), plan_y=float(positions[j, 1]))
                  for j in range(4)]
        hinge = max(range(4), key=lambda j: (tf[ax][j], -j))
        heights = z[s:] - (z[s - 1] if s else 0.0)
        mass = float(m[s:].sum())
        h_cm = float((m[s:] * heights).sum() / mass)

        def lateral(A, f):
            if A not in views:
                return 0.0, 0.0
            V = views[A]
            return float(V.u_abs[f, k]), float(V.velocity[f, k] + ground[A]['v'][k])

        floors = []
        for f in range(s, N):
            x, vx = lateral('X', f)
            y, vy = lateral('Y', f)
            floors.append(dict(floor=f + 1, x=x, y=y, z=0.0,
                               theta_z=float(theta[f, k]) if torsional else 0.0,
                               vx=vx, vy=vy,
                               omega_z=float(omega[f, k]) if torsional else 0.0))
        gs = {q: 0.0 for q in ('x', 'y', 'vx', 'vy')}
        for A, key in (('X', 'x'), ('Y', 'y')):
            if A in ground:
                gs[key] = float(ground[A]['d'][k])
                gs['v' + key] = float(ground[A]['v'][k])
        residual = None
        if s:
            residual = {A.lower(): float(views[A].story_drift[s - 1, -1]) if A in views else 0.0
                        for A in ('X', 'Y')}
        drift = v.story_drift[s, k]
        events.append(dict(
            story=s + 1, t_detach=float(t_d), axis=ax, axes_tripped=list(e['axes_tripped']),
            direction=1 if drift >= 0 else -1, criterion=v.summary['collapse_criterion'][s],
            failed_columns=failed, surviving_columns=[], hinge_column=hinge,
            floor_state_frame='absolute', floor_state=floors, ground_state=gs,
            upper_mass=mass, upper_cm_height=h_cm,
            upper_inertia_cm=float((m[s:] * ((a * a + b * b) / 12 + (heights - h_cm) ** 2)).sum()),
            story_height_at_failure=float(h[s]),
            P_cap_below=float(P_cap[s - 1]) if s else None,
            residual_drift_below=residual, surviving_stories=s))
    return events


def handoff_header(building, building_y=None):
    """The `collapse.*` keys spec 13 adds to the /compute header."""
    info = getattr(building, 'detachment', None) or {}
    ok = info.get('converged', False)
    return dict(handoff_version=HANDOFF_VERSION,
                detachment_events=build_detachment_events(building, building_y),
                cap_reached=bool(info.get('cap_reached', False)) and ok,
                surviving_stories=int(info.get('surviving_stories', building.N)) if ok else building.N)


def validate_handoff(collapse):
    """Reference consumer: refuse an unknown version rather than guess.

    Spec 15's JavaScript mirrors this. Raises ValueError on a version it
    does not know or a missing field; returns the event list otherwise.
    """
    version = collapse.get('handoff_version')
    if version != HANDOFF_VERSION:
        raise ValueError(f'unknown handoff_version {version!r}; this consumer reads '
                         f'version {HANDOFF_VERSION} only')
    for key in ('detachment_events', 'cap_reached', 'surviving_stories'):
        if key not in collapse:
            raise ValueError(f'hand-off contract is missing {key!r}')
    for event in collapse['detachment_events']:
        missing = [key for key in HANDOFF_EVENT_FIELDS if key not in event]
        if missing:
            raise ValueError(f'detachment event is missing {missing}')
    return collapse['detachment_events']


def slice_hysteresis_state(machine, stories):
    """Row slice of a ColumnHysteresis state; peaks and slopes untouched."""
    out = copy.copy(machine)
    for key, value in vars(machine).items():
        if isinstance(value, np.ndarray):
            setattr(out, key, value[:stories].copy())
    out.bb = slice_backbones(machine.bb, stories)
    return out


class MDOF_ShearBuilding:
    """
    Multi-degree-of-freedom building model: one translational DOF per
    floor (the "shear building" name still describes this shape -- a
    single lateral DOF per floor feeding the same modal-superposition/FFT
    machinery below), fixed at the base, genuinely free at the roof.

    As of spec 5, `K` is no longer derived backward from a target period
    -- it comes from real column/beam section properties via the
    matrix-stiffness method with static condensation (see
    assemble_frame_stiffness/condense_rotations/build_condensed_K above).
    `M` is unchanged: column/beam self-mass is neglected (spec A2),
    `mass_per_floor` stays the single source of floor mass.

    X and Y sway are no longer isotropic -- a rectangular column is
    stiffer one way than the other, so each axis needs its OWN instance
    (`axis="X"` or `axis="Y"`), each with its own independently condensed
    `K`. No torsional coupling between the two axes (deliberately excluded
    -- spec A1).
    """

    def __init__(self, num_stories, mass_per_floor=1000e3, zeta=0.05,
                 story_height=3.5, column_depth_x=1.10, column_depth_y=1.10,
                 beam_depth=1.50, axis="X", E=E_CONCRETE,
                 plan_span_x=PLAN_SPAN_X, plan_span_y=PLAN_SPAN_Y,
                 section_stiffness_mode=DEFAULT_SECTION_STIFFNESS_MODE,
                 cracked_factor_column=None, cracked_factor_beam=None,
                 p_delta=True,
                 f_y=F_Y_STEEL, f_c=F_C_CONCRETE,
                 rho_longitudinal=RHO_LONGITUDINAL,
                 concrete_cover=CONCRETE_COVER, phi_axial=PHI_AXIAL,
                 axial_cap_factor=AXIAL_CAP_FACTOR, nonlinear=False, **nonlinear_params):
        unknown = set(nonlinear_params) - set(HFTD_DEFAULTS) - {'record'}
        if unknown:
            raise TypeError(f'Unknown nonlinear parameters: {sorted(unknown)}')
        self.nonlinear = bool(nonlinear)
        self.nonlinear_params = nonlinear_params
        self.N = num_stories
        self.m = mass_per_floor
        self.zeta = zeta
        # Per-floor property arrays (spec 10, C1). Each of these may be
        # passed as a scalar -- broadcast to every story, which is the
        # pre-spec-10 behaviour reproduced bit-for-bit -- or as a length-N
        # sequence, which is how a soft ground story and localised damage
        # are expressed. `.copy()` so a caller's list/array can't alias in.
        self.h = self._per_floor(story_height, "story_height")
        self.column_depth_x = self._per_floor(column_depth_x, "column_depth_x")
        self.column_depth_y = self._per_floor(column_depth_y, "column_depth_y")
        self.beam_depth = self._per_floor(beam_depth, "beam_depth")
        self.total_height = float(self.h.sum())
        self.axis = axis
        self.E = E
        self.plan_span_x = plan_span_x
        self.plan_span_y = plan_span_y

        # Cracked-section stiffness (spec 10, Part A). The preset supplies
        # both factors; an explicit cracked_factor_* overrides its half,
        # so a caller can sweep one factor without inventing a preset.
        if section_stiffness_mode not in SECTION_STIFFNESS_PRESETS:
            raise ValueError(
                f"section_stiffness_mode must be one of "
                f"{sorted(SECTION_STIFFNESS_PRESETS)}, got "
                f"{section_stiffness_mode!r}")
        preset_c, preset_b = SECTION_STIFFNESS_PRESETS[section_stiffness_mode]
        self.section_stiffness_mode = section_stiffness_mode
        self.cracked_factor_column = float(
            preset_c if cracked_factor_column is None else cracked_factor_column)
        self.cracked_factor_beam = float(
            preset_b if cracked_factor_beam is None else cracked_factor_beam)
        self.p_delta = bool(p_delta)
        self.gravity_unstable = False

        # RC material properties (spec 10, D1) -- kwargs so spec 18's
        # generator can vary them. They affect capacities only, never the
        # elastic solve.
        self.f_y = float(f_y)
        self.f_c = float(f_c)
        self.rho_longitudinal = float(rho_longitudinal)
        self.concrete_cover = float(concrete_cover)
        self.phi_axial = float(phi_axial)
        self.axial_cap_factor = float(axial_cap_factor)

        self._build_matrices()
        self._modal_analysis()
        self._stability_coefficients()

    def _per_floor(self, value, name):
        """Normalise a scalar-or-length-N property to a length-N float array.

        A wrong-length profile is rejected rather than truncated or
        recycled: a truncated profile is a *different building* that still
        renders plausibly, which is precisely the class of bug that is
        impossible to spot on screen (see server.py's 400 on the same
        condition).
        """
        arr = np.asarray(value, dtype=float)
        if arr.ndim == 0:
            arr = np.broadcast_to(arr, (self.N,))
        elif arr.shape != (self.N,):
            raise ValueError(
                f"{name} must be a scalar or a length-{self.N} sequence, "
                f"got shape {arr.shape}")
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"{name} must be finite, got {value!r}")
        return arr.copy()

    def _build_matrices(self):
        self.M = np.eye(self.N) * self.m

        # EFFECTIVE (cracked) second moments -- the knock-down is applied
        # here, where the structural model is assembled, and deliberately
        # NOT inside column_inertia()/beam_inertia(), which are honest
        # geometry functions with independent callers
        # (claude_scripts/verify_frame_furniture.py, calibrate_frame.py,
        # frame_story_stiffness_closed_form's callers) that would all
        # silently change meaning. Spec 10, A1. With the "gross" preset
        # both factors are exactly 1.0, so this is bit-identical to the
        # pre-spec-10 line it replaces.
        self.I_c = self.cracked_factor_column * column_inertia(
            self.column_depth_x, self.column_depth_y, self.axis)
        self.I_b = self.cracked_factor_beam * beam_inertia(
            self.beam_depth, BEAM_WIDTH)
        self.L = frame_span(self.axis, self.plan_span_x, self.plan_span_y)

        # Elastic (cracked) frame stiffness, before gravity. Kept as its
        # own attribute because spec 11 needs the undamaged story
        # stiffnesses separately, and every verification check that
        # isolates P-Delta needs the "before" matrix.
        self.K_no_pdelta = build_condensed_K(
            self.N, self.E, self.I_c, self.I_b, self.h, self.L)

        # K_G depends only on masses and story heights -- never on the
        # response -- so it is constant and everything downstream (the
        # eigensolve, H_n(jw), the FFT solve, the participation factors,
        # transfer_function() and its JS mirror) is untouched in form.
        self.K_G, self.k_g, self.P_gravity = geometric_stiffness_matrix(
            self.N, self.m, self.h)

        if self.p_delta:
            self.K = self.K_no_pdelta - self.K_G
        else:
            # Assigned directly, not via a zero-scaled subtraction, so the
            # regression path is bit-identical rather than merely close.
            self.K = self.K_no_pdelta

        # Base-story (floor 1) condensed lateral stiffness -- useful
        # metadata (period readout, sanity display), but explicitly NOT a
        # universal "k": unlike the old abstract-spring model, every
        # floor's diagonal entry in a condensed frame stiffness matrix is
        # generally different (see verification check 3's "no spurious
        # long-range coupling" wording, not exact tridiagonality).
        self.story_stiffness = float(self.K[0, 0])

    def _elastic_k0(self):
        """Per-story elastic stiffness k0,i, from `K_no_pdelta`.

        **The denominator is the ELASTIC (pre-gravity) stiffness, not
        `self.K`.** Spec 10 B4 writes the pushover as `K_L u = s` with
        `K_L = K_cracked - K_G`, but that contradicts B5's own stated
        meaning of theta ("how much of this story's stiffness gravity has
        already eaten") and breaks verification check 6's exact N=1
        identity. With k0 from K_L you get k_g/k_eff = theta/(1-theta)
        instead of theta -- measured as a 7e-6 relative miss on that
        identity, which is what exposed it. ASCE 7's theta uses the
        first-order stiffness too, and spec 11's `k_t,i <= P_i/h_i`
        criterion wants a tangent stiffness that gravity has not already
        been subtracted from. See progress.md's ruling for spec 10 Task 3.

        Using K_no_pdelta's own first mode (rather than self.phi[:, 0])
        also makes k0 a pure property of the elastic frame: toggling
        p_delta no longer perturbs it, so theta stays comparable across
        that toggle.
        """
        eigvals, eigvecs = scipy_eigh(self.K_no_pdelta, self.M, lower=True)
        phi1 = eigvecs[:, np.argsort(eigvals)[0]]
        return story_stiffness_profile(self.K_no_pdelta, self.M, phi1)

    def _weakest_story(self):
        """0-indexed story with the largest k_g,i / k0,i -- the one gravity
        ate first, reported by GravityInstabilityError so a caller can say
        *where*, not just *that*.

        Runs on the error path, where `self.K` is the indefinite matrix
        whose eigenproblem just failed, so `_elastic_k0()`'s use of
        `K_no_pdelta` is load-bearing here and not just a preference.
        Falls back to the diagonal if even that solve fails, so the
        diagnostic can never itself raise and mask the real error.
        """
        try:
            k0 = self._elastic_k0()
        except Exception:  # noqa: BLE001 -- diagnostics must not raise
            k0 = np.diag(self.K_no_pdelta)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(k0 > 0, self.k_g / k0, np.inf)
        return int(np.argmax(ratio))

    def _stability_coefficients(self):
        """Per-story stiffness k0,i and the response-independent stability
        coefficient theta_stiffness,i = k_g,i / k0,i (spec 10, B4/B5).

        theta_stiffness reads directly as "how much of this story's
        stiffness gravity has already eaten", and it is exactly the
        quantity spec 11's collapse criterion (k_t,i <= P_i/h_i)
        generalises, with the *tangent* stiffness in the denominator.

        It is NOT monotonically decreasing with height for this model, and
        that is real: story 1's columns are fixed at the base while every
        story above has a rotating joint at both ends, so k0,1 is markedly
        the largest and theta peaks at story 2. Verification check 6
        originally asserted a story-1 peak; that assumed a uniform k0 and
        was corrected to report the peak instead.

        The response-dependent theta_demand needs a solved time history and
        is computed by compute_response() instead.
        """
        self.k0_profile = self._elastic_k0()
        self.theta_stiffness = self.k_g / self.k0_profile

        # Derived member capacities (spec 10, Part D). None of this touches
        # the elastic solve -- it exists so spec 11's backbones and spec
        # 15's impact trigger read capacities off the same geometry the
        # stiffness came from. Each inherits column_plastic_moment's
        # axial-load (P-M interaction) simplification.
        self.M_p_profile = column_plastic_moment(
            self.column_depth_x, self.column_depth_y, self.axis,
            f_y=self.f_y, f_c=self.f_c, rho=self.rho_longitudinal,
            cover=self.concrete_cover)
        self.V_p_profile = story_plastic_shear(
            self.column_depth_x, self.column_depth_y, self.axis, self.h,
            f_y=self.f_y, f_c=self.f_c, rho=self.rho_longitudinal,
            cover=self.concrete_cover)
        self.P_cap_profile = column_axial_capacity(
            self.column_depth_x, self.column_depth_y,
            f_y=self.f_y, f_c=self.f_c, rho=self.rho_longitudinal,
            phi=self.phi_axial, cap_factor=self.axial_cap_factor)

        # Story yield shear == plastic shear capacity (spec D5), so the
        # yield drift follows from the elastic story stiffness.
        self.delta_y_profile = self.V_p_profile / self.k0_profile

    def _modal_analysis(self):
        """Solve generalized eigenvalue problem: K φ = ω² M φ using scipy.linalg.eigh."""
        # Use SciPy's eigh which supports the b matrix for generalized problems
        eigvals, eigvecs = scipy_eigh(self.K, self.M, lower=True)

        # Sort by ascending eigenvalues
        idx = np.argsort(eigvals)

        # Gravity-instability guard (spec 10, B3) -- BEFORE np.sqrt, which
        # is the whole point. eigh needs M positive definite, not K, so an
        # indefinite K_L comes back as a negative eigenvalue rather than an
        # error, and sqrt would quietly turn it into nan. See
        # GravityInstabilityError's docstring for where that nan ends up.
        if eigvals[idx][0] <= 0.0:
            self.gravity_unstable = True
            raise GravityInstabilityError(
                f"Gravity exceeds lateral stiffness: the building cannot "
                f"stand under its own weight at these parameters "
                f"(story {self._weakest_story() + 1} of {self.N}, axis "
                f"{self.axis}; smallest eigenvalue "
                f"{float(eigvals[idx][0]):.6e} <= 0). Reduce the mass or "
                f"story height, or increase the column depth.",
                story=self._weakest_story())

        self.omega_n = np.sqrt(eigvals[idx])
        self.phi = eigvecs[:, idx]

        # Mass-normalize mode shapes (phi^T M phi = 1)
        for i in range(self.N):
            norm = np.sqrt(np.dot(self.phi[:, i].conj(), self.M @ self.phi[:, i]))
            self.phi[:, i] /= norm

        # Modal participation factors: Gamma_i = phi_i^T M 1
        ones = np.ones(self.N)
        self.Gamma = np.zeros(self.N)
        for i in range(self.N):
            self.Gamma[i] = np.dot(self.phi[:, i], self.M @ ones)

    def compute_response(self, acceleration, displacement, dt, nonlinear=None):
        """
        Compute floor displacement time histories given ground acceleration and displacement.
        """
        if nonlinear is True or (nonlinear is None and self.nonlinear):
            return self.compute_response_nonlinear(acceleration, displacement, dt)
        self.accel = acceleration
        self.ground_disp = displacement
        self.dt = dt
        self.npts = len(acceleration)
        self.time = np.arange(self.npts) * dt

        # Zero-pad before the FFT to avoid circular-convolution wraparound:
        # fft/ifft on the raw record computes a *circular* convolution, and
        # a lightly-damped mode's impulse response can easily outlast the
        # record, so its decaying tail wraps around and contaminates the
        # start of the response. Pad to cover ~5 time constants of the
        # slowest (lowest-frequency) mode's decay, capped at 4x the original
        # length so a low-damping/long-period parameter combination can't
        # blow up compute time for live recompute (specs/03-live-archetypes.md).
        settle_time = 5.0 / (self.zeta * self.omega_n[0])
        pad_len = min(4 * self.npts, self.npts + int(np.ceil(settle_time / dt)))

        accel_padded = np.zeros(pad_len)
        accel_padded[:self.npts] = acceleration

        A_fft = fft(accel_padded)
        freqs = fftfreq(pad_len, dt)
        omega = 2 * np.pi * freqs

        q_time = np.zeros((self.N, pad_len))
        qacc_time = np.zeros((self.N, pad_len))
        for i in range(self.N):
            wn = self.omega_n[i]
            z = self.zeta
            Gamma_i = self.Gamma[i]
            denom = (wn**2 - omega**2 + 1j * 2 * z * wn * omega)
            H = -Gamma_i / denom
            Q_fft = H * A_fft
            q = np.real(ifft(Q_fft))
            q_time[i, :] = q

            # Floor absolute acceleration (spec 5, Part A): furniture
            # (Part B) needs each floor's x_b''(t). Get it by multiplying
            # this mode's already-computed Q(jw) by -omega^2 before the
            # inverse FFT -- the Fourier-transform "differentiate in time"
            # property applied twice (d^2/dt^2 <-> (j*omega)^2 = -omega^2),
            # not a np.gradient shortcut. This has to happen HERE, on
            # Q_fft, rather than by re-differentiating the trimmed,
            # time-domain floor_disp_abs afterward with
            # _differentiate_to_accel's drift-removal filter, which would
            # distort the result. One extra ifft per mode -- negligible
            # against the ~20-50ms budget this already runs in.
            Qacc_fft = -(omega**2) * Q_fft
            qacc = np.real(ifft(Qacc_fft))
            qacc_time[i, :] = qacc

        # Trim back to the original record length now that the padding has
        # done its job of keeping the decaying tail out of the wraparound.
        q_time = q_time[:, :self.npts]
        qacc_time = qacc_time[:, :self.npts]

        self.floor_disp_rel = np.zeros((self.N, self.npts))
        self.floor_accel_rel = np.zeros((self.N, self.npts))
        for i in range(self.N):
            self.floor_disp_rel += np.outer(self.phi[:, i], q_time[i, :])
            self.floor_accel_rel += np.outer(self.phi[:, i], qacc_time[i, :])

        self.floor_disp_abs = self.floor_disp_rel + self.ground_disp[np.newaxis, :]
        self.floor_accel_abs = self.floor_accel_rel + self.accel[np.newaxis, :]

        self._demand_stability_coefficients()

        return self.time, self.ground_disp, self.floor_disp_rel, self.floor_disp_abs

    def compute_response_nonlinear(self, acceleration, displacement, dt, **params):
        result = solve_hftd(self, acceleration, displacement, dt,
                            {**self.nonlinear_params, **params})
        return self._install_nonlinear(result)

    def _install_nonlinear(self, result):
        self.hftd_result = result
        self.floor_disp_rel = result.u_rel
        self.floor_disp_abs = result.u_abs
        self.floor_accel_abs = result.acceleration
        self.floor_accel_rel = result.acceleration-self.accel[None,:]
        self._demand_stability_coefficients()
        return self.time, self.ground_disp, self.floor_disp_rel, self.floor_disp_abs

    def _demand_stability_coefficients(self):
        """Response-dependent drift and stability coefficient (spec 10, B5):

            theta_demand,i = P_i * delta_i,max / (V_i(t*) * h_i * C_d)
            t* = argmax_t |delta_i(t)|
            V_i(t) = sum_{j >= i} m_j * u_j''_abs(t)

        The story shear comes straight from `floor_accel_abs`, which
        compute_response() already produces for the furniture subsystem --
        **no new time-domain differentiation is introduced**, which matters
        because re-differentiating the trimmed floor displacement would run
        it back through the drift-removal filter and distort it.

        C_d is exactly 1.0 (C_D_DEFLECTION_AMPLIFICATION): this model is
        not code-designed and has no deflection amplification factor, and
        inventing one would be a fabricated number.

        Magnitudes are taken of the shear because the drift peak and the
        shear can carry either sign; theta is a ratio of magnitudes.
        """
        # Story drift from RELATIVE displacement (drift is relative by
        # definition; the absolute arrays carry the ground motion, which
        # would swamp it).
        u = self.floor_disp_rel
        drift = np.empty_like(u)
        drift[0, :] = u[0, :]
        if self.N > 1:
            drift[1:, :] = u[1:, :] - u[:-1, :]
        # Spec 13: a detached story stops being structural at its t_detach
        # sample (set by solve_with_detachment). Past it, its drift is held
        # floor minus moving survivor, and the held floors carry zero
        # acceleration, so a peak landing there would divide by a zero
        # story shear. Its drift is held at that sample, like its HFTD
        # story histories.
        for i, k in enumerate(getattr(self, 'held_from', None) or ()):
            if k is not None:
                drift[i, k + 1:] = drift[i, k]
        self.story_drift = drift

        t_star = np.argmax(np.abs(drift), axis=1)
        self.peak_drift = np.abs(drift[np.arange(self.N), t_star])
        self.peak_drift_ratio = self.peak_drift / self.h

        # V_i(t) = sum over floors at or above i of m_j * a_j_abs(t).
        story_shear = np.cumsum(
            (self.m * self.floor_accel_abs)[::-1, :], axis=0)[::-1, :]
        V_at_peak = np.abs(story_shear[np.arange(self.N), t_star])

        denom = V_at_peak * self.h * C_D_DEFLECTION_AMPLIFICATION
        with np.errstate(divide="ignore", invalid="ignore"):
            theta = np.where(denom > 0,
                             self.P_gravity * self.peak_drift / denom,
                             np.inf)
        self.theta_demand = theta
        self.story_shear_at_peak_drift = V_at_peak

        # Demand ductility ratio (spec 10, D5). **An ELASTIC-DEMAND
        # indicator only, and must be labelled as one everywhere it
        # appears.** An elastic model over-predicts force and
        # under-predicts displacement once the real structure yields, so
        # mu_i > 1 here means "this story would have yielded" -- NOT "this
        # story reached ductility mu_i". Reported, never enforced.
        self.mu_demand = self.peak_drift / self.delta_y_profile

    def compute_furniture_response(self):
        """
        Per-class (table/chair/fan) furniture relative-sway response, one
        oscillator per class per floor, not per physical item (spec Part
        B2 -- keeps the solve count at 3 classes independent of floor
        count or item count per floor). Requires compute_response() to
        have been run first (needs floor_accel_abs).

        Reuses this building's own zero-padding/settle-time treatment
        (spec 1): a lightly-damped furniture oscillator needs the same
        protection against FFT-wraparound contamination as a
        lightly-damped building mode. Each class's FFT is batched across
        every floor in one call (not a per-floor loop), since the transfer
        function furniture_frf() only depends on the class, not the
        floor.

        This is the convolution theorem (frequency-domain multiplication
        = time-domain convolution) applied a SECOND time in this file, to
        an independent LTI subsystem one-way-coupled to the primary
        structure's already-computed floor acceleration -- the whole
        point of spec 5's Part B, not incidental (see
        specs/COURSE-CONCEPTS.md).
        """
        if not hasattr(self, 'floor_accel_abs'):
            raise RuntimeError("Run compute_response() before compute_furniture_response().")

        dt = self.dt
        results = {}
        for cls_name, params in FURNITURE_CLASSES.items():
            f_Hz = params["f_hz"]
            zeta_f = params["zeta"]
            wf = 2 * np.pi * f_Hz

            settle_time = 5.0 / (zeta_f * wf)
            pad_len = min(4 * self.npts, self.npts + int(np.ceil(settle_time / dt)))

            accel_padded = np.zeros((self.N, pad_len))
            accel_padded[:, :self.npts] = self.floor_accel_abs

            Xb_fft = fft(accel_padded, axis=1)
            freqs = fftfreq(pad_len, dt)
            omega = 2 * np.pi * freqs
            H = furniture_frf(omega, f_Hz, zeta_f)

            # Governing equation is m*u'' + c*u' + k*u = -m*x_b'' -- the
            # minus sign here is that same inertial-load sign, not part of
            # H itself (furniture_frf's H(jw) is the bare 1/(...) transfer
            # function, matching its docstring/verification exactly).
            U_fft = -H[np.newaxis, :] * Xb_fft
            u = np.real(ifft(U_fft, axis=1))
            results[cls_name] = u[:, :self.npts]

        self.furniture_response = results
        return results

    def get_decimated_furniture(self, target_rate=FURNITURE_TARGET_RATE_HZ):
        """
        Compute this axis's per-class furniture response and decimate it
        to ~target_rate Hz (spec Part B4's size justification -- see
        furniture_decimation's docstring). Returns (array shaped
        (3, N, npts_dec), ordered per FURNITURE_CLASSES; npts_dec;
        decimation_factor; decimated_rate_hz).

        Shared by both the offline __main__ pipeline and server.py's
        /compute so the two callers never duplicate this logic --  mirrors
        compute_response()'s own "one implementation, two callers" rule
        (see AGENTS.md).
        """
        responses = self.compute_furniture_response()
        q = furniture_decimation(self.dt, target_rate)

        dec_list = []
        npts_dec = None
        for cls_name in FURNITURE_CLASSES:
            u_dec = decimate_furniture(responses[cls_name], q)
            if npts_dec is None:
                npts_dec = u_dec.shape[1]
            dec_list.append(u_dec)

        stacked = np.stack(dec_list, axis=0).astype(np.float32)  # (3, N, npts_dec)
        decimated_rate = (1.0 / self.dt) / q
        return stacked, npts_dec, q, decimated_rate

    def _integrate_accel(self, accel, dt):
        """
        Recover ground displacement from acceleration by double integration
        in the frequency domain. Before integrating, the signal is passed
        through a smooth high-pass filter (4th-order Butterworth, zero-phase
        via filtfilt) instead of the old brick-wall FFT-bin zeroing, to avoid
        Gibbs-type ringing in the recovered displacement.

        The cutoff is adaptive -- capped at 0.1 Hz, but never above half the
        building's own fundamental frequency, so a soft/long-period
        archetype's genuine low-frequency structural response doesn't get
        filtered out along with real sensor drift. f1 is always known
        upfront since it only depends on the building parameters (no
        circular dependency on the ground motion being processed).
        """
        f1 = self.omega_n[0] / (2 * np.pi)
        f_cut = min(0.1, f1 * 0.5)
        nyquist = 0.5 / dt
        b, a = butter(4, f_cut / nyquist, btype='high')
        accel_filtered = filtfilt(b, a, accel)

        n = len(accel_filtered)
        freqs = fftfreq(n, dt)
        omega = 2 * np.pi * freqs
        A = fft(accel_filtered)
        omega_sq = omega**2
        omega_sq[0] = 1.0  # guard 0/0; true DC content is ~0 after the filter
        disp_fft = -A / omega_sq
        disp_fft[0] = 0.0
        disp = np.real(ifft(disp_fft))
        disp -= np.mean(disp)
        return disp

    def _differentiate_to_accel(self, disp, dt):
        """
        Recover ground acceleration from displacement by double
        differentiation in the frequency domain, with the same smooth
        adaptive high-pass filtering as _integrate_accel (cutoff capped at
        0.05 Hz, never above a quarter of the building's fundamental
        frequency -- slightly lower than the integration cutoff to preserve
        more long-period content, matching the original design intent).
        """
        f1 = self.omega_n[0] / (2 * np.pi)
        f_cut = min(0.05, f1 * 0.25)
        nyquist = 0.5 / dt
        b, a = butter(4, f_cut / nyquist, btype='high')
        disp_filtered = filtfilt(b, a, disp)

        n = len(disp_filtered)
        freqs = fftfreq(n, dt)
        omega = 2 * np.pi * freqs
        D = fft(disp_filtered)
        accel_fft = -(omega**2) * D
        accel = np.real(ifft(accel_fft))
        accel -= np.mean(accel)
        return accel

    def save_to_csv(self, filename, prefix=""):
        """Save absolute displacements to CSV with optional prefix for columns."""
        if not hasattr(self, 'floor_disp_abs'):
            raise RuntimeError("No response computed. Run compute_response first.")
        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            header = ['time']
            if prefix:
                header.append(f'{prefix}_ground_disp')
                for i in range(self.N):
                    header.append(f'{prefix}_floor_{i+1}_abs')
            else:
                header.append('ground_disp')
                for i in range(self.N):
                    header.append(f'floor_{i+1}_abs')
            writer.writerow(header)

            for t_idx in range(self.npts):
                row = [self.time[t_idx], self.ground_disp[t_idx]]
                for i in range(self.N):
                    row.append(self.floor_disp_abs[i, t_idx])
                writer.writerow(row)
        print(f"Saved response to {filename}")


# --- Spec 12: the coupled 3N building -------------------------------------

class MDOF_Building3N:
    """Three DOFs per floor -- u_x, u_y and theta_z -- in ONE coupled solve.

    Spec 11 gave every (story, column) its own hysteresis, but in a
    one-lateral-DOF-per-floor model that asymmetry has no dynamic
    consequence: all four columns share one drift, so a weakened east
    column changes nothing about how the building moves. With a rotational
    DOF the feedback -- twist -> more demand on the damaged side -> more
    damage -> more twist -- emerges from the dynamics instead of being
    animated.

    This is still modal frequency-domain FFT convolution. `M` and `K_3N`
    are both real symmetric, so `Phi^T M Phi = I` still diagonalises and
    the system stays classically dampable with one shared `zeta` across
    all 3N modes -- that property is exactly what lets the FFT kernel
    survive the change (spec A5). Newmark remains validation-only.

    DOF ordering is `[u_x,1..N, u_y,1..N, theta_1..N]` throughout; see the
    "Spec 12" block beside the frame math for the sign convention and the
    pinned column plan positions.

    **This is a new class, not a mutation of MDOF_ShearBuilding.** It
    *holds* two per-axis MDOF_ShearBuilding instances (`self.bx`,
    `self.by`) and takes `k0_profile`, `V_p_profile`, `delta_y_profile`
    and the nonlinear backbones from them unchanged. That is not
    convenience: `story_stiffness_profile` is a first-mode pushover of a
    single-axis condensed frame, and there is no meaningful "first-mode
    pushover" of a coupled 3N system that reproduces those numbers (spec
    correction C-2). It also keeps spec 11's per-column backbones and the
    elastic 3N assembly derived from the *same* condensed frames, with no
    second definition of story stiffness introduced, and it is what makes
    verification check 1's bit-identity reachable at all.
    """

    def __init__(self, num_stories, mass_per_floor=1000e3, zeta=0.05,
                 story_height=3.5, column_depth_x=1.10, column_depth_y=1.10,
                 beam_depth=1.50, E=E_CONCRETE,
                 plan_span_x=PLAN_SPAN_X, plan_span_y=PLAN_SPAN_Y,
                 section_stiffness_mode=DEFAULT_SECTION_STIFFNESS_MODE,
                 cracked_factor_column=None, cracked_factor_beam=None,
                 p_delta=True,
                 f_y=F_Y_STEEL, f_c=F_C_CONCRETE,
                 rho_longitudinal=RHO_LONGITUDINAL,
                 concrete_cover=CONCRETE_COVER, phi_axial=PHI_AXIAL,
                 axial_cap_factor=AXIAL_CAP_FACTOR,
                 nonlinear=False, **nonlinear_params):
        per_axis = dict(
            num_stories=num_stories, mass_per_floor=mass_per_floor,
            zeta=zeta, story_height=story_height,
            column_depth_x=column_depth_x, column_depth_y=column_depth_y,
            beam_depth=beam_depth, E=E,
            plan_span_x=plan_span_x, plan_span_y=plan_span_y,
            section_stiffness_mode=section_stiffness_mode,
            cracked_factor_column=cracked_factor_column,
            cracked_factor_beam=cracked_factor_beam,
            p_delta=p_delta, f_y=f_y, f_c=f_c,
            rho_longitudinal=rho_longitudinal, concrete_cover=concrete_cover,
            phi_axial=phi_axial, axial_cap_factor=axial_cap_factor,
            nonlinear=nonlinear, **nonlinear_params)

        # Two cheap elastic eigensolves. They also mean a gravity-unstable
        # parameter set raises with the per-axis machinery's story
        # diagnostic before the 3N assembly is even built.
        self.bx = MDOF_ShearBuilding(axis="X", **per_axis)
        self.by = MDOF_ShearBuilding(axis="Y", **per_axis)

        self.nonlinear = bool(nonlinear)
        self.nonlinear_params = nonlinear_params
        self.N = num_stories
        self.m = mass_per_floor
        self.zeta = zeta
        self.h = self.bx.h
        self.total_height = self.bx.total_height
        self.E = E
        self.plan_span_x = plan_span_x
        self.plan_span_y = plan_span_y
        self.section_stiffness_mode = self.bx.section_stiffness_mode
        self.cracked_factor_column = self.bx.cracked_factor_column
        self.cracked_factor_beam = self.bx.cracked_factor_beam
        self.p_delta = bool(p_delta)
        self.gravity_unstable = False

        self.column_positions = column_plan_positions(plan_span_x, plan_span_y)

        # Per-axis capacity/stiffness profiles, taken unchanged (C-2). Named
        # `*_x` / `*_y` rather than collapsed into one array, because a
        # column's plastic shear genuinely differs by bending axis.
        self.k0_profile_x = self.bx.k0_profile
        self.k0_profile_y = self.by.k0_profile
        self.V_p_profile_x = self.bx.V_p_profile
        self.V_p_profile_y = self.by.V_p_profile
        self.delta_y_profile_x = self.bx.delta_y_profile
        self.delta_y_profile_y = self.by.delta_y_profile

        self._build_matrices()
        self._modal_analysis()

    def _build_matrices(self):
        N = self.N
        self.M = mass_matrix_3N(N, self.m, self.plan_span_x, self.plan_span_y)

        # Same cracked-section knock-down as the per-axis class, taken from
        # the instances themselves so the two can never disagree.
        self.I_c_x = self.bx.I_c
        self.I_c_y = self.by.I_c
        self.I_b = self.bx.I_b

        self.K_no_pdelta = assemble_K3N(
            N, self.E, self.I_c_x, self.I_c_y, self.I_b, self.h,
            self.plan_span_x, self.plan_span_y)

        self.K_G, self.k_g, self.P_gravity = geometric_stiffness_3N(
            N, self.m, self.h, self.plan_span_x, self.plan_span_y)

        if self.p_delta:
            self.K = self.K_no_pdelta - self.K_G
        else:
            # Assigned directly, not via a zero-scaled subtraction, so the
            # regression path is bit-identical rather than merely close --
            # same reason as MDOF_ShearBuilding._build_matrices.
            self.K = self.K_no_pdelta

    def _modal_analysis(self):
        """Generalized eigenproblem K_3N Phi = omega^2 M Phi, over 3N modes."""
        n = 3 * self.N
        eigvals, eigvecs = scipy_eigh(self.K, self.M, lower=True)
        idx = np.argsort(eigvals)

        # Gravity-instability guard BEFORE np.sqrt -- see
        # GravityInstabilityError's docstring for where the nan would
        # otherwise surface. The 3N system can go unstable torsionally as
        # well as laterally, so the message names which block the failing
        # mode lives in rather than assuming it is lateral.
        if eigvals[idx][0] <= 0.0:
            self.gravity_unstable = True
            v = eigvecs[:, idx[0]]
            share = [float(np.sum(v[k * self.N:(k + 1) * self.N] ** 2))
                     for k in range(3)]
            block = ("u_x", "u_y", "theta_z")[int(np.argmax(share))]
            story = self.bx._weakest_story()
            raise GravityInstabilityError(
                f"Gravity exceeds lateral stiffness: the building cannot "
                f"stand under its own weight at these parameters (3N "
                f"coupled model, failing mode is dominantly {block}; "
                f"weakest lateral story {story + 1} of {self.N}; smallest "
                f"eigenvalue {float(eigvals[idx][0]):.6e} <= 0). Reduce the "
                f"mass or story height, or increase the column depth.",
                story=story)

        self.omega_n = np.sqrt(eigvals[idx])
        self.phi = eigvecs[:, idx]
        for i in range(n):
            norm = np.sqrt(np.dot(self.phi[:, i].conj(), self.M @ self.phi[:, i]))
            self.phi[:, i] /= norm

        # Influence vectors for translational base excitation: unit
        # displacement of every floor in one direction, and **zero on the
        # theta DOFs** -- the ground translates, it does not spin.
        self.iota_x = np.zeros(n)
        self.iota_x[:self.N] = 1.0
        self.iota_y = np.zeros(n)
        self.iota_y[self.N:2 * self.N] = 1.0

        self.Gamma_x = self.phi.T @ (self.M @ self.iota_x)
        self.Gamma_y = self.phi.T @ (self.M @ self.iota_y)

        # Gamma^theta is the rotational share of that load vector. M is
        # block-diagonal and iota is zero on the theta DOFs, so this is
        # identically zero -- which is the point. A nonzero value means the
        # mass matrix picked up spurious u-theta coupling or an influence
        # vector was built with 1s in the theta block, and either would
        # otherwise show up only as a wrong answer. Verification check 4
        # asserts it rather than assuming it.
        rot = slice(2 * self.N, 3 * self.N)
        self.Gamma_theta = (
            self.phi[rot, :].T @ (self.M @ self.iota_x)[rot]
            + self.phi[rot, :].T @ (self.M @ self.iota_y)[rot])

        # Modal mass share per DOF block, used to say which modes are
        # torsional. Sums to 1 per mode because Phi is mass-normalised.
        self.modal_block_share = np.vstack([
            np.einsum('in,ij,jn->n',
                      self.phi[k * self.N:(k + 1) * self.N, :],
                      self.M[k * self.N:(k + 1) * self.N,
                             k * self.N:(k + 1) * self.N],
                      self.phi[k * self.N:(k + 1) * self.N, :])
            for k in range(3)])

    def _modes_of_block(self, k):
        """Ascending frequencies of the modes dominated by DOF block `k`."""
        which = np.argmax(self.modal_block_share, axis=0) == k
        return self.omega_n[which]

    def torsional_frequencies(self):
        """Ascending omega of the theta-dominated modes (rad/s)."""
        return self._modes_of_block(2)

    def lateral_frequencies(self, axis):
        """Ascending omega of the u_x- or u_y-dominated modes (rad/s)."""
        if axis not in ("X", "Y"):
            raise ValueError(f"axis must be 'X' or 'Y', got {axis!r}")
        return self._modes_of_block(0 if axis == "X" else 1)

    def frequency_ratio(self, axis="X"):
        """Omega = omega_theta,1 / omega_lateral,1 (spec A3).

        Below ~1 marks a torsionally-flexible structure, which codes
        restrict -- useful for spec 18.
        """
        return float(self.torsional_frequencies()[0]
                     / self.lateral_frequencies(axis)[0])

    # --- DOF-block addressing (spec 12 B4) -------------------------------

    _DOF_BLOCK = {"X": 0, "Y": 1, "THETA": 2}

    def dof_offset(self, axis="X"):
        """Row offset of a DOF block in the `[u_x, u_y, theta]` ordering.

        `phi[building.dof_offset('Y') + i, n]` is floor `i`'s u_y component
        of mode `n`. This is what `transfer_function`'s `dof_offset`
        argument takes: the Frequency tab's X/Y toggle selects a DOF block
        of one 3N solve, not a separate solve (B4).
        """
        try:
            return self.N * self._DOF_BLOCK[axis]
        except KeyError:
            raise ValueError(
                f"axis must be 'X', 'Y' or 'THETA', got {axis!r}") from None

    def participation(self, axis="X"):
        """Gamma^x / Gamma^y / Gamma^theta, by the same axis names."""
        self.dof_offset(axis)          # one place validates the name
        return {"X": self.Gamma_x, "Y": self.Gamma_y,
                "THETA": self.Gamma_theta}[axis]

    # --- Response (spec 12 B1/B5) ----------------------------------------

    def compute_response(self, accel_x, disp_x, accel_y, disp_y, dt,
                         nonlinear=None):
        """One coupled 3N solve driven by BOTH components simultaneously.

            RHS(t) = -M ( iota_x a_g,x(t) + iota_y a_g,y(t) )

        so modal coordinate `n` obeys

            q_n'' + 2 zeta w_n q_n' + w_n^2 q_n
                = -( Gamma_n^x a_g,x + Gamma_n^y a_g,y )

        which is `MDOF_ShearBuilding.compute_response`'s per-mode solve
        with a two-term forcing instead of one. Same H_n(jw), same
        zero-padding rationale, same `min(4*npts, ...)` cap -- see that
        method for why the padding exists at all.

        The two components must already be paired onto one time base
        (`pair_components`); this method does not re-align them.

        **Why u_x(t) is not bit-identical to the per-axis solve**, even
        though `K`'s u_x sub-block is (check 1's matrix half, and
        verification correction V-10): the 3N eigensolve is one LAPACK
        call over the whole `3N x 3N` pair, which reproduces the per-axis
        eigenvalues to ~4e-16 relative rather than exactly; and `pad_len`
        follows the slowest mode of the *coupled* system, which is
        generally the other axis's. A blockwise eigensolve would recover
        bit-identity, but only by making the "one solve" of B1 three
        decoupled ones -- and it would make check 1 and check 4 true by
        construction instead of testing the assembly. V-10 records the
        measured departure the tolerance was set against.
        """
        if nonlinear is True or (nonlinear is None and self.nonlinear):
            return self.compute_response_nonlinear(
                accel_x, disp_x, accel_y, disp_y, dt)

        accel_x = np.asarray(accel_x, dtype=float)
        accel_y = np.asarray(accel_y, dtype=float)
        if len(accel_x) != len(accel_y):
            raise ComponentPairingError(
                f"the two components must already be aligned to one length "
                f"(got {len(accel_x)} and {len(accel_y)}); call "
                f"pair_components first")

        self.accel_x = accel_x
        self.accel_y = accel_y
        self.ground_disp_x = np.asarray(disp_x, dtype=float)
        self.ground_disp_y = np.asarray(disp_y, dtype=float)
        self.dt = dt
        self.npts = len(accel_x)
        self.time = np.arange(self.npts) * dt

        settle_time = 5.0 / (self.zeta * self.omega_n[0])
        pad_len = min(4 * self.npts,
                      self.npts + int(np.ceil(settle_time / dt)))
        self.pad_len = pad_len

        ax_padded = np.zeros(pad_len)
        ax_padded[:self.npts] = accel_x
        ay_padded = np.zeros(pad_len)
        ay_padded[:self.npts] = accel_y

        Ax_fft = fft(ax_padded)
        Ay_fft = fft(ay_padded)
        omega = 2 * np.pi * fftfreq(pad_len, dt)

        n = 3 * self.N
        q_time = np.zeros((n, pad_len))
        qvel_time = np.zeros((n, pad_len))
        qacc_time = np.zeros((n, pad_len))
        for i in range(n):
            wn = self.omega_n[i]
            z = self.zeta
            denom = (wn**2 - omega**2 + 1j * 2 * z * wn * omega)
            # Superposition of the two components' forcing on this mode.
            # Gamma_x and Gamma_y are the two participation factors of B4;
            # Gamma_theta is zero for translational excitation and so does
            # not appear (asserted, not assumed -- check 4).
            Q_fft = -(self.Gamma_x[i] * Ax_fft + self.Gamma_y[i] * Ay_fft) \
                / denom
            q_time[i, :] = np.real(ifft(Q_fft))
            # Floor absolute acceleration via the transform's
            # differentiate-in-time property, on Q_fft -- same reasoning as
            # the per-axis method, not a np.gradient shortcut.
            qvel_time[i, :] = np.real(ifft(1j * omega * Q_fft))
            qacc_time[i, :] = np.real(ifft(-(omega**2) * Q_fft))

        q_time = q_time[:, :self.npts]
        qvel_time = qvel_time[:, :self.npts]
        qacc_time = qacc_time[:, :self.npts]

        u_rel = self.phi @ q_time
        # Kept here rather than re-derived by the nonlinear solver: the
        # per-axis path re-runs its own FRF loop to get base velocity, which
        # is a second copy of this expression waiting to disagree with it.
        # Only the energy balance consumes it, so the extra ifft per mode is
        # paid once per solve, not once per HFTD iteration.
        self.floor_vel_rel = self.phi @ qvel_time
        a_rel = self.phi @ qacc_time

        x, y, t = (slice(0, self.N), slice(self.N, 2 * self.N),
                   slice(2 * self.N, n))
        self.floor_disp_rel_x = u_rel[x]
        self.floor_disp_rel_y = u_rel[y]
        self.floor_rot = u_rel[t]
        self.floor_accel_rel_x = a_rel[x]
        self.floor_accel_rel_y = a_rel[y]
        self.floor_rot_accel = a_rel[t]

        # The full 3N vectors, kept alongside the per-block views because
        # the nonlinear kernel works on the whole DOF set.
        self.floor_disp_rel = u_rel
        self.floor_accel_rel = a_rel

        self.floor_disp_abs_x = self.floor_disp_rel_x + self.ground_disp_x
        self.floor_disp_abs_y = self.floor_disp_rel_y + self.ground_disp_y
        self.floor_accel_abs_x = self.floor_accel_rel_x + self.accel_x
        self.floor_accel_abs_y = self.floor_accel_rel_y + self.accel_y

        self._install_axis_views()
        return (self.time, self.floor_disp_rel_x, self.floor_disp_rel_y,
                self.floor_rot)

    def _install_axis_views(self, hftd=None):
        """Hand each held per-axis building its DOF block of the 3N solve.

        Furniture (`get_decimated_furniture`) and spec 10's response-
        dependent keys (`theta_demand`, `peak_drift_ratio`, `mu_demand`) are
        per-axis quantities that already live on `MDOF_ShearBuilding`.
        Installing the coupled histories there reuses those methods instead
        of growing a second copy of them here. They see the floor-CENTRE
        drift and acceleration; per-column drift is what the hysteresis
        sees, and those stay on `hftd_result.x/.y`.
        """
        for b, s in ((self.bx, "x"), (self.by, "y")):
            b.accel = getattr(self, f"accel_{s}")
            b.ground_disp = getattr(self, f"ground_disp_{s}")
            b.dt, b.npts, b.time = self.dt, self.npts, self.time
            b.floor_disp_rel = getattr(self, f"floor_disp_rel_{s}")
            b.floor_disp_abs = getattr(self, f"floor_disp_abs_{s}")
            b.floor_accel_abs = getattr(self, f"floor_accel_abs_{s}")
            b.floor_accel_rel = getattr(self, f"floor_accel_rel_{s}")
            if hftd is not None:
                b.hftd_result = getattr(hftd, s)
            b._demand_stability_coefficients()

    def compute_response_nonlinear(self, accel_x, disp_x, accel_y, disp_y, dt,
                                   **params):
        """The coupled nonlinear solve; installs its histories on `self`.

        Mirrors `MDOF_ShearBuilding.compute_response_nonlinear`, including
        the elastic solve it runs first to establish the base response and
        the time metadata. `floor_rot` is the quantity that did not exist
        before spec 12: elastically it is identically zero, so any nonzero
        value here is emergent twist from asymmetric yielding (check 5).
        """
        result = solve_hftd_3N(self, accel_x, disp_x, accel_y, disp_y, dt,
                               {**self.nonlinear_params, **params})
        return self._install_nonlinear(result)

    def _install_nonlinear(self, result):
        self.hftd_result = result
        N = self.N
        self.floor_disp_rel = result.u_rel
        self.floor_disp_rel_x = result.u_rel[:N]
        self.floor_disp_rel_y = result.u_rel[N:2*N]
        self.floor_rot = result.theta
        self.floor_disp_abs_x = result.x.u_abs
        self.floor_disp_abs_y = result.y.u_abs
        self.floor_accel_abs_x = result.x.acceleration
        self.floor_accel_abs_y = result.y.acceleration
        self.floor_accel_rel_x = result.x.acceleration - self.accel_x[None,:]
        self.floor_accel_rel_y = result.y.acceleration - self.accel_y[None,:]
        self.floor_rot_accel = result.acceleration[2*N:]
        self._install_axis_views(result)
        return (self.time, self.floor_disp_rel_x, self.floor_disp_rel_y,
                self.floor_rot)


# --- Ground-motion frequency-domain spectrum (spec 6, Part A) -------------

def log_bin_edges(nyquist_hz, f_min=0.1, n_bins=400):
    """
    Geometrically-spaced bin edges for the frequency-domain panel's
    log-frequency axis (spec 6). Length n_bins+1. np.geomspace, not
    np.linspace -- a seismic magnitude spectrum is conventionally read on
    a log-frequency axis, and a linear grid would crowd all the
    interesting low-frequency structure into a few pixels.
    """
    return np.geomspace(f_min, nyquist_hz, n_bins + 1)


def log_bin_spectrum(signal, dt, n_bins=400):
    """
    Zero-padded FFT magnitude spectrum of `signal`, averaged into
    log-spaced frequency bins (spec 6 Part A). Returns (f_Hz, mag), each
    length n_bins.

    THE CONTRACT: index.html's frequency-domain panel re-implements this
    exact algorithm in JavaScript, and the two must agree bit-for-bit.
    The panel's Input trace comes from the artifact this produces (the
    browser never receives raw acceleration); its Output trace is an
    INDEPENDENT in-browser FFT of the selected floor's relative
    displacement. Both have to land on the same log-frequency grid, or
    the identity |Output| == |Transfer| x |Input| stops reading as
    vertical alignment across the stacked panels.

    NOTE FOR ANYONE TOUCHING THE PANEL: the Output trace must NEVER be
    derived as Input x Transfer. That would make the identity true by
    construction -- a tautology that demonstrates nothing -- when the
    entire point of the figure is that two independently-computed things
    agree. See specs/06-frequency-domain-panel.md Part A2.

    A verification check (claude_scripts/verify_spectrum.py) compares this
    against a from-scratch recomputation to 1e-12 relative -- don't change
    this function's math without keeping that mirror in mind.

        NFFT = next power of two >= len(signal)      (zero-padded)
        F = rfft(signal, NFFT)
        mag[k] = |F[k]| * dt                 for k = 0 .. NFFT//2 (incl.)
        freq[k] = k / (NFFT * dt)  (== np.fft.rfftfreq(NFFT, dt))

    The `* dt` scaling approximates the continuous Fourier transform (a
    Riemann-sum discretization of the CFT integral), so the panel's
    absolute magnitude values are physically meaningful (units of
    accel-seconds). It exactly cancels in the Output/Input transfer-
    function ratio the panel also plots, so it only matters for reading
    the Input panel alone, not for the ratio.

        nyquist = 1 / (2*dt)
        edges = geomspace(f_min, nyquist, n_bins+1)
        f_Hz[b] = sqrt(edges[b] * edges[b+1])      (geometric-mean centre)
        value[b] = mean of mag[k] for all k with edges[b] <= freq[k] < edges[b+1]
                   (last bin closed on the right too, so freq == nyquist
                   is captured)

    An empty bin (common at the low end, where the log grid is narrower
    than the FFT's uniform bin spacing df = 1/(NFFT*dt)) falls back to the
    single mag[k] whose freq[k] is nearest f_Hz[b] -- not interpolation,
    not zero, not NaN.
    """
    n = len(signal)
    nfft = 1 << (n - 1).bit_length()  # next power of two >= n
    F = np.fft.rfft(signal, nfft)
    mag = np.abs(F) * dt
    freq = np.fft.rfftfreq(nfft, dt)

    nyquist = 1.0 / (2 * dt)
    edges = log_bin_edges(nyquist, n_bins=n_bins)
    f_centers = np.sqrt(edges[:-1] * edges[1:])

    values = np.empty(n_bins)
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        if b == n_bins - 1:
            in_bin = (freq >= lo) & (freq <= hi)
        else:
            in_bin = (freq >= lo) & (freq < hi)
        if np.any(in_bin):
            values[b] = mag[in_bin].mean()
        else:
            nearest = np.argmin(np.abs(freq - f_centers[b]))
            values[b] = mag[nearest]

    return f_centers, values


def transfer_function(f_hz, omega_n, phi, Gamma, zeta, floor_idx,
                      dof_offset=0):
    """
    Analytic transfer function from ground acceleration to floor
    `floor_idx`'s displacement RELATIVE to ground (spec 6 Part A2) --
    the middle panel's solid curve.

    Mirrors compute_response()'s per-mode frequency-domain solve exactly
    (there: ``H = -Gamma_i / (wn**2 - omega**2 + 1j*2*z*wn*omega)``,
    ``q_i = phi[:, i] * H * A_fft``, ``floor_disp_rel = phi @ q``), just
    evaluated analytically at arbitrary frequencies instead of via
    FFT/ifft on a padded time grid:

        H_j(jw) = 1 / (omega_j^2 - w^2 + 2j*zeta*omega_j*w)
        T_i(jw) = -sum_j phi[floor_idx, j] * Gamma[j] * H_j(jw)

    This is the "input x transfer = output" identity the frequency-domain
    panel draws -- claude_scripts/verify_spectrum.py's check_identity()
    confirms it against compute_response()'s own FFT-based solution.
    index.html re-implements this same formula in JavaScript for the
    Transfer panel; keep the two in sync if this changes.

    `phi` must be mass-normalized (phi^T M phi = I), as produced by
    _modal_analysis() -- do not renormalize it here.

    `dof_offset` selects the DOF block for the 3N torsional kernel (spec
    12 B4): `phi`'s rows are ordered `[u_x,1..N, u_y,1..N, theta_1..N]`, so
    floor `floor_idx`'s u_y row is `MDOF_Building3N.dof_offset('Y') +
    floor_idx`. Pass the matching `Gamma^x`/`Gamma^y` from
    `MDOF_Building3N.participation(axis)` -- an axis's transfer function
    needs BOTH the right rows of `phi` and the right participation vector.
    The default 0 leaves the per-axis `MDOF_ShearBuilding` callers
    unchanged.

    Returns (T_i, mode_terms):
        T_i: complex ndarray, shape (len(f_hz),) -- the solid curve,
            T_i = -mode_terms.sum(axis=0).
        mode_terms: complex ndarray, shape (n_modes, len(f_hz)) --
            phi[floor_idx, j] * Gamma[j] * H_j(jw) per mode j, drawn
            faintly underneath T_i so the modal decomposition stays
            visible (spec 6 Part A2, requirement 2).
    """
    omega = 2 * np.pi * np.asarray(f_hz, dtype=float)
    n_modes = phi.shape[1]
    mode_terms = np.empty((n_modes, len(omega)), dtype=complex)
    row = dof_offset + floor_idx
    for j in range(n_modes):
        wn = omega_n[j]
        denom = (wn**2 - omega**2 + 1j * 2 * zeta * wn * omega)
        H_j = 1.0 / denom
        mode_terms[j] = phi[row, j] * Gamma[j] * H_j
    T_i = -mode_terms.sum(axis=0)
    return T_i, mode_terms


def save_ground_spectrum(filename, accel_x, accel_y, dt, n_bins=400):
    """
    Writes out/<record>/spectrum.json -- the ground-acceleration magnitude
    spectrum index.html's frequency-domain panel (spec 6) plots as its
    Input trace. Precomputed here, not in the browser: the browser only
    ever holds ground *displacement* (ground_accel.json's X_disp/Y_disp,
    used to drive live /compute recomputation), never acceleration, and
    this panel is parameter-independent (Building Parameter sliders change
    the building's transfer function, never the record) -- so there's no
    reason to ship raw acceleration to the client just to FFT it there.
    """
    nyquist = 1.0 / (2 * dt)
    f_hz, x_mag = log_bin_spectrum(accel_x, dt, n_bins=n_bins)
    y_mag = None
    if accel_y is not None:
        _, y_mag = log_bin_spectrum(accel_y, dt, n_bins=n_bins)

    data = {
        "dt": dt,
        "npts": len(accel_x),
        "nyquist_Hz": nyquist,
        "f_Hz": f_hz.tolist(),
        "X_mag": x_mag.tolist(),
        "Y_mag": y_mag.tolist() if y_mag is not None else None,
    }
    with open(filename, 'w') as f:
        json.dump(data, f)


def finite_or_none(values):
    """A per-story list for JSON: a non-finite entry becomes None (null).

    theta_demand is inf for a story whose shear at its drift peak is
    exactly zero -- undefined, not huge -- and json.dumps would write a
    bare `Infinity` that JSON.parse rejects (or, with allow_nan=False,
    raise and turn /compute into an HTTP 500). The model keeps the inf;
    only the wire encoding changes, the same null convention as
    eccentricity_x/y.
    """
    return [float(v) if np.isfinite(v) else None
            for v in np.asarray(values, dtype=float)]


def save_building_data(filename, building_x, building_y, furniture_meta=None,
                        reference_magnitude=6.0, soft_ground_story=False):
    """
    Module-level replacement for the old per-instance save_to_json --
    spec 5 needs BOTH axes' independent condensed-stiffness modal results
    in one file (building_x, building_y: MDOF_ShearBuilding instances with
    axis="X"/"Y" respectively, same N/h/mass/zeta/geometry otherwise), plus
    the new frame-geometry fields and a furniture metadata block describing
    the paired furniture_response.bin artifact.
    """
    data = {
        "num_stories": int(building_x.N),
        # Scalar ground-story values, kept for compatibility with every
        # pre-spec-10 reader. These are per-floor arrays internally now
        # (spec 10, C1); the full profiles ship alongside them below.
        "story_height": float(building_x.h[0]),
        "damping_ratio": float(building_x.zeta),
        # Richter magnitude this record corresponds to (data/richter
        # readings.json), used as the Earthquake Parameters panel's
        # per-record Richter slider default -- spec 7, Part A.
        "reference_magnitude": float(reference_magnitude),

        # Frame geometry (spec A2) -- shared by both axes.
        "elastic_modulus_Pa": float(building_x.E),
        "column_depth_x": float(building_x.column_depth_x[0]),
        "column_depth_y": float(building_x.column_depth_y[0]),
        "beam_depth": float(building_x.beam_depth[0]),
        "beam_width": float(BEAM_WIDTH),
        # Read off the INSTANCE, not the module constants. Identical today
        # because the offline pipeline always runs at the default area,
        # but the constants were a latent spec-8 inconsistency: a
        # building_x built with a non-default plan_span would have had its
        # real dimensions silently replaced by the defaults here.
        "plan_span_x": float(building_x.plan_span_x),
        "plan_span_y": float(building_x.plan_span_y),

        # --- spec 10, Part E -- the same keys the /compute header carries,
        # so the static and live paths stay symmetric (the asymmetry spec 6
        # had to close for participation_factors_*).
        "story_heights": building_x.h.tolist(),
        "section_stiffness_mode": building_x.section_stiffness_mode,
        "cracked_factor_column": float(building_x.cracked_factor_column),
        "cracked_factor_beam": float(building_x.cracked_factor_beam),
        "p_delta_enabled": bool(building_x.p_delta),
        "soft_ground_story": bool(soft_ground_story),
        "gravity_unstable": False,
        "k_g_per_story_N_per_m_X": building_x.k_g.tolist(),
        "k_g_per_story_N_per_m_Y": building_y.k_g.tolist(),
        "k0_per_story_N_per_m_X": building_x.k0_profile.tolist(),
        "k0_per_story_N_per_m_Y": building_y.k0_profile.tolist(),
        "theta_stiffness_X": building_x.theta_stiffness.tolist(),
        "theta_stiffness_Y": building_y.theta_stiffness.tolist(),
        "theta_demand_X": finite_or_none(building_x.theta_demand),
        "theta_demand_Y": (finite_or_none(building_y.theta_demand)
                           if hasattr(building_y, "theta_demand") else None),
        "peak_drift_ratio_X": building_x.peak_drift_ratio.tolist(),
        "peak_drift_ratio_Y": (building_y.peak_drift_ratio.tolist()
                               if hasattr(building_y, "peak_drift_ratio")
                               else None),
        "V_p_per_story_N_X": building_x.V_p_profile.tolist(),
        "V_p_per_story_N_Y": building_y.V_p_profile.tolist(),
        "delta_y_per_story_m_X": building_x.delta_y_profile.tolist(),
        "delta_y_per_story_m_Y": building_y.delta_y_profile.tolist(),
        # ELASTIC-DEMAND indicator, not an achieved ductility (spec D5).
        "mu_demand_X": building_x.mu_demand.tolist(),
        "mu_demand_Y": (building_y.mu_demand.tolist()
                        if hasattr(building_y, "mu_demand") else None),
        "P_cap_per_story_N": np.broadcast_to(
            np.asarray(building_x.P_cap_profile, dtype=float),
            (building_x.N,)).tolist(),

        # Per-axis modal results -- independently condensed K per axis
        # (spec A1's anisotropy), so these are no longer shared numbers.
        "natural_frequencies_Hz_X": (building_x.omega_n / (2 * np.pi)).tolist(),
        "mode_shapes_X": building_x.phi.tolist(),
        "participation_factors_X": building_x.Gamma.tolist(),
        "fundamental_period_s_X": float(2 * np.pi / building_x.omega_n[0]),
        "story_stiffness_X_N_per_m": float(building_x.story_stiffness),

        "natural_frequencies_Hz_Y": (building_y.omega_n / (2 * np.pi)).tolist(),
        "mode_shapes_Y": building_y.phi.tolist(),
        "participation_factors_Y": building_y.Gamma.tolist(),
        "fundamental_period_s_Y": float(2 * np.pi / building_y.omega_n[0]),
        "story_stiffness_Y_N_per_m": float(building_y.story_stiffness),

        "story_stiffness_note": (
            "Condensed base-story (K[0,0]) lateral stiffness per axis -- "
            "not a single universal k like the old abstract-spring model; "
            "every floor's diagonal entry in a condensed frame stiffness "
            "matrix is generally different."
        ),

        "furniture": furniture_meta,
    }
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved building data to {filename}")


if __name__ == "__main__":
    data_dir = "data"
    out_dir = "out"

    if not os.path.exists(data_dir):
        print(f"Error: data directory '{data_dir}' not found.")
        sys.exit(1)

    NUM_STORIES = 7  # change as needed
    # Frame-geometry defaults (spec 5) -- MUST match server.py's
    # _validate_params defaults AND index.html's slider defaults exactly.
    # A comment at each of the three sites points at the other two.
    # index.html's buildingParamsAtDefault() uses equality against these to
    # decide whether out/'s precomputed static files are still valid for
    # the current slider positions -- a silent mismatch here reproduces
    # the exact desync bug spec 3 already had to fix once (see AGENTS.md).
    # Calibrated (claude_scripts/calibrate_frame.py) so N=7's T1 lands at
    # ~1.06s (within the plan's 0.6-1.2s target band) and rho ~= 0.48 (within
    # the 0.3-0.8 band needed for the Beam depth slider to visibly matter).
    COLUMN_DEPTH_X = 1.10  # m
    COLUMN_DEPTH_Y = 1.10  # m
    BEAM_DEPTH = 1.50      # m

    # Section-stiffness preset for this regeneration (spec 10, A2).
    # Overridable so verification check 1's end-to-end half can regenerate
    # out/ with the pre-spec-10 "gross" behaviour and confirm an EMPTY
    # git diff, without editing this file:
    #     SEISMIC_SIM_SECTION_MODE=gross SEISMIC_SIM_P_DELTA=0 python mdof_response.py
    SECTION_MODE = os.environ.get("SEISMIC_SIM_SECTION_MODE",
                                  DEFAULT_SECTION_STIFFNESS_MODE)
    if SECTION_MODE not in SECTION_STIFFNESS_PRESETS:
        print(f"Warning: unknown SEISMIC_SIM_SECTION_MODE={SECTION_MODE!r}; "
              f"falling back to {DEFAULT_SECTION_STIFFNESS_MODE!r}.")
        SECTION_MODE = DEFAULT_SECTION_STIFFNESS_MODE
    P_DELTA = os.environ.get("SEISMIC_SIM_P_DELTA", "1") not in ("0", "false", "False")
    print(f"Section stiffness mode: {SECTION_MODE} "
          f"{SECTION_STIFFNESS_PRESETS[SECTION_MODE]}   P-Delta: {P_DELTA}")

    # Per-record Richter magnitude (spec 7, Part A) -- maps folder name to
    # the record's real-world magnitude, used as the Earthquake Parameters
    # panel's per-record slider default. This __main__ block only stores
    # the value; apply_synthetic_earthquake_scaling() itself is only ever
    # invoked by server.py's /compute, never here, so regenerating out/
    # stays byte-identical except for this one new field.
    richter_path = os.path.join(data_dir, "richter readings.json")
    if os.path.isfile(richter_path):
        with open(richter_path) as f:
            RICHTER_READINGS = json.load(f)
    else:
        print(f"Warning: {richter_path} not found -- reference_magnitude "
              f"will default to 6.0 for every record.")
        RICHTER_READINGS = {}

    folders = [f for f in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, f))]
    if not folders:
        print("No subfolders found in data/. Please place earthquake folders inside data/.")
        sys.exit(1)

    for folder in folders:
        folder_path = os.path.join(data_dir, folder)
        reference_magnitude = RICHTER_READINGS.get(folder, 6.0)
        print(f"\nProcessing folder: {folder}")

        all_files = os.listdir(folder_path)
        print(f"  All files in folder: {all_files}")

        at2_files = [f for f in all_files if f.lower().endswith('.at2')]
        dt2_files = [f for f in all_files if f.lower().endswith('.dt2')]

        print(f"  Found AT2 files: {at2_files}")
        print(f"  Found DT2 files: {dt2_files}")

        # Build orientation mapping for AT2
        at2_by_orient = {}
        for f in at2_files:
            orient = get_orientation_from_filename(f)
            if orient is not None:
                at2_by_orient[orient] = f
                print(f"    {f} -> orientation {orient}°")
            else:
                print(f"    {f} -> skipped (vertical or unknown)")

        at2_orients = sorted(at2_by_orient.keys())
        print(f"  Horizontal orientations found: {at2_orients}")

        if len(at2_orients) < 2:
            print("  Need at least two horizontal components. Skipping folder.")
            continue

        x_orient, y_orient = assign_component_axes(at2_orients)
        print(f"  X axis: {x_orient}°, Y axis: {y_orient}°")

        # Build DT2 orientation dict
        dt2_by_orient = {}
        for f in dt2_files:
            orient = get_orientation_from_filename(f)
            if orient is not None:
                dt2_by_orient[orient] = f

        out_folder = os.path.join(out_dir, folder)
        os.makedirs(out_folder, exist_ok=True)

        # X and Y each need their own instance now -- independent
        # condensed K per axis (spec A1's anisotropy; a rectangular
        # column is stiffer one way than the other once
        # COLUMN_DEPTH_X != COLUMN_DEPTH_Y).
        try:
            building_x = MDOF_ShearBuilding(
                NUM_STORIES, mass_per_floor=1000e3, zeta=0.05,
                column_depth_x=COLUMN_DEPTH_X, column_depth_y=COLUMN_DEPTH_Y,
                beam_depth=BEAM_DEPTH, axis="X",
                section_stiffness_mode=SECTION_MODE, p_delta=P_DELTA,
            )
            building_y = MDOF_ShearBuilding(
                NUM_STORIES, mass_per_floor=1000e3, zeta=0.05,
                column_depth_x=COLUMN_DEPTH_X, column_depth_y=COLUMN_DEPTH_Y,
                beam_depth=BEAM_DEPTH, axis="Y",
                section_stiffness_mode=SECTION_MODE, p_delta=P_DELTA,
            )
        except GravityInstabilityError as e:
            # Print and move on -- one unstable parameter set must not
            # abort the whole batch (spec 10, B3).
            print(f"  GRAVITY INSTABILITY: {e}")
            print("  Skipping this record.")
            continue

        def load_component(orient, label, building):
            at2_file = at2_by_orient.get(orient)
            if at2_file is None:
                print(f"  {label}: No AT2 for orientation {orient}°. Skipping.")
                return None, None, None

            at2_path = os.path.join(folder_path, at2_file)
            accel, dt = parse_peer_file(at2_path)

            dt2_file = dt2_by_orient.get(orient)
            if dt2_file is not None:
                dt2_path = os.path.join(folder_path, dt2_file)
                disp, _ = parse_peer_displacement_file(dt2_path)
                print(f"  {label}: Using DT2 for displacement.")
            else:
                print(f"  {label}: No DT2 – integrating acceleration to get displacement.")
                disp = building._integrate_accel(accel, dt)

            min_len = min(len(accel), len(disp))
            if len(accel) != len(disp):
                print(f"  {label}: Length mismatch – truncating to {min_len} samples.")
                accel = accel[:min_len]
                disp = disp[:min_len]

            return accel, disp, dt

        # Process X
        accel_x, disp_x, dt = load_component(x_orient, "X", building_x)
        if accel_x is None:
            print("  Failed to load X component. Skipping folder.")
            continue

        time, g_disp_x, rel_x, abs_x = building_x.compute_response(accel_x, disp_x, dt)
        building_x.save_to_csv(os.path.join(out_folder, "response_X.csv"), prefix="X")
        furn_x, npts_dec_x, q_x, rate_x = building_x.get_decimated_furniture()

        # Process Y
        accel_y, disp_y, dt_y = load_component(y_orient, "Y", building_y)
        has_y = accel_y is not None

        # Spec 12 B5 / correction C-4: decide NOW whether this record's two
        # components can legitimately drive one coupled 3N solve, and record
        # the answer alongside the cached traces. Y's own dt used to be
        # discarded here in favour of X's, which is harmless while the two
        # axes are solved independently and a silent correctness defect the
        # moment they are not.
        try:
            paired = pair_components(accel_x, disp_x, dt,
                                     accel_y, disp_y, dt_y,
                                     x_orient=x_orient, y_orient=y_orient,
                                     label=folder)
            torsion_header = dict(paired.header, torsion_eligible=True)
        except ComponentPairingError as exc:
            print(f"  Not eligible for torsion: {exc}")
            torsion_header = {"x_orient_deg": x_orient,
                              "y_orient_deg": y_orient,
                              "component_pad_x": 0, "component_pad_y": 0,
                              "torsion_eligible": False,
                              "torsion_rejected_because": str(exc)}

        if has_y:
            time, g_disp_y, rel_y, abs_y = building_y.compute_response(accel_y, disp_y, dt)
            building_y.save_to_csv(os.path.join(out_folder, "response_Y.csv"), prefix="Y")
            furn_y, npts_dec_y, q_y, rate_y = building_y.get_decimated_furniture()
        else:
            print("  Y component failed – skipping Y (no zero file created).")
            npts_dec_y = npts_dec_x
            furn_y = np.zeros((len(FURNITURE_CLASSES), NUM_STORIES, npts_dec_x), dtype=np.float32)
            rate_y = rate_x

        # X and Y share dt by construction (Y's own dt is discarded above
        # in favor of X's), so their decimation factors match too -- but
        # the two orientation files could in principle have different
        # record lengths, so defensively align to the shorter decimated
        # length rather than assume it.
        npts_dec = min(npts_dec_x, npts_dec_y)
        furn_x = furn_x[:, :, :npts_dec]
        furn_y = furn_y[:, :, :npts_dec]

        # (2 axes, 3 classes, N floors, npts_dec) float32 -- see
        # specs/05-structural-frame-furniture.md Part A/B and this file's
        # module docstring constants.
        furniture_stack = np.stack([furn_x, furn_y], axis=0).astype(np.float32)
        furniture_bin_path = os.path.join(out_folder, "furniture_response.bin")
        furniture_stack.tofile(furniture_bin_path)
        print(f"Saved furniture response to {furniture_bin_path} "
              f"(shape {furniture_stack.shape}, {furniture_stack.nbytes / 1e6:.2f} MB)")

        furniture_meta = {
            "artifact": "furniture_response.bin",
            "dtype": "float32",
            "shape": [2, len(FURNITURE_CLASSES), NUM_STORIES, npts_dec],
            "axes": ["X", "Y"],
            "classes": list(FURNITURE_CLASSES.keys()),
            "class_params": FURNITURE_CLASSES,
            "decimated_rate_hz": rate_x,
            "decimation_factor": q_x,
            "npts_decimated": npts_dec,
            "has_y": has_y,
        }

        # Save JSON metadata (both axes' modal results + frame geometry +
        # furniture metadata).
        save_building_data(os.path.join(out_folder, "building_data.json"),
                            building_x, building_y, furniture_meta,
                            reference_magnitude=reference_magnitude)

        # Cache the raw ground acceleration and displacement (both already
        # unit-converted) so the live-recompute backend
        # (specs/03-live-archetypes.md) can rebuild the response for new
        # building parameters without re-parsing the original PEER file or
        # redoing baseline correction on every request. Both are properties
        # of the recorded earthquake, independent of the building
        # parameters, so they only need to be computed once here.
        ground_accel_data = {
            "dt": dt,
            "X": accel_x.tolist(),
            "Y": accel_y.tolist() if accel_y is not None else None,
            "X_disp": disp_x.tolist(),
            "Y_disp": disp_y.tolist() if accel_y is not None else None,
            "reference_magnitude": reference_magnitude,
            # Which azimuth was assigned X and which Y. The assignment is a
            # sort (`assign_component_axes`), not a compass reading, so
            # recording it is what keeps a swapped pairing detectable after
            # the fact (spec correction C-4, verification V-6).
            **torsion_header,
        }
        with open(os.path.join(out_folder, "ground_accel.json"), 'w') as f:
            json.dump(ground_accel_data, f)
        print(f"Saved ground acceleration cache to {os.path.join(out_folder, 'ground_accel.json')}")

        # Precomputed ground-acceleration magnitude spectrum for the
        # frequency-domain panel (spec 6, Part A) -- see save_ground_spectrum
        # for why this can't just be computed client-side from ground_accel.json.
        spectrum_path = os.path.join(out_folder, "spectrum.json")
        save_ground_spectrum(spectrum_path, accel_x, accel_y, dt)
        print(f"Saved ground spectrum to {spectrum_path}")

    # After processing all folders, create a manifest file in out/
    manifest_path = os.path.join(out_dir, "folders.json")
    try:
        with open(manifest_path, 'w') as f:
            json.dump({"folders": folders}, f, indent=2)
        print(f"Created manifest: {manifest_path}")
    except Exception as e:
        print(f"Could not create manifest: {e}")

    print("\nAll folders processed.")
