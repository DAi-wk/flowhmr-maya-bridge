"""Drive an AdvancedSkeleton rig from the character FBX FlowHMR exports for that same rig.

FlowHMR's preview retargets the detected motion onto an FBX character (its own solver: collar and
limb alignment, arm reach, foot planting, spine spread) and can export the result on the
character's own skeleton. When that character is the rig's skin skeleton, the export already holds
the final pose of every deformation joint, so nothing is solved here: each FK control follows its
joint in the imported export with the offset the two have in the rig's build pose, and the result
is baked onto a new override animation layer.

Arms, legs and spine are left in FK (FKIKBlend 0). Only FK reproduces the export exactly: an IK
elbow or knee bends around one axis, while the exported joints may also twist.

    from flowhmr_bridge import character
    character.apply("Main", "path/to/job_retarget_fbx_skeleton.fbx")
"""
from __future__ import annotations

import math
import re
import time
import traceback
from pathlib import Path

import maya.api.OpenMaya as om
import maya.cmds as cmds
import maya.mel as mel

from . import adv
from . import generic
from . import hinge
from . import layers
from . import timing as timing_mod
from .adv import ApplyError, count_flips

CHAR_NS = "FlowHMRChar"
TAG = "flowhmrCharacterCN"
ROOT_CONTROL = "RootX_M"
PELVIS = "Root_M"
MOVE_EPSILON = 1e-4          # a channel whose keys stay within this range does not move
STRUCTURE_DEG = 0.01         # jointOrient / rotateAxis
STRUCTURE_CM = 0.01          # bone offsets
CHECK_DEG = 0.05             # rig joint vs exported joint while connected (joints without Part joints)
IK_LIMBS = (("Leg", "Hip", "Knee", "Ankle", "Toes"), ("Arm", "Shoulder", "Elbow", "Wrist", None))


# ------------------------------------------------------------------ import
def char(name):
    return f"{CHAR_NS}:{name}"


def remove_character():
    if cmds.namespace(exists=CHAR_NS):
        cmds.namespace(removeNamespace=CHAR_NS, deleteNamespaceContent=True)


def import_character(fbx):
    """Import the export into CHAR_NS (see generic.import_source for why the namespace is set
    instead of passed). Returns {joint short name: node}."""
    remove_character()
    cmds.namespace(add=CHAR_NS)
    mel.eval("FBXImportMode -v add;")
    cmds.namespace(set=":" + CHAR_NS)
    try:
        cmds.file(str(fbx), i=True, type="FBX", ignoreVersion=True)
    finally:
        cmds.namespace(set=":")
    joints = {node.split(":")[-1]: node for node in cmds.ls(CHAR_NS + ":*", type="joint") or []}
    if not joints:
        raise ApplyError("角色 FBX 匯入後找不到任何骨架。")
    return joints


def _moves(node, attrs):
    for attr in attrs:
        values = cmds.keyframe(f"{node}.{attr}", q=True, valueChange=True) or []
        if values and max(values) - min(values) > MOVE_EPSILON:
            return True
    return False


def moving_joints(joints):
    """{short name: (rotates, translates)} for the exported joints whose keys change."""
    found = {}
    for name, node in joints.items():
        rotates, translates = _moves(node, ("rx", "ry", "rz")), _moves(node, ("tx", "ty", "tz"))
        if rotates or translates:
            found[name] = (rotates, translates)
    return found


def key_range(joints):
    times = []
    for node in joints.values():
        times += cmds.keyframe(node, q=True, timeChange=True) or []
    if not times:
        raise ApplyError("角色 FBX 沒有動作 key。")
    return min(times), max(times)


# ------------------------------------------------------------------ checks
def _short(node):
    return node.split("|")[-1].split(":")[-1] if node else None


