# Progress log — spec 7 (synthetic earthquake)

## Task 1: Core physics (`apply_synthetic_earthquake_scaling`)

Added to `mdof_response.py`: `apply_synthetic_earthquake_scaling()` plus
`DEFAULT_EPICENTER_DISTANCE_KM`/`DEFAULT_EPICENTER_DEPTH_KM`/
`ATTENUATION_Q`/`ATTENUATION_VELOCITY_MPS`. Wrote
`claude_scripts/verify_synthetic_earthquake.py` (checks 1-3 runnable
immediately, check 4 deferred to Task 3's server.py wiring).

Verification: checks 1-3 run directly, all PASS (identity at defaults
across all 10 records; exact 10x ratio at M0+1; R=R0 ratio=1, R=5*R0
low-f ratio 0.1877 > high-f ratio 0.0005).

Ruling: `claude_scripts/` is fully gitignored in this project, so the
verify script is never committed -- only `mdof_response.py` went into
commit `e732231`.

## Task 2: Bake `reference_magnitude` into `out/`

`save_building_data()` gained a `reference_magnitude` param + JSON field.
`__main__` loads `data/richter readings.json` into `RICHTER_READINGS`,
looks up per-folder, threads into both `save_building_data()` and
`ground_accel_data`. Regenerated all 10 records.

Verification: `git diff --stat` matched the expected shape exactly (one
added line per `building_data.json`, single-line-JSON rewrite per
`ground_accel.json`); spot-checked ANZA1_CIDLA by hand -- only the new
field changed, value 4.92 matches richter readings.json. Committed as
`d83f476`.

## Task 3: `server.py` wiring

Extended `_validate_params` with `epicenter_distance_km`/
`epicenter_depth_km`/`richter_magnitude` (already present from an earlier
pass); `/compute` now calls `apply_synthetic_earthquake_scaling()` on both
axes' ground acceleration before `compute_response()`, and echoes
`reference_magnitude`/`richter_magnitude`/`epicenter_distance_km`/
`epicenter_depth_km` in the response header.

Bug found and fixed during this task: unconditionally re-integrating
displacement from the (possibly reshaped) acceleration broke `/compute`
parity with `out/` even at default Earthquake Parameters, because
`ground_accel.json`'s cached `X_disp`/`Y_disp` is sometimes the actually
recorded DT2 displacement (`parse_peer_displacement_file`), not
acceleration's double integral -- the two differ by ~7.6e-5 m at their
peak, which propagated past check 4's floor-response tolerance. Fixed by
gating: displacement is only re-derived via `_integrate_accel()` when the
requested Earthquake Parameters actually deviate from identity
(magnitude != reference, or distance/depth != defaults); at defaults the
cached displacement is reused unchanged, exactly like before spec 7.

Verification: `claude_scripts/verify_synthetic_earthquake.py` — all 4
checks PASS (identity, magnitude scaling, distance attenuation, /compute
parity at defaults). Committed as `1e60361`.

## Task 4: `index.html` Earthquake Parameters panel

Removed the Amplify `<ctrl-row>` from Playback; added an "Earthquake
Parameters" `<details>` section (Epicenter Dist./Depth sliders, Magnitude
slider) between Building Parameters and View. New DOM refs, `DEFAULT_
EPICENTER_DISTANCE_KM`/`DEFAULT_EPICENTER_DEPTH_KM` JS constants (match
`mdof_response.py`'s), `currentRecordName`/`currentReferenceMagnitude`
module state, `earthquakeParamsAtDefault()` extending
`buildingParamsAtDefault()`, three slider handlers wired into
`scheduleLiveRecompute(true)`. `applyLoadedData()` now resets the Richter
default + epicenter sliders only on an actual record switch (not a
same-record Earthquake Parameter drag). `liveRecompute`'s POST body and
`/compute`'s two `applyLoadedData` call sites now carry
`epicenter_distance_km`/`epicenter_depth_km`/`richter_magnitude` and
`reference_magnitude` respectively.

Ruling: the plan's Step 7 calls `updateEpicenterMap()`, which Task 6
defines -- added a temporary no-op stub in Task 4 (JS function hoisting
means the eventual real definition works fine regardless of file order)
so Task 4 could be tested and committed on its own rather than leaving a
ReferenceError until Task 6 lands. Task 6 will replace the stub.

Ruling: `sliderPosToAmplify()`/`amplifyToSliderPos()` had no remaining
callers once the Amplify slider was removed (amplify is auto-computed via
`setAmplify()` elsewhere) -- deleted as dead code rather than left in
place, along with the now-unneeded `AMPLIFY_SLIDER_MAX` constant.

Verification: real-browser pass (Claude in Chrome) against
`http://127.0.0.1:8000/` served from this worktree (killed two stale
server processes squatting on port 8000 from earlier sessions first).
Confirmed: Amplify row is gone; Earthquake Parameters panel opens and
shows the record's own reference magnitude (4.9 M for ANZA1_CIDLA, not a
hardcoded default); dragging Magnitude to 9.0 M triggers a live recompute
with visibly stronger sway and an active shaking meter; zero console
errors on page load or after the interaction. Committed as `116fe79`.

## Task 5: `index.html` live Input trace frequency-domain drawer

