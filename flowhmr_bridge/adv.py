"""Apply a FlowHMR ``maya_adv`` FBX to an AdvancedSkeleton rig through ADV's MoCapLibrary, then bake.

One call does connect -> bake -> euler filter -> frame-rate change. On any failure the
imported mocap skeleton, its constraints and any keys this run created are removed,
and the scene's frame rate is put back.

Tested with AdvancedSkeleton 6.755 on Maya 2025, on rigs referenced with and without a namespace.
Two workarounds depend on ADV 6.755 internals (see _panel_namespace and _connect_fingers).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import traceback
from pathlib import Path

import maya.api.OpenMaya as om
import maya.cmds as cmds
import maya.mel as mel

from . import layers
from . import timing as timing_mod

TESTED_ADV = ("6.755",)
MOCAP_NS = "MotionCaptureLibrary"
OPTION_VAR = "asMCLMoCapCustomPathTextField"
WINDOW = "asMoCapLibraryUI"
MENU = "asMCLMoCapCustomOptionMenu"
ADV_PROCS = ("asMCLApply", "asMCLBakeBody", "asMoCapLibraryUI", "asMCLConnectBody", "asGoToBuildPose")
REQUIRED = ("Group", "ControlSet", "Main", "RootX_M", "FKIKLeg_R", "FKIKLeg_L", "IKLeg_R", "IKLeg_L",
            "PoleLeg_R", "PoleLeg_L", "FKShoulder_R", "FKShoulder_L", "FKElbow_R", "FKElbow_L",
            "FKWrist_R", "FKWrist_L")
# Controls asMCLConnectBody drives on a biped; fingers are listed separately.
BODY_DRIVEN = ("RootX_M", "FKRoot_M", "FKSpine1_M", "FKChest_M", "FKNeck_M", "FKHead_M") + tuple(
    f"{control}{side}" for side in ("_R", "_L") for control in (
        "FKHip", "FKKnee", "FKAnkle", "FKToes", "IKLeg", "PoleLeg", "IKToes",
        "FKScapula", "FKShoulder", "FKElbow", "FKWrist"))
FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")
# asMCLConnectBody ties these with -mo at frame -1, so right after it they must still sit
# exactly where the build pose put them. A bad scene state once gave FKSpine1_M a ~130 deg
# offset there, which baked a folded-over body without any error.
CONNECT_CHECK = ("RootX_M", "FKRoot_M", "FKSpine1_M", "FKChest_M", "FKNeck_M", "FKHead_M")
CONNECT_TOLERANCE = 5.0


def _world_rotations(namespace, names):
    found = {}
    for name in names:
        node = namespace + name
        if cmds.objExists(node):
            matrix = om.MMatrix(cmds.xform(node, q=True, ws=True, matrix=True))
            found[name] = om.MTransformationMatrix(matrix).rotation(asQuaternion=True)
    return found


def _connect_offsets(namespace, before):
    """Degrees each checked control moved at frame -1 between the build pose and ADV's connection."""
    previous = cmds.currentTime(q=True)
    cmds.currentTime(-1, edit=True, update=True)
    try:
        after = _world_rotations(namespace, before)
    finally:
        cmds.currentTime(previous, edit=True)
    offsets = {}
    for name, q in before.items():
        if name in after:
            d = q * after[name].inverse()
            offsets[name] = math.degrees(2 * math.acos(min(1.0, abs(d.w))))
    return offsets


class ApplyError(RuntimeError):
    """The rig, scene or FBX cannot be applied; nothing was left behind."""


# ------------------------------------------------------------------ AdvancedSkeleton
OPTION_ADV_MEL = "flowhmrBridgeAdvMel"
ADV_MEL = "AdvancedSkeleton.mel"
# ADV's installer writes shelf buttons like:  source \"D:/Tools/AdvancedSkeleton/AdvancedSkeleton.mel\";
_SHELF_SOURCE = re.compile(r'source\s+\\?"([^"\n]*?AdvancedSkeleton\.mel)\\?"', re.IGNORECASE)
_found_cache = None


