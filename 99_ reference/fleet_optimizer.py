"""
Fleet Optimizer: 5-agent RF impedance matching optimization.

Each agent sweeps all BOM components for tunable ports using a different
optimization objective. The Principal Agent selects the lowest-risk solution.

Performance is evaluated over the configured frequency range using scikit-rf.
"""

from smith_chart_utils import draw_smith_chart_background
from .smith_targets import (
    build_target_gamma_matrix,
    coerce_special_smith_targets,
    format_impedance,
    impedance_to_gamma,
)
from .network_builder import NetworkConfig, PortTermination, build_network_from_config
from .bom_parser import list_capacitors, list_inductors
from matplotlib.patches import Circle
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
import matplotlib.pyplot as plt
import json
import itertools
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import skrf as rf
import matplotlib
matplotlib.use('Agg')  # non-interactive backend for fleet


def _nan_separate(freq_arr: np.ndarray, *data_arrs, gap_factor: float = 3.0):
    """Insert np.nan separators at large frequency gaps to break matplotlib lines.

    Returns (freq_out, data1_out, data2_out, ...) as np.ndarray each.
    gap_factor: a step > gap_factor * median_step is treated as a band gap.
    """
    freq_arr = np.asarray(freq_arr, dtype=float)
    data_arrs = [np.asarray(d, dtype=float) for d in data_arrs]
    if len(freq_arr) < 2:
        return (freq_arr,) + tuple(data_arrs)
    steps = np.diff(freq_arr)
    median_step = np.median(steps)
    gap_mask = steps > gap_factor * median_step
    if not np.any(gap_mask):
        return (freq_arr,) + tuple(data_arrs)
    # Insert NaN after each gap index
    gap_positions = np.where(gap_mask)[0] + 1  # positions to insert NaN before
    out_lists = [[] for _ in range(1 + len(data_arrs))]
    prev = 0
    for pos in gap_positions:
        for j, arr in enumerate([freq_arr] + data_arrs):
            out_lists[j].extend(arr[prev:pos].tolist())
            out_lists[j].append(np.nan)
        prev = pos
    for j, arr in enumerate([freq_arr] + data_arrs):
        out_lists[j].extend(arr[prev:].tolist())
    return tuple(np.array(lst) for lst in out_lists)


# Try to import the Rust extension for accelerated sweeps
try:
    import rf_sweep as _rf_sweep
    _RUST_AVAILABLE = True
    _RUST_TARGETS_AVAILABLE = hasattr(
        _rf_sweep, "sweep_terminations_parallel_targets")
except ImportError:
    _RUST_AVAILABLE = False
    _RUST_TARGETS_AVAILABLE = False


@dataclass
class ComponentAssignment:
    """Maps a tunable port to its assigned component."""
    network_id: str
    port_index: int     # 1-based
    term_type: str      # 'capacitor' | 'inductor' | 'open'
    component_name: str = ""
    component_path: str = ""


@dataclass
class AgentResult:
    """Result from one optimization agent."""
    agent_id: int
    agent_name: str
    strategy: str
    assignments: List[ComponentAssignment]
    vswr_s11_max: float
    vswr_s22_max: float
    worst_il_db: float       # worst (most negative) S21 in freq range
    component_count: int
    # max |Sii - target_gamma| across non-antenna ports
    target_error_s11_max: float = 0.0
    # max |Saa - target_gamma| at antenna/common port
    target_error_s22_max: float = 0.0
    target_error_max: float = 0.0
    vswr_s11_5pct_max: float = 0.0   # worst VSWR under ±5% component tolerance
    vswr_s22_5pct_max: float = 0.0
    target_error_5pct_max: float = 0.0
    worst_il_5pct_db: float = 0.0
    vswr_sensitivity: float = 0.0    # max VSWR degradation across tolerance sweep
    vswr_spread: float = 0.0         # |vswr_s11_max - vswr_s22_max|
    target_error_spread: float = 0.0
    risk_score: float = 0.0
    freq_ghz: List[float] = field(default_factory=list)
    s11_mag: List[float] = field(default_factory=list)
    s22_mag: List[float] = field(default_factory=list)
    s21_db: List[float] = field(default_factory=list)
    # Complex S-params for Smith chart (real/imag split to keep JSON-serialisable)
    s11_re: List[float] = field(default_factory=list)
    s11_im: List[float] = field(default_factory=list)
    s22_re: List[float] = field(default_factory=list)
    s22_im: List[float] = field(default_factory=list)
    # N-port generalised fields (N >= 2).  For N=2 these mirror the scalar fields above.
    # sii_mag_list[i] = |S_{i+1,i+1}| magnitude array for port i (0-indexed)
    # sij_db_list[i]  = IL dB array for S_{ant, i}   (antenna→signal port i)
    # sii_re_list[i]  = Re(S_{i+1,i+1}) array;  sii_im_list[i] = Im(...)
    sii_mag_list: List[List[float]] = field(default_factory=list)
    sij_db_list:  List[List[float]] = field(default_factory=list)
    sii_re_list:  List[List[float]] = field(default_factory=list)
    sii_im_list:  List[List[float]] = field(default_factory=list)
    freq_ghz_list: List[List[float]] = field(
        default_factory=list)  # per-port freq arrays
    # [[start1,stop1],[start2,stop2],...] per non-ant port
    signal_freq_ranges_list: List[List[float]] = field(default_factory=list)
    special_smith_targets_list: List[List[float]] = field(
        default_factory=list)  # [[sig,start,stop,R,X],...]
    global_freq_start: float = 0.0
    global_freq_stop: float = 0.0


@dataclass
class FleetResult:
    """Full results from the fleet run."""
    agent_results: List[AgentResult]
    winner_agent_id: int
    winner_reason: str
    risk_scores: Dict[str, float] = field(default_factory=dict)


def _get_tunable_ports(app_state) -> List[tuple]:
    """
    Return list of (network_id, port_index, term_type) for tunable (swept) ports.

    open/* ports sweep with an 'open' baseline (shunt-to-ground components).
    short/* ports sweep with a 'short' baseline (series components, other end open).
    Ports set to 'capacitor' or 'inductor' with a specific component already
    selected are treated as FIXED — the fleet uses that component as-is.
    """
    SWEEP_TYPES = {
        'open/ind', 'open/cap', 'open/ind/cap',
        'short/ind', 'short/cap', 'short/ind/cap',
    }
    tunable = []
    for fid, fc in app_state.files.items():
        for pnum, pc in fc.ports.items():
            if pc.term_type in SWEEP_TYPES:
                tunable.append((fid, pnum, pc.term_type))
    return tunable


def _build_config_with_assignments(
    base_config: NetworkConfig,
    tunable_ports: List[tuple],
    assignments: List
) -> NetworkConfig:
    """
    Clone base_config and apply component assignments to tunable ports.
    assignments: list of component dicts (or None for the baseline — 'open' for open/* ports,
    'short' for short/* ports).
    Each component dict may carry a 'comp_type' key used when the port type is 'open/ind/cap'
    or 'short/ind/cap'.
    """
    import copy
    cfg = copy.deepcopy(base_config)
    for (nid, pnum, ttype), comp in zip(tunable_ports, assignments):
        if comp is None:
            # Baseline termination depends on the original port type
            baseline = 'short' if ttype.startswith('short/') else 'open'
            cfg.terminations[nid][pnum].type = baseline
            cfg.terminations[nid][pnum].component_path = None
        else:
            # For open/ind/cap or short/ind/cap ports the actual type lives in comp['comp_type']
            resolved_type = comp.get('comp_type', ttype)
            cfg.terminations[nid][pnum].type = resolved_type
            cfg.terminations[nid][pnum].component_path = comp['path']
    return cfg


