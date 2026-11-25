"""Apply a FlowHMR ``maya_adv`` FBX to an AdvancedSkeleton rig through ADV's MoCapLibrary, then bake.

One call does connect -> bake -> euler filter -> frame-rate change. On any failure the
imported mocap skeleton, its constraints and any keys this run created are removed,
and the scene's frame rate is put back.

Tested with AdvancedSkeleton 6.755 on Maya 2025, on rigs referenced with and without a namespace.
Two workarounds depend on ADV 6.755 internals (see _panel_namespace and _connect_fingers).
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import maya.cmds as cmds
import maya.mel as mel

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


class ApplyError(RuntimeError):
    """The rig, scene or FBX cannot be applied; nothing was left behind."""


# ------------------------------------------------------------------ AdvancedSkeleton
def _adv_mel_path():
    found = mel.eval('whatIs "AdvancedSkeleton"')
    if "found in: " in found:
        return Path(found.split("found in: ", 1)[1].strip())
    candidates = sorted(Path(cmds.internalVar(userAppDir=True)).glob("AdvancedSkeleton*/AdvancedSkeleton.mel"))
    if not candidates:
        raise ApplyError("AdvancedSkeleton.mel was not found under the Maya user folder.")
    return candidates[-1]


def ensure_adv():
    """Source AdvancedSkeleton if needed; return (version, path)."""
    path = _adv_mel_path()
    if not all(mel.eval(f'exists "{proc}"') for proc in ADV_PROCS):
        mel.eval(f'source "{path.as_posix()}"')
    missing = [proc for proc in ADV_PROCS if not mel.eval(f'exists "{proc}"')]
    if missing:
        raise ApplyError(f"AdvancedSkeleton procedures are missing: {missing}")
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
            f"有 {len(result['animated'])} 個控制器已經有動畫。ADV 會在第 -1 幀依目前姿勢算偏移，"
            "並重烘全部 ControlSet。要取代原動畫，請勾選「覆蓋現有動畫」。")
    elif result["animated"]:
        _, blocked = _sources(namespace)
        if blocked:
            result["issues"].append(
                f"{len(blocked)} 個控制器的動畫在動畫層或 pairBlend 上（例如 {', '.join(blocked[:3])}），"
                "無法自動備份。請先合併動畫層，或刪掉這些控制器的動畫。")
        else:
            result["notes"].append(f"將覆蓋 {len(result['animated'])} 個控制器的動畫；原動畫會先備份在場景裡，可以還原。")
    for node in backups(namespace):
        result["notes"].append(f"場景中有覆蓋前的備份：{node}。")
    return result


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
          overwrite=False):
    """Connect, bake, euler-filter and set the frame rate. Returns a summary dict.

    ``target_unit`` is a Maya time unit; None keeps the scene's rate from before the call.
    ``overwrite`` detaches existing keys into a backup node first (see restore_backup);
    if the apply fails, the backup is put back automatically.
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
    notes = [note for note in rig["notes"] if not note.startswith(("將覆蓋", "場景中有覆蓋前的備份"))]
    if version not in TESTED_ADV:
        notes.append(f"AdvancedSkeleton {version} 未實測（已測 {', '.join(TESTED_ADV)}）。")
    fbx = _stage(fbx)
    timing = timing_mod.Timing()
    target = target_unit or timing.unit
    curves_before = set(cmds.ls(type="animCurve"))
    previous_option = cmds.optionVar(q=OPTION_VAR) if cmds.optionVar(exists=OPTION_VAR) else None
    log(f"套用 {fbx.name} → {main}")
    backup = None
    cmds.undoInfo(openChunk=True, chunkName="FlowHMR apply")
    try:
        if rig["animated"]:
            backup = backup_animation(main, namespace, label=fbx.name)
            log(f"原動畫已備份到 {backup}（{len(rig['animated'])} 個控制器）")
        timing.prepare()
        cmds.currentTime(-1, edit=True)
        if build_pose:
            cmds.select(main, replace=True)
            mel.eval('asGoToBuildPose ""')
        cmds.optionVar(sv=(OPTION_VAR, fbx.parent.as_posix()))
        mel.eval('asMoCapLibraryUI ""')
        labels = [cmds.menuItem(item, q=True, label=True) for item in cmds.optionMenu(MENU, q=True, itemListLong=True) or []]
        if fbx.name not in labels:
            raise ApplyError(f"ADV MoCapLibrary 沒有列出 {fbx.name}。")
        cmds.optionMenu(MENU, e=True, value=fbx.name)
        cmds.select(main, replace=True)
        with _panel_namespace(namespace):
            mel.eval('asMCLApply "asPanel" Custom')
        timing.imported()
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

        timing.finish(target)
        keys = key_report(namespace)
        fps = timing_mod.scene_fps()
        if keys["fractional_keys"]:
            notes.append(f"{fps:g} fps 不是 30 的倍數，{keys['fractional_keys']} 個 key 落在小數幀。")
        cmds.currentTime(0, edit=True)
        cmds.select(clear=True)
        return {"main": main, "namespace": namespace, "fbx": str(fbx), "adv_version": version, "backup": backup,
                "driven_controls": len(driven), "fingers": fingers, "source_frames": len(source_keys or []),
                "bake_range_30fps": bake_range, "euler_flips_fixed": flips, "time_unit": cmds.currentUnit(q=True, time=True),
                "fps": fps, "keys": keys, "playback": (cmds.playbackOptions(q=True, min=True),
                                                       cmds.playbackOptions(q=True, max=True)),
                "notes": notes, "seconds": round(time.time() - started, 2)}
    except Exception as error:
        removed, problems = 0, []
        steps = [_remove_mocap, lambda: _remove_new_keys(namespace, curves_before), timing.abort]
        if backup:
            steps.append(lambda: restore_backup(backup))
        for step in steps:
            try:
                value = step()
                removed = value if isinstance(value, int) else removed
            except Exception as cleanup_error:  # keep going: the frame rate must come back
                problems.append(str(cleanup_error))
        log(f"套用失敗，已清除動捕骨架與 {removed} 條新曲線{'，並還原原動畫' if backup else ''}：{error}")
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