def _mel_version(path):
    """(6, 755) from the "Version 6.755" header line, () when unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _ in range(10):
                match = re.search(r"Version\s+([\d.]+)", handle.readline())
                if match:
                    return tuple(int(part) for part in match[1].split(".") if part)
    except OSError:
        pass
    return ()


def _shelf_paths():
    """AdvancedSkeleton.mel paths that shelf buttons source: the shelves loaded now, and the
    saved shelf files (a shelf that has not been opened yet in this session)."""
    texts = []
    try:
        for button in cmds.lsUI(type="shelfButton") or []:
            texts.append(cmds.shelfButton(button, q=True, command=True) or "")
    except RuntimeError:  # no UI (mayapy)
        pass
    shelves = Path(cmds.internalVar(userShelfDir=True))
    for path in sorted(shelves.glob("shelf_*.mel")) if shelves.is_dir() else []:
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    found = []
    for text in texts:
        found += [match.replace("\\\\", "/") for match in _SHELF_SOURCE.findall(text)]
    return found


def adv_mel_candidates():
    """Every AdvancedSkeleton.mel this Maya points at, wherever ADV was unpacked: shelf buttons
    from ADV's installer, MAYA_SCRIPT_PATH and the Maya user folder."""
    paths = list(_shelf_paths())
    for folder in filter(None, (mel.eval('getenv "MAYA_SCRIPT_PATH"') or "").split(os.pathsep)):
        paths.append(Path(folder) / ADV_MEL)
        paths += list(Path(folder).glob(f"AdvancedSkeleton*/{ADV_MEL}")) if Path(folder).is_dir() else []
    user = Path(cmds.internalVar(userAppDir=True))
    paths += list(user.glob(f"AdvancedSkeleton*/{ADV_MEL}")) + list(user.glob(f"scripts/AdvancedSkeleton*/{ADV_MEL}"))
    unique = {}
    for path in paths:
        path = Path(path)
        if path.is_file():
            unique.setdefault(str(path.resolve()).lower(), path.resolve())
    return list(unique.values())


def set_adv_mel(path):
    """Remember the AdvancedSkeleton.mel to use (the panel's ADV… button). Returns its version."""
    path = Path(path)
    if path.name.lower() != ADV_MEL.lower() or not path.is_file():
        raise ApplyError(f"請選擇 {ADV_MEL}（AdvancedSkeleton 解壓縮後資料夾裡的主程式）。")
    global _found_cache
    cmds.optionVar(sv=(OPTION_ADV_MEL, path.as_posix()))
    _found_cache = None
    return ".".join(map(str, _mel_version(path)))


def find_adv_mel():
    """The AdvancedSkeleton.mel to source, or None:
    1. the one this Maya session already loaded (or finds on its script path);
    2. the one picked in the panel, or found on an earlier run;
    3. the newest version among adv_mel_candidates() (later file on a tie)."""
    global _found_cache
    found = mel.eval('whatIs "AdvancedSkeleton"')
    if "found in: " in found:
        path = Path(found.split("found in: ", 1)[1].strip())
        if path.is_file():
            return path
    saved = cmds.optionVar(q=OPTION_ADV_MEL) if cmds.optionVar(exists=OPTION_ADV_MEL) else ""
    if saved and Path(saved).is_file():
        return Path(saved)
    if _found_cache is None or not _found_cache.is_file():
        candidates = adv_mel_candidates()
        _found_cache = max(candidates, key=lambda p: (_mel_version(p), p.stat().st_mtime)) if candidates else None
    return _found_cache


def _adv_mel_path():
    path = find_adv_mel()
    if path is None:
        raise ApplyError("找不到 AdvancedSkeleton.mel。請先在 Maya 開一次 AdvancedSkeleton，"
                         "或在面板按「ADV…」指定 AdvancedSkeleton.mel 的位置。")
    cmds.optionVar(sv=(OPTION_ADV_MEL, path.as_posix()))  # found once, found again next session
    return path


def ensure_adv():
    """Source AdvancedSkeleton if needed; return (version, path)."""
    path = _adv_mel_path()
    if not all(mel.eval(f'exists "{proc}"') for proc in ADV_PROCS):
        mel.eval(f'source "{path.as_posix()}"')
    missing = [proc for proc in ADV_PROCS if not mel.eval(f'exists "{proc}"')]
    if missing:
        raise ApplyError(f"{path} 少了 AdvancedSkeleton 的指令（{', '.join(missing)}），可能不是完整的 AdvancedSkeleton。"
                         "請在面板按「ADV…」重新指定 AdvancedSkeleton.mel。")
    version = ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for _ in range(10):
            line = handle.readline()
            if "Version" in line:
                version = line.split("Version", 1)[1].strip()
                break
    return version, path


