# Seismic-Sim

This project takes a real recording of an earthquake (how much the ground
shook, second by second) and answers one question: **if a building stood on
that ground, how would each of its floors move?** It then draws that motion
as an animated 3D building swaying in your browser.

You do not need any prior background in engineering, physics, or programming
to follow this document. Every term is explained the first time it's used.
For the *math* behind the simulation (in similarly beginner-friendly detail,
with diagrams and worked examples), see **`Seismic-Sim Math.pdf`** in this
folder — read that when you want to understand *why* the numbers come out
the way they do. This README is about *what each file does* and *how the
pieces connect*.

## The three things this project does, in plain English

1. **Read a real earthquake recording.** Seismologists place instruments
   called seismometers on the ground near past earthquakes. Each instrument
   produces a file that's basically a long list of numbers: "at this instant
   the ground was accelerating this fast, in this direction." This project
   reads those files (a standard format called **PEER**, named after the
   research center that publishes them).

2. **Simulate a building's reaction.** A tall building doesn't move as one
   rigid block when the ground shakes — the base moves with the ground, but
   each floor above it lags, overshoots, and sways somewhat independently,
   like a stack of shelves connected by springs. This project models a
   building as exactly that: a stack of floors connected by springs, and
   calculates how far *each individual floor* moves at *each individual
   instant* of the recording.

3. **Animate the result.** The floor-by-floor motion is played back as a 3D
   building sway animation in a web browser, using real numbers from step 2
   — not a fake or decorative wobble.

## Words you'll run into

A quick glossary — just enough to read the rest of this file comfortably.
The math PDF explains all of these properly, with pictures.

| Term | Plain-English meaning |
|---|---|
| **Ground motion** | How the ground itself moved during the earthquake — the raw recording. |
| **PEER file (.AT2 / .DT2)** | The standard file format these recordings come in. `.AT2` = acceleration recording, `.DT2` = displacement recording. |
| **Floor / story** | One level of the building. This project treats every floor as a single point with a single weight — it doesn't model rooms, columns, etc. |
| **MDOF (multi-degree-of-freedom)** | Engineering jargon for "a model with several moving parts that can each move somewhat independently" — here, each floor is one "degree of freedom." |
| **Mode / mode shape** | A building doesn't sway randomly — it has a small number of *natural* sway patterns (like a guitar string's harmonics), and every real motion is a mix of these. Explained fully, with pictures, in the math PDF. |
| **Damping** | Friction-like energy loss that makes swaying die down over time instead of continuing forever. |
| **FFT (Fast Fourier Transform)** | A method for taking a wiggly signal (like the ground shaking) and figuring out which "pure tones" it's built from — similar to how your ear splits a chord into individual notes. Used here to solve the physics quickly. |
| **Amplify** | A slider in the visualization that exaggerates the motion so tiny, real sway (anywhere from a fraction of a millimeter to tens of centimeters, depending on the earthquake) is visible on screen. Its default is auto-computed per record, and its range is logarithmic — it never affects the underlying numbers, only the picture. Switching records recomputes this default; adjusting a Building Parameter slider never does — those are independent knobs. |

## How the pieces connect

```
data/<record>/*.AT2, *.DT2          ← a real earthquake recording (you supply these)
        │
        ▼
mdof_response.py                     ← builds the building model, computes floor-by-floor motion
        │
        ▼
out/<record>/response_X.csv          ← every floor's position, at every instant, saved to a spreadsheet-like file
out/<record>/response_Y.csv
out/<record>/building_data.json      ← a summary: number of floors, natural sway patterns, etc.
out/<record>/ground_accel.json       ← the raw ground motion, cached for live recompute
out/folders.json                     ← a list of which earthquake recordings have been processed
        │                        │           │
        ▼                        ▼           │
plot_response.py              index.html      │ POST /compute (live building-
   → static picture (PNG)   (fetch + three.js)│  parameter sliders, debounced)
                             animated 3D       ▼
                             building sway  server.py
                                    ▲       (reruns MDOF_ShearBuilding with
                                    └────── your slider values, reusing the
                                 binary response  cached ground motion)
```

In short: you run `mdof_response.py` once per batch of earthquake
recordings, and it does all the physics up front, saving the results as
plain files. The static plots and the *initial* 3D animation just *read
those already-computed results* — no physics happens live in the browser
for those. The **Building Parameters** sliders are the one exception: they
POST to `server.py`, which reruns the same physics live with your chosen
parameters (see "Running it yourself" below).

