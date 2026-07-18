from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """A user-editable project setting is invalid."""


class ConnectionType(str, Enum):
    OPEN = "open"
    SHORT = "short"
    INDUCTOR = "inductor"
    CAPACITOR = "capacitor"
    INDUCTOR_CAPACITOR = "inductor/capacitor"
    OPEN_INDUCTOR_CAPACITOR = "open/inductor/capacitor"
    CONNECT = "connect"
    SIGNAL = "signal"


@dataclass
class PortConfig:
    port: int
    mode: ConnectionType = ConnectionType.OPEN
    component_path: str | None = None
    connect_network: str | None = None
    connect_port: int | None = None
    signal: str | None = None
    start_ghz: float | None = None
    stop_ghz: float | None = None
    smith_target_enabled: bool = False
    smith_target_resistance_ohm: float = 50.0
    smith_target_reactance_ohm: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortConfig":
        value = dict(data)
        value["mode"] = ConnectionType(value.get("mode", "open"))
        return cls(**value)

    @property
    def smith_target_impedance(self) -> complex:
        return complex(self.smith_target_resistance_ohm, self.smith_target_reactance_ohm)

    def smith_target_gamma(self, reference_ohm: float = 50.0) -> complex:
        denominator = self.smith_target_impedance + reference_ohm
        if abs(denominator) <= 1e-15:
            raise ConfigError("Smith target impedance cannot equal negative reference impedance.")
        return (self.smith_target_impedance - reference_ohm) / denominator


@dataclass
class NetworkConfig:
    path: str
    ports: list[PortConfig] = field(default_factory=list)

    @classmethod
    def with_open_ports(cls, path: str | Path, nports: int) -> "NetworkConfig":
        return cls(str(path), [PortConfig(port=i + 1) for i in range(nports)])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NetworkConfig":
        return cls(path=data["path"], ports=[PortConfig.from_dict(p) for p in data["ports"]])

    def is_unused_reference(self) -> bool:
        """A fully open file is retained in the project but excluded from RF calculations."""
        return bool(self.ports) and all(port.mode == ConnectionType.OPEN for port in self.ports)