class _panel_namespace:
    """With uiName "" asMCLConnectBody re-reads the namespace from a selection that
    asMCLApply has already replaced. "asPanel" reads ADV's panel global instead."""

    def __init__(self, namespace):
        self.value = namespace or ":"

    def __enter__(self):
        mel.eval('global proc string flowhmrGetAsPanelNs() { global string $gAsPanelNamespace; '
                 'return $gAsPanelNamespace; }')
        mel.eval('global proc flowhmrSetAsPanelNs(string $ns) { global string $gAsPanelNamespace; '
                 '$gAsPanelNamespace = $ns; }')
        self.previous = mel.eval("flowhmrGetAsPanelNs()") or ""
        mel.eval(f'flowhmrSetAsPanelNs("{self.value}")')
        return self

    def __exit__(self, *_):
        mel.eval(f'flowhmrSetAsPanelNs("{self.previous}")')
        return False


def _connect_fingers(namespace):
    """ADV 6.755 prefixes the namespace twice on finger controls, so a namespaced rig gets
    none. Add the frame -1 orient constraints ADV makes for an un-namespaced rig."""
    if not namespace:
        return 0
    cmds.currentTime(-1, edit=True)
    made = 0
    for side, word in (("_R", "Right"), ("_L", "Left")):
        for finger in FINGERS:
            for i in (1, 2, 3):
                control, source = f"{namespace}FK{finger}Finger{i}{side}", f"{MOCAP_NS}:{word}Hand{finger}{i}"
                if not (cmds.objExists(control) and cmds.objExists(source)):
                    continue
                if any(cmds.attributeQuery("moCapLibCN", node=c, exists=True)
                       for c in cmds.listRelatives(control, type="constraint") or []):
                    continue
                skip = [axis for axis in "xyz" if not cmds.getAttr(f"{control}.r{axis}", settable=True)]
                if len(skip) == 3:
                    continue
                constraint = cmds.orientConstraint(source, control, maintainOffset=True, skip=skip or "none")[0]
                cmds.addAttr(constraint, longName="moCapLibCN", attributeType="bool", defaultValue=1, keyable=False)
                made += 1
    return made


# ------------------------------------------------------------------ rigs
def find_rigs():
    """``Main`` controls of ADV rigs in the scene (root or one namespace level)."""
    rigs = []
    for control_set in cmds.ls("ControlSet", "*:ControlSet", type="objectSet") or []:
        prefix = control_set[: -len("ControlSet")]
        if cmds.objExists(prefix + "Main") and cmds.objExists(prefix + "Group"):
            rigs.append(prefix + "Main")
    return rigs


def animated_controls(namespace):
    members = cmds.sets(namespace + "ControlSet", q=True) or []
    animated = []
    for member in members:
        for source in cmds.listConnections(member, s=True, d=False, skipConversionNodes=True) or []:
            kinds = cmds.nodeType(source, inherited=True) or []
            if (("animCurve" in kinds and cmds.nodeType(source).startswith("animCurveT"))
                    or "animBlendNodeBase" in kinds or "pairBlend" in kinds):
                animated.append(member)
                break
    return animated


def _sources(namespace):
    """Keyed channels on ControlSet members: ([(curve, plug)], [controls on layers or pairBlends])."""
    keyed, blocked = [], []
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        pairs = cmds.listConnections(member, s=True, d=False, plugs=True, connections=True,
                                     skipConversionNodes=True) or []
        for destination, source in zip(pairs[::2], pairs[1::2]):
            node = source.split(".", 1)[0]
            kinds = cmds.nodeType(node, inherited=True) or []
            if "animBlendNodeBase" in kinds or "pairBlend" in kinds:
                blocked.append(member)
            elif "animCurve" in kinds and cmds.nodeType(node).startswith("animCurveT"):
                keyed.append((node, destination))
    return keyed, sorted(set(blocked))