def _max_diff(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def check_structure(namespace, joints, moving):
    """Same skeleton? Compares, for every moving exported joint, the rig joint's parent name,
    rotate order, jointOrient, rotateAxis and (unless the export moves it) bone offset, and that
    the export's own groups above the skeleton do not move it. Raises on a mismatch.

    The FBX importer does not bring a bind pose for a skeleton-only export, so this checks the
    skeleton's structure, not its bind pose."""
    problems, missing = [], []
    for name, (_, translates) in sorted(moving.items()):
        rig, node = namespace + name, joints[name]
        if not cmds.objExists(rig) or cmds.nodeType(rig) != "joint":
            missing.append(name)
            continue
        parent = _short((cmds.listRelatives(node, parent=True) or [None])[0])
        if parent in joints and parent != _short((cmds.listRelatives(rig, parent=True) or [None])[0]):
            problems.append(f"{name} 的父骨不同")
        if cmds.getAttr(node + ".rotateOrder") != cmds.getAttr(rig + ".rotateOrder"):
            problems.append(f"{name} 的旋轉順序不同")
        for attr, limit in (("jointOrient", STRUCTURE_DEG), ("rotateAxis", STRUCTURE_DEG)):
            if _max_diff(cmds.getAttr(f"{node}.{attr}")[0], cmds.getAttr(f"{rig}.{attr}")[0]) > limit:
                problems.append(f"{name} 的 {attr} 不同")
        if not translates and _max_diff(cmds.getAttr(node + ".translate")[0], cmds.getAttr(rig + ".translate")[0]) > STRUCTURE_CM:
            problems.append(f"{name} 的骨長不同")
    groups = set()
    for node in joints.values():
        parts = (cmds.ls(node, long=True) or [node])[0].split("|")
        for depth in range(2, len(parts)):
            ancestor = "|".join(parts[:depth])
            if cmds.nodeType(ancestor) != "joint":
                groups.add(ancestor)
    for group in sorted(groups):
        matrix = cmds.xform(group, q=True, ws=True, matrix=True)
        if _max_diff(matrix, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]) > 1e-6:
            problems.append(f"FBX 裡的群組 {_short(group)} 有位移或旋轉")
    if missing:
        problems.insert(0, f"rig 沒有 {len(missing)} 個會動的關節：{', '.join(missing[:6])}")
    if problems:
        raise ApplyError("角色 FBX 的骨架和這個 rig 不同，不能直接套用（請匯出 rig 自己的蒙皮骨架當 FlowHMR 角色）："
                         + "；".join(problems[:6]))
    return {"joints": len(moving), "groups": len(groups)}


def _rotation(matrix):
    return om.MTransformationMatrix(om.MMatrix(matrix)).rotation(asQuaternion=True)


def _angle(a, b):
    d = a * b.inverse()
    return math.degrees(2 * math.acos(min(1.0, abs(d.w))))


def split_joints(namespace, names):
    """Joints the rig splits into Part joints (Shoulder_L -> ShoulderPart1_L...). ADV spreads
    such a joint's twist (limbs) or bend (spine, neck) over its Part joints, while the export
    keeps the Part joints rigid, so these joints cannot match the export's rotation exactly."""
    found = set()
    for name in names:
        base, sep, side = name.rpartition("_")
        if sep and cmds.objExists(f"{namespace}{base}Part1_{side}"):
            found.add(name)
    return found


