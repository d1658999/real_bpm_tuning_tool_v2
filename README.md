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
2. Assign every active port in the middle panel. Connections must be reciprocal, and signal names must be unique and consecutive from `s1` (two to four total). A file that is not yet part of the circuit may remain in the project when every one of its ports is `open`; it is ignored by cascade and optimization.
3. Leave `open/inductor/capacitor` unselected to let optimization choose from the measured BOM. Smith targets are off by default and appear on each driven signal row: `s1` for a two-port result, `s1`/`s2` for three ports, and `s1`/`s2`/`s3` for four ports. The final signal is the dependent antenna port. Enable targets individually and enter physical resistance/reactance in ohms—for example, `50 + j0 Ω` targets the center of a 50-ohm Smith chart.
4. Use **Run Cascade** to simulate the selected configuration or **Run Optimization** to run all five strategies. Optimization progress and cancellation are shown at the top.
5. Save/load JSON configurations, export the cascaded Touchstone network and S21 CSV, or save the combined plot.

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

The five result strategies are `minimum_bom`, `balanced`, `minimum_target`, `smith_contour`, and `minimum_insertion_loss`. Each winner receives an independent `1.00/0.95/1.05` component-value tolerance sweep before the Principal Engineer applies the normalized production-risk score documented in `Requirements.md`.

Outputs include one JSON configuration and one combined PNG per agent, an agent comparison PNG, the final decision PNG, and `report.md`.

## Validation

```powershell
python -m pytest -q --basetemp .pytest_tmp
cargo test --manifest-path rust_optimizer\Cargo.toml
```

## Engineering caveat

The reported ±5% value is an electrical sensitivity proxy based on scaling the measured component network away from an ideal thru. It supports consistent solution ranking, but it is not vendor tolerance or full production Monte Carlo evidence. Validate the selected network against component tolerance distributions, PCB stack-up and geometry, temperature, bias, connector/fixture uncertainty, and measured production samples before release.

See [requirements traceability](docs/requirements-traceability.md) for implementation coverage and known limitations.
