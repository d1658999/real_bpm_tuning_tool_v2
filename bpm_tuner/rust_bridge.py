from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


class RustKernelError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    bom_count: int
    max_vswr: float
    worst_il_db: float
    smith_radius: float
    target_distance: float


class RustOptimizer:
    """Build and invoke the dependency-free Rust ranking hot path."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        suffix = ".exe" if sys.platform == "win32" else ""
        self.executable = self.root / "rust_optimizer" / "target" / "release" / f"bpm-ranking-optimizer{suffix}"

    def ensure_built(self) -> Path:
        if self.executable.exists():
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
                    ["candidate_id", "bom_count", "max_vswr", "worst_il_db", "smith_radius", "target_distance"]
                )
                for item in candidates:
                    writer.writerow(
                        [
                            item.candidate_id,
                            item.bom_count,
                            item.max_vswr,
                            item.worst_il_db,
                            item.smith_radius,
                            item.target_distance,
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
