"""Humanoid definition editor (after Maya HumanIK's Character Definition and Unity's Humanoid
avatar): assign rig controls to FlowHMR joints on a body / hand figure and save a profile.

    import flowhmr_bridge.mapping_ui
    flowhmr_bridge.mapping_ui.show_editor()
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import maya.cmds as cmds
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # Maya 2024 and earlier
    from PySide2 import QtCore, QtGui, QtWidgets

from . import generic, mapping

SLOT_ROLE = QtCore.Qt.UserRole
COLORS = {"ok": QtGui.QColor(90, 190, 110), "driven": QtGui.QColor(224, 160, 64), "missing": QtGui.QColor(210, 80, 80),
          "": QtGui.QColor(43, 43, 43)}
STATUS_TEXT = {"ok": "✓", "driven": "已被驅動或鎖住", "missing": "找不到", "": ""}
_editor = None


def _mirror(points):
    """Points are given for the character's left (screen right in this front view)."""
    result = {}
    for key, (x, y) in points.items():
        result[f"{key}_L"] = (x, y)
        result[f"{key}_R"] = (1.0 - x, y)
    return result


BODY = {"head": (0.50, 0.075), "neck": (0.50, 0.150), "spine2": (0.50, 0.235), "spine1": (0.50, 0.300),
        "spine": (0.50, 0.365), "pelvis": (0.445, 0.430), "spine_base": (0.555, 0.430), "root": (0.50, 0.480)}
BODY.update(_mirror({"clavicle": (0.570, 0.188), "arm": (0.645, 0.205), "forearm": (0.730, 0.315),
                     "hand": (0.800, 0.425), "handaim": (0.835, 0.478), "upleg": (0.555, 0.505),
                     "leg": (0.570, 0.705), "foot": (0.578, 0.895), "toe": (0.625, 0.945),
                     # IK controls (squares) and pole vectors (triangles)
                     "ikhand": (0.885, 0.405), "armpole": (0.815, 0.285), "ikfoot": (0.680, 0.880),
                     "legpole": (0.665, 0.690), "iktoe": (0.735, 0.950)}))
LIMB_LABELS = {"arm_L": "左手臂", "arm_R": "右手臂", "leg_L": "左腿", "leg_R": "右腿"}
_HAND_LEFT = {"hand": (0.50, 0.90), "handaim": (0.58, 0.66),
              "thumb1": (0.36, 0.76), "thumb2": (0.27, 0.66), "thumb3": (0.20, 0.57)}
for _finger, _x, _ys in (("index", 0.42, (0.60, 0.45, 0.32)), ("middle", 0.52, (0.58, 0.42, 0.28)),
                         ("ring", 0.62, (0.60, 0.45, 0.33)), ("pinky", 0.71, (0.65, 0.53, 0.43))):
    for _i, _y in enumerate(_ys, start=1):
        _HAND_LEFT[f"{_finger}{_i}"] = (_x, _y)


def hand_layout(side):
    flip = side == "R"
    return {f"{key}_{side}": ((1.0 - x) if flip else x, y) for key, (x, y) in _HAND_LEFT.items()}


def _slot_kind(key):
    kind = key.rsplit("_", 1)[0]
    if kind in ("armpole", "legpole"):
        return "point"
    if kind in ("ikhand", "ikfoot", "root"):
        return "parent"
    return "orient"


def _number(text):
    text = (text or "").strip()
    if not text:
        return ""
    try:
        value = float(text)
        return int(value) if value.is_integer() else value
    except ValueError:
        return text


def _namespaces():
    spaces = [ns + ":" for ns in cmds.namespaceInfo(":", listOnlyNamespaces=True, recurse=True) or []
              if ns not in ("UI", "shared") and ns != generic.SOURCE_NS]
    return [""] + spaces