Added `computeInputSpectrum()` -- derives the ground *acceleration*
magnitude spectrum from ground *displacement* via frequency-domain
double differentiation (`|accel(f)| = omega^2 * |disp(f)|`) at
`logBinSpectrum`'s own bin-center frequencies, same technique already
used for furniture's absolute-acceleration input. `redrawSpectrumPanels()`
now branches: at default Earthquake Parameters it uses the exact cached
`spectrum.json` acceleration spectrum unchanged; otherwise it uses
`computeInputSpectrum()`'s live approximation. Unified both shapes into
one `gSpec = { fHz, mag, dt, nyquist_Hz }` object so the rest of the
function (transfer function, output spectrum, draw calls) doesn't care
which source it came from.

## Task 6: Epicenter cross-section map

Replaced the Task 4 `updateEpicenterMap()` no-op stub with the real
implementation: a small always-visible fixed-position overlay (CSS-only,
`#epicenterMap`) with a circular plan view (epicenter distance -> dot
radius from a fixed center building mark, illustrative fixed angle since
the model has no azimuth input) plus a separate vertical depth gauge
(own channel, since the circular view has no room left for a second
distance-like axis). Magnitude drives a CSS `@keyframes` pulse's
period/scale via two custom properties (`--pulse-duration`,
`--pulse-scale`) set from JS -- decorative, not physics. Wired into all
three Earthquake Parameter slider handlers and record load.

Verification (Tasks 5+6 combined): `node --check` on the extracted
`<script type="module">` block (syntax clean), then
`claude_scripts/fft_check.mjs` + `claude_scripts/fft_check_scipy.py` --
all 4 checks PASS (complex FFT and binned-spectrum agreement with
`scipy.fft` to ~3e-14 relative error at two lengths), confirming Task 5's
reuse of the existing FFT/binning path didn't regress it. Committed as
`1f11903`, together with the bug fix below (found while testing this
work in a real browser, so fixed in the same commit rather than filed
separately).

## Bug found + fixed: unbounded attenuation amplification (commit `1f11903`)

Real-browser testing at an extreme Earthquake Parameters combo
(distance=1km, depth=1km, magnitude=8.9) made the building vanish
entirely -- console showed no errors, but peak floor displacement read
**11,875,958.984 mm (~11.9 km)**. Root cause:
`apply_synthetic_earthquake_scaling()`'s anelastic-attenuation term
(`mdof_response.py`) computed `exp(-pi*f*(R-R0)*1000/(Q*v))` -- correct
for R > R0 (decay), but when the requested hypocenter is *closer* than
the reference geometry (R < R0, as at this combo: R=hypot(1,1)=1.41km,
R0=hypot(20,10)=22.36km), `(R-R0)` goes negative and the exponent flips
positive, so the term stops attenuating and starts **amplifying** high
frequencies without bound (12,097x at 100 Hz for this exact combo,
confirmed with a standalone numpy check). This blows up whatever
high-frequency noise floor is in the recorded trace, since there's no
real source spectrum to "restore" -- you can't recover energy the
reference recording already lost to real-world attenuation.

Fix: clamped the exponent to `<= 0` (`max(R - R0, 0.0)`) -- R <= R0 now
means "no attenuation adjustment" (factor 1, same as identity), never
amplification. This is the physically correct treatment: Q attenuation
can only remove energy the recorded trace still has.

Verification: `claude_scripts/verify_synthetic_earthquake.py`'s 4 checks
still all PASS after the fix (identity, magnitude scaling, distance
attenuation ratio, `/compute` parity at defaults -- none of those
exercise R < R0, so this is a real regression-safety confirmation, not
just "didn't break the tests that already covered this"). Standalone
check at the exact bug combo: peak floor displacement dropped from
~11.9km to ~226m (large -- a M8.9 quake 1km away is genuinely
catastrophic in this model's own magnitude/geometric-spreading scaling
-- but now bounded/consistent with the model's own math instead of an
unbounded numerical artifact). Confirmed in a real browser: building
renders and animates normally at this combo post-fix.

## Bug found + fixed: building drifts off-center at extreme parameters (commit `d96a5de`)

After the attenuation fix above, the user reported a second, related bug
at the *same* distance=1km/depth=1km/magnitude=8.9 combo: the building no
longer vanishes, but now visibly drifts away from the camera's fixed
center as playback progresses, instead of swaying in place. Root cause:
`index.html`'s `AMPLIFY_MIN = 1` constant. The auto-scale amplify formula
(`TARGET_SWAY_FRACTION * storyHeight / peakAbsDisp`, in
`applyLoadedData()`) is supposed to keep on-screen sway legible
regardless of the earthquake's actual physical magnitude -- for real
recorded earthquakes `peakAbsDisp` is always mm-cm scale, so the computed
amplify is always >> 1 and `AMPLIFY_MIN` never binds. But post-fix, this
combo's *legitimate* peak floor displacement (~226m, see above) makes the
mathematically correct amplify fall to ~0.005 -- and the `AMPLIFY_MIN=1`
floor silently forced it back up to 1, reapplying ~190x more
amplification than the auto-scale formula intended, which is what sent
the building swinging far off-center rather than staying centered.

Fix: lowered `AMPLIFY_MIN` to `1e-6` (a true division-by-zero guard, not
a value that can plausibly bind for any realistic target sway) so the
auto-scale formula's own math is trusted at extreme magnitudes instead of
being overridden.

Verification: real browser, same combo (distance=1km, depth=1km,
magnitude=8.9) -- building now stays centered and sways visibly in view
through at least 5s of playback (previously drifted off within that
window), zero console errors. `node --check` confirmed no syntax
regression from the edit.

## Next