def inspect_rig(main, overwrite=False):
    """Problems that block applying, and caveats worth showing, for one rig."""
    result = {"main": main, "namespace": "", "issues": [], "notes": [], "animated": []}
    nodes = cmds.ls(main)
    if len(nodes) != 1:
        result["issues"].append(f"找不到唯一的 rig 節點 {main}")
        return result
    short = nodes[0].split("|")[-1]
    namespace = short.rpartition(":")[0]
    if ":" in namespace:
        result["issues"].append("ADV MoCapLibrary 只支援一層 namespace")
        return result
    namespace = f"{namespace}:" if namespace else ""
    result["namespace"] = namespace
    missing = [name for name in REQUIRED if not cmds.objExists(namespace + name)]
    if missing:
        result["issues"].append(f"不是 AdvancedSkeleton 身體 rig，缺少 {', '.join(missing[:4])}")
        return result
    result["animated"] = animated_controls(namespace)
    if result["animated"] and not overwrite:
        result["issues"].append(
            f"有 {len(result['animated'])} 個控制器已經有動畫。要疊上 FlowHMR 的動作，請勾選「rig 已有動畫時也套用」。")
    elif result["animated"]:
        result["notes"].append(f"{len(result['animated'])} 個控制器已經有動畫：FlowHMR 的動作會放在新的動畫層，原動畫保留在下層。")
    for node in backups(namespace):
        result["notes"].append(f"場景中有舊版的覆蓋備份：{node}（可用「還原」按鈕找回）。")
    return result


def controlset_plugs(namespace):
    """Every keyable channel ADV's Bake Body may key."""
    plugs = []
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        plugs += [f"{member}.{attr}" for attr in cmds.listAttr(member, keyable=True, unlocked=True) or []]
    return plugs


# ------------------------------------------------------------------ overwrite backups
BACKUP_ATTR = "flowhmrBackup"


def backups(namespace=None):
    """Backup nodes in the scene, oldest first; filtered to one rig namespace if given."""
    found = []
    for node in cmds.ls(type="network") or []:
        if not cmds.attributeQuery(BACKUP_ATTR, node=node, exists=True):
            continue
        try:
            data = json.loads(cmds.getAttr(f"{node}.{BACKUP_ATTR}") or "{}")
        except ValueError:
            continue
        if namespace is None or data.get("namespace") == namespace:
            found.append((data.get("created", 0), node))
    return [node for _, node in sorted(found)]


def _static_values(namespace, skip):
    values = {}
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        for attr in cmds.listAttr(member, keyable=True, unlocked=True) or []:
            plug = f"{member}.{attr}"
            if plug in skip or not cmds.objExists(plug) or cmds.connectionInfo(plug, isDestination=True):
                continue
            try:
                value = cmds.getAttr(plug)
            except (RuntimeError, ValueError):
                continue
            if isinstance(value, (int, float, bool)):
                values[plug] = value
    return values


def backup_animation(main, namespace, label=""):
    """Detach the rig's keys into a backup node so FlowHMR can bake over them.

    The curves stay in the scene, linked from the backup node, together with the
    rig's unkeyed channel values and the playback range. restore_backup() undoes it.
    """
    keyed, blocked = _sources(namespace)
    if blocked:
        raise ApplyError(f"{len(blocked)} 個控制器的動畫在動畫層或 pairBlend 上，無法自動備份。")
    if not keyed:
        return None
    fps = timing_mod.scene_fps()
    record = {"rig": main, "namespace": namespace, "label": label, "created": time.time(),
              "unit": cmds.currentUnit(q=True, time=True),
              "range_seconds": [cmds.playbackOptions(q=True, **{flag: True}) / fps for flag in timing_mod.RANGE_FLAGS],
              "plugs": [], "static": _static_values(namespace, {plug for _, plug in keyed})}
    node = cmds.createNode("network", name=time.strftime("FlowHMR_animBackup_%Y%m%d_%H%M%S"), skipSelect=True)
    cmds.addAttr(node, longName=BACKUP_ATTR, dataType="string")
    cmds.addAttr(node, longName="curves", attributeType="message", multi=True)
    for index, (curve, plug) in enumerate(keyed):
        source = cmds.connectionInfo(plug, sourceFromDestination=True)
        if source:
            cmds.disconnectAttr(source, plug)
        cmds.connectAttr(f"{curve}.message", f"{node}.curves[{index}]")
        record["plugs"].append(plug)
    cmds.setAttr(f"{node}.{BACKUP_ATTR}", json.dumps(record), type="string")
    return node


