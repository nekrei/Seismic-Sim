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
# Each axis's condensed stiffness is built from ONE planar frame (2 corner
# columns + 1 beam) and then scaled by this factor to account for the
# second, identical parallel frame on the far side of the building's other
# plan dimension -- see assemble_frame_stiffness's docstring and spec A4.
N_PARALLEL_FRAMES = 2
# Furniture time-series are decimated toward this rate before being written
# to out/<record>/furniture_response.bin -- see furniture_decimation's
# docstring for why this is a legitimate sampling-theorem application, not
# just a size fudge.
FURNITURE_TARGET_RATE_HZ = 50.0

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


def frame_span(axis):
    """
    Beam span for the planar frame resisting sway in `axis`. The beam
    connecting the two columns of that frame runs parallel to the sway
    direction, so its span is the plan dimension in that same direction.
    """
    if axis == "X":
        return PLAN_SPAN_X
    elif axis == "Y":
        return PLAN_SPAN_Y
    else:
        raise ValueError(f"axis must be 'X' or 'Y', got {axis!r}")


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
    """
    size = 2 * N
    K = np.zeros((size, size))
    c = 2.0 * E * I_c

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
        add(t_i, t_i, 12 * E * I_b / L)

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
    """
    K_full = assemble_frame_stiffness(N, E, I_c, I_b, h, L)
    K_one_frame = condense_rotations(K_full, N)
    return N_PARALLEL_FRAMES * K_one_frame


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
                 beam_depth=1.50, axis="X", E=E_CONCRETE):
        self.N = num_stories
        self.m = mass_per_floor
        self.zeta = zeta
        self.h = story_height
        self.column_depth_x = column_depth_x
        self.column_depth_y = column_depth_y
        self.beam_depth = beam_depth
        self.axis = axis
        self.E = E
        self._build_matrices()
        self._modal_analysis()

    def _build_matrices(self):
        self.M = np.eye(self.N) * self.m

        self.I_c = column_inertia(self.column_depth_x, self.column_depth_y, self.axis)
        self.I_b = beam_inertia(self.beam_depth, BEAM_WIDTH)
        self.L = frame_span(self.axis)
        self.K = build_condensed_K(self.N, self.E, self.I_c, self.I_b, self.h, self.L)

        # Base-story (floor 1) condensed lateral stiffness -- useful
        # metadata (period readout, sanity display), but explicitly NOT a
        # universal "k": unlike the old abstract-spring model, every
        # floor's diagonal entry in a condensed frame stiffness matrix is
        # generally different (see verification check 3's "no spurious
        # long-range coupling" wording, not exact tridiagonality).
        self.story_stiffness = float(self.K[0, 0])

    def _modal_analysis(self):
        """Solve generalized eigenvalue problem: K φ = ω² M φ using scipy.linalg.eigh."""
        # Use SciPy's eigh which supports the b matrix for generalized problems
        eigvals, eigvecs = scipy_eigh(self.K, self.M, lower=True)

        # Sort by ascending eigenvalues
        idx = np.argsort(eigvals)
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

        return self.time, self.ground_disp, self.floor_disp_rel, self.floor_disp_abs

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


def save_building_data(filename, building_x, building_y, furniture_meta=None):
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
        "story_height": float(building_x.h),
        "damping_ratio": float(building_x.zeta),

        # Frame geometry (spec A2) -- shared by both axes.
        "elastic_modulus_Pa": float(building_x.E),
        "column_depth_x": float(building_x.column_depth_x),
        "column_depth_y": float(building_x.column_depth_y),
        "beam_depth": float(building_x.beam_depth),
        "beam_width": float(BEAM_WIDTH),
        "plan_span_x": float(PLAN_SPAN_X),
        "plan_span_y": float(PLAN_SPAN_Y),

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

    folders = [f for f in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, f))]
    if not folders:
        print("No subfolders found in data/. Please place earthquake folders inside data/.")
        sys.exit(1)

    for folder in folders:
        folder_path = os.path.join(data_dir, folder)
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
        building_x = MDOF_ShearBuilding(
            NUM_STORIES, mass_per_floor=1000e3, zeta=0.05,
            column_depth_x=COLUMN_DEPTH_X, column_depth_y=COLUMN_DEPTH_Y,
            beam_depth=BEAM_DEPTH, axis="X",
        )
        building_y = MDOF_ShearBuilding(
            NUM_STORIES, mass_per_floor=1000e3, zeta=0.05,
            column_depth_x=COLUMN_DEPTH_X, column_depth_y=COLUMN_DEPTH_Y,
            beam_depth=BEAM_DEPTH, axis="Y",
        )

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
                            building_x, building_y, furniture_meta)

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
        }
        with open(os.path.join(out_folder, "ground_accel.json"), 'w') as f:
            json.dump(ground_accel_data, f)
        print(f"Saved ground acceleration cache to {os.path.join(out_folder, 'ground_accel.json')}")

    # After processing all folders, create a manifest file in out/
    manifest_path = os.path.join(out_dir, "folders.json")
    try:
        with open(manifest_path, 'w') as f:
            json.dump({"folders": folders}, f, indent=2)
        print(f"Created manifest: {manifest_path}")
    except Exception as e:
        print(f"Could not create manifest: {e}")

    print("\nAll folders processed.")