def _evaluate_network(
    net: rf.Network,
    signal_freq_ranges: List[Tuple[float, float]],
    special_smith_targets=None,
) -> dict:
    """
    Evaluate an N-port network using per-signal-port frequency ranges.

    signal_freq_ranges[i] = (start_ghz, stop_ghz) for port i (0-indexed, non-antenna).
    Length = ant (= N-1). Antenna port mask = union of all non-antenna masks.

    Convention: antenna port = last port (index N-1).
    Returns a dict with composite scalars and per-port detail lists.

    Composite scalars (preserved for agent ranking logic):
      vswr_s11_max  — max VSWR across all NON-antenna signal ports (0..N-2)
      vswr_s22_max  — VSWR at antenna port (N-1)
      worst_il_db   — worst (most negative dB) IL from antenna to any signal port

    Legacy 2-port keys (s11_mag, s22_mag, s21_db, s11_re/im, s22_re/im) are
    populated from port 0 and the antenna port for backward compat with N=2.
    """
    n = net.nports
    ant = n - 1  # antenna port index
    f_ghz = net.frequency.f / 1e9
    target_gamma = build_target_gamma_matrix(
        f_ghz, n, special_smith_targets or {})

    # Build per-signal-port masks
    port_masks = []
    for i in range(ant):
        start, stop = signal_freq_ranges[i]
        port_masks.append((f_ghz >= start) & (f_ghz <= stop))
    # Antenna uses union of all non-antenna masks
    ant_mask = np.zeros(len(f_ghz), dtype=bool)
    for m in port_masks:
        ant_mask |= m
    port_masks.append(ant_mask)  # index = ant

    def vswr(s):
        mag = np.clip(np.abs(s), 0, 0.9999)
        return (1 + mag) / (1 - mag)

    # Per-port Sii reflection and VSWR
    sii_mag_list = []
    sii_re_list = []
    sii_im_list = []
    vswr_per_port = []
    target_error_per_port = []
    for i in range(n):
        s_ii = net.s[port_masks[i], i, i]
        sii_mag_list.append(np.abs(s_ii).tolist())
        sii_re_list.append(np.real(s_ii).tolist())
        sii_im_list.append(np.imag(s_ii).tolist())
        vswr_per_port.append(vswr(s_ii))
        target_i = target_gamma[i, port_masks[i]]
        target_error_per_port.append(np.abs(s_ii - target_i))

    # IL from antenna to each non-antenna signal port
    sij_db_list = []
    worst_il_db = 0.0
    for i in range(ant):  # signal ports 0..ant-1
        s_ant_i = net.s[port_masks[i], ant, i]
        il_db = 20 * np.log10(np.abs(s_ant_i) + 1e-15)
        sij_db_list.append(il_db.tolist())
        worst_il_db = min(worst_il_db, float(np.min(il_db)))

    # Composite VSWR metrics
    vswr_s11_max = float(
        max(np.max(vswr_per_port[i]) for i in range(ant))) if ant > 0 else 1.0
    vswr_s22_max = float(np.max(vswr_per_port[ant]))
    target_error_s11_max = (
        float(max(np.max(target_error_per_port[i]) for i in range(ant)))
        if ant > 0 else 0.0
    )
    target_error_s22_max = float(np.max(target_error_per_port[ant]))
    target_error_max = max(target_error_s11_max, target_error_s22_max)

    # Legacy 2-port keys: port 0 and antenna
    s11_legacy = net.s[port_masks[0], 0, 0]
    s_ant_0 = net.s[port_masks[0], ant, 0]
    il_legacy = 20 * np.log10(np.abs(s_ant_0) + 1e-15)

    # Per-port freq arrays
    freq_ghz_list = [f_ghz[port_masks[i]].tolist() for i in range(n)]

    return {
        'vswr_s11_max': vswr_s11_max,
        'vswr_s22_max': vswr_s22_max,
        'target_error_s11_max': target_error_s11_max,
        'target_error_s22_max': target_error_s22_max,
        'target_error_max': target_error_max,
        'target_error_spread': abs(target_error_s11_max - target_error_s22_max),
        'worst_il_db':  worst_il_db,
        # legacy: port-0 range
        'freq_ghz': f_ghz[port_masks[0]].tolist(),
        'freq_ghz_list': freq_ghz_list,                    # per-port freq arrays
        # Legacy 2-port fields
        's11_mag': np.abs(s11_legacy).tolist(),
        's22_mag': sii_mag_list[ant],
        's21_db':  il_legacy.tolist(),
        's11_re':  np.real(s11_legacy).tolist(),
        's11_im':  np.imag(s11_legacy).tolist(),
        's22_re':  sii_re_list[ant],
        's22_im':  sii_im_list[ant],
        # N-port generalised fields
        'sii_mag_list': sii_mag_list,
        'sij_db_list':  sij_db_list,
        'sii_re_list':  sii_re_list,
        'sii_im_list':  sii_im_list,
    }


def _tolerance_value_factors(n_tolerance: int) -> List[float]:
    """Return component value multipliers for the tolerance sweep."""
    if n_tolerance <= 1:
        return [1.0]
    if n_tolerance == 3:
        return [1.0, 0.95, 1.05]
    return np.linspace(0.95, 1.05, n_tolerance).tolist()


def _build_eval_masks(
    f_ghz: np.ndarray,
    signal_freq_ranges: List[Tuple[float, float]],
    n_signals: int,
) -> List[np.ndarray]:
    """Build per-signal boolean masks matching _evaluate_network semantics."""
    if n_signals < 2:
        raise ValueError("At least two signal ports are required.")

    eval_masks = []
    ant_mask = np.zeros(len(f_ghz), dtype=bool)
    fallback = (float(f_ghz[0]), float(f_ghz[-1]))

    for i in range(n_signals - 1):
        start, stop = signal_freq_ranges[i] if i < len(
            signal_freq_ranges) else fallback
        mask = (f_ghz >= start) & (f_ghz <= stop)
        if not np.any(mask):
            raise ValueError(
                f"Signal port {i + 1} has an empty tolerance evaluation range.")
        eval_masks.append(mask)
        ant_mask |= mask

    if not np.any(ant_mask):
        raise ValueError(
            "Antenna port has an empty tolerance evaluation range.")
    eval_masks.append(ant_mask)
    return eval_masks


def _mask_to_range(mask: np.ndarray, label: str) -> Tuple[int, int]:
    idx = np.where(mask)[0]
    if len(idx) == 0:
        raise ValueError(f"{label} has an empty tolerance evaluation range.")
    return int(idx[0]), int(idx[-1])


def _build_eval_ranges(
    f_ghz: np.ndarray,
    signal_freq_ranges: List[Tuple[float, float]],
    n_signals: int,
) -> List[Tuple[int, int]]:
    """Convert per-signal GHz ranges to inclusive frequency index ranges."""
    masks = _build_eval_masks(f_ghz, signal_freq_ranges, n_signals)
    return [_mask_to_range(mask, f"Signal port {i + 1}") for i, mask in enumerate(masks)]


def _eval_masks_are_contiguous(eval_masks: List[np.ndarray]) -> bool:
    for mask in eval_masks:
        start, stop = _mask_to_range(mask, "Evaluation mask")
        if not np.all(mask[start:stop + 1]):
            return False
    return True


def _scale_component_gamma(
    gamma: np.ndarray,
    z0: np.ndarray,
    term_type: str,
    value_factor: float,
) -> np.ndarray:
    """
    Approximate component value tolerance from a nominal one-port termination.

    Inductor reactance scales with L, while capacitor reactance scales with 1/C.
    This keeps the sweep tied to the selected component's measured/modelled gamma
    instead of merely multiplying finished VSWR summaries.
    """
    if value_factor == 1.0:
        return gamma.copy()

    lower_type = term_type.lower()
    if 'capacitor' not in lower_type and 'inductor' not in lower_type:
        return gamma.copy()

    denom = 1.0 - gamma
    near_open = np.abs(denom) < 1e-12
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        z_nom = z0 * (1.0 + gamma) / denom
        if 'capacitor' in lower_type:
            z_var = z_nom / value_factor
        else:
            z_var = z_nom * value_factor
        gamma_var = (z_var - z0) / (z_var + z0)

    gamma_var = np.asarray(gamma_var, dtype=complex)
    gamma_var[near_open] = gamma[near_open]
    invalid = ~np.isfinite(gamma_var.real) | ~np.isfinite(gamma_var.imag)
    if np.any(invalid):
        gamma_var[invalid] = gamma[invalid]
    return gamma_var


def _tolerance_gamma_variants(
    base_net: rf.Network,
    tunable_ports: List[tuple],
    assignments: List,
    value_factors: List[float],
) -> List[np.ndarray]:
    """Build per-tunable-port gamma rows for nominal and tolerance variants."""
    from .network_builder import NetworkBuilder, PortTermination

    nfreq = len(base_net.frequency)
    variants_per_port = []

    for (_nid, _pnum, ttype), comp in zip(tunable_ports, assignments):
        if comp is None:
            gamma = -np.ones(nfreq, dtype=complex) if ttype.startswith(
                'short/') else np.ones(nfreq, dtype=complex)
            variants_per_port.append(gamma.reshape(1, nfreq))
            continue

        term_type = comp.get('comp_type', ttype)
        term = PortTermination(type=term_type, component_path=comp['path'])
        term_net = NetworkBuilder._build_termination_network_static(
            term, base_net.frequency)
        gamma_nom = np.asarray(term_net.s[:, 0, 0], dtype=complex)
        z0 = np.asarray(term_net.z0[:, 0], dtype=complex)

        rows = [
            _scale_component_gamma(gamma_nom, z0, term_type, factor)
            for factor in value_factors
        ]
        variants_per_port.append(np.vstack(rows))

    return variants_per_port


def _apply_termination_smatrix(
    s: np.ndarray,
    port_k: int,
    gamma: np.ndarray,
) -> np.ndarray:
    """Apply a one-port termination to an S-matrix over all frequency points."""
    _nfreq, nports, _ = s.shape
    keep = [i for i in range(nports) if i != port_k]

    reduced = s[:, keep, :][:, :, keep]
    s_ik = s[:, keep, port_k]
    s_kj = s[:, port_k, keep]
    denom = 1.0 - s[:, port_k, port_k] * gamma

    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        update = (
            s_ik[:, :, None]
            * gamma[:, None, None]
            * s_kj[:, None, :]
            / denom[:, None, None]
        )
    return reduced + update