def compare(namespace, joints, names, frames):
    """Worst world difference between the rig's joints and the exported joints over ``frames``
    (30 FPS frame numbers): position (cm) over all joints, rotation (deg) separately for the
    joints the rig splits into Part joints and for the rest."""
    deg, cm = {name: 0.0 for name in names}, {name: 0.0 for name in names}
    previous = cmds.currentTime(q=True)
    try:
        for frame in frames:
            # ADV secondary systems can cache stale values in an off-time DG context.
            cmds.currentTime(frame, update=True)
            for name in names:
                a = cmds.getAttr(namespace + name + ".worldMatrix[0]")
                b = cmds.getAttr(joints[name] + ".worldMatrix[0]")
                deg[name] = max(deg[name], _angle(_rotation(a), _rotation(b)))
                cm[name] = max(cm[name], math.sqrt(sum((x - y) ** 2 for x, y in zip(a[12:15], b[12:15]))))
    finally:
        cmds.currentTime(previous, update=True)
    # A joint with Part joints that still matches (the pelvis, say) counts with the exact ones.
    split = sorted(n for n in split_joints(namespace, names) if deg[n] > CHECK_DEG)
    rest = [n for n in names if n not in split]

    def worst(values, group):
        name = max(group, key=values.get) if group else None
        return (round(values[name], 3) if name else 0.0), name

    result = {"frames": len(frames), "split_joints": split}
    result["cm"], result["cm_joint"] = worst(cm, names)
    result["deg"], result["deg_joint"] = worst(deg, rest)
    result["split_deg"], result["split_deg_joint"] = worst(deg, split)
    return result


def describe(check):
    text = (f"關節位置最大差 {check['cm']} cm（{check['cm_joint']}），"
            f"旋轉最大差 {check['deg']}°（{check['deg_joint']}）")
    if check["split_joints"]:
        text += (f"；{len(check['split_joints'])} 個有分段關節的骨頭（{', '.join(check['split_joints'][:4])}…）"
                 f"由 rig 自己把扭轉／彎曲分散到 Part 關節，最大差 {check['split_deg']}°")
    return text


# ------------------------------------------------------------------ controls
def control_for(namespace, joint):
    """ADV names an FK control after its joint: FKShoulder_L, FKRoot_M; FKroot_M for a joint
    without a side suffix."""
    for name in (f"FK{joint}", f"FK{joint}_M"):
        if cmds.objExists(namespace + name):
            return namespace + name
    return None


def pairs(namespace, moving):
    """[(joint, control, kind)] and the moving joints no control could be found for.
    RootX_M carries the pelvis (rotation and travel, as ADV's own mocap import does); every
    other moving joint drives its FK control, by rotation, or rotation and translation when the
    export also moves the joint (a root joint above the pelvis)."""
    found, unmapped = [], []
    root = namespace + ROOT_CONTROL
    if PELVIS in moving:
        found.append((PELVIS, root, "parent"))
    for joint, (_, translates) in sorted(moving.items()):
        control = control_for(namespace, joint)
        if control and control != root:
            found.append((joint, control, "parent" if translates else "orient"))
        elif not control:
            unmapped.append(joint)
    return found, unmapped


def ik_pairs(namespace, moving):
    """IK controls that follow the FK result, so switching a limb to IK starts from the same pose:
    [(joint, control, kind)] for the IK hand/foot and toe, and [(pole, start, mid, end)] joints.
    The switches stay at FK; an IK limb bends around one axis, so it keeps the position but not
    every twist of the export."""
    ends, poles = [], []
    for limb, start, mid, end, toe in IK_LIMBS:
        for side in ("L", "R"):
            chain = [f"{joint}_{side}" for joint in (start, mid, end)]
            if not all(joint in moving for joint in chain):
                continue
            control, pole = f"{namespace}IK{limb}_{side}", f"{namespace}Pole{limb}_{side}"
            if cmds.objExists(control):
                ends.append((chain[2], control, "parent"))
            if toe and f"{toe}_{side}" in moving and cmds.objExists(f"{namespace}IK{toe}_{side}"):
                ends.append((f"{toe}_{side}", f"{namespace}IK{toe}_{side}", "orient"))
            if cmds.objExists(pole):
                poles.append((pole, *chain))
    return ends, poles


