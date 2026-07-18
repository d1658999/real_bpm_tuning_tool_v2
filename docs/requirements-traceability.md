# Requirements traceability

This matrix translates `Requirements.md`, `fleet.txt`, and `DESIGN-apple.md` into implementation and verification targets. The implementation is complete at application level; automated tests cover deterministic Python/Rust contracts, while RF sign-off and visual acceptance still require engineering review with real hardware.

## Fleet roles and decision workflow

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| F-01 | One Product Manager and five Senior RF Engineer strategies | A strategy registry with the six names and responsibilities preserved from `Requirements.md`/`fleet.txt` | Unit test registry names/count; report inspection |
| F-02 | Minimum-BOM strategy within 10% of the best non-antenna target error | Rust ranking filters to the near-optimal target set, then minimizes component count and total target error | Unit tests for floor, rejection, and ordering; RF integration run |
| F-03 | Balanced target-error/insertion-loss strategy | Sum of normalized peak target error and positive insertion-loss magnitude | Unit test score ordering; report comparison |
| F-04 | Minimum-target strategy | Lexicographically minimize non-antenna target error, then dependent antenna target error | Unit test metrics/objective; RF integration run |
| F-05 | Tightest target-centred Smith-chart strategy | Rust path minimizes target-error spread plus peak target error | Unit test known complex traces; plotted trace review |
| F-06 | Lowest-insertion-loss strategy after meeting the target gate | Filter to target error within `floor + max(0.005, 15% floor)`, then minimize positive S21 loss magnitude | Unit test objective; RF integration run |
| F-07 | PM selects lowest mass-production-risk result among five strategies | Apply the specified normalized risk formula and deterministic tie-breaking | Unit tests for formula, normalization and tie cases |
| F-08 | Sequential phases: assignment, five sweeps, PM judgement, written reason | Optimization orchestrator emits phase/progress events and report sections | Integration test; generated report inspection |
| F-09 | Fine-grained sweep using real BOM values | Search candidates come from supplied Murata files; optional generated 0.1-step ideal values may be diagnostic only, not selected as real BOM | BOM loader tests; selected-result provenance check |

## Workspace, ports, and RF computation

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| R-01 | Add and remove multiple `.sNp` files | Project model plus left-panel add/remove controls | Model round-trip test; GUI smoke test |
| R-01A | Keep unconnected files as future references when every port is open | Fully open disconnected networks remain serialized but are excluded from active circuit assembly and frequency intersection | Model and disjoint-frequency integration tests |
| R-02 | Default every port to `open` | Port model factory initializes `open` | Unit test after importing a Touchstone file |
| R-03 | Support `open`, `short`, `inductor`, `capacitor`, `inductor/capacitor`, `open/inductor/capacitor`, `connect`, and `signal` | Closed connection-kind enum and kind-specific settings | Parametrized model validation tests |
| R-04 | Open and short do not count toward GUI BOM count | `component_count` counts only selected capacitors/inductors | Unit tests for mixed connections |
| R-05 | Connect selects another file and port | Connection reference contains stable file ID and one-based port | Validation tests for missing/self/duplicate endpoints; GUI smoke test |
| R-06 | Up to four unique signal ports named s1-s4, ordered from PA-mid toward antenna | Signal validation enforces unique labels and range | Unit tests for duplicates, gaps and >4 signals |
| R-07 | Warn rather than run invalid connection settings | Central validator returns actionable messages; GUI presents them | Unit tests and message-dialog smoke test |
| R-08 | Default frequency range comes from each Touchstone file; user may override it | Per-file frequency-band model, bounded by available data | Unit tests for invalid/reversed/out-of-data bands |
| R-09 | Multiport signal bands: for N assigned signals, the highest signal is dependent and preceding signals can have individual bands | Per-signal optional bands with dependency validation | Unit tests for two-, three-, and four-signal configurations |
| R-10 | Optional non-normalized Smith-chart impedance targets per driven signal, disabled individually by default | Port-level resistance/reactance are stored in ohms and converted to Γ; dynamic controls exclude the final dependent antenna signal | Two/three/four-port scope validation, conversion, JSON migration, GUI, metrics, and plot tests |
| R-11 | Run cascade of configured Touchstone networks | scikit-rf circuit/cascade service with frequency interpolation and explicit terminations | Integration run on supplied files; finite S-parameter assertions |
| R-12 | Run optimization through Rust | Python exposes signal/tunable ports once; Rust performs the exhaustive parallel rank-one termination sweep, rejects non-passive signal results, and reduces the five exact strategy winners without materializing the full Cartesian result set in Python | Rust unit tests plus binary-bridge, passivity-regression, winner-parity, and RF integration tests |
| R-13 | Complex calculations use Rust; Python owns GUI/integration | Rust performs candidate scoring/search loop; Python/scikit-rf performs network I/O and final verification | Source/build audit; parity test on a small candidate set |
| R-14 | Supplied fleet topology and 3.3-5 GHz configuration can be represented | Seed/sample project mirrors all file/port assignments in `fleet.txt` | Configuration validation and cascade integration test |