def _compute_metrics_from_smatrix(
    s: np.ndarray,
    eval_masks: List[np.ndarray],
    n_signals: int,
    target_gamma: Optional[np.ndarray] = None,
) -> Optional[Tuple[float, float, float, float, float]]:
    """Evaluate the same composite metrics as the Rust sweep for an S-matrix."""
    nfreq = s.shape[0]
    ant = n_signals - 1
    vswr_s11_max = 1.0
    vswr_s22_max = 1.0
    target_error_s11_max = 0.0
    target_error_s22_max = 0.0
    worst_il = 0.0
    found = False
    if target_gamma is None:
        target_gamma = np.zeros((n_signals, nfreq), dtype=complex)

    for i in range(ant):
        mask = eval_masks[i]
        if len(mask) != nfreq or not np.any(mask):
            continue
        sii_mag = np.abs(s[mask, i, i])
        finite = np.isfinite(sii_mag)
        if np.any(finite):
            mag = np.clip(sii_mag[finite], 0, 0.99999)
            vswr_s11_max = max(vswr_s11_max, float(
                np.max((1 + mag) / (1 - mag))))
            found = True
        target_err = np.abs(s[mask, i, i] - target_gamma[i, mask])
        finite = np.isfinite(target_err)
        if np.any(finite):
            target_error_s11_max = max(
                target_error_s11_max, float(np.max(target_err[finite])))
            found = True

        il_mag = np.abs(s[mask, ant, i])
        finite = np.isfinite(il_mag)
        if np.any(finite):
            il_db = 20 * np.log10(il_mag[finite] + 1e-15)
            worst_il = min(worst_il, float(np.min(il_db)))
            found = True

    ant_mask = eval_masks[ant]
    if len(ant_mask) == nfreq and np.any(ant_mask):
        ant_mag = np.abs(s[ant_mask, ant, ant])
    else:
        ant_mag = np.asarray([], dtype=float)
    finite = np.isfinite(ant_mag)
    if np.any(finite):
        mag = np.clip(ant_mag[finite], 0, 0.99999)
        vswr_s22_max = max(vswr_s22_max, float(np.max((1 + mag) / (1 - mag))))
        found = True
    if len(ant_mask) == nfreq and np.any(ant_mask):
        target_err = np.abs(s[ant_mask, ant, ant] -
                            target_gamma[ant, ant_mask])
        finite = np.isfinite(target_err)
        if np.any(finite):
            target_error_s22_max = max(
                target_error_s22_max, float(np.max(target_err[finite])))
            found = True

    if not found:
        return None
    return vswr_s11_max, vswr_s22_max, worst_il, target_error_s11_max, target_error_s22_max


