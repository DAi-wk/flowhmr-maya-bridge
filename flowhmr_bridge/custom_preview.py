"""Custom mapping driven by FlowHMR's own preview solver (自訂對應＋HMR 預覽求解).

The job's detected motion (its SMPL-H stream) is solved by FlowHMR's createFbxCharacterRetarget on
the rig's own skin skeleton, as the character preview does; the profile's controls then follow the
solved joints. On a rig whose controls turn their joints one to one the result equals the preview.

1. Build pose: the profile's rest rule ("zero": every driven control at 0) and hold values.
2. Probe: each mapped control is turned a few degrees per axis. The joint nearest the root that turns
   with it in world space on at least two axes is the joint it drives. A control that turns no axis
   one to one ("spread": a spine bias, a ribbon) is reported, and optionally solved numerically.
3. Slots: profile["hmr"]["joints"], else the probed joint of each slot's control. With HMR's saved
   character, the solver uses HMR's own mapping and 校正值 on that FBX instead.
4. Each control follows its joint's solved target, with the offset the two have at the build pose
   (character.connect); IK controls follow the limb end, poles a point in front of the elbow/knee.
5. Bake, compare every solved joint with its target, and move the result to a new override layer.

Profile block read only by this engine (see mapping.expand_hmr):
    "hmr": {"joints": {...}, "followers": [[joint, control, kind], ...], "hold": {plug: value}}
"""
from __future__ import annotations

import math
import time
import traceback
from pathlib import Path

import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma
import maya.cmds as cmds

from . import character, generic, layers, mapping, raw
from . import timing as timing_mod
from .adv import ApplyError, count_flips

STEP = 5.0                  # probe turn, degrees
ONE_TO_ONE = 0.05           # local share within this of 1 ...
RIGID_DEG = 0.5             # ... and world turn within this of the control's: the axis is 1:1
BIND_DEG, BIND_CM = 0.5, 0.1
CHECK_DEG, CHECK_CM = 0.05, 0.05


# ------------------------------------------------------------------ small helpers
def _world(node):
    return om.MMatrix(cmds.getAttr(node + ".worldMatrix[0]"))


def _quat(matrix):
    return om.MTransformationMatrix(matrix).rotation(asQuaternion=True)


def _angle(a, b):
    d = a.inverse() * b
    return math.degrees(2 * math.acos(min(1.0, abs(d.w))))


def _short(node):
    return character._short(node)


def _distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in (12, 13, 14)))


def _joint(namespace, name):
    found = cmds.ls(namespace + name, long=True, type="joint") or []
    if len(found) > 1:
        raise ApplyError(f"rig 裡有 {len(found)} 個叫 {name} 的關節，無法分辨。")
    return found[0] if found else None


# ------------------------------------------------------------------ skeleton
def deformation_tree(namespace):
    """Every joint of the largest joint-only hierarchy among the rig's skin influences. Helper
    hierarchies (bendy limbs, ribbons, face rigs under groups) follow it and are left out."""
    influences = set()
    for cluster in cmds.ls(namespace + "*", type="skinCluster") or []:
        for influence in cmds.skinCluster(cluster, q=True, influence=True) or []:
            path = (cmds.ls(influence, long=True) or [None])[0]
            if path and cmds.nodeType(path) == "joint":
                influences.add(path)
    if not influences:
        raise ApplyError("這個 namespace 裡找不到蒙皮（skinCluster）骨架。HMR 預覽求解要在 rig 的蒙皮骨架上計算。")
    trees = {}
    for path in influences:
        top, parent = path, path.rpartition("|")[0]
        while parent and cmds.nodeType(parent) == "joint":
            top, parent = parent, parent.rpartition("|")[0]
        trees.setdefault(top, []).append(path)
    top = max(trees, key=lambda t: len(trees[t]))
    tree = [top] + (cmds.listRelatives(top, allDescendents=True, type="joint", fullPath=True) or [])
    return sorted(tree, key=lambda p: (p.count("|"), p))


def bind_gaps(paths):
    """{short name: (deg, cm)} for joints that sit off their skin bind pose in every skinCluster
    they belong to (one cluster bound elsewhere, a hair mesh say, is not counted)."""
    best = {}
    wanted = set(paths)
    for cluster in cmds.ls(type="skinCluster") or []:
        for i in cmds.getAttr(cluster + ".matrix", multiIndices=True) or []:
            source = cmds.listConnections(f"{cluster}.matrix[{i}]", s=True, d=False) or []
            path = (cmds.ls(source[0], long=True) or [None])[0] if source else None
            if path not in wanted:
                continue
            bind = om.MMatrix(cmds.getAttr(f"{cluster}.bindPreMatrix[{i}]")).inverse()
            now = _world(path)
            gap = (_angle(_quat(bind), _quat(now)), _distance(bind, now))
            old = best.get(path)
            if old is None or gap[0] + gap[1] < old[0] + old[1]:
                best[path] = gap
    return {_short(p): g for p, g in best.items() if g[0] > BIND_DEG or g[1] > BIND_CM}


