from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Protocol

import numpy as np
import skrf as rf


PASSIVITY_LIMIT = 1.0 + 1e-9
INVALID_RF_PENALTY = 1e6


@dataclass(frozen=True)
class PerformanceMetrics:
    max_vswr_s11: float
    max_vswr_s22: float
    worst_vswr: float
    worst_il_db: float
    smith_radius: float
    smith_contour: float
    target_distance: float
    target_error_s11_max: float = 0.0
    target_error_s22_max: float = 0.0
    target_error_spread: float = 0.0
    vswr_spread: float = 0.0
    component_count: int = 0
    tolerance_vswr: float | None = None
    tolerance_il_db: float | None = None
    target_error_5pct_max: float | None = None
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
    raw_diagonal = np.stack([magnitude[:, i, i] for i in range(network.nports)], axis=1)
    diagonal = np.clip(raw_diagonal, 0.0, 0.999999)
    vswr = (1.0 + diagonal) / (1.0 - diagonal)
    frequencies = network.f / 1e9

    def configured_mask(index: int) -> np.ndarray:
        if not port_ranges_ghz or index >= len(port_ranges_ghz):
            return np.ones(len(frequencies), dtype=bool)
        start, stop = port_ranges_ghz[index]
        if start is None or stop is None:
            return np.ones(len(frequencies), dtype=bool)
        mask = (frequencies >= start) & (frequencies <= stop)
        if not np.any(mask):
            raise ValueError(f"Signal s{index + 1} frequency range contains no simulated points.")
        return mask

    antenna_index = network.nports - 1
    masks = [configured_mask(index) for index in range(antenna_index)]
    antenna_mask = np.logical_or.reduce(masks) if masks else np.ones(len(frequencies), dtype=bool)
    masks.append(antenna_mask)
    vswr_maxima = [_finite_max(vswr[mask, index]) for index, mask in enumerate(masks)]
    reflection_traces = [network.s[mask, index, index] for index, mask in enumerate(masks)]
    insertion_losses = [
        -20.0 * np.log10(np.maximum(magnitude[mask, antenna_index, index], 1e-15))
        for index, mask in enumerate(masks[:-1])
    ]
    relevant_reflections = [
        raw_diagonal[mask, index] for index, mask in enumerate(masks)
    ]
    relevant_transmissions = [
        magnitude[mask, antenna_index, index] for index, mask in enumerate(masks[:-1])
    ]
    invalid_passivity = any(
        np.any(~np.isfinite(values)) or np.any(values > PASSIVITY_LIMIT)
        for values in (*relevant_reflections, *relevant_transmissions)
    )
    traces = np.concatenate(reflection_traces)
    active_targets = dict(targets or {})
    if target is not None and targets is None:
        # Preserve the original project-wide target API for integrations that
        # have not moved to per-signal targets.
        target_values = [target] * network.nports
    else:
        if target is not None and 0 not in active_targets:
            active_targets[0] = target
        # The reference optimizer treats a disabled special target as the
        # Smith-chart centre (50 + j0 ohm, gamma=0), including the dependent
        # antenna port.
        target_values = [active_targets.get(index, 0.0 + 0.0j) for index in range(network.nports)]
    target_deltas = [
        reflection_traces[index] - target_values[index] for index in range(network.nports)
    ]
    target_distances = [_finite_max(np.abs(delta)) for delta in target_deltas]
    target_error_s11_max = max(target_distances[:-1], default=0.0)
    target_error_s22_max = target_distances[-1]
    target_distance = max(target_error_s11_max, target_error_s22_max)
    target_points = np.concatenate(target_deltas)
    smith_contour = float(
        np.std(np.real(target_points)) ** 2
        + np.std(np.imag(target_points)) ** 2
        + np.mean(np.abs(target_points)) ** 2
    )
    if invalid_passivity:
        return PerformanceMetrics(
            max_vswr_s11=INVALID_RF_PENALTY,
            max_vswr_s22=INVALID_RF_PENALTY,
            worst_vswr=INVALID_RF_PENALTY,
            worst_il_db=INVALID_RF_PENALTY,
            smith_radius=INVALID_RF_PENALTY,
            smith_contour=INVALID_RF_PENALTY,
            target_distance=INVALID_RF_PENALTY,
            target_error_s11_max=INVALID_RF_PENALTY,
            target_error_s22_max=INVALID_RF_PENALTY,
            target_error_spread=0.0,
            vswr_spread=0.0,
            component_count=component_count,
        )

    return PerformanceMetrics(
        max_vswr_s11=max(vswr_maxima[:-1], default=1.0),
        max_vswr_s22=vswr_maxima[-1],
        worst_vswr=max(vswr_maxima),
        worst_il_db=max(0.0, max((_finite_max(loss) for loss in insertion_losses), default=0.0)),
        smith_radius=_finite_max(np.abs(traces)),
        smith_contour=smith_contour,
        target_distance=target_distance,
        target_error_s11_max=target_error_s11_max,
        target_error_s22_max=target_error_s22_max,
        target_error_spread=abs(target_error_s11_max - target_error_s22_max),
        vswr_spread=abs(max(vswr_maxima[:-1], default=1.0) - vswr_maxima[-1]),
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
    target_error = _normalize(
        item.metrics.target_error_5pct_max
        if item.metrics.target_error_5pct_max not in (None, 0.0)
        else item.metrics.target_distance
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
    spread = _normalize(
        item.metrics.target_error_spread
        if item.metrics.target_error_spread != 0.0
        else item.metrics.vswr_spread
        for item in items
    )
    scores = 0.30 * target_error + 0.25 * count + 0.20 * sensitivity + 0.15 * loss + 0.10 * spread
    for item, score in zip(items, scores, strict=True):
        item.metrics = replace(item.metrics, production_risk=round(float(score), 4))
