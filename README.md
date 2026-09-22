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
| **Amplify** | An internal, auto-computed scale factor that exaggerates the motion on screen so tiny, real sway (anywhere from a fraction of a millimeter to tens of centimeters, depending on the earthquake) is visible. There's no manual slider for it any more. It's recomputed from the *actual* peak floor displacement each time you switch records, and then deliberately held fixed while you drag the Building or Earthquake Parameter sliders — re-deriving it on every drag would normalise away the very difference those sliders exist to show, making a magnitude-9 quake look pixel-for-pixel identical to a magnitude-3 one. What keeps the picture on screen instead is a separate render-time compression: because the Richter scale is logarithmic, growth beyond the record's own baseline is compressed (10x in real ground motion reads as roughly 2.2x on screen) and hard-capped, so bigger always looks bigger without the building ever leaving the frame. Furniture is scaled by its own equivalent factor, sharing the same compression. |
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
                            + Signals       your slider values, reusing the
                              drawer (Time  cached ground motion, and sending
                              & Frequency)  back the scaled ground
                                   ▲         acceleration too)
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
   - Knocks that stiffness down to account for **cracked concrete**.
     Reinforced concrete develops hairline cracks long before it comes
     anywhere near breaking, and a cracked beam bends more easily than an
     intact one, so using the raw geometry overstates how stiff a real
     building is. Columns are multiplied by 0.35 and beams by 0.50
     (`SECTION_STIFFNESS_PRESETS`). Those two numbers are **this
     project's chosen defaults, not values copied out of a building
     code** — the code tables say something different, and the comment in
     the source says so rather than pretending otherwise. Other presets
     (including `"gross"`, meaning no knock-down at all) are one
     parameter away.
   - Accounts for **gravity making the building easier to push over**.
     When a building leans, its own weight is no longer pulling straight
     down through the columns — it pulls slightly sideways too, adding to
     whatever the earthquake is already doing. Engineers call this the
     P-Δ effect. It is subtracted from the stiffness as a second matrix
     (`geometric_stiffness_matrix`), and it is strongest at the **bottom**
     of the building, because the ground floor's columns carry the weight
     of every floor above them. That is exactly why real buildings tend to
     fail at the bottom.
   - **Refuses to pretend** when the sliders describe a building that
     cannot stand up. Push the mass high enough, or the columns thin
     enough, and gravity wins outright: there is no sway period, because
     the building simply falls over. The code detects that and raises a
     `GravityInstabilityError` naming which storey gave way, instead of
     quietly producing "not a number" and animating nonsense.
   - Lets **each floor differ** — storey heights, column depths and beam
     depths are per-floor lists internally, not single numbers. That is
     what makes the "soft ground story" option possible: a taller, and
     therefore floppier, ground floor of the kind used for parking
     underneath an apartment block. Nothing about it is special-cased;
     the weakness falls out of the arithmetic, because stiffness drops
     with the cube of a column's height.
   - Works out each storey's **strength** as well as its stiffness — how
     much bending a column can take before it gives (`M_p`), how much
     sideways force a storey can resist (`V_p`), how much weight it can
     carry straight down (`P_cap`), and how far it can lean before it
     starts yielding. None of this changes the animation; it exists so
     later work has real numbers to compare against instead of invented
     ones.
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

   Two environment variables do exist, both for regression-checking rather
   than everyday use: `SEISMIC_SIM_SECTION_MODE=gross` turns the
   cracked-concrete knock-down off, and `SEISMIC_SIM_P_DELTA=0` turns the
   gravity effect off. Together they reproduce the pre-cracked-sections
   model exactly, which is how the change was verified as
   behaviour-preserving before the new defaults were judged.

   **Why `out/` changed when cracked sections and P-Δ landed.** Both
   effects make the building less stiff, and a less stiff building sways
   more slowly, so every stored result moved: the 7-floor default's sway
   period went from **1.06 s to 1.62 s** in X and **0.95 s to 1.45 s** in
   Y. The stored *ground motion* files (`ground_accel.json`,
   `spectrum.json`) did **not** change, and could not have — the
   earthquake recording does not care what building you put in front of
   it.

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
  column depth X, column depth Y, beam depth, floor area, a
  soft-ground-story checkbox, plus a read-only period readout) — moving
  any of the controls sends your values
  to `server.py`,
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
  the read-only `T₁ (X/Y)` readout shows what it comes out to. The
  **Area (sq ft)** slider (200-2000, default ~543 sq ft — the building's
  original fixed footprint) picks a target floor-plate area and derives
  both plan dimensions from it, holding the footprint's aspect ratio
  (1.4) fixed — a bigger floor plate means a longer beam span, which
  makes the frame genuinely more flexible (a real physics effect, via the
  same static-condensation math every other Building Parameters slider
  already drives), not just a bigger-looking building. The 3D view
  reflects it directly: the floor plates and beams grow and shrink with
  the slider while the columns keep their own thickness (that's set by
  the Column X/Y sliders — changing how much floor you have doesn't
  change the size of the columns holding it up), and the furniture
  spreads out across the larger plate.