# ------------------------------------------------------------------ probe
def probe(controls, tree):
    """{control: {"primary": joint short name or None, "axes": {axis: [(joint, local share,
    world share, depth, deviation)]}, "kind": "rigid" | "twist" | "spread" | "none"}}.

    Values are restored whatever happens; the nudged control's own world turn is checked so a
    stale evaluation cannot pass for a rig that does not move."""
    parent_of = {p: p.rpartition("|")[0] or None for p in tree}
    parents = {p for p in parent_of.values() if p}

    def sample():
        worlds = {p: _world(p) for p in set(tree) | parents}
        local = {p: _quat(worlds[p] * worlds[parent_of[p]].inverse()) if parent_of[p] else _quat(worlds[p])
                 for p in tree}
        return worlds, local

    base_world, base_local = sample()
    base_quat = {p: _quat(m) for p, m in base_world.items()}
    result = {}
    for control in controls:
        entry = {"axes": {}}
        c0 = _quat(_world(control))
        for axis in "XYZ":
            plug = f"{control}.rotate{axis}"
            if not generic._free(plug):
                continue
            value = cmds.getAttr(plug)
            try:
                cmds.setAttr(plug, value + STEP)
                c1 = _quat(_world(control))
                turned = _angle(c0, c1)
                if abs(turned - STEP) > 0.5:
                    raise ApplyError(f"轉動 {_short(control)}.rotate{axis} {STEP:g}° 後，控制器本身只轉了 {turned:.2f}°："
                                     "場景的評估沒有更新，或這個通道被 rig 內部改寫。")
                worlds, local = sample()
            finally:
                cmds.setAttr(plug, value)
            turn = c0.inverse() * c1
            responses = []
            for p in tree:
                share = _angle(base_local[p], local[p]) / STEP
                if share < 0.002:
                    continue
                q = _quat(worlds[p])
                # (joint, local share, world share, depth, degrees off a rigid follow of the control)
                responses.append((_short(p), round(share, 4), round(_angle(base_quat[p], q) / STEP, 4),
                                  p.count("|"), round(_angle(turn, base_quat[p].inverse() * q), 3)))
            entry["axes"][axis] = responses
        # The joint nearest the root that turns in world space with the control on at least two
        # of the probed axes; ties go to the larger local share.
        hits, share, depth = {}, {}, {}
        for responses in entry["axes"].values():
            for name, local_share, world_share, d, _ in responses:
                if world_share < 0.3:
                    continue
                hits[name] = hits.get(name, 0) + 1
                share[name] = share.get(name, 0) + local_share
                depth[name] = d
        need = min(2, len(entry["axes"]))
        candidates = [n for n in share if hits[n] >= need]
        primary = min(candidates, key=lambda n: (depth[n], -share[n])) if candidates else None
        entry["primary"] = primary
        one_to_one = 0
        for responses in entry["axes"].values():
            mine = next((r for r in responses if r[0] == primary), None)
            if mine and abs(mine[1] - 1) <= ONE_TO_ONE and mine[4] <= RIGID_DEG:
                one_to_one += 1
        entry["kind"] = ("none" if primary is None else "rigid" if one_to_one == len(entry["axes"])
                         else "twist" if one_to_one else "spread")
        result[control] = entry
    cmds.currentTime(cmds.currentTime(q=True), update=True)
    return result


# ------------------------------------------------------------------ solve
def _slot_of(source):
    return mapping.FLOWHMR_JOINTS.index(source) if source in mapping.FLOWHMR_JOINTS else None