## Real BOM

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| B-01 | Murata GJM02 capacitors come from `Capacitors_BOM` | BOM scanner loads `.s2p`, part number, type and nominal pF parsed from filenames | Unit/integration tests against representative files |
| B-02 | Murata LQP02TQ inductors come from `Inductors_BOM` | BOM scanner loads `.s2p`, part number, type and nominal nH parsed from filenames | Unit/integration tests against representative files |
| B-03 | UI shows the selected component's real value | Component selection stores part number and nominal value/unit | Model test; GUI inspection |
| B-04 | Optimization selects actual supplied parts | Candidate/result includes BOM file provenance and rejects unknown paths | Result validation test; report inspection |
| B-05 | User can constrain inductor nH and capacitor pF ranges independently for every tunable port | Inclusive range fields persist on each port and filter that port's measured BOM before evenly spaced sampling; legacy project-wide windows remain fallback-only; invalid or empty ranges stop with a file/port-specific warning | Per-port model validation, GUI mode/independence and migration round-trips, and optimizer option-filter tests |

## GUI layout and controls

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| G-01 | Three panels: file list left, connections middle, plots right | Resizable desktop splitter/layout | GUI smoke test and screenshot review |
| G-02 | Right panel shows S11/S22 Smith chart in standard scikit-rf form | Matplotlib/scikit-rf Smith projection with both traces | Plot-generation test; RF engineer review |
| G-03 | Plot insertion loss S21, VSWR S11/S22, and return loss S11/S22 | Shared-frequency plot dashboard with units and legend | Metric unit tests; plot inspection |
| G-04 | Reset original, zoom in/out, move and marker controls | Matplotlib navigation/marker handlers; reset restores original cascade | GUI interaction test/manual check |
| G-05 | Save combined figures as PNG | Figure export service writes one dashboard image | Temp-path export test verifies PNG signature/nonzero size |
| G-06 | Top toolbar has Run Cascade and Run Optimization | Toolbar actions call validation then background services | GUI smoke test |
| G-07 | Save and Load Config as JSON | Versioned JSON serialization with relative/source paths | Round-trip test and malformed-file error test |
| G-08 | Export current cascade as `.sNp` | scikit-rf Touchstone writer preserves port count | Export/read-back integration test |
| G-09 | Export insertion loss as CSV | CSV includes frequency and S21 dB columns | Export schema/value test |
| G-10 | Show optimization percentage/progress and allow cancellation | Worker thread/process receives Rust progress and cooperative cancel token; GUI exposes sampled BOM parts/type with 100-million-combination and estimated 10-minute warnings | Runner callback/cancellation, winner-mode parity, and GUI config round-trip tests; GUI smoke test |
| G-11 | Invalid settings or failures produce an actionable warning | Exception boundary turns failures into user-facing messages without hanging | Unit tests for service errors; GUI smoke test |
| G-12 | Merge measured component, connection target, signal assignment, and per-port L/C ranges into one Port configuration column | Four-column port table uses a mode-driven contextual editor; single-type modes show one range and combined modes stack both ranges for readable width | GUI structure, all four tunable-mode editors, independent range, validation, and configuration round-trip tests |