@dataclass
class ProjectConfig:
    networks: list[NetworkConfig] = field(default_factory=list)
    start_ghz: float | None = None
    stop_ghz: float | None = None
    points: int = 201
    smith_target_enabled: bool = False
    smith_target_resistance_ohm: float = 50.0
    smith_target_reactance_ohm: float = 0.0
    smith_reference_ohm: float = 50.0
    # Exhaustive search grows multiplicatively across tunable ports. Two real
    # parts per type keeps the supplied six-slot project at 15,625 combinations.
    candidates_per_type: int = 2
    optimization_passes: int = 2
    # Optional inclusive real-BOM value windows. ``None`` preserves the full
    # measured catalog for configurations saved before range controls existed.
    inductor_min_nh: float | None = None
    inductor_max_nh: float | None = None
    capacitor_min_pf: float | None = None
    capacitor_max_pf: float | None = None

    def validate(self, *, allow_unselected: bool = False) -> None:
        if not self.networks:
            raise ConfigError("Add at least one Touchstone file before running.")
        if (self.start_ghz is None) != (self.stop_ghz is None):
            raise ConfigError("Set both the project start and stop frequencies, or leave both automatic.")
        if self.start_ghz is not None and self.start_ghz >= self.stop_ghz:
            raise ConfigError("The project start frequency must be below the stop frequency.")
        if not 11 <= self.points <= 5001:
            raise ConfigError("Frequency points must be between 11 and 5001.")
        if self.candidates_per_type < 1 or self.optimization_passes < 1:
            raise ConfigError("Optimization candidate and pass counts must be positive.")
        for label, minimum, maximum, unit in (
            ("Inductor", self.inductor_min_nh, self.inductor_max_nh, "nH"),
            ("Capacitor", self.capacitor_min_pf, self.capacitor_max_pf, "pF"),
        ):
            if (minimum is None) != (maximum is None):
                raise ConfigError(
                    f"Set both the {label.lower()} minimum and maximum, or leave both automatic."
                )
            if minimum is None:
                continue
            if not math.isfinite(minimum) or not math.isfinite(maximum):
                raise ConfigError(f"{label} optimization range must use finite values in {unit}.")
            if minimum <= 0 or maximum <= 0:
                raise ConfigError(f"{label} optimization range values must be greater than zero {unit}.")
            if minimum > maximum:
                raise ConfigError(
                    f"{label} optimization range minimum must not exceed its maximum."
                )
        if self.smith_target_enabled:
            target_values = (
                self.smith_target_resistance_ohm,
                self.smith_target_reactance_ohm,
                self.smith_reference_ohm,
            )
            if not all(math.isfinite(value) for value in target_values):
                raise ConfigError("Smith target impedance values must be finite.")
            if self.smith_target_resistance_ohm < 0:
                raise ConfigError("Smith target resistance must be zero or greater.")
            if self.smith_reference_ohm <= 0:
                raise ConfigError("Smith target reference impedance must be greater than zero.")

        by_name: dict[str, NetworkConfig] = {}
        for network in self.networks:
            name = Path(network.path).name
            if name in by_name:
                raise ConfigError(f"Touchstone file is listed twice: {name}")
            by_name[name] = network
            numbers = [p.port for p in network.ports]
            if numbers != list(range(1, len(numbers) + 1)):
                raise ConfigError(f"{name}: ports must be numbered consecutively from 1.")

        signals: list[str] = []
        signal_ports: list[PortConfig] = []
        for network in self.networks:
            source = Path(network.path).name
            for port in network.ports:
                if port.smith_target_enabled and port.mode != ConnectionType.SIGNAL:
                    raise ConfigError(
                        f"{source} port {port.port}: Smith targets can be enabled only on signal ports."
                    )
                if (port.start_ghz is None) != (port.stop_ghz is None):
                    raise ConfigError(f"{source} port {port.port}: set both frequency limits.")
                if port.start_ghz is not None and port.start_ghz >= port.stop_ghz:
                    raise ConfigError(f"{source} port {port.port}: start frequency must be below stop.")
                if port.mode == ConnectionType.SIGNAL:
                    if not port.signal:
                        raise ConfigError(f"{source} port {port.port}: select s1, s2, s3, or s4.")
                    signals.append(port.signal.lower())
                    signal_ports.append(port)
                elif port.mode == ConnectionType.CONNECT:
                    if not port.connect_network or port.connect_port is None:
                        raise ConfigError(f"{source} port {port.port}: select a destination network and port.")
                    target_name = Path(port.connect_network).name
                    target = by_name.get(target_name)
                    if target is None or not 1 <= port.connect_port <= len(target.ports):
                        raise ConfigError(f"{source} port {port.port}: connection destination does not exist.")
                    if source == target_name and port.port == port.connect_port:
                        raise ConfigError(f"{source} port {port.port}: a port cannot connect to itself.")
                    peer = target.ports[port.connect_port - 1]
                    if not (
                        peer.mode == ConnectionType.CONNECT
                        and Path(peer.connect_network or "").name == source
                        and peer.connect_port == port.port
                    ):
                        raise ConfigError(
                            f"{source} port {port.port}: connection must be reciprocal at "
                            f"{target_name} port {port.connect_port}."
                        )
                elif port.mode in (ConnectionType.INDUCTOR, ConnectionType.CAPACITOR):
                    if not port.component_path and not allow_unselected:
                        raise ConfigError(
                            f"{source} port {port.port}: select a real BOM component, or run optimization."
                        )

        if not 2 <= len(signals) <= 4:
            raise ConfigError("Assign between 2 and 4 signal ports.")
        if len(set(signals)) != len(signals):
            raise ConfigError("Each signal name can be assigned only once.")
        expected = [f"s{i}" for i in range(1, len(signals) + 1)]
        if sorted(signals) != expected:
            raise ConfigError(f"Signal names must be consecutive: {', '.join(expected)}.")

        dependent_signal = expected[-1]
        for port in signal_ports:
            if port.smith_target_enabled and (port.signal or "").lower() == dependent_signal:
                raise ConfigError(
                    f"{dependent_signal} is the dependent antenna port and cannot have an independent Smith target."
                )
            if port.smith_target_enabled:
                values = (port.smith_target_resistance_ohm, port.smith_target_reactance_ohm)
                if not all(math.isfinite(value) for value in values):
                    raise ConfigError(f"{port.signal} Smith target impedance values must be finite.")
                if port.smith_target_resistance_ohm < 0:
                    raise ConfigError(f"{port.signal} Smith target resistance must be zero or greater.")

        active_names = self.active_network_names()
        for network in self.networks:
            name = Path(network.path).name
            if name not in active_names and not network.is_unused_reference():
                raise ConfigError(
                    f"{name} is not connected to the signal circuit. Connect it, assign its signal ports, "
                    "or set every port to open so it can remain as an unused reference file."
                )

    def active_network_names(self) -> set[str]:
        """Return the connected network component containing the first signal port."""
        names = {Path(network.path).name: network for network in self.networks}
        roots = [
            Path(network.path).name
            for network in self.networks
            if any(port.mode == ConnectionType.SIGNAL for port in network.ports)
        ]
        if not roots:
            return set()
        edges: dict[str, set[str]] = {name: set() for name in names}
        for name, network in names.items():
            for port in network.ports:
                if port.mode == ConnectionType.CONNECT and port.connect_network:
                    target = Path(port.connect_network).name
                    if target in names:
                        edges[name].add(target)
                        edges[target].add(name)
        active: set[str] = set()
        pending = [roots[0]]
        while pending:
            name = pending.pop()
            if name in active:
                continue
            active.add(name)
            pending.extend(edges[name] - active)
        return active

    def signal_ports(self) -> list[PortConfig]:
        return sorted(
            (port for network in self.networks for port in network.ports if port.mode == ConnectionType.SIGNAL),
            key=lambda port: port.signal or "",
        )

    def smith_targets_by_signal(self) -> dict[str, tuple[complex, complex]]:
        """Map driven signal names to (physical impedance, reflection coefficient)."""
        ports = self.signal_ports()
        targets = {
            port.signal or "": (port.smith_target_impedance, port.smith_target_gamma(self.smith_reference_ohm))
            for port in ports[:-1]
            if port.smith_target_enabled
        }
        # Backward-compatible fallback for configurations saved before targets
        # became per-signal settings.
        if not targets and self.smith_target_enabled and ports:
            targets[ports[0].signal or "s1"] = (self.smith_target_impedance, self.smith_target_gamma)
        return targets

    @property
    def smith_target_impedance(self) -> complex:
        return complex(self.smith_target_resistance_ohm, self.smith_target_reactance_ohm)

    @property
    def smith_target_gamma(self) -> complex:
        impedance = self.smith_target_impedance
        reference = self.smith_reference_ohm
        denominator = impedance + reference
        if abs(denominator) <= 1e-15:
            raise ConfigError("Smith target impedance cannot equal negative reference impedance.")
        return (impedance - reference) / denominator

    # Compatibility aliases for callers that used the original ambiguous names.
    @property
    def smith_target_real(self) -> float:
        return self.smith_target_resistance_ohm

    @smith_target_real.setter
    def smith_target_real(self, value: float) -> None:
        self.smith_target_resistance_ohm = value

    @property
    def smith_target_imag(self) -> float:
        return self.smith_target_reactance_ohm

    @smith_target_imag.setter
    def smith_target_imag(self, value: float) -> None:
        self.smith_target_reactance_ohm = value

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for network in value["networks"]:
            for port in network["ports"]:
                if isinstance(port["mode"], ConnectionType):
                    port["mode"] = port["mode"].value
        return value

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectConfig":
        value = dict(data)
        value["networks"] = [NetworkConfig.from_dict(n) for n in value.get("networks", [])]
        # Version-1 files stored a raw reflection coefficient in the ambiguous
        # smith_target_real/imag fields. Convert it to physical impedance.
        if "smith_target_resistance_ohm" not in value and (
            "smith_target_real" in value or "smith_target_imag" in value
        ):
            gamma = complex(value.pop("smith_target_real", 0.0), value.pop("smith_target_imag", 0.0))
            reference = float(value.get("smith_reference_ohm", 50.0))
            if abs(1 - gamma) <= 1e-15:
                raise ConfigError("Legacy Smith target reflection coefficient cannot equal 1.")
            impedance = reference * (1 + gamma) / (1 - gamma)
            value["smith_target_resistance_ohm"] = float(impedance.real)
            value["smith_target_reactance_ohm"] = float(impedance.imag)
        allowed = {item.name for item in fields(cls)}
        project = cls(**{key: item for key, item in value.items() if key in allowed})
        # Move the former project-wide target to s1 so the new GUI exposes it
        # as an individual driven-port target.
        if project.smith_target_enabled and not any(
            port.smith_target_enabled for port in project.signal_ports()
        ):
            signal_ports = project.signal_ports()
            if signal_ports:
                signal_ports[0].smith_target_enabled = True
                signal_ports[0].smith_target_resistance_ohm = project.smith_target_resistance_ohm
                signal_ports[0].smith_target_reactance_ohm = project.smith_target_reactance_ohm
                project.smith_target_enabled = False
        return project

    @classmethod
    def load(cls, path: str | Path) -> "ProjectConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# Terminology used by older integrations.
PortSetting = PortConfig