## File by file

### `mdof_response.py` — the only file that does real physics

This is the heart of the project. It has three jobs:

1. **Read the earthquake file.** Functions `parse_peer_file` and
   `parse_peer_displacement_file` open a `.AT2`/`.DT2` file and pull out:
   the list of numbers (the actual recording), the time gap between each
   number (`dt` — e.g. one measurement every 0.01 seconds), and the units
   used (acceleration files are usually measured in multiples of Earth's
   gravity, "g"; this gets converted to standard metric units, m/s²).
   `get_orientation_from_filename` looks at the filename to figure out
   which horizontal direction (north-south? east-west?) that particular
   file measured, since real ground shaking is recorded in more than one
   direction at once.

2. **Build a virtual building and see how it reacts** — this is the
   `MDOF_ShearBuilding` class. It:
   - Decides how heavy each floor is and how "springy" the connections
     between floors are, so the building's natural sway speed matches a
     realistic target. The building is modeled as a real free-standing
     cantilever — fixed at the base, genuinely free at the roof (nothing
     holds the top floor back except the floor below it).
   - Works out the building's small number of *natural sway patterns*
     (the "modes" from the glossary above) — this is the part of the code
     that needs the most background to understand, and it's explained
     step by step with a worked numeric example in the math PDF.
   - Combines the earthquake recording with those sway patterns to compute
     exactly how far every floor moves, at every instant in the recording.
   - Saves everything to files (`save_to_csv`, `save_to_json`).

3. **The bottom of the file (`if __name__ == "__main__":`)** loops over
   every earthquake recording you've placed in the `data/` folder, runs the
   steps above on each one, and writes results into `out/`.

   One setting worth knowing: `NUM_STORIES = 7` is hardcoded near the
   bottom — every recording currently gets simulated with a 7-floor
   building. There's no menu option to change this per-recording yet; you'd
   edit that line directly.

### `plot_response.py` — makes a static picture

Reads one earthquake's results from `out/` and draws a normal 2D chart:
time along the bottom, how far each floor moved up the side, one colored
line per floor plus a black line for the ground itself. Saved as a PNG
image. This is independent of the 3D browser animation — a quick way to
eyeball whether a result "looks reasonable" without opening a browser.

### `index.html` — the real, interactive 3D animation

