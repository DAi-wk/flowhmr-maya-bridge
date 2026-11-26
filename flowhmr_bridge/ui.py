"""Dockable Maya panel: pick a finished FlowHMR job and apply it to a rig.

Five modes:
  原始動作          — the FlowHMR preview solver drives ADV from the job's SMPL-H samples
  AdvancedSkeleton — ADV's own MoCapLibrary (flowhmr_bridge.adv)
  自訂對應          — any rig through a mapping profile (flowhmr_bridge.generic), edited in
                      the separate mapping editor (flowhmr_bridge.mapping_ui)
  自訂＋HMR 求解    — the same profile, but FlowHMR's preview solver runs on the rig's skin
                      skeleton and the controls follow it (flowhmr_bridge.custom_preview)
  HMR 角色 FBX      — an ADV rig follows the character FBX FlowHMR's preview exported for that
                      rig's own skin skeleton (flowhmr_bridge.character); a local file, no download

    import flowhmr_bridge.ui
    flowhmr_bridge.ui.show()
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

import maya.cmds as cmds
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # Maya 2024 and earlier
    from PySide2 import QtCore, QtGui, QtWidgets

from . import __version__, adv, character, custom_preview, generic, layers
from . import client as client_mod
from . import timing as timing_mod

OPTION_ROOT = "flowhmrBridgeRoot"
OPTION_MODE = "flowhmrBridgeMode"
OPTION_PROFILE = "flowhmrBridgeProfile"
OPTION_CHAR_DIR = "flowhmrBridgeCharacterDir"
OPTION_REFINE = "flowhmrBridgeRefineSpread"
CUSTOM_MODES = ("custom", "custom_hmr")
PREVIEW_MODES = ("raw", "custom_hmr")  # read the job's SMPL-H stream and run FlowHMR's preview solver
POLL_MS = 3000
CHARACTER_HINT = ("在 FlowHMR 預覽裡，替這個人物套用 rig 自己的蒙皮 FBX 當角色並儲存重定向，"
                  "再按「匯出 FBX…」，格式選「目前角色 FBX」（不用勾「包含蒙皮網格」）。"
                  "瀏覽器會把檔案存到下載資料夾，選了任務後這裡會自動找最新的一個。")
STATUS = {"done": "完成", "running": "生成中", "queued": "排隊中", "waiting_user": "等待選人（請在 FlowHMR 選擇）",
          "failed": "失敗", "cancelled": "已取消", "cancel_requested": "取消中"}
_panel = None


class _Signals(QtCore.QObject):
    jobs = QtCore.Signal(object)
    thumbnail = QtCore.Signal(str, object)
    fbx = QtCore.Signal(object)


def _option(name, default):
    return cmds.optionVar(q=name) if cmds.optionVar(exists=name) else default


class BridgePanel(MayaQWidgetDockableMixin, QtWidgets.QWidget):
    NAME = "FlowHMRBridgePanel"

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName(self.NAME)
        self.setWindowTitle(f"FlowHMR Bridge {__version__}")
        self.client = client_mod.FlowHMRClient(root=_option(OPTION_ROOT, str(client_mod.DEFAULT_ROOT)))
        self.signals = _Signals()
        self.signals.jobs.connect(self._on_jobs)
        self.signals.thumbnail.connect(self._on_thumbnail)
        self.signals.fbx.connect(self._on_fbx)
        self._jobs, self._signature, self._thumbs, self._asked = [], None, {}, set()
        self._polling = self._busy = False
        self.connected = False
        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)
        self.refresh_rigs()
        self.poll(force=True)

    # ------------------------------------------------------------------ layout
    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        top = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel()
        self.status.setTextFormat(QtCore.Qt.RichText)
        refresh = QtWidgets.QToolButton(text="重新整理")
        refresh.clicked.connect(lambda: (self.refresh_rigs(), self.poll(force=True)))
        top.addWidget(self.status, 1)
        top.addWidget(refresh)
        layout.addLayout(top)

        root_row = QtWidgets.QHBoxLayout()
        root_row.addWidget(QtWidgets.QLabel("FlowHMR 資料夾"))
        self.root_edit = QtWidgets.QLineEdit(str(self.client.root))
        self.root_edit.editingFinished.connect(self._root_changed)
        browse = QtWidgets.QToolButton(text="…")
        browse.clicked.connect(self._browse_root)
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(browse)
        layout.addLayout(root_row)

        layout.addWidget(QtWidgets.QLabel("FlowHMR 任務"))
        self.jobs_list = QtWidgets.QListWidget()
        self.jobs_list.setIconSize(QtCore.QSize(72, 72))
        self.jobs_list.setSpacing(2)
        self.jobs_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.jobs_list.setTextElideMode(QtCore.Qt.ElideMiddle)
        self.jobs_list.itemSelectionChanged.connect(lambda: (self._suggest_character(), self._update_state()))
        layout.addWidget(self.jobs_list, 1)

        mode_form = QtWidgets.QFormLayout()
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItem("原始動作 → ADV（HMR 預覽求解器）", "raw")
        self.mode_combo.addItem("AdvancedSkeleton（ADV 動捕庫）", "adv")
        self.mode_combo.addItem("自訂對應（任意 rig）", "custom")
        self.mode_combo.addItem("自訂對應＋HMR 預覽求解（任意 rig）", "custom_hmr")
        self.mode_combo.addItem("HMR 角色 FBX（ADV rig，照 FlowHMR 預覽）", "character")
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(_option(OPTION_MODE, "raw"))))
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_form.addRow("rig 類型", self.mode_combo)
        layout.addLayout(mode_form)

        # ADV mode
        self.adv_box = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.adv_box)
        form.setContentsMargins(0, 0, 0, 0)
        rig_row = QtWidgets.QHBoxLayout()
        self.rig_combo = QtWidgets.QComboBox()
        self.rig_combo.currentIndexChanged.connect(self._update_state)
        rig_refresh = QtWidgets.QToolButton(text="↻")
        rig_refresh.setToolTip("重新掃描場景中的 ADV rig")
        rig_refresh.clicked.connect(self.refresh_rigs)
        rig_row.addWidget(self.rig_combo, 1)
        rig_row.addWidget(rig_refresh)
        form.addRow("目標 rig", rig_row)
        layout.addWidget(self.adv_box)

        # custom mode
        self.custom_box = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.custom_box)
        form.setContentsMargins(0, 0, 0, 0)
        profile_row = QtWidgets.QHBoxLayout()
        self.profile_combo = QtWidgets.QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        edit = QtWidgets.QToolButton(text="編輯…")
        edit.setToolTip("在人形定義面板中開啟這個設定")
        edit.clicked.connect(lambda: self.open_editor(self.profile_combo.currentData()))
        new = QtWidgets.QToolButton(text="新增…")
        new.setToolTip("開啟人形定義面板，建立新的人形定義（可用「自動偵測」）")
        new.clicked.connect(lambda: self.open_editor(None))
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(edit)
        profile_row.addWidget(new)
        form.addRow("對應設定", profile_row)
        ns_row = QtWidgets.QHBoxLayout()
        self.ns_combo = QtWidgets.QComboBox()
        self.ns_combo.currentIndexChanged.connect(self._update_state)
        ns_refresh = QtWidgets.QToolButton(text="↻")
        ns_refresh.clicked.connect(self.refresh_rigs)
        ns_row.addWidget(self.ns_combo, 1)
        ns_row.addWidget(ns_refresh)
        form.addRow("目標 namespace", ns_row)
        layout.addWidget(self.custom_box)

        # preview-solver options (原始動作 and 自訂＋HMR 求解)
        self.use_character = QtWidgets.QCheckBox("使用 HMR 已儲存的角色設定")
        # On by default: HMR's saved joint mapping and 校正值 (joint and root offsets) are part of
        # the preview; without them a tuned preview cannot be reproduced.
        self.use_character.setChecked(True)
        self.use_character.setToolTip("勾選時讀取 HMR 為這個人物儲存的角色、關節對應和校正值，結果和 HMR 預覽相同"
                                      "（那個角色必須是這個 rig 的蒙皮 FBX）。取消勾選時只讀原始動作，"
                                      "從場景中 rig 的 build pose 建立對應，HMR 的校正值不會套用。")
        layout.addWidget(self.use_character)
        self.refine_check = QtWidgets.QCheckBox("分散型控制器用數值求解（ribbon 脊椎等，較慢）")
        self.refine_check.setChecked(bool(_option(OPTION_REFINE, 0)))
        self.refine_check.setToolTip("一個控制器把旋轉分給好幾節關節時（例如 ribbon 脊椎），逐幀求解它的旋轉，讓那些關節整體盡量貼近 "
                                     "HMR 預覽。每幀從不勾時的結果出發，只接受讓整體誤差變小的解，但個別關節仍可能變差。"
                                     "每段動作多花 1–4 分鐘；只會用在探測結果是「分散」的控制器。")
        self.refine_check.toggled.connect(lambda on: cmds.optionVar(iv=(OPTION_REFINE, int(on))))
        layout.addWidget(self.refine_check)

        # character FBX mode (uses the ADV rig row above)
        self.char_box = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.char_box)
        form.setContentsMargins(0, 0, 0, 0)
        file_row = QtWidgets.QHBoxLayout()
        self.char_edit = QtWidgets.QLineEdit()
        self.char_edit.setPlaceholderText("…_retarget_fbx_skeleton.fbx")
        self.char_edit.textEdited.connect(self._char_typed)
        self.char_edit.editingFinished.connect(self._update_state)
        char_browse = QtWidgets.QToolButton(text="…")
        char_browse.setToolTip("選擇 FlowHMR 匯出的角色 FBX")
        char_browse.clicked.connect(self._browse_character)
        file_row.addWidget(self.char_edit, 1)
        file_row.addWidget(char_browse)
        form.addRow("角色 FBX", file_row)
        hint = QtWidgets.QLabel(CHARACTER_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #909090;")
        form.addRow(hint)
        self._char_auto = True
        layout.addWidget(self.char_box)

        self.overwrite_check = QtWidgets.QCheckBox("rig 已有動畫時也套用（FlowHMR 放在新的動畫層，原動畫保留在下層）")
        self.overwrite_check.setToolTip("每次套用都會建立一個新的覆蓋（override）動畫層。把它靜音或刪除，就回到原本的動畫。")
        self.overwrite_check.toggled.connect(self._update_state)
        layout.addWidget(self.overwrite_check)

        rate_form = QtWidgets.QFormLayout()
        self.rate_combo = QtWidgets.QComboBox()
        self.rate_combo.currentIndexChanged.connect(self._update_state)
        rate_form.addRow("完成後幀率", self.rate_combo)
        layout.addLayout(rate_form)

        self.notes = QtWidgets.QLabel()
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color: #e0a040;")
        layout.addWidget(self.notes)

        buttons = QtWidgets.QHBoxLayout()
        self.apply_button = QtWidgets.QPushButton("套用")
        self.apply_button.setMinimumHeight(32)
        self.apply_button.clicked.connect(self.apply)
        self.restore_button = QtWidgets.QPushButton("移除最新的 FlowHMR 動畫層")
        self.restore_button.setToolTip("刪除這個 rig 最新的 FlowHMR 動畫層，回到下層的動畫和套用前的播放範圍（幀率不會改回）。")
        self.restore_button.setMinimumHeight(32)
        self.restore_button.clicked.connect(self.restore)
        buttons.addWidget(self.apply_button, 2)
        buttons.addWidget(self.restore_button, 1)
        layout.addLayout(buttons)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(300)
        self.log_view.setFixedHeight(120)
        layout.addWidget(self.log_view)
        self._apply_mode_visibility()

    def mode(self):
        return self.mode_combo.currentData()

    def _apply_mode_visibility(self):
        mode = self.mode()
        self.adv_box.setVisible(mode not in CUSTOM_MODES)
        self.use_character.setVisible(mode in PREVIEW_MODES)
        self.refine_check.setVisible(mode == "custom_hmr")
        self.custom_box.setVisible(mode in CUSTOM_MODES)
        self.char_box.setVisible(mode == "character")
        self.apply_button.setText({"custom": "套用到自訂 rig", "custom_hmr": "用 HMR 預覽求解套用到自訂 rig",
                                   "character": "用角色 FBX 套用到 ADV rig",
                                   "raw": "用原始動作套用到 ADV rig"}.get(mode, "套用到 ADV rig"))

    def _mode_changed(self, *_):
        cmds.optionVar(sv=(OPTION_MODE, self.mode()))
        self._apply_mode_visibility()
        self._suggest_character()
        self.refresh_rigs()

    # ------------------------------------------------------------------ character FBX
    def _char_typed(self, *_):
        self._char_auto = not self.char_edit.text().strip()

    def _browse_character(self):
        start = self.char_edit.text().strip() or _option(OPTION_CHAR_DIR, str(Path.home() / "Downloads"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "選擇 FlowHMR 匯出的角色 FBX", start, "FBX (*.fbx)")
        if path:
            self.char_edit.setText(path)
            self._char_auto = False
            cmds.optionVar(sv=(OPTION_CHAR_DIR, str(Path(path).parent)))
            self._update_state()

    def _character_candidates(self, job):
        """Newest first: the job's character exports in the folder last used (Downloads by default).
        FlowHMR names them <video>_retarget_fbx_skeleton.fbx; a browser adds " (1)" to repeats."""
        stem = Path(job.get("filename") or "").stem
        folders = [Path(_option(OPTION_CHAR_DIR, "")), Path.home() / "Downloads"]
        found = []
        for folder in dict.fromkeys(f for f in folders if str(f) not in ("", ".")):
            if stem and folder.is_dir():  # no glob: video names may hold [ ] or *
                found += [p for p in folder.iterdir() if p.name.startswith(stem) and "retarget_fbx" in p.name
                          and p.suffix.lower() == ".fbx" and p.is_file()]
        return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)

    def _suggest_character(self):
        if not hasattr(self, "char_edit") or self.mode() != "character" or not self._char_auto:
            return
        job = self.selected_job()
        candidates = self._character_candidates(job) if job else []
        self.char_edit.setText(str(candidates[0]) if candidates else "")

    def character_path(self):
        text = self.char_edit.text().strip().strip('"')
        return Path(text) if text else None

    # ------------------------------------------------------------------ FlowHMR
    def _root_changed(self):
        root = self.root_edit.text().strip()
        if root and root != str(self.client.root):
            cmds.optionVar(sv=(OPTION_ROOT, root))
            self.client = client_mod.FlowHMRClient(root=root)
            self._signature = None
            self.poll(force=True)

    def _browse_root(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "選擇 FlowHMR 資料夾", self.root_edit.text())
        if folder:
            self.root_edit.setText(folder)
            self._root_changed()

    def poll(self, force=False):
        if self._polling or (not force and not self.isVisible()):
            return
        self._polling = True
        client = self.client
        if not self.connected:  # FlowHMR may have restarted on another port.
            client.port = client_mod.discover_port(client.root)

        def work():
            try:
                self.signals.jobs.emit((True, client.jobs(), client))
            except client_mod.FlowHMRError as error:
                self.signals.jobs.emit((False, str(error), client))
            except Exception as error:  # never let a worker thread raise into Maya
                self.signals.jobs.emit((False, repr(error), client))

        threading.Thread(target=work, daemon=True).start()

    def _on_jobs(self, payload):
        self._polling = False
        ok, data, client = payload
        if client is not self.client:
            return
        was = self.connected
        self.connected = ok
        if not ok:
            self.status.setText(f"<span style='color:#d05050'>●</span> 未連線 · {client.base_url}")
            self.status.setToolTip(f"{data}\n請先啟動 FlowHMR 桌面版。")
            if was:
                self.log("與 FlowHMR 的連線中斷。")
            self._set_jobs([])
            return
        self.status.setText(f"<span style='color:#40c060'>●</span> 已連線 FlowHMR {client.version()} · {client.base_url}")
        self.status.setToolTip("")
        if not was:
            self.log(f"已連線 FlowHMR（{client.base_url}）。")
        self._set_jobs(data)

    def _set_jobs(self, jobs):
        signature = tuple((j["id"], j["status"], j["progress"]) for j in jobs)
        if signature == self._signature:
            return
        self._signature = signature
        selected = self.selected_job()
        selected_id = selected["id"] if selected else None
        self._jobs = jobs
        self.jobs_list.blockSignals(True)
        self.jobs_list.clear()
        for job in jobs:
            status = STATUS.get(job["status"], job["status"])
            if job["status"] in ("running", "queued") and job["progress"] is not None:
                status += f" {job['progress']}%"
            if job["scene"]:
                status += " · 多人場景（暫不支援）"
            when = time.strftime("%m-%d %H:%M", time.localtime(job["created"])) if job["created"] else ""
            frames = f" · {job['frames']} 幀" if job["frames"] else ""
            item = QtWidgets.QListWidgetItem(f"{job['filename']}\n{status}{frames} · {when}")
            item.setData(QtCore.Qt.UserRole, job["id"])
            item.setToolTip(job["filename"])
            if not job["usable"]:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
                item.setForeground(QtGui.QBrush(QtGui.QColor(130, 130, 130)))
            if job["id"] in self._thumbs:
                item.setIcon(self._thumbs[job["id"]])
            elif job["status"] == "done" and job["id"] not in self._asked:
                self._fetch_thumbnail(job["id"])
            self.jobs_list.addItem(item)
            if job["id"] == selected_id and job["usable"]:
                item.setSelected(True)
        self.jobs_list.blockSignals(False)
        self._update_state()

    def _fetch_thumbnail(self, job_id):
        self._asked.add(job_id)
        client = self.client
        threading.Thread(target=lambda: self.signals.thumbnail.emit(job_id, client.thumbnail(job_id)),
                         daemon=True).start()

    def _on_thumbnail(self, job_id, data):
        if not data:
            return
        image = QtGui.QPixmap()
        if not image.loadFromData(QtCore.QByteArray(data)):
            return
        icon = QtGui.QIcon(image.scaled(72, 72, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation))
        self._thumbs[job_id] = icon
        for row in range(self.jobs_list.count()):
            item = self.jobs_list.item(row)
            if item.data(QtCore.Qt.UserRole) == job_id:
                item.setIcon(icon)

    def selected_job(self):
        items = self.jobs_list.selectedItems()
        if not items:
            return None
        job_id = items[0].data(QtCore.Qt.UserRole)
        return next((job for job in self._jobs if job["id"] == job_id), None)

    # ------------------------------------------------------------------ scene
    def refresh_rigs(self, *_):
        current = self.rig_combo.currentText()
        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()
        rigs = adv.find_rigs()
        self.rig_combo.addItems(rigs)
        if current in rigs:
            self.rig_combo.setCurrentText(current)
        self.rig_combo.blockSignals(False)
        self.refresh_profiles()

        unit = self.rate_combo.currentData() if self.rate_combo.count() else None
        self.rate_combo.blockSignals(True)
        self.rate_combo.clear()
        self.rate_combo.addItem(f"跟隨場景（目前 {timing_mod.scene_fps():g} fps）", None)
        for name, _fps, label in timing_mod.UNITS:
            self.rate_combo.addItem(label, name)
        self.rate_combo.setCurrentIndex(max(0, self.rate_combo.findData(unit)))
        self.rate_combo.blockSignals(False)
        self._update_state()

    def refresh_profiles(self, select=None):
        current = select or self.profile_combo.currentData() or _option(OPTION_PROFILE, "")
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for name, path, _builtin in generic.list_profiles():
            self.profile_combo.addItem(name, str(path))
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(str(current)) if current else 0))
        self.profile_combo.blockSignals(False)
        self._refresh_namespaces()

    def _profile_changed(self, *_):
        if self.profile_combo.currentData():
            cmds.optionVar(sv=(OPTION_PROFILE, self.profile_combo.currentData()))
        self._refresh_namespaces()
        self._update_state()

    def _refresh_namespaces(self):
        current = self.ns_combo.currentData()
        self.ns_combo.blockSignals(True)
        self.ns_combo.clear()
        path = self.profile_combo.currentData()
        spaces = []
        if path:
            try:
                spaces = generic.namespaces_with(generic.load_profile(path))
            except (OSError, ValueError, KeyError):
                spaces = []
        for ns in spaces:
            self.ns_combo.addItem(ns or "（無 namespace）", ns)
        index = self.ns_combo.findData(current) if current is not None else -1
        self.ns_combo.setCurrentIndex(index if index >= 0 else 0)
        self.ns_combo.blockSignals(False)

    def open_editor(self, path):
        from . import mapping_ui

        namespace = self.ns_combo.currentData() if self.ns_combo.count() else None
        mapping_ui.show_editor(path=path, namespace=namespace,
                               on_saved=lambda saved: (self.refresh_profiles(select=saved), self._update_state()))

    def _update_state(self, *_):
        notes, blocked, namespace = [], self._busy, None
        self._suggest_character()  # picks up a newer export (重新整理 / selecting the job again)
        if self.mode() in CUSTOM_MODES:
            path, namespace = self.profile_combo.currentData(), self.ns_combo.currentData()
            if not path:
                notes.append("還沒有對應設定。按「新增…」建立一個。")
                blocked = True
            elif namespace is None:
                notes.append("場景中找不到這個對應設定的 Root 控制器。請檢查 rig 是否已載入，或在對應面板確認設定。")
                blocked = True
            else:
                try:
                    info = generic.inspect(generic.load_profile(path), namespace,
                                           overwrite=self.overwrite_check.isChecked())
                    notes += info["issues"] + info["notes"]
                    blocked = blocked or bool(info["issues"])
                except (OSError, ValueError, KeyError) as error:
                    notes.append(f"對應設定讀取失敗：{error}")
                    blocked = True
        else:
            main = self.rig_combo.currentText()
            if not main:
                notes.append("場景中找不到 AdvancedSkeleton rig。其他 rig 請改用「自訂對應」。")
                blocked = True
            else:
                rig = adv.inspect_rig(main, overwrite=self.overwrite_check.isChecked())
                notes += rig["issues"] + rig["notes"]
                blocked = blocked or bool(rig["issues"])
                namespace = rig["namespace"] if cmds.objExists(main) else None
            if self.mode() == "character":
                path, job = self.character_path(), self.selected_job()
                if path is None:
                    notes.append("還沒有角色 FBX：" + ("下載資料夾裡找不到這個任務的「目前角色 FBX」。" if job else "請選任務或按「…」選檔案。"))
                    blocked = True
                elif not path.is_file() or path.suffix.lower() != ".fbx":
                    notes.append(f"找不到角色 FBX：{path}")
                    blocked = True
                elif job and Path(job.get("filename") or "").stem not in path.stem:
                    notes.append("這個角色 FBX 的檔名不是選取的任務匯出的，請確認是同一段動作。")
        undo = self._undo_target(namespace)
        if layers.detached_nodes():
            notes.append("場景中有上次套用失敗時拆下、還沒接回的動畫（FlowHMR_detached_…），按右邊的按鈕可以接回。")
        self.restore_button.setText(undo[0] if undo else "移除最新的 FlowHMR 動畫層")
        self.restore_button.setEnabled(bool(undo) and not self._busy)
        unit = self.rate_combo.currentData()
        fps = timing_mod.unit_fps(unit) if unit else timing_mod.scene_fps()
        if not timing_mod.keeps_whole_frames(fps):
            notes.append(f"{fps:g} fps 不是 30 的倍數：key 會保留秒數，但多數會落在小數幀。")
        character_mode = self.mode() == "character"
        if self.selected_job() is None and not character_mode:
            blocked = True
        self.notes.setText("\n".join(notes))
        self.notes.setVisible(bool(notes))
        # The character FBX is a local file: no FlowHMR connection needed.
        self.apply_button.setEnabled(not blocked and (self.connected or character_mode))

    # ------------------------------------------------------------------ apply
    def _confirm(self, text):
        answer = QtWidgets.QMessageBox.question(self, "FlowHMR Bridge", text,
                                                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                                QtWidgets.QMessageBox.No)
        return answer == QtWidgets.QMessageBox.Yes

    def apply(self):
        job = self.selected_job()
        if self._busy or (not job and self.mode() != "character"):
            return
        unit = self.rate_combo.currentData()
        if self.mode() == "character":
            path = self.character_path()
            if not path or not path.is_file():
                return
            target = {"mode": "character", "main": self.rig_combo.currentText(),
                      "overwrite": self.overwrite_check.isChecked(),
                      "label": Path(job["filename"]).stem if job and job.get("filename") else None}
            self._busy = True
            self._update_state()
            self._on_fbx((True, path, (job, target, unit)))
            return
        if self.mode() == "custom":
            target = {"mode": "custom", "profile": self.profile_combo.currentData(), "namespace": self.ns_combo.currentData()}
            folder = adv.staging_root() / f"{job['id']}_flowhmr_t"
            download = dict(profile="flowhmr", mesh=0, first_pose="t")
        elif self.mode() == "custom_hmr":
            target = {"mode": "custom_hmr", "profile": self.profile_combo.currentData(),
                      "namespace": self.ns_combo.currentData(), "use_character": self.use_character.isChecked(),
                      "refine": self.refine_check.isChecked()}
        elif self.mode() == "raw":
            target = {"mode": "raw", "main": self.rig_combo.currentText(), "use_character": self.use_character.isChecked()}
        else:
            target = {"mode": "adv", "main": self.rig_combo.currentText()}
            folder = adv.staging_root() / job["id"]
            download = {}
        target["overwrite"] = self.overwrite_check.isChecked()
        target["label"] = Path(job.get("filename") or job["id"]).stem
        self._busy = True
        self._update_state()
        raw = target["mode"] in PREVIEW_MODES
        source_name = "原始動作與預覽設定" if raw else "FBX"
        self.apply_button.setText(f"讀取{source_name}…")
        client = self.client
        context = (job, target, unit)
        self.log(f"讀取 {job['filename']} 的{source_name}…")

        def work():
            try:
                if raw:
                    data = client.preview_source(job["id"], use_character=target["use_character"])
                else:
                    data = client.download_fbx(job["id"], folder, **download)
                self.signals.fbx.emit((True, data, context))
            except Exception as error:
                self.signals.fbx.emit((False, str(error), context))

        threading.Thread(target=work, daemon=True).start()

    def _target_namespace(self):
        if self.mode() in CUSTOM_MODES:
            return self.ns_combo.currentData()
        main = self.rig_combo.currentText()
        return adv.inspect_rig(main, overwrite=True)["namespace"] if main and cmds.objExists(main) else None

    def _undo_target(self, namespace):
        """(button text, kind, node) for what the restore button would undo, or None."""
        detached = layers.detached_nodes()
        if detached:
            return ("接回上次拆下的動畫", "detached", detached[-1])
        if namespace is None:
            return None
        found = layers.flowhmr_layers(namespace)
        if found:
            return ("移除最新的 FlowHMR 動畫層", "layer", found[-1])
        if self.mode() == "adv":
            nodes = adv.backups(namespace)
            if nodes:
                return ("還原舊版的覆蓋備份", "backup", nodes[-1])
        return None

    def restore(self):
        if self._busy:
            return
        undo = self._undo_target(self._target_namespace())
        if not undo:
            return
        _, kind, node = undo
        if kind == "layer":
            label = layers.layer_info(node).get("label", "")
            text = (f"要刪除動畫層 {node}" + (f"（{label}）" if label else "") + " 嗎？\n\n"
                    "rig 會回到下層的動畫，播放範圍回到套用前；幀率不會改回。")
        elif kind == "detached":
            text = f"要把 {node} 記錄的動畫接回控制器嗎？"
        else:
            text = f"要刪除目前的動畫，並還原舊版備份 {node} 嗎？還原後這個備份節點會被刪除。"
        if not self._confirm(text):
            return
        try:
            if kind == "layer":
                result = layers.remove_layer(node)
                self.log(f"已刪除動畫層 {node}（{result['curves']} 條曲線）。")
            elif kind == "detached":
                self.log(f"已接回 {layers.reattach(node)} 個通道的動畫。")
            else:
                result = adv.restore_backup(node)
                self.log(f"已還原 {node}：{result['restored_curves']} 條曲線"
                         + (f"，{result['missing']} 條找不到" if result["missing"] else "") + "。")
        except Exception as error:
            self.log(f"還原失敗：{error}")
            QtWidgets.QMessageBox.warning(self, "FlowHMR Bridge", str(error))
        finally:
            self.refresh_rigs()

    def _on_fbx(self, payload):
        ok, data, (job, target, unit) = payload
        try:
            if not ok:
                raise adv.ApplyError(f"無法從 FlowHMR 取得動作：{data}")
            self.apply_button.setText("套用中…")
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            QtWidgets.QApplication.processEvents()
            try:
                if target["mode"] == "custom":
                    result = generic.apply(target["namespace"], target["profile"], data, target_unit=unit,
                                           log=self.log, overwrite=target["overwrite"], label=target["label"])
                elif target["mode"] == "custom_hmr":
                    result = custom_preview.apply(target["namespace"], target["profile"], data, target_unit=unit,
                                                  log=self.log, overwrite=target["overwrite"], label=target["label"],
                                                  refine=target["refine"])
                elif target["mode"] == "character":
                    result = character.apply(target["main"], data, target_unit=unit, log=self.log,
                                             overwrite=target["overwrite"], label=target["label"])
                elif target["mode"] == "raw":
                    result = character.apply(target["main"], preview_source=data, target_unit=unit, log=self.log,
                                             overwrite=target["overwrite"], label=target["label"])
                else:
                    result = adv.apply(target["main"], data, target_unit=unit, log=self.log,
                                       overwrite=target["overwrite"], label=target["label"])
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
            keys = result["keys"]
            first, last = keys["first"], keys["last"]
            reference = "" if target["mode"] in ("raw", "character", "custom_hmr") else f"第 {first:g} 幀是 T Pose 參考。"
            self.log(f"完成：{result['driven_controls']} 個控制器、{keys['keys']} 個 key，{result['fps']:g} fps，"
                     f"範圍 {first:g}–{last:g}（{result['seconds']:.1f} 秒）。{reference}"
                     "要撤銷請移除這次的動畫層。")
            if result.get("layer"):
                self.log(f"動作在動畫層 {result['layer']}；要回到原本的動畫，把它靜音，或按「移除最新的 FlowHMR 動畫層」。")
            if result.get("skipped_controls"):
                self.log(f"略過 {len(result['skipped_controls'])} 個被 rig 內部驅動的控制器。")
            for note in result.get("notes", []):
                self.log("注意：" + note)
        except adv.ApplyError as error:
            self.log(f"失敗：{error}")
            QtWidgets.QMessageBox.warning(self, "FlowHMR Bridge", str(error))
        except Exception:
            self.log("發生未預期的錯誤，詳見 Script Editor。")
            traceback.print_exc()
        finally:
            self._busy = False
            self._apply_mode_visibility()
            self.refresh_rigs()

    def log(self, text):
        self.log_view.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")
        print(f"// FlowHMR Bridge: {text}")

    # ------------------------------------------------------------------ lifetime
    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "rig_combo"):
            self.refresh_rigs()

    def dockCloseEventTriggered(self):
        self.timer.stop()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)


def show():
    """Open (or reopen) the panel as a floating, dockable Maya window."""
    global _panel
    control = BridgePanel.NAME + "WorkspaceControl"
    if cmds.workspaceControl(control, exists=True):
        cmds.deleteUI(control)
    if _panel is not None:
        try:
            _panel.timer.stop()
            _panel.deleteLater()
        except RuntimeError:
            pass
    _panel = BridgePanel()
    _panel.show(dockable=True, floating=True, retain=False, width=420, height=760)
    return _panel
