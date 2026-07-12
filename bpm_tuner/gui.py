"""PyQt5 desktop interface for the broadband matching tool.

The GUI deliberately keeps imports from the calculation layer lazy.  This lets
the window be exercised independently and confines the small amount of API
adaptation required while the RF/optimization modules evolve to :class:`CoreAPI`.
"""
from __future__ import annotations

import importlib
import json
import re
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QCloseEvent, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


CONNECTION_MODES = (
    "open",
    "short",
    "inductor",
    "capacitor",
    "inductor/capacitor",
    "open/inductor/capacitor",
    "connect",
    "signal",
)
SIGNALS = ("s1", "s2", "s3", "s4")
TOUCHSTONE_FILTER = "Touchstone (*.s?p *.s??p);;All files (*)"


APPLE_STYLE = """
QMainWindow, QWidget { background: #f5f5f7; color: #1d1d1f; }
QWidget { font-family: "SF Pro Text", "Segoe UI", sans-serif; font-size: 13px; }
QFrame#topBar { background: #000000; border: 0; }
QFrame#topBar QLabel { background: transparent; color: #ffffff; }
QFrame#panel, QGroupBox {
    background: #ffffff; border: 1px solid #e0e0e0; border-radius: 18px;
}
QGroupBox { margin-top: 10px; padding: 14px 10px 10px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QPushButton {
    min-height: 28px; padding: 5px 14px; border: 0; border-radius: 15px;
    color: #0066cc; background: #fafafc;
}
QPushButton:hover { background: #f0f0f0; }
QPushButton:pressed { background: #e7e7e9; }
QPushButton:checked { color: #ffffff; background: #0066cc; }
QPushButton#primaryButton { color: #ffffff; background: #0066cc; }
QPushButton#primaryButton:hover { background: #0071e3; }
QPushButton#dangerButton { color: #b42318; }
QPushButton:disabled { color: #7a7a7a; background: #e8e8ed; }
QListWidget, QTableWidget, QComboBox, QDoubleSpinBox {
    background: #ffffff; border: 1px solid #d2d2d7; border-radius: 8px;
    selection-background-color: #dceeff; selection-color: #1d1d1f;
}
QComboBox, QDoubleSpinBox { min-height: 28px; padding: 1px 6px; }
QHeaderView::section {
    background: #fafafc; color: #333333; border: 0; border-bottom: 1px solid #e0e0e0;
    padding: 7px; font-weight: 600;
}
QProgressBar {
    min-height: 8px; max-height: 8px; border: 0; border-radius: 4px; background: #d2d2d7;
}
QProgressBar::chunk { border-radius: 4px; background: #0066cc; }
QSplitter::handle { background: #f5f5f7; width: 6px; }
QToolTip { color: #ffffff; background: #272729; border: 0; padding: 5px; }
"""


def _import_symbol(modules: Sequence[str], symbol: str) -> Any:
    """Return *symbol* from the first local module that provides it."""

    failures: list[str] = []
    for module_name in modules:
        try:
            module = importlib.import_module(module_name, package=__package__)
        except ImportError as exc:
            failures.append(f"{module_name}: {exc}")
            continue
        if hasattr(module, symbol):
            return getattr(module, symbol)
    searched = ", ".join(modules)
    detail = "; ".join(failures[-2:])
    raise ImportError(f"{symbol} was not found in {searched}. {detail}".strip())


