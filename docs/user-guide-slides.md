# Real BPM Tuning Tool user-guide slides

## Deliverable

`Real_BPM_Tuning_Tool_User_Guide.pptx` is a 16:9 PowerPoint guide for engineers who need to configure, simulate, optimize, review, and export an RF tuning project through the desktop GUI.

The deck uses the visual direction in `DESIGN-apple.md` and current behavior from `Requirements.md`, `README.md`, and `bpm_tuner/gui.py`. Its screenshots use the non-confidential fixture `snp_files/Cubs_RJF_DK2p55.s4p`. Slide 10 contains a clearly labeled example optimization plot from an existing repo-local Fleet results folder.

## Slide map

1. Tool purpose and scope
2. Six-step user workflow
3. Windows EXE and developer launch paths
4. Three-panel interface tour
5. Port configuration modes and validation rules
6. Frequency bands and Smith targets
7. BOM samples/type, per-port ranges, and search-size growth
8. Cascade workflow and plot interpretation
9. Five optimization strategies, progress, and cancellation
10. Fleet-winner metrics and the production-tolerance caveat
11. Save/load and export deliverables
12. Preflight checklist and common warnings

## Use the deck

- Present it in order for onboarding.
- For experienced users, start at slide 5 and use slides 7, 10, and 12 as the key RF and production checkpoints.
- Replace the example result on slide 10 with a project-specific `final_decision.png` when presenting a real design review.
- Keep the ±5% statement intact: it is an electrical sensitivity proxy, not measured yield or process capability.

## Regenerate the PowerPoint

The generator uses Python only and writes the PowerPoint plus two current GUI screenshots under `docs/assets/user-guide`.

```powershell
.\.venv\Scripts\python.exe -m pip install python-pptx
.\.venv\Scripts\python.exe docs\build_user_guide_ppt.py
```

Expected output:

```text
docs\Real_BPM_Tuning_Tool_User_Guide.pptx
docs\assets\user-guide\bpm_tuner_configured.png
docs\assets\user-guide\bpm_tuner_cascade_result.png
```

The GUI screenshots are captured with `QT_QPA_PLATFORM=offscreen`, so regenerating the deck does not require interacting with the desktop window.

## First-run guidance reflected in the slides

1. Add the required Touchstone files.
2. Assign every active port and make all `connect` relationships reciprocal.
3. Assign unique, consecutive signals from `s1`; the highest-numbered signal is the dependent antenna/common port.
4. Set frequency limits or leave both values on `Auto`. Enable Smith targets only for driven signals.
5. Set **BOM samples/type** to `2` for the first optimization, then increase it only after the topology and bands are correct.
6. Run **Cascade** and inspect Smith, S21, VSWR, and return-loss plots.
7. Run **Optimization**, monitor progress, and cancel if the search is too large.
8. Review all five strategies and the Principal Engineer's production-risk selection.
9. Save the JSON configuration and export the required SNP, CSV, plots, and report.

## Search-size reference

For `N = BOM samples/type`, a tunable port contributes:

| Mode | Choices at that port |
| --- | ---: |
| `inductor` | `N` |
| `capacitor` | `N` |
| `inductor/capacitor` | `2N` |
| `open/inductor/capacitor` | `2N + 1` |

The total exhaustive combination count is the product of the choices at every tunable port. For example, three `open/inductor/capacitor` ports with `N = 2` produce `(2 × 2 + 1)^3 = 125` combinations.

## Deployment reminder

When distributing the one-file Windows build, keep this layout and move the three items together:

```text
dist\
  BPMTuningTool.exe
  Capacitors_BOM\
  Inductors_BOM\
```

The two measured BOM folder names must remain unchanged.
