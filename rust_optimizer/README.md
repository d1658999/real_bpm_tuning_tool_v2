# BPM Ranking Optimizer

A dependency-free Rust CLI that ranks already-evaluated impedance-matching
candidates for the Python BPM tuning application. Rust performs the hot ranking
step; the Python GUI remains responsible for RF simulation, progress reporting,
and process cancellation.

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
candidate_id	bom_count	max_vswr	worst_il_db	smith_radius	target_distance
network-001	2	1.25	0.35	0.12	0.08
network-002	3	1.10	0.48	0.09	0.04
```

`worst_il_db` is the positive insertion-loss magnitude, so a lower number is
better. All four floating-point metrics must be finite and non-negative;
candidate IDs must be non-empty and unique. On success stdout contains only the
winning `candidate_id`. Invalid arguments, files, rows, or metrics produce a
clear error on stderr and a non-zero exit code.

Strategies:

- `minimum_bom`: among candidates with `max_vswr <= 1.4`, minimize BOM count;
  if none pass, return the candidate with the lowest VSWR.
- `balanced`: normalize each metric over the candidate set, then weight VSWR
  40%, insertion loss 35%, Smith radius 10%, target distance 10%, and BOM 5%.
- `lowest_vswr`: minimize worst-case VSWR.
- `tightest_contour`: minimize Smith-chart contour radius, then target distance.
- `lowest_insertion_loss`: minimize worst-case insertion-loss magnitude.

All ties are resolved by documented secondary metrics and finally by lexical
`candidate_id`, making repeated runs deterministic.

