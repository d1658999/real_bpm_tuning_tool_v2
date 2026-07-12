from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import skrf as rf

from .metrics import PerformanceMetrics, network_metrics
from .models import ConfigError, ConnectionType, NetworkConfig, PortConfig, ProjectConfig


@dataclass
class SimulationResult:
    network: rf.Network
    metrics: PerformanceMetrics
    config: ProjectConfig
    signal_names: list[str]
    inactive_network_names: list[str]


class CircuitEngine:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate

    def inspect_network(self, path: str | Path) -> tuple[int, float, float]:
        network = rf.Network(str(self._resolve(path)))
        return network.nports, float(network.f[0] / 1e9), float(network.f[-1] / 1e9)

    def _frequency(self, config: ProjectConfig, originals: list[rf.Network]) -> rf.Frequency:
        available_start = max(float(n.f[0]) for n in originals) / 1e9
        available_stop = min(float(n.f[-1]) for n in originals) / 1e9
        configured_ranges = [
            (port.start_ghz, port.stop_ghz)
            for network in config.networks
            for port in network.ports
            if port.mode == ConnectionType.SIGNAL and port.start_ghz is not None and port.stop_ghz is not None
        ]
        start = (
            config.start_ghz
            if config.start_ghz is not None
            else min((value[0] for value in configured_ranges), default=available_start)
        )
        stop = (
            config.stop_ghz
            if config.stop_ghz is not None
            else max((value[1] for value in configured_ranges), default=available_stop)
        )
        if start < available_start or stop > available_stop:
            raise ConfigError(
                f"Requested {start:g}-{stop:g} GHz is outside the common Touchstone range "
                f"{available_start:g}-{available_stop:g} GHz. Check the frequency settings and try again."
            )
        return rf.Frequency(start, stop, config.points, unit="ghz")

    @staticmethod
    def _interpolate(network: rf.Network, frequency: rf.Frequency, name: str) -> rf.Network:
        result = network.interpolate(frequency)
        result.name = name
        return result

    def run(
        self,
        config: ProjectConfig,
        *,
        component_scale: float = 1.0,
        validate: bool = True,
    ) -> SimulationResult:
        if validate:
            config.validate()
        originals: dict[str, rf.Network] = {}
        for item in config.networks:
            path = self._resolve(item.path)
            if not path.exists():
                raise ConfigError(f"Touchstone file not found: {path}")
            originals[Path(item.path).name] = rf.Network(str(path))
        for item in config.networks:
            network = originals[Path(item.path).name]
            if len(item.ports) != network.nports:
                raise ConfigError(
                    f"{Path(item.path).name} has {network.nports} ports, but the configuration has "
                    f"{len(item.ports)}. Remove and add the file again."
                )

        active_names = config.active_network_names()
        active_items = [item for item in config.networks if Path(item.path).name in active_names]
        inactive_names = [Path(item.path).name for item in config.networks if Path(item.path).name not in active_names]
        if not active_items:
            raise ConfigError("No active signal circuit was found. Assign between two and four signal ports.")

        frequency = self._frequency(config, [originals[Path(item.path).name] for item in active_items])
        loaded: dict[str, rf.Network] = {}
        for index, item in enumerate(active_items):
            name = Path(item.path).name
            original = originals[name]
            loaded[name] = self._interpolate(original, frequency, f"dut_{index}_{name}")

        connections: list[list[tuple[rf.Network, int]]] = []
        consumed: set[tuple[str, int]] = set()
        signal_names: list[str] = []

        # Circuit output port ordering follows the order these nodes are declared.
        signal_ports: list[tuple[str, PortConfig]] = []
        for item in active_items:
            name = Path(item.path).name
            signal_ports.extend((name, p) for p in item.ports if p.mode == ConnectionType.SIGNAL)
        for name, port in sorted(signal_ports, key=lambda pair: pair[1].signal or ""):
            external = rf.Circuit.Port(frequency, name=port.signal or "signal", z0=50.0)
            connections.append([(external, 0), (loaded[name], port.port - 1)])
            consumed.add((name, port.port))
            signal_names.append(port.signal or "signal")

        for item in active_items:
            name = Path(item.path).name
            for port in item.ports:
                key = (name, port.port)
                if key in consumed:
                    continue
                if port.mode == ConnectionType.CONNECT:
                    target = Path(port.connect_network or "").name
                    target_key = (target, int(port.connect_port or 0))
                    connections.append(
                        [(loaded[name], port.port - 1), (loaded[target], int(port.connect_port) - 1)]
                    )
                    consumed.update((key, target_key))
                elif port.mode == ConnectionType.OPEN or (
                    port.mode == ConnectionType.OPEN_INDUCTOR_CAPACITOR and not port.component_path
                ):
                    termination = rf.Circuit.Open(frequency, name=f"open_{len(connections)}", z0=50.0)
                    connections.append([(loaded[name], port.port - 1), (termination, 0)])
                    consumed.add(key)
                elif port.mode == ConnectionType.SHORT:
                    termination = rf.Circuit.Ground(frequency, name=f"short_{len(connections)}", z0=50.0)
                    connections.append([(loaded[name], port.port - 1), (termination, 0)])
                    consumed.add(key)
                else:
                    if not port.component_path:
                        raise ConfigError(
                            f"{name} port {port.port}: no component is selected. Run optimization or choose a BOM part."
                        )
                    part_path = self._resolve(port.component_path)
                    if not part_path.exists():
                        raise ConfigError(f"BOM component not found: {part_path}")
                    part = rf.Network(str(part_path)).interpolate(frequency)
                    part.name = f"part_{len(connections)}_{part_path.stem}"
                    if component_scale != 1.0:
                        # Scale the measured departure from an ideal thru. This preserves the measured
                        # frequency dependence and is a conservative electrical ±5% proxy.
                        thru = np.zeros_like(part.s)
                        thru[:, 0, 1] = thru[:, 1, 0] = 1.0
                        part.s = thru + component_scale * (part.s - thru)
                    ground = rf.Circuit.Ground(frequency, name=f"ground_{len(connections)}", z0=50.0)
                    connections.append([(loaded[name], port.port - 1), (part, 0)])
                    connections.append([(part, 1), (ground, 0)])
                    consumed.add(key)

        try:
            circuit = rf.Circuit(connections, auto_reduce=True)
            output = circuit.network
            output.name = "bpm_tuned_network"
        except Exception as exc:
            raise ConfigError(
                "Circuit assembly failed. Check reciprocal connections, signal ports, components, and "
                f"frequency settings, then try again. Details: {exc}"
            ) from exc
        component_count = sum(
            bool(port.component_path)
            for item in active_items
            for port in item.ports
            if port.mode not in (ConnectionType.OPEN, ConnectionType.SHORT, ConnectionType.CONNECT, ConnectionType.SIGNAL)
        )
        target = config.smith_target_gamma if config.smith_target_enabled else None
        port_ranges = [(port.start_ghz, port.stop_ghz) for _, port in sorted(signal_ports, key=lambda pair: pair[1].signal or "")]
        metrics = network_metrics(
            output,
            component_count=component_count,
            target=target,
            port_ranges_ghz=port_ranges,
        )
        return SimulationResult(output, metrics, deepcopy(config), signal_names, inactive_names)


# Compatibility name used by UI integrations.
RFEngine = CircuitEngine
