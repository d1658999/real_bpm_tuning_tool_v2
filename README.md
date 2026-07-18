# Real BPM Tuning Tool

A local Python/PyQt5 and Rust application for assembling multiport Touchstone circuits, tuning real Murata BOM components, and comparing five RF engineering strategies with a production-aware PM decision.

## Install

Requirements: Python 3.11+, Cargo/Rust, and a desktop environment.

```powershell
python -m pip install -e .
cargo build --release --manifest-path rust_optimizer\Cargo.toml
```

The Rust build is also started automatically on the first optimization if Cargo is available.

## Start the GUI

```powershell
bpm-tuner --gui
```

1. Add two or more `.sNp` files in the left panel.
2. Assign every active port in the middle panel's **Port configuration** table. Its four columns are `File`, `Port`, `Mode`, and `Port configuration`; the final column automatically changes to the measured-part picker, connection destination, signal assignment, or open/short summary required by the selected mode. Connections must be reciprocal, and signal names must be unique and consecutive from `s1` (two to four total). A file that is not yet part of the circuit may remain in the project when every one of its ports is `open`; it is ignored by cascade and optimization.
3. Use the compact **Frequency and Smith targets** table above Port configuration to set start/stop frequencies and optional impedance targets. It automatically shows only the driven signal rows (`s1`, then `s2`/`s3` when applicable); the dependent final signal is omitted. `Auto` uses the full Touchstone frequency range. Smith targets are off by default. Enable targets individually and enter physical resistance/reactance in ohms—for example, `50 + j0 Ω` targets the center of a 50-ohm Smith chart.
4. Leave `open/inductor/capacitor` unselected to use a true open baseline and let optimization choose a measured capacitor or inductor. A saved agent result resolves that flexible state to the actual winning `open`, `capacitor`, or `inductor`, so loading it restores a fixed circuit.
5. Set **BOM samples/type**, **L range**, and **C range** before optimization. Ranges are inclusive nominal values in nH and pF. The defaults span every measured BOM part; narrowing a range filters the real catalog before the requested number of evenly spaced samples is selected. Reversed, non-positive, non-finite, or empty measured-part ranges produce an actionable warning. The sample default is 2; increasing it covers more real parts but multiplies the Cartesian search at every tunable port. The GUI warns before searches above 100,000,000 combinations or an estimated 10-minute native sweep.
6. Use **Run Cascade** to simulate the selected configuration or **Run Optimization** to run all five strategies. Optimization progress and cancellation are shown at the top.
7. Save/load JSON configurations, export the cascaded Touchstone network and S21 CSV, or save the combined plot.

The plot panel supports reset, zoom, pan, and click markers. It shows S11/S22 on a Smith chart plus S21, VSWR, and return loss.

## Use the fleet.txt example

Run the supplied five-network ANT6 topology without optimization:

```powershell
bpm-tuner --cascade --output outputs_cascade
```

Run all five optimization styles with a small development sweep:

```powershell
bpm-tuner --output outputs --candidates 2
```

For a more granular production study, increase `--candidates`. The optimizer evaluates the full Cartesian product of the sampled real components, so runtime and result count grow multiplicatively with every tunable port. Every candidate is a measured component from `Capacitors_BOM` or `Inductors_BOM`; Rust performs the parallel S-matrix termination sweep and deterministic target-aware ranking.

Limit the command-line optimization to specific nominal component windows when needed:

```powershell
bpm-tuner --candidates 7 --inductor-range 0.1 10 --capacitor-range 0.1 100
```

Saved JSON configurations use `inductor_min_nh`, `inductor_max_nh`, `capacitor_min_pf`, and `capacitor_max_pf`. Older configurations without these fields continue to use the complete measured catalogs.

The five result strategies are `minimum_bom`, `balanced`, `minimum_target`, `smith_contour`, and `minimum_insertion_loss`. Each winner receives an independent `1.00/0.95/1.05` component-value tolerance sweep before the Principal Engineer applies the normalized production-risk score documented in `Requirements.md`.

The Rust sweep rejects a result when an evaluated signal reflection or transmission magnitude exceeds the passive limit. This prevents a numerically active point outside the Smith circle from winning merely because it is close to a near-edge Smith target. The exhaustive traversal shares every evaluated termination prefix across its descendants and reduces the five strategy winners in Rust, so production optimization does not materialize one Python object per combination. If several agents produce the same lowest risk, the Principal Engineer keeps the declared reference agent order; identical results are not forced to look different.

Outputs include one JSON configuration and one combined PNG per agent, an agent comparison PNG, the final decision PNG, and `report.md`.

## Validation

```powershell
python -m pytest -q --basetemp .pytest_tmp
cargo test --manifest-path rust_optimizer\Cargo.toml
```

## Engineering caveat

The reported ±5% value is an electrical sensitivity proxy. It converts each measured shunt termination reflection to impedance, independently scales inductor impedance by the value factor or divides capacitor impedance by it, and converts the result back to reflection coefficient. It supports consistent solution ranking, but it is not vendor tolerance or full production Monte Carlo evidence. Validate the selected network against component tolerance distributions, PCB stack-up and geometry, temperature, bias, connector/fixture uncertainty, and measured production samples before release.

See [requirements traceability](docs/requirements-traceability.md) for implementation coverage and known limitations.