- The **Soft ground story** checkbox makes the ground floor 1.6× taller
  than the rest, leaving every column and beam exactly as it was. This is
  the apartment-block-over-open-parking layout that fails so
  characteristically in real earthquakes. Nothing about it is
  special-cased in the physics: a column's sideways stiffness falls with
  the *cube* of its height, so simply making that one storey taller makes
  it much floppier than the ones above, and the sway then concentrates
  there. The server decides what "soft ground story" means, not the
  browser, so there is exactly one definition of it.
- If you push the sliders far enough — very heavy floors, very thin
  columns, very tall storeys — you reach a building that **cannot stand
  up under its own weight**, and the viewer says so in as many words,
  naming the storey that gives way first, and leaves the last working
  building on screen. That is a real result rather than an error: past
  that point gravity alone overcomes the frame's sideways stiffness, and
  there is no sway period to compute because the building just falls
  over.
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
- Has a **Signals** drawer (the small chart button on the right edge) with two
  tabs, **Time** and **Frequency**, sharing one Floor selector and one X/Y
  toggle so both tabs always describe the same thing.

  The **Time** tab shows the signal that is actually driving the animation, as
  it plays. Two stacked traces on one shared time axis: the **ground
  acceleration** on top (the real physical input — ground *displacement* is a
  slow drift that doesn't look like an earthquake at all) and the selected
  floor's **relative** displacement below. A pen sweeps left to right in
  lockstep with the building on screen, leaving the played part in full colour
  and the rest as a faint ghost, so you can see where in the record you are
  without the axes ever rescaling under you. The payoff is the contrast between
  the two: a broadband input that arrives and stops, over a narrowband response
  that keeps ringing well after it does — that ringing *is* the building's
  resonance, the same thing the Frequency tab draws as a peak. Each panel prints
  its own peak value, which is not decoration: Richter magnitude scales the
  ground motion by the same factor at every frequency, so on a self-normalising
  axis the *shape* wouldn't move at all and the slider would look broken.

  The **Frequency** tab is three stacked panels that share one horizontal
  frequency axis:
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
- The elastic model itself got three corrections (spec 10) before any of
  the collapse work that depends on it. **Cracked concrete** is now
  accounted for (columns ×0.35, beams ×0.50 — this project's defaults,
  labelled as such rather than attributed to a code table they do not
  match). **Gravity** now works against the frame through the P-Δ effect,
  strongest at the base, where a real building's weight actually
  accumulates. And every floor can now **differ** from every other, which
  is what makes the soft-ground-story option possible. Together the first
  two lengthened the default building's sway period from 1.06 s to 1.62 s
  in X — which is why `out/` changed. The refactor was checked as
  behaviour-preserving *first*: with both effects switched off it
  reproduces the previous model bit-for-bit across 216 parameter
  combinations and a full record, and regenerating `out/` in that mode
  changes no numerical file at all.
- Pushed far enough, the sliders now describe a building that cannot
  stand under its own weight, and the code says so — naming the storey
  that fails — rather than silently producing "not a number" and
  animating it.

- Only the X-direction static plots are generated by default.
- The Signals drawer's Frequency tab (spec 6) makes the FFT machinery visible
  instead of merely internal: input spectrum, transfer function and output
  spectrum on one shared axis, live under the parameter sliders. The
  `input × transfer = output` identity behind it is checked numerically, not
  just asserted — it holds to ~3e-15 on the zero-padded grid the solver
  actually works on — and the browser's hand-written FFT is checked against
  `scipy.fft` to 3.4e-14.
- The Time tab (spec 9) is the same record seen the other way round, and it is
  deliberately *measured*, not reconstructed: the ground trace is the exact
  acceleration array the solver ran on (`/compute` now sends it back alongside
  the response rather than the browser re-deriving it from displacement), and
  the floor trace is floor-minus-ground, not the absolute floor position. Both
  are reduced once per load to a per-pixel-column min/max envelope, which is an
  exact record of what a plot that wide can show, so the per-frame redraw costs
  the same whether the record is 20 seconds or 200.
- `out/`'s precomputed results use a fixed 7-floor building with fixed
  default column/beam dimensions (see `NUM_STORIES`/`COLUMN_DEPTH_X/Y`/
  `BEAM_DEPTH` above) — but stories, mass, damping, and the three frame
  dimensions are all now adjustable *live* via `index.html`'s Building
  Parameters sliders, backed by `server.py`. The old "Target Period"
  slider is gone — period is now an *output* of the frame dimensions, not
  something you dial in directly (a read-only readout shows it instead).
  Per-floor variation **is** supported now (spec 10): storey heights,
  column depths and beam depths are per-floor internally, and the
  **Soft ground story** checkbox is the one-click version of it.
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
- Three further bugs from that panel, all reported as "the building
  sometimes drifts off-centre and the furniture shakes oddly", all fixed:
  1. **Switching records applied the previous record's magnitude.** The
     check that decides whether the precomputed `out/` files still match
     the panel compared the magnitude slider — still holding the record
     you were leaving — against that same outgoing record's magnitude. So
     moving from a 4.9 M record to a 7.5 M one quietly recomputed the new
     record *as* a 4.9 M quake (~400x too weak) while the panel went on to
     display "7.5 M"; going the other way overdrove it by the same factor
     and threw the building off-centre. Record switches now reset the
     earthquake parameters before choosing a load path, and don't send a
     magnitude at all until the incoming record's own is known.
  2. **Nudging any earthquake slider swapped the ground-displacement
     source.** Ground displacement came from the recorded PEER `.DT2`
     trace at the default settings but was re-derived by double-
     integrating acceleration at any other setting — and those two differ
     by about 2.3x in peak, because PEER's own baseline correction isn't
     reproducible from a generic high-pass filter. One click of a slider
     therefore jumped the whole building to a differently-shaped waveform.
     The synthetic-earthquake filter is now applied to the recorded
     displacement directly, which is exactly equivalent (displacement and
     acceleration spectra differ only by a factor of −1/ω², so the same
     real, frequency-dependent scale factor applies unchanged to both) and
     continuous everywhere.
  3. **The on-screen sway grew linearly with a logarithmic scale.** One
     step of the Richter slider is a literal 10x in ground amplitude, and
     that went straight to the screen, so +1 M already threw the building
     several building-widths sideways. On-screen sway is now compressed
     above the record's own baseline (10x physical reads as about 2.2x on
     screen) and hard-capped, so a bigger quake always visibly sways more
     without ever leaving the frame. Furniture shares the same compression
     and now gets its own auto-scale derived from the furniture response
     itself, rather than borrowing the building's displacement-based one —
     the two differ by orders of magnitude and by a record-dependent
     amount, which is why furniture used to look either frozen or pinned
     depending on which record was loaded. The physics is untouched: only
     the render gain changed, and the relative sway between a stiff table,
     a chair, and a low-frequency ceiling fan is preserved exactly.
