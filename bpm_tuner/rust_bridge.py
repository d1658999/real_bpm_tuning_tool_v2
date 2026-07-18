from __future__ import annotations

import csv
import shutil
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np


class RustKernelError(RuntimeError):
    pass


class RustKernelCancelled(RustKernelError):
    pass


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    bom_count: int
    vswr_non_ant: float
    vswr_ant: float
    worst_il_db: float
    smith_score: float
    target_non_ant: float
    target_ant: float
    target_spread: float


@dataclass(frozen=True)
class SweepScore:
    vswr_non_ant: float
    vswr_ant: float
    worst_il_db: float
    target_non_ant: float
    target_ant: float
    combination: tuple[int, ...]

    @property
    def target_max(self) -> float:
        return max(self.target_non_ant, self.target_ant)

    @property
    def target_spread(self) -> float:
        return abs(self.target_non_ant - self.target_ant)


class RustOptimizer:
    """Build and invoke the exhaustive Rust RF sweep and ranking kernel."""

    _SWEEP_INPUT_MAGIC = b"BPMSWP02"
    _SWEEP_OUTPUT_MAGIC = b"BPMOUT01"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        suffix = ".exe" if sys.platform == "win32" else ""
        self.executable = (
            self.root / "rust_optimizer" / "target" / "release" / f"bpm-ranking-optimizer{suffix}"
        )

    def ensure_built(self) -> Path:
        sources = [
            self.root / "rust_optimizer" / "Cargo.toml",
            *sorted((self.root / "rust_optimizer" / "src").glob("*.rs")),
        ]
        if self.executable.exists() and self.executable.stat().st_mtime >= max(
            source.stat().st_mtime for source in sources
        ):
            return self.executable
        cargo = shutil.which("cargo")
        if not cargo:
            raise RustKernelError(
                "Rust optimizer is not built and Cargo was not found. Install Rust, then run "
                "`cargo build --release --manifest-path rust_optimizer/Cargo.toml`."
            )
        completed = subprocess.run(
            [cargo, "build", "--release", "--manifest-path", str(self.root / "rust_optimizer" / "Cargo.toml")],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode or not self.executable.exists():
            raise RustKernelError(f"Could not build the Rust optimizer: {completed.stderr.strip()}")
        return self.executable

    def rank(self, strategy: str, candidates: list[CandidateScore]) -> str:
        if not candidates:
            raise RustKernelError("At least one evaluated candidate is required.")
        executable = self.ensure_built()
        work = self.root / ".bpm_work"
        work.mkdir(exist_ok=True)
        input_path = work / f"candidates-{uuid.uuid4().hex}.tsv"
        try:
            with input_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
                writer.writerow(
                    [
                        "candidate_id",
                        "bom_count",
                        "vswr_non_ant",
                        "vswr_ant",
                        "worst_il_db",
                        "smith_score",
                        "target_non_ant",
                        "target_ant",
                        "target_spread",
                    ]
                )
                for item in candidates:
                    writer.writerow(
                        [
                            item.candidate_id,
                            item.bom_count,
                            item.vswr_non_ant,
                            item.vswr_ant,
                            item.worst_il_db,
                            item.smith_score,
                            item.target_non_ant,
                            item.target_ant,
                            item.target_spread,
                        ]
                    )
            completed = subprocess.run(
                [str(executable), strategy, str(input_path)],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode:
                raise RustKernelError(completed.stderr.strip() or "Rust optimizer failed.")
            winner = completed.stdout.strip()
            if winner not in {item.candidate_id for item in candidates}:
                raise RustKernelError(f"Rust optimizer returned an unknown candidate: {winner!r}")
            return winner
        finally:
            input_path.unlink(missing_ok=True)

    def sweep(
        self,
        base_s: np.ndarray,
        termination_gammas: list[np.ndarray],
        evaluation_ranges: list[tuple[int, int]],
        target_gamma: np.ndarray,
        cancel_callback: Callable[[], bool] | None = None,
        *,
        return_winners: bool = False,
        termination_component_counts: list[np.ndarray] | None = None,
    ) -> list[SweepScore]:
        """Evaluate every termination combination in parallel inside Rust.

        The compatibility default returns every score. Fleet optimization uses
        ``return_winners=True`` so Rust still evaluates the complete Cartesian
        product but retains only the five exact strategy winners. This avoids
        materializing hundreds of megabytes of duplicate Python/Rust result
        state for a large production sweep.
        """
        executable = self.ensure_built()
        base_s = np.asarray(base_s, dtype=np.complex128)
        target_gamma = np.asarray(target_gamma, dtype=np.complex128)
        if base_s.ndim != 3 or base_s.shape[1] != base_s.shape[2]:
            raise RustKernelError("The base S-matrix must have shape (frequency, port, port).")
        nfreq, nports, _ = base_s.shape
        nsignals = len(evaluation_ranges)
        if nsignals < 2 or nports != nsignals + len(termination_gammas):
            raise RustKernelError("Sweep signal/tunable port dimensions do not match the base network.")
        if target_gamma.shape != (nsignals, nfreq):
            raise RustKernelError("Target gamma must have shape (signal_port, frequency).")
        normalized_gammas: list[np.ndarray] = []
        for values in termination_gammas:
            array = np.asarray(values, dtype=np.complex128)
            if array.ndim != 2 or array.shape[0] < 1 or array.shape[1] != nfreq:
                raise RustKernelError("Each termination matrix must have shape (candidate, frequency).")
            normalized_gammas.append(array)
        if termination_component_counts is None:
            normalized_costs = [np.ones(values.shape[0], dtype=np.uint64) for values in normalized_gammas]
        else:
            if len(termination_component_counts) != len(normalized_gammas):
                raise RustKernelError("Component-count metadata must match tunable ports.")
            normalized_costs = []
            for values, costs in zip(normalized_gammas, termination_component_counts, strict=True):
                array = np.asarray(costs, dtype=np.uint64)
                if array.ndim != 1 or array.shape[0] != values.shape[0]:
                    raise RustKernelError("Each component-count vector must match its gamma rows.")
                normalized_costs.append(array)
        if return_winners and any(np.any(costs > 1) for costs in normalized_costs):
            raise RustKernelError("Component-count metadata must contain only zero or one.")

        work = self.root / ".bpm_work"
        work.mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        input_path = work / f"sweep-{token}.bin"
        output_path = work / f"sweep-{token}.out"
        try:
            payload = bytearray(self._SWEEP_INPUT_MAGIC)
            payload.extend(
                struct.pack(
                    "<QQQQQ",
                    nfreq,
                    nports,
                    nsignals,
                    len(normalized_gammas),
                    int(return_winners),
                )
            )
            for start, stop in evaluation_ranges:
                if not 0 <= start <= stop < nfreq:
                    raise RustKernelError("Sweep evaluation ranges must be valid inclusive indexes.")
                payload.extend(struct.pack("<QQ", start, stop))
            payload.extend(self._complex_bytes(base_s))
            payload.extend(self._complex_bytes(target_gamma))
            for index, values in enumerate(normalized_gammas):
                payload.extend(struct.pack("<Q", values.shape[0]))
                costs = normalized_costs[index]
                payload.extend(struct.pack(f"<{len(costs)}Q", *[int(value) for value in costs]))
                payload.extend(self._complex_bytes(values))
            input_path.write_bytes(payload)

            process = subprocess.Popen(
                [str(executable), "sweep", str(input_path), str(output_path)],
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            while process.poll() is None:
                if cancel_callback and cancel_callback():
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise RustKernelCancelled("Rust RF sweep was cancelled by the user.")
                time.sleep(0.05)
            stdout, stderr = process.communicate()
            if process.returncode:
                raise RustKernelError(stderr.strip() or stdout.strip() or "Rust RF sweep failed.")
            results = self._read_sweep_output(output_path, len(normalized_gammas))
            if return_winners and len(results) != 5:
                raise RustKernelError(
                    f"Rust optimizer returned {len(results)} winners; expected five strategies."
                )
            return results
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    @staticmethod
    def _complex_bytes(values: np.ndarray) -> bytes:
        array = np.ascontiguousarray(values, dtype=np.complex128)
        interleaved = np.empty(array.shape + (2,), dtype="<f8")
        interleaved[..., 0] = array.real
        interleaved[..., 1] = array.imag
        return interleaved.tobytes(order="C")

    def _read_sweep_output(self, path: Path, expected_tunable: int) -> list[SweepScore]:
        data = path.read_bytes()
        if len(data) < 24 or data[:8] != self._SWEEP_OUTPUT_MAGIC:
            raise RustKernelError("Rust optimizer returned an invalid sweep file.")
        total, tunable = struct.unpack_from("<QQ", data, 8)
        if tunable != expected_tunable:
            raise RustKernelError("Rust optimizer returned the wrong tunable-port count.")
        row_size = 40 + 8 * tunable
        if len(data) != 24 + total * row_size:
            raise RustKernelError("Rust optimizer returned a truncated sweep file.")
        results: list[SweepScore] = []
        offset = 24
        for _ in range(total):
            vswr_non_ant, vswr_ant, loss, target_non_ant, target_ant = struct.unpack_from(
                "<ddddd", data, offset
            )
            offset += 40
            combination = struct.unpack_from(f"<{tunable}Q", data, offset) if tunable else ()
            offset += 8 * tunable
            results.append(
                SweepScore(
                    vswr_non_ant=vswr_non_ant,
                    vswr_ant=vswr_ant,
                    worst_il_db=loss,
                    target_non_ant=target_non_ant,
                    target_ant=target_ant,
                    combination=tuple(int(value) for value in combination),
                )
            )
        return results