def slot_joints(profile, namespace, probed, joints_map):
    """{slot index: joint short name}: the profile's hmr.joints first, then the probed joint of
    each slot's control (the root control for the pelvis). Each joint is used once."""
    root, chains, _ = generic.expand(profile)
    slots = {}
    root_control = namespace + root["control"]
    if probed.get(root_control, {}).get("primary"):
        slots[0] = probed[root_control]["primary"]
    for chain in chains:
        for source, control in chain["links"]:
            slot = _slot_of(source)
            primary = probed.get(namespace + control, {}).get("primary")
            if slot is not None and slot not in slots and primary:
                slots[slot] = primary
    for source, joint in joints_map.items():
        slot = _slot_of(source)
        if joint:
            if not _joint(namespace, joint):
                raise ApplyError(f"hmr.joints 指定的關節 {joint} 在場景中找不到。")
            slots[slot] = joint
        else:
            slots.pop(slot, None)
    used, notes = {}, []
    for slot in sorted(slots):
        joint = slots[slot]
        if joint in used:
            notes.append(f"{mapping.FLOWHMR_JOINTS[slot]} 和 {mapping.FLOWHMR_JOINTS[used[joint]]} 都對到 {joint}，"
                         f"只保留後者")
        else:
            used[joint] = slot
    slots = {slot: joint for joint, slot in used.items()}
    missing = [mapping.FLOWHMR_JOINTS[s] for s in mapping.SOLVER_REQUIRED if s not in slots]
    if missing:
        raise ApplyError("HMR 預覽求解缺少這些關節的對應：" + "、".join(missing) +
                         "。請在對應設定指定它們的控制器，或在設定檔的 hmr.joints 直接指定蒙皮關節。")
    return slots, notes


def scene_skeleton(namespace, slots):
    """The solver's target skeleton from the rig at its build pose (raw._scene_skeleton for any rig)."""
    paths = [_joint(namespace, slots[i]) if i in slots else None for i in range(len(mapping.FLOWHMR_JOINTS))]
    ancestors = set()
    for path in paths:
        while path and path not in ancestors:
            if cmds.nodeType(path) == "joint":
                ancestors.add(path)
            path = path.rpartition("|")[0]
    ankles = [p for p in paths[7:9] if p]
    for ankle in ankles:
        ancestors.update(cmds.listRelatives(ankle, allDescendents=True, type="joint", fullPath=True) or [])
    ordered = sorted(ancestors, key=lambda p: (p.count("|"), p))
    names = [_short(p) for p in ordered]
    if len(set(names)) != len(names):
        raise ApplyError("求解用的骨架裡有同名的關節，無法分辨。")
    index = {p: i for i, p in enumerate(ordered)}
    bones = []
    for path in ordered:
        matrix = list(cmds.getAttr(path + ".worldMatrix[0]"))
        matrix[12:15] = [v / 100.0 for v in matrix[12:15]]
        parent = path.rpartition("|")[0]
        while parent and parent not in index:
            parent = parent.rpartition("|")[0]
        bones.append(dict(name=_short(path), parent=index.get(parent, -1), matrix=matrix))
    mapping_ = [index.get(p, -1) if p else -1 for p in paths]
    carrier = mapping_[0]
    while bones[carrier]["parent"] >= 0:
        carrier = bones[carrier]["parent"]
    return dict(bones=bones, map=mapping_, carrier=carrier, feet=raw._foot_geometry(ankles, index))


class Targets:
    """World targets per frame for every rig joint the apply needs. Solved joints come from the
    solver; any other joint keeps its build-pose transform relative to its nearest solved
    ancestor, as unmapped bones do in the preview."""

    def __init__(self, namespace, tracks, samples, frames):
        self.namespace, self.frames = namespace, frames
        self.records = {r["name"]: (i, r) for i, r in enumerate(tracks["records"])}
        self.samples = samples
        self.rest = {}
        self.cache = {}
        self.nodes = {}

    def _rest(self, name):
        if name not in self.rest:
            self.rest[name] = _world(_joint(self.namespace, name))
        return self.rest[name]

    def matrices(self, name):
        if name in self.cache:
            return self.cache[name]
        if name in self.records:
            i, _ = self.records[name]
            out = []
            for f in range(self.frames):
                at = 7 * (i * self.frames + f)
                m = om.MTransformationMatrix(om.MQuaternion(*self.samples[at + 3:at + 7]).asMatrix())
                m.setTranslation(om.MVector(*self.samples[at:at + 3]), om.MSpace.kWorld)
                out.append(m.asMatrix())
        else:
            path = _joint(self.namespace, name)
            ancestor = path.rpartition("|")[0]
            while ancestor and _short(ancestor) not in self.records:
                ancestor = ancestor.rpartition("|")[0]
            if not ancestor:
                out = [self._rest(name)] * self.frames
            else:
                local = self._rest(name) * self._rest(_short(ancestor)).inverse()
                out = [local * m for m in self.matrices(_short(ancestor))]
        self.cache[name] = out
        return out

    def node(self, name):
        """A transform keyed on the joint's target (cm, radians)."""
        if name in self.nodes:
            return self.nodes[name]
        node = cmds.createNode("transform", name=character.char("target_" + name), skipSelect=True)
        times = om.MTimeArray()
        for f in range(self.frames):
            times.append(om.MTime(f, om.MTime.uiUnit()))
        values = [[] for _ in range(6)]
        previous = None
        for matrix in self.matrices(name):
            m = om.MTransformationMatrix(matrix)
            r = m.rotation()
            if previous is not None:
                r = r.closestSolution(previous)
            previous = r
            t = m.translation(om.MSpace.kWorld)
            for axis, v in zip(values, (t.x, t.y, t.z, r.x, r.y, r.z)):
                axis.append(v)
        selection = om.MSelectionList()
        selection.add(node)
        fn = om.MFnDependencyNode(selection.getDependNode(0))
        for attr, axis in zip(("tx", "ty", "tz", "rx", "ry", "rz"), values):
            curve = oma.MFnAnimCurve()
            curve.create(fn.findPlug(attr, False), curve.kAnimCurveTL if attr[0] == "t" else curve.kAnimCurveTA)
            curve.addKeys(times, om.MDoubleArray(axis), curve.kTangentLinear, curve.kTangentLinear)
        self.nodes[name] = node
        return node

    def translates(self, name):
        return name in self.records and self.records[name][1]["translates"]