# ------------------------------------------------------------------ figure
class FigureView(QtWidgets.QWidget):
    """A clickable humanoid (or hand) figure. Solid ring = required, dashed = optional."""

    def __init__(self, editor, points, kind, parent=None):
        super().__init__(parent)
        self.editor, self.points, self.kind = editor, points, kind
        self.setMouseTracking(True)
        self.setMinimumSize(280, 380)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def _frame(self):
        aspect = 0.75 if self.kind == "body" else 0.85
        h = min(self.height() - 8, (self.width() - 8) / aspect)
        w = h * aspect
        return QtCore.QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _point(self, frame, xy):
        return QtCore.QPointF(frame.left() + xy[0] * frame.width(), frame.top() + xy[1] * frame.height())

    def _radius(self, frame):
        return max(6.0, frame.width() * (0.024 if self.kind == "body" else 0.032))

    def _hit(self, pos):
        frame = self._frame()
        radius = self._radius(frame) + 4
        best, distance = None, radius
        for key, xy in self.points.items():
            d = QtCore.QLineF(pos, self._point(frame, xy)).length()
            if d <= distance:
                best, distance = key, d
        return best

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        frame = self._frame()
        (self._body if self.kind == "body" else self._hand)(painter, frame)
        radius = self._radius(frame)
        for key, xy in self.points.items():
            center = self._point(frame, xy)
            status = self.editor.status_of(key)
            required = key in mapping.REQUIRED or (key in mapping.SPINE_SLOTS and not any(
                self.editor.slot_text(k) for k in mapping.SPINE_SLOTS))
            small = key.startswith("handaim") or key == "spine_base"
            r = radius * (0.72 if small else 1.0)
            if key == self.editor.selected_key:
                painter.setPen(QtGui.QPen(QtGui.QColor(74, 163, 255), 3))
                painter.setBrush(QtCore.Qt.NoBrush)
                painter.drawEllipse(center, r + 4, r + 4)
            pen = QtGui.QPen(QtGui.QColor(220, 220, 220) if required else QtGui.QColor(150, 150, 150), 1.6)
            if not required:
                pen.setStyle(QtCore.Qt.DashLine)
            if required and status == "":
                pen.setColor(QtGui.QColor(210, 80, 80))
            painter.setPen(pen)
            painter.setBrush(QtGui.QBrush(COLORS[status]))
            kind = key.rsplit("_", 1)[0]
            if key == "root":
                diamond = QtGui.QPolygonF([center + QtCore.QPointF(0, -r * 1.3), center + QtCore.QPointF(r * 1.3, 0),
                                           center + QtCore.QPointF(0, r * 1.3), center + QtCore.QPointF(-r * 1.3, 0)])
                painter.drawPolygon(diamond)
            elif kind in ("ikhand", "ikfoot", "iktoe"):
                size = r * (0.8 if kind == "iktoe" else 1.0)
                painter.drawRoundedRect(QtCore.QRectF(center.x() - size, center.y() - size, size * 2, size * 2), 2, 2)
            elif kind in ("armpole", "legpole"):
                painter.drawPolygon(QtGui.QPolygonF([center + QtCore.QPointF(0, -r * 1.25),
                                                     center + QtCore.QPointF(r * 1.15, r * 0.85),
                                                     center + QtCore.QPointF(-r * 1.15, r * 0.85)]))
            else:
                painter.drawEllipse(center, r, r)
        painter.end()

    def _limb(self, painter, frame, points, width):
        pen = QtGui.QPen(QtGui.QColor(74, 74, 74), width, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QtGui.QPolygonF([self._point(frame, p) for p in points]))

    def _body(self, painter, frame):
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(74, 74, 74))
        head = self._point(frame, (0.5, 0.075))
        painter.drawEllipse(head, frame.width() * 0.07, frame.height() * 0.055)
        torso = [(0.405, 0.185), (0.595, 0.185), (0.585, 0.33), (0.575, 0.47), (0.425, 0.47), (0.415, 0.33)]
        painter.drawPolygon(QtGui.QPolygonF([self._point(frame, p) for p in torso]))
        width = frame.width() * 0.055
        self._limb(painter, frame, [(0.5, 0.12), (0.5, 0.19)], width)
        for sign in (1, -1):
            x = lambda v: 0.5 + sign * (v - 0.5)  # noqa: E731
            self._limb(painter, frame, [(x(0.60), 0.195), (x(0.735), 0.315), (x(0.80), 0.425), (x(0.845), 0.50)], width)
            self._limb(painter, frame, [(x(0.548), 0.47), (x(0.570), 0.705), (x(0.578), 0.895), (x(0.63), 0.948)],
                       width * 1.25)
        painter.setPen(QtGui.QColor(150, 150, 150))
        painter.drawText(QtCore.QRectF(frame.left(), frame.top(), frame.width() * 0.3, 20), QtCore.Qt.AlignLeft, "右")
        painter.drawText(QtCore.QRectF(frame.right() - frame.width() * 0.3, frame.top(), frame.width() * 0.3, 20),
                         QtCore.Qt.AlignRight, "左")

    def _hand(self, painter, frame):
        side = next(iter(self.points))[-1]
        flip = (lambda v: 1.0 - v) if side == "R" else (lambda v: v)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor(74, 74, 74))
        palm = [(0.36, 0.93), (0.64, 0.93), (0.76, 0.72), (0.74, 0.62), (0.38, 0.62), (0.32, 0.80)]
        painter.drawPolygon(QtGui.QPolygonF([self._point(frame, (flip(x), y)) for x, y in palm]))
        width = frame.width() * 0.065
        for finger, x, ys in (("index", 0.42, (0.60, 0.45, 0.32)), ("middle", 0.52, (0.58, 0.42, 0.28)),
                              ("ring", 0.62, (0.60, 0.45, 0.33)), ("pinky", 0.71, (0.65, 0.53, 0.43))):
            self._limb(painter, frame, [(flip(x), ys[0] + 0.06)] + [(flip(x), y) for y in ys] + [(flip(x), ys[-1] - 0.08)],
                       width)
        self._limb(painter, frame, [(flip(0.42), 0.84), (flip(0.36), 0.76), (flip(0.27), 0.66), (flip(0.20), 0.57),
                                    (flip(0.15), 0.50)], width)
        painter.setPen(QtGui.QColor(150, 150, 150))
        painter.drawText(QtCore.QRectF(frame.left(), frame.top(), frame.width(), 20), QtCore.Qt.AlignHCenter,
                         "左手" if side == "L" else "右手")

    def mousePressEvent(self, event):
        key = self._hit(event.position() if hasattr(event, "position") else event.pos())
        if key:
            self.editor.select_slot(key)

    def mouseDoubleClickEvent(self, event):
        key = self._hit(event.position() if hasattr(event, "position") else event.pos())
        if key:
            self.editor.select_slot(key)
            self.editor.assign_from_maya()

    def mouseMoveEvent(self, event):
        pos = event.position() if hasattr(event, "position") else event.pos()
        key = self._hit(pos)
        if key:
            text = self.editor.slot_text(key)
            status = STATUS_TEXT[self.editor.status_of(key)]
            need = "必要" if key in mapping.REQUIRED else "可選"
            tip = f"{mapping.SLOT_LABEL[key]}（{need}）\nFlowHMR：{mapping.SLOT_SOURCE[key]}\n控制器：{text or '—'} {status}"
            QtWidgets.QToolTip.showText(event.globalPosition().toPoint() if hasattr(event, "globalPosition")
                                        else event.globalPos(), tip, self)
        else:
            QtWidgets.QToolTip.hideText()

    def _menu(self, pos):
        key = self._hit(QtCore.QPointF(pos))
        if not key:
            return
        self.editor.select_slot(key)
        menu = QtWidgets.QMenu(self)
        menu.addAction("指定選取的控制器", self.editor.assign_from_maya)
        menu.addAction("清除", lambda: self.editor.set_slot(key, ""))
        menu.addAction("在 Maya 選取", lambda: self.editor.select_keys_in_maya([key]))
        menu.exec_(self.mapToGlobal(pos)) if hasattr(menu, "exec_") else menu.exec(self.mapToGlobal(pos))


