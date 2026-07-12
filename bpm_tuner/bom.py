from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BOMComponent:
    kind: str
    path: Path
    part_number: str
    value: float
    unit: str

    @property
    def display_value(self) -> str:
        return f"{self.value:g} {self.unit}"


def _murata_code_value(code: str) -> float:
    """Decode R-as-decimal Murata value fragments such as 1R5 and ER90."""
    code = code.upper()
    if "R" in code:
        left, right = code.split("R", 1)
        left_digits = "".join(ch for ch in left if ch.isdigit()) or "0"
        right_digits = "".join(ch for ch in right if ch.isdigit())
        return float(f"{int(left_digits)}.{right_digits or '0'}")
    digits = "".join(ch for ch in code if ch.isdigit())
    return float(digits) if digits else 0.0


def component_from_path(path: str | Path, kind: str | None = None) -> BOMComponent:
    path = Path(path)
    name = path.stem
    inferred = kind or ("capacitor" if name.startswith("GJM") else "inductor")
    if inferred == "capacitor":
        match = re.search(r"C1E([0-9]*R[0-9]+|[0-9]{3})", name, re.IGNORECASE)
        token = match.group(1) if match else ""
        if "R" in token.upper():
            value = _murata_code_value(token)
        elif len(token) == 3:
            value = float(token[:2]) * (10 ** int(token[2]))
        else:
            value = 0.0
        unit = "pF"
    else:
        match = re.search(r"LQP02TQ([0-9]*N[0-9]+|[0-9]+NH)", name, re.IGNORECASE)
        token = match.group(1).upper() if match else "0N0"
        if "N" in token:
            left, right = token.split("N", 1)
            value = float(f"{int(left or '0')}.{''.join(c for c in right if c.isdigit()) or '0'}")
        else:
            value = float("".join(c for c in token if c.isdigit()) or 0)
        unit = "nH"
    return BOMComponent(inferred, path, name.split("_")[0], value, unit)


def load_bom(root: str | Path) -> dict[str, list[BOMComponent]]:
    root = Path(root)
    result: dict[str, list[BOMComponent]] = {"inductor": [], "capacitor": []}
    for kind, folder in (("inductor", "Inductors_BOM"), ("capacitor", "Capacitors_BOM")):
        result[kind] = sorted(
            (component_from_path(p, kind) for p in (root / folder).glob("*.s2p")),
            key=lambda component: (component.value, component.part_number),
        )
    return result


def evenly_spaced(items: list[BOMComponent], count: int) -> list[BOMComponent]:
    if len(items) <= count:
        return items
    indexes = {round(i * (len(items) - 1) / (count - 1)) for i in range(count)} if count > 1 else {0}
    return [items[i] for i in sorted(indexes)]
