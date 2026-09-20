import numpy as np
from scipy.fft import fft, ifft, fftfreq
from scipy.linalg import eigh as scipy_eigh
from scipy.signal import butter, filtfilt, decimate
import csv
import json
import re
import os
import sys

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

    K_G = np.zeros((N, N))
    for i in range(N):
        k_above = k_g[i + 1] if i + 1 < N else 0.0
        K_G[i, i] = k_g[i] + k_above
        if i + 1 < N:
            K_G[i, i + 1] = -k_g[i + 1]
            K_G[i + 1, i] = -k_g[i + 1]
    return K_G, k_g, P


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
                 axial_cap_factor=AXIAL_CAP_FACTOR):
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

    def compute_response(self, acceleration, displacement, dt):
        """
        Compute floor displacement time histories given ground acceleration and displacement.
        """
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


def transfer_function(f_hz, omega_n, phi, Gamma, zeta, floor_idx):
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
    for j in range(n_modes):
        wn = omega_n[j]
        denom = (wn**2 - omega**2 + 1j * 2 * zeta * wn * omega)
        H_j = 1.0 / denom
        mode_terms[j] = phi[floor_idx, j] * Gamma[j] * H_j
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
        "theta_demand_X": building_x.theta_demand.tolist(),
        "theta_demand_Y": (building_y.theta_demand.tolist()
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

        x_orient = at2_orients[0]
        y_orient = at2_orients[1]
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
        accel_y, disp_y, _ = load_component(y_orient, "Y", building_y)
        has_y = accel_y is not None
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