- Two more bugs, found after the three above: **the frequency-domain
  drawer showed no visible change when the Magnitude slider moved.** The
  Richter magnitude and geometric-spreading terms are both flat multipliers
  across every frequency — a real amplitude change, but the drawer's dB
  axis auto-scales to each panel's own peak with no level ever printed, so
  a flat gain shifts the axis by exactly the same amount as the curve and
  renders pixel-identical. Fixed by printing each panel's actual peak
  level (e.g. "Input · 4.0 dB") into its label — confirmed a +1.49 M bump
  now reads as the expected +29.8 dB. And **furniture could still pin at
  its render clamp** the same way the building used to (bug 3 above) —
  a follow-up code-review pass found the per-sample clamp on furniture's
  own render code was never converted to a gain cap when the building's
  was. Fixed the same way: cap the gain once per frame, not each sample.

Keep this file — and the math PDF — updated as the project evolves. That's
the whole point of having them.

## Spec 11: nonlinear FFT response

The optional nonlinear analysis keeps the modal FFT solver and feeds a
history-dependent pseudo-force back into it. Four parallel column springs
per story have a trilinear backbone, peak-oriented hysteresis, pinching,
degraded unloading stiffness and deterministic strength scatter. The
condensed frame's remaining flexural coupling stays elastic. Material
variability and the hysteresis constants are assumptions, not calibrated
component data; progressive load-path changes are not included, and torsion
is opt-in (see Spec 12 below).

