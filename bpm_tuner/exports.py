from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .circuit import SimulationResult
from .bom import component_from_path
from .optimizer import AgentResult, OptimizationReport


def _simulation(value: object) -> SimulationResult:
    if isinstance(value, SimulationResult):
        return value
    result = getattr(value, "result", None)
    if isinstance(result, SimulationResult):
        return result
    raise TypeError("Expected a SimulationResult or AgentResult.")


def export_touchstone(result: object, destination: str | Path) -> Path:
    simulation = _simulation(result)
    destination = Path(destination)
    if destination.suffix.lower().startswith(".s"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        base = destination.with_suffix("")
    else:
        destination.mkdir(parents=True, exist_ok=True)
        base = destination / simulation.network.name
    simulation.network.write_touchstone(str(base), return_string=False)
    return base.with_suffix(f".s{simulation.network.nports}p")


def export_il_csv(result: object, destination: str | Path) -> Path:
    simulation = _simulation(result)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    s21 = simulation.network.s[:, 1, 0]
    il = -20.0 * np.log10(np.maximum(np.abs(s21), 1e-15))
    with destination.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frequency_ghz", "insertion_loss_s21_db"])
        writer.writerows(zip(simulation.network.f / 1e9, il, strict=True))
    return destination


def _plot_result(result: SimulationResult, destination: Path, *, final: bool = False) -> Path:
    network = result.network
    frequency = network.f / 1e9
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    for index in range(network.nports):
        network.plot_s_smith(m=index, n=index, ax=axes[0, 0], label=f"S{index + 1}{index + 1}")
    axes[0, 0].set_title("Smith chart")
    for signal_name, (impedance, target) in result.config.smith_targets_by_signal().items():
        axes[0, 0].plot(
            [target.real],
            [target.imag],
            marker="*",
            markersize=11,
            color="#0066cc",
            markeredgecolor="white",
            label=f"{signal_name} target {impedance.real:g}{impedance.imag:+g}j Ω",
        )
        axes[0, 0].legend()
    if final:
        angle = np.linspace(0, 2 * np.pi, 361)
        axes[0, 0].plot(
            np.cos(angle) / 3,
            np.sin(angle) / 3,
            linestyle=":",
            color="#7a7a7a",
            label="VSWR = 2",
        )
        axes[0, 0].legend()
    s21_db = 20 * np.log10(np.maximum(np.abs(network.s[:, 1, 0]), 1e-15))
    axes[0, 1].plot(frequency, s21_db, color="#0066cc")
    axes[0, 1].set(title="Insertion loss (S21)", xlabel="Frequency (GHz)", ylabel="S21 (dB)")
    for index, label in ((0, "S11"), (1, "S22")):
        gamma = np.clip(np.abs(network.s[:, index, index]), 0, 0.999999)
        axes[1, 0].plot(frequency, (1 + gamma) / (1 - gamma), label=label)
        axes[1, 1].plot(frequency, -20 * np.log10(np.maximum(gamma, 1e-15)), label=label)
    axes[1, 0].axhline(2.0, linestyle=":" if final else "--", color="#7a7a7a", label="VSWR = 2")
    axes[1, 0].set(title="VSWR", xlabel="Frequency (GHz)", ylabel="VSWR")
    axes[1, 1].set(title="Return loss", xlabel="Frequency (GHz)", ylabel="Return loss (dB)")
    for ax in axes.flat[1:]:
        ax.grid(True, color="#e0e0e0")
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=160)
    plt.close(fig)
    return destination


def save_combined_figure(result: object, destination: str | Path) -> Path:
    return _plot_result(_simulation(result), Path(destination))