def pole_points(namespace, joints, pole, start, mid, end, frames):
    """Pole positions per frame from the exported limb, as generic.pole_locator does for the
    source skeleton: on the side the elbow/knee bends towards, and the rig's own rest direction
    carried by the upper bone while the limb is nearly straight. The rest data comes from the rig
    at the build pose (same skeleton as the export)."""
    rest_offset = generic._pivot(pole) - generic._pos(namespace + mid)
    distance = max(rest_offset.length(), 1e-3)
    rest_start = om.MMatrix(cmds.getAttr(namespace + start + ".worldMatrix[0]"))
    local = rest_offset.normal() * generic._rotation(rest_start).inverse()
    points = []
    for frame in frames:
        m_start = generic._world(joints[start], frame)
        ps = generic._translation(m_start)
        pm = generic._translation(generic._world(joints[mid], frame))
        pe = generic._translation(generic._world(joints[end], frame))
        length = (pm - ps).length() + (pe - pm).length()
        line = pe - ps
        along = ((pm - ps) * line) / (line * line) if line.length() > 1e-6 else 0.0
        bend = pm - (ps + line * along)
        carried = (local * generic._rotation(m_start)).normal()
        weight = min(1.0, max(0.0, (bend.length() / length - 0.03) / 0.07)) if length > 1e-6 else 0.0
        direction = carried * (1.0 - weight) + (bend.normal() * weight if bend.length() > 1e-6 else carried * weight)
        if direction.length() < 1e-6:
            direction = carried
        point = pm + direction.normal() * distance
        points.append((point.x, point.y, point.z))
    return points


def connect_poles(pole_data, frames, kinds):
    """Keyed locators for the pole controls (point constraints, the pole's pivot on the point).
    ``pole_data`` is [(pole, points)], the points computed while the rig was at the build pose."""
    made, plugs = [], []
    for pole, points in pole_data:
        if pole in kinds:
            continue
        skip = generic._skip(pole, "translate")
        if skip != "none" and len(skip) == 3:
            continue
        locator = cmds.spaceLocator(name=char(f"pole_{_short(pole)}"))[0]
        generic._key_locator(locator, frames, points)
        plugs += generic._channels(pole, "point")
        constraint = cmds.pointConstraint(locator, pole, maintainOffset=False, skip=skip)[0]
        cmds.addAttr(constraint, longName=TAG, attributeType="bool", defaultValue=1, keyable=False)
        made.append(constraint)
        kinds[pole] = "point"
    return made, plugs


def fk_holds(namespace):
    """Every FK/IK switch in the rig set to FK."""
    holds = {}
    for member in cmds.sets(namespace + "ControlSet", q=True) or []:
        if _short(member).startswith("FKIK") and cmds.attributeQuery("FKIKBlend", node=member, exists=True):
            holds[f"{member}.FKIKBlend"] = 0.0
    return holds


def connect(namespace, joints, found):
    """Constrain each control to a transform that sits on the rig joint at the build pose, then
    hand that transform to the exported joint. The constraints keep the build-pose offset between
    control and joint, whatever pivots and rotate axes the control has. Returns (constraints,
    {control: kind}, skipped controls, plugs to bake)."""
    made, kinds, skipped, plugs, followers = [], {}, [], [], []
    for joint, control, kind in found:
        if control in kinds:
            continue
        rig_joint = namespace + joint
        skip_t = generic._skip(control, "translate")
        skip_r = generic._skip(control, "rotate")
        locked_r = skip_r != "none" and len(skip_r) == 3
        locked_t = skip_t != "none" and len(skip_t) == 3
        if locked_r and (kind == "orient" or locked_t):
            skipped.append(control)
            continue
        follower = cmds.createNode("transform", name=char(f"follow_{_short(control)}"), skipSelect=True)
        cmds.xform(follower, ws=True, matrix=cmds.getAttr(rig_joint + ".worldMatrix[0]"))
        if kind == "parent":
            plugs += generic._channels(control, "parent")
            constraint = cmds.parentConstraint(follower, control, maintainOffset=True, skipTranslate=skip_t,
                                               skipRotate=skip_r)[0]
        else:
            plugs += generic._channels(control, "orient")
            constraint = cmds.orientConstraint(follower, control, maintainOffset=True, skip=skip_r)[0]
        cmds.addAttr(constraint, longName=TAG, attributeType="bool", defaultValue=1, keyable=False)
        made.append(constraint)
        kinds[control] = kind
        followers.append((follower, joints[joint]))
    for follower, joint in followers:
        cmds.parent(follower, joint)
        for attr, value in (("translate", (0, 0, 0)), ("rotate", (0, 0, 0)), ("scale", (1, 1, 1)), ("shear", (0, 0, 0))):
            cmds.setAttr(f"{follower}.{attr}", *value)
    return made, kinds, skipped, plugs