Open this file **through a local web server** (see below — opening it
directly by double-clicking won't work) to watch the actual simulation
results. It:
- Shows a dropdown listing every earthquake recording that's been
  processed.
- Loads that recording's results and builds a matching 3D building, floor
  by floor.
- Plays back the *real* computed floor positions as an animation, frame by
  frame, matching the timing of the original recording.
- Gives you controls: play/pause, playback speed, a scrubber to jump to
  any moment, and the **amplify** slider from the glossary above.
- Has a **Building Parameters** panel (stories, mass per floor, damping,
  target period) — moving any of these sends your values to `server.py`,
  which recomputes the *actual physics* for that building live (not a
  visual trick) and updates the animation, roughly 200-400ms after you stop
  moving the slider. Requires `server.py` running, not a plain static
  server — see "Running it yourself" below.
- Reframes the camera automatically whenever the building's height changes
  (e.g. the Stories slider), easing to the new shot instead of snapping, so
  a 20-story building is never cut off — the camera distance, its far
  clip plane, and the scene fog range are all derived from the same fit
  calculation instead of being independent fixed constants.
- Shows a live **Shaking** meter (how strong the *current instant* of real
  ground motion is, relative to that record's own peak) alongside a subtle
  camera shake at high intensity, and renders lit windows/the roof beacon
  through a bloom pass for a bit more visual punch — all purely cosmetic,
  none of it feeds back into the physics.

**This is the file to open when you want to see whether the simulation
"looks right."**

### `demo3js.html` — a look-alike demo with no real physics

This file looks similar to `index.html` — a 3D building that sways — but
it is **not connected to any earthquake data at all**. Its sway is just a
mathematical wave pattern (a sine wave) picked to look plausible. It exists
only to test/showcase the building's 3D appearance (shape, lighting,
camera) in isolation. If you're checking whether the *physics* is correct,
this is the wrong file — use `index.html`.

### `server.py` — the live backend for the parameter sliders

A small Flask app with two jobs: (1) serve the project's static files
(replacing the plain `python -m http.server` used before this existed), and
(2) handle `POST /compute` — takes a record name plus building parameters
(stories, mass/floor, damping, period) from `index.html`'s sliders, rebuilds
the same `MDOF_ShearBuilding` model from `mdof_response.py` with those
values, and returns freshly computed floor displacements. It reuses each
record's cached `ground_accel.json` rather than re-reading the original
earthquake file, and returns the result as compact binary data (not JSON) —
sending the full time series as JSON text turned out to be slow enough to
matter, since it's transferred on every slider move.

### `style.css`

A handful of small visual tweaks (dropdown menu colors) for `index.html`.
Everything else is styled inline inside the HTML files themselves.

### `out/` — the computed results (already included in this repo)

One folder per earthquake recording, containing:
- `building_data.json` — a summary of the virtual building used: how many
  floors, its natural sway speeds, sway-pattern shapes, etc.
- `response_X.csv` / `response_Y.csv` — the actual result: a spreadsheet
  where each row is one instant in time, and each column is one floor's
  position at that instant (X = one horizontal direction, Y = the other).
- `response_plot_X.png` — the static picture from `plot_response.py`.
- `ground_accel.json` — the raw ground acceleration and displacement used as
  input, cached so `server.py`'s live parameter sliders can recompute a
  building's response without re-reading the original earthquake file.

Plus `out/folders.json`, a simple list of which recordings have been
processed (this is what fills the dropdown in `index.html`).

### `data/` — the raw earthquake recordings (not included in this repo)

Expected layout: `data/<recording-name>/*.AT2` (plus matching `*.DT2` files
if you have them). This folder is intentionally excluded from the repo
(see `.gitignore`) — you have to supply real recordings yourself, for
example from the public PEER strong-motion database. The repo currently
ships the *already-computed results* for ten recordings (see `out/`
above) without the raw recordings that produced them.

## Running it yourself

1. **To regenerate results from `data/`:** run `mdof_response.py`, then
   optionally `plot_response.py` for static pictures.
2. **To view the animation (including the live parameter sliders):** run
   `server.py` from this folder, then open `http://127.0.0.1:8000/` in your
   browser (prefer `127.0.0.1` over `localhost` — some tools resolve
   `localhost` slowly on Windows). `server.py` handles both serving the
   page and the sliders' live recompute requests — a plain static server
   (like `python -m http.server`) will load the page fine but the sliders
   will fail, since there's no `/compute` to POST to.

## The live version, hosted

This project is also hosted online. GitHub Pages (static-only — no
Python) serves the frontend and the precomputed `out/` data directly.
The live Building Parameters sliders need real Python compute, though,
which Pages can't run — so `index.html` sends those requests to a
separately-deployed backend instead: see `seismic-sim-backend` (a trimmed
mirror of `server.py`, deployed on Render — link: `TODO: fill in once
created`). That repo's own README explains why it exists and what it
contains.

## Current state (as of this pull)

- The pipeline runs end-to-end for the 10 recordings already included in
  `out/`, using a corrected free-top cantilever building model — fixed at
  the base, genuinely free at the roof, matching how a real building is
  actually supported (an earlier version incorrectly modeled it as fixed
  at both ends).
- Only the X-direction static plot is generated by default.
- `out/`'s precomputed results still use a fixed 7-floor building (see
  `NUM_STORIES` above) — but stories, mass, damping, and target period are
  all now adjustable *live* via `index.html`'s Building Parameters sliders,
  backed by `server.py`. Per-floor stiffness variation (e.g. a "soft story")
  isn't supported yet.
- No automated test suite as a project convention.
- Two bugs in the Building Parameters panel are fixed: a parameter tweak
  was silently changing the Amplify zoom (now decoupled — Amplify only
  auto-rescales on an actual record switch), and increasing story count
  used to cut the building off on screen (the camera, its far clip plane,
  and the scene fog range are now all derived from one fit calculation
  instead of independent fixed constants, and ease into the new framing
  instead of snapping).
- Two further bugs found and fixed: Stories = 1 used to render as a solid
  black box (a `0/0` divide in the floor color gradient), and switching
  the Earthquake Record while any Building Parameter was off its default
  used to silently reload the static 7-story data, leaving the panel's
  sliders showing values that no longer matched the rendered building.

Keep this file — and the math PDF — updated as the project evolves. That's
the whole point of having them.
