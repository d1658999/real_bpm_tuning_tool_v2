from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from math import prod
from pathlib import Path
from typing import Callable

import numpy as np
import skrf as rf

from .bom import BOMComponent, evenly_spaced, load_bom
from .circuit import CircuitEngine, SimulationResult
from .metrics import PerformanceMetrics, assign_production_risk
from .models import ConnectionType, PortConfig, ProjectConfig
from .rust_bridge import CandidateScore, RustKernelCancelled, RustOptimizer, SweepScore


STRATEGIES = (
    "minimum_bom",
    "balanced",
    "minimum_target",
    "smith_contour",
    "minimum_insertion_loss",
)

AGENT_NAMES = {
    "minimum_bom": "Senior_engineer_Agent_1",
    "balanced": "Senior_engineer_Agent_2",
    "minimum_target": "Senior_engineer_Agent_3",
    "smith_contour": "Senior_engineer_Agent_4",
    "minimum_insertion_loss": "Senior_engineer_Agent_5",
}


class OptimizationCancelled(RuntimeError):
    pass


@dataclass
class AgentResult:
    strategy: str
    agent_name: str
    result: SimulationResult
    metrics: PerformanceMetrics

    @property
    def config(self) -> ProjectConfig:
        return self.result.config


@dataclass
class OptimizationReport:
    agents: list[AgentResult]
    selected: AgentResult
    saved_dir: Path | None = None

    @property
    def result(self) -> SimulationResult:
        return self.selected.result


@dataclass(frozen=True)
class _EvaluatedCombination:
    candidate_id: str
    combination: tuple[int, ...]
    metrics: PerformanceMetrics