def export_optimization_report(report: OptimizationReport, destination: str | Path) -> Path:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for agent in report.agents:
        slug = agent.strategy
        payload = agent.config.to_dict()
        payload["agent"] = agent.agent_name
        payload["strategy"] = agent.strategy
        payload["metrics"] = asdict(agent.metrics)
        payload["metrics"].update(
            {
                "target_error_max": agent.metrics.target_distance,
                "worst_il_5pct_db": (
                    agent.metrics.tolerance_il_db
                    if agent.metrics.tolerance_il_db is not None
                    else agent.metrics.worst_il_db
                ),
                "vswr_5pct_max": (
                    agent.metrics.tolerance_vswr
                    if agent.metrics.tolerance_vswr is not None
                    else agent.metrics.worst_vswr
                ),
                "risk_score": agent.metrics.production_risk,
            }
        )
        import json

        (destination / f"{slug}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _plot_result(agent.result, destination / f"{slug}.png")

    labels = [agent.strategy.replace("_", "\n") for agent in report.agents]
    metrics = [
        ("VSWR S11 max", [a.metrics.max_vswr_s11 for a in report.agents]),
        ("VSWR S22 max", [a.metrics.max_vswr_s22 for a in report.agents]),
        ("Worst IL (dB)", [a.metrics.worst_il_db for a in report.agents]),
        ("Components", [a.metrics.component_count for a in report.agents]),
        ("VSWR ±5%", [a.metrics.tolerance_vswr or a.metrics.worst_vswr for a in report.agents]),
        ("Production risk", [a.metrics.production_risk for a in report.agents]),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(16, 9), constrained_layout=True)
    for ax, (title, values) in zip(axes.flat, metrics, strict=True):
        ax.bar(labels, values, color="#0066cc")
        ax.set_title(title)
        ax.tick_params(axis="x", labelsize=8)
        ax.grid(axis="y", color="#e0e0e0")
    fig.savefig(destination / "agent_comparison.png", dpi=160)
    plt.close(fig)
    _plot_result(report.selected.result, destination / "final_decision.png", final=True)

    selected = report.selected
    lines = [
        "# BPM tuning optimization report",
        "",
        "## Principal engineer decision",
        "",
        f"Selected **{selected.agent_name}** (`{selected.strategy}`) with production-risk score "
        f"**{selected.metrics.production_risk:.4f}**.",
        "",
        "The decision minimizes the required target-aware normalized risk formula across all five "
        "engineering strategies, including ±5% target error, BOM count, VSWR sensitivity, insertion "
        "loss, and target-error spread.",
        "",
        "## Five-agent comparison",
        "",
        "| Agent | Strategy | VSWR S11 | VSWR S22 | Target error | Worst IL dB | BOM | "
        "VSWR ±5% | Target error ±5% | Risk |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for agent in report.agents:
        m = agent.metrics
        tolerance_vswr = m.tolerance_vswr if m.tolerance_vswr is not None else m.worst_vswr
        tolerance_target = (
            m.target_error_5pct_max
            if m.target_error_5pct_max not in (None, 0.0)
            else m.target_distance
        )
        lines.append(
            f"| {agent.agent_name} | {agent.strategy} | {m.max_vswr_s11:.3f} | {m.max_vswr_s22:.3f} | "
            f"{m.target_distance:.4f} | {m.worst_il_db:.3f} | {m.component_count} | "
            f"{tolerance_vswr:.3f} | {tolerance_target:.4f} | {m.production_risk:.4f} |"
        )
    lines += [
        "",
        "## Selected real BOM components",
        "",
        "| Network | Port | Type | Value | Murata part |",
        "|---|---:|---|---:|---|",
    ]
    selected_parts = 0
    for network in selected.config.networks:
        for port in network.ports:
            if not port.component_path:
                continue
            part = component_from_path(port.component_path)
            lines.append(
                f"| {Path(network.path).name} | {port.port} | {part.kind} | {part.display_value} | "
                f"{part.part_number} |"
            )
            selected_parts += 1
    if not selected_parts:
        lines.append("| - | - | open/short only | - | - |")
    lines += [
        "",
        "## Enabled Smith targets",
        "",
        "| Signal | Target impedance | Target Γ |",
        "|---|---:|---:|",
    ]
    selected_targets = selected.config.smith_targets_by_signal()
    for signal_name, (impedance, gamma) in selected_targets.items():
        lines.append(
            f"| {signal_name} | {impedance.real:g}{impedance.imag:+g}j Ω | "
            f"{gamma.real:.5f}{gamma.imag:+.5f}j |"
        )
    if not selected_targets:
        lines.append("| - | Disabled | - |")
    lines += [
        "",
        "## Production tolerance note",
        "",
        "The ±5% result is an electrical proxy that scales each measured component model's departure "
        "from an ideal thru. It is useful for ranking, but it is not a substitute for vendor tolerance, "
        "PCB variation, temperature, bias, and Monte Carlo validation before mass production.",
    ]
    report_path = destination / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