class CoreAPI:
    """Narrow compatibility layer around the separately implemented core API."""

    CONFIG_MODULES = (".config", ".models", ".project")
    ENGINE_MODULES = (".circuit", ".engine", ".rf_engine", ".simulation")
    OPTIMIZER_MODULES = (".optimizer", ".optimization", ".fleet")
    EXPORT_MODULES = (".exports", ".export", ".reporting")

    @classmethod
    def project_config(cls) -> type:
        return _import_symbol(cls.CONFIG_MODULES, "ProjectConfig")

    @classmethod
    def make_config(cls, payload: Mapping[str, Any]) -> Any:
        config_type = cls.project_config()
        if "snp_files" in payload and "ports" in payload:
            # Concrete application model: flattening the rows in the table keeps
            # GUI editing simple; the calculation layer receives nested networks.
            network_type = _import_symbol(cls.CONFIG_MODULES, "NetworkConfig")
            port_type = _import_symbol(cls.CONFIG_MODULES, "PortConfig")
            connection_type = _import_symbol(cls.CONFIG_MODULES, "ConnectionType")
            rows_by_file: dict[str, list[Mapping[str, Any]]] = {}
            for row in payload.get("ports", []):
                rows_by_file.setdefault(str(row["file"]), []).append(row)
            networks = []
            for path in payload.get("snp_files", []):
                ports = []
                for row in sorted(rows_by_file.get(str(path), []), key=lambda value: int(value["port"])):
                    connect_network: str | None = None
                    connect_port: int | None = None
                    target = row.get("connect_to")
                    if target:
                        match = re.match(r"^(.*):p(\d+)$", str(target))
                        if not match:
                            raise ValueError(f"Invalid port connection target: {target}")
                        connect_network = match.group(1)
                        connect_port = int(match.group(2))
                    ports.append(
                        port_type(
                            port=int(row["port"]),
                            mode=connection_type(str(row.get("mode", "open"))),
                            component_path=row.get("component"),
                            connect_network=connect_network,
                            connect_port=connect_port,
                            signal=row.get("signal"),
                            start_ghz=row.get("start_ghz"),
                            stop_ghz=row.get("stop_ghz"),
                            smith_target_enabled=bool(row.get("smith_target_enabled", False)),
                            smith_target_resistance_ohm=float(row.get("smith_target_resistance_ohm", 50.0)),
                            smith_target_reactance_ohm=float(row.get("smith_target_reactance_ohm", 0.0)),
                        )
                    )
                networks.append(network_type(path=str(path), ports=ports))
            target = payload.get("smith_target", {})
            config = config_type(
                networks=networks,
                smith_target_enabled=bool(target.get("enabled", False)),
                smith_target_resistance_ohm=float(target.get("resistance_ohm", target.get("real", 50.0))),
                smith_target_reactance_ohm=float(target.get("reactance_ohm", target.get("imag", 0.0))),
                smith_reference_ohm=float(target.get("reference_ohm", 50.0)),
            )
            validate = getattr(config, "validate", None)
            if callable(validate):
                validate(allow_unselected=True)
            return config
        for factory_name in ("from_dict", "from_mapping", "parse_obj", "model_validate"):
            factory = getattr(config_type, factory_name, None)
            if callable(factory):
                try:
                    return factory(dict(payload))
                except (TypeError, ValueError):
                    pass
        try:
            return config_type(**dict(payload))
        except TypeError as first_error:
            # Some intentionally lightweight cores use a single payload field.
            for kwargs in ({"data": dict(payload)}, {"payload": dict(payload)}):
                try:
                    return config_type(**kwargs)
                except TypeError:
                    continue
            raise TypeError(
                "ProjectConfig cannot be built from the GUI mapping. Provide "
                "ProjectConfig.from_dict(mapping) or matching keyword fields."
            ) from first_error

    @classmethod
    def config_to_mapping(cls, config: Any) -> dict[str, Any]:
        if isinstance(config, Mapping):
            return dict(config)
        if is_dataclass(config):
            return asdict(config)
        for method_name in ("to_dict", "model_dump", "dict"):
            method = getattr(config, method_name, None)
            if callable(method):
                value = method()
                if isinstance(value, Mapping):
                    return dict(value)
        return dict(vars(config))

    @classmethod
    def config_to_gui_mapping(cls, config: Any) -> dict[str, Any]:
        """Convert the nested core model to the table-oriented GUI mapping."""

        value = cls.config_to_mapping(config)
        if "networks" not in value:
            return value
        files: list[str] = []
        ports: list[dict[str, Any]] = []
        for network in value.get("networks", []):
            path = str(network["path"])
            files.append(path)
            for port in network.get("ports", []):
                mode = port.get("mode", "open")
                if hasattr(mode, "value"):
                    mode = mode.value
                target = None
                if port.get("connect_network") and port.get("connect_port") is not None:
                    target = f"{Path(str(port['connect_network'])).name}:p{int(port['connect_port'])}"
                ports.append(
                    {
                        "file": path,
                        "port": int(port["port"]),
                        "mode": str(mode),
                        "component": port.get("component_path"),
                        "connect_to": target,
                        "signal": port.get("signal"),
                        "start_ghz": port.get("start_ghz"),
                        "stop_ghz": port.get("stop_ghz"),
                        "smith_target_enabled": bool(port.get("smith_target_enabled", False)),
                        "smith_target_resistance_ohm": float(port.get("smith_target_resistance_ohm", 50.0)),
                        "smith_target_reactance_ohm": float(port.get("smith_target_reactance_ohm", 0.0)),
                    }
                )
        return {
            "snp_files": files,
            "ports": ports,
            "smith_target": {
                "enabled": bool(value.get("smith_target_enabled", False)),
                "resistance_ohm": float(value.get("smith_target_resistance_ohm", 50.0)),
                "reactance_ohm": float(value.get("smith_target_reactance_ohm", 0.0)),
                "reference_ohm": float(value.get("smith_reference_ohm", 50.0)),
            },
        }

    @classmethod
    def save_config(cls, config: Any, path: Path) -> None:
        try:
            function = _import_symbol(cls.CONFIG_MODULES, "save_config")
        except ImportError:
            function = None
        if callable(function):
            for args in ((config, path), (path, config)):
                try:
                    function(*args)
                    return
                except TypeError:
                    continue
        method = getattr(config, "save", None)
        if callable(method):
            method(path)
            return
        path.write_text(json.dumps(cls.config_to_mapping(config), indent=2), encoding="utf-8")

    @classmethod
    def load_config(cls, path: Path) -> Any:
        try:
            function = _import_symbol(cls.CONFIG_MODULES, "load_config")
        except ImportError:
            function = None
        if callable(function):
            return function(path)
        config_type = cls.project_config()
        method = getattr(config_type, "load", None)
        if callable(method):
            return method(path)
        return cls.make_config(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def run_cascade(cls, config: Any) -> Any:
        try:
            engine_type = _import_symbol(cls.ENGINE_MODULES, "CircuitEngine")
            root = Path(getattr(config, "_project_root", Path.cwd()))
            engine = engine_type(root)
            return engine.run(config)
        except ImportError:
            engine_type = _import_symbol(cls.ENGINE_MODULES, "RFEngine")
            try:
                engine = engine_type()
            except TypeError:
                engine = engine_type(config)
            method = getattr(engine, "run_cascade")
            try:
                return method(config)
            except TypeError:
                return method()

    @classmethod
    def new_optimizer(cls, config: Any) -> Any:
        try:
            runner_type = _import_symbol(cls.OPTIMIZER_MODULES, "FleetOptimizer")
            root = Path(getattr(config, "_project_root", Path.cwd()))
            return runner_type(root)
        except ImportError:
            runner_type = _import_symbol(cls.OPTIMIZER_MODULES, "OptimizationRunner")
        try:
            return runner_type()
        except TypeError:
            return runner_type(config)

    @classmethod
    def export(cls, kind: str, result: Any, destination: Path) -> None:
        module = None
        for module_name in cls.EXPORT_MODULES:
            try:
                module = importlib.import_module(module_name, package=__package__)
                break
            except ImportError:
                continue
        if module is None:
            raise ImportError("The bpm_tuner exports module is unavailable.")
        names = {
            "snp": ("export_touchstone", "export_snp", "save_snp"),
            "il_csv": ("export_il_csv", "export_insertion_loss_csv", "save_il_csv"),
        }[kind]
        for name in names:
            function = getattr(module, name, None)
            if callable(function):
                for args in ((result, destination), (destination, result)):
                    try:
                        function(*args)
                        return
                    except TypeError:
                        continue
        raise AttributeError(f"exports module must provide one of: {', '.join(names)}")


def _touchstone_port_count(path: Path) -> int:
    match = re.search(r"\.s(\d+)p$", path.name, flags=re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    try:
        import skrf as rf

        return int(rf.Network(str(path)).nports)
    except Exception:
        return 2


def _component_catalog(root: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return display labels for measured capacitor and inductor models."""

    def collect(folder: str, kind: str) -> list[tuple[str, str]]:
        directory = root / folder
        if not directory.exists():
            return []
        values: list[tuple[str, str]] = []
        try:
            component_from_path = _import_symbol((".bom",), "component_from_path")
        except ImportError:
            component_from_path = None
        for path in directory.rglob("*"):
            if not path.is_file() or not re.search(r"\.s\d+p$", path.name, re.IGNORECASE):
                continue
            relative = str(path.relative_to(root))
            if component_from_path is not None:
                component = component_from_path(path, kind)
                label = f"{component.display_value} · {component.part_number}"
            else:
                label = path.stem
            values.append((label, relative))
        return sorted(values, key=lambda item: item[0])

    return collect("Capacitors_BOM", "capacitor"), collect("Inductors_BOM", "inductor")


class FilesPanel(QFrame):
    """Left panel: ordered Touchstone input list."""

    files_changed = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(17, 17, 17, 17)
        title = QLabel("Touchstone files")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        hint = QLabel("Add the measured blocks in assembly order.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7a7a7a;")
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_widget.setAlternatingRowColors(False)
        self.list_widget.setToolTip("Drag files into the desired cascade order")
        self.list_widget.setDragDropMode(QAbstractItemView.InternalMove)
        self.list_widget.model().rowsMoved.connect(self._emit_files)

        actions = QHBoxLayout()
        self.add_button = QPushButton("Add")
        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.setObjectName("dangerButton")
        self.add_button.clicked.connect(self.add_files_dialog)
        self.remove_button.clicked.connect(self.remove_selected)
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(actions)

    def paths(self) -> list[Path]:
        return [Path(self.list_widget.item(index).data(Qt.UserRole)) for index in range(self.list_widget.count())]

    def set_paths(self, paths: Iterable[str | Path]) -> None:
        self.list_widget.clear()
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            item.setData(Qt.UserRole, str(path))
            self.list_widget.addItem(item)
        self._emit_files()

    @pyqtSlot()
    def add_files_dialog(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(self, "Add Touchstone files", str(Path.cwd()), TOUCHSTONE_FILTER)
        if filenames:
            self.add_paths(filenames)

    def add_paths(self, paths: Iterable[str | Path]) -> None:
        existing = {path.resolve() for path in self.paths()}
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            if path in existing:
                continue
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            item.setData(Qt.UserRole, str(path))
            self.list_widget.addItem(item)
            existing.add(path)
        self._emit_files()

    @pyqtSlot()
    def remove_selected(self) -> None:
        rows = sorted({self.list_widget.row(item) for item in self.list_widget.selectedItems()}, reverse=True)
        for row in rows:
            self.list_widget.takeItem(row)
        if rows:
            self._emit_files()

    @pyqtSlot()
    def _emit_files(self, *_args: Any) -> None:
        self.files_changed.emit(self.paths())


class PortSettingsPanel(QFrame):
    """Middle panel: editable settings for every port of every input file."""

    HEADERS = (
        "File",
        "Port",
        "Mode",
        "Measured component",
        "Connect to",
        "Signal",
        "Start GHz",
        "Stop GHz",
        "Target",
        "Target R Ω",
        "Target X Ω",
    )

    def __init__(self, project_root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.project_root = project_root
        self.capacitors, self.inductors = _component_catalog(project_root)
        self._paths: list[Path] = []
        self._pending_settings: dict[tuple[str, int], Mapping[str, Any]] = {}
        self._rebuilding = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(17, 17, 17, 17)
        title = QLabel("Port configuration")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        hint = QLabel(
            "Open is the safe default. Smith-target controls appear only for driven signal ports; "
            "the highest signal number is the dependent antenna port."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7a7a7a;")
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)

    @staticmethod
    def _target_spin(minimum: float, maximum: float, value: float, tooltip: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(4)
        spin.setSingleStep(1.0)
        spin.setValue(value)
        spin.setSuffix(" Ω")
        spin.setToolTip(tooltip)
        return spin

    @staticmethod
    def _frequency_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1000.0)
        spin.setDecimals(6)
        spin.setSpecialValueText("Auto")
        spin.setSingleStep(0.1)
        spin.setToolTip("0/Auto uses the Touchstone file frequency range")
        return spin

    def set_files(self, paths: Sequence[str | Path]) -> None:
        current = self.settings_by_key()
        current.update(self._pending_settings)
        self._paths = [Path(path) for path in paths]
        rows = sum(_touchstone_port_count(path) for path in self._paths)
        self._rebuilding = True
        self.table.setRowCount(rows)
        targets = [f"{path.name}:p{port}" for path in self._paths for port in range(1, _touchstone_port_count(path) + 1)]
        row = 0
        for path in self._paths:
            relative_key = str(path)
            for port in range(1, _touchstone_port_count(path) + 1):
                setting = current.get((relative_key, port), {})
                self._build_row(row, path, port, targets, setting)
                row += 1
        self._rebuilding = False
        self._refresh_target_controls()
        self._pending_settings = {}

    def _build_row(
        self,
        row: int,
        path: Path,
        port: int,
        targets: Sequence[str],
        setting: Mapping[str, Any],
    ) -> None:
        file_item = QTableWidgetItem(path.name)
        file_item.setToolTip(str(path))
        file_item.setData(Qt.UserRole, str(path))
        file_item.setFlags(file_item.flags() & ~Qt.ItemIsEditable)
        port_item = QTableWidgetItem(str(port))
        port_item.setFlags(port_item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, 0, file_item)
        self.table.setItem(row, 1, port_item)

        mode = QComboBox()
        mode.addItems(CONNECTION_MODES)
        mode.setCurrentText(str(setting.get("mode", "open")))
        component = QComboBox()
        connection = QComboBox()
        signal = QComboBox()
        signal.addItem("—")
        signal.addItems(SIGNALS)
        connection.addItem("—")
        own_target = f"{path.name}:p{port}"
        connection.addItems([target for target in targets if target != own_target])
        start = self._frequency_spin()
        stop = self._frequency_spin()
        target_enabled = QCheckBox("Enable")
        target_resistance = self._target_spin(0.0, 1_000_000.0, 50.0, "Target resistance in ohms")
        target_reactance = self._target_spin(-1_000_000.0, 1_000_000.0, 0.0, "Target reactance in ohms")
        start.setValue(float(setting.get("start_ghz") or 0.0))
        stop.setValue(float(setting.get("stop_ghz") or 0.0))
        target_enabled.setChecked(bool(setting.get("smith_target_enabled", False)))
        target_resistance.setValue(float(setting.get("smith_target_resistance_ohm", 50.0)))
        target_reactance.setValue(float(setting.get("smith_target_reactance_ohm", 0.0)))

        self.table.setCellWidget(row, 2, mode)
        self.table.setCellWidget(row, 3, component)
        self.table.setCellWidget(row, 4, connection)
        self.table.setCellWidget(row, 5, signal)
        self.table.setCellWidget(row, 6, start)
        self.table.setCellWidget(row, 7, stop)
        self.table.setCellWidget(row, 8, target_enabled)
        self.table.setCellWidget(row, 9, target_resistance)
        self.table.setCellWidget(row, 10, target_reactance)
        self._mode_changed(row, mode.currentText())
        self._set_combo_data(component, setting.get("component"))
        self._set_combo_value(connection, str(setting.get("connect_to", "—")))
        self._set_combo_value(signal, str(setting.get("signal", "—")))
        mode.currentTextChanged.connect(lambda value, row=row: self._mode_changed(row, value))
        signal.currentTextChanged.connect(self._refresh_target_controls)
        target_enabled.toggled.connect(lambda checked, row=row: self._target_toggled(row, checked))

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: Any) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @pyqtSlot(str)
    def _mode_changed(self, row: int, mode: str) -> None:
        component = self.table.cellWidget(row, 3)
        connection = self.table.cellWidget(row, 4)
        signal = self.table.cellWidget(row, 5)
        if not isinstance(component, QComboBox) or not isinstance(connection, QComboBox) or not isinstance(signal, QComboBox):
            return
        previous = component.currentData()
        component.clear()
        component.addItem("—", None)
        if mode in ("inductor", "inductor/capacitor", "open/inductor/capacitor"):
            for label, path in self.inductors:
                component.addItem(label, path)
        if mode in ("capacitor", "inductor/capacitor", "open/inductor/capacitor"):
            for label, path in self.capacitors:
                component.addItem(label, path)
        self._set_combo_data(component, previous)
        component.setEnabled(mode in ("inductor", "capacitor", "inductor/capacitor", "open/inductor/capacitor"))
        connection.setEnabled(mode == "connect")
        signal.setEnabled(mode == "signal")
        if not self._rebuilding:
            self._refresh_target_controls()

    @pyqtSlot()
    def _refresh_target_controls(self, *_args: Any) -> None:
        assignments: list[tuple[int, int]] = []
        for row in range(self.table.rowCount()):
            mode = self.table.cellWidget(row, 2)
            signal = self.table.cellWidget(row, 5)
            if isinstance(mode, QComboBox) and isinstance(signal, QComboBox):
                text = signal.currentText()
                if mode.currentText() == "signal" and text in SIGNALS:
                    assignments.append((row, int(text[1:])))
        dependent_number = max((number for _, number in assignments), default=0)
        enough_signals = len(assignments) >= 2
        for row in range(self.table.rowCount()):
            enabled = self.table.cellWidget(row, 8)
            resistance = self.table.cellWidget(row, 9)
            reactance = self.table.cellWidget(row, 10)
            assignment = next((number for target_row, number in assignments if target_row == row), None)
            eligible = enough_signals and assignment is not None and assignment != dependent_number
            if not isinstance(enabled, QCheckBox):
                continue
            enabled.setProperty("targetEligible", eligible)
            enabled.setVisible(eligible)
            if not eligible and enabled.isChecked():
                enabled.blockSignals(True)
                enabled.setChecked(False)
                enabled.blockSignals(False)
            if isinstance(resistance, QDoubleSpinBox):
                resistance.setVisible(eligible)
                resistance.setEnabled(eligible and enabled.isChecked())
            if isinstance(reactance, QDoubleSpinBox):
                reactance.setVisible(eligible)
                reactance.setEnabled(eligible and enabled.isChecked())

    def _target_toggled(self, row: int, checked: bool) -> None:
        enabled = self.table.cellWidget(row, 8)
        resistance = self.table.cellWidget(row, 9)
        reactance = self.table.cellWidget(row, 10)
        eligible = isinstance(enabled, QCheckBox) and bool(enabled.property("targetEligible"))
        if isinstance(resistance, QDoubleSpinBox):
            resistance.setEnabled(eligible and checked)
        if isinstance(reactance, QDoubleSpinBox):
            reactance.setEnabled(eligible and checked)

    def settings_by_key(self) -> dict[tuple[str, int], Mapping[str, Any]]:
        return {(setting["file"], int(setting["port"])): setting for setting in self.port_settings()}

    def port_settings(self) -> list[dict[str, Any]]:
        settings: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            file_item = self.table.item(row, 0)
            port_item = self.table.item(row, 1)
            if file_item is None or port_item is None:
                continue
            mode = self.table.cellWidget(row, 2)
            component = self.table.cellWidget(row, 3)
            connection = self.table.cellWidget(row, 4)
            signal = self.table.cellWidget(row, 5)
            start = self.table.cellWidget(row, 6)
            stop = self.table.cellWidget(row, 7)
            target_enabled = self.table.cellWidget(row, 8)
            target_resistance = self.table.cellWidget(row, 9)
            target_reactance = self.table.cellWidget(row, 10)
            settings.append(
                {
                    "file": str(file_item.data(Qt.UserRole)),
                    "port": int(port_item.text()),
                    "mode": mode.currentText(),
                    "component": component.currentData(),
                    "connect_to": None if connection.currentText() == "—" else connection.currentText(),
                    "signal": None if signal.currentText() == "—" else signal.currentText(),
                    "start_ghz": None if start.value() == 0 else start.value(),
                    "stop_ghz": None if stop.value() == 0 else stop.value(),
                    "smith_target_enabled": target_enabled.isChecked(),
                    "smith_target_resistance_ohm": target_resistance.value(),
                    "smith_target_reactance_ohm": target_reactance.value(),
                }
            )
        return settings

    def target(self) -> dict[str, Any]:
        """Legacy project-wide target payload; new targets live on signal rows."""
        return {
            "enabled": False,
            "resistance_ohm": 50.0,
            "reactance_ohm": 0.0,
            "reference_ohm": 50.0,
        }

    def apply_mapping(self, payload: Mapping[str, Any]) -> None:
        raw_settings = payload.get("ports", payload.get("port_settings", []))
        self._pending_settings = {
            (str(setting.get("file", setting.get("path", ""))), int(setting.get("port", 0))): setting
            for setting in raw_settings
            if isinstance(setting, Mapping)
        }


class PlotPanel(QFrame):
    """Right panel containing a Smith chart and three Cartesian responses."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.result: Any = None
        self._data: dict[str, np.ndarray] | None = None
        self._marker_cid: int | None = None
        self._marker_artists: list[Any] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        heading = QHBoxLayout()
        title = QLabel("RF performance")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        heading.addWidget(title)
        heading.addStretch()

        self.figure = Figure(figsize=(9.2, 7.0), facecolor="#ffffff", constrained_layout=True)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.navigation = NavigationToolbar2QT(self.canvas, self)
        self.navigation.hide()
        self.axes = self.figure.subplots(2, 2)

        controls = QHBoxLayout()
        self.reset_button = QPushButton("Reset Original")
        self.zoom_button = QPushButton("Zoom In/Out")
        self.move_button = QPushButton("Move")
        self.marker_button = QPushButton("Marker")
        self.save_button = QPushButton("Save Figures")
        self.zoom_button.setCheckable(True)
        self.move_button.setCheckable(True)
        self.marker_button.setCheckable(True)
        self.reset_button.clicked.connect(self.reset)
        self.zoom_button.toggled.connect(self._toggle_zoom)
        self.move_button.toggled.connect(self._toggle_move)
        self.marker_button.toggled.connect(self._toggle_marker)
        self.save_button.clicked.connect(self.save_figure)
        for button in (self.reset_button, self.zoom_button, self.move_button, self.marker_button, self.save_button):
            controls.addWidget(button)
        controls.addStretch()

        layout.addLayout(heading)
        layout.addWidget(self.canvas, 1)
        layout.addLayout(controls)
        self._draw_empty()

    @property
    def smith_axis(self):
        return self.axes[0, 0]

    def _draw_empty(self) -> None:
        for axis in self.axes.flat:
            axis.clear()
        self._draw_smith_grid(self.smith_axis)
        self.smith_axis.set_title("Smith chart · S11")
        for axis, title, ylabel in (
            (self.axes[0, 1], "Insertion loss · S21", "dB"),
            (self.axes[1, 0], "VSWR · S11 / S22", "VSWR"),
            (self.axes[1, 1], "Return loss · S11 / S22", "dB"),
        ):
            axis.set_title(title)
            axis.set_xlabel("Frequency (GHz)")
            axis.set_ylabel(ylabel)
            axis.grid(True, color="#e0e0e0", linewidth=0.65)
        self.canvas.draw_idle()

    @staticmethod
    def _draw_smith_grid(axis: Any) -> None:
        axis.set_aspect("equal", adjustable="box")
        axis.set_xlim(-1.08, 1.08)
        axis.set_ylim(-1.08, 1.08)
        axis.axis("off")
        theta = np.linspace(0, 2 * np.pi, 512)
        axis.plot(np.cos(theta), np.sin(theta), color="#333333", linewidth=1.0)
        axis.axhline(0, color="#d2d2d7", linewidth=0.65)
        for resistance in (0.2, 0.5, 1, 2, 5):
            center = resistance / (1 + resistance)
            radius = 1 / (1 + resistance)
            x = center + radius * np.cos(theta)
            y = radius * np.sin(theta)
            mask = x * x + y * y <= 1.00001
            axis.plot(x[mask], y[mask], color="#e0e0e0", linewidth=0.55)
        for reactance in (0.2, 0.5, 1, 2, 5):
            for sign in (-1, 1):
                center_y = sign / reactance
                radius = 1 / reactance
                x = 1 + radius * np.cos(theta)
                y = center_y + radius * np.sin(theta)
                mask = x * x + y * y <= 1.00001
                axis.plot(x[mask], y[mask], color="#e0e0e0", linewidth=0.55)

    @staticmethod
    def _find_network(result: Any) -> Any:
        queue = [result]
        visited: set[int] = set()
        while queue:
            value = queue.pop(0)
            if value is None or id(value) in visited:
                continue
            visited.add(id(value))
            if hasattr(value, "s") and (hasattr(value, "f") or hasattr(value, "frequency")):
                return value
            if isinstance(value, Mapping):
                queue.extend(value.get(key) for key in ("network", "cascaded_network", "cascade", "simulation", "result", "selected"))
            else:
                queue.extend(getattr(value, key, None) for key in ("network", "cascaded_network", "cascade", "simulation", "result", "selected"))
        raise ValueError("Simulation result does not expose a scikit-rf-like network (f and s).")

    @classmethod
    def _extract_data(cls, result: Any) -> dict[str, np.ndarray]:
        network = cls._find_network(result)
        frequency = np.asarray(getattr(network, "f", getattr(getattr(network, "frequency", None), "f", [])), dtype=float)
        s = np.asarray(network.s, dtype=complex)
        if s.ndim != 3 or s.shape[0] != frequency.size or s.shape[1] < 1:
            raise ValueError("The cascaded network has invalid S-parameter dimensions.")
        tiny = np.finfo(float).tiny
        s11 = s[:, 0, 0]
        s22 = s[:, 1, 1] if s.shape[1] > 1 else np.full_like(s11, np.nan)
        s21 = s[:, 1, 0] if s.shape[1] > 1 else np.full_like(s11, np.nan)
        reflections = np.stack([s[:, index, index] for index in range(s.shape[1])], axis=1)
        magnitude11 = np.clip(np.abs(s11), 0, 1 - 1e-12)
        magnitude22 = np.clip(np.abs(s22), 0, 1 - 1e-12)
        return {
            "frequency": frequency / 1e9,
            "s11": s11,
            "s22": s22,
            "reflections": reflections,
            "s21_db": 20 * np.log10(np.maximum(np.abs(s21), tiny)),
            "vswr11": (1 + magnitude11) / (1 - magnitude11),
            "vswr22": (1 + magnitude22) / (1 - magnitude22),
            "rl11": -20 * np.log10(np.maximum(np.abs(s11), tiny)),
            "rl22": -20 * np.log10(np.maximum(np.abs(s22), tiny)),
        }

    def show_result(self, result: Any) -> None:
        self.result = result
        self._data = self._extract_data(result)
        for axis in self.axes.flat:
            axis.clear()
        data = self._data
        blue, black = "#0066cc", "#1d1d1f"
        smith_colors = (blue, black, "#7a7a7a", "#2997ff")
        self._draw_smith_grid(self.smith_axis)
        for index in range(data["reflections"].shape[1]):
            trace = data["reflections"][:, index]
            self.smith_axis.plot(
                trace.real,
                trace.imag,
                color=smith_colors[index % len(smith_colors)],
                linewidth=1.8 if index == 0 else 1.3,
                label=f"S{index + 1}{index + 1}",
            )
        config = getattr(result, "config", None)
        if config is not None:
            target_specs = getattr(config, "smith_targets_by_signal", lambda: {})()
            for target_index, (signal_name, (impedance, target)) in enumerate(target_specs.items()):
                self.smith_axis.plot(
                    [target.real],
                    [target.imag],
                    marker="*",
                    markersize=11,
                    color=smith_colors[target_index % len(smith_colors)],
                    markeredgecolor="white",
                    label=f"{signal_name} target {impedance.real:g}{impedance.imag:+g}j Ω",
                )
        smith_labels = " / ".join(f"S{i + 1}{i + 1}" for i in range(data["reflections"].shape[1]))
        self.smith_axis.set_title(f"Smith chart · {smith_labels}")
        self.smith_axis.legend(frameon=False, fontsize=8, loc="upper right")
        self.axes[0, 1].plot(data["frequency"], data["s21_db"], color=blue, linewidth=1.6, label="S21")
        self.axes[1, 0].plot(data["frequency"], data["vswr11"], color=blue, linewidth=1.6, label="S11")
        if np.isfinite(data["vswr22"]).any():
            self.axes[1, 0].plot(data["frequency"], data["vswr22"], color=black, linewidth=1.3, label="S22")
        self.axes[1, 1].plot(data["frequency"], data["rl11"], color=blue, linewidth=1.6, label="S11")
        if np.isfinite(data["rl22"]).any():
            self.axes[1, 1].plot(data["frequency"], data["rl22"], color=black, linewidth=1.3, label="S22")
        for axis, title, ylabel in (
            (self.axes[0, 1], "Insertion loss · S21", "dB"),
            (self.axes[1, 0], "VSWR · S11 / S22", "VSWR"),
            (self.axes[1, 1], "Return loss · S11 / S22", "dB"),
        ):
            axis.set_title(title)
            axis.set_xlabel("Frequency (GHz)")
            axis.set_ylabel(ylabel)
            axis.grid(True, color="#e0e0e0", linewidth=0.65)
            axis.legend(frameon=False, fontsize=8)
        self.navigation.update()
        self.canvas.draw_idle()

    @pyqtSlot()
    def reset(self) -> None:
        self.navigation.home()
        if self.result is not None:
            self.show_result(self.result)

    @pyqtSlot(bool)
    def _toggle_zoom(self, enabled: bool) -> None:
        if enabled and self.move_button.isChecked():
            self.move_button.setChecked(False)
        self.navigation.zoom()

    @pyqtSlot(bool)
    def _toggle_move(self, enabled: bool) -> None:
        if enabled and self.zoom_button.isChecked():
            self.zoom_button.setChecked(False)
        self.navigation.pan()

    @pyqtSlot(bool)
    def _toggle_marker(self, enabled: bool) -> None:
        if enabled:
            if self.zoom_button.isChecked():
                self.zoom_button.setChecked(False)
            if self.move_button.isChecked():
                self.move_button.setChecked(False)
            if self._marker_cid is None:
                self._marker_cid = self.canvas.mpl_connect("button_press_event", self._place_marker)
        elif self._marker_cid is not None:
            self.canvas.mpl_disconnect(self._marker_cid)
            self._marker_cid = None

    def _place_marker(self, event: Any) -> None:
        if self._data is None or event.inaxes is None:
            return
        frequency = self._data["frequency"]
        if event.inaxes is self.smith_axis:
            point = complex(event.xdata, event.ydata)
            distance = np.min(np.abs(self._data["reflections"] - point), axis=1)
            index = int(np.nanargmin(distance))
        else:
            if event.xdata is None:
                return
            index = int(np.nanargmin(np.abs(frequency - event.xdata)))
        self._clear_markers()
        marker_frequency = frequency[index]
        marker_colors = ("#0066cc", "#1d1d1f", "#7a7a7a", "#2997ff")
        for reflection_index, reflection in enumerate(self._data["reflections"][index]):
            self._marker_artists.append(
                self.smith_axis.plot(
                    reflection.real,
                    reflection.imag,
                    "o",
                    color=marker_colors[reflection_index % len(marker_colors)],
                    markeredgecolor="white",
                    markersize=7,
                )[0]
            )
        for axis in (self.axes[0, 1], self.axes[1, 0], self.axes[1, 1]):
            self._marker_artists.append(axis.axvline(marker_frequency, color="#0066cc", linestyle="--", linewidth=0.9))
        label = (
            f"{marker_frequency:.6g} GHz\n"
            f"S11={self._data['rl11'][index]:.2f} dB RL\n"
            f"S22={self._data['rl22'][index]:.2f} dB RL\n"
            f"S21={self._data['s21_db'][index]:.2f} dB\n"
            f"VSWR={self._data['vswr11'][index]:.3f}"
        )
        annotation = self.axes[0, 1].annotate(
            label,
            xy=(marker_frequency, self._data["s21_db"][index]),
            xytext=(9, 9),
            textcoords="offset points",
            fontsize=8,
            bbox={"boxstyle": "round,pad=0.45", "facecolor": "white", "edgecolor": "#d2d2d7"},
        )
        self._marker_artists.append(annotation)
        self.canvas.draw_idle()

    def _clear_markers(self) -> None:
        for artist in self._marker_artists:
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass
        self._marker_artists.clear()

    @pyqtSlot()
    def save_figure(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Save combined RF figures", "rf_performance.png", "PNG image (*.png)")
        if filename:
            try:
                self.figure.savefig(filename, dpi=180, facecolor="white")
            except Exception as exc:
                QMessageBox.critical(self, "Could not save figures", str(exc))


class OptimizationWorker(QObject):
    """Worker object executed in a dedicated QThread."""

    progress = pyqtSignal(int, str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, config: Any, runner: Any) -> None:
        super().__init__()
        self.config = config
        self.runner = runner
        self._cancel = threading.Event()

    @pyqtSlot()
    def run(self) -> None:
        try:
            method = getattr(self.runner, "optimize", None) or getattr(self.runner, "run")
            try:
                report = method(self.config, self._progress_callback, self._cancel.is_set)
            except TypeError:
                report = method(
                    config=self.config,
                    progress_cb=self._progress_callback,
                    cancel_cb=self._cancel.is_set,
                )
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(report)
        except Exception as exc:
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))

    def _progress_callback(self, *values: Any, **kwargs: Any) -> None:
        percent: float = 0.0
        message = str(kwargs.get("message", "Optimizing…"))
        if values:
            first = values[0]
            if isinstance(first, (int, float)):
                percent = float(first)
                if len(values) > 1:
                    message = str(values[1])
            elif isinstance(first, Mapping):
                percent = float(first.get("percent", first.get("progress", 0)))
                message = str(first.get("message", message))
            else:
                message = str(first)
        if isinstance(values[0] if values else None, float) and 0 <= percent <= 1:
            percent *= 100
        self.progress.emit(max(0, min(100, int(round(percent)))), message)

    @pyqtSlot()
    def cancel(self) -> None:
        self._cancel.set()
        cancel_method = getattr(self.runner, "cancel", None)
        if callable(cancel_method):
            cancel_method()


class MainWindow(QMainWindow):
    """Three-panel desktop application described by ``Requirements.md``."""

    def __init__(self, project_root: str | Path | None = None) -> None:
        super().__init__()
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.current_result: Any = None
        self.optimization_thread: QThread | None = None
        self.optimization_worker: OptimizationWorker | None = None
        self.setWindowTitle("BPM Tuning Tool")
        self.resize(1600, 920)
        self.setMinimumSize(1050, 680)
        self.setStyleSheet(APPLE_STYLE)
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(22, 10, 22, 10)
        brand = QLabel("BPM Tuner")
        brand.setFont(QFont("SF Pro Display", 15, QFont.DemiBold))
        top_layout.addWidget(brand)
        top_layout.addSpacing(18)
        self.run_cascade_button = self._toolbar_button("Run Cascade", self.run_cascade, primary=True)
        self.run_optimization_button = self._toolbar_button("Run Optimization", self.run_optimization, primary=True)
        self.save_config_button = self._toolbar_button("Save Config", self.save_config)
        self.load_config_button = self._toolbar_button("Load Config", self.load_config)
        self.export_snp_button = self._toolbar_button("Export SNP", self.export_snp)
        self.export_csv_button = self._toolbar_button("Export IL CSV", self.export_il_csv)
        self.export_snp_button.setEnabled(False)
        self.export_csv_button.setEnabled(False)
        for button in (
            self.run_cascade_button,
            self.run_optimization_button,
            self.save_config_button,
            self.load_config_button,
            self.export_snp_button,
            self.export_csv_button,
        ):
            top_layout.addWidget(button)
        top_layout.addStretch()
        root.addWidget(top_bar)

        progress_row = QWidget()
        progress_layout = QHBoxLayout(progress_row)
        progress_layout.setContentsMargins(22, 8, 22, 8)
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #7a7a7a;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        self.cancel_button = QPushButton("Cancel optimization")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self.cancel_optimization)
        self.cancel_button.hide()
        progress_layout.addWidget(self.status_label)
        progress_layout.addWidget(self.progress_bar, 1)
        progress_layout.addWidget(self.cancel_button)
        root.addWidget(progress_row)

        self.files_panel = FilesPanel()
        self.port_panel = PortSettingsPanel(self.project_root)
        self.plot_panel = PlotPanel()
        self.files_panel.files_changed.connect(self.port_panel.set_files)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.files_panel)
        splitter.addWidget(self.port_panel)
        splitter.addWidget(self.plot_panel)
        splitter.setSizes([250, 660, 800])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 4)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(12, 4, 12, 12)
        container_layout.addWidget(splitter)
        root.addWidget(container, 1)
        self.setCentralWidget(central)

    @staticmethod
    def _toolbar_button(label: str, callback: Callable[[], None], primary: bool = False) -> QPushButton:
        button = QPushButton(label)
        if primary:
            button.setObjectName("primaryButton")
        button.clicked.connect(callback)
        return button

    def gui_payload(self) -> dict[str, Any]:
        return {
            "snp_files": [str(path) for path in self.files_panel.paths()],
            "ports": self.port_panel.port_settings(),
            "smith_target": self.port_panel.target(),
        }

    def _validate_payload(self, payload: Mapping[str, Any]) -> None:
        files = [Path(path) for path in payload.get("snp_files", [])]
        if not files:
            raise ValueError("Add at least one Touchstone file.")
        missing = [str(path) for path in files if not path.is_file()]
        if missing:
            raise ValueError("Touchstone file not found: " + missing[0])
        signals: list[str] = []
        target_signals: list[str] = []
        for setting in payload.get("ports", []):
            mode = setting["mode"]
            if mode == "connect" and not setting.get("connect_to"):
                raise ValueError(f"{Path(setting['file']).name} port {setting['port']} needs a connection target.")
            if mode == "signal":
                if not setting.get("signal"):
                    raise ValueError(f"{Path(setting['file']).name} port {setting['port']} needs s1, s2, s3, or s4.")
                signals.append(setting["signal"])
            if setting.get("smith_target_enabled"):
                if mode != "signal" or not setting.get("signal"):
                    raise ValueError("Smith targets can be enabled only on assigned signal ports.")
                resistance = float(setting.get("smith_target_resistance_ohm", 50.0))
                reactance = float(setting.get("smith_target_reactance_ohm", 0.0))
                if not np.isfinite(resistance) or not np.isfinite(reactance) or resistance < 0:
                    raise ValueError(
                        f"{setting['signal']} Smith target must use finite resistance/reactance in ohms, "
                        "with resistance zero or greater."
                    )
                target_signals.append(setting["signal"])
            start, stop = setting.get("start_ghz"), setting.get("stop_ghz")
            if (start is None) != (stop is None):
                raise ValueError(f"Set both frequency limits, or leave both Auto, for {Path(setting['file']).name} port {setting['port']}.")
            if start is not None and start >= stop:
                raise ValueError(f"Frequency start must be below stop for {Path(setting['file']).name} port {setting['port']}.")
        if len(signals) > 4 or len(signals) != len(set(signals)):
            raise ValueError("Signal assignments must be unique and limited to s1, s2, s3, and s4.")
        if signals:
            dependent_signal = max(signals, key=lambda value: int(value[1:]))
            if dependent_signal in target_signals:
                raise ValueError(f"{dependent_signal} is the dependent antenna port and cannot have a Smith target.")
        target = payload.get("smith_target", {})
        if target.get("enabled"):
            resistance = float(target.get("resistance_ohm", 50.0))
            reference = float(target.get("reference_ohm", 50.0))
            if not np.isfinite(resistance) or resistance < 0:
                raise ValueError("Smith target resistance must be a finite value of zero ohms or greater.")
            if reference <= 0:
                raise ValueError("Smith target reference impedance must be greater than zero.")

    def current_config(self) -> Any:
        payload = self.gui_payload()
        self._validate_payload(payload)
        config = CoreAPI.make_config(payload)
        # The project root owns the real-component catalogs.  It is runtime
        # context rather than serialized configuration.
        setattr(config, "_project_root", self.project_root)
        return config

    def _show_error(self, title: str, error: Exception | str) -> None:
        QMessageBox.critical(self, title, f"{error}\n\nCheck the port settings and try again.")

    @pyqtSlot()
    def run_cascade(self) -> None:
        try:
            self.status_label.setText("Running cascade…")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            result = CoreAPI.run_cascade(self.current_config())
            self._set_result(result)
            self.status_label.setText("Cascade complete")
        except Exception as exc:
            self.status_label.setText("Cascade failed")
            self._show_error("Cascade could not run", exc)
        finally:
            QApplication.restoreOverrideCursor()

    @pyqtSlot()
    def run_optimization(self) -> None:
        if self.optimization_thread is not None and self.optimization_thread.isRunning():
            return
        try:
            config = self.current_config()
            runner = CoreAPI.new_optimizer(config)
        except Exception as exc:
            self._show_error("Optimization could not start", exc)
            return
        self.optimization_thread = QThread(self)
        self.optimization_worker = OptimizationWorker(config, runner)
        self.optimization_worker.moveToThread(self.optimization_thread)
        self.optimization_thread.started.connect(self.optimization_worker.run)
        self.optimization_worker.progress.connect(self._optimization_progress)
        self.optimization_worker.finished.connect(self._optimization_finished)
        self.optimization_worker.failed.connect(self._optimization_failed)
        self.optimization_worker.cancelled.connect(self._optimization_cancelled)
        for signal in (self.optimization_worker.finished, self.optimization_worker.failed, self.optimization_worker.cancelled):
            signal.connect(self.optimization_thread.quit)
        self.optimization_thread.finished.connect(self.optimization_worker.deleteLater)
        self.optimization_thread.finished.connect(self._thread_finished)
        self.run_optimization_button.setEnabled(False)
        self.run_cascade_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.cancel_button.show()
        self.status_label.setText("Starting optimization…")
        self.optimization_thread.start()

    @pyqtSlot()
    def cancel_optimization(self) -> None:
        if self.optimization_worker is not None:
            # A direct call is intentional: setting threading.Event is thread-safe,
            # while a queued slot cannot run until a busy worker returns.
            self.optimization_worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling after the current candidate…")

    @pyqtSlot(int, str)
    def _optimization_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(percent)
        self.status_label.setText(message or f"Optimizing… {percent}%")

    @pyqtSlot(object)
    def _optimization_finished(self, report: Any) -> None:
        result = self._result_from_report(report)
        if result is not None:
            try:
                self._set_result(result)
            except Exception as exc:
                self._show_error("Optimization plot unavailable", exc)
        self.progress_bar.setValue(100)
        self.status_label.setText("Optimization complete")
        QMessageBox.information(self, "Optimization complete", "The best production-aware solution is ready.")

    @staticmethod
    def _result_from_report(report: Any) -> Any:
        if report is None:
            return None
        if isinstance(report, Mapping):
            for key in ("simulation_result", "result", "best_result", "selected"):
                if report.get(key) is not None:
                    return report[key]
        for key in ("simulation_result", "result", "best_result", "selected"):
            value = getattr(report, key, None)
            if value is not None:
                return value
        return report

    @pyqtSlot(str)
    def _optimization_failed(self, message: str) -> None:
        self.status_label.setText("Optimization failed")
        self._show_error("Optimization failed", message)

    @pyqtSlot()
    def _optimization_cancelled(self) -> None:
        self.status_label.setText("Optimization cancelled")

    @pyqtSlot()
    def _thread_finished(self) -> None:
        self.run_optimization_button.setEnabled(True)
        self.run_cascade_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.hide()
        self.progress_bar.hide()
        thread = self.optimization_thread
        self.optimization_thread = None
        self.optimization_worker = None
        if thread is not None:
            thread.deleteLater()

    def _set_result(self, result: Any) -> None:
        self.plot_panel.show_result(result)
        self.current_result = result
        self.export_snp_button.setEnabled(True)
        self.export_csv_button.setEnabled(True)

    @pyqtSlot()
    def save_config(self) -> None:
        try:
            config = self.current_config()
        except Exception as exc:
            self._show_error("Configuration is invalid", exc)
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save project configuration", "bpm_config.json", "JSON (*.json)")
        if filename:
            try:
                CoreAPI.save_config(config, Path(filename))
                self.status_label.setText(f"Saved {Path(filename).name}")
            except Exception as exc:
                self._show_error("Could not save configuration", exc)

    @pyqtSlot()
    def load_config(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Load project configuration", str(self.project_root), "JSON (*.json)")
        if not filename:
            return
        try:
            config = CoreAPI.load_config(Path(filename))
            payload = CoreAPI.config_to_gui_mapping(config)
            files = payload.get("snp_files", payload.get("files", []))
            normalized: dict[str, str] = {}
            for raw_path in files:
                path = Path(raw_path).expanduser()
                resolved = path.resolve() if path.is_absolute() else (self.project_root / path).resolve()
                normalized[str(raw_path)] = str(resolved)
            files = [normalized[str(path)] for path in files]
            for port in payload.get("ports", []):
                port["file"] = normalized.get(str(port.get("file", "")), str(port.get("file", "")))
            self.port_panel.apply_mapping(payload)
            self.files_panel.set_paths(files)
            self.status_label.setText(f"Loaded {Path(filename).name}")
        except Exception as exc:
            self._show_error("Could not load configuration", exc)

    @pyqtSlot()
    def export_snp(self) -> None:
        if self.current_result is None:
            QMessageBox.information(self, "Nothing to export", "Run Cascade or Run Optimization first.")
            return
        directory = QFileDialog.getExistingDirectory(self, "Export cascaded Touchstone", str(self.project_root))
        if directory:
            try:
                CoreAPI.export("snp", self.current_result, Path(directory))
                self.status_label.setText("Touchstone export complete")
            except Exception as exc:
                self._show_error("Could not export SNP", exc)

    @pyqtSlot()
    def export_il_csv(self) -> None:
        if self.current_result is None:
            QMessageBox.information(self, "Nothing to export", "Run Cascade or Run Optimization first.")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export insertion loss", "insertion_loss.csv", "CSV (*.csv)")
        if filename:
            try:
                CoreAPI.export("il_csv", self.current_result, Path(filename))
                self.status_label.setText("Insertion-loss CSV exported")
            except Exception as exc:
                self._show_error("Could not export insertion-loss CSV", exc)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.optimization_thread is not None and self.optimization_thread.isRunning():
            answer = QMessageBox.question(
                self,
                "Optimization is running",
                "Cancel optimization and close after the worker stops?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.cancel_optimization()
            self.optimization_thread.quit()
            if not self.optimization_thread.wait(3000):
                event.ignore()
                return
        event.accept()


# Compatibility names for launchers and integrations that use a generic name.
TunerWindow = MainWindow
BPMTunerWindow = MainWindow


def main(project_root: str | Path | None = None) -> int:
    """Launch the desktop application and return the Qt exit code."""

    app = QApplication.instance() or QApplication([])
    window = MainWindow(project_root)
    window.show()
    # Retain a Python reference when embedding into an existing QApplication.
    setattr(app, "_bpm_tuner_window", window)
    return app.exec_()


__all__ = [
    "BPMTunerWindow",
    "CoreAPI",
    "FilesPanel",
    "MainWindow",
    "OptimizationWorker",
    "PlotPanel",
    "PortSettingsPanel",
    "TunerWindow",
    "main",
]