def _constraints():
    return [c for c in cmds.ls(type="constraint") if cmds.attributeQuery(TAG, node=c, exists=True)]


ARM_TWIST = re.compile(r"(Shoulder|Elbow)(Part\d+)?_[LR]")


def clean_elbows(namespace, frames, log):
    """Z-only FK elbows on both arms after the bake (hinge.clean_baked). The FKX joints are the
    positions kept: with the arm in FK they carry the skin joints."""
    chains = []
    for side, label in (("L", "左臂"), ("R", "右臂")):
        controls = tuple(f"{namespace}FK{j}_{side}" for j in ("Shoulder", "Elbow", "Wrist"))
        joints = tuple(f"{namespace}FKX{j}_{side}" for j in ("Shoulder", "Elbow", "Wrist"))
        if not all(cmds.objExists(n) for n in joints):
            joints = tuple(f"{namespace}{j}_{side}" for j in ("Shoulder", "Elbow", "Wrist"))
        problem = hinge.usable(controls) or next((f"找不到 {j}" for j in joints if not cmds.objExists(j)), None)
        if problem:
            log(f"{label}不能改成手肘只轉單軸：{problem}")
            continue
        chains.append((label, controls, joints))
    report = hinge.clean_baked(chains, frames)
    if report:
        log(hinge.describe(report) + ("。上臂、前臂的扭轉改由 rig 分配，和預覽不同；手臂位置和手掌方向不變。"
                                               if hinge.cleaned(report) else "。"))
    return report


def _check_frames(start, end, count=6):
    if end <= start:
        return [start]
    return sorted({int(round(start + (end - start) * i / (count - 1))) for i in range(count)})