Task 7: real-browser verification pass against
`verification/07-synthetic-earthquake.md` sections 6-8 (full checklist,
not just the two bugs above -- those were caught incidentally while
testing Task 4/5/6, not from a systematic pass through the verification
doc yet). Task 8: docs/tooling checklist (README, math PDF,
`claude_scripts/math-pdf-sections-goal5.md`-style notes file for spec 7 if
the PDF needs new equations, `AGENTS.md`, `graphify update .`).

**Before starting Task 7**, re-run the full `claude_scripts/
verify_synthetic_earthquake.py` suite once more (last run was before the
two bug-fix commits above touched `mdof_response.py`/`index.html` again)
just to have a fresh clean PASS on record, then do the systematic
real-browser pass. Also worth deliberately re-testing 2-3 *other* extreme
corners of the Earthquake Parameters space (e.g. max distance + max
depth + low magnitude; default distance/depth + max magnitude) since the
two bugs found so far were both specific to the "very close + very
strong" corner -- there could be a third issue lurking in a corner not
yet tried (e.g. very large R relative to R0, where `spreading_scale =
R0/R` gets very small -- check it doesn't collapse to zero/NaN at
`distance_km=200, depth_km=100`).

## Task 7: Systematic real-browser verification pass

Ran `claude_scripts/verify_synthetic_earthquake.py` fresh first (per the
checkpoint's step 1) -- all 4 checks still PASS post-bugfix commits.

Deliberately tested 2-3 extreme corners beyond the one already found buggy
(per checkpoint step 2), all in a real browser (Claude in Chrome) against
this worktree's `server.py`:
- Max distance (200km) alone: T1 unchanged (1.06s/0.95s), epicenter dot
  moved outward, zero console errors.
- Max depth (100km) alone: same, plus depth gauge (separate channel from
  the circular distance dot) moved to its own max independently.
- Max distance + max depth + max magnitude (9.0M) together: stable, no
  NaN/blowup, T1 unchanged -- confirms the far corner opposite the
  previously-fixed R<R0 amplification bug is also safe (large R relative
  to R0 doesn't collapse `spreading_scale` to zero/NaN).
- Magnitude alone at max (9.0M), distance/depth held at exact defaults
  (20km/10km, reloaded page to confirm): T1 unchanged, epicenter dot
  position unchanged (only the pulse animation reacts), zero console
  errors -- confirms the three sliders are genuinely independent.
- Re-tested the original bug corner (dist=1km, depth=1km, mag=9.0, close
  to the exact combo that produced both prior bugs): building renders
  centered and bounded (epicenter map correctly reads "1 km - 1 km"),
  peak floor displacement 284,659mm (~285m) -- matches the magnitude-scale
  math almost exactly against the previously-documented ~226m at M=8.9
  (226 * 10^(9.0-8.9) = 284.5m predicted vs 284.66m measured), an
  independent confirmation of check 2's exact-10x-per-magnitude-unit rule
  using a real end-to-end browser render, not just the offline script.

Worked through verification doc sections 6-8 systematically:

**Section 6** (live sliders): each of the 3 sliders tested individually
(above) -- no console errors, T1 fixed. Amplify slider/label confirmed
absent from Playback in every panel screenshot this session. Sway stays
visible (not off-screen/invisible) at the extreme close/large combo.
Dragged Column X (a Building Parameter) while the extreme earthquake combo
was still active: building stayed centered, no sudden sway rescale, T1
correctly shifted to 1.01s/0.93s reflecting the stiffer column, zero
console errors -- confirms auto-scale re-trigger stays correctly scoped to
Earthquake Parameter changes only, not Building Parameters (no regression
of existing spec 2/4 behavior).

**Section 7** (epicenter map): confirmed always visible (verified via
`getBoundingClientRect()`) with the Earthquake Parameters section
collapsed. Distance/depth drag independently (already covered above --
dot moves horizontally, a separate depth gauge moves vertically, matching
the spec's "own channel" design). Richter slider's pulse animation
measured programmatically via computed `--pulse-duration`/`--pulse-scale`
custom properties, not just eyeballed: 2.20s/1.80 at M=3.0 (min) vs.
0.60s/3.20 at M=9.0 (max) -- both get faster and larger with magnitude, as
designed. Mobile breakpoint (`@media (max-width: 600px)`) rule confirmed
via stylesheet inspection (`#epicenterMap { width: 128px }` vs. 168px
default) and simulated by injecting it directly, since Claude in Chrome
can't resize the window (same limitation/workaround spec 6 already
documented) -- map is `position: fixed` anchored top-right, the mobile
bottom sheet is anchored to the viewport bottom, so overlap is
structurally impossible regardless of exact sheet height.

**Section 8** (frequency-domain drawer under synthetic scaling): opened
the drawer with the extreme combo active (dist=1km, depth=1km, mag=9.0).
Input trace visibly differed in shape from the default-record trace
(confirms it's recomputed live per Part B4, not stale `spectrum.json`).
Wrote a throwaway script,
`claude_scripts/verify_synth_spectrum_identity.py` (reuses
`verify_spectrum.py`'s `check_identity()` math, swapping in
`apply_synthetic_earthquake_scaling()`-reshaped accel/disp at this same
extreme combo) to numerically re-run goal 6's own identity check
(`|FFT(u_rel)| == |T(jw)| * |A_g(jw)|`) against reshaped ground motion
instead of the static default. **Result: max rel err 2.9e-15 on the
padded/exact grid** -- the only grid `verify_spectrum.py` itself gates on
(threshold 1e-9) -- matching goal 6's own precision at defaults. Confirms
the identity isn't accidentally broken by synthetic reshaping; the two
code paths (Input's frequency-domain double-differentiation of reshaped
ground displacement, and the *unchanged* transfer function) still combine
correctly.

Final network/console check across the whole session: all 9 `/compute`
POSTs returned 200, zero console errors captured in the full 216-message
buffer (all LOG-level, none ERROR/NaN/Infinity).

**Ruling**: `verify_synth_spectrum_identity.py`'s grid (b) (trimmed,
measured-only per `verify_spectrum.py`'s own documented precedent) showed
a higher max rel err (7.3%) than an arbitrary 5% threshold I first tried --
not a real problem, just this project's own established precedent that
grid (b) is loose due to FFT leakage from the truncated free-vibration
tail and was never meant to be hard-gated (only grid (a) is, at 1e-9).
Rewrote the check to report grid (b) and gate only on grid (a), matching
`verify_spectrum.py`'s own structure exactly rather than inventing a new
threshold.

Task 7 complete -- all of sections 6-8 pass. Next: Task 8 (docs/tooling
checklist).

## Task 8: docs/tooling checklist

- README.md updated (glossary entries for Richter magnitude/epicenter
  distance-depth/geometric spreading/anelastic attenuation; the Amplify
  glossary entry rewritten to reflect there being no manual slider;
  index.html section documents the new Earthquake Parameters panel and
  the Frequency Domain drawer's live reaction to it; a Current State
  changelog entry covers the panel, the epicenter map, and the R<=R0
  attenuation-clamp bug fix).
- Math PDF source notes written at
  `claude_scripts/math-pdf-sections-goal7.md` (Part E, sections E1-E6),
  matching the beginner-friendly style of `math-pdf-sections-goal5.md`:
  the frequency-domain framing, the literal Richter amplitude-ratio
  definition, geometric spreading, anelastic attenuation as a genuine
  per-frequency filter (and why the R<=R0 clamp is a physical necessity,
  not a numerical safety valve), and a section explaining why the
  on-screen sway still looks calm at most instants even at extreme
  parameters (real earthquakes are inherently peaky; both scale factors
  are frequency-uniform so they cannot change that peakiness; the
  auto-scale normalizes to the single global peak) -- not a bug, a
  documented property of the existing spec 2 auto-scale design.
- `AGENTS.md`'s own spec 7 status line intentionally NOT touched here --
  per this project's convention it only gets marked done in the main
  checkout after the user actually merges (see the goal skill's
  checkpoint note); still says "not yet implemented" in this worktree's
  copy, which is expected.
- `graphify update .` run -- 104 nodes, 131 edges, 8 communities, clean.
- No new dependency was added by this spec, so `requirements.txt` needs no
  change.
- `seismic-sim-backend` mirror is still owed once merged (server.py's
  `/compute` gained the epicenter/magnitude params in Task 3, and
  mdof_response.py gained `apply_synthetic_earthquake_scaling()` in Task
  1) -- to be called out in the final report, not done here.

Task 8 complete. Next: final whole-branch review pass against the spec
and verification doc, then checkpoint stage `verify`, then the finish
menu.

## Final whole-branch review (stage: verify)

- Reviewed the full `main..HEAD` diff against `specs/07-synthetic-earthquake.md`
  Parts A-D and `verification/07-synthetic-earthquake.md` checks 1-8, all
  pass (checks 1-4 via `claude_scripts/verify_synthetic_earthquake.py`,
  check 5 confirmed by re-inspecting commit d83f476's diff -- only the new
  `reference_magnitude` field was added to `out/`, no other bytes changed --
  checks 6-8 via the Task 7 real-browser pass, already in this file above).
- Confirmed `mdof_response.py`'s `apply_synthetic_earthquake_scaling()`
  matches spec Parts A2-A4 exactly: literal Richter definition, R<=R0
  attenuation clamp, one function called by both `__main__` (always
  identity params) and `server.py`'s `/compute` (live slider values) --
  no forked implementation.
- Confirmed the three-site DEFAULT_EPICENTER_DISTANCE_KM/DEFAULT_EPICENTER_DEPTH_KM
  agreement this project is strict about: `mdof_response.py` defines them,
  `server.py` imports them directly (not a duplicate copy), `index.html`
  mirrors them with a cross-referencing comment -- matches the existing
  COLUMN_DEPTH_X/Y pattern`AGENTS.md` already documents.
- Filled in the two remaining doc-checklist items not yet done going into
  this pass: `specs/COURSE-CONCEPTS.md`'s Filtering row now covers the
  attenuation filter (and why it's hand-derived, not library-sourced --
  a physical model, not a digital-filter-design problem); `specs/README.md`'s
  goal-7 status row updated from "Not started" to done-and-verified
  (noting the branch is not yet merged). Both files are gitignored and
  junctioned into this worktree, so these edits land directly in the main
  checkout, not just this branch.
- `AGENTS.md`'s own status line update stays deferred to the main checkout
  post-merge, per the goal skill's own checkpoint note (AGENTS.md is
  copied, not junctioned, into this worktree).
- Working tree is clean; no uncommitted changes remain in the branch itself.

Stage: verify complete. Next: present the finish menu (merge locally /
keep as-is).

---

## Post-f307bc1: three off-centre / furniture bugs (2026-08-28)

User report: "if you tweak the parameters sometimes the building visibly
moves away from the center" (screenshot: a single chair on its slab, far
off the grid origin) and "the shaking is really odd for the furniture."

Three independent root causes, all found by measurement, all fixed.

### 1. Record switch applied the OUTGOING record's magnitude (`index.html`)

`earthquakeParamsAtDefault()` compares `richterSlider.value` against
`currentReferenceMagnitude`. On a record switch the slider still holds the
record you are leaving, and `currentReferenceMagnitude` is still that same
record's — so the check said "not at default" whenever the two records had
different magnitudes, dropping out of the static `out/` fast path into
`/compute` **with the wrong magnitude**.

Measured, ANZA1_CIDLA (4.92 M) -> KOCAELI_ATK (7.51 M): furniture peak came
out at 0.087 mm against the bin's true 35.47 mm — 407x too small, i.e.
`10^(4.92-7.51)`. The panel then displayed "7.5 M". The reverse direction
(big record -> small record) overdrives by the same factor and is the
visible half: the building sways orders of magnitude too hard and parks
off-centre.

Fix: reset the epicenter sliders and null `currentReferenceMagnitude`
in the `folderSelect` handler *before* the load-path branch, and omit
`richter_magnitude` from `/compute`'s body while it is null — `server.py`
already falls back to that record's own `reference_magnitude`.

Verified: all 10 records, switched in both directions (20 transitions),
now take the static path (`compute=0`) and match their `furniture_response.bin`
peak to **relative error 0.0**, with the magnitude slider showing the right
record's value every time.

Related, same class: the slider's `step` was 0.1 but real magnitudes carry
two decimals (7.51, 4.92, 6.6), so the "default" slider position was 0.01 M
off what `out/` was built at — a permanent 2.3% amplitude step between the
static and `/compute` paths at nominally identical settings. `step` is now
0.01 (label still reads one decimal), and `currentReferenceMagnitude` is
read back off the slider after assignment so the invariant holds regardless.

### 2. Any earthquake-slider nudge swapped the ground-displacement source (`server.py`)

`disp_x` was `ground["X_disp"]` (the recorded PEER `.DT2` trace) at default
parameters but `_integrate_accel(accel_x)` at any other parameters. Those
two differ by **2.3x in peak and correlate only 0.65** on KOCAELI_ATK —
PEER's own baseline correction is not reproducible from a generic
Butterworth high-pass. One slider step therefore jumped the whole building
to a differently-shaped, differently-scaled waveform.

Fix: apply `apply_synthetic_earthquake_scaling()` to the cached displacement
directly. This is exact, not an approximation: the filter multiplies the
spectrum by a real, non-negative `s(f)`, and displacement/acceleration
spectra differ only by `-1/w^2`, so the same `s(f)` applies unchanged to
both. Identity at defaults is preserved bit-for-bit, and the
`is_default_quake` special case disappears entirely.

`claude_scripts/check_quake_continuity.py` (new): identity to 4.3e-16;
a 1 km distance nudge now moves the ground-displacement peak **3.92%**
(old path: 58.9%); `s(f)` measured bin-by-bin off the acceleration and the
displacement agrees to <1e-9 relative at three parameter corners.

### 3. On-screen sway grew linearly with a logarithmic scale (`index.html`)

`amplify` is frozen at record-load by design (so a bigger synthetic quake
genuinely sways more). But one Richter step is a literal 10x, passed
straight through to the render — `+1 M` already threw the building several
building-widths sideways, and `f307bc1`'s clamp only caught it at
`0.6 * totalHeight` = **14.7 scene units** for the default 7-story building,
against a ~2.8-unit footprint. That is the screenshot.

Fix: `swayDisplayGain()` compresses everything above the record-load
baseline (`displayPeak = baseline * (physical/baseline)^0.35`) and caps at
`0.15 * totalHeight`. Exactly the identity at the baseline, linear below it,
strictly monotonic, bounded above.

`claude_scripts/check_sway_gain.mjs` (new) extracts the function out of
`index.html` at runtime (never a copy — same rule as `fft_check.mjs`) and
asserts all five properties:
`baseline=0.612u  cap=3.67u  +1M=2.24x  extreme(489x)=3.68u (was 14.7u)`.

Furniture was scaled by `amplify` — a **displacement**-calibrated number,
while furniture responds to floor **acceleration**. The ratio between the
two swings by ~130x across the dataset (measured furniture auto-gain now
ranges 4.3 to 562.6 across the 10 records), which is why furniture looked
frozen on some records and pinned against its clamp on others. It now has
its own record-load auto-scale targeting a 0.22-unit peak, shared across
all three classes so the actual physics on display — a 3 Hz fan swings far
more than an 8 Hz table under the same floor motion — is preserved, and it
rides the building's own compression ratio so the two never disagree.

### Furniture *math* reviewed, deliberately unchanged

`compute_furniture_response()` and `furniture_frf()` were re-checked against
the governing equation and the verification suite: `H(0) = 1/wf^2`, peak at
`wf*sqrt(1-2*zeta^2)`, `u = -H*Xb''` sign correct, absolute floor
acceleration obtained by `-w^2 * Q(jw)` in the frequency domain (not a
time-domain re-differentiation). All correct. The "odd shaking" was
entirely the render gain above.

Two candidate physics changes were measured and rejected:
- Raising `zeta` from 0.02 on the theory that furniture rings on after the
  quake: measured post-shaking residual is only 1.5-2.3% of peak. No
  visible ringing to remove.
- Re-deriving the fan as a real pendulum (`f = sqrt(g/L)/2pi`, ~0.85 Hz for
  a 0.35 m rod): physically appealing, but it produces 464 mm of relative
  displacement on KOCAELI_ATK — roughly 60 degrees of swing on that rod,
  far outside the small-angle regime the linear SDOF model assumes. Kept
  the stiff-mount interpretation the docstring already describes.

`FURNITURE_CLASSES` is therefore untouched, so **no `out/` regeneration and
no `mdof_response.py` change** — the backend mirror obligation is limited to
`server.py`'s `/compute` (still due on merge, alongside spec 6's).

