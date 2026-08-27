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

## Next

Task 4: `index.html` Earthquake Parameters panel (remove Amplify row, add
3 sliders, default-reset-on-record-switch, wire into live-recompute
debounce).
