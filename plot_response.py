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
        if PLOT_Y_AXIS:
            plot_response(folder_path, axis='Y')

    print("\nAll plots generated.")

if __name__ == "__main__":
    main()