### Verification run

- `claude_scripts/verify_frame_furniture.py` — ALL CHECKS PASSED (incl.
  check 6, `/compute` parity against `out/`).
- `claude_scripts/verify_spectrum.py` — OVERALL: PASS.
- `claude_scripts/fft_check.mjs` + `fft_check_scipy.py` — ALL CHECKS PASS
  (rel_err 3.4e-14).
- `claude_scripts/check_quake_continuity.py` — new, passes.
- `claude_scripts/check_sway_gain.mjs` — new, passes.
- Real browser (`http://127.0.0.1:8000/`): 20 record transitions clean,
  magnitude sweep 3->9 M monotonic and bounded, extreme corner
  (1 km / 1 km / 9.0 M) capped at 3.72 units, return-to-default exact,
  zero console errors.
- `graphify update .` — 106 nodes, 132 edges, 8 communities.

Note for the next session: a `server.py` process from an earlier session was
still holding port 8000 with the pre-fix module loaded in memory, which made
the first round of browser measurements look like the fix had not landed.
Static files are re-read per request, Python modules are not — check
`Get-NetTCPConnection -LocalPort 8000` before trusting a `/compute` result.

---

## Frequency-domain panel audit: is it stale against Earthquake Parameters? (2026-08-28)

User suspicion: the frequency-domain drawer (spec 6) predates Earthquake
Parameters (spec 7) and might still show the original record's spectrum
regardless of epicenter/magnitude reshaping -- "computes based on past
computations when earthquake didn't have a specified epicentre."

