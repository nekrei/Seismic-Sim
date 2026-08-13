def compute_response(self, acceleration=None, displacement=None, dt=None):
    """
    Compute the displacement time histories of all floors.

    Parameters
    ----------
    acceleration : 1D array, optional
        Ground acceleration time series (m/s²). Used for the structural forcing.
    displacement : 1D array, optional
        Ground displacement time series (meters). Used for the absolute base motion.
    dt : float, required
        Time step (s).

    Best Practice:
        Provide BOTH acceleration (from .AT2) and displacement (from .DT2).
        If only one is provided, the other will be estimated (via integration/differentiation).
    """
    if dt is None:
        raise ValueError("dt must be provided.")

    # --- Determine Ground Acceleration (Forcing function) ---
    if acceleration is not None:
        accel = acceleration
        print("Using provided acceleration (.AT2) for structural forcing.")
    elif displacement is not None:
        print("Warning: Acceleration not provided. Differentiating displacement to get acceleration (may amplify noise).")
        accel = self._differentiate_to_accel(displacement, dt)
    else:
        raise ValueError("At least one of acceleration or displacement must be provided.")

    # --- Determine Ground Displacement (Absolute base motion) ---
    if displacement is not None:
        ground_disp = displacement
        print("Using provided displacement (.DT2) for absolute base motion.")
    elif acceleration is not None:
        print("Warning: Displacement not provided. Integrating acceleration to get displacement (may introduce drift).")
        ground_disp = self._integrate_accel(acceleration, dt)
    else:
        raise ValueError("At least one of acceleration or displacement must be provided.")

    # Store for later use
    self.accel = accel
    self.ground_disp = ground_disp
    self.dt = dt
    self.npts = len(accel)
    self.time = np.arange(self.npts) * dt

    # ---- Modal response via FFT (using the provided acceleration) ----
    A_fft = fft(accel)
    freqs = fftfreq(self.npts, dt)
    omega = 2 * np.pi * freqs

    q_time = np.zeros((self.N, self.npts))
    for i in range(self.N):
        wn = self.omega_n[i]
        z = self.zeta
        Gamma_i = self.Gamma[i]

        denom = (wn**2 - omega**2 + 1j * 2 * z * wn * omega)
        H = -Gamma_i / denom
        Q_fft = H * A_fft
        q = np.real(ifft(Q_fft))
        q_time[i, :] = q

    # Reconstruct relative displacement
    self.floor_disp_rel = np.zeros((self.N, self.npts))
    for i in range(self.N):
        self.floor_disp_rel += np.outer(self.phi[:, i], q_time[i, :])

    # Absolute displacement = ground motion + relative bending
    self.floor_disp_abs = self.floor_disp_rel + self.ground_disp[np.newaxis, :]

    return self.time, self.ground_disp, self.floor_disp_rel, self.floor_disp_abs

def _integrate_accel(self, accel, dt):
    """
    Double integrate acceleration to displacement using FFT with a high‑pass
    filter to remove drift.
    """
    n = len(accel)
    freqs = fftfreq(n, dt)
    omega = 2 * np.pi * freqs
    # FFT of acceleration
    A = fft(accel)
    # High‑pass filter: remove frequencies below 0.1 Hz to avoid DC drift.
    # Use a Butterworth filter in frequency domain (ideal filter with smooth transition).
    # Alternatively, we can apply a high‑pass in frequency domain by zeroing low freqs.
    # We'll use a simple sharp cut‑off.
    f_cut = 0.1  # Hz
    idx_low = np.abs(freqs) < f_cut
    A_filtered = A.copy()
    A_filtered[idx_low] = 0.0
    # Integrate twice in frequency domain: displacement = - A / omega^2
    # Avoid division by zero at omega=0 (already zeroed by filter)
    omega_sq = omega**2
    omega_sq[0] = 1.0  # to avoid division by zero, but its contribution is zero anyway
    disp_fft = -A_filtered / omega_sq
    disp_fft[0] = 0.0  # DC component
    disp = np.real(ifft(disp_fft))
    # Remove mean (just in case)
    disp -= np.mean(disp)
    return disp

def _differentiate_to_accel(self, disp, dt):
    """
    Double differentiate displacement to acceleration using FFT with a high‑pass
    filter to remove low‑frequency noise.
    """
    n = len(disp)
    freqs = fftfreq(n, dt)
    omega = 2 * np.pi * freqs

    # FFT of displacement
    D = fft(disp)

    # High‑pass filter: remove frequencies below 0.05 Hz to avoid DC drift.
    f_cut = 0.05  # Hz (slightly lower than integration to preserve long-period content)
    idx_low = np.abs(freqs) < f_cut
    D_filtered = D.copy()
    D_filtered[idx_low] = 0.0

    # Differentiate twice in frequency domain: acceleration = - omega^2 * D
    # Avoid division by zero at omega=0 (already zeroed by filter)
    omega_sq = omega**2
    omega_sq[0] = 1.0  # placeholder, contribution is zero anyway
    accel_fft = -omega_sq * D_filtered
    accel_fft[0] = 0.0  # DC component

    accel = np.real(ifft(accel_fft))
    # Remove mean (just in case)
    accel -= np.mean(accel)
    return accel