def compare(namespace, targets, names, frames):
    """Worst world difference between rig joints and their targets over ``frames``."""
    deg = {n: 0.0 for n in names}
    cm = {n: 0.0 for n in names}
    paths = {n: _joint(namespace, n) for n in names}
    previous = cmds.currentTime(q=True)
    try:
        for f in frames:
            cmds.currentTime(f, update=True)
            for n in names:
                a, e = _world(paths[n]), targets.matrices(n)[int(f)]
                deg[n] = max(deg[n], _angle(_quat(a), _quat(e)))
                cm[n] = max(cm[n], _distance(a, e))
    finally:
        cmds.currentTime(previous, update=True)
    worst_deg = max(deg, key=deg.get) if deg else None
    worst_cm = max(cm, key=cm.get) if cm else None
    off = sorted((n for n in names if deg[n] > CHECK_DEG or cm[n] > CHECK_CM), key=lambda n: -deg[n])
    return {"deg": round(deg[worst_deg], 3) if worst_deg else 0.0, "deg_joint": worst_deg,
            "cm": round(cm[worst_cm], 3) if worst_cm else 0.0, "cm_joint": worst_cm,
            "off": [(n, round(deg[n], 2), round(cm[n], 2)) for n in off], "frames": len(frames)}


def describe(check):
    text = f"旋轉最大差 {check['deg']}°（{check['deg_joint']}），位置最大差 {check['cm']} cm（{check['cm_joint']}）"
    if check["off"]:
        text += "；超過 0.05° 或 0.05 cm 的關節：" + "、".join(f"{n} {d}°/{c} cm" for n, d, c in check["off"][:6])
        if len(check["off"]) > 6:
            text += f" 等 {len(check['off'])} 個"
    return text


