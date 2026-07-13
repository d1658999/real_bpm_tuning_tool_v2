# Fleet Requirements Implementation

This document describes the implementation of the **Fleet Requirements** feature, which ensures that agent optimization results are saved to a dedicated timestamped folder.

## Feature Description

As specified in `Requirements.md`:
> After `Run optimization`, the results from agents SHALL be saved to `.json` and `.png` files like example folder `outputs_port_target_optimization`. The result folder name is `Fleet_results_YYYYMMDD_HHMMSS`.

## Technical Architecture

The feature is integrated into the core `FleetOptimizer.run` workflow:
1. When optimization is run (from either the CLI or GUI), the optimizer calculates optimal matching network solutions for all 5 strategies/agents.
2. At the end of `FleetOptimizer.run`, it generates a timestamp string formatted as `%Y%m%d_%H%M%S`.
3. It creates a dynamic result folder under the configured output root named `Fleet_results_YYYYMMDD_HHMMSS`.
4. It calls `export_optimization_report` to generate the complete suite of results:
   - `{strategy}.json` and `{strategy}.png` for all five strategies (`minimum_bom`, `balanced`, `minimum_target`, `smith_contour`, `minimum_insertion_loss`).
   - `agent_comparison.png` for a metrics comparison bar plot.
   - `final_decision.png` for the final selected agent plot with the dotted VSWR = 2 circle constraint.
   - `report.md` describing the selection decision, performance tables, and BOM details.

### Code Integration

The `saved_dir` attribute was added to the `OptimizationReport` dataclass:

```python
@dataclass
class OptimizationReport:
    agents: list[AgentResult]
    selected: AgentResult
    saved_dir: Path | None = None
```

Inside the `FleetOptimizer.run` method (in [optimizer.py](file:///C:/Users/d1658/Documents/project/real_bpm_tuning_tool_v2/bpm_tuner/optimizer.py)), the results are exported as follows:

```python
from datetime import datetime
from .exports import export_optimization_report

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
saved_dir = self.output_root / f"Fleet_results_{timestamp}"
report = OptimizationReport(agent_results, selected, saved_dir=saved_dir)
export_optimization_report(report, saved_dir)
```

## User Experience

### CLI
When running via the command line:
```bash
python -m bpm_tuner --output outputs --candidates 2
```
The console will print the status of the selected agent and state where the fleet results folder was saved:
```
Selected Senior_engineer_Agent_2 (balanced). Report: outputs
Fleet results saved to: C:\Users\d1658\Documents\project\real_bpm_tuning_tool_v2\Fleet_results_20260713_011444
```

### GUI
When running from the Qt Desktop Application:
1. Click **Run Optimization**.
2. Upon completion, the success message box displays the exact directory where the results are saved.

---

## Verification

An automated integration test has been added to [test_integration.py](file:///C:/Users/d1658/Documents/project/real_bpm_tuning_tool_v2/tests/test_integration.py):

```python
def test_fleet_optimizer_saves_results_to_timestamped_folder(tmp_path: Path) -> None:
    from bpm_tuner.optimizer import FleetOptimizer
    optimizer = FleetOptimizer(ROOT)
    optimizer.output_root = tmp_path

    config = default_fleet_config(ROOT)
    config.points = 11
    config.candidates_per_type = 2
    config.validate(allow_unselected=True)

    report = optimizer.run(config)
    assert report.saved_dir is not None
    assert report.saved_dir.parent == tmp_path
    assert report.saved_dir.exists()
    assert (report.saved_dir / "report.md").exists()
    assert (report.saved_dir / "agent_comparison.png").exists()
    assert (report.saved_dir / "final_decision.png").exists()

    for strategy in ("minimum_bom", "balanced", "minimum_target", "smith_contour", "minimum_insertion_loss"):
        assert (report.saved_dir / f"{strategy}.json").exists()
        assert (report.saved_dir / f"{strategy}.png").exists()
```

To run this verification suite:
```powershell
python -m pytest
```