Open **Collapse Analysis** and press **Run collapse analysis**. This sends
one `/compute` request with `nonlinear: true`. Live sliders still run the
elastic model; changing a parameter or record invalidates the nonlinear
cache. The result adds a `collapse` JSON header, including convergence,
per-story criterion times, per-column ultimate-drift times and residual
drifts. It adds no binary payload blocks or new damage animation. A failed
iteration reports numerical failure and preserves the previous valid view.
It never substitutes divergence for a collapse event.

The Python entry point is `compute_response_nonlinear(accel, disp, dt,
**params)`; `compute_response(..., nonlinear=False)` retains the existing
elastic arithmetic. The offline pipeline remains elastic. `/compute`
accepts `intensity_scale`, backbone and pinching parameters,
`column_strength_cov`, `hftd_relaxation`, `hftd_tolerance`,
`hftd_max_iterations`, optional `hftd_segment_seconds`, `drift_limit_cp`
and `collapse_mu_cap`. Inconsistent backbones return HTTP 400.

The default softening slope magnitude is 0.30: 0.10 cannot continuously
reach the assumed five-percent residual strength by ductility eight.
Zero softening selects the bilinear model. Default force relaxation is
0.4, tolerance 1e-4 and outer limit 40. A causal FFT block predictor and
Anderson mixing aid convergence; predictor iterations are reported
separately. The final whole-record or overlap-save force residual must
still pass, including a separate check for periodic tail contamination.
Holding the pseudo-force tail preserves permanent drift. No filter is
applied to nonlinear floor displacement.

The public solver first evaluates native-grid elastic demand. If no column
can yield, it keeps the exact native path; otherwise it band-limits and
interpolates the input to four times the record sampling rate, performs the
same FFT pseudo-force solve there, and returns native-time samples. This
adaptive sampling resolves sharp hysteresis reversals without adding a
time-stepping production solver or changing the binary wire format.

Drift, ductility and live tangent/gravity thresholds identify modelled
collapse onset only after convergence. The four-percent drift level is
descriptive performance guidance from FEMA 356, not a calibrated collapse
prediction. Everything up to and including collapse onset is simulated;
how it falls is animated (in the later visual extension). See Part G of
**Seismic-Sim Math.pdf** for the derivation and limits.

## Spec 12: torsion

Each floor can now also **rotate** about the vertical axis. The 3N model has
degrees of freedom `[u_x,1..N, u_y,1..N, θ_1..N]` and is assembled as
`K_3N = Σ_f T_fᵀ K_f T_f` from the four condensed planar frames placed at
their plan positions (not from scalar per-column formulas), with rotational
inertia `m(a²+b²)/12`. Its gravity term uses the *corner-column* radius
`(a²+b²)/4`, not `/12`; the two look like a typo of each other and are not
(Part H of **Seismic-Sim Math.pdf** explains why). The system is still
real-symmetric and classically dampable, so the same modal FFT kernel solves
it, now with both PEER components applied at once.
`MDOF_Building3N` in `mdof_response.py` is the class; per-column drift
(`column_drift_3N`) replaces the shared story drift, which also corrects
spec 11's "every column shares one drift" statement.