class FleetOptimizer:
    """Reference-style exhaustive BOM optimizer with Rust RF sweeping."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.output_root = self.root
        self.engine = CircuitEngine(self.root)
        self.ranker = RustOptimizer(self.root)
        self.bom = load_bom(self.root)

    @staticmethod
    def _slots(config: ProjectConfig) -> list[tuple[int, int]]:
        return [
            (network_index, port_index)
            for network_index, network in enumerate(config.networks)
            for port_index, port in enumerate(network.ports)
            if port.mode
            in {
                ConnectionType.INDUCTOR_CAPACITOR,
                ConnectionType.OPEN_INDUCTOR_CAPACITOR,
            }
            or (
                port.mode in {ConnectionType.INDUCTOR, ConnectionType.CAPACITOR}
                and not port.component_path
            )
        ]

    def _options(self, port: PortConfig, candidate_count: int) -> list[BOMComponent | None]:
        result: list[BOMComponent | None] = []
        if port.mode == ConnectionType.OPEN_INDUCTOR_CAPACITOR:
            result.append(None)
        if port.mode in (
            ConnectionType.INDUCTOR,
            ConnectionType.INDUCTOR_CAPACITOR,
            ConnectionType.OPEN_INDUCTOR_CAPACITOR,
        ):
            result.extend(evenly_spaced(self.bom["inductor"], candidate_count))
        if port.mode in (
            ConnectionType.CAPACITOR,
            ConnectionType.INDUCTOR_CAPACITOR,
            ConnectionType.OPEN_INDUCTOR_CAPACITOR,
        ):
            result.extend(evenly_spaced(self.bom["capacitor"], candidate_count))
        if not result:
            raise ValueError(f"No real BOM candidates are available for {port.mode.value}.")
        return result

    def _build_sweep_base(
        self, config: ProjectConfig, slots: list[tuple[int, int]]
    ) -> rf.Network:
        exposed = deepcopy(config)
        for index, (network_index, port_index) in enumerate(slots):
            port = exposed.networks[network_index].ports[port_index]
            port.mode = ConnectionType.SIGNAL
            port.signal = f"tune{index + 1:04d}"
            port.component_path = None
            port.start_ghz = None
            port.stop_ghz = None
            port.smith_target_enabled = False
        return self.engine.run(exposed, validate=False).network

    @staticmethod
    def _evaluation_ranges(config: ProjectConfig, frequency_hz: np.ndarray) -> list[tuple[int, int]]:
        signal_ports = config.signal_ports()
        non_antenna_ranges: list[tuple[int, int]] = []
        union = np.zeros(len(frequency_hz), dtype=bool)
        frequency_ghz = frequency_hz / 1e9
        for port in signal_ports[:-1]:
            if port.start_ghz is None or port.stop_ghz is None:
                mask = np.ones(len(frequency_ghz), dtype=bool)
            else:
                mask = (frequency_ghz >= port.start_ghz) & (frequency_ghz <= port.stop_ghz)
            indexes = np.flatnonzero(mask)
            if not len(indexes):
                raise ValueError(f"{port.signal} frequency range contains no optimization points.")
            non_antenna_ranges.append((int(indexes[0]), int(indexes[-1])))
            union |= mask
        antenna_indexes = np.flatnonzero(union)
        if not len(antenna_indexes):
            raise ValueError("The dependent antenna port has no optimization frequency points.")
        return [
            *non_antenna_ranges,
            (int(antenna_indexes[0]), int(antenna_indexes[-1])),
        ]

    @staticmethod
    def _target_gamma(config: ProjectConfig, nfreq: int) -> np.ndarray:
        signal_ports = config.signal_ports()
        target_specs = config.smith_targets_by_signal()
        result = np.zeros((len(signal_ports), nfreq), dtype=np.complex128)
        for index, port in enumerate(signal_ports):
            specification = target_specs.get(port.signal or "")
            if specification is not None:
                result[index, :] = specification[1]
        return result

    def _component_gamma(self, component: BOMComponent, frequency: rf.Frequency) -> np.ndarray:
        network = rf.Network(str(component.path)).interpolate(frequency)
        if network.nports != 2:
            raise ValueError(f"BOM component must be a two-port Touchstone model: {component.path}")
        load = -1.0 + 0.0j
        denominator = 1.0 - network.s[:, 1, 1] * load
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            gamma = network.s[:, 0, 0] + (
                network.s[:, 0, 1] * load * network.s[:, 1, 0] / denominator
            )
        gamma = np.asarray(gamma, dtype=np.complex128)
        invalid = ~np.isfinite(gamma.real) | ~np.isfinite(gamma.imag)
        gamma[invalid] = 1e6 + 0.0j
        return gamma

    @staticmethod
    def _scale_component_gamma(
        gamma: np.ndarray, kind: str, value_factor: float, reference_ohm: float = 50.0
    ) -> np.ndarray:
        """Apply the reference impedance-domain L/C value-tolerance model."""
        if value_factor == 1.0:
            return gamma.copy()
        denominator = 1.0 - gamma
        near_open = np.abs(denominator) < 1e-12
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            nominal_impedance = reference_ohm * (1.0 + gamma) / denominator
            varied_impedance = (
                nominal_impedance / value_factor
                if kind == "capacitor"
                else nominal_impedance * value_factor
            )
            varied_gamma = (varied_impedance - reference_ohm) / (
                varied_impedance + reference_ohm
            )
        varied_gamma = np.asarray(varied_gamma, dtype=np.complex128)
        varied_gamma[near_open] = gamma[near_open]
        invalid = ~np.isfinite(varied_gamma.real) | ~np.isfinite(varied_gamma.imag)
        varied_gamma[invalid] = gamma[invalid]
        return varied_gamma

    def _termination_matrices(
        self,
        options_per_slot: list[list[BOMComponent | None]],
        frequency: rf.Frequency,
    ) -> tuple[list[np.ndarray], dict[Path, np.ndarray]]:
        gamma_cache: dict[Path, np.ndarray] = {}
        matrices: list[np.ndarray] = []
        for options in options_per_slot:
            rows = []
            for option in options:
                if option is None:
                    rows.append(np.ones(len(frequency), dtype=np.complex128))
                else:
                    if option.path not in gamma_cache:
                        gamma_cache[option.path] = self._component_gamma(option, frequency)
                    rows.append(gamma_cache[option.path])
            matrices.append(np.vstack(rows))
        return matrices, gamma_cache

    @staticmethod
    def _metrics_from_sweep(score: SweepScore, component_count: int) -> PerformanceMetrics:
        target_max = score.target_max
        target_spread = score.target_spread
        return PerformanceMetrics(
            max_vswr_s11=score.vswr_non_ant,
            max_vswr_s22=score.vswr_ant,
            worst_vswr=max(score.vswr_non_ant, score.vswr_ant),
            worst_il_db=score.worst_il_db,
            smith_radius=target_max,
            smith_contour=target_spread + target_max,
            target_distance=target_max,
            target_error_s11_max=score.target_non_ant,
            target_error_s22_max=score.target_ant,
            target_error_spread=target_spread,
            vswr_spread=abs(score.vswr_non_ant - score.vswr_ant),
            component_count=component_count,
        )

    @staticmethod
    def _ranking_score(candidate: _EvaluatedCombination) -> CandidateScore:
        metrics = candidate.metrics
        return CandidateScore(
            candidate_id=candidate.candidate_id,
            bom_count=metrics.component_count,
            vswr_non_ant=metrics.max_vswr_s11,
            vswr_ant=metrics.max_vswr_s22,
            worst_il_db=metrics.worst_il_db,
            smith_score=metrics.smith_contour,
            target_non_ant=metrics.target_error_s11_max,
            target_ant=metrics.target_error_s22_max,
            target_spread=metrics.target_error_spread,
        )

    def _config_for_combination(
        self,
        source: ProjectConfig,
        slots: list[tuple[int, int]],
        options_per_slot: list[list[BOMComponent | None]],
        combination: tuple[int, ...],
    ) -> ProjectConfig:
        config = deepcopy(source)
        for (network_index, port_index), options, selected_index in zip(
            slots, options_per_slot, combination, strict=True
        ):
            selected = options[selected_index]
            config.networks[network_index].ports[port_index].component_path = (
                str(selected.path.relative_to(self.root)) if selected is not None else None
            )
        return config

    def _tolerance_metrics(
        self,
        nominal: PerformanceMetrics,
        combination: tuple[int, ...],
        options_per_slot: list[list[BOMComponent | None]],
        gamma_cache: dict[Path, np.ndarray],
        base_s: np.ndarray,
        evaluation_ranges: list[tuple[int, int]],
        target_gamma: np.ndarray,
        cancel_callback: Callable[[], bool] | None,
    ) -> PerformanceMetrics:
        tolerance_matrices: list[np.ndarray] = []
        for options, selected_index in zip(options_per_slot, combination, strict=True):
            selected = options[selected_index]
            if selected is None:
                tolerance_matrices.append(np.ones((1, base_s.shape[0]), dtype=np.complex128))
                continue
            nominal_gamma = gamma_cache[selected.path]
            tolerance_matrices.append(
                np.vstack(
                    [
                        self._scale_component_gamma(nominal_gamma, selected.kind, factor)
                        for factor in (1.0, 0.95, 1.05)
                    ]
                )
            )
        tolerance_scores = self.ranker.sweep(
            base_s,
            tolerance_matrices,
            evaluation_ranges,
            target_gamma,
            cancel_callback,
        )
        tolerance_vswr = max(
            max(score.vswr_non_ant, score.vswr_ant) for score in tolerance_scores
        )
        return replace(
            nominal,
            tolerance_vswr=tolerance_vswr,
            tolerance_il_db=max(score.worst_il_db for score in tolerance_scores),
            target_error_5pct_max=max(score.target_max for score in tolerance_scores),
            vswr_sensitivity=max(0.0, tolerance_vswr - nominal.worst_vswr),
        )

    @staticmethod
    def _check_cancelled(cancel_callback: Callable[[], bool] | None) -> None:
        if cancel_callback and cancel_callback():
            raise OptimizationCancelled("Optimization was cancelled by the user.")

    def run(
        self,
        config: ProjectConfig,
        progress_callback: Callable[[int, str], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> OptimizationReport:
        config.validate(allow_unselected=True)
        self.ranker.ensure_built()
        slots = self._slots(config)
        options_per_slot = [
            self._options(
                config.networks[network_index].ports[port_index],
                config.candidates_per_type,
            )
            for network_index, port_index in slots
        ]
        total_combinations = prod(len(options) for options in options_per_slot)
        if progress_callback:
            progress_callback(2, f"Building Rust sweep for {total_combinations:,} BOM combinations")
        self._check_cancelled(cancel_callback)

        base_network = self._build_sweep_base(config, slots)
        nsignals = len(config.signal_ports())
        if base_network.nports != nsignals + len(slots):
            raise ValueError("Optimization base network did not expose every tunable port.")
        evaluation_ranges = self._evaluation_ranges(config, base_network.f)
        target_gamma = self._target_gamma(config, len(base_network.f))
        termination_matrices, gamma_cache = self._termination_matrices(
            options_per_slot, base_network.frequency
        )
        if progress_callback:
            progress_callback(8, "Rust is evaluating all target-aware BOM combinations")
        try:
            sweep_scores = self.ranker.sweep(
                base_network.s,
                termination_matrices,
                evaluation_ranges,
                target_gamma,
                cancel_callback,
            )
        except RustKernelCancelled as exc:
            raise OptimizationCancelled(str(exc)) from exc
        self._check_cancelled(cancel_callback)
        if not sweep_scores:
            raise ValueError("The Rust optimizer produced no valid BOM combinations.")
        if progress_callback:
            progress_callback(65, f"Rust evaluated {len(sweep_scores):,} BOM combinations")

        evaluated: list[_EvaluatedCombination] = []
        for index, score in enumerate(sweep_scores):
            component_count = sum(
                options[selected] is not None
                for options, selected in zip(options_per_slot, score.combination, strict=True)
            )
            evaluated.append(
                _EvaluatedCombination(
                    candidate_id=f"candidate-{index:08d}",
                    combination=score.combination,
                    metrics=self._metrics_from_sweep(score, component_count),
                )
            )
        candidate_scores = [self._ranking_score(candidate) for candidate in evaluated]
        by_id = {candidate.candidate_id: candidate for candidate in evaluated}
        winners = {
            strategy: by_id[self.ranker.rank(strategy, candidate_scores)] for strategy in STRATEGIES
        }

        agent_results: list[AgentResult] = []
        tolerance_cache: dict[tuple[int, ...], PerformanceMetrics] = {}
        for index, strategy in enumerate(STRATEGIES):
            self._check_cancelled(cancel_callback)
            winner = winners[strategy]
            winner_config = self._config_for_combination(
                config, slots, options_per_slot, winner.combination
            )
            nominal = self.engine.run(winner_config, validate=False)
            metrics = tolerance_cache.get(winner.combination)
            if metrics is None:
                try:
                    metrics = self._tolerance_metrics(
                        nominal.metrics,
                        winner.combination,
                        options_per_slot,
                        gamma_cache,
                        base_network.s,
                        evaluation_ranges,
                        target_gamma,
                        cancel_callback,
                    )
                except RustKernelCancelled as exc:
                    raise OptimizationCancelled(str(exc)) from exc
                tolerance_cache[winner.combination] = metrics
            nominal.metrics = metrics
            agent_results.append(
                AgentResult(strategy, AGENT_NAMES[strategy], nominal, metrics)
            )
            if progress_callback:
                progress_callback(
                    70 + round(25 * (index + 1) / len(STRATEGIES)),
                    f"{AGENT_NAMES[strategy]} completed independent +/-5% tolerance analysis",
                )

        assign_production_risk(agent_results)
        for item in agent_results:
            item.result.metrics = item.metrics
        selected = min(
            agent_results,
            key=lambda item: (
                item.metrics.production_risk,
                item.metrics.target_error_5pct_max
                if item.metrics.target_error_5pct_max is not None
                else item.metrics.target_distance,
                item.strategy,
            ),
        )
        if progress_callback:
            progress_callback(100, f"Principal_engineer_Agent selected {selected.agent_name}")

        from datetime import datetime

        from .exports import export_optimization_report

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_dir = self.output_root / f"Fleet_results_{timestamp}"
        report = OptimizationReport(agent_results, selected, saved_dir=saved_dir)
        export_optimization_report(report, saved_dir)
        return report


# Compatibility name used by earlier GUI implementations.
OptimizationRunner = FleetOptimizer
