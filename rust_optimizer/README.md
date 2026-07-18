# BPM Exhaustive RF Optimizer

A dependency-free Rust CLI that performs the exhaustive S-matrix termination
sweep and ranks target-aware impedance-matching candidates for the Python BPM
tuning application. The sweep uses native worker threads and the rank-one port
termination equation adopted from `99_ reference/lib.rs`.

## Build and test

From this directory:

```powershell
cargo test
cargo build --release
```

The Windows executable is written to
`target\release\bpm-ranking-optimizer.exe`.

## CLI interface

```text
bpm-ranking-optimizer <strategy> <input.tsv>
```

The TSV may include this exact header (recommended) or contain data rows only:

```text
candidate_id	bom_count	vswr_non_ant	vswr_ant	worst_il_db	smith_score	target_non_ant	target_ant	target_spread
network-001	2	1.25	1.30	0.35	0.12	0.08	0.09	0.01
network-002	3	1.10	1.20	0.48	0.09	0.04	0.05	0.01
```

`worst_il_db` is the positive insertion-loss magnitude, so a lower number is
better. All floating-point metrics must be finite and non-negative;
candidate IDs must be non-empty and unique. On success stdout contains only the
winning `candidate_id`. Invalid arguments, files, rows, or metrics produce a
clear error on stderr and a non-zero exit code.

Canonical strategies:

- `minimum_bom`: keep candidates within 10% of the best non-antenna target
  error, then minimize BOM count.
- `balanced`: minimize normalized peak target error plus normalized insertion
  loss.
- `minimum_target`: minimize non-antenna target error, then antenna target error.
- `smith_contour`: minimize the target-centred Smith score.
- `minimum_insertion_loss`: apply the reference target gate, then minimize
  insertion-loss magnitude.

The Python bridge also invokes this binary as:

```text
bpm-ranking-optimizer sweep <input.bin> <output.bin>
```

The private, versioned little-endian bridge format carries the base complex
S-matrix, per-port termination gamma matrices, component-count metadata,
frequency evaluation ranges, and target gamma matrix. The compatibility mode
returns metrics and candidate indexes for the complete Cartesian product. Fleet
optimization uses winner mode: it still evaluates every combination exactly
once, but shares prefix S-matrices and retains only the five exact strategy
winners. This keeps memory bounded by the ranking frontiers instead of the
number of combinations. It is intentionally an internal interface; use
`bpm_tuner.rust_bridge.RustOptimizer.sweep` from Python.

All ties are resolved by documented secondary metrics and finally by lexical
`candidate_id`, making repeated runs deterministic.
