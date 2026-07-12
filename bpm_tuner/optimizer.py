from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .bom import BOMComponent, evenly_spaced, load_bom
from .circuit import CircuitEngine, SimulationResult
from .metrics import PerformanceMetrics, assign_production_risk
from .models import ConnectionType, PortConfig, ProjectConfig
from .rust_bridge import CandidateScore, RustOptimizer


STRATEGIES = (
    "minimum_bom",
    "balanced",
    "lowest_vswr",
    "tightest_contour",
    "lowest_insertion_loss",
)

AGENT_NAMES = {
    "minimum_bom": "Senior_engineer_Agent_1",
    "balanced": "Senior_engineer_Agent_2",
    "lowest_vswr": "Senior_engineer_Agent_3",
    "tightest_contour": "Senior_engineer_Agent_4",
    "lowest_insertion_loss": "Senior_engineer_Agent_5",
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

    @property
    def result(self) -> SimulationResult:
        return self.selected.result


class FleetOptimizer:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.engine = CircuitEngine(self.root)
        self.ranker = RustOptimizer(self.root)
        self.bom = load_bom(self.root)

    @staticmethod
    def _slots(config: ProjectConfig) -> list[tuple[int, int]]:
        tunable = {
            ConnectionType.INDUCTOR,
            ConnectionType.CAPACITOR,
            ConnectionType.INDUCTOR_CAPACITOR,
            ConnectionType.OPEN_INDUCTOR_CAPACITOR,
        }
        return [
            (network_index, port_index)
            for network_index, network in enumerate(config.networks)
            for port_index, port in enumerate(network.ports)
            if port.mode in tunable
        ]

    def _options(self, port: PortConfig, count: int) -> list[BOMComponent | None]:
        result: list[BOMComponent | None] = []
        if port.mode == ConnectionType.OPEN_INDUCTOR_CAPACITOR:
            result.append(None)
        if port.mode in (ConnectionType.INDUCTOR, ConnectionType.INDUCTOR_CAPACITOR, ConnectionType.OPEN_INDUCTOR_CAPACITOR):
            result.extend(evenly_spaced(self.bom["inductor"], count))
        if port.mode in (ConnectionType.CAPACITOR, ConnectionType.INDUCTOR_CAPACITOR, ConnectionType.OPEN_INDUCTOR_CAPACITOR):
            result.extend(evenly_spaced(self.bom["capacitor"], count))
        return result

    def _initial_config(self, source: ProjectConfig) -> ProjectConfig:
        config = deepcopy(source)
        for network_index, port_index in self._slots(config):
            port = config.networks[network_index].ports[port_index]
            options = self._options(port, config.candidates_per_type)
            if not options:
                raise ValueError("The real BOM folders contain no compatible Touchstone components.")
            selected = options[0]
            port.component_path = str(selected.path.relative_to(self.root)) if selected else None
        return config

    def run(
        self,
        config: ProjectConfig,
        progress_callback: Callable[[int, str], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> OptimizationReport:
        config.validate(allow_unselected=True)
        self.ranker.ensure_built()
        slots = self._slots(config)
        option_counts = [len(self._options(config.networks[n].ports[p], config.candidates_per_type)) for n, p in slots]
        total = max(1, len(STRATEGIES) * config.optimization_passes * sum(option_counts))
        completed = 0
        agent_results: list[AgentResult] = []

        for strategy in STRATEGIES:
            working = self._initial_config(config)
            for pass_index in range(config.optimization_passes):
                for network_index, port_index in slots:
                    port = working.networks[network_index].ports[port_index]
                    evaluated: list[tuple[str, str | None, SimulationResult]] = []
                    for option_index, option in enumerate(self._options(port, config.candidates_per_type)):
                        if cancel_callback and cancel_callback():
                            raise OptimizationCancelled("Optimization was cancelled by the user.")
                        candidate = deepcopy(working)
                        selected_path = str(option.path.relative_to(self.root)) if option else None
                        candidate.networks[network_index].ports[port_index].component_path = selected_path
                        simulation = self.engine.run(candidate, validate=False)
                        candidate_id = f"candidate-{option_index:04d}"
                        evaluated.append((candidate_id, selected_path, simulation))
                        completed += 1
                        if progress_callback:
                            progress_callback(
                                min(99, round(100 * completed / total)),
                                f"{AGENT_NAMES[strategy]}: pass {pass_index + 1}, port {port.port}",
                            )
                    scores = [
                        CandidateScore(
                            candidate_id=item[0],
                            bom_count=item[2].metrics.component_count,
                            max_vswr=item[2].metrics.worst_vswr,
                            worst_il_db=item[2].metrics.worst_il_db,
                            smith_radius=item[2].metrics.smith_contour,
                            target_distance=item[2].metrics.target_distance,
                        )
                        for item in evaluated
                    ]
                    winner = self.ranker.rank(strategy, scores)
                    chosen = next(item for item in evaluated if item[0] == winner)
                    working.networks[network_index].ports[port_index].component_path = chosen[1]

            nominal = self.engine.run(working, validate=False)
            tolerance_runs = [
                self.engine.run(working, component_scale=scale, validate=False) for scale in (0.95, 1.05)
            ]
            tolerance_vswr = max(run.metrics.worst_vswr for run in tolerance_runs)
            tolerance_il = max(run.metrics.worst_il_db for run in tolerance_runs)
            metrics = replace(
                nominal.metrics,
                tolerance_vswr=tolerance_vswr,
                tolerance_il_db=tolerance_il,
                vswr_sensitivity=max(0.0, tolerance_vswr - nominal.metrics.worst_vswr),
            )
            nominal.metrics = metrics
            agent_results.append(AgentResult(strategy, AGENT_NAMES[strategy], nominal, metrics))

        assign_production_risk(agent_results)
        for item in agent_results:
            item.result.metrics = item.metrics
        selected = min(
            agent_results,
            key=lambda item: (item.metrics.production_risk, item.metrics.tolerance_vswr or float("inf")),
        )
        if progress_callback:
            progress_callback(100, f"Principal_engineer_Agent selected {selected.agent_name}")
        return OptimizationReport(agent_results, selected)


# Compatibility name used by earlier GUI implementations.
OptimizationRunner = FleetOptimizer