def _sweep_tolerance_python(
    base_net: rf.Network,
    gamma_variants: List[np.ndarray],
    eval_masks: List[np.ndarray],
    n_signals: int,
    target_gamma: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Python fallback for independent component tolerance combinations."""
    vswr_s11 = []
    vswr_s22 = []
    worst_il = []
    target_s11 = []
    target_s22 = []
    variant_ranges = [range(g.shape[0]) for g in gamma_variants]

    for combo in itertools.product(*variant_ranges):
        s = np.array(base_net.s, dtype=complex, copy=True)
        for port_variants, variant_idx in zip(gamma_variants, combo):
            s = _apply_termination_smatrix(
                s, n_signals, port_variants[variant_idx])

        metrics = _compute_metrics_from_smatrix(
            s, eval_masks, n_signals, target_gamma)
        if metrics is None:
            continue
        s11, s22, il, target_non_ant, target_ant = metrics
        vswr_s11.append(s11)
        vswr_s22.append(s22)
        worst_il.append(il)
        target_s11.append(target_non_ant)
        target_s22.append(target_ant)

    return (
        np.asarray(vswr_s11, dtype=float),
        np.asarray(vswr_s22, dtype=float),
        np.asarray(worst_il, dtype=float),
        np.asarray(target_s11, dtype=float),
        np.asarray(target_s22, dtype=float),
    )


def _evaluate_with_tolerance(
    base_config: NetworkConfig, tunable_ports: List[tuple], assignments: List,
    signal_freq_ranges: List[Tuple[float, float]], special_smith_targets=None,
    n_tolerance: int = 3
) -> dict:
    """
    Evaluate network metrics under independent +/-5% component value variation.
    n_tolerance: number of tolerance samples (3 = nominal, -5%, +5%).
    Returns worst-case metrics.
    """
    value_factors = _tolerance_value_factors(n_tolerance)
    cfg_nominal = _build_config_with_assignments(
        base_config, tunable_ports, assignments)

    try:
        net_nominal = build_network_from_config(cfg_nominal)
        m = _evaluate_network(
            net_nominal, signal_freq_ranges, special_smith_targets)
        nominal_vswr = max(m['vswr_s11_max'], m['vswr_s22_max'])
    except Exception:
        return {'vswr_5pct_max_s11': 99, 'vswr_5pct_max_s22': 99, 'worst_il_5pct': -99,
                'vswr_sensitivity': 99, 'target_error_5pct_max': 99}

    try:
        from .network_builder import build_base_network_for_fleet

        tunable_keys = [(nid, pnum) for nid, pnum, _ttype in tunable_ports]
        base_net, _ordered_keys = build_base_network_for_fleet(
            base_config, tunable_keys)
        n_signals = net_nominal.nports
        if base_net.nports != n_signals + len(tunable_ports):
            raise ValueError(
                "Tolerance base network port count does not match tunable assignments.")
        if n_signals < 2:
            raise ValueError(
                "Tolerance base network has fewer than two signal ports.")

        f_ghz = base_net.frequency.f / 1e9
        eval_masks = _build_eval_masks(f_ghz, signal_freq_ranges, n_signals)
        eval_ranges = [
            _mask_to_range(mask, f"Signal port {i + 1}")
            for i, mask in enumerate(eval_masks)
        ]
        gamma_variants = _tolerance_gamma_variants(
            base_net, tunable_ports, assignments, value_factors)
        target_gamma = build_target_gamma_matrix(
            f_ghz, n_signals, special_smith_targets or {})
        has_special_targets = bool(coerce_special_smith_targets(
            special_smith_targets or {}, n_signals))

        if _RUST_AVAILABLE and _eval_masks_are_contiguous(eval_masks) and (
            not has_special_targets or _RUST_TARGETS_AVAILABLE
        ):
            if has_special_targets:
                (vswr_s11, vswr_s22, worst_il_arr,
                 target_s11, target_s22, _combo_indices) = _rf_sweep.sweep_terminations_parallel_targets(
                    np.ascontiguousarray(base_net.s.real, dtype=np.float64),
                    np.ascontiguousarray(base_net.s.imag, dtype=np.float64),
                    [np.ascontiguousarray(g.real, dtype=np.float64)
                     for g in gamma_variants],
                    [np.ascontiguousarray(g.imag, dtype=np.float64)
                     for g in gamma_variants],
                    eval_ranges,
                    n_signals,
                    np.ascontiguousarray(target_gamma.real, dtype=np.float64),
                    np.ascontiguousarray(target_gamma.imag, dtype=np.float64),
                )
            else:
                vswr_s11, vswr_s22, worst_il_arr, _combo_indices = _rf_sweep.sweep_terminations_parallel(
                    np.ascontiguousarray(base_net.s.real, dtype=np.float64),
                    np.ascontiguousarray(base_net.s.imag, dtype=np.float64),
                    [np.ascontiguousarray(g.real, dtype=np.float64)
                     for g in gamma_variants],
                    [np.ascontiguousarray(g.imag, dtype=np.float64)
                     for g in gamma_variants],
                    eval_ranges,
                    n_signals,
                )
                target_s11 = (vswr_s11 - 1.0) / (vswr_s11 + 1.0)
                target_s22 = (vswr_s22 - 1.0) / (vswr_s22 + 1.0)
        else:
            vswr_s11, vswr_s22, worst_il_arr, target_s11, target_s22 = _sweep_tolerance_python(
                base_net, gamma_variants, eval_masks, n_signals, target_gamma
            )

        if len(vswr_s11) == 0 or len(vswr_s22) == 0:
            raise ValueError(
                "Tolerance sweep produced no valid variation points.")

        worst_vswr_s11 = float(np.max(vswr_s11))
        worst_vswr_s22 = float(np.max(vswr_s22))
        target_error_5pct_max = float(
            np.max(np.maximum(target_s11, target_s22)))
        worst_il = float(np.min(worst_il_arr)) if len(
            worst_il_arr) else m['worst_il_db']
        max_vswr_deg = max(
            0.0,
            float(np.max(np.maximum(vswr_s11, vswr_s22)) - nominal_vswr),
        )
    except Exception:
        worst_vswr_s11 = m['vswr_s11_max']
        worst_vswr_s22 = m['vswr_s22_max']
        target_error_5pct_max = m.get('target_error_max', 0.0)
        worst_il = m['worst_il_db']
        max_vswr_deg = 0.0

    return {
        'vswr_5pct_max_s11': worst_vswr_s11,
        'vswr_5pct_max_s22': worst_vswr_s22,
        'target_error_5pct_max': target_error_5pct_max,
        'worst_il_5pct': worst_il,
        'vswr_sensitivity': max_vswr_deg,
    }


class FleetOptimizer:
    """
    Runs the 5-agent fleet optimization.

    Usage:
        optimizer = FleetOptimizer(app_state, progress_callback=print)
        result = optimizer.run()
    """

    def __init__(self, app_state, progress_callback=None):
        self.app_state = app_state
        self.progress_callback = progress_callback or (lambda msg: None)

    def _log(self, msg: str):
        self.progress_callback(msg)

    def _build_base_config(self) -> NetworkConfig:
        """Convert AppState to NetworkConfig."""
        cfg = NetworkConfig(
            freq_start_ghz=self.app_state.freq_start_ghz,
            freq_stop_ghz=self.app_state.freq_stop_ghz,
            freq_npoints=self.app_state.freq_npoints,
        )
        for fid, fc in self.app_state.files.items():
            cfg.networks[fid] = fc.file_path
            cfg.terminations[fid] = {}
            for pnum, pc in fc.ports.items():
                term = PortTermination()
                term.type = pc.term_type
                if pc.term_type in ('capacitor', 'inductor'):
                    term.component_path = pc.component_path
                elif pc.term_type in ('open/ind', 'open/cap', 'open/ind/cap'):
                    term.type = 'open'   # baseline; fleet will override per-combination
                elif pc.term_type in ('short/ind', 'short/cap', 'short/ind/cap'):
                    term.type = 'short'  # baseline; fleet will override per-combination
                elif pc.term_type == 'connect':
                    term.connect_to = (pc.connect_to_file, pc.connect_to_port)
                elif pc.term_type == 'signal':
                    term.signal_port_index = pc.signal_index
                cfg.terminations[fid][pnum] = term
        return cfg

    def _get_signal_freq_ranges(self, n_signal_ports: int) -> List[Tuple[float, float]]:
        """
        Build a list of (start_ghz, stop_ghz) for each non-antenna signal port (0-indexed).
        Length = n_signal_ports (ports 0..n_signal_ports-1).
        Falls back to global freq if a signal index is not in signal_freq_ranges.
        signal_index is 1-based (s1=1, s2=2, ...), port index is 0-based.
        """
        fallback = (self.app_state.freq_start_ghz,
                    self.app_state.freq_stop_ghz)
        sfr = getattr(self.app_state, 'signal_freq_ranges', {})
        result = []
        for i in range(n_signal_ports):
            sig_idx = i + 1  # 1-based signal index
            result.append(sfr.get(sig_idx, fallback))
        return result

    def _get_special_smith_targets(self, n_signal_ports: int) -> Dict[int, Tuple[float, float, float, float]]:
        """Return enabled special Smith targets for signal ports 1..n_signal_ports."""
        return coerce_special_smith_targets(
            getattr(self.app_state, 'special_smith_targets', {}),
            n_signal_ports,
        )

    def _get_candidate_components(
        self, term_type: str,
        ind_min_nh: float = 0.0, ind_max_nh: float = 10000.0,
        cap_min_pf: float = 0.0, cap_max_pf: float = 10000.0,
    ) -> List[dict]:
        """Get list of component candidates for a given type, filtered by value range.

        Limits from PortConfig are already rounded to 2 dp by the GUI spinboxes.
        A small EPS (1e-6) is added to the upper bound to absorb any residual
        floating-point imprecision so that exact boundary values are always included.
        """
        EPS = 1e-6  # < smallest meaningful component step (0.1 nH / 0.1 pF)

        def _ind_ok(i: dict) -> bool:
            v = i.get('value_nH', 0.0)
            return (ind_min_nh - EPS) <= v <= (ind_max_nh + EPS)

        def _cap_ok(c: dict) -> bool:
            v = c.get('value_pF', 0.0)
            return (cap_min_pf - EPS) <= v <= (cap_max_pf + EPS)

        if term_type == 'capacitor':
            return [{'name': c['name'], 'path': c['path'], 'comp_type': 'capacitor', 'value_pF': c.get('value_pF', 0.0)}
                    for c in list_capacitors() if _cap_ok(c)]
        elif term_type == 'inductor':
            return [{'name': i['name'], 'path': i['path'], 'comp_type': 'inductor', 'value_nH': i.get('value_nH', 0.0)}
                    for i in list_inductors() if _ind_ok(i)]
        elif term_type == 'open/ind':
            return [{'name': i['name'], 'path': i['path'], 'comp_type': 'inductor', 'value_nH': i.get('value_nH', 0.0)}
                    for i in list_inductors() if _ind_ok(i)]
        elif term_type == 'open/cap':
            return [{'name': c['name'], 'path': c['path'], 'comp_type': 'capacitor', 'value_pF': c.get('value_pF', 0.0)}
                    for c in list_capacitors() if _cap_ok(c)]
        elif term_type == 'open/ind/cap':
            caps = [{'name': c['name'], 'path': c['path'], 'comp_type': 'capacitor', 'value_pF': c.get('value_pF', 0.0)}
                    for c in list_capacitors() if _cap_ok(c)]
            inds = [{'name': i['name'], 'path': i['path'], 'comp_type': 'inductor', 'value_nH': i.get('value_nH', 0.0)}
                    for i in list_inductors() if _ind_ok(i)]
            return caps + inds
        # --- series variants (short baseline, port 1 of S2P connected to open) ---
        elif term_type == 'short/ind':
            return [{'name': i['name'], 'path': i['path'], 'comp_type': 'inductor_series', 'value_nH': i.get('value_nH', 0.0)}
                    for i in list_inductors() if _ind_ok(i)]
        elif term_type == 'short/cap':
            return [{'name': c['name'], 'path': c['path'], 'comp_type': 'capacitor_series', 'value_pF': c.get('value_pF', 0.0)}
                    for c in list_capacitors() if _cap_ok(c)]
        elif term_type == 'short/ind/cap':
            caps = [{'name': c['name'], 'path': c['path'], 'comp_type': 'capacitor_series', 'value_pF': c.get('value_pF', 0.0)}
                    for c in list_capacitors() if _cap_ok(c)]
            inds = [{'name': i['name'], 'path': i['path'], 'comp_type': 'inductor_series', 'value_nH': i.get('value_nH', 0.0)}
                    for i in list_inductors() if _ind_ok(i)]
            return caps + inds
        return []

    def _sweep_all_combinations(
        self, base_config: NetworkConfig, tunable_ports: List[tuple]
    ) -> List[dict]:
        """
        Sweep all combinations of components for tunable ports.
        Uses Rust extension if available, falls back to pure Python.
        Returns list of {assignments, metrics} dicts.
        """
        candidates_per_port = []
        for nid, pnum, ttype in tunable_ports:
            pc = self.app_state.files[nid].ports[pnum]
            comps = self._get_candidate_components(
                ttype,
                ind_min_nh=pc.ind_min_nh, ind_max_nh=pc.ind_max_nh,
                cap_min_pf=pc.cap_min_pf, cap_max_pf=pc.cap_max_pf,
            )
            candidates_per_port.append([None] + comps)  # None = open

        total = 1
        for c in candidates_per_port:
            total *= len(c)
        self._log(f"  Total combinations: {total:,}")

        if _RUST_AVAILABLE and len(tunable_ports) > 0:
            return self._sweep_rust(base_config, tunable_ports, candidates_per_port, total)
        else:
            if not _RUST_AVAILABLE:
                self._log(
                    "  [WARNING] rf_sweep Rust module not found, using Python fallback")
            return self._sweep_python(base_config, tunable_ports, candidates_per_port, total)

    def _sweep_rust(
        self, base_config: NetworkConfig, tunable_ports: List[tuple],
        candidates_per_port: List[list], total: int
    ) -> List[dict]:
        """Rust-accelerated sweep using pre-built base network."""
        import rf_sweep
        import numpy as np
        from .network_builder import build_base_network_for_fleet

        self._log("  [Rust] Building base network...")
        tunable_keys = [(nid, pnum) for nid, pnum, _ in tunable_ports]

        try:
            base_net, ordered_keys = build_base_network_for_fleet(
                base_config, tunable_keys)
            nfreq = len(base_net.frequency)
            f_ghz = base_net.frequency.f / 1e9
            n_signals = base_net.nports - len(tunable_ports)

            # Build per-signal eval ranges (one per signal port, including antenna)
            sfr_non_ant = self._get_signal_freq_ranges(
                n_signals - 1)  # non-ant signal ports only
            special_targets = self._get_special_smith_targets(n_signals)
            eval_ranges = []
            for start_ghz, stop_ghz in sfr_non_ant:
                mask_i = (f_ghz >= start_ghz) & (f_ghz <= stop_ghz)
                idx_i = np.where(mask_i)[0]
                if len(idx_i) > 0:
                    eval_ranges.append((int(idx_i[0]), int(idx_i[-1])))
                else:
                    eval_ranges.append((1, 0))

            # Antenna port = union of all non-ant bands
            valid_ranges = [r for r in eval_ranges if r[1] >= r[0]]
            if valid_ranges:
                ant_start_idx = min(r[0] for r in valid_ranges)
                ant_stop_idx = max(r[1] for r in valid_ranges)
            else:
                ant_start_idx, ant_stop_idx = 1, 0
            eval_ranges.append((ant_start_idx, ant_stop_idx))
        except Exception as e:
            self._log(
                f"  [Rust] Base network build failed: {e}, falling back to Python")
            return self._sweep_python(base_config, tunable_ports, candidates_per_port, total)

        if special_targets and not _RUST_TARGETS_AVAILABLE:
            self._log(
                "  [Rust] rf_sweep target API not found, falling back to Python for special Smith targets")
            return self._sweep_python(base_config, tunable_ports, candidates_per_port, total)

        # Check all ranges are valid
        if any(r[1] < r[0] for r in eval_ranges):
            self._log(
                "  [Rust] Empty or invalid eval range, falling back to Python")
            return self._sweep_python(base_config, tunable_ports, candidates_per_port, total)

        self._log(
            f"  [Rust] Base network: {base_net.nports} ports, {nfreq} freq points")
        self._log(f"  [Rust] Per-signal eval ranges: {eval_ranges}")

        # Build gamma arrays per tunable port: shape (n_cands, nfreq)
        # Row 0 = open (Γ = +1), rows 1..n_cands-1 = component gammas
        self._log("  [Rust] Pre-loading termination gammas...")
        term_gammas_re = []
        term_gammas_im = []
        all_candidates = []  # parallel list to candidates_per_port

        for (nid, pnum, ttype), comps in zip(tunable_ports, candidates_per_port):
            n_cands = len(comps)
            gamma_re = np.zeros((n_cands, nfreq), dtype=np.float64)
            gamma_im = np.zeros((n_cands, nfreq), dtype=np.float64)

            for c_idx, comp in enumerate(comps):
                if comp is None:
                    # Baseline: open (Γ = +1) for open/* types, short (Γ = -1) for short/* types
                    gamma_re[c_idx, :] = - \
                        1.0 if ttype.startswith('short/') else 1.0
                else:
                    # Build 1-port shunt termination, extract S11
                    from .network_builder import PortTermination
                    term = PortTermination(
                        type=comp.get('comp_type', ttype),
                        component_path=comp['path']
                    )
                    from .network_builder import NetworkBuilder
                    term_net_1port = NetworkBuilder._build_termination_network_static(
                        term, base_net.frequency
                    )
                    gamma_re[c_idx, :] = term_net_1port.s[:, 0, 0].real
                    gamma_im[c_idx, :] = term_net_1port.s[:, 0, 0].imag

            term_gammas_re.append(gamma_re)
            term_gammas_im.append(gamma_im)
            all_candidates.append(comps)

        # Call Rust
        self._log(
            f"  [Rust] Launching parallel sweep ({total:,} combinations)...")
        base_s = base_net.s  # (nfreq, N, N) complex128
        # n_signals already computed in the try block above

        if special_targets:
            target_gamma = build_target_gamma_matrix(
                f_ghz, n_signals, special_targets)
            (vswr_s11, vswr_s22, worst_il,
             target_s11, target_s22, combo_indices) = rf_sweep.sweep_terminations_parallel_targets(
                np.ascontiguousarray(base_s.real, dtype=np.float64),
                np.ascontiguousarray(base_s.imag, dtype=np.float64),
                [np.ascontiguousarray(g, dtype=np.float64)
                 for g in term_gammas_re],
                [np.ascontiguousarray(g, dtype=np.float64)
                 for g in term_gammas_im],
                eval_ranges,
                n_signals,
                np.ascontiguousarray(target_gamma.real, dtype=np.float64),
                np.ascontiguousarray(target_gamma.imag, dtype=np.float64),
            )
        else:
            vswr_s11, vswr_s22, worst_il, combo_indices = rf_sweep.sweep_terminations_parallel(
                np.ascontiguousarray(base_s.real, dtype=np.float64),
                np.ascontiguousarray(base_s.imag, dtype=np.float64),
                [np.ascontiguousarray(g, dtype=np.float64)
                 for g in term_gammas_re],
                [np.ascontiguousarray(g, dtype=np.float64)
                 for g in term_gammas_im],
                eval_ranges,
                n_signals,
            )
            target_s11 = (vswr_s11 - 1.0) / (vswr_s11 + 1.0)
            target_s22 = (vswr_s22 - 1.0) / (vswr_s22 + 1.0)
        self._log(
            f"  [Rust] Sweep complete: {len(vswr_s11):,} valid combinations")

        # Pack results into the same format as _sweep_python
        results = []
        for i in range(len(vswr_s11)):
            assignments = [all_candidates[p][combo_indices[i, p]]
                           for p in range(len(tunable_ports))]
            results.append({
                'assignments': assignments,
                'net': None,   # lazy: will be built on demand for the winner
                'vswr_s11_max': float(vswr_s11[i]),
                'vswr_s22_max': float(vswr_s22[i]),
                'target_error_s11_max': float(target_s11[i]),
                'target_error_s22_max': float(target_s22[i]),
                'target_error_max': float(max(target_s11[i], target_s22[i])),
                'target_error_spread': float(abs(target_s11[i] - target_s22[i])),
                'worst_il_db': float(worst_il[i]),
                'freq_ghz': [],
                's11_mag': [],
                's22_mag': [],
                's21_db': [],
            })
        return results

    def _sweep_python(
        self, base_config: NetworkConfig, tunable_ports: List[tuple],
        candidates_per_port: List[list], total: int
    ) -> List[dict]:
        """Pure-Python fallback sweep (original implementation)."""
        results = []
        sfr = None  # computed lazily from the first successfully built network
        special_targets = None
        for i, combo in enumerate(itertools.product(*candidates_per_port)):
            if i % max(1, total // 20) == 0:
                self._log(f"  Progress: {i}/{total} ({100*i//total}%)")
            try:
                cfg = _build_config_with_assignments(
                    base_config, tunable_ports, list(combo))
                net = build_network_from_config(cfg)
                if sfr is None:
                    sfr = self._get_signal_freq_ranges(net.nports - 1)
                    special_targets = self._get_special_smith_targets(
                        net.nports)
                ev = _evaluate_network(net, sfr, special_targets)
                results.append({
                    'assignments': list(combo),
                    'net': net,
                    **ev,
                })
            except Exception:
                pass
        self._log(f"  Valid evaluations: {len(results)}/{total}")
        return results

    def _count_components(self, assignments: List) -> int:
        """Count non-open assignments."""
        return sum(1 for a in assignments if a is not None)

    def _smith_spread(
        self,
        net: rf.Network,
        signal_freq_ranges: List[Tuple[float, float]],
        special_smith_targets=None,
    ) -> float:
        """
        Compute Smith chart spread across all signal ports relative to target.
        Lower = tighter cluster around the configured target locus.
        """
        f_ghz = net.frequency.f / 1e9
        ant = net.nports - 1
        target_gamma = build_target_gamma_matrix(
            f_ghz, net.nports, special_smith_targets or {})
        parts = []
        ant_mask = np.zeros(len(f_ghz), dtype=bool)
        for i in range(ant):
            if i < len(signal_freq_ranges):
                start, stop = signal_freq_ranges[i]
            else:
                start, stop = self.app_state.freq_start_ghz, self.app_state.freq_stop_ghz
            mask = (f_ghz >= start) & (f_ghz <= stop)
            ant_mask |= mask
            if np.any(mask):
                parts.append(net.s[mask, i, i] - target_gamma[i, mask])
        if np.any(ant_mask):
            parts.append(net.s[ant_mask, ant, ant] -
                         target_gamma[ant, ant_mask])
        if not parts:
            return 99.0
        pts = np.concatenate(parts)
        spread = (np.std(np.real(pts))**2 + np.std(np.imag(pts))**2 +
                  np.mean(np.abs(pts))**2)
        return float(spread)

    def _run_agent(self, agent_id: int, all_results: List[dict],
                   tunable_ports: List[tuple], base_config: NetworkConfig) -> AgentResult:
        """Run one agent: select best result according to strategy."""
        special_targets = self._get_special_smith_targets(99)

        if agent_id == 1:
            name = "Agent 1 - Min BOM"
            strategy = "Fewest components within 10% of the best achievable target match"
            # Base the floor on non-antenna signal port target distance only.
            # The antenna (common) port's VSWR is a secondary concern — it sees all bands
            # simultaneously and is harder to match independently.
            match_floor = min(r.get('target_error_s11_max',
                              r['vswr_s11_max']) for r in all_results)
            # Accept results within 10% above the floor.  This prevents "open" (0 components)
            # from winning just because it scrapes under a loose absolute threshold — it only
            # wins if it genuinely comes close to what any component can achieve.
            match_threshold = match_floor * 1.10
            near_optimal = [
                r for r in all_results
                if r.get('target_error_s11_max', r['vswr_s11_max']) <= match_threshold
            ]
            # Among near-optimal, prefer fewest components; break ties by signal+ant target error.
            best = min(near_optimal, key=lambda r: (
                self._count_components(r['assignments']),
                r.get('target_error_s11_max', r['vswr_s11_max']) +
                r.get('target_error_s22_max', r['vswr_s22_max'])
            ))

        elif agent_id == 2:
            name = "Agent 2 - Balance"
            strategy = "Balance low target mismatch and low insertion loss"
            # Score = normalize(target mismatch) + normalize(-il)
            matches = [r.get('target_error_max', max(
                r['vswr_s11_max'], r['vswr_s22_max'])) for r in all_results]
            ils = [r['worst_il_db'] for r in all_results]
            v_min, v_max = min(matches), max(matches)
            i_min, i_max = min(ils), max(ils)

            def score(r):
                v = r.get('target_error_max', max(
                    r['vswr_s11_max'], r['vswr_s22_max']))
                il = r['worst_il_db']
                nv = (v - v_min) / (v_max - v_min + 1e-9)
                # higher il_db = better
                ni = 1 - (il - i_min) / (i_max - i_min + 1e-9)
                return nv + ni
            best = min(all_results, key=score)

        elif agent_id == 3:
            name = "Agent 3 - Min Target"
            strategy = "Strictly minimize peak target mismatch across signal ports"
            # Minimize target distance at non-antenna signal ports (s1, s2, ...).
            # Antenna/common port target distance is a secondary objective.
            best = min(all_results, key=lambda r: (
                r.get('target_error_s11_max', r['vswr_s11_max']),
                r.get('target_error_s22_max', r['vswr_s22_max'])
            ))

        elif agent_id == 4:
            name = "Agent 4 - Smith Contour"
            strategy = "Minimize Smith chart contour area around the active target"
            # Check ALL results — previous agents may have lazily rebuilt some nets,
            # so checking only [0] is unreliable for Rust-path results.
            if all_results and all(r.get('net') is not None for r in all_results):
                first_net = all_results[0]['net']
                sfr_for_spread = self._get_signal_freq_ranges(
                    first_net.nports - 1)
                spread_targets = self._get_special_smith_targets(
                    first_net.nports)
                best = min(all_results,
                           key=lambda r: self._smith_spread(r['net'], sfr_for_spread, spread_targets))
            else:
                # Rust path: use target-error spread as proxy for Smith contour tightness.
                best = min(all_results,
                           key=lambda r: r.get('target_error_spread', abs(r['vswr_s11_max'] - r['vswr_s22_max'])) +
                           r.get('target_error_max', max(r['vswr_s11_max'], r['vswr_s22_max'])))

        elif agent_id == 5:
            name = "Agent 5 - Min IL"
            strategy = "Minimize insertion loss after meeting the active Smith target"
            # IL-only selection can choose a high-transmission candidate that misses the
            # requested special Smith target. Keep Agent 5 transmission-focused, but
            # constrain it to candidates near the best achievable target mismatch first.
            target_floor = min(
                r.get('target_error_max', max(
                    r['vswr_s11_max'], r['vswr_s22_max']))
                for r in all_results
            )
            target_threshold = target_floor + max(0.005, 0.15 * target_floor)
            target_matched = [
                r for r in all_results
                if r.get('target_error_max', max(r['vswr_s11_max'], r['vswr_s22_max'])) <= target_threshold
            ]
            if not target_matched:
                target_matched = all_results
            best = max(target_matched, key=lambda r: (
                r['worst_il_db'],
                -r.get('target_error_max',
                       max(r['vswr_s11_max'], r['vswr_s22_max']))
            ))

        else:
            raise ValueError(f"Unknown agent_id {agent_id}")

        # If net was not pre-computed (Rust path), build it now for the winner.
        # Work on a shallow copy so we don't mutate the shared all_results list
        # (other agents still need clean net=None entries to detect the Rust path).
        best = dict(best)
        sfr = None  # will be set below
        if best.get('net') is None:
            try:
                cfg = _build_config_with_assignments(
                    base_config, tunable_ports, best['assignments'])
                best['net'] = build_network_from_config(cfg)
                n_sig_ports = best['net'].nports - 1
                sfr = self._get_signal_freq_ranges(n_sig_ports)
                special_targets = self._get_special_smith_targets(
                    best['net'].nports)
                ev = _evaluate_network(best['net'], sfr, special_targets)
                best.update(ev)
            except Exception as e:
                self._log(f"  [Agent {agent_id}] Lazy rebuild failed: {e}")

        # Ensure sfr is set (Python path or after Rust-path rebuild)
        if sfr is None:
            if best.get('net') is not None:
                sfr = self._get_signal_freq_ranges(best['net'].nports - 1)
                special_targets = self._get_special_smith_targets(
                    best['net'].nports)
            else:
                sfr = [(self.app_state.freq_start_ghz,
                        self.app_state.freq_stop_ghz)]
                special_targets = self._get_special_smith_targets(1)

        # Count components
        count = self._count_components(best['assignments'])

        # Build assignment objects
        assignments = []
        for (nid, pnum, ttype), comp in zip(tunable_ports, best['assignments']):
            if comp is None:
                # Baseline depends on the original port type: short/* → short, open/* → open
                baseline = 'short' if ttype.startswith('short/') else 'open'
                assignments.append(ComponentAssignment(
                    network_id=nid, port_index=pnum, term_type=baseline
                ))
            else:
                # For open/ind/cap ports, use the resolved comp_type
                resolved_type = comp.get('comp_type', ttype)
                assignments.append(ComponentAssignment(
                    network_id=nid, port_index=pnum, term_type=resolved_type,
                    component_name=comp['name'], component_path=comp['path']
                ))

        # Tolerance analysis
        try:
            tol = _evaluate_with_tolerance(
                base_config, tunable_ports, best['assignments'],
                sfr, special_targets
            )
        except Exception as e:
            self._log(f"  [Agent {agent_id}] Tolerance analysis failed: {e}")
            tol = {'vswr_5pct_max_s11': 0.0, 'vswr_5pct_max_s22': 0.0,
                   'worst_il_5pct': 0.0, 'vswr_sensitivity': 0.0,
                   'target_error_5pct_max': 0.0}

        return AgentResult(
            agent_id=agent_id,
            agent_name=name,
            strategy=strategy,
            assignments=assignments,
            vswr_s11_max=best['vswr_s11_max'],
            vswr_s22_max=best['vswr_s22_max'],
            worst_il_db=best['worst_il_db'],
            component_count=count,
            target_error_s11_max=best.get('target_error_s11_max', 0.0),
            target_error_s22_max=best.get('target_error_s22_max', 0.0),
            target_error_max=best.get('target_error_max', 0.0),
            vswr_s11_5pct_max=tol['vswr_5pct_max_s11'],
            vswr_s22_5pct_max=tol['vswr_5pct_max_s22'],
            target_error_5pct_max=tol['target_error_5pct_max'],
            worst_il_5pct_db=tol['worst_il_5pct'],
            vswr_sensitivity=tol['vswr_sensitivity'],
            vswr_spread=abs(best['vswr_s11_max'] - best['vswr_s22_max']),
            target_error_spread=best.get('target_error_spread', 0.0),
            freq_ghz=best['freq_ghz'],
            s11_mag=best['s11_mag'],
            s22_mag=best['s22_mag'],
            s21_db=best['s21_db'],
            s11_re=best.get('s11_re', []),
            s11_im=best.get('s11_im', []),
            s22_re=best.get('s22_re', []),
            s22_im=best.get('s22_im', []),
            sii_mag_list=best.get('sii_mag_list', []),
            sij_db_list=best.get('sij_db_list',  []),
            sii_re_list=best.get('sii_re_list',  []),
            sii_im_list=best.get('sii_im_list',  []),
            freq_ghz_list=best.get('freq_ghz_list', []),
            signal_freq_ranges_list=[[s, e] for s, e in sfr],
            special_smith_targets_list=[
                [sig_idx, start, stop, resistance, reactance]
                for sig_idx, (start, stop, resistance, reactance) in sorted(special_targets.items())
            ],
            global_freq_start=self.app_state.freq_start_ghz,
            global_freq_stop=self.app_state.freq_stop_ghz,
        )

    def _compute_risk_scores(self, agent_results: List[AgentResult]) -> Dict[str, float]:
        """
        risk_score = 0.30 × normalize(worst_target_error_5pct)
                   + 0.25 × normalize(component_count)
                   + 0.20 × normalize(vswr_sensitivity)
                   + 0.15 × normalize(abs(worst_il_5pct))
                   + 0.10 × normalize(target_error_spread)
        """
        def normalize(vals):
            mn, mx = min(vals), max(vals)
            if mx == mn:
                return [0.0] * len(vals)
            return [(v - mn) / (mx - mn) for v in vals]

        worst_match = [
            r.target_error_5pct_max if r.target_error_5pct_max else r.target_error_max
            for r in agent_results
        ]
        counts = [float(r.component_count) for r in agent_results]
        sens = [r.vswr_sensitivity for r in agent_results]
        ils = [abs(r.worst_il_5pct_db) for r in agent_results]
        spreads = [
            r.target_error_spread if r.target_error_spread else r.vswr_spread for r in agent_results]

        n_match = normalize(worst_match)
        n_cnt = normalize(counts)
        n_sens = normalize(sens)
        n_il = normalize(ils)
        n_spr = normalize(spreads)

        scores = {}
        for i, r in enumerate(agent_results):
            score = (0.30 * n_match[i] + 0.25 * n_cnt[i] + 0.20 * n_sens[i] +
                     0.15 * n_il[i] + 0.10 * n_spr[i])
            scores[r.agent_name] = round(score, 4)
            r.risk_score = scores[r.agent_name]

        return scores

    def _draw_special_smith_targets(self, ax, targets: List[List[float]], colors: List[str]):
        """Draw configured special target markers on a Smith chart axis."""
        for target in targets:
            if len(target) < 5:
                continue
            sig_idx, start, stop, resistance, reactance = target[:5]
            gamma = impedance_to_gamma(resistance, reactance)
            color = colors[(int(sig_idx) - 1) % len(colors)]
            ax.plot(
                [gamma.real], [gamma.imag],
                marker='x', markersize=8, markeredgewidth=1.6,
                color=color, linestyle='None',
                label=(
                    f'S{int(sig_idx)}{int(sig_idx)} target '
                    f'{format_impedance(resistance, reactance)} '
                    f'[{start:.2f}-{stop:.2f}]'
                ),
            )

    def _save_agent_plots(self, result: AgentResult, output_dir: Path):
        """Save Smith chart, VSWR, and IL plots for one agent."""
        fig, axes = plt.subplots(1, 3, figsize=(15, 4))
        fig.suptitle(
            f"{result.agent_name}\nRisk Score: {result.risk_score:.3f}", fontsize=11)

        COLORS = ['blue', 'red', 'green', 'darkorange']
        freq = np.array(result.freq_ghz)
        n_ports = len(result.sii_mag_list) if result.sii_mag_list else 2
        ant = n_ports - 1

        # Smith chart
        ax = axes[0]
        draw_smith_chart_background(ax, 'Smith Chart')
        ax.add_patch(Circle((0, 0), 1/3, fill=False, linestyle='--',
                     color='k', linewidth=0.8, label='VSWR=2'))
        if result.sii_re_list:
            BAND_STYLES = ['-', '--', ':', '-.']
            for i, (re_vals, im_vals) in enumerate(zip(result.sii_re_list, result.sii_im_list)):
                color = COLORS[i % len(COLORS)]
                if i == ant and ant > 1 and result.signal_freq_ranges_list:
                    # One trace per band with distinct linestyle
                    freq_arr = np.array(result.freq_ghz_list[ant] if i < len(
                        result.freq_ghz_list) else result.freq_ghz)
                    re_arr = np.array(re_vals)
                    im_arr = np.array(im_vals)
                    for band_i, (band_start, band_stop) in enumerate(result.signal_freq_ranges_list):
                        bm = (freq_arr >= band_start) & (freq_arr <= band_stop)
                        if not np.any(bm):
                            continue
                        lbl = f'S{ant+1}{ant+1}[s{band_i+1}:{band_start:.2f}\u2013{band_stop:.2f}]'
                        ax.plot(re_arr[bm], im_arr[bm],
                                color=color, linestyle=BAND_STYLES[band_i % len(
                                    BAND_STYLES)],
                                lw=1.5, label=lbl)
                else:
                    tag = " ANT" if i == ant else ""
                    ax.plot(re_vals, im_vals, color=color,
                            lw=1.5, label=f'S{i+1}{i+1}{tag}')
        else:
            # Fallback legacy fields
            if result.s11_re:
                ax.plot(result.s11_re, result.s11_im,
                        'b-', lw=1.5, label='S11')
            if result.s22_re:
                ax.plot(result.s22_re, result.s22_im,
                        'r-', lw=1.5, label='S22')
        self._draw_special_smith_targets(
            ax, result.special_smith_targets_list, COLORS)
        ax.legend(fontsize=7)

        # VSWR
        ax = axes[1]
        if result.sii_mag_list:
            for i, mag in enumerate(result.sii_mag_list):
                freq_i = result.freq_ghz_list[i] if i < len(
                    result.freq_ghz_list) else result.freq_ghz
                freq_arr = np.array(freq_i)
                s_arr = np.clip(np.array(mag), 0, 0.9999)
                vswr_i = (1 + s_arr) / (1 - s_arr)
                color = COLORS[i % len(COLORS)]
                tag = " ANT" if i == ant else ""
                if i == ant and ant > 1:
                    freq_arr, vswr_i = _nan_separate(freq_arr, vswr_i)[:2]
                ax.plot(freq_arr, vswr_i, color=color,
                        label=f'VSWR S{i+1}{i+1}{tag}')
        else:
            s11 = np.array(result.s11_mag)
            s22 = np.array(result.s22_mag)
            ax.plot(freq, (1+s11)/(1-np.clip(s11, 0, 0.9999)), 'b-',
                    label=f'VSWR S11 (max={result.vswr_s11_max:.2f})')
            ax.plot(freq, (1+s22)/(1-np.clip(s22, 0, 0.9999)), 'r-',
                    label=f'VSWR S22 (max={result.vswr_s22_max:.2f})')
        ax.axhline(1.4, color='g', linestyle='--', lw=0.8, label='VSWR=1.4')
        ax.axhline(2.0, color='orange', linestyle='--',
                   lw=0.8, label='VSWR=2.0')
        ax.set_xlabel('Frequency (GHz)')
        ax.set_ylabel('VSWR')
        ax.set_title('VSWR')
        ax.legend(fontsize=7)
        ax.grid(True, alpha=0.3)
        if result.global_freq_start or result.global_freq_stop:
            ax.set_xlim(result.global_freq_start, result.global_freq_stop)

        # IL
        ax = axes[2]
        if result.sij_db_list:
            for i, il in enumerate(result.sij_db_list):
                freq_i = result.freq_ghz_list[i] if i < len(
                    result.freq_ghz_list) else result.freq_ghz
                ax.plot(np.array(freq_i), il, color=COLORS[i % len(COLORS)],
                        label=f'S{ant+1}{i+1} IL')
        else:
            ax.plot(freq, result.s21_db, 'g-',
                    label=f'IL (worst={result.worst_il_db:.2f}dB)')
        ax.set_xlabel('Frequency (GHz)')
        ax.set_ylabel('dB')
        ax.set_title('Insertion Loss')
        ax.legend(fontsize=7)
        ax.grid(True, alpha=0.3)
        if result.global_freq_start or result.global_freq_stop:
            ax.set_xlim(result.global_freq_start, result.global_freq_stop)

        plt.tight_layout()
        fname = output_dir / \
            f"agent_{result.agent_id}_{result.agent_name.replace(' ', '_').replace('-', '')}.png"
        plt.savefig(fname, dpi=100, bbox_inches='tight')
        plt.close(fig)
        return str(fname)

    def _save_comparison_plot(self, agent_results: List[AgentResult], output_dir: Path):
        """Save comparison bar chart of all 5 agents."""
        names = [f"A{r.agent_id}" for r in agent_results]

        fig, axes = plt.subplots(2, 3, figsize=(15, 8))
        fig.suptitle('Fleet Agent Comparison', fontsize=13)

        metrics = [
            ('VSWR S11 Max', [r.vswr_s11_max for r in agent_results], 'blue'),
            ('VSWR S22 Max', [r.vswr_s22_max for r in agent_results], 'red'),
            ('Worst IL (dB)', [r.worst_il_db for r in agent_results], 'green'),
            ('Component Count', [
             r.component_count for r in agent_results], 'purple'),
            ('VSWR under ±5%', [
             max(r.vswr_s11_5pct_max, r.vswr_s22_5pct_max) for r in agent_results], 'orange'),
            ('Risk Score', [r.risk_score for r in agent_results], 'darkred'),
        ]

        for ax, (title, vals, color) in zip(axes.flat, metrics):
            bars = ax.bar(names, vals, color=color, alpha=0.7)
            ax.set_title(title)
            ax.set_ylabel(title)
            for bar, v in zip(bars, vals):
                ax.text(bar.get_x() + bar.get_width()/2, bar.get_height(),
                        f'{v:.2f}', ha='center', va='bottom', fontsize=8)

        plt.tight_layout()
        fname = output_dir / "fleet_comparison.png"
        plt.savefig(fname, dpi=100, bbox_inches='tight')
        plt.close(fig)
        return str(fname)

    def _save_final_decision_plot(self, winner: AgentResult, output_dir: Path):
        """Save final decision Smith chart + VSWR + IL."""
        fig, axes = plt.subplots(1, 3, figsize=(15, 5))
        fig.suptitle(
            f'FINAL DECISION: {winner.agent_name}\n'
            f'Risk Score: {winner.risk_score:.3f} | Strategy: {winner.strategy}',
            fontsize=10
        )

        COLORS = ['blue', 'red', 'green', 'darkorange']
        freq = np.array(winner.freq_ghz)
        n_ports = len(winner.sii_mag_list) if winner.sii_mag_list else 2
        ant = n_ports - 1

        # Smith — plot actual Sii locus for all ports
        ax = axes[0]
        draw_smith_chart_background(ax, 'Smith Chart (Final)')
        ax.add_patch(Circle((0, 0), 1/3, fill=False, linestyle='--',
                     color='k', linewidth=1.2, label='VSWR=2'))
        if winner.sii_re_list:
            BAND_STYLES = ['-', '--', ':', '-.']
            for i, (re_vals, im_vals) in enumerate(zip(winner.sii_re_list, winner.sii_im_list)):
                color = COLORS[i % len(COLORS)]
                if i == ant and ant > 1 and winner.signal_freq_ranges_list:
                    freq_arr = np.array(winner.freq_ghz_list[ant] if i < len(
                        winner.freq_ghz_list) else winner.freq_ghz)
                    re_arr = np.array(re_vals)
                    im_arr = np.array(im_vals)
                    for band_i, (band_start, band_stop) in enumerate(winner.signal_freq_ranges_list):
                        bm = (freq_arr >= band_start) & (freq_arr <= band_stop)
                        if not np.any(bm):
                            continue
                        lbl = f'S{ant+1}{ant+1}[s{band_i+1}:{band_start:.2f}\u2013{band_stop:.2f}]'
                        ax.plot(re_arr[bm], im_arr[bm],
                                color=color, linestyle=BAND_STYLES[band_i % len(
                                    BAND_STYLES)],
                                lw=2, label=lbl)
                else:
                    tag = " ANT" if i == ant else ""
                    ax.plot(re_vals, im_vals, color=color,
                            lw=2, label=f'S{i+1}{i+1}{tag}')
        else:
            if winner.s11_re:
                ax.plot(np.array(winner.s11_re), np.array(
                    winner.s11_im), 'b-', lw=2, label='S11')
            if winner.s22_re:
                ax.plot(np.array(winner.s22_re), np.array(
                    winner.s22_im), 'r-', lw=2, label='S22')
        self._draw_special_smith_targets(
            ax, winner.special_smith_targets_list, COLORS)
        ax.legend(fontsize=8)

        # VSWR
        ax = axes[1]
        if winner.sii_mag_list:
            for i, mag in enumerate(winner.sii_mag_list):
                freq_i = winner.freq_ghz_list[i] if i < len(
                    winner.freq_ghz_list) else winner.freq_ghz
                freq_arr = np.array(freq_i)
                s_arr = np.clip(np.array(mag), 0, 0.9999)
                vswr_i = (1 + s_arr) / (1 - s_arr)
                color = COLORS[i % len(COLORS)]
                tag = " ANT" if i == ant else ""
                if i == ant and ant > 1:
                    freq_arr, vswr_i = _nan_separate(freq_arr, vswr_i)[:2]
                ax.plot(freq_arr, vswr_i, color=color,
                        label=f'VSWR S{i+1}{i+1}{tag}')
        else:
            s11 = np.array(winner.s11_mag)
            s22 = np.array(winner.s22_mag)
            ax.plot(freq, (1+s11)/(1-np.clip(s11, 0, 0.9999)), 'b-',
                    label=f'VSWR S11 (max={winner.vswr_s11_max:.2f})')
            ax.plot(freq, (1+s22)/(1-np.clip(s22, 0, 0.9999)), 'r-',
                    label=f'VSWR S22 (max={winner.vswr_s22_max:.2f})')
        ax.axhline(1.4, color='g', linestyle='--', lw=1, label='VSWR=1.4')
        ax.axhline(2.0, color='orange', linestyle='--', lw=1, label='VSWR=2.0')
        ax.set_xlabel('Frequency (GHz)')
        ax.set_ylabel('VSWR')
        ax.set_title('VSWR (Final)')
        ax.legend()
        ax.grid(True, alpha=0.3)
        if winner.global_freq_start or winner.global_freq_stop:
            ax.set_xlim(winner.global_freq_start, winner.global_freq_stop)

        # IL
        ax = axes[2]
        if winner.sij_db_list:
            for i, il in enumerate(winner.sij_db_list):
                freq_i = winner.freq_ghz_list[i] if i < len(
                    winner.freq_ghz_list) else winner.freq_ghz
                ax.plot(np.array(freq_i), il, color=COLORS[i % len(COLORS)],
                        label=f'S{ant+1}{i+1} IL')
        else:
            ax.plot(freq, winner.s21_db, 'g-',
                    label=f'IL (worst={winner.worst_il_db:.2f}dB)')
        ax.set_xlabel('Frequency (GHz)')
        ax.set_ylabel('dB')
        ax.set_title('Insertion Loss (Final)')
        ax.legend()
        ax.grid(True, alpha=0.3)
        if winner.global_freq_start or winner.global_freq_stop:
            ax.set_xlim(winner.global_freq_start, winner.global_freq_stop)

        plt.tight_layout()
        fname = output_dir / "final_decision.png"
        plt.savefig(fname, dpi=100, bbox_inches='tight')
        plt.close(fig)
        return str(fname)

    def _save_report(self, result: FleetResult, output_dir: Path):
        """Save Markdown report."""
        winner = next(
            r for r in result.agent_results if r.agent_id == result.winner_agent_id)

        lines = [
            "# RF Network Cascade Optimization Report",
            "",
            "## Executive Summary",
            "",
            f"**Winner:** {winner.agent_name}",
            f"**Strategy:** {winner.strategy}",
            f"**Risk Score:** {winner.risk_score:.4f} (lower = better)",
            f"**Reason:** {result.winner_reason}",
            "",
            "## Comparison Table",
            "",
            "| Agent | VSWR S11 Max | VSWR S22 Max | Target Error | Worst IL (dB) | Components | Target Error ±5% | Risk Score |",
            "|-------|-------------|-------------|--------------|---------------|------------|------------------|------------|",
        ]
        for r in result.agent_results:
            lines.append(
                f"| {r.agent_name} | {r.vswr_s11_max:.3f} | {r.vswr_s22_max:.3f} | "
                f"{r.target_error_max:.4f} | {r.worst_il_db:.2f} | {r.component_count} | "
                f"{r.target_error_5pct_max:.4f} | {r.risk_score:.4f} |"
            )

        if winner.special_smith_targets_list:
            lines += [
                "",
                "## Special Smith Targets",
                "",
            ]
            for sig_idx, start, stop, resistance, reactance in winner.special_smith_targets_list:
                gamma = impedance_to_gamma(resistance, reactance)
                lines.append(
                    f"- **S{int(sig_idx)}{int(sig_idx)}**: {format_impedance(resistance, reactance)} "
                    f"from {start:.3f} to {stop:.3f} GHz "
                    f"(gamma={gamma.real:+.4f}{gamma.imag:+.4f}j)"
                )

        lines += [
            "",
            "## Final Component Assignments",
            "",
        ]
        for a in winner.assignments:
            if a.term_type == 'open':
                lines.append(f"- **{a.network_id} Port {a.port_index}**: Open")
            elif a.term_type == 'short':
                lines.append(
                    f"- **{a.network_id} Port {a.port_index}**: Short")
            else:
                lines.append(
                    f"- **{a.network_id} Port {a.port_index}**: "
                    f"{a.term_type.capitalize()} - `{a.component_name}`"
                )

        lines += [
            "",
            "## Individual Agent Results",
            "",
        ]
        for r in result.agent_results:
            lines += [
                f"### {r.agent_name}",
                f"- Strategy: {r.strategy}",
                f"- VSWR S11 max: {r.vswr_s11_max:.3f}",
                f"- VSWR S22 max: {r.vswr_s22_max:.3f}",
                f"- Target error max: {r.target_error_max:.4f}",
                f"- Worst IL: {r.worst_il_db:.2f} dB",
                f"- Components: {r.component_count}",
                f"- Risk Score: {r.risk_score:.4f}",
                "",
            ]

        report_path = output_dir / "fleet_report.md"
        report_path.write_text("\n".join(lines), encoding='utf-8')
        return str(report_path)

    def run(self, output_dir: Optional[str] = None) -> FleetResult:
        """
        Run the full fleet optimization.
        Returns FleetResult with all agent results and the winner.
        """
        if output_dir is None:
            output_dir = Path(
                self.app_state.files[next(
                    iter(self.app_state.files))].file_path
            ).parent / "fleet_results"
        else:
            output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)

        self._log("=== Fleet Optimizer Started ===")

        # Build base config
        base_config = self._build_base_config()

        # Identify tunable ports
        tunable_ports = _get_tunable_ports(self.app_state)
        self._log(f"Tunable ports: {len(tunable_ports)}")
        for t in tunable_ports:
            self._log(f"  {t[0]} Port {t[1]} ({t[2]})")

        if not tunable_ports:
            self._log("No tunable ports found - nothing to optimize.")
            raise ValueError(
                "No tunable (capacitor/inductor) ports defined in configuration.")

        # Sweep all combinations (shared across all agents)
        self._log("\n[Phase 1] Sweeping all component combinations...")
        all_results = self._sweep_all_combinations(base_config, tunable_ports)

        if not all_results:
            raise ValueError(
                "No valid network configurations found during sweep.")

        # Run all 5 agents
        self._log("\n[Phase 2] Running 5 optimization agents...")
        agent_results = []
        for agent_id in range(1, 6):
            self._log(f"\n  Running Agent {agent_id}...")
            try:
                result = self._run_agent(
                    agent_id, all_results, tunable_ports, base_config)
                agent_results.append(result)
                self._log(
                    f"  ✓ {result.agent_name}: VSWR S11={result.vswr_s11_max:.2f}, "
                    f"S22={result.vswr_s22_max:.2f}, IL={result.worst_il_db:.2f}dB, "
                    f"TargetErr={result.target_error_max:.4f}, Components={result.component_count}"
                )
            except Exception as e:
                self._log(f"  ✗ Agent {agent_id} failed: {e}")

        # Compute risk scores
        self._log("\n[Phase 3] Computing risk scores...")
        risk_scores = self._compute_risk_scores(agent_results)
        for name, score in risk_scores.items():
            self._log(f"  {name}: {score:.4f}")

        # Select winner (lowest risk score)
        winner = min(agent_results, key=lambda r: r.risk_score)
        runner_up = sorted(agent_results, key=lambda r: r.risk_score)[
            1] if len(agent_results) > 1 else None

        reason = (
            f"{winner.agent_name} achieves the lowest production risk score ({winner.risk_score:.4f}). "
            f"It uses {winner.component_count} component(s), "
            f"VSWR S11={winner.vswr_s11_max:.2f}, S22={winner.vswr_s22_max:.2f}, "
            f"target error={winner.target_error_max:.4f}, "
            f"IL={winner.worst_il_db:.2f}dB, "
            f"sensitivity under ±5% tolerance={winner.vswr_sensitivity:.3f}."
        )
        if runner_up:
            reason += f" Runner-up: {runner_up.agent_name} (risk={runner_up.risk_score:.4f})."

        self._log(f"\n[Principal Agent] Winner: {winner.agent_name}")
        self._log(f"  Reason: {reason}")

        fleet_result = FleetResult(
            agent_results=agent_results,
            winner_agent_id=winner.agent_id,
            winner_reason=reason,
            risk_scores=risk_scores,
        )

        # Save outputs
        self._log("\n[Phase 4] Saving results...")

        # Per-agent JSON + plots
        for r in agent_results:
            json_path = output_dir / f"agent_{r.agent_id}_result.json"
            data = {
                'agent_name': r.agent_name,
                'strategy': r.strategy,
                'vswr_s11_max': r.vswr_s11_max,
                'vswr_s22_max': r.vswr_s22_max,
                'target_error_s11_max': r.target_error_s11_max,
                'target_error_s22_max': r.target_error_s22_max,
                'target_error_max': r.target_error_max,
                'worst_il_db': r.worst_il_db,
                'component_count': r.component_count,
                'risk_score': r.risk_score,
                'special_smith_targets': r.special_smith_targets_list,
                'assignments': [
                    {'network_id': a.network_id, 'port_index': a.port_index,
                     'term_type': a.term_type, 'component_name': a.component_name,
                     'component_path': a.component_path}
                    for a in r.assignments
                ],
            }
            json_path.write_text(json.dumps(data, indent=2))
            self._save_agent_plots(r, output_dir)

        # Comparison + final plots
        self._save_comparison_plot(agent_results, output_dir)
        self._save_final_decision_plot(winner, output_dir)
        self._save_report(fleet_result, output_dir)

        self._log(f"\n✓ Results saved to: {output_dir}")
        self._log("=== Fleet Optimizer Complete ===")

        return fleet_result
