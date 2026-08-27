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
   each floor above it lags, overshoots, and sways somewhat independently.
   This project models a building as a real structural frame: concrete
   columns and beams at the four corners of each floor, connected the way
   an actual building's skeleton is connected (not an abstract stack of
   floors on springs — see "Words you'll run into" below), and calculates
   how far *each individual floor* moves at *each individual instant* of
   the recording. It also simulates a handful of pieces of furniture
   (tables, chairs, a fan) on every floor, each swaying slightly on its own
   relative to the floor it sits on — the same kind of physics as the
   building itself, just applied a second time to a much smaller, lighter
   system riding on top of it.

3. **Animate the result.** The floor-by-floor (and furniture-by-furniture)
   motion is played back as a 3D building sway animation in a web browser,
   using real numbers from step 2 — not a fake or decorative wobble. The
   building is drawn as an open cutaway — no exterior walls — so the
   columns, beams, floor slabs, and furniture are all visible at once.

## Words you'll run into

A quick glossary — just enough to read the rest of this file comfortably.
The math PDF explains all of these properly, with pictures.

| Term | Plain-English meaning |
|---|---|
| **Ground motion** | How the ground itself moved during the earthquake — the raw recording. |
| **PEER file (.AT2 / .DT2)** | The standard file format these recordings come in. `.AT2` = acceleration recording, `.DT2` = displacement recording. |
| **Floor / story** | One level of the building. Every floor moves as a single rigid unit (one lateral position per floor), but the building's *stiffness* now comes from real column and beam dimensions, not an abstract spring. |
| **Frame (columns + beams)** | The building's actual skeleton: four corner columns per floor connected by four beams, one bay wide, fixed at the ground floor's base and genuinely free at the roof. Column cross-sections can be a different depth in each horizontal direction, which is what makes the building noticeably stiffer sideways in one direction than the other — a real effect this project didn't model before. It's an idealized single-bay frame, not a scale model of real construction. |
| **Static condensation** | The math trick that turns "every floor has both a sideways position AND a rotation at each beam-column joint" (twice as many unknowns as before) back down to "one sideways position per floor" — the same shape the rest of the simulation already expects — by algebraically eliminating the rotation unknowns. Explained with a worked example in the math PDF. |
| **MDOF (multi-degree-of-freedom)** | Engineering jargon for "a model with several moving parts that can each move somewhat independently" — here, each floor is one "degree of freedom." |
| **Mode / mode shape** | A building doesn't sway randomly — it has a small number of *natural* sway patterns (like a guitar string's harmonics), and every real motion is a mix of these. Explained fully, with pictures, in the math PDF. |
| **Damping** | Friction-like energy loss that makes swaying die down over time instead of continuing forever. |
| **FFT (Fast Fourier Transform)** | A method for taking a wiggly signal (like the ground shaking) and figuring out which "pure tones" it's built from — similar to how your ear splits a chord into individual notes. Used here to solve the physics quickly, for both the building and (a second, independent time) the furniture. |
| **Spectrum** | The output of an FFT, drawn as a picture: frequency along the bottom, "how much of the signal is at that frequency" up the side. A tall spike at 1 Hz means the signal contains a lot of once-per-second wobble. |
| **Transfer function** | A building's "answer sheet" for every possible frequency of shaking: for each one, how much does the roof move? It peaks at the building's natural sway frequencies (that's resonance) and is small everywhere else. It depends only on the building, never on the earthquake. |
| **Furniture sway** | Each piece of furniture is modeled as its own tiny mass-on-a-spring riding on its floor, with the floor's own computed motion as the "ground motion" it reacts to — the exact same kind of equation as the building itself, solved the same way (FFT), just applied recursively to a lighter, independent system. Furniture mass never feeds back into the building's own physics — it's a one-way, decorative-but-honestly-computed effect. |
| **Amplify** | An internal, auto-computed scale factor that exaggerates the motion on screen so tiny, real sway (anywhere from a fraction of a millimeter to tens of centimeters, depending on the earthquake) is visible. There's no manual slider for it any more — it recomputes itself from the *actual* peak floor displacement every time you switch records, drag a Building Parameter slider, or reshape the earthquake via the Earthquake Parameters below, so the picture always stays legible whether the underlying physics moved a millimeter or a few hundred meters. |
| **Richter magnitude** | The earthquake's overall size on the (literal, historical) Richter scale, where each whole unit is a 10x jump in shaking amplitude — not "10x stronger" in some vague sense, but exactly a 10x multiplier on the ground motion's own FFT spectrum. |
| **Epicenter distance / depth** | Two synthetic "what if this quake had happened somewhere else" knobs. Combined via the Pythagorean theorem into a single **hypocentral distance** (straight-line distance from the point underground where the rupture started to the building), which is what actually drives how the shaking changes — not distance and depth separately. |
| **Geometric spreading** | The purely-geometric reason shaking gets weaker with distance even with no other physics involved: the same seismic energy is spread across an ever-larger sphere as it travels outward, so amplitude falls off as 1/R (R = hypocentral distance). |
| **Anelastic attenuation** | A *second*, separate reason distant shaking is weaker: real rock isn't a perfect elastic spring, so it quietly absorbs energy as a wave passes through — and it eats high frequencies faster than low ones, which is why distant earthquakes don't just feel weaker, they feel duller/lower-pitched. Modeled here as a genuine per-frequency-bin filter, not a single number. |

## How the pieces connect

```
data/<record>/*.AT2, *.DT2          ← a real earthquake recording (you supply these)
        │
        ▼
mdof_response.py                     ← builds the frame model (per axis), computes floor + furniture motion
        │
        ▼
out/<record>/response_X.csv          ← every floor's position, at every instant, saved to a spreadsheet-like file
out/<record>/response_Y.csv
out/<record>/building_data.json      ← a summary: floor count, frame geometry, natural sway patterns (per axis), etc.
out/<record>/furniture_response.bin  ← every furniture item's own extra sway, decimated for size
out/<record>/ground_accel.json       ← the raw ground motion, cached for live recompute
out/<record>/spectrum.json           ← the ground motion's FFT magnitude spectrum, log-binned
out/folders.json                     ← a list of which earthquake recordings have been processed
        │                        │           │
        ▼                        ▼           │
plot_response.py              index.html     │ POST /compute (live building-
  → response_plot_X.png    (fetch + three.js)│  parameter sliders, debounced)
  → spectrum_plot_X.png     animated 3D      ▼
                            open-cutaway   server.py
                            frame + sway   (rebuilds the frame model with
                            + frequency-    your slider values, reusing the
                              domain drawer  cached ground motion)
                                   ▲                │
                                   └────────────────┘
                                     binary response
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
   `MDOF_ShearBuilding` class, plus a set of standalone frame-assembly
   functions above it (`column_inertia`, `beam_inertia`,
   `assemble_frame_stiffness`, `condense_rotations`, `build_condensed_K`).
   It:
   - Builds the building's stiffness from **real column and beam
     dimensions** via the matrix-stiffness method — four corner columns
     per floor, connected by beams, fixed at the ground floor's base and
     genuinely free at the roof (nothing holds the top floor back except
     the floor below it). This replaced an earlier version that derived an
     abstract spring stiffness backward from a target sway period, with no
     real geometry behind it at all.
   - Because a column can be a different depth in each direction, the
     building is now genuinely **stiffer sideways one way than the
     other** — X and Y each get their own independently-built model
     (`axis="X"` / `axis="Y"`), instead of reusing one number for both.
   - Works out the building's small number of *natural sway patterns*
     (the "modes" from the glossary above) — this is the part of the code
     that needs the most background to understand, and it's explained
     step by step with a worked numeric example in the math PDF.
   - Combines the earthquake recording with those sway patterns to compute
     exactly how far every floor moves, at every instant in the recording
     — plus each floor's *acceleration*, which feeds the furniture.
   - Also computes each **furniture class's** (table/chair/fan) own extra
     sway relative to its floor (`compute_furniture_response`), using the
     exact same FFT technique a second time, then shrinks that data down
     (`get_decimated_furniture`) before saving it, since it doesn't need
     the ground motion's full time resolution to look right (see
     "Furniture sway" in the glossary).
   - Saves everything to files (`save_to_csv`, `save_building_data`).

3. **The bottom of the file (`if __name__ == "__main__":`)** loops over
   every earthquake recording you've placed in the `data/` folder, runs the
   steps above on each one (building both an X and a Y model), and writes
   results into `out/`.

   Settings worth knowing: `NUM_STORIES = 7` is hardcoded near the
   bottom — every recording currently gets simulated with a 7-floor
   building. `COLUMN_DEPTH_X`, `COLUMN_DEPTH_Y`, and `BEAM_DEPTH` are also
   hardcoded there as the *default* frame dimensions (the live sliders in
   `index.html` can move away from these, but `out/`'s precomputed static
   files always reflect these exact defaults). There's no menu option to
   change any of these per-recording yet; you'd edit those lines directly.

### `plot_response.py` — makes a static picture

Reads one earthquake's results from `out/` and draws a normal 2D chart:
time along the bottom, how far each floor moved up the side, one colored
line per floor plus a black line for the ground itself. Saved as a PNG
image. This is independent of the 3D browser animation — a quick way to
eyeball whether a result "looks reasonable" without opening a browser.

It also draws a second figure, `spectrum_plot_X.png` — the same three stacked
frequency-domain panels that `index.html`'s drawer shows (see below), for the
roof floor in the X direction. Having both means a screenshot from the browser
and a figure in a written report are recognisably the same argument, drawn from
the same numbers.

### `index.html` — the real, interactive 3D animation

Open this file **through a local web server** (see below — opening it
directly by double-clicking won't work) to watch the actual simulation
results. It:
- Shows a dropdown listing every earthquake recording that's been
  processed.
- Loads that recording's results and builds a matching 3D building as an
  **open cutaway** — visible columns, beams, and floor slabs, no exterior
  walls — plus a handful of procedurally-placed furniture items (tables,
  chairs, a fan) on every floor, deterministically positioned so the same
  record always looks the same. There's no lit-window texture any more
  (there's no wall left to paint it on). Furniture is rendered noticeably
  **larger than true physical scale** (a single named constant,
  `FURNITURE_SCALE`, currently 1.6x every linear dimension) so it actually
  reads as furniture at whole-building camera distance — the same
  "stylized for visibility" idea the columns/beams already used, now
  extended to furniture. This is a purely cosmetic render constant; it
  doesn't touch the physics-computed sway amplitude at all, and each
  instance also gets a small deterministic per-item material tint so
  furniture doesn't look perfectly uniform.
- Plays back the *real* computed floor positions as an animation, frame by
  frame, matching the timing of the original recording — columns visibly
  lean/shear between floors as each floor sways by a different amount, and
  furniture sways with a motion of its own, distinct from (but riding on
  top of) its floor's rigid motion.
- Gives you controls: play/pause, playback speed, and a scrubber to jump to
  any moment — plus two new camera modes (below) and a redesigned,
  mobile-friendly panel. There's no manual Amplify slider any more (see the
  glossary entry above) — it's fully automatic now, which matters because
  Earthquake Parameters (below) can legitimately push peak floor
  displacement from millimeters to hundreds of meters, a range no single
  manual slider position could stay legible across.
- Has a **Building Parameters** panel (stories, mass per floor, damping,
  column depth X, column depth Y, beam depth, plus a read-only period
  readout) — moving any of the sliders sends your values to `server.py`,
  which recomputes the *actual physics* for that building live (not a
  visual trick) and updates the animation, roughly 200-400ms after you stop
  moving the slider. While a live recompute is in flight, a full-viewport
  loading overlay (animated floor-bars assembling, plus a pulsing
  "Recomputing…" label) appears over the 3D view — the control panel stays
  fully usable throughout, this is purely a visual cue that new results are
  on the way. Requires `server.py` running, not a plain static server —
  see "Running it yourself" below. There's no Target Period slider any
  more: once stiffness comes from real column/beam dimensions, the sway
  period is an *output* of the model, not something you dial in directly —
  the read-only `T₁ (X/Y)` readout shows what it comes out to.
- Has an **Earthquake Parameters** panel — Epicenter Distance, Epicenter
  Depth, and Richter Magnitude sliders that reshape the *selected record's*
  own ground motion into a synthetic "what if this quake had happened
  closer/farther/stronger" version, rather than picking from a fixed set of
  canned earthquakes. Magnitude applies the literal historical Richter
  definition (each whole unit = exactly 10x the ground motion's own FFT
  spectrum, not an approximation); distance/depth combine into a single
  hypocentral distance that drives two physically distinct effects —
  geometric spreading (uniform 1/R amplitude falloff) and anelastic
  attenuation (a genuine per-frequency filter that eats high frequencies
  faster than low ones as the "quake" gets farther away, the same reason
  real distant earthquakes sound duller, not just quieter). These sliders
  never touch the building's own stiffness/mass/damping — only reshape the
  input ground motion — so the `T₁ (X/Y)` readout stays fixed while you
  drag them; only Building Parameters change it. A small always-visible
  **epicenter map** in the top-right corner gives a rough plan-view sense
  of where the synthetic hypocenter sits (dot position + a separate depth
  gauge) and pulses faster/larger as magnitude increases — illustrative,
  not to scale, since the model has no azimuth/direction input.
- Has a **View** section with a floor selector: pick a floor to smoothly
  reframe the camera in close on it (playback and every animation keeps
  running throughout — this only changes what the camera is looking at),
  and an always-visible "Back to full view" button to return to the
  whole-building framing. Switching earthquake records or any Building
  Parameter always resets back to the full-building view.
- Reframes the camera automatically whenever the building's height changes
  (e.g. the Stories slider), easing to the new shot instead of snapping, so
  a 20-story building is never cut off — the camera distance, its far
  clip plane, and the scene fog range are all derived from the same fit
  calculation instead of being independent fixed constants.
- Slowly auto-orbits the camera on its own after a few seconds of no manual
  input (while viewing the whole building — auto-orbit stays off while a
  single floor is selected, since the tighter per-floor camera distance
  would otherwise swing away from the floor you zoomed into), and yields
  instantly the moment you touch the controls again.
- Has a **Frequency Domain** drawer (the small chart button on the right edge)
  showing three stacked panels that share one horizontal frequency axis:
  **Input** (the earthquake's own spectrum), **Transfer** (this building's
  answer sheet — see the glossary), and **Output** (how the selected floor
  actually ended up moving, relative to the ground). Read top to bottom, the
  picture *is* the calculation: **input × transfer = output**, which is exactly
  what `mdof_response.py` computes internally. The peaks in the Transfer panel
  are the building's resonances, and wherever one lines up with energy in the
  Input panel, the Output panel shows a peak too. A floor selector and an X/Y
  toggle drive the bottom two panels. The Input panel doesn't react to
  Building Parameters — the earthquake doesn't care what you built — but it
  *does* redraw live when you drag an Earthquake Parameters slider, since
  that's reshaping the ground motion itself. Transfer/Output are recomputed
  live when you move a Building Parameter slider — stiffen the columns and you
  can watch the resonance peak slide to the right. The Output panel is measured
  from the actual computed floor motion, *not* derived by multiplying the other
  two panels together, which would make the agreement true by construction and
  therefore meaningless.
- Shows a live **Shaking** meter (how strong the *current instant* of real
  ground motion is, relative to that record's own peak) alongside a subtle
  camera shake at high intensity, and renders lit surfaces/the roof beacon
  through a bloom pass for a bit more visual punch — all purely cosmetic,
  none of it feeds back into the physics.
- Lighting is brighter across the board than earlier versions of this
  project, plus three fixed interior point lights (warm-neutral, no
  shadows) specifically so the columns/beams/furniture inside the open
  cutaway read clearly from any camera angle — the moody night exterior
  (fog, background, hemisphere colors) is unchanged, only the amount of
  light went up, not its color/character.
- The control panel is a real redesign, not just a re-layout: grouped
  collapsible sections (Playback / Building Parameters / View), a
  deliberate system-font typography scale (headers/labels/values sized and
  weighted differently, numeric readouts in a monospace-flavored stack with
  tabular figures), and a genuine mobile layout — below ~600px width the
  panel becomes a bottom sheet with a collapsed strip (record name +
  play/pause) you can tap to expand, larger touch targets throughout, and
  no hover-only controls. One-finger drag / two-finger pinch-pan orbiting
  and zooming still come from `OrbitControls`' own built-in touch handling.

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
(stories, mass/floor, damping, column depth X/Y, beam depth) from
`index.html`'s sliders, rebuilds two `MDOF_ShearBuilding` models from
`mdof_response.py` (one per axis, since X and Y are no longer identical)
with those values, computes each axis's furniture response too, and
returns freshly computed floor displacements plus furniture sway. It
reuses each record's cached `ground_accel.json` rather than re-reading the
original earthquake file, and returns the result as compact binary data
(not JSON) — sending the full time series as JSON text turned out to be
slow enough to matter, since it's transferred on every slider move.

### `style.css`

A handful of small visual tweaks (dropdown menu colors) for `index.html`.
Everything else is styled inline inside the HTML files themselves.

### `out/` — the computed results (already included in this repo)

One folder per earthquake recording, containing:
- `building_data.json` — a summary of the virtual building used: floor
  count, the frame geometry (column/beam dimensions, plan span, material),
  and **per-axis** natural sway speeds/sway-pattern shapes/fundamental
  period (X and Y are independently condensed now, so these are no longer
  shared numbers), plus a `furniture` block describing
  `furniture_response.bin`.
- `response_X.csv` / `response_Y.csv` — the actual result: a spreadsheet
  where each row is one instant in time, and each column is one floor's
  position at that instant (X = one horizontal direction, Y = the other).
- `response_plot_X.png` — the static picture from `plot_response.py`.
- `spectrum_plot_X.png` — the static version of the frequency-domain panels.
- `furniture_response.bin` — every furniture class's (table/chair/fan) own
  extra sway relative to its floor, for both axes, as raw binary
  (float32). Resampled to a lower rate than the ground motion before
  saving (see "Furniture sway" in the glossary) — this keeps the addition
  to `out/`'s size modest (a few MB per record) instead of doubling it.
- `ground_accel.json` — the raw ground acceleration and displacement used as
  input, cached so `server.py`'s live parameter sliders can recompute a
  building's response without re-reading the original earthquake file.
- `spectrum.json` (~10 KB) — the ground motion's magnitude spectrum, averaged
  into 400 logarithmically-spaced frequency bins. This is what feeds the
  drawer's Input panel. It's precomputed for two reasons: the ground motion
  doesn't change when you move a Building Parameter slider (so recomputing it
  per drag would be pure waste), and the browser only ever receives ground
  *displacement*, never ground acceleration, so it couldn't derive this one
  itself even if it wanted to. Unlike the response files, it's read only by
  the frontend — `server.py` never touches it.

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
separately-deployed backend instead: see
[seismic-sim-backend](https://github.com/Raufur1234/seismic-sim-backend)
(a trimmed mirror of `server.py`, deployed on Render at
https://seismic-sim-backend.onrender.com). That repo's own README
explains why it exists and what it contains.

## Current state (as of this pull)

- The pipeline runs end-to-end for the 10 recordings already included in
  `out/`, using a real structural frame — four corner columns per floor
  connected by beams, fixed at the ground floor's base, genuinely free at
  the roof — instead of the abstract "floors on springs" model this
  project started with. Stiffness comes from actual column/beam
  dimensions via the matrix-stiffness method with static condensation, not
  a target-period guess.
- X and Y sway are independently modeled now (each axis gets its own
  condensed stiffness matrix), so a building with unequal column depths in
  each direction is genuinely stiffer one way than the other — this
  wasn't possible with the old single isotropic spring constant.
- Every floor also has a handful of furniture items (tables, chairs, a
  fan) with their own small extra sway relative to their floor, computed
  with the same frequency-domain technique as the building itself.
- Only the X-direction static plots are generated by default.
- The frequency-domain drawer (spec 6) makes the FFT machinery visible instead
  of merely internal: input spectrum, transfer function and output spectrum on
  one shared axis, live under the parameter sliders. The `input × transfer =
  output` identity behind it is checked numerically, not just asserted — it
  holds to ~3e-15 on the zero-padded grid the solver actually works on — and
  the browser's hand-written FFT is checked against `scipy.fft` to 3.4e-14.
- `out/`'s precomputed results use a fixed 7-floor building with fixed
  default column/beam dimensions (see `NUM_STORIES`/`COLUMN_DEPTH_X/Y`/
  `BEAM_DEPTH` above) — but stories, mass, damping, and the three frame
  dimensions are all now adjustable *live* via `index.html`'s Building
  Parameters sliders, backed by `server.py`. The old "Target Period"
  slider is gone — period is now an *output* of the frame dimensions, not
  something you dial in directly (a read-only readout shows it instead).
  Per-floor stiffness variation (e.g. a "soft story") isn't supported yet.
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
- A visual/UI redesign pass (spec 4) followed direct feedback that
  furniture was too small to read, the lighting felt too dark since the
  open-cutaway rewrite, the animation felt static, and the control panel
  needed a real redesign rather than another patch: furniture is now
  rendered ~1.6x larger (a purely cosmetic constant, doesn't touch sway
  physics) with per-instance material tint variety; every light's
  intensity went up and three new interior point lights specifically
  light the now-visible columns/beams/furniture, while the moody exterior
  night mood is unchanged; the camera idle-auto-orbits and can also
  reframe in close on any single selected floor (with an explicit "back to
  full view" control); building materials got richer PBR parameters and
  per-column tint jitter; and the control panel was rebuilt from scratch
  (grouped collapsible sections, deliberate typography, a real mobile
  bottom-sheet layout) with a full loading-overlay replacing the old tiny
  "recomputing…" text line during live parameter recomputes. A follow-up
  fix found during that pass's own review: the scene's fog range used to
  only be recalculated at framing events (record switch, per-floor select,
  a parameter change), so manually zooming out further than the last-framed
  distance ran the building straight into a now-too-close, frozen fog wall
  and faded it to black — especially noticeable on tall buildings. Fog
  near/far are now recomputed every animation frame from the camera's
  actual current distance, not a value cached from the last framing event.
- An Earthquake Parameters panel (spec 7) lets you reshape any selected
  record's own ground motion into a synthetic "what if" version via
  Epicenter Distance/Depth and Richter Magnitude sliders, instead of only
  ever replaying the 10 recordings exactly as recorded. This replaced the
  old manual Amplify slider entirely — with earthquake magnitude now
  user-adjustable, peak floor displacement can legitimately span
  millimeters to hundreds of meters, a range no fixed manual amplify
  position could stay legible across, so amplify is now purely automatic.
  A small always-visible epicenter map gives a rough plan-view sense of
  where the synthetic hypocenter sits. One bug was found and fixed during
  this work: at a hypocenter *closer* than the reference recording's own
  geometry, the anelastic-attenuation term used to flip from removing
  high-frequency energy to amplifying it without bound (the recorded
  trace's noise floor blew up to kilometers of "displacement") — fixed by
  clamping the effect so a closer-than-reference hypocenter means no
  attenuation adjustment, never amplification, matching what's physically
  possible (distance can only remove energy a real recording still has,
  never add energy that was never there).

Keep this file — and the math PDF — updated as the project evolves. That's
the whole point of having them.
