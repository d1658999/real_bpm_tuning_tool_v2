from __future__ import annotations

import argparse
from pathlib import Path

from .circuit import CircuitEngine
from .defaults import default_fleet_config
from .exports import export_optimization_report, save_combined_figure
from .models import ProjectConfig
from .optimizer import FleetOptimizer


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="BPM multiport impedance tuning tool")
    result.add_argument("--gui", action="store_true", help="launch the PyQt5 desktop application")
    result.add_argument("--root", type=Path, default=Path.cwd(), help="project root containing BOM and SNP folders")
    result.add_argument("--config", type=Path, help="saved JSON configuration; default uses fleet.txt ANT6 topology")
    result.add_argument("--output", type=Path, default=Path("outputs"), help="optimization report directory")
    result.add_argument("--cascade", action="store_true", help="run only the configured cascade")
    result.add_argument("--candidates", type=int, default=None, help="real BOM candidates sampled per component type")
    result.add_argument(
        "--inductor-range",
        type=float,
        nargs=2,
        metavar=("MIN_NH", "MAX_NH"),
        help="inclusive inductor range in nH, applied before BOM sampling",
    )
    result.add_argument(
        "--capacitor-range",
        type=float,
        nargs=2,
        metavar=("MIN_PF", "MAX_PF"),
        help="inclusive capacitor range in pF, applied before BOM sampling",
    )
    result.add_argument(
        "--passes",
        type=int,
        default=None,
        help="deprecated compatibility option; the exhaustive optimizer ignores pass count",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.resolve()
    if args.gui:
        from .gui import main as gui_main

        return gui_main(root)
    config = ProjectConfig.load(args.config) if args.config else default_fleet_config(root)
    if args.candidates is not None:
        config.candidates_per_type = args.candidates
    if args.inductor_range is not None:
        config.inductor_min_nh, config.inductor_max_nh = args.inductor_range
    if args.capacitor_range is not None:
        config.capacitor_min_pf, config.capacitor_max_pf = args.capacitor_range
    if args.passes is not None:
        config.optimization_passes = args.passes
    if args.cascade:
        result = CircuitEngine(root).run(config)
        path = save_combined_figure(result, args.output / "cascade.png")
        print(f"Cascade complete: {path}")
        return 0

    def progress(percent: int, message: str) -> None:
        print(f"[{percent:3d}%] {message}", flush=True)

    report = FleetOptimizer(root).run(config, progress_callback=progress)
    path = export_optimization_report(report, args.output)
    print(f"Selected {report.selected.agent_name} ({report.selected.strategy}). Report: {path}")
    if getattr(report, "saved_dir", None):
        print(f"Fleet results saved to: {report.saved_dir}")
    return 0
