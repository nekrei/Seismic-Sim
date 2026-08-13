import numpy as np
from scipy.fft import fft, ifft, fftfreq
from scipy.linalg import eigh as scipy_eigh
import csv
import json
import re
import os
import sys

# Constants
G_TO_MS2 = 9.80665


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
    def __init__(self, num_stories, mass_per_floor=1000e3, zeta=0.05,
                 story_height=3.5, T1_factor=0.1):
        self.N = num_stories
        self.m = mass_per_floor
        self.zeta = zeta
        self.h = story_height
        self.T1_factor = T1_factor
        self._build_matrices()
        self._modal_analysis()

    def _build_matrices(self):
        self.M = np.eye(self.N) * self.m
        T1 = self.T1_factor * self.N
        omega1 = 2 * np.pi / T1
        sin_term = np.sin(np.pi / (2 * (self.N + 1)))
        self.k = self.m * (omega1 / (2 * sin_term))**2

        self.K = np.zeros((self.N, self.N))
        for i in range(self.N):
            if i > 0:
                self.K[i, i-1] = -self.k
            if i < self.N - 1:
                self.K[i, i+1] = -self.k
            self.K[i, i] = 2 * self.k

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

        A_fft = fft(acceleration)
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

        self.floor_disp_rel = np.zeros((self.N, self.npts))
        for i in range(self.N):
            self.floor_disp_rel += np.outer(self.phi[:, i], q_time[i, :])

        self.floor_disp_abs = self.floor_disp_rel + self.ground_disp[np.newaxis, :]

        return self.time, self.ground_disp, self.floor_disp_rel, self.floor_disp_abs

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

    def save_to_json(self, filename):
        data = {
            "num_stories": int(self.N),
            "story_height": float(self.h),
            "natural_frequencies_Hz": (self.omega_n / (2 * np.pi)).tolist(),
            "mode_shapes": self.phi.tolist(),
            "participation_factors": self.Gamma.tolist(),
            "damping_ratio": float(self.zeta)
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

        building = MDOF_ShearBuilding(NUM_STORIES, mass_per_floor=1000e3, zeta=0.05)

        def load_component(orient, label):
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
        accel_x, disp_x, dt = load_component(x_orient, "X")
        if accel_x is None:
            print("  Failed to load X component. Skipping folder.")
            continue

        time, g_disp_x, rel_x, abs_x = building.compute_response(accel_x, disp_x, dt)
        building.save_to_csv(os.path.join(out_folder, "response_X.csv"), prefix="X")

        # Process Y
        accel_y, disp_y, _ = load_component(y_orient, "Y")
        if accel_y is not None:
            time, g_disp_y, rel_y, abs_y = building.compute_response(accel_y, disp_y, dt)
            building.save_to_csv(os.path.join(out_folder, "response_Y.csv"), prefix="Y")
        else:
            print("  Y component failed – skipping Y (no zero file created).")

        # Save JSON metadata
        building.save_to_json(os.path.join(out_folder, "building_data.json"))
        
    # After processing all folders, create a manifest file in out/
    manifest_path = os.path.join(out_dir, "folders.json")
    try:
        with open(manifest_path, 'w') as f:
            json.dump({"folders": folders}, f, indent=2)
        print(f"Created manifest: {manifest_path}")
    except Exception as e:
        print(f"Could not create manifest: {e}")

    print("\nAll folders processed.")