# ------------------------------------------------------------------ editor
class MappingEditor(MayaQWidgetDockableMixin, QtWidgets.QWidget):
    NAME = "FlowHMRMappingEditor"

    def __init__(self, path=None, namespace=None, on_saved=None, parent=None):
        super().__init__(parent=parent)
        self.setObjectName(self.NAME)
        self.setWindowTitle("FlowHMR 人形定義")
        self.on_saved = on_saved
        self.path, self.extra = None, {}
        self.selected_key = "root"
        self._items, self._status, self._figures = {}, {}, []
        self._saved_state, self._loaded_name, self._closing = None, "", False
        self._build()
        self.refresh_namespaces(namespace)
        if path:
            self.load(path)
        else:
            self.new()
        self.select_slot("root")
        # Cheap comparison of the whole definition, so every kind of edit is noticed.
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh_banner)
        self._timer.start(400)

    # ------------------------------------------------------------------ layout
    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(8, 8, 8, 8)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("設定名稱"))
        self.name_edit = QtWidgets.QLineEdit()
        row.addWidget(self.name_edit, 1)
        for label, slot in (("開新", self.new), ("開啟…", self.open_file), ("儲存", self.save), ("另存…", self.save_as),
                            ("匯入 preset…", self.import_preset_file)):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("從範本"))
        self.template_combo = QtWidgets.QComboBox()
        self._refresh_templates()
        load_template = QtWidgets.QPushButton("載入")
        load_template.clicked.connect(self.load_template)
        row.addWidget(self.template_combo, 1)
        row.addWidget(load_template)
        row.addWidget(QtWidgets.QLabel("namespace"))
        self.ns_combo = QtWidgets.QComboBox()
        self.ns_combo.currentIndexChanged.connect(self.validate)
        refresh = QtWidgets.QToolButton(text="↻")
        refresh.clicked.connect(lambda: self.refresh_namespaces(self.namespace()))
        row.addWidget(self.ns_combo, 1)
        row.addWidget(refresh)
        layout.addLayout(row)

        self.file_label = QtWidgets.QLabel()
        self.file_label.setStyleSheet("color: #909090;")
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)
        self.banner = QtWidgets.QFrame()
        self.banner.setStyleSheet("QFrame { background: #4a3a1c; border-radius: 3px; }")
        banner_row = QtWidgets.QHBoxLayout(self.banner)
        banner_row.setContentsMargins(6, 4, 6, 4)
        self.banner_label = QtWidgets.QLabel()
        self.banner_label.setWordWrap(True)
        self.banner_label.setStyleSheet("color: #f0c060; background: transparent;")
        self.banner_button = QtWidgets.QPushButton("另存…")
        self.banner_button.clicked.connect(self.save)
        banner_row.addWidget(self.banner_label, 1)
        banner_row.addWidget(self.banner_button)
        self.banner.setVisible(False)
        layout.addWidget(self.banner)

        row = QtWidgets.QHBoxLayout()
        auto = QtWidgets.QPushButton("自動偵測")
        auto.setToolTip("依控制器名稱自動配對（會略過 IK、pole、twist、space 等輔助控制器）")
        auto.clicked.connect(self.auto_detect)
        mirror = QtWidgets.QPushButton("左 → 右鏡像")
        mirror.setToolTip("依左側名稱猜右側控制器，只填空白的部位")
        mirror.clicked.connect(self.mirror)
        clear = QtWidgets.QPushButton("全部清除")
        clear.clicked.connect(self.clear_all)
        self.summary = QtWidgets.QLabel()
        for widget in (auto, mirror, clear):
            row.addWidget(widget)
        row.addWidget(self.summary, 1)
        layout.addLayout(row)

        self.tabs = QtWidgets.QTabWidget()
        body = FigureView(self, BODY, "body")
        left = FigureView(self, hand_layout("L"), "hand")
        right = FigureView(self, hand_layout("R"), "hand")
        self._figures = [body, left, right]
        self.tabs.addTab(body, "身體")
        self.tabs.addTab(right, "右手")
        self.tabs.addTab(left, "左手")
        self.tabs.addTab(self._build_list(), "清單")
        layout.addWidget(self.tabs, 1)

        legend = QtWidgets.QLabel(
            "<span style='color:#5abe6e'>●</span> 可用　<span style='color:#e0a040'>●</span> 已被驅動或鎖住　"
            "<span style='color:#d25050'>●</span> 找不到／必要未指定　實線＝必要　虛線＝可選　"
            "■ IK 控制器　▲ Pole　◆ Root　點選部位，雙擊＝指定 Maya 選取的控制器，右鍵＝選單")
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #a0a0a0;")
        layout.addWidget(legend)

        detail = QtWidgets.QGroupBox("部位")
        grid = QtWidgets.QGridLayout(detail)
        self.detail_title = QtWidgets.QLabel()
        grid.addWidget(self.detail_title, 0, 0, 1, 4)
        grid.addWidget(QtWidgets.QLabel("控制器"), 1, 0)
        self.detail_edit = QtWidgets.QLineEdit()
        self.detail_edit.editingFinished.connect(lambda: self.set_slot(self.selected_key, self.detail_edit.text()))
        grid.addWidget(self.detail_edit, 1, 1, 1, 3)
        assign = QtWidgets.QPushButton("指定選取的控制器")
        assign.setToolTip("Maya 中選了多個控制器時，會從這個部位開始依序往下填")
        assign.clicked.connect(self.assign_from_maya)
        clear_one = QtWidgets.QPushButton("清除")
        clear_one.clicked.connect(lambda: self.set_slot(self.selected_key, ""))
        select = QtWidgets.QPushButton("在 Maya 選取")
        select.clicked.connect(lambda: self.select_keys_in_maya([self.selected_key]))
        self.advance_check = QtWidgets.QCheckBox("指定後跳到下一個部位")
        self.advance_check.setChecked(True)
        grid.addWidget(assign, 2, 0)
        grid.addWidget(clear_one, 2, 1)
        grid.addWidget(select, 2, 2)
        grid.addWidget(self.advance_check, 2, 3)
        layout.addWidget(detail)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("靜置姿勢"))
        self.rest_zero = QtWidgets.QRadioButton("控制器歸零")
        self.rest_current = QtWidgets.QRadioButton("目前姿勢（T／A Pose 皆可）")
        self.rest_zero.setToolTip("套用前把對應的控制器歸零；大多數 rig 歸零就是綁定姿勢")
        row.addWidget(self.rest_zero)
        row.addWidget(self.rest_current)
        row.addStretch(1)
        layout.addLayout(row)

        limb_box = QtWidgets.QGroupBox("四肢 FK／IK（有對應到的 FK、IK 都會烘焙；這裡決定套用後顯示哪一個）")
        limb_layout = QtWidgets.QVBoxLayout(limb_box)
        self.limb_table = QtWidgets.QTableWidget(len(LIMB_LABELS), 4)
        self.limb_table.setHorizontalHeaderLabels(["顯示", "切換屬性（控制器.屬性）", "FK 值", "IK 值"])
        self.limb_table.setVerticalHeaderLabels(list(LIMB_LABELS.values()))
        header = self.limb_table.horizontalHeader()
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        for column in (0, 2, 3):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeToContents)
        self._mode_boxes = {}
        for row, key in enumerate(LIMB_LABELS):
            box = QtWidgets.QComboBox()
            box.addItem("FK", "fk")
            box.addItem("IK", "ik")
            self.limb_table.setCellWidget(row, 0, box)
            self._mode_boxes[key] = box
            for column in (1, 2, 3):
                self.limb_table.setItem(row, column, QtWidgets.QTableWidgetItem(""))
        self.limb_table.setFixedHeight(150)
        limb_layout.addWidget(self.limb_table)
        detect = QtWidgets.QPushButton("偵測切換屬性")
        detect.setToolTip("在四肢的控制器和名稱含 fkik／ikfk／switch／ui／settings 的控制器上，找像 FKIKBlend、ikfk、*_blend 的屬性")
        detect.clicked.connect(self.detect_switches)
        limb_layout.addWidget(detect)
        layout.addWidget(limb_box)

        hold_box = QtWidgets.QGroupBox("其他烘焙時固定的屬性（名稱不含 namespace）")
        hold_layout = QtWidgets.QVBoxLayout(hold_box)
        self.hold_table = QtWidgets.QTableWidget(0, 2)
        self.hold_table.setHorizontalHeaderLabels(["屬性（控制器.屬性）", "值"])
        self.hold_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.hold_table.setFixedHeight(96)
        hold_layout.addWidget(self.hold_table)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("新增")
        add.clicked.connect(lambda: self._add_hold("", ""))
        add_selected = QtWidgets.QPushButton("加入 Channel Box 選取的屬性")
        add_selected.clicked.connect(self.add_channel_box_selection)
        remove = QtWidgets.QPushButton("刪除")
        remove.clicked.connect(lambda: [self.hold_table.removeRow(r) for r in
                                        sorted({i.row() for i in self.hold_table.selectedIndexes()}, reverse=True)])
        for button in (add, add_selected, remove):
            row.addWidget(button)
        hold_layout.addLayout(row)
        layout.addWidget(hold_box)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _build_list(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["部位", "FlowHMR 關節", "控制器", "狀態"])
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(lambda item, _: self.select_keys_in_maya([item.data(0, SLOT_ROLE)]))
        self.tree.itemSelectionChanged.connect(self._tree_selection)
        groups = {}
        for key, source, label, group in mapping.SLOTS:
            if group not in groups:
                groups[group] = QtWidgets.QTreeWidgetItem(self.tree, [group])
                groups[group].setFlags(QtCore.Qt.ItemIsEnabled)
                groups[group].setExpanded("手指" not in group)
            item = QtWidgets.QTreeWidgetItem(groups[group], [label, source, "", ""])
            item.setData(0, SLOT_ROLE, key)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
            self._items[key] = item
        header = self.tree.header()
        header.setStretchLastSection(False)
        for column, mode in ((0, QtWidgets.QHeaderView.ResizeToContents), (1, QtWidgets.QHeaderView.ResizeToContents),
                             (2, QtWidgets.QHeaderView.Stretch), (3, QtWidgets.QHeaderView.ResizeToContents)):
            header.setSectionResizeMode(column, mode)
        layout.addWidget(self.tree, 1)
        row = QtWidgets.QHBoxLayout()
        assign = QtWidgets.QPushButton("依序指定給選取的列")
        assign.setToolTip("選多列後，到 Maya 依相同順序選取控制器")
        assign.clicked.connect(self.assign_rows)
        clear = QtWidgets.QPushButton("清除選取的列")
        clear.clicked.connect(lambda: [self.set_slot(i.data(0, SLOT_ROLE), "", refresh=False) for i in self._selected_rows()]
                              and self.validate())
        row.addWidget(assign)
        row.addWidget(clear)
        layout.addLayout(row)
        return page

    # ------------------------------------------------------------------ state
    def namespace(self):
        return self.ns_combo.currentData() or ""

    def refresh_namespaces(self, prefer=None):
        self.ns_combo.blockSignals(True)
        self.ns_combo.clear()
        for ns in _namespaces():
            self.ns_combo.addItem(ns or "（無 namespace）", ns)
        if prefer is not None:
            self.ns_combo.setCurrentIndex(max(0, self.ns_combo.findData(prefer)))
        self.ns_combo.blockSignals(False)
        self.validate()

    def slot_text(self, key):
        item = self._items.get(key)
        return item.text(2).strip() if item else ""

    def status_of(self, key):
        return self._status.get(key, "")

    def slots(self):
        return {key: self.slot_text(key) for key in self._items if self.slot_text(key)}

    def set_slot(self, key, name, refresh=True):
        item = self._items.get(key)
        if item is None:
            return
        self.tree.blockSignals(True)
        item.setText(2, (name or "").strip())
        self.tree.blockSignals(False)
        if refresh:
            self.validate()
            if key == self.selected_key:
                self.detail_edit.setText(self.slot_text(key))

    def select_slot(self, key):
        if key not in self._items:
            return
        self.selected_key = key
        need = "必要" if key in mapping.REQUIRED else "可選"
        self.detail_title.setText(f"<b>{mapping.SLOT_LABEL[key]}</b>　FlowHMR：{mapping.SLOT_SOURCE[key]}　（{need}）")
        self.detail_edit.setText(self.slot_text(key))
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        self._items[key].setSelected(True)
        self.tree.blockSignals(False)
        for figure in self._figures:
            figure.update()

    def _tree_selection(self):
        rows = self._selected_rows()
        if len(rows) == 1:
            self.select_slot(rows[0].data(0, SLOT_ROLE))

    def _selected_rows(self):
        return [item for item in self.tree.selectedItems() if item.data(0, SLOT_ROLE)]

    def _short(self, node):
        name = node.split("|")[-1]
        ns = self.namespace()
        if ns and name.startswith(ns):
            return name[len(ns):]
        return name.rsplit(":", 1)[-1] if ns else name

    # ------------------------------------------------------------------ assigning
    def assign_from_maya(self):
        picked = cmds.ls(selection=True, transforms=True) or []
        if not picked:
            self.status.setText("請先在 Maya 選取控制器。")
            return
        # The hand-aim reference is optional; chains and auto-advance step over it.
        order = [k for k in mapping.SLOT_ORDER if not k.startswith("handaim") or k == self.selected_key]
        start = order.index(self.selected_key)
        keys = order[start:start + len(picked)]
        for key, node in zip(keys, picked):
            self.set_slot(key, self._short(node), refresh=False)
        self.validate()
        if len(picked) > 1:
            self.status.setText(f"從「{mapping.SLOT_LABEL[keys[0]]}」開始依序指定了 {len(keys)} 個部位。")
        last = keys[-1]
        nxt = order[order.index(last) + 1] if self.advance_check.isChecked() and order.index(last) + 1 < len(order) else last
        self.select_slot(nxt)

    def assign_rows(self):
        rows = self._selected_rows()
        picked = cmds.ls(selection=True, transforms=True) or []
        if not rows or not picked:
            self.status.setText("請先在清單選列，再到 Maya 依相同順序選取控制器。")
            return
        for item, node in zip(rows, picked):
            self.set_slot(item.data(0, SLOT_ROLE), self._short(node), refresh=False)
        self.validate()

    def select_keys_in_maya(self, keys):
        ns = self.namespace()
        nodes = [ns + self.slot_text(k) for k in keys if self.slot_text(k) and cmds.objExists(ns + self.slot_text(k))]
        if nodes:
            cmds.select(nodes, replace=True)

    def clear_all(self):
        if self.slots() and not self._confirm("清除所有部位的指定？"):
            return
        for key in self._items:
            self.set_slot(key, "", refresh=False)
        self.validate()
        self.select_slot(self.selected_key)

    def mirror(self):
        ns, filled = self.namespace(), 0
        for key in self._items:
            if not key.endswith("_L") or not self.slot_text(key):
                continue
            target = key[:-2] + "_R"
            if target not in self._items or self.slot_text(target):
                continue
            for candidate in mapping.mirror_candidates(self.slot_text(key)):
                if cmds.objExists(ns + candidate):
                    self.set_slot(target, candidate, refresh=False)
                    filled += 1
                    break
        self.validate()
        self.select_slot(self.selected_key)
        self.status.setText(f"鏡像填入 {filled} 個右側部位。")

    def _candidates(self):
        """Control curves (or joints, for a rig without curves) in the chosen namespace."""
        ns = self.namespace()
        shapes = cmds.ls(f"{ns}*" if ns else "*", type="nurbsCurve", long=True) or []
        nodes = {cmds.listRelatives(s, parent=True, fullPath=True)[0] for s in shapes}
        if not nodes:
            nodes = set(cmds.ls(f"{ns}*" if ns else "*", type="joint", long=True) or [])
        result = []
        for node in nodes:
            short = node.split("|")[-1]
            if (ns and not short.startswith(ns)) or (not ns and ":" in short):
                continue
            result.append({"name": short[len(ns):], "y": cmds.xform(node, q=True, ws=True, rotatePivot=True)[1]})
        return result

    def auto_detect(self):
        if self.slots() and not self._confirm("自動偵測會取代目前所有部位的指定，要繼續嗎？"):
            return
        found, _ = mapping.automap(self._candidates())
        for key in self._items:
            self.set_slot(key, found.get(key, ""), refresh=False)
        self.validate()
        self.select_slot(self.selected_key)
        missing = mapping.missing_required(found)
        self.status.setText(f"自動偵測指定了 {len(found)} 個部位。"
                            + (f"必要部位還缺：{'、'.join(mapping.SLOT_LABEL[k] for k in missing)}。" if missing else
                               "必要部位都有了，請逐一檢查是否正確。"))

    def add_channel_box_selection(self):
        attrs = cmds.channelBox("mainChannelBox", q=True, selectedMainAttributes=True) or []
        nodes = cmds.ls(selection=True) or []
        if not attrs or not nodes:
            self.status.setText("請在 Maya 選取控制器，並在 Channel Box 選取要固定的屬性。")
            return
        for node in nodes:
            for attr in attrs:
                plug = f"{node}.{attr}"
                if cmds.objExists(plug):
                    self._add_hold(f"{self._short(node)}.{attr}", cmds.getAttr(plug))

    def _confirm(self, text):
        return QtWidgets.QMessageBox.question(self, "FlowHMR 人形定義", text, QtWidgets.QMessageBox.Yes |
                                              QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes

    def _item_changed(self, item, column):
        if column == 2:
            self.validate()
            if item.data(0, SLOT_ROLE) == self.selected_key:
                self.detail_edit.setText(item.text(2))

    def validate(self, *_):
        if not hasattr(self, "tree"):
            return
        ns = self.namespace()
        counts = {"ok": 0, "missing": 0, "driven": 0}
        self.tree.blockSignals(True)
        for key, item in self._items.items():
            name = item.text(2).strip()
            if not name:
                status = ""
            elif not cmds.objExists(ns + name):
                status = "missing"
            elif key not in ("root", "handaim_L", "handaim_R") and all(
                    generic.rig_driven(plug) for plug in generic._plugs(ns + name, _slot_kind(key))):
                status = "driven"
            else:
                status = "ok"
            self._status[key] = status
            if status:
                counts[status] += 1
            item.setText(3, STATUS_TEXT[status])
            item.setForeground(3, QtGui.QBrush(COLORS[status] if status else QtGui.QColor(0, 0, 0, 0)))
        self.tree.blockSignals(False)
        missing = mapping.missing_required(self.slots())
        total = len(mapping.REQUIRED) + 1
        self.summary.setText(f"必要部位 {total - len(missing)}／{total}" +
                             (f"　缺：{'、'.join(mapping.SLOT_LABEL[k] for k in missing[:3])}{'…' if len(missing) > 3 else ''}"
                              if missing else "　✓"))
        self.status.setText(f"可用 {counts['ok']}、找不到 {counts['missing']}、已被驅動 {counts['driven']}。")
        for figure in self._figures:
            figure.update()

    # ------------------------------------------------------------------ files
    def hold(self):
        values = {}
        for row in range(self.hold_table.rowCount()):
            plug = (self.hold_table.item(row, 0) or QtWidgets.QTableWidgetItem("")).text().strip()
            text = (self.hold_table.item(row, 1) or QtWidgets.QTableWidgetItem("")).text().strip()
            if not plug or not text:
                continue
            try:
                value = float(text)
                values[plug] = int(value) if value.is_integer() else value
            except ValueError:
                values[plug] = text
        return values

    def _add_hold(self, plug, value):
        row = self.hold_table.rowCount()
        self.hold_table.insertRow(row)
        self.hold_table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(plug)))
        self.hold_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(value)))

    def limbs(self):
        result = {}
        for row, key in enumerate(LIMB_LABELS):
            result[key] = {"mode": self._mode_boxes[key].currentData(),
                           "switch": self.limb_table.item(row, 1).text().strip(),
                           "fk": _number(self.limb_table.item(row, 2).text()),
                           "ik": _number(self.limb_table.item(row, 3).text())}
        return result

    def _fill_limbs(self, settings):
        for row, key in enumerate(LIMB_LABELS):
            setting = settings.get(key, {})
            box = self._mode_boxes[key]
            box.setCurrentIndex(max(0, box.findData(setting.get("mode", "fk"))))
            for column, field in ((1, "switch"), (2, "fk"), (3, "ik")):
                value = setting.get(field, "")
                self.limb_table.item(row, column).setText("" if value is None else str(value))

    def detect_switches(self):
        """Find an FK/IK switch attribute for each limb and guess its FK and IK values."""
        ns, found = self.namespace(), []
        limb_slots = {"arm": ("arm", "forearm", "hand", "clavicle", "ikhand", "armpole"),
                      "leg": ("upleg", "leg", "foot", "toe", "ikfoot", "legpole", "iktoe")}
        limb_words = {"arm": {"arm", "hand", "wrist", "elbow"}, "leg": {"leg", "foot", "knee", "ankle"}}
        candidates = self._candidates()
        for row, key in enumerate(LIMB_LABELS):
            limb, side = key.split("_")
            side_words = mapping.LEFT if side == "L" else mapping.RIGHT
            names = [self.slot_text(f"{k}_{side}") for k in limb_slots[limb] if self.slot_text(f"{k}_{side}")]
            for item in candidates:
                words = set(mapping.tokens(item["name"]))
                if words & {"fkik", "ikfk", "switch", "ui", "settings"} and words & limb_words[limb] and words & side_words:
                    names.append(item["name"])
            for name in names:
                node = ns + name
                if not cmds.objExists(node):
                    continue
                attr = next((a for a in cmds.listAttr(node, userDefined=True, keyable=True) or []
                             if mapping.is_switch_attr(a)), None)
                if not attr:
                    continue
                low = cmds.attributeQuery(attr, node=node, minimum=True)[0] \
                    if cmds.attributeQuery(attr, node=node, minExists=True) else None
                high = cmds.attributeQuery(attr, node=node, maximum=True)[0] \
                    if cmds.attributeQuery(attr, node=node, maxExists=True) else None
                values = mapping.switch_values(attr, low, high)
                self.limb_table.item(row, 1).setText(f"{name}.{attr}")
                self.limb_table.item(row, 2).setText(str(_number(str(values["fk"]))))
                self.limb_table.item(row, 3).setText(str(_number(str(values["ik"]))))
                for hold_row in reversed(range(self.hold_table.rowCount())):  # the limb table owns this plug now
                    item = self.hold_table.item(hold_row, 0)
                    if item and item.text().strip() == f"{name}.{attr}":
                        self.hold_table.removeRow(hold_row)
                ik_kind = "ikhand" if limb == "arm" else "ikfoot"
                if self.slot_text(f"{ik_kind}_{side}"):
                    self._mode_boxes[key].setCurrentIndex(self._mode_boxes[key].findData("ik"))
                found.append(LIMB_LABELS[key])
                break
        missing = [label for label in LIMB_LABELS.values() if label not in found]
        self.status.setText(f"找到 {len(found)} 個切換屬性。" + (f"沒找到：{'、'.join(missing)}，可以手動輸入。" if missing else
                                                         "FK／IK 值是依屬性名稱和範圍猜的，請在 Maya 確認一次。"))

    def _fill(self, name, slots, rest, hold, extra, path, limbs=None):
        for key in self._items:
            self.set_slot(key, slots.get(key, ""), refresh=False)
        self._fill_limbs(limbs or {})
        self.name_edit.setText(name)
        (self.rest_current if rest == "current" else self.rest_zero).setChecked(True)
        self.hold_table.setRowCount(0)
        for plug, value in hold.items():
            self._add_hold(plug, value)
        self.extra = dict(extra)
        self.path = Path(path) if path else None
        self._show_file()
        self.validate()
        self.select_slot(self.selected_key)
        self._loaded_name = name
        self._mark_saved()

    def _show_file(self):
        if not self.path:
            text = "尚未存檔"
        elif self.readonly():
            kind = "內建範本" if self.path.parent.resolve() == generic.PROFILE_DIR.resolve() else "preset"
            text = f"{self.path}（{kind}：不能直接修改，儲存時會另存成新的設定檔）"
        else:
            text = str(self.path)
        self.file_label.setText(text)

    # ------------------------------------------------------------------ unsaved edits
    def readonly(self):
        return self.path is not None and generic.is_preset(self.path)

    def _state(self):
        try:
            hold = self.hold()
        except Exception:
            hold = {}
        return json.dumps({"name": self.name_edit.text().strip(), "slots": self.slots(), "hold": hold,
                           "rest": self.rest_current.isChecked(), "limbs": self.limbs()},
                          sort_keys=True, ensure_ascii=False, default=str)

    def is_dirty(self):
        return self._saved_state is not None and self._state() != self._saved_state

    def _mark_saved(self):
        self._saved_state = self._state()
        self._refresh_banner()

    def _refresh_banner(self):
        if not hasattr(self, "banner"):
            return
        dirty = self.is_dirty()
        if dirty and self.readonly():
            self.banner_label.setText("這是 preset，不能直接修改。要保留這些改動，請按「另存…」存成新的設定檔。")
            self.banner_button.setText("另存…")
        elif dirty:
            self.banner_label.setText("有尚未儲存的修改。")
            self.banner_button.setText("儲存")
        self.banner.setVisible(dirty)

    def _ask_unsaved(self, allow_cancel=True):
        """'save', 'discard' or 'cancel' for the current unsaved edits."""
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("FlowHMR 人形定義")
        name = self.path.name if self.path else (self.name_edit.text().strip() or "新的設定")
        if self.readonly():
            box.setText(f"「{name}」是 preset，修改不能存回去。要另存成新的設定檔嗎？")
            save = box.addButton("另存…", QtWidgets.QMessageBox.AcceptRole)
        else:
            box.setText(f"「{name}」有尚未儲存的修改，要儲存嗎？")
            save = box.addButton("儲存", QtWidgets.QMessageBox.AcceptRole)
        discard = box.addButton("不儲存", QtWidgets.QMessageBox.DestructiveRole)
        cancel = box.addButton("取消", QtWidgets.QMessageBox.RejectRole) if allow_cancel else None
        box.setDefaultButton(save)
        (box.exec if hasattr(box, "exec") else box.exec_)()
        clicked = box.clickedButton()
        return "save" if clicked is save else "discard" if clicked is discard else "cancel"

    def resolve_unsaved(self, allow_cancel=True):
        """True when the current edits may be dropped: nothing changed, saved, or the user said so."""
        if not self.is_dirty():
            return True
        answer = self._ask_unsaved(allow_cancel)
        if answer == "save":
            return self.save() is not None
        return answer == "discard"

    def _write_recovery(self):
        """Closing cannot be cancelled: keep edits the user did not save or discard."""
        folder = generic.user_profile_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w\-]+", "_", self.name_edit.text().strip() or "my_rig").strip("_") or "my_rig"
        path = folder / f"{stem}_未儲存_{time.strftime('%Y%m%d_%H%M%S')}.json"
        try:
            data = self._profile()
        except ValueError:
            data = json.loads(self._state())
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"// FlowHMR 人形定義：未儲存的修改已另存到 {path}")
        return path

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        if self.is_dirty():
            answer = self._ask_unsaved(allow_cancel=False)
            saved = answer == "save" and self.save() is not None
            if not saved and answer != "discard":
                self._write_recovery()

    def dockCloseEventTriggered(self):
        self._on_close()

    def closeEvent(self, event):
        self._on_close()
        super().closeEvent(event)

    def new(self):
        if self._saved_state is not None and not self.resolve_unsaved():
            return
        self._fill("我的 rig", {}, "zero", {}, {}, None)

    def load(self, path):
        profile = generic.load_profile(path)
        slots, rest, hold, extra = mapping.slots_from_profile(profile)
        guess = generic.namespaces_with(profile) if slots.get("root") else []
        if guess and self.namespace() not in guess:
            self.ns_combo.setCurrentIndex(max(0, self.ns_combo.findData(guess[0])))
        self._fill(profile.get("name", Path(path).stem), slots, rest, hold, extra, path, mapping.limb_settings(profile))

    def load_template(self):
        path = self.template_combo.currentData()
        if not path or not self.resolve_unsaved():
            return
        self.load(path)
        self.extra.pop("test", None)
        self.path = None
        self.name_edit.setText(self.name_edit.text().replace("（內建範本）", "") + "（複本）")
        self.file_label.setText("尚未存檔（由範本建立）")
        self._loaded_name = self.name_edit.text()
        self._mark_saved()

    def _refresh_templates(self):
        current = self.template_combo.currentData()
        self.template_combo.clear()
        for name, path, _read_only in generic.list_profiles():
            self.template_combo.addItem(name, str(path))
        index = self.template_combo.findData(current) if current else -1
        if index >= 0:
            self.template_combo.setCurrentIndex(index)

    def open_file(self):
        if not self.resolve_unsaved():
            return
        start = str(generic.user_profile_dir())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "開啟人形定義", start, "JSON (*.json)")
        if path:
            self.load(path)

    def import_preset_file(self):
        if not self.resolve_unsaved():
            return
        start = str(generic.user_profile_dir())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "匯入 preset（複製到 preset 資料夾，之後不能直接修改）",
                                                        start, "JSON (*.json)")
        if path:
            self.import_preset(path)

    def import_preset(self, path):
        try:
            target = generic.import_preset(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            QtWidgets.QMessageBox.warning(self, "FlowHMR 人形定義", f"不是可用的人形定義檔：{error}")
            return None
        self._refresh_templates()
        self.load(target)
        self.status.setText(f"已匯入 preset：{target.name}。修改後要用「另存…」存成新的設定檔。")
        if self.on_saved:
            self.on_saved(str(target))
        return target

    def _profile(self):
        return mapping.build_profile(self.name_edit.text().strip() or "未命名", self.slots(),
                                     "current" if self.rest_current.isChecked() else "zero", self.hold(), self.extra,
                                     limbs=self.limbs())

    def save(self):
        if self.path is None or self.readonly():
            return self.save_as()
        return self._write(self.path)

    def _choose_save_path(self, default):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "另存人形定義", str(default), "JSON (*.json)")
        return Path(path) if path else None

    def save_as(self):
        folder = generic.user_profile_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = self.name_edit.text().strip() or "my_rig"
        if self.readonly() and name == self._loaded_name:
            name += "（複本）"  # the copy should not look like the preset in the profile list
        stem = re.sub(r"[^\w\-]+", "_", name).strip("_") or "my_rig"
        path = self._choose_save_path(folder / f"{stem}.json")
        if not path:
            return None
        if generic.is_preset(path):
            QtWidgets.QMessageBox.warning(self, "FlowHMR 人形定義",
                                          "preset 和內建範本的資料夾不能存檔。請存到其他位置（預設是 profiles 資料夾）。")
            return None
        self.name_edit.setText(name)
        return self._write(path)

    def _write(self, path):
        if generic.is_preset(path):
            QtWidgets.QMessageBox.warning(self, "FlowHMR 人形定義", f"{path.name} 是 preset，不能直接修改。請用「另存…」。")
            return None
        try:
            profile = self._profile()
        except ValueError as error:
            QtWidgets.QMessageBox.warning(self, "FlowHMR 人形定義", str(error))
            return None
        missing = mapping.missing_required(self.slots())
        if missing and not self._confirm(f"必要部位還沒指定：{'、'.join(mapping.SLOT_LABEL[k] for k in missing)}。\n"
                                         "沒指定的部位不會被驅動。仍要儲存嗎？"):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        self.path = path
        self._show_file()
        self._loaded_name = profile.get("name", "")
        self._mark_saved()
        self._refresh_templates()
        self.status.setText(f"已儲存：{path.name}（{len(self.slots())} 個部位）")
        if self.on_saved:
            self.on_saved(str(path))
        return path


def show_editor(path=None, namespace=None, on_saved=None):
    global _editor
    control = MappingEditor.NAME + "WorkspaceControl"
    if _editor is not None:
        try:  # an editor that was closed already asked about its edits
            if not _editor._closing and not _editor.resolve_unsaved():
                _editor.raise_()
                return _editor  # keep the open window and its edits
            _editor._closing = True
        except RuntimeError:  # its Qt object is already gone
            pass
    if cmds.workspaceControl(control, exists=True):
        cmds.deleteUI(control)
    _editor = MappingEditor(path=path, namespace=namespace, on_saved=on_saved)
    _editor.show(dockable=True, floating=True, retain=False, width=640, height=900)
    return _editor