# ------------------------------------------------------------------ apply
def apply(main, fbx=None, target_unit=None, log=print, on_connected=None, on_baked=None, overwrite=False,
          label=None, preview_source=None, z_only=False):
    """Bake the exported character motion ``fbx`` onto the ADV rig whose Main control is ``main``.
    The result goes on a new override animation layer. Returns a summary dict; the connection
    check (rig joints vs exported joints with the constraints live, before the bake) is in
    ``check``. ``z_only``: the FK elbows keep only their Z bend (hinge.clean_baked); arm
    positions and hands stay, the arm Micro controls are left to the rig's own twist."""
    started = time.time()
    rig = adv.inspect_rig(main, overwrite=overwrite)
    if rig["issues"]:
        raise ApplyError(" ".join(rig["issues"]))
    namespace = rig["namespace"]
    raw = preview_source is not None
    if raw and cmds.upAxis(q=True, axis=True) != "y":
        raise ApplyError("原始動作模式需要 Y 軸朝上的場景。")
    if raw and (cmds.namespace(exists=CHAR_NS) or _constraints()):
        raise ApplyError(f"場景已有 {CHAR_NS} 節點或舊的角色約束，無法建立這次的暫存目標。")
    if not label:
        label = "SMPL-H" if raw else Path(fbx).stem.replace("_retarget_fbx_skeleton", "").replace("_retarget_fbx_skin", "")
    extra_weights, biases = {}, {}
    switches = fk_holds(namespace)
    if raw:
        from .raw import neck_twist_weights, spine_holds, targets
        extra_weights = neck_twist_weights(namespace)
        biases = spine_holds(namespace)
    # Some ADV versions expose bias in the Channel Box but mark it non-keyable.
    recorded = adv.controlset_plugs(namespace) + list(extra_weights.values()) + list(biases) + list(switches)
    prior, prior_animated = layers.underneath(namespace, recorded, layers.sample_times())
    channels = layers.Channels(recorded + list(prior), label=label)
    timing = timing_mod.Timing()
    target = target_unit or timing.unit
    previous_range = layers.range_seconds()
    curves_before = set(cmds.ls(type="animCurve"))
    layer = None
    source_name = "原始動作預覽" if raw else "角色 FBX"
    log(f"{source_name}：{label} → {main}")
    if channels.animated:
        log(f"{len(channels.animated)} 個控制器已有動畫：烘焙時先拆下，烘焙後放回動畫層下方。")
    try:
        channels.free()
        timing.prepare(import_fbx=not raw)
        if raw:
            start, end = 0, preview_source["meta"]["frames"] - 1
            cmds.namespace(add=CHAR_NS)
        else:
            joints = import_character(fbx)
            timing.imported()
            moving = moving_joints(joints)
            start, end = key_range({name: joints[name] for name in moving} or joints)
        cmds.playbackOptions(minTime=start, maxTime=end, animationStartTime=start, animationEndTime=end)
        cmds.currentTime(start, edit=True)
        reset = adv.go_to_build_pose(main, namespace)
        if reset:
            log(f"ADV 的 build pose 沒有記錄 {len(reset)} 個控制器，已一併歸零：{', '.join(reset[:6])}")
        # Earlier FlowHMR layers' settings first; this apply's own holds below override them.
        layers.put(prior)
        for line in layers.describe_underneath(prior, prior_animated):
            log(line)
        holds = switches.copy()
        correctives = []
        holds.update(biases)
        for plug, value in holds.items():
            if generic._settable(plug):
                cmds.setAttr(plug, value)
        # Queries at the current frame can return the value cached before the build pose.
        cmds.currentTime(start, edit=True, update=True)
        if raw:
            joints, moving, correctives = targets(namespace, preview_source, log)
            for name, plug in extra_weights.items():
                if name in joints:
                    cmds.setAttr(plug, 0)
                    holds[plug] = 0
            structure = {"joints": len(joints)}
        else:
            structure = check_structure(namespace, joints, moving)
        found, unmapped = pairs(namespace, moving)
        if z_only:
            # Micro controls on the upper arm / forearm would put the preview's twist back.
            correctives = [c for c in correctives if not ARM_TWIST.fullmatch(c[0])]
        found.extend(correctives)
        if not any(control == namespace + ROOT_CONTROL for _, control, _ in found):
            raise ApplyError(f"找不到 {ROOT_CONTROL}，或角色 FBX 的 {PELVIS} 沒有動作。")
        ik_ends, poles = ik_pairs(namespace, moving)
        frames = list(range(int(round(start)), int(round(end)) + 1))
        # The pole rest data is read from the rig, so before anything is constrained.
        pole_data = [(pole, pole_points(namespace, joints, pole, a, b, c, frames)) for pole, a, b, c in poles]
        constraints, kinds, skipped, plugs = connect(namespace, joints, found + ik_ends)
        pole_constraints, pole_plugs = connect_poles(pole_data, frames, kinds)
        constraints += pole_constraints
        plugs += pole_plugs
        ik_count = sum(1 for _, control, _ in ik_ends if control in kinds) + len(pole_constraints)
        cmds.currentTime(start, edit=True, update=True)
        driven = sorted({joint for joint, control, _ in found if control in kinds})
        check = compare(namespace, joints, driven, _check_frames(start, end))
        log(f"已接上 {len(constraints)} 個控制器（IK 控制器 {ik_count} 個跟著 FK 擺好）。接上後和{source_name}比：{describe(check)}")
        if check["deg"] > CHECK_DEG:
            log(f"注意：沒有分段關節的骨頭也差了 {check['deg']}°，rig 的控制器和關節關係可能和預期不同。")
        if unmapped:
            log(f"{len(unmapped)} 個會動的關節找不到 FK 控制器，沒有套用：{', '.join(unmapped)}")
        if skipped:
            log(f"略過 {len(skipped)} 個全部鎖住的控制器：{', '.join(_short(c) for c in skipped)}")
        if on_connected:
            on_connected(namespace)
        if not plugs:
            raise ApplyError("沒有可以烘焙的控制器通道。")

        bake_started = time.time()
        cmds.refresh(suspend=True)
        try:
            cmds.bakeResults(plugs, simulation=True, time=(start, end), sampleBy=1, disableImplicitControl=True,
                             preserveOutsideKeys=False, sparseAnimCurveBake=False,
                             removeBakedAttributeFromLayer=False, bakeOnOverrideLayer=False,
                             controlPoints=False, shape=False)
        finally:
            cmds.refresh(suspend=False)
        cmds.delete(_constraints())
        hinge_report = None
        if z_only:
            hinge_report = clean_elbows(namespace, frames, log)
        cmds.currentTime(start, edit=True, update=True)
        baked = compare(namespace, joints, driven, _check_frames(start, end))
        log(f"烘焙後：{describe(baked)}")
        if on_baked:
            on_baked(namespace)
        curves = sorted({c for control in kinds for c in cmds.listConnections(control, s=True, d=False, type="animCurveTA") or []})
        flips = count_flips(curves)
        if curves:
            cmds.filterCurve(curves, filter="euler")
        log(f"烘焙完成（{time.time() - bake_started:.1f} 秒），修正 {flips} 條 euler 翻轉曲線")
        info = {"namespace": namespace, "mode": "raw" if raw else "character", "rig": main, "label": label, "created": time.time(),
                "range_seconds": previous_range}
        layer, layer_curves, unheld = layers.to_layer(channels, plugs, holds, layers.layer_name(namespace, label), info, start)
        if set(unheld).intersection([*extra_weights.values(), *biases]):
            raise ApplyError("無法把分段彎曲與扭轉的設定保存在本次動畫層。")
        if raw:
            baked = compare(namespace, joints, driven, _check_frames(start, end))
            log(f"動畫層和原始動作預覽比：{describe(baked)}")
        remove_character()
        log(f"已放進動畫層 {layer}（{len(layer_curves)} 條曲線），手腳和脊椎都是 FK。把這個動畫層靜音或刪除，就回到原本的動畫。")
        if unheld:
            log(f"{len(unheld)} 個屬性無法放進動畫層，已直接設成套用時的值：{', '.join(_short(p) for p in unheld[:6])}")
        timing.finish(target)
        keys = layers.key_report(layer_curves)
        cmds.currentTime(0, edit=True)
        cmds.select(clear=True)
        return {"main": main, "namespace": namespace, "layer": layer, "structure": structure, "check": check, "baked_check": baked,
                "z_only": hinge_report,
                "driven_controls": len(kinds), "unmapped_joints": unmapped, "skipped_controls": [_short(c) for c in skipped],
                "fk_switches": len(switches), "bake_range_30fps": (start, end), "euler_flips_fixed": flips, "keys": keys,
                "time_unit": cmds.currentUnit(q=True, time=True), "fps": timing_mod.scene_fps(),
                "seconds": round(time.time() - started, 2)}
    except Exception as error:
        if not isinstance(error, ApplyError):
            traceback.print_exc()
        problems = []
        for step in (lambda: cmds.delete(_constraints()) if _constraints() else None, remove_character,
                     lambda: layers.remove_layer(layer, restore_range=False) if layer and cmds.objExists(layer) else None,
                     lambda: adv._remove_new_keys(namespace, curves_before), channels.restore, timing.abort):
            try:
                step()
            except Exception as cleanup_error:
                problems.append(str(cleanup_error))
        log(f"{source_name}套用失敗，已清理：{error}" + (f"（清理問題：{'；'.join(problems)}）" if problems else ""))
        if isinstance(error, ApplyError):
            raise
        raise ApplyError(str(error)) from error
    finally:
        cmds.flushUndo()