# ------------------------------------------------------------------ numeric solve
def refine_spread(controls, channels, probed, targets, frames, chain, iterations=4):
    """Per frame, damped Gauss-Newton on the rotate channels of ``controls`` (spread controls)
    against the targets of the skeleton joints they move, while the other controls stay on their
    live constraints. ``chain``: the solver's skeleton (solved joints and the joints between
    them); hair, cloth and other helpers that also respond are left out. Keys each frame;
    returns (channels, report). ``channels``: the controls' baked rotate channels."""
    import numpy as np

    objective = set()
    for control in controls:
        info = probed[control]
        objective.add(info["primary"])
        for responses in info["axes"].values():
            objective.update(r[0] for r in responses if r[1] >= 0.01)
    names = sorted(n for n in objective & chain if _joint(targets.namespace, n))
    paths = {n: _joint(targets.namespace, n) for n in names}
    for path in list(paths.values()):  # the next skeleton joints, whose positions they move
        for child in cmds.listRelatives(path, children=True, type="joint", fullPath=True) or []:
            if _short(child) in chain:
                paths.setdefault(_short(child), child)
    names = sorted(paths)

    def rotvec(q):
        if q.w < 0:
            q = om.MQuaternion(-q.x, -q.y, -q.z, -q.w)
        axis, theta = q.asAxisAngle()
        d = math.degrees(theta)
        return [axis.x * d, axis.y * d, axis.z * d]

    def residual(x, f):
        for plug, value in zip(channels, x):
            cmds.setAttr(plug, value)
        out = []
        for n in names:
            a, e = _world(paths[n]), targets.matrices(n)[f]
            out += rotvec(_quat(e).inverse() * _quat(a))
            out += [a[i] - e[i] for i in (12, 13, 14)]
        return np.array(out)

    worst, improved = 0.0, 0
    for f in frames:
        cmds.currentTime(f, update=True)
        x = np.array([cmds.getAttr(p) for p in channels])  # the follower pose baked at this frame
        r = residual(x, f)
        start_cost = r @ r
        damping = 1e-3
        for _ in range(iterations):
            jac = np.empty((len(r), len(x)))
            for i in range(len(x)):
                step = x.copy()
                step[i] += 0.5
                jac[:, i] = (residual(step, f) - r) / 0.5
            a, g = jac.T @ jac, jac.T @ r
            while True:
                dx = -np.linalg.solve(a + damping * np.diag(np.diag(a)) + 1e-9 * np.eye(len(x)), g)
                candidate = residual(x + dx, f)
                if candidate @ candidate < r @ r or damping > 1e6:
                    break
                damping *= 10
            if candidate @ candidate < r @ r:
                x, r = x + dx, candidate
                damping = max(damping / 10, 1e-6)
            else:
                break
        residual(x, f)
        for plug, value in zip(channels, x):
            node, attr = plug.rsplit(".", 1)
            cmds.setKeyframe(node, attribute=attr, time=f, value=float(value))
        improved += r @ r < start_cost * (1 - 1e-6)
        worst = max(worst, float(np.abs(r).max()))
    return channels, {"joints": names, "channels": len(channels), "worst_residual": round(worst, 3),
                      "improved": int(improved), "frames": len(frames)}


# ------------------------------------------------------------------ apply
def _check_frames(start, end, count=6):
    return character._check_frames(start, end, count)


