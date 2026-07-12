from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Protocol

import numpy as np
import skrf as rf


@dataclass(frozen=True)
class PerformanceMetrics:
    max_vswr_s11: float
    max_vswr_s22: float
    worst_vswr: float
    worst_il_db: float
    smith_radius: float
    smith_contour: float
    target_distance: float
    component_count: int = 0
    tolerance_vswr: float | None = None
    tolerance_il_db: float | None = None
    vswr_sensitivity: float = 0.0
    production_risk: float = 0.0


def _finite_max(values: np.ndarray, ceiling: float = 1e6) -> float:
    finite = np.asarray(values)[np.isfinite(values)]
    return float(min(np.max(finite), ceiling)) if finite.size else ceiling


def network_metrics(
    network: rf.Network,
    *,
    component_count: int = 0,
    target: complex | None = None,
    targets: dict[int, complex] | None = None,
    port_ranges_ghz: list[tuple[float | None, float | None]] | None = None,
) -> PerformanceMetrics:
    if network.nports < 2:
        raise ValueError("At least two signal ports are required to calculate S11, S22, and S21.")
    magnitude = np.abs(network.s)
    diagonal = np.stack([magnitude[:, i, i] for i in range(network.nports)], axis=1)
    diagonal = np.clip(diagonal, 0.0, 0.999999)
    vswr = (1.0 + diagonal) / (1.0 - diagonal)
    frequencies = network.f / 1e9

    def mask_for(index: int) -> np.ndarray:
        if not port_ranges_ghz or index >= len(port_ranges_ghz):
            return np.ones(len(frequencies), dtype=bool)
        start, stop = port_ranges_ghz[index]
        if start is None or stop is None:
            return np.ones(len(frequencies), dtype=bool)
        mask = (frequencies >= start) & (frequencies <= stop)
        if not np.any(mask):
            raise ValueError(f"Signal s{index + 1} frequency range contains no simulated points.")
        return mask

    masks = [mask_for(index) for index in range(network.nports)]
    vswr_maxima = [_finite_max(vswr[mask, index]) for index, mask in enumerate(masks)]
    reflection_traces = [network.s[mask, index, index] for index, mask in enumerate(masks)]
    s21_mag = np.maximum(magnitude[:, 1, 0], 1e-15)
    insertion_loss = -20.0 * np.log10(s21_mag[masks[0]])
    traces = np.concatenate(reflection_traces)
    centroid = np.mean(traces)
    active_targets = dict(targets or {})
    if target is not None and targets is None:
        # Preserve the original project-wide target API for integrations that
        # have not moved to per-signal targets.
        target_distances = [_finite_max(np.abs(traces - target))]
    else:
        if target is not None and 0 not in active_targets:
            active_targets[0] = target
        target_distances = [
            _finite_max(np.abs(reflection_traces[index] - target_value))
            for index, target_value in active_targets.items()
            if 0 <= index < len(reflection_traces)
        ]
    target_distance = max(target_distances, default=0.0)
    return PerformanceMetrics(
        max_vswr_s11=vswr_maxima[0],
        max_vswr_s22=vswr_maxima[1],
        worst_vswr=max(vswr_maxima),
        worst_il_db=max(0.0, _finite_max(insertion_loss)),
        smith_radius=_finite_max(np.abs(traces)),
        smith_contour=_finite_max(np.abs(traces - centroid)),
        target_distance=target_distance,
        component_count=component_count,
    )


class _RiskItem(Protocol):
    metrics: PerformanceMetrics


def _normalize(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    span = float(np.max(array) - np.min(array))
    return np.zeros_like(array) if span <= 1e-15 else (array - np.min(array)) / span


def assign_production_risk(items: list[_RiskItem]) -> None:
    """Assign the exact Requirements.md weighted score across the five profiles."""
    if not items:
        return
    tolerance = _normalize(
        item.metrics.tolerance_vswr if item.metrics.tolerance_vswr is not None else item.metrics.worst_vswr
        for item in items
    )
    count = _normalize(item.metrics.component_count for item in items)
    sensitivity = _normalize(item.metrics.vswr_sensitivity for item in items)
    loss = _normalize(
        abs(item.metrics.tolerance_il_db)
        if item.metrics.tolerance_il_db is not None
        else abs(item.metrics.worst_il_db)
        for item in items
    )
    spread = _normalize(abs(item.metrics.max_vswr_s11 - item.metrics.max_vswr_s22) for item in items)
    scores = 0.30 * tolerance + 0.25 * count + 0.20 * sensitivity + 0.15 * loss + 0.10 * spread
    for item, score in zip(items, scores, strict=True):
        item.metrics = replace(item.metrics, production_risk=float(score))
