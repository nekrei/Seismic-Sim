#!/usr/bin/env python3
"""
Plot time-domain displacement responses for all earthquake records.

Usage:
    python plot_response.py

The script looks for folders inside 'out/' and generates a plot for each.
"""
import os
import json
import csv
import numpy as np
import matplotlib.pyplot as plt
import mdof_response as mr
from matplotlib.ticker import AutoMinorLocator

# ------------------------------------------------------------
#  Configuration
# ------------------------------------------------------------
OUT_DIR = "out"               # directory containing earthquake folders
PLOT_Y_AXIS = False           # set True to also plot Y-component (if available)

# ------------------------------------------------------------
#  Helper functions
# ------------------------------------------------------------
def load_csv_data(csv_path):
    """Load a CSV with header, returns time and data columns as numpy arrays."""
    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)   # skip header
        data = np.array([list(map(float, row)) for row in reader])
    return data[:, 0], data[:, 1:]

def plot_response(folder_path, axis='X'):
    """
    Generate a time-domain displacement plot for a single earthquake folder.

    Parameters:
        folder_path: str, path to the folder (e.g., 'out/ANZA1_CIDLA')
        axis: str, 'X' or 'Y' to select which component to plot.
    """
    # --- load building metadata ---
    meta_path = os.path.join(folder_path, 'building_data.json')
    if not os.path.exists(meta_path):
        print(f"  Warning: {meta_path} not found, skipping.")
        return

    with open(meta_path, 'r') as f:
        meta = json.load(f)
    num_stories = meta['num_stories']

    # --- load response CSV ---
    csv_name = f'response_{axis}.csv'
    csv_path = os.path.join(folder_path, csv_name)
    if not os.path.exists(csv_path):
        print(f"  Warning: {csv_path} not found, skipping {axis}-component.")
        return

    time, disp = load_csv_data(csv_path)
    # disp columns: [ground, floor1, floor2, ...]
    ground = disp[:, 0]
    floors = disp[:, 1:1 + num_stories]   # only take the number of stories we have

    # --- create the plot ---
    fig, ax = plt.subplots(figsize=(12, 6))

    # Ground (black, thicker)
    ax.plot(time, ground, label='Ground', linewidth=2, color='black')

    # Floors with a colormap
    cmap = plt.cm.plasma
    colors = [cmap(i / max(1, num_stories - 1)) for i in range(num_stories)]
    for i in range(num_stories):
        ax.plot(time, floors[:, i], label=f'Floor {i+1}',
                color=colors[i], alpha=0.8, linewidth=1.2)

    # --- styling ---
    ax.set_xlabel('Time (s)', fontsize=12)
    ax.set_ylabel(f'Absolute Displacement ({axis}-axis) (m)', fontsize=12)
    title = f'{os.path.basename(folder_path)} – {axis}-axis Response'
    ax.set_title(title, fontsize=14, fontweight='semibold')
    ax.grid(True, alpha=0.3, linestyle='--')
    ax.xaxis.set_minor_locator(AutoMinorLocator())
    ax.yaxis.set_minor_locator(AutoMinorLocator())

    # Legend – place outside if too many floors
    if num_stories <= 6:
        ax.legend(loc='upper right', fontsize=9, ncol=2)
    else:
        ax.legend(loc='center left', bbox_to_anchor=(1, 0.5), fontsize=8, ncol=1)

    plt.tight_layout()

    # --- save ---
    out_path = os.path.join(folder_path, f'response_plot_{axis}.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"  Saved plot: {out_path}")

def plot_spectrum(folder_path, axis='X'):
    """
    Generate the frequency-domain figure -- spectrum_plot_<axis>.png, the
    offline twin of index.html's drawer (spec 6 Part D). Three stacked
    panels sharing one log-frequency axis, so "input x transfer = output"
    reads as vertical alignment in the report exactly as it does on screen:

        Input     |A_g(f)|      the record's ground-acceleration spectrum
        Transfer  |T_i(jw)|     the building's frequency response (+ per-mode terms)
        Output    |U_i^rel(f)|  the roof's relative-displacement spectrum

    The Output panel is an INDEPENDENT FFT of (floor - ground) read back
    from response_<axis>.csv -- the same trimmed arrays the browser holds.
    Never derive it by multiplying the other two panels: that would make
    the identity true by construction instead of by measurement, which is
    the one thing this figure exists to demonstrate.
    """
    meta_path = os.path.join(folder_path, 'building_data.json')
    spec_path = os.path.join(folder_path, 'spectrum.json')
    csv_path = os.path.join(folder_path, f'response_{axis}.csv')
    for path in (meta_path, spec_path, csv_path):
        if not os.path.exists(path):
            print(f"  Warning: {path} not found, skipping {axis} spectrum plot.")
            return

    with open(meta_path, 'r') as f:
        meta = json.load(f)
    with open(spec_path, 'r') as f:
        spec = json.load(f)

    g_mag = spec.get(f'{axis}_mag')
    freqs_hz = meta.get(f'natural_frequencies_Hz_{axis}')
    phi = meta.get(f'mode_shapes_{axis}')
    gamma = meta.get(f'participation_factors_{axis}')
    if g_mag is None or freqs_hz is None or phi is None or gamma is None:
        print(f"  Warning: no {axis} spectrum/modal data, skipping.")
        return

    f_hz = np.array(spec['f_Hz'])
    g_mag = np.array(g_mag)
    num_stories = meta['num_stories']
    roof = num_stories - 1

    # --- Transfer: analytic, at the artifact's own bin centres (shared grid) ---
    T, mode_terms = mr.transfer_function(
        f_hz, 2 * np.pi * np.array(freqs_hz), np.array(phi),
        np.array(gamma), meta['damping_ratio'], roof)

    # --- Output: independent FFT of the roof's relative displacement ---
    time, disp = load_csv_data(csv_path)
    dt = float(time[1] - time[0])
    u_rel = disp[:, 1 + roof] - disp[:, 0]
    f_out, out_mag = mr.log_bin_spectrum(u_rel, dt)

    def to_db(mag):
        return 20 * np.log10(np.maximum(np.asarray(mag), 1e-12))

    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    cmap = plt.cm.plasma
    panels = [
        (axes[0], f_hz, g_mag, [], f'Input  $|A_g(f)|$  ({axis})', 'black', 2),
        (axes[1], f_hz, np.abs(T), np.abs(mode_terms),
         rf'Transfer  $|T_i(j\omega)|$  (floor {roof + 1})', cmap(0.55), 2),
        (axes[2], f_out, out_mag, [], f'Output  $|U^{{rel}}_i(f)|$  (floor {roof + 1})', cmap(0.15), 2),
    ]
    for ax, xf, mag, extra, label, color, lw in panels:
        db = to_db(mag)
        db_max = float(db.max())
        for k, term in enumerate(extra):
            ax.semilogx(xf, to_db(term), color=cmap(k / max(1, len(extra) - 1)),
                        alpha=0.35, linewidth=0.9,
                        label='per-mode terms' if k == 0 else None)
        ax.semilogx(xf, db, color=color, linewidth=lw)
        ax.set_xlim(0.1, 50)
        ax.set_ylim(db_max - 70, db_max + 5)   # 70 dB below this panel's own peak, matching the drawer
        ax.set_ylabel('dB', fontsize=10)
        ax.grid(True, alpha=0.3, linestyle='--', which='both')
        ax.yaxis.set_minor_locator(AutoMinorLocator())
        ax.set_title(label, fontsize=11, loc='left')
        if len(extra):
            ax.legend(loc='upper right', fontsize=8)

    # Grey the region above this record's own Nyquist -- zero-width for
    # every record in this dataset (dt 0.005/0.01), kept correct for any
    # future lower-rate record. The annotation below is what makes the
    # sampling rate visible today.
    nyq = spec['nyquist_Hz']
    if nyq < 50:
        for ax in axes:
            ax.axvspan(nyq, 50, color='black', alpha=0.25)

    axes[2].set_xlabel('Frequency (Hz)', fontsize=12)
    fig.suptitle(f'{os.path.basename(folder_path)} - {axis}-axis frequency domain '
                 f'(input x transfer = output)', fontsize=14, fontweight='semibold')
    axes[0].annotate(f'fs = {1 / spec["dt"]:.0f} Hz  ·  Nyquist {nyq:.0f} Hz',
                     xy=(1, 1.02), xycoords='axes fraction', ha='right',
                     fontsize=9, color='0.35')

    plt.tight_layout()
    out_path = os.path.join(folder_path, f'spectrum_plot_{axis}.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"  Saved plot: {out_path}")

# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    if not os.path.exists(OUT_DIR):
        print(f"Error: '{OUT_DIR}' directory not found.")
        return

    # Get list of earthquake folders
    folders = []
    manifest = os.path.join(OUT_DIR, 'folders.json')
    if os.path.exists(manifest):
        with open(manifest, 'r') as f:
            data = json.load(f)
            folders = data.get('folders', [])
    else:
        # fallback: list subdirectories in out/
        folders = [f for f in os.listdir(OUT_DIR)
                   if os.path.isdir(os.path.join(OUT_DIR, f))]

    if not folders:
        print("No earthquake folders found.")
        return

    print(f"Found {len(folders)} folder(s): {', '.join(folders)}")

    for folder in folders:
        folder_path = os.path.join(OUT_DIR, folder)
        print(f"\nProcessing: {folder}")
        plot_response(folder_path, axis='X')
        plot_spectrum(folder_path, axis='X')
        if PLOT_Y_AXIS:
            plot_response(folder_path, axis='Y')
            plot_spectrum(folder_path, axis='Y')

    print("\nAll plots generated.")

if __name__ == "__main__":
    main()