**What it does and does not show.** Twist is *emergent*: nothing puts an
eccentricity in by hand. With the elastic stiffness the plan is symmetric, so
elastic `θ` is exactly `0.0` for every building this model can describe.
Twist appears only after the seeded strength scatter makes one column yield
before its neighbours moves the centre of rigidity. No biaxial interaction
surface is modelled (unconservative for a column driven hard on both axes).

**Request and payload.** `/compute` accepts a strict-boolean `torsion`,
default `true` for elastic and `false` for nonlinear (the coupled N=20
nonlinear solve takes ~203 s, above the ~133 s the two-axis path already
costs). If the cached ground record cannot be paired (missing Y component,
unassigned orientation, or unequal `dt`) it falls back to the per-axis path
and reports `torsion_fallback_reason`. `torsion=false` is byte-identical to
spec 11. When on, the header gains torsion keys (`torsion_enabled`,
`has_floor_rotation`, `plan_a/_b`, `eccentricity_x/_y`,
`omega_theta_over_omega_x`, 3N modal keys) and a `theta_z` float32 block
`(N, npts)` is appended after `gaccel_y`. The eccentricity envelope is `null`
where the centre of rigidity is undefined. `intensity_scale` is clamped to 20.

**Viewer.** Floors yaw by `-θ·scale/u` (the same display gain as sway) about
their own centre, with column ends rotated about the corner offset and columns
twisted to match. The **Torsion in collapse** checkbox (default off) and the **Run collapse
analysis** button live in their own collapsible **Collapse Analysis** panel
section and send `torsion` with nonlinear requests. The Transfer panel
reads the right DOF block of the 3N modes.

**Open items.** The offline pipeline stays per-axis and `out/` was not
regenerated (it would only add an all-zero `θ` block and ~1e-16 churn). The
`t_collapse` agreement criterion against the Newmark reference is vacuous
(neither side collapses in the checked cases). Whether elastic torsion should
default on given `θ ≡ 0` there is an open question.

**Messages and toasts.** Any red message on the recompute overlay (unstable
building, invalid parameters, collapse not converged, unsupported backend) can
be closed with its × button or Esc. Closing an *unstable building* or *invalid
parameters* message also snaps the building sliders back to their last
accepted values, so the panel matches the building still on screen. A new
collapse result being cached shows a brief green "Successfully Cached" toast
at the top right; the run button's label never changes.

## Spec 13: what happens after a story breaks

Spec 11 finds collapse *onset*. Spec 13 carries the computation one step
further. When a story actually **detaches**, the building above it stops
being part of the structure. The part below keeps shaking, and it is solved
again as a shorter building from that instant on.

> **Everything up to and including collapse onset — which story, at what
> instant, in which direction, with how much residual drift, and which
> individual columns failed — is *simulated* from the recorded ground motion.
> Everything after detachment is *animated* using a post-failure rigid-body
> model with assumed contact parameters, not a solution of the structural
> equations of motion.**

**Detachment is stronger than onset.** A story detaches only when **all four**
of its columns have failed (drift past their ultimate `du`) **and** gravity has
overcome what stiffness is left (summed tangent `≤ P/h`, spec 11's gravity
test). With P-Delta off that second test becomes "tangent `≤ 0`", which is
stricter, not equivalent. Onset is a performance statement; detachment is a
kinematic one, and only detachment ends the structural solve for the floors
above.

**The survivor is rebuilt, not cut out.** The floors below keep their mass
(the detached floors are *removed* from `M`, never zeroed), and their frame
is condensed again from geometry. Slicing the old stiffness matrix would keep
restraint from columns that no longer exist; at the default 7-story geometry
the slice is 20–62 % wrong. Gravity loads drop because less weight is above,
so the stump is more stable. Surviving columns keep their exact damage state
and their original backbone. A reset would silently undamage the building.
The strength gained from the lower axial load is deliberately not credited
(conservative).

**How the restart works (Signals and Systems).** The survivor's response is
split into the classic LTI parts:

- the **zero-input** response: the closed-form free vibration of each survivor
  mode from the state (`u`, `u̇`) handed over at the detachment instant;