def apply(namespace, profile, source, target_unit=None, log=print, on_connected=None, on_baked=None,
          overwrite=False, label=None, refine=False):
    """Bake the job's preview solve (``source`` = client.preview_source) onto the rig in ``namespace``
    ("" or "Ns:") through ``profile``. The result goes on a new override animation layer.
    ``refine`` solves the spread controls numerically (slow) instead of following one joint."""
    started = time.time()
    if isinstance(profile, (str, Path)):
        profile = generic.load_profile(profile)
    if cmds.upAxis(q=True, axis=True) != "y":
        raise ApplyError("HMR 預覽求解需要 Y 軸朝上的場景。")
    if cmds.namespace(exists=character.CHAR_NS) or character._constraints():
        raise ApplyError(f"場景已有 {character.CHAR_NS} 節點或舊的角色約束，無法建立這次的暫存目標。")
    joints_map, followers, hmr_hold = mapping.expand_hmr(profile)
    root, chains, hold = generic.expand(profile)
    hold.update(mapping.switch_holds(profile))
    hold.update(hmr_hold)
    holds = {namespace + plug: value for plug, value in hold.items()}
    missing_holds = [plug for plug in holds if not cmds.objExists(plug)]
    holds = {plug: value for plug, value in holds.items() if cmds.objExists(plug)}
    controls = generic.mapped_controls(profile, namespace)
    if not controls:
        raise ApplyError("設定檔裡的控制器在場景中都找不到。")
    follower_list = [(joint, namespace + control, kind) for joint, control, kind in followers
                     if cmds.objExists(namespace + control)]
    missing_followers = [control for _, control, _ in followers if not cmds.objExists(namespace + control)]
    driven = list(dict.fromkeys(controls + [c for _, c, _ in follower_list]))
    label = label or "SMPL-H"
    recorded = [p for c in driven for p in generic._plugs(c, "parent")] + list(holds)
    prior, prior_animated = layers.underneath(namespace, recorded, layers.sample_times())
    channels = layers.Channels(recorded + list(prior), label=label)
    if channels.animated and not overwrite:
        raise ApplyError(f"有 {len(channels.animated)} 個控制器已經有動畫（例如 {', '.join(channels.animated[:3])}）。"
                         "要疊上 FlowHMR 的動作，請勾選「rig 已有動畫時也套用」。")
    timing = timing_mod.Timing()
    target_unit = target_unit or timing.unit
    previous_range = layers.range_seconds()
    curves_before = set(cmds.ls(type="animCurve"))
    layer = None
    saved = source.get("model") is not None
    log(f"HMR 預覽求解：{profile.get('name', '?')} → {namespace or '(無 namespace)'}，{len(driven)} 個控制器")
    if channels.animated:
        log(f"{len(channels.animated)} 個控制器已有動畫：烘焙時先拆下，烘焙後放回動畫層下方。")
    if missing_followers:
        log(f"hmr.followers 有 {len(missing_followers)} 個控制器在場景中找不到，略過：{', '.join(missing_followers[:6])}")
    if missing_holds:
        log(f"{len(missing_holds)} 個 hold 屬性在場景中找不到，略過：{', '.join(_short(p) for p in missing_holds[:6])}")
    try:
        channels.free()
        # after free(): _free() rejects keyed channels, so this must not run earlier
        rest_plugs = ([p for c in driven for p in generic._channels(c, "parent")]
                      if profile.get("rest", "zero") == "zero" else [])
        timing.prepare(import_fbx=False)
        frames_count = source["meta"]["frames"]
        start, end = 0, frames_count - 1
        cmds.namespace(add=character.CHAR_NS)
        cmds.playbackOptions(minTime=start, maxTime=end, animationStartTime=start, animationEndTime=end)
        cmds.currentTime(start, edit=True)
        for plug in rest_plugs:
            cmds.setAttr(plug, 0)
        layers.put(prior)  # what earlier FlowHMR layers set on channels this apply does not drive
        for line in layers.describe_underneath(prior, prior_animated):
            log(line)
        for plug, value in holds.items():
            if generic._settable(plug):
                cmds.setAttr(plug, value)
        cmds.currentTime(start, edit=True, update=True)

        # ---- probe
        tree = deformation_tree(namespace)
        probe_controls = [namespace + root["control"]] + [namespace + c for chain in chains for _, c in chain["links"]]
        probe_controls = [c for c in dict.fromkeys(probe_controls) if cmds.objExists(c)]
        probe_started = time.time()
        cmds.refresh(suspend=True)
        try:
            probed = probe(probe_controls, tree)
        finally:
            cmds.refresh(suspend=False)
        cmds.currentTime(start, edit=True, update=True)
        kinds_count = {k: [c for c, e in probed.items() if e["kind"] == k] for k in ("rigid", "twist", "spread", "none")}
        log(f"探測 {len(probed)} 個控制器（{time.time() - probe_started:.1f} 秒）：{len(kinds_count['rigid'])} 個一比一帶動關節，"
            f"{len(kinds_count['twist'])} 個有一軸的扭轉分給其他關節，{len(kinds_count['spread'])} 個把旋轉分散到多個關節")
        if kinds_count["spread"]:
            log("旋轉分散（例如脊椎 bias、ribbon；可用 hmr.hold 固定分配，或勾選數值求解）："
                + "、".join(f"{_short(c)}→{probed[c]['primary']}" for c in kinds_count["spread"][:8]))
        if kinds_count["none"]:
            log(f"{len(kinds_count['none'])} 個控制器轉動時沒有帶動任何蒙皮關節，略過：{', '.join(_short(c) for c in kinds_count['none'][:6])}")

        # ---- solve
        skeleton = None
        if saved:
            log("套用 HMR 為這個人物儲存的角色、對應和校正值：" + source.get("model_name", "?"))
        else:
            slots, notes = slot_joints(profile, namespace, probed, joints_map)
            for note in notes:
                log("注意：" + note)
            skeleton = scene_skeleton(namespace, slots)
            gaps = bind_gaps([_joint(namespace, slots[s]) for s in slots])
            if gaps:
                log("注意：這些關節不在蒙皮的 bind pose（HMR 讀匯出的 FBX 時會用 bind pose）："
                    + "、".join(f"{n} {d:.1f}°/{c:.1f} cm" for n, (d, c) in list(gaps.items())[:6])
                    + "。請確認 rig 在 build pose，沒有對應到的控制器也要歸零。")
            sole = "場景中的足部蒙皮" if skeleton["feet"] else "求解器估計的鞋底"
            log(f"以 rig 的 build pose 和{sole}求解，{len(slots)} 個關節對應")
        tracks, samples = raw._solve(source, skeleton, [])
        try:
            raw._check_target(namespace, tracks, pelvis_name=None)
        except ApplyError as error:
            if not saved:
                raise
            raise ApplyError(f"{error} HMR 為這個人物儲存的預覽角色（{source.get('model_name')}）不是這個 rig 的蒙皮骨架。"
                             "請在 HMR 改用這個 rig 的蒙皮 FBX 並儲存，或取消勾選「使用 HMR 已儲存的角色設定」。") from None
        targets = Targets(namespace, tracks, samples, frames_count)
        # Joints the preview keeps at their rest local transform between solved ones (Part joints):
        # a control that twists them is fine only if a follower puts them back.
        solved_paths = [_joint(namespace, r["name"]) for r in tracks["records"]]
        skeleton_names = set()
        for path in solved_paths:
            while path and _short(path) not in skeleton_names:
                skeleton_names.add(_short(path))
                path = path.rpartition("|")[0]
        followed = {j for j, _, _ in follower_list}
        primaries = {e["primary"] for e in probed.values()}
        for control in kinds_count["twist"]:
            spread_to = sorted({r[0] for responses in probed[control]["axes"].values() for r in responses
                                if r[1] >= 0.01} & skeleton_names - followed - primaries)
            if spread_to:
                log(f"注意：{_short(control)} 的扭轉會分給 {'、'.join(spread_to[:4])}，HMR 預覽讓這些關節保持靜置。"
                    "要一致，請在 hmr.followers 加上帶動它們的控制器。")

        # ---- pairs
        found, unpaired = [], []
        root_control = namespace + root["control"]
        for control in probe_controls:
            joint = probed[control]["primary"]
            if not joint:
                continue
            kind = "parent" if control == root_control or targets.translates(joint) else "orient"
            found.append((joint, control, kind))
        for joint, control, kind in follower_list:
            if not _joint(namespace, joint):
                unpaired.append(f"{joint}（找不到關節）")
                continue
            found.append((joint, control, "parent" if targets.translates(joint) else kind))
        # FlowHMR joint -> rig joint for the IK limbs: the solver slots, else (HMR's saved mapping,
        # which the records do not label) the probed joints of the profile's FK chain.
        by_source = {}
        for chain in chains:
            for src, control in chain["links"]:
                primary = probed.get(namespace + control, {}).get("primary")
                if primary:
                    by_source.setdefault(src, primary)
        if not saved:
            by_source.update({mapping.FLOWHMR_JOINTS[s]: j for s, j in slots.items()})
        poles = []
        for entry in generic.expand_ik(profile):
            chain_joints = [by_source.get(entry[k]) for k in ("start", "mid", "end")]
            if not all(chain_joints):
                continue
            control = namespace + entry["control"] if entry["control"] else None
            if control and cmds.objExists(control):
                found.append((chain_joints[2], control, "parent"))
            if entry["toe"] and by_source.get(entry["toe"][0]) and cmds.objExists(namespace + entry["toe"][1]):
                found.append((by_source[entry["toe"][0]], namespace + entry["toe"][1], "orient"))
            pole = namespace + entry["pole"] if entry["pole"] else None
            if pole and cmds.objExists(pole):
                poles.append((pole, *chain_joints))
        solved = {probed[c]["primary"] for c in kinds_count["spread"]} if refine else set()
        covered = {j for j, _, _ in found} | solved
        loose = [r["name"] for r in tracks["records"] if not r["auxiliary"] and r["name"] not in covered]
        frames = list(range(start, end + 1))
        joints = {j: targets.node(j) for j in {j for j, _, _ in found} | {j for p in poles for j in p[1:]}}
        pole_data = [(pole, character.pole_points(namespace, joints, pole, a, b, c, frames)) for pole, a, b, c in poles]
        constraints, kinds, skipped, plugs = character.connect(namespace, joints, found)
        pole_constraints, pole_plugs = character.connect_poles(pole_data, frames, kinds)
        constraints += pole_constraints
        plugs += pole_plugs
        cmds.currentTime(start, edit=True, update=True)
        check_names = sorted({j for j, c, _ in found if c in kinds} | solved | set(loose))
        check = compare(namespace, targets, check_names, _check_frames(start, end))
        log(f"已接上 {len(constraints)} 個控制器。接上後和 HMR 預覽比：{describe(check)}")
        if loose:
            log(f"{len(loose)} 個求解的關節沒有控制器跟著（例如角色整體移動的 root 關節，可加到 hmr.followers）：{', '.join(loose[:6])}")
        if unpaired:
            log(f"hmr.followers 略過 {len(unpaired)} 項：{', '.join(unpaired[:6])}")
        if skipped:
            log(f"略過 {len(skipped)} 個全部鎖住的控制器：{', '.join(_short(c) for c in skipped)}")
        if on_connected:
            on_connected(namespace)

        refine_report = None
        spread = [c for c in kinds_count["spread"] if c in kinds]
        if refine and spread:
            refine_started = time.time()
            log(f"數值求解 {len(spread)} 個分散型控制器…")
            # Start every frame from the follower result: bake those controls, take them off
            # their constraints (the others stay live), then improve frame by frame. A frame keeps
            # the follower pose unless the solve lowers its residual.
            spread_paths = {(cmds.ls(c, long=True) or [c])[0] for c in spread}
            spread_plugs = [p for p in plugs if p.split(".")[0] in spread]
            cmds.refresh(suspend=True)
            try:
                cmds.bakeResults(spread_plugs, simulation=True, time=(start, end), sampleBy=1,
                                 disableImplicitControl=True, preserveOutsideKeys=False, sparseAnimCurveBake=False,
                                 removeBakedAttributeFromLayer=False, bakeOnOverrideLayer=False,
                                 controlPoints=False, shape=False)
                own = [c for c in character._constraints()
                       if (cmds.listRelatives(c, parent=True, fullPath=True) or [None])[0] in spread_paths]
                if own:
                    cmds.delete(own)
                rotate = [p for p in spread_plugs if p.rsplit(".", 1)[1].startswith("rotate")]
                refine_channels, refine_report = refine_spread(spread, rotate, probed, targets, frames, skeleton_names)
            finally:
                cmds.refresh(suspend=False)
            plugs += refine_channels
            log(f"數值求解完成（{time.time() - refine_started:.1f} 秒）：{refine_report['improved']} / "
                f"{refine_report['frames']} 幀比跟隨更接近，其餘保留跟隨的姿勢")
        plugs = list(dict.fromkeys(plugs))
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
        cmds.delete(character._constraints())
        curves = sorted({c for control in kinds for c in cmds.listConnections(control, s=True, d=False, type="animCurveTA") or []})
        flips = count_flips(curves)
        if curves:
            cmds.filterCurve(curves, filter="euler")
        log(f"烘焙完成（{time.time() - bake_started:.1f} 秒），修正 {flips} 條 euler 翻轉曲線")
        if on_baked:
            on_baked(namespace)
        info = {"namespace": namespace, "mode": "custom_hmr", "profile": profile.get("name"), "label": label,
                "created": time.time(), "range_seconds": previous_range}
        layer, layer_curves, unheld = layers.to_layer(channels, plugs, holds, layers.layer_name(namespace, label), info, start)
        settings = {namespace + p for p in hmr_hold}
        if settings.intersection(unheld):
            raise ApplyError("hmr.hold 的設定無法放進動畫層（移除動畫層後會留在 rig 上）："
                             + "、".join(_short(p) for p in settings.intersection(unheld)))
        baked = compare(namespace, targets, check_names, _check_frames(start, end))
        log(f"動畫層和 HMR 預覽比：{describe(baked)}")
        character.remove_character()
        log(f"已放進動畫層 {layer}（{len(layer_curves)} 條曲線）。把這個動畫層靜音或刪除，就回到原本的動畫。")
        if unheld:
            log(f"{len(unheld)} 個屬性無法放進動畫層，已直接設成套用時的值：{', '.join(_short(p) for p in unheld[:6])}")
        timing.finish(target_unit)
        keys = layers.key_report(layer_curves)
        cmds.currentTime(0, edit=True)
        cmds.select(clear=True)
        return {"namespace": namespace, "profile": profile.get("name"), "layer": layer, "check": check,
                "baked_check": baked, "driven_controls": len(kinds), "skipped_controls": [_short(c) for c in skipped],
                "probe": {k: [_short(c) for c in v] for k, v in kinds_count.items()}, "loose_joints": loose,
                "refine": refine_report, "bake_range_30fps": (start, end), "euler_flips_fixed": flips, "keys": keys,
                "time_unit": cmds.currentUnit(q=True, time=True), "fps": timing_mod.scene_fps(),
                "seconds": round(time.time() - started, 2)}
    except Exception as error:
        if not isinstance(error, ApplyError):
            traceback.print_exc()
        problems = []
        for step in (lambda: cmds.delete(character._constraints()) if character._constraints() else None,
                     character.remove_character,
                     lambda: layers.remove_layer(layer, restore_range=False) if layer and cmds.objExists(layer) else None,
                     lambda: generic._remove_new_keys(driven, curves_before), channels.restore, timing.abort):
            try:
                step()
            except Exception as cleanup_error:
                problems.append(str(cleanup_error))
        log(f"HMR 預覽求解套用失敗，已清理：{error}" + (f"（清理問題：{'；'.join(problems)}）" if problems else ""))
        if isinstance(error, ApplyError):
            raise
        raise ApplyError(str(error)) from error
    finally:
        cmds.flushUndo()