**Checked the code path, then verified behaviorally in the browser.**
`redrawSpectrumPanels()` already gates on `earthquakeParamsAtDefault()`:
at default parameters it uses the cached, parameter-independent
`spectrum.json` (the recorded quake's own spectrum); away from default it
calls `computeInputSpectrum()` (added by spec 7, Part B4), which recovers
the acceleration spectrum from whichever ground *displacement* is
currently loaded -- i.e. the already-reshaped synthetic quake -- via
`|FFT(accel)| = omega^2 * |FFT(disp)|`. This was correctly wired up in
spec 7's own implementation.

Verified by comparing `spectrumCanvas.toDataURL()` before/after moving
Earthquake Parameter sliders (a byte-for-byte proxy for "did the drawn
content change"):
- Default -> extreme corner (1 km / 1 km / 9.0 M): canvas changes
  (46034 -> 46106 bytes), drawer stays enabled, no disabled-note.
- Extreme corner -> back to default: canvas returns **byte-identical**
  (46034 == 46034) -- confirms the cached `spectrum.json` path is used
  again exactly, no drift/hysteresis from having gone through the live
  path.
- A single small nudge (distance 20km -> 5km -> 20km) also round-trips
  byte-identical.
- Zero console errors from the app itself across all of this (one
  leftover console error in the tab was from an earlier broken debug
  `javascript_exec` call of mine, unrelated to the app -- confirmed by a
  fresh navigate producing the same cached message before any app code
  ran).

**Conclusion: the code was already correct.** What was actually stale was
one documentation bullet in `AGENTS.md` ("`spectrum.json` is ...
parameter-independent"), written for spec 6 and never revisited when spec
7 added Earthquake Parameters that DO reshape the record -- an accurate
statement became a misleading one out from under it, and no spec-7
"Things to know" bullet was ever added to replace it (a gap in spec 7's
own docs checklist). Fixed in the worktree's local `AGENTS.md` (gitignored,
not part of this commit) to state the actual invariant precisely and
describe the Part B4 fallback that keeps it true.

---

## Real bug: frequency panel invisible to Magnitude changes (2026-08-28)

User re-reported after the audit above: "I changed the sliders of
earthquake parameters but the frequency domain panel didn't change." This
time it was real -- my earlier canvas-byte-length spot check missed it
because the diff was small but nonzero (attenuation had kicked in at the
extreme corner I happened to test), and I concluded "the panel updates"
without checking whether the update was actually *visible*.

**Root cause**: `magnitude_scale = 10**(magnitude - reference_magnitude)`
and `spreading_scale = R0/R` in `apply_synthetic_earthquake_scaling()` are
both frequency-flat multipliers by construction (verified: a Richter bump
at R==R0 changes every FFT bin by the identical ratio to 1e-14). The
frequency-domain drawer's per-subplot dB axis auto-scales to that panel's
OWN peak every redraw (`dbMax = max over this panel's curves`, `dbMin =
dbMax - 70`, no persisted reference) and never printed a dB value
anywhere -- so a flat gain shifts the axis by exactly the same amount as
the curve, leaving the rendered SHAPE (and therefore every pixel except
antialiasing noise) unchanged. Distance/depth changes large enough to
clear the `R <= R0` attenuation clamp DO reshape the curve and were
already visible; Magnitude -- the slider anyone reaches for first -- never
was, at any value.

**Fix**: `drawSpectrumSubplot()` now appends the panel's own peak level to
its label (`Input |A_g(f)| · X · 4.0 dB` etc), computed from `dbMax`
which the function already had. One line, no call-site changes, and
doesn't touch the auto-scale itself -- shape-legibility (what makes the
attenuation filter's real frequency-dependent effect visible) is
preserved.

Verified by intercepting `CanvasRenderingContext2D.prototype.fillText` and
reading the actual printed strings (not just canvas byte-length, which is
too easy to fool with antialiasing noise -- the mistake made during the
earlier audit):
```
default:        Input 4.0 dB   Transfer -8.7 dB   Output -11.9 dB
M=9.0 (R=R0):    Input 33.8 dB  Transfer -8.7 dB   Output  17.9 dB
back to default: Input 4.0 dB   Transfer -8.7 dB   Output -11.9 dB
```
+29.8 dB on both Input and Output matches `20*log10(10**(9.00-7.51))`
exactly; Transfer is correctly unchanged (building-only, earthquake-
independent); returning to default reproduces the original values
exactly, no drift. Zero console errors from the app.

All four checks (`check_sway_gain.mjs`, `check_quake_continuity.py`,
`verify_frame_furniture.py`, `fft_check`) still pass -- this was a
visualization-only fix, no physics touched.

---

## Code review pass (2026-08-28)

Full review of `f307bc1..a1ba992` -- diff read line by line, math re-derived
independently, animations exercised in a real browser. One Important issue
found and fixed; everything else confirmed correct.

### Important (fixed in this pass)

**Furniture still used a per-sample clamp -- the exact pathology `f307bc1`
fixed for the building.** `updateFurnitureOffsets()` did
`Math.max(-C, Math.min(C, offset))` on every sample. The building and the
furniture are compressed by the same `gainRatio`, but their headrooms differ:
the building may grow 6x over its baseline before hitting its cap
(0.612 -> 3.67 units), furniture only 2.3x (`FURNITURE_TARGET_SWAY` 0.22 ->
`FURNITURE_RENDER_CLAMP` 0.5). So furniture reached its ceiling while the
building was still well inside its own.

Measured on KOCAELI_ATK at M=9.0 (growth 30.9x, measured gainRatio 0.1075):
rendered furniture peak 0.731 units against a 0.5 clamp -- **0.021% of
samples flattened**. Small in count, but those are precisely the peak
moments, and a per-sample clamp pins every one of them to the identical
value: shape destroyed rather than size bounded.

Fixed by mirroring `swayDisplayGain()`: added `currentFurniturePeak`
(refreshed on EVERY load, like `currentPeakDisp`), and
`updateFurnitureOffsets()` now caps the gain so the peak lands exactly on
the clamp, scaling the whole waveform uniformly. New
`claude_scripts/check_furniture_gain.mjs` extracts the shipped code out of
`index.html` (never a copy) and asserts the per-sample clamp is gone,
identity at baseline, monotonicity, boundedness over a 1e-3..1e4 growth
range, and that the M=9.0 regression scenario is capped.

### Verified correct (no change needed)

- **Furniture SDOF math.** Checked `compute_furniture_response()` against a
  piecewise-exact (Duhamel) recurrence -- a genuinely different method with
  zero period elongation, itself validated to 1.65e-6 against the closed-form
  steady state. Worst relative difference 4.7e-3 across 3 floors x 3 classes,
  and the residual scales with frequency exactly as the recurrence's
  piecewise-linear assumption predicts (table 4.7e-3 > chair 1.7e-3 > fan
  6.8e-4). An initial Newmark-beta check appeared to FAIL at 12.8%; that was
  the integrator's own period elongation accumulating over ~1000 cycles, not
  a real discrepancy -- worth recording, since the naive check is misleading.
- **`server.py`'s `scaled("X_disp")`.** All 10 records carry non-null
  `X_disp`/`Y_disp`, and `Y_disp` is only reachable under the same `has_y`
  guard the old code used. No None-deref regression.
- **`swayBaseline` is never 0 in practice.** The initial load path
  (`loadFolderData(folders[0])`) calls `applyLoadedData()` without the
  `autoScaleAmplify` argument, so it defaults to `true` and the baseline is
  always set before any render.
- **`furnitureAmplify` frozen across Building Parameters is safe.** Swept
  the full slider ranges via `/compute`: rendered furniture peak stays within
  0.018..0.373 units against the 0.5 clamp (worst = zeta 0.01; smallest =
  stories 20 + mass 5e6 + column 0.5). No pinning, no invisibility.
- **`computeInputSpectrum()`'s bin-centre omega^2 approximation.** Compared
  against the exact `spectrum.json` path: mean -0.21 dB, median -0.04 dB,
  std 0.58 dB over the 367 bins actually drawn, worst +-3.3 dB confined to
  44-56 Hz (at/above the 50 Hz display cap). **Panel label jump 0.0 dB**, so
  crossing from the default to the live path causes no visible discontinuity.
- **Zero console errors.** A fresh tab driven through a full stress sweep
  (3 record switches, drawer open, M=9.0, epicenter 1km/1km, stories 20,
  then epicenter 200km/100km + M=3.0) produced no errors. An error seen in
  earlier tabs was a stray debug `javascript_exec` of mine, confirmed by its
  absence here.

### Minor (noted, not changed)

- The dB label added in `a1ba992` is 242px at its worst case
  (`Output |U_rel(f)| · floor 20 · -111.9 dB`). The drawer is `420px`
  capped at `92vw`, so the tightest real device (320px) gives ~252px of plot
  width -- it fits, but with only ~10px to spare. A narrower future
  breakpoint would clip it.
- The `📐 On-screen sway peak` line logs on every live recompute, so dragging
  a slider produces a line per debounced settle. Consistent with the existing
  `✅ Live recompute` line; left as-is since it is the readout that made both
  render-gain bugs diagnosable.

### Suite after the fix

`check_sway_gain.mjs`, `check_furniture_gain.mjs` (new),
`check_quake_continuity.py`, `verify_frame_furniture.py` (ALL CHECKS PASSED),
`verify_spectrum.py` (OVERALL: PASS), `fft_check` -- all green.
`graphify update .` clean.

---

## Merge, backend mirror, and math PDF (2026-08-28, post-merge on main)

After the code-review pass above, the user asked to update all stale
docs, merge to main, and mirror the backend. Full sequence:

**Docs updated before merge**: `verification/07-synthetic-earthquake.md`
got correction notes on checks 6 and 8 (both originally passed on
narrower criteria than what real usage later exposed); `specs/README.md`'s
spec 7 row updated to merged status with the four extra defects
summarized; this branch's `README.md` got two more changelog bullets for
the furniture-clamp and frequency-panel fixes that predated those docs.

**Merge**: `git merge --no-ff goal/07-synthetic-earthquake` into `main`
(commit `76a1b81`), clean, no conflicts. Worktree cleanup hit a snag: a
stale `server.py` process (started earlier in the review pass, cwd
pointed at the worktree) held a file lock that made `git worktree remove`
silently leave junction/symlink remnants behind even though `git worktree
list` no longer showed the entry. Killed the process, `rm -rf`'d the
leftover directory, `git worktree prune`'d clean. Lesson: after `git
worktree remove`, check the directory is actually gone on disk, not just
absent from `git worktree list` -- Windows file locks can desync the two.
Ran the full check suite again against the merged main checkout itself
(not just the now-deleted worktree) to confirm the merge didn't silently
drop anything -- all green. `AGENTS.md` (main checkout's own copy, not
junctioned) rewritten for spec 7's final status, splitting spec 8 back out
into its own bullet. `graphify update .` on main: 119 nodes, 145 edges.

**Backend mirror** (`D:\seismic-sim-backend`, commit `39eb0ff`):
`mdof_response.py` copied verbatim (gains `apply_synthetic_earthquake_
scaling()` and the two `DEFAULT_EPICENTER_*` constants). `server.py`
rebuilt from the local file programmatically (a small Python script did
the 5 intentional substitutions -- docstring, CORS import/setup, dropped
static-file routes, `$PORT`-aware `__main__` -- then was deleted; never
hand-retyped 250 lines). All 10 `out/<record>/ground_accel.json` copied
(each gained `reference_magnitude`). Verified with a byte-for-byte
`/compute` parity check between the mirror (run locally on port 8001) and
the main checkout (port 8000) across 7 cases spanning Building AND the
new Earthquake Parameters (magnitude 9.0, extreme close epicenter,
extreme far epicenter + low magnitude, a Building Parameter combo, two
other records) -- all seven byte-identical. Committed, not pushed, per
the user's explicit instruction ("I'll do the push manually").

**Math PDF** (main checkout, commit `7ba91c6`): the user asked directly
whether the epicenter/magnitude math was actually in the PDF. Checked by
extracting the previous PDF's text and searching for Richter/epicenter/
attenuation/hypocentral/spreading -- zero hits across all 20 pages, only
6 unrelated "magnitude" hits (spectrum magnitude, not Richter). Confirmed
the gap was real, not just undocumented.

Root cause of the gap: `claude_scripts/math-pdf-sections-goal7.md` (spec
7's own draft notes, written during that spec's implementation) opens
with "there's no editable source file... regenerating the PDF is the
user's manual step" -- copied forward from spec 5's equivalent notes file
without checking. That claim was already false by the time it was
written: `claude_scripts/generate_math_pdf.py`, a genuine ~1800-line
reportlab pipeline (matplotlib-rendered equations/diagrams, laid out with
Platypus), already existed and is how Parts A-D of the current PDF are
actually produced. Nobody had extended it for spec 7.

Added a new "Part E -- Reshaping the earthquake itself" (E1-E5) directly
to the generator script, adapting the draft notes' content into the
reader-facing prose style Part D already uses -- new equations
(`escale`, `richterdef`, `magscale`, `Rhypo`, `R0hypo`, `spreadscale`,
`attenuation`, `scaletotal`), one new diagram (`fig_attenuation_scale`),
and 5 new rows in the symbol reference table. Two real corrections made
along the way, not just a mechanical port:
- The draft notes' §E6 cited "peakiness measured at roughly 20x" for why
  the sway can look calm at most instants even at extreme parameters.
  Checked against the actual 10 records rather than trusted: the
  median-to-peak ratio ranges 13x to 97x, nothing close to a single
  "roughly 20x" figure. Rewrote that sentence to cite the real range.
- The first version of the new diagram plotted `scale(f)` on a raw linear
  axis across three parameter combinations -- the identity and
  magnitude-only curves sit at 1 and 10, dwarfing the attenuation curve's
  roll-off (which decays from ~0.15 toward 0), so the one thing the
  figure existed to show (a genuine per-frequency filter vs. two flat
  multipliers) was nearly invisible. Fixed by normalizing each curve to
  its own value at the lowest plotted frequency, so magnitude and
  identity land exactly on top of each other at 1.0 (visually proving "no
  shape change") while the far-hypocenter curve visibly bends.

Verified: every new equation PNG and the new diagram read back and
visually inspected directly (the documented failure mode for this
pipeline, per its own docstring, is silent baseline-clipping that only
shows up in the raster, not in text extraction) -- clean. A post-build
check confirmed Part E and all five subsections are present, all three
new formula terms appear, the symbol table gained its new rows, no
leftover TODO/placeholder text, and Parts A-D are still intact (24 pages,
up from 20). `math-pdf-sections-goal7.md`'s header rewritten to mark
itself historical and flag the stale claim so it doesn't get copied
forward into a future spec's notes file again.