def restore_backup(node):
    """Remove the FlowHMR keys from the rig and put the backed-up animation back."""
    record = json.loads(cmds.getAttr(f"{node}.{BACKUP_ATTR}"))
    namespace = record["namespace"]
    keyed, blocked = _sources(namespace)
    if blocked:
        raise ApplyError("rig 上有動畫層或 pairBlend，無法自動還原。")
    current = {curve for curve, _ in keyed}
    if current:
        cmds.delete(list(current))
    restored = missing = 0
    for index, plug in enumerate(record["plugs"]):
        curve = (cmds.listConnections(f"{node}.curves[{index}]", s=True, d=False) or [None])[0]
        if curve is None or not cmds.objExists(plug):
            missing += 1
            continue
        cmds.connectAttr(f"{curve}.output", plug, force=True)
        restored += 1
    for plug, value in record["static"].items():
        if cmds.objExists(plug) and not cmds.connectionInfo(plug, isDestination=True) and cmds.getAttr(plug, settable=True):
            cmds.setAttr(plug, value)
    fps = timing_mod.scene_fps()
    cmds.playbackOptions(**{flag: seconds * fps for flag, seconds in zip(timing_mod.RANGE_FLAGS, record["range_seconds"])})
    cmds.delete(node)
    return {"restored_curves": restored, "missing": missing, "removed_flowhmr_curves": len(current)}


# ------------------------------------------------------------------ build pose
BUILD_POSE_ATTRS = ("udAttr", "udExtraAttr")
RESET_PREFIXES = ("translate", "rotate", "scale")


def build_pose_names(namespace):
    """Node names (no namespace) ADV's build pose record mentions; None when there is no record."""
    node = namespace + "buildPose"
    texts = [cmds.getAttr(f"{node}.{attr}") or "" for attr in BUILD_POSE_ATTRS if cmds.objExists(f"{node}.{attr}")]
    if not texts:
        return None
    return {name.rpartition(":")[2] for name in re.findall(r"[A-Za-z_][\w:]*", ";".join(texts))}


def reset_unrecorded(namespace):
    """Put the free translate/rotate/scale channels of ControlSet members the build pose record
    leaves out back to their defaults. asGoToBuildPose does not touch such a control (a root
    control added after the record was made, say), so once its animation is detached it keeps
    the value it last evaluated to, e.g. the first frame of an earlier FlowHMR layer. Returns the
    controls that were reset."""
    recorded = build_pose_names(namespace)
    if recorded is None:
        return []
    reset = []
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        short = member.split("|")[-1].rpartition(":")[2]
        if short in recorded:
            continue
        changed = False
        for attr in cmds.listAttr(member, keyable=True, unlocked=True) or []:
            plug = f"{member}.{attr}"
            if not attr.startswith(RESET_PREFIXES) or cmds.connectionInfo(plug, isDestination=True) \
                    or not cmds.getAttr(plug, settable=True):
                continue
            default = (cmds.attributeQuery(attr, node=member, listDefault=True) or [None])[0]
            if isinstance(default, (int, float)) and abs(cmds.getAttr(plug) - default) > 1e-9:
                cmds.setAttr(plug, default)
                changed = True
        if changed:
            reset.append(short)
    return reset


_BUILD_POSE_COMMANDS = ("setAttr", "xform")


def apply_build_pose(namespace):
    """What asGoToBuildPose does for a body ControlSet, without AdvancedSkeleton's scripts:
    run the setAttr / xform commands ADV recorded on the rig's buildPose node, with the
    namespace put in front of the node name (the second word, or the last one for xform).
    Only those two commands run. Returns the commands that failed."""
    node = namespace + "buildPose"
    text = "".join(cmds.getAttr(f"{node}.{attr}") or "" for attr in BUILD_POSE_ATTRS
                   if cmds.objExists(f"{node}.{attr}"))
    if not text:
        raise ApplyError(f"找不到 ADV 的 build pose 記錄（{node}.udAttr）。")
    failed = []
    for command in text.split(";"):
        words = command.split()
        if not words:
            continue
        if words[0] not in _BUILD_POSE_COMMANDS or len(words) < 2:
            failed.append(command.strip())
            continue
        if namespace:
            at = len(words) - 1 if words[0] == "xform" else 1
            words[at] = namespace + words[at]
        command = " ".join(words)
        # catchQuiet like ADV: a locked or driven channel is skipped without an error message
        if mel.eval('catchQuiet(`eval "%s"`)' % command.replace("\\", "\\\\").replace('"', '\\"')):
            failed.append(command)
    return failed