- the **zero-state** response: the modal FFT solve of the forcing.

The forced part is computed over the whole record, with the surviving columns'
real pre-detachment pseudo-force as a fixed prefix. That way the input is
never cut off at the detachment instant. A from-rest FFT solve started there
would count the first sample only half (Gibbs ringing) and break velocity
continuity. The free-vibration term then matches the handed-over state
exactly, and continuity at the restart measures 0.0.

**Both axes restart together.** A detachment belongs to the story, not to one
axis. The earliest detachment across X and Y restarts both. Cascades repeat
this on the new survivor, up to `MAX_DETACHMENT_EVENTS = 4` (a budget, not
physics). When the cap binds, `cap_reached` says so. A story-1 detachment
leaves nothing standing and ends the solve cleanly.

**What the payload carries.**
- **Hold, never NaN.** From `t_detach` on, floors above the failure plane
  *hold* their last absolute position (and floor rotation) in `abs_x`,
  `abs_y` and `theta_z`. They carry zero velocity and acceleration. NaN would
  poison the renderer and every Signals envelope. A held value looks exactly
  like a real one, so the event timestamps are the authoritative "no longer
  structural" signal. On the Time tab, a held floor's relative trace is
  therefore the negative of the ground motion.
- **The hand-off contract.** `collapse.handoff_version` (= 1),
  `detachment_events`, `cap_reached` and `surviving_stories` are added to the
  collapse header. There is no new binary block. Each event gives:
  - `story`: **1-based**. The existing `collapse_events[*].story` stays
    0-based.
  - `t_detach`, the tripped `axis`, `axes_tripped`, `direction` and
    `criterion`.
  - Every failed column with `t_fail_x/_y` and plan position.
  - `surviving_columns`: always empty, because detachment means every column
    failed. `hinge_column` instead names the column that held longest.
  - Per-floor `floor_state` in **metres, absolute frame**, with
    `ground_state` beside it.
  - The detached block's mass, CM height (from the failure plane) and
    inertia, plus the drop height, `P_cap_below` and `residual_drift_below`.
  - Consumers must refuse an unknown version; `validate_handoff()` is the
    reference.

A run that never detaches is byte-identical to before, apart from those four
header keys.

**Limits you should know.**
- A detaching run costs about 1.3–2.2× a normal collapse run. The deployed
  backend keeps collapse analysis disabled, so run it locally.
- At the time this section was written, the panel's own sliders could not
  reach a converged detachment — no intensity or weak-story control existed
  yet, and nothing above a `story_drift`/`stiffness_ratio` freeze was drawn.
  **Spec 14 (below) closes that gap**: intensity, weak-story and weak-column
  controls, a one-click demo preset, a readout, timeline markers and real
  damage visuals. Spec 15 still owns the animated fall itself.

## Spec 14: scenario controls, a readout, and honest damage visuals

Spec 13 made the physics of detachment real, but the UI still couldn't
*reach* one: the sliders had no intensity, weak-story or weak-column
control, and nothing above `story_drift`/`stiffness_ratio` numbers was drawn.
Spec 14 closes both gaps — a scenario the user can actually trigger, and a
building that visibly bends, cracks, and shows where it failed.

**Part F — scenario controls, demo preset, readout.** The Collapse Analysis
panel gained:
- **Intensity** — a stepped multiplier (0.05× to 20×, always including 1×
  and 20×) applied to the ground motion for the collapse request only. It is
  *not* the live-recompute Magnitude slider elsewhere in the UI; intensity
  scales the input record, Magnitude re-derives it from source-distance
  physics.
- **Weak story** and **Weak columns (m)** — pick one story and shrink its
  column depth in both directions, independent of the regular Building
  Parameters sliders, so a single story can be made deliberately softer
  than the rest without touching the whole building.
- **Load collapse demo** — one click sets a known-detaching configuration
  (record, story count, weak story, weak columns, intensity) so a first-time
  reader doesn't have to hunt for parameters that actually converge to a
  detachment.
- **The readout** — plain text naming, per axis, the first onset story and
  time, then (if it happened) the detached story, its time and axis, framed
  by the same honesty sentence spec 13 introduced: *"Structural solve; the
  fall is not animated yet."*