## Reports and result artifacts

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| O-01 | Compare five results: max non-antenna/antenna VSWR, target error, worst S21 insertion loss, component count, ±5% VSWR and target error, sensitivity, spread, and risk | Common target-aware metrics schema, JSON payload, and comparison table | Unit tests for known arrays; report schema check |
| O-02 | Risk = 0.30 normalized worst tolerance target error + 0.25 normalized count + 0.20 normalized VSWR sensitivity + 0.15 normalized absolute tolerance IL + 0.10 normalized target-error spread | Pure scoring function with nominal-target and VSWR-spread fallbacks; all-equal columns normalize to zero; result rounds to four decimals | Exact numeric unit tests |
| O-03 | Save each strategy's S11/S22 Smith plot, S21, VSWR and restorable JSON | Artifact exporter creates per-strategy JSON/PNG files; flexible open/L/C winners are resolved to fixed `open`, `capacitor`, or `inductor` modes | Temp-directory integration test; resolved-assignment and JSON reload tests |
| O-04 | Final decision plots include dotted VSWR=2 circle | Final dashboard adds constant-|Gamma| = 1/3 circle | Figure object or image review |
| O-05 | Written Markdown report identifies selected agent, exact parts/values and decision rationale | Report generator consumes comparison and winning result | Golden-section/schema assertions and human review |
| O-06 | Independent ±5% component-value analysis | Convert nominal termination gamma to impedance, scale L by the value factor or divide C impedance by it, and exhaustively sweep `[1.00, 0.95, 1.05]` per selected component in Rust | Rust bridge/integration tests; limitation must appear in report |
| O-07 | Fleet requirements output directory | Save agent optimization results to a timestamped folder `Fleet_results_YYYYMMDD_HHMMSS` containing all agents' JSON/PNG data, comparison plots, and report | Exporter saves directly to a dynamic subdirectory under the project root | Integration test checks timestamped folder creation and file presence |

## Visual design

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| D-01 | Follow `DESIGN-apple.md` | Desktop theme uses system/SF-like fonts, white/parchment/near-black surfaces, #0066cc action color, restrained hairlines, pill primary actions and no decorative gradients | Palette/style constants test where practical; screenshot review |
| D-02 | UI remains usable across desktop sizes and controls meet roughly 44 px targets | Splitters, scroll areas and minimum control heights | Manual checks at supported Windows display scaling |

## Optimization time-consumption acceptance

The native optimizer now evaluates the required six-slot, seven-sample case
(`15^6 = 11,390,625` combinations) with shared prefix states and bounded
winner reduction. The GUI warns above the specified 100,000,000-combination
ceiling and when a conservative preflight estimate exceeds ten minutes; the
runner also reports a post-sweep warning if the measured native sweep exceeds
ten minutes. Benchmark the actual machine and frequency grid before using a
large production run as a schedule commitment.

## Test commands

From the workspace root after installing project dependencies:

```powershell
python -m pytest -q --basetemp .pytest_tmp
```

If the Rust crate is present:

```powershell
cargo test --manifest-path rust_optimizer/Cargo.toml
```

## Current limitations and sign-off boundaries

- A passing unit suite does not certify the supplied multi-board RF topology. Final acceptance needs a cascade run over 3.3-5 GHz and review by an RF engineer.
- The requirements conflict on short-circuit counting: `Requirements.md` says short does not count, while `fleet.txt` says a 0-ohm part counts as one. The product-level GUI rule in `Requirements.md` is used for BOM count; reports should disclose any separate mounted-0-ohm count.
- "Lowest insertion loss" is interpreted as the least positive loss, equivalently the greatest (closest to 0 dB) passive S21 dB. Naming a negative S21 dB value as “lowest” without this convention would invert the objective.
- The supplied BOM is finite and does not contain every 0.1 increment through 20 nH/pF. Production selections must remain traceable to supplied files; an ideal fine-grid sweep cannot be described as a real-BOM result.
- A generic ±5% scaling of S-parameters is not a physical vendor-tolerance model. If component-level Monte Carlo is unavailable, output must label the result as a conservative proxy and not as production qualification.
- GUI plotting, drag/pan/marker behavior, cancellation responsiveness and Apple-style fidelity need manual or Qt integration testing in addition to headless unit tests.
