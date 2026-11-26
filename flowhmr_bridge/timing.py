"""Hold the scene at 30 FPS while a FlowHMR FBX is applied and baked, then leave it at a chosen rate.

FlowHMR motion is sampled at 30 FPS. Baking at 30 keeps every sample on a key.
Keys keep their time in seconds when the rate changes afterwards: multiples of
30 (30, 60, 120) keep whole frames, other rates put keys between frames.
"""
from __future__ import annotations

import maya.cmds as cmds
import maya.mel as mel
from maya.api.OpenMaya import MTime

SOURCE_UNIT = "ntsc"
RANGE_FLAGS = ("animationStartTime", "animationEndTime", "minTime", "maxTime")
PLAYBACK_FLAGS = ("playbackSpeed", "by")
UNITS = [  # (Maya unit, fps, label)
    ("film", 24.0, "24 fps (film)"), ("pal", 25.0, "25 fps (pal)"), ("ntsc", 30.0, "30 fps (ntsc)"),
    ("show", 48.0, "48 fps (show)"), ("palf", 50.0, "50 fps (palf)"), ("ntscf", 60.0, "60 fps (ntscf)"),
    ("120fps", 120.0, "120 fps"),
]


def scene_fps():
    return MTime(1.0, MTime.kSeconds).asUnits(MTime.uiUnit())


def keeps_whole_frames(fps):
    ratio = fps / 30.0
    return abs(ratio - round(ratio)) < 1e-6 and ratio >= 1


def unit_fps(unit):
    for name, fps, _ in UNITS:
        if name == unit:
            return fps
    current = cmds.currentUnit(q=True, time=True)
    try:
        cmds.currentUnit(time=unit, updateAnimation=False)
        return scene_fps()
    finally:
        cmds.currentUnit(time=current, updateAnimation=False)


def _timeline():
    fps = scene_fps()
    ranges = {flag: cmds.playbackOptions(q=True, **{flag: True}) / fps for flag in RANGE_FLAGS}
    return ranges, cmds.currentTime(q=True) / fps


def _set_unit(unit, timeline):
    if cmds.currentUnit(q=True, time=True) != unit:
        cmds.currentUnit(time=unit, updateAnimation=True)
    fps = scene_fps()
    ranges, current = timeline
    cmds.playbackOptions(**{flag: seconds * fps for flag, seconds in ranges.items()})
    cmds.currentTime(current * fps, edit=True)


class Timing:
    """prepare() -> import -> imported() -> bake -> finish(unit); abort() on failure."""

    def __init__(self):
        self.unit = cmds.currentUnit(q=True, time=True)
        self.timeline = _timeline()
        self.playback = {flag: cmds.playbackOptions(q=True, **{flag: True}) for flag in PLAYBACK_FLAGS}
        self._pushed = False
        self._changed = False

    def prepare(self, import_fbx=True):
        if import_fbx:
            if not cmds.pluginInfo("fbxmaya", q=True, loaded=True):
                cmds.loadPlugin("fbxmaya", quiet=True)
            mel.eval("FBXPushSettings;")
            self._pushed = True
            mel.eval("FBXImportResamplingRateSource -v File;")
            mel.eval("FBXImportSetMayaFrameRate -v false;")
        self._changed = True
        _set_unit(SOURCE_UNIT, self.timeline)
        cmds.playbackOptions(playbackSpeed=1.0, by=1.0)

    def imported(self):
        self._pop()
        if abs(scene_fps() - 30.0) > 1e-6:
            raise RuntimeError("The frame rate changed while the FBX was imported.")

    def finish(self, unit=None):
        """Leave the motion range in seconds at ``unit`` (default: the scene's rate before prepare)."""
        self._pop()
        if self._changed:
            _set_unit(unit or self.unit, _timeline())
            cmds.playbackOptions(**self.playback)
            self._changed = False

    def abort(self):
        self._pop()
        if self._changed:
            _set_unit(self.unit, self.timeline)
            cmds.playbackOptions(**self.playback)
            self._changed = False

    def _pop(self):
        if self._pushed:
            mel.eval("FBXPopSettings;")
            self._pushed = False