- **Timeline markers** on the seek bar — amber for onset, red for
  detachment, each with a tooltip naming the story and criterion, so
  scrubbing the record lines up with the readout's numbers instead of
  requiring a second lookup.
- **Elastic ghost** — a faint outline of how the same building would have
  swayed with no yielding, replayed alongside the real (possibly detaching)
  building, so degradation is visible by contrast rather than by memory. It
  rebuilds only when the underlying elastic run actually changes (load,
  floor, axis, resize) — never every frame.
- Every one of these controls changes the collapse result's cache key, so
  switching Weak story or Intensity invalidates a stale cached run instead
  of silently reusing it.

**New optional payload blocks (`damage_blocks`, nonlinear requests only).**
`story_drift`, `story_shear` and `p_nl` arrive at the *full* sample rate —
undecimated, because a filtered peak would round off the exact instant a
column fails and make the hysteresis loop wrong in the one place it
matters. `stiffness_ratio`, `damage_state` and `column_damage` are
nearest-neighbour subsampled (never FIR-filtered: a categorical damage code
has no meaningful "in-between" value). Damage codes are the six
`ColumnHysteresis` branches; the *displayed* `damage_state`/`column_damage`
are a running max over time per column, so a column that briefly shows a
higher stiffness ratio on UNLOAD/RELOAD (real, not a bug) never appears to
"heal" on screen. Requesting no blocks reproduces spec 13's payload
byte-for-byte; the new blocks always append after `theta_z`
(`has_floor_rotation`), never before it.

**The building now visibly bends and shows where it broke.** Every column
in every story is one `InstancedMesh` (`COLUMN_SEGMENTS = 8` bending
segments each) instead of a separate mesh per column — so this doesn't cost
extra draw calls as story count grows; verified live at N=20 with every
damage visual on: **≈1454 draw calls/frame, about the same as the pre-spec
straight-column baseline (≈1542) at the same N**. Each segment's joint sits
on a cubic (`3ξ²−2ξ³`, derived in the math PDF's Part J) fixed-fixed
deflected shape between the column's two floor endpoints, so the S-curve
gets sharpest at the ends, matching a real fixed-fixed frame column rather
than a straight sliding tray.

- **Drift colouring** — every column continuously tinted along a
  cyan→green→amber→red ramp keyed to its story's drift ratio, with IO/LS/CP
  marks (1%/2%/4%, FEMA 356 descriptive guidance, not a calibrated collapse
  prediction) on the legend.
- **Damage shade** — darker once a column has yielded, darkest once it has
  failed (computed, not painted from drift alone).
- **Hinge glow** — a marker at a column's end lights up at the exact
  instant that end's material state first goes non-elastic, never earlier.
- **Crack bands** — accumulate and never un-accumulate on a forward play,
  sized by the column's own computed stiffness loss `1 − k_t/k₀`.
- Per-story, per-column tint also varies slightly within one story — the
  legend says plainly this represents *assumed* 10% material variability,
  not something measured.

**All four of the above are driven by already-computed numbers
(drift, stiffness ratio, damage code) but are themselves cosmetic layers —
none of them feed back into the structural solve.** The bending shape is
the same kind of thing: a kinematic interpolation between two real,
solved endpoints, not a new stiffness computation (see the math PDF's Part
J2 for the distinction spelled out).

**A fourth drawer tab, Hysteresis**, plots one story's force–drift loop as
it plays: an exact, undecorated polyline through the transmitted samples —
never smoothed, never re-fit into an envelope, because a degrading loop can
self-intersect and the reversal points are the physically meaningful part.
It shows the running peaks (`V_max`, `δ_max`) and the work `∫V dδ` done so
far (explicitly labelled as work, not "dissipated energy" — separating out
the truly dissipated part would need the unloading stiffness, which the
panel does not infer), overlaid on the story's real backbone from
`build_backbones()`.

**Honesty boundary, reaffirmed.** Every visual above only ever reflects a
value the solver already computed at or before the frame being shown — check
5 in the verification doc exists specifically to catch a hinge or crack
rendered even one frame early. The fall itself is still spec 15's job.