def go_to_build_pose(main, namespace):
    """ADV's build pose, plus the controls its record leaves out (see reset_unrecorded).
    Works without AdvancedSkeleton loaded (see apply_build_pose)."""
    failed = apply_build_pose(namespace)
    if failed:
        print("// FlowHMR Bridge: build pose commands that did not apply: " + "; ".join(failed[:10]))
    return reset_unrecorded(namespace)


# ------------------------------------------------------------------ helpers
def staging_root():
    return Path(cmds.internalVar(userAppDir=True)) / "FlowHMRBridge" / "staging"


def _stage(fbx):
    """ADV lists every file in the folder, so the FBX must be alone in it."""
    fbx = Path(fbx)
    siblings = [p for p in fbx.parent.iterdir() if p.is_file()]
    if siblings == [fbx]:
        return fbx
    folder = staging_root() / f"local_{fbx.stem}"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    return Path(shutil.copy2(fbx, folder / fbx.name))


def _mocap_constraints():
    return [c for c in cmds.ls(type="constraint") if cmds.attributeQuery("moCapLibCN", node=c, exists=True)]


def _driven(constraints):
    driven = set()
    for constraint in constraints:
        driven.update(cmds.listConnections(constraint + ".constraintParentInverseMatrix", s=True, d=False) or [])
    return driven


def rotate_curves(namespace):
    curves = set()
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        curves.update(cmds.listConnections(member, s=True, d=False, type="animCurveTA") or [])
    return sorted(curves)


def count_flips(curves, limit=180.0):
    flips = 0
    for curve in curves:
        values = cmds.keyframe(curve, q=True, valueChange=True) or []
        if any(abs(b - a) > limit for a, b in zip(values, values[1:])):
            flips += 1
    return flips


def key_report(namespace):
    curves = set()
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        for curve in cmds.listConnections(member, s=True, d=False, type="animCurve") or []:
            if cmds.nodeType(curve).startswith("animCurveT"):
                curves.add(curve)
    times = []
    for curve in curves:
        times.extend(cmds.keyframe(curve, q=True, timeChange=True) or [])
    return {"curves": len(curves), "keys": len(times),
            "fractional_keys": sum(1 for t in times if abs(t - round(t)) > 1e-4),
            "first": min(times) if times else None, "last": max(times) if times else None}


def _remove_mocap():
    for constraint in _mocap_constraints():
        if cmds.objExists(constraint):
            cmds.delete(constraint)
    if cmds.objExists("MotionCaptureLibraryScale"):
        cmds.delete("MotionCaptureLibraryScale")
    if cmds.namespace(exists=MOCAP_NS):
        cmds.namespace(removeNamespace=MOCAP_NS, deleteNamespaceContent=True)


def _remove_new_keys(namespace, curves_before):
    members = set(cmds.ls(cmds.sets(namespace + "ControlSet", q=True) or [], long=False))
    ours = []
    for curve in set(cmds.ls(type="animCurve")) - curves_before:
        targets = set(cmds.listConnections(curve, s=False, d=True, skipConversionNodes=True) or [])
        for blend in [t for t in targets if cmds.nodeType(t) == "pairBlend"]:
            targets.update(cmds.listConnections(blend, s=False, d=True) or [])
        if targets & members:
            ours.append(curve)
    if ours:
        cmds.delete(ours)
    return len(ours)


def _restore_adv_ui(previous):
    if previous is None:
        if cmds.optionVar(exists=OPTION_VAR):
            cmds.optionVar(remove=OPTION_VAR)
    else:
        cmds.optionVar(sv=(OPTION_VAR, previous))
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)


