from __future__ import annotations

from pathlib import Path

from .models import ConnectionType as CT
from .models import NetworkConfig, PortConfig, ProjectConfig


def _ports(count: int) -> list[PortConfig]:
    return [PortConfig(index + 1) for index in range(count)]


def default_fleet_config(root: str | Path) -> ProjectConfig:
    """Return the consistent ANT6 two-signal circuit described in fleet.txt."""
    root = Path(root)
    files = {
        path.name: path.relative_to(root)
        for path in (root / "snp_files").glob("*.s*p")
    }

    def named(fragment: str) -> str:
        matches = [path for name, path in files.items() if fragment.lower() in name.lower()]
        if len(matches) != 1:
            raise FileNotFoundError(f"Expected one Touchstone file containing {fragment!r}; found {len(matches)}.")
        return str(matches[0])

    ask_path = named("ASK_SLB_0403")
    slb_path = named("ANT6_SLB")
    clb_filter_path = named("ANT6_CLB")
    cubs_path = named("Cubs_RJF")
    clb_path = named("G651-19062")

    ask = NetworkConfig(ask_path, _ports(10))
    for index in (0, 1, 2):
        ask.ports[index].mode = CT.OPEN_INDUCTOR_CAPACITOR
    ask.ports[3] = PortConfig(4, CT.SIGNAL, signal="s1", start_ghz=3.3, stop_ghz=5.0)
    ask.ports[4] = PortConfig(5, CT.CONNECT, connect_network=slb_path, connect_port=2)

    slb = NetworkConfig(slb_path, _ports(2))
    slb.ports[0] = PortConfig(1, CT.CONNECT, connect_network=cubs_path, connect_port=1)
    slb.ports[1] = PortConfig(2, CT.CONNECT, connect_network=ask_path, connect_port=5)

    clb_filter = NetworkConfig(clb_filter_path, _ports(2))
    clb_filter.ports[0] = PortConfig(1, CT.CONNECT, connect_network=cubs_path, connect_port=2)
    clb_filter.ports[1] = PortConfig(2, CT.CONNECT, connect_network=clb_path, connect_port=4)

    cubs = NetworkConfig(cubs_path, _ports(4))
    cubs.ports[0] = PortConfig(1, CT.CONNECT, connect_network=slb_path, connect_port=1)
    cubs.ports[1] = PortConfig(2, CT.CONNECT, connect_network=clb_filter_path, connect_port=1)

    clb = NetworkConfig(clb_path, _ports(12))
    clb.ports[0].mode = CT.SHORT
    for index in (1, 2, 5):
        clb.ports[index].mode = CT.OPEN_INDUCTOR_CAPACITOR
    clb.ports[3] = PortConfig(4, CT.CONNECT, connect_network=clb_filter_path, connect_port=2)
    clb.ports[4] = PortConfig(5, CT.SIGNAL, signal="s2", start_ghz=3.3, stop_ghz=5.0)

    return ProjectConfig(
        networks=[ask, slb, clb_filter, cubs, clb],
        start_ghz=3.3,
        stop_ghz=5.0,
        points=201,
    )