# ------------------------------------------------------------------ apply
def apply(main, fbx, target_unit=None, build_pose=True, log=print, on_connected=None, on_baked=None,
          overwrite=False, label=None):
    """Connect, bake, euler-filter, move the keys onto a new override animation layer and set
    the frame rate. Returns a summary dict.

    ``target_unit`` is a Maya time unit; None keeps the scene's rate from before the call.
    ``overwrite`` allows a rig that already has animation: it is detached for the bake and put
    back under the FlowHMR layer (see layers.Channels); a failed apply puts it back as well.
    ``on_connected(ns)`` / ``on_baked(ns)`` run at 30 FPS before and after the bake (for tests).
    """
    started = time.time()
    if cmds.upAxis(q=True, axis=True) != "y":
        raise ApplyError("ADV MoCapLibrary 需要 Y 軸朝上的場景。")
    rig = inspect_rig(main, overwrite=overwrite)
    if rig["issues"]:
        raise ApplyError(" ".join(rig["issues"]))
    namespace = rig["namespace"]
    version, _ = ensure_adv()
    notes = [note for note in rig["notes"] if "已經有動畫" not in note and "舊版的覆蓋備份" not in note]
    if version not in TESTED_ADV:
        notes.append(f"AdvancedSkeleton {version} 未實測（已測 {', '.join(TESTED_ADV)}）。")
    fbx = _stage(fbx)
    timing = timing_mod.Timing()
    target = target_unit or timing.unit
    curves_before = set(cmds.ls(type="animCurve"))
    previous_option = cmds.optionVar(q=OPTION_VAR) if cmds.optionVar(exists=OPTION_VAR) else None
    log(f"套用 {fbx.name} → {main}")
    label = label or fbx.stem.replace("_maya_adv", "")
    recorded = controlset_plugs(namespace)
    prior, prior_animated = layers.underneath(namespace, recorded, layers.sample_times())
    channels = layers.Channels(recorded + list(prior), label=label)
    previous_range = layers.range_seconds()
    layer = None
    if channels.animated:
        log(f"{len(channels.animated)} 個控制器已有動畫：烘焙時先拆下，烘焙後放回動畫層下方。")
    cmds.undoInfo(openChunk=True, chunkName="FlowHMR apply")
    try:
        channels.free()
        timing.prepare()
        cmds.currentTime(-1, edit=True)
        if build_pose:
            reset = go_to_build_pose(main, namespace)
            if reset:
                log(f"ADV 的 build pose 沒有記錄 {len(reset)} 個控制器，已一併歸零：{', '.join(reset[:6])}")
        layers.put(prior)  # what earlier FlowHMR layers changed outside the ControlSet (layers.underneath)
        for line in layers.describe_underneath(prior, prior_animated):
            log(line)
        cmds.optionVar(sv=(OPTION_VAR, fbx.parent.as_posix()))
        mel.eval('asMoCapLibraryUI ""')
        labels = [cmds.menuItem(item, q=True, label=True) for item in cmds.optionMenu(MENU, q=True, itemListLong=True) or []]
        if fbx.name not in labels:
            raise ApplyError(f"ADV MoCapLibrary 沒有列出 {fbx.name}。")
        cmds.optionMenu(MENU, e=True, value=fbx.name)
        cmds.select(main, replace=True)
        build_pose_rotations = _world_rotations(namespace, CONNECT_CHECK)  # at frame -1, after the build pose
        with _panel_namespace(namespace):
            mel.eval('asMCLApply "asPanel" Custom')
        timing.imported()
        offsets = _connect_offsets(namespace, build_pose_rotations)
        wrong = sorted(((angle, name) for name, angle in offsets.items() if angle > CONNECT_TOLERANCE), reverse=True)
        if wrong:
            raise ApplyError(
                f"ADV 接上後，第 -1 幀的身體控制器偏離 build pose（{'、'.join(f'{n} {a:.0f}°' for a, n in wrong[:3])}），"
                "烘焙會讓身體扭曲，已停止。場景可能殘留之前失敗的狀態：請存檔、重開 Maya 後再套用。")
        fingers = _connect_fingers(namespace)
        driven = _driven(_mocap_constraints())
        expected = [namespace + name for name in BODY_DRIVEN if cmds.objExists(namespace + name)]
        missing = [name for name in expected if name not in driven]
        if missing:
            raise ApplyError(f"ADV 沒有接上 {len(missing)} 個身體控制器（例如 {', '.join(missing[:4])}）。")
        hips = cmds.ls(f"{MOCAP_NS}:Hips")
        source_keys = cmds.keyframe(hips[0] + ".rotateX", q=True, timeChange=True) if hips else []
        log(f"已接上 {len(driven)} 個控制器（手指 {fingers}），來源 {len(source_keys or [])} 幀")
        if on_connected:
            on_connected(namespace)

        bake_range = (cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True))
        bake_started = time.time()
        cmds.select(main, replace=True)
        cmds.refresh(suspend=True)
        try:
            with _panel_namespace(namespace):
                mel.eval('asMCLBakeBody "asPanel"')
        finally:
            cmds.refresh(suspend=False)
        curves = rotate_curves(namespace)
        flips = count_flips(curves)
        if curves:
            cmds.filterCurve(curves, filter="euler")
        if _mocap_constraints() or cmds.namespace(exists=MOCAP_NS) or cmds.objExists("MotionCaptureLibraryScale"):
            raise ApplyError("ADV 烘焙後沒有清乾淨動捕骨架或約束。")
        log(f"烘焙完成（{time.time() - bake_started:.1f} 秒），修正 {flips} 條 euler 翻轉曲線")
        if on_baked:
            on_baked(namespace)
        baked = []
        for plug in controlset_plugs(namespace):
            source = cmds.connectionInfo(plug, sourceFromDestination=True)
            node = source.split(".", 1)[0] if source else None
            if node and node not in curves_before and cmds.nodeType(node).startswith("animCurveT"):
                baked.append(plug)
        info = {"namespace": namespace, "mode": "adv", "rig": main, "label": label, "created": time.time(),
                "range_seconds": previous_range}
        layer, layer_curves, unheld = layers.to_layer(channels, baked, {}, layers.layer_name(namespace, label), info,
                                                      bake_range[0])
        log(f"已放進動畫層 {layer}（{len(layer_curves)} 條曲線）。把這個動畫層靜音或刪除，就回到原本的動畫。")
        if unheld:
            notes.append(f"{len(unheld)} 個屬性無法放進動畫層，已直接設成套用時的值：{', '.join(p.split(':')[-1] for p in unheld[:6])}")

        timing.finish(target)
        keys = layers.key_report(layer_curves)
        fps = timing_mod.scene_fps()
        if keys["fractional_keys"]:
            notes.append(f"{fps:g} fps 不是 30 的倍數，{keys['fractional_keys']} 個 key 落在小數幀。")
        cmds.currentTime(0, edit=True)
        cmds.select(clear=True)
        return {"main": main, "namespace": namespace, "fbx": str(fbx), "adv_version": version, "layer": layer,
                "driven_controls": len(driven), "fingers": fingers, "source_frames": len(source_keys or []),
                "bake_range_30fps": bake_range, "euler_flips_fixed": flips, "time_unit": cmds.currentUnit(q=True, time=True),
                "fps": fps, "keys": keys, "playback": (cmds.playbackOptions(q=True, min=True),
                                                       cmds.playbackOptions(q=True, max=True)),
                "notes": notes, "seconds": round(time.time() - started, 2)}
    except Exception as error:
        if not isinstance(error, ApplyError):
            traceback.print_exc()  # the Script Editor keeps the full cause; the panel only shows the message
        removed, problems = 0, []
        steps = [_remove_mocap,
                 lambda: layers.remove_layer(layer, restore_range=False) if layer and cmds.objExists(layer) else None,
                 lambda: _remove_new_keys(namespace, curves_before), channels.restore, timing.abort]
        for step in steps:
            try:
                value = step()
                removed = value if isinstance(value, int) else removed
            except Exception as cleanup_error:  # keep going: the frame rate must come back
                problems.append(str(cleanup_error))
        log(f"套用失敗，已清除動捕骨架與 {removed} 條新曲線{'，並接回原動畫' if channels.sources else ''}：{error}")
        if problems:
            log("清理時有問題，請檢查場景：" + "；".join(problems))
        if isinstance(error, ApplyError):
            raise
        raise ApplyError(str(error)) from error
    finally:
        _restore_adv_ui(previous_option)
        cmds.undoInfo(closeChunk=True)
        # The FBX reference/import inside asMCLApply is not undoable and flushes the queue
        # before it, so Ctrl+Z would only revert the later steps: it brings the deleted mocap
        # skeleton back and leaves the scene at 30 FPS. Flush so no half undo is possible.
        cmds.flushUndo()
