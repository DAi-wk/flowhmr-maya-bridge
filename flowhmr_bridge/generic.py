"""Retarget a FlowHMR ``flowhmr``-profile FBX onto any rig's controls from a mapping profile.

Stage 1: FK chains + root, no IK. The core step is rest calibration: at the source's
T-pose frame (-1 at 30 FPS) each mapped source joint is rotated so that its bone points
the same way as the matching control chain on the rig at rest. Orient constraints with
maintained offsets then pass the source's world-space rotation changes to the controls,
so the rig's bones follow the source bones' directions whatever the rig's rest pose is.

Profile (JSON), names without namespace; "{s}" / "{Side}" expand per entry in "sides":
    {"name": "...", "rest": "zero" | "current",
     "root": {"source": "Hips", "control": "RootX_M"},
     "hold": {"FKIKLeg_{s}.FKIKBlend": 0},            # values held during the bake
     "sides": [["L", "Left"], ["R", "Right"]],
     "chains": [{"name": "arm_{s}", "align_from": 0,
                 "links": [["{Side}Arm", "FKShoulder_{s}"], ...],
                 "end_aim": ["{Side}HandMiddle1", "FKMiddleFinger1_{s}"]}]}
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import maya.api.OpenMaya as om
import maya.cmds as cmds
import maya.mel as mel

from . import timing as timing_mod
from .adv import ApplyError, count_flips
from .mapping import _expand_text, expand, expand_ik, switch_holds  # noqa: F401  (re-exported for callers)

SOURCE_NS = "FlowHMRSrc"
TAG = "flowhmrGenericCN"
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
TEST_ONLY = {"adv_generic_test"}  # FK-only ADV profile used to test the engine; ADV rigs use MoCapLibrary


# ------------------------------------------------------------------ profiles
def user_profile_dir():
    return Path(cmds.internalVar(userAppDir=True)) / "FlowHMRBridge" / "profiles"


def list_profiles():
    """[(display name, path, builtin)] — the user's own profiles first, then the shipped ones."""
    found = []
    for folder, builtin in ((user_profile_dir(), False), (PROFILE_DIR, True)):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            if builtin and path.stem in TEST_ONLY:
                continue
            try:
                name = json.loads(path.read_text(encoding="utf-8")).get("name") or path.stem
            except (OSError, ValueError):
                continue
            found.append((name + ("（內建範本）" if builtin else ""), path, builtin))
    return found


def load_profile(name_or_path):
    path = Path(name_or_path)
    if not path.suffix:
        user = user_profile_dir() / f"{name_or_path}.json"
        path = user if user.is_file() else PROFILE_DIR / f"{name_or_path}.json"
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def namespaces_with(profile):
    """Namespaces ("" = none) where the profile's root control exists."""
    root = expand(profile)[0]["control"]
    spaces = [""] + [ns + ":" for ns in cmds.namespaceInfo(":", listOnlyNamespaces=True, recurse=True) or []
                     if ns not in ("UI", "shared") and ns != SOURCE_NS]
    return [ns for ns in spaces if cmds.objExists(ns + root)]


def control_kinds(profile):
    """{control name (no namespace): constraint kind} for everything the profile drives."""
    root, chains, _ = expand(profile)
    kinds = {root["control"]: "parent"}
    for chain in chains:
        for _, control in chain["links"]:
            kinds.setdefault(control, "orient")
    for entry in expand_ik(profile):
        if entry["control"]:
            kinds.setdefault(entry["control"], "parent")
        if entry["pole"]:
            kinds.setdefault(entry["pole"], "point")
        if entry["toe"]:
            kinds.setdefault(entry["toe"][1], "orient")
    return kinds


def inspect(profile, namespace):
    """Issues that block applying and notes worth showing, like adv.inspect_rig."""
    kinds = control_kinds(profile)
    wanted = {namespace + c for c in kinds}
    controls = mapped_controls(profile, namespace)
    issues, notes = [], []
    if not controls:
        issues.append("設定檔裡的控制器在這個 namespace 都找不到。")
        return {"controls": controls, "issues": issues, "notes": notes}
    animated = _animated(controls)
    if animated:
        issues.append(f"有 {len(animated)} 個對應的控制器已經有動畫（例如 {', '.join(a.split(':')[-1] for a in animated[:3])}）。"
                      "自訂模式還不支援覆蓋，請在沒有動畫的場景或副本裡套用。")
    missing = sorted(wanted - set(controls))
    if missing:
        notes.append(f"{len(missing)} 個控制器在場景中找不到，會略過：{', '.join(m.split(':')[-1] for m in missing[:4])}")
    driven = [c for c in controls if all(rig_driven(plug) for plug in _plugs(c, kinds[c[len(namespace):]]))]
    if driven:
        notes.append(f"{len(driven)} 個控制器已被 rig 內部驅動或全部鎖住，會略過：{', '.join(d.split(':')[-1] for d in driven[:4])}")
    return {"controls": controls, "issues": issues, "notes": notes}


def _plugs(control, kind):
    attrs = {"orient": ("rotate",), "point": ("translate",)}.get(kind, ("translate", "rotate"))
    return [f"{control}.{attr}{axis}" for attr in attrs for axis in "XYZ"]


def mapped_controls(profile, namespace):
    names = list(control_kinds(profile))
    seen, result = set(), []
    for name in names:
        full = namespace + name
        if full not in seen and cmds.objExists(full):
            seen.add(full)
            result.append(full)
    return result


# ------------------------------------------------------------------ math
def _world_quat(node):
    matrix = om.MMatrix(cmds.xform(node, q=True, ws=True, matrix=True))
    return om.MTransformationMatrix(matrix).rotation(asQuaternion=True)


def _pos(node):
    return om.MVector(cmds.xform(node, q=True, ws=True, translation=True))


def _pivot(node):
    return om.MVector(cmds.xform(node, q=True, ws=True, rotatePivot=True))


def _angle(a, b):
    if a.length() < 1e-6 or b.length() < 1e-6:
        return None
    return math.degrees(a.angle(b))


def _key_world_rotation(joint, world_matrix, frame):
    """Key the joint's rotate channels at ``frame`` so its world rotation becomes ``world_matrix``.

    Maya joint: local = rotateAxis * rotate * jointOrient (row vectors); world = local * parentWorld.
    """
    parent = (cmds.listRelatives(joint, parent=True, fullPath=True) or [None])[0]
    parent_m = _world_quat(parent).asMatrix() if parent else om.MMatrix()
    local = world_matrix * parent_m.inverse()
    radians = [math.radians(v) for v in cmds.getAttr(joint + ".jointOrient")[0]]
    orient = om.MEulerRotation(radians).asMatrix()
    radians = [math.radians(v) for v in cmds.getAttr(joint + ".rotateAxis")[0]]
    axis = om.MEulerRotation(radians).asMatrix()
    rotate = axis.inverse() * local * orient.inverse()
    euler = om.MTransformationMatrix(rotate).rotation().reorder(cmds.getAttr(joint + ".rotateOrder"))
    for name, value in zip("XYZ", (euler.x, euler.y, euler.z)):
        cmds.setKeyframe(joint, attribute="rotate" + name, time=frame, value=math.degrees(value))
    # Under parallel evaluation a query at the current frame returns the cached value
    # until the time is set again; later joints must see this rotation.
    cmds.currentTime(frame, edit=True, update=True)


def _rotate_towards(joint, child, target_direction, frame):
    """Rotate ``joint`` (world, minimal turn) so ``joint``->``child`` points along ``target_direction``."""
    current = _pos(child) - _pos(joint)
    if current.length() < 1e-6 or target_direction.length() < 1e-6:
        return None
    turn = om.MQuaternion(current.normal(), target_direction.normal())
    world = _world_quat(joint).asMatrix() * turn.asMatrix()
    _key_world_rotation(joint, world, frame)
    return _angle(_pos(child) - _pos(joint), target_direction)


# ------------------------------------------------------------------ source skeleton
def import_source(fbx):
    """Import the FBX into SOURCE_NS. Maya's FBX importer ignores -namespace, so set the
    current namespace instead; import mode is pinned to add so same-named rig nodes are untouched."""
    remove_source()
    cmds.namespace(add=SOURCE_NS)
    mel.eval("FBXImportMode -v add;")
    cmds.namespace(set=":" + SOURCE_NS)
    try:
        cmds.file(str(fbx), i=True, type="FBX", ignoreVersion=True)
    finally:
        cmds.namespace(set=":")
    tops = [node for node in cmds.ls(SOURCE_NS + ":*", assemblies=True) or []]
    if not tops or not cmds.objExists(f"{SOURCE_NS}:Hips"):
        raise ApplyError("FlowHMR FBX 匯入後找不到 Hips。")
    return tops[0]


def remove_source():
    if cmds.namespace(exists=SOURCE_NS):
        cmds.namespace(removeNamespace=SOURCE_NS, deleteNamespaceContent=True)


def source(name):
    return f"{SOURCE_NS}:{name}"


# ------------------------------------------------------------------ rig state
def _animated(controls):
    animated = []
    for control in controls:
        for node in cmds.listConnections(control, s=True, d=False, skipConversionNodes=True) or []:
            kinds = cmds.nodeType(node, inherited=True) or []
            if (("animCurve" in kinds and cmds.nodeType(node).startswith("animCurveT"))
                    or "animBlendNodeBase" in kinds or "pairBlend" in kinds):
                animated.append(control)
                break
    return animated


def _settable(plug):
    """For hold values (switches are often proxy attributes, which are connected)."""
    return cmds.objExists(plug) and cmds.getAttr(plug, settable=True)


def _free(plug):
    """A control channel we may drive: unlocked and not already driven by the rig itself.
    getAttr -settable is True for constraint-driven channels, so check the input too."""
    return _settable(plug) and not cmds.connectionInfo(plug, isDestination=True)


def rig_driven(plug):
    """Locked, or driven by something other than keys (constraint, expression, rig network)."""
    if not cmds.objExists(plug):
        return False
    if cmds.getAttr(plug, lock=True):
        return True
    source = cmds.connectionInfo(plug, sourceFromDestination=True)
    if not source:
        return False
    node = source.split(".", 1)[0]
    kinds = cmds.nodeType(node, inherited=True) or []
    return not ("animCurve" in kinds or "animBlendNodeBase" in kinds or "pairBlend" in kinds)


def _channels(control, kind):
    return [plug for plug in _plugs(control, kind) if _free(plug)]


def _skip(control, attr):
    return [axis.lower() for axis in "XYZ" if not _free(f"{control}.{attr}{axis}")] or "none"


def _remember(plugs):
    saved = {}
    for plug in plugs:
        if _settable(plug):
            saved[plug] = cmds.getAttr(plug)
    return saved


def _put_back(saved):
    for plug, value in saved.items():
        if _settable(plug):
            cmds.setAttr(plug, value)


# ------------------------------------------------------------------ calibration
def _leg_length(points):
    return sum((b - a).length() for a, b in zip(points, points[1:]))


def calibrate(profile, namespace, top, frame=-1):
    """Scale the source to the rig's leg length, then align each mapped bone direction.
    Returns per-link residual angles (degrees) after alignment."""
    _, chains, _ = expand(profile)
    cmds.currentTime(frame, edit=True)
    report = {"scale": 1.0, "aligned": {}, "skipped": []}

    target = current = None
    legs = next((c for c in chains if c["name"].startswith("leg")), None)
    if legs and len(legs["links"]) >= 3 and all(cmds.objExists(namespace + c) for _, c in legs["links"][:3]):
        names = legs["links"][:3]
        target = _leg_length([_pivot(namespace + c) for _, c in names])
        current = _leg_length([_pos(source(s)) for s, _ in names])
    else:
        # IK-only legs: compare the root-to-IK-foot height with the source's hips-to-foot height.
        root = namespace + expand(profile)[0]["control"]
        foot = next((e for e in expand_ik(profile) if e["control"] and e["end"].endswith("Foot")
                     and cmds.objExists(namespace + e["control"])), None)
        if foot and cmds.objExists(root):
            target = _pivot(root).y - _pivot(namespace + foot["control"]).y
            current = _pos(source("Hips")).y - _pos(source(foot["end"])).y
            report["scale_from"] = "root_to_ik_foot"
    if target and current and current > 1e-6 and target > 1e-6:
        ratio = target / current
        scale = cmds.getAttr(top + ".scaleX") * ratio
        cmds.setAttr(top + ".scale", scale, scale, scale)
        report["scale"] = round(ratio, 4)

    for chain in chains:
        links = [(s, namespace + c) for s, c in chain["links"] if cmds.objExists(source(s)) and cmds.objExists(namespace + c)]
        targets = list(links)
        if chain["end_aim"] and cmds.objExists(source(chain["end_aim"][0])) and cmds.objExists(namespace + chain["end_aim"][1]):
            targets.append((chain["end_aim"][0], namespace + chain["end_aim"][1]))
        for index in range(chain["align_from"], len(targets) - 1):
            (joint, control), (child, child_control) = targets[index], targets[index + 1]
            residual = _rotate_towards(source(joint), source(child), _pivot(child_control) - _pivot(control), frame)
            if residual is None:
                report["skipped"].append(joint)
            else:
                report["aligned"][joint] = round(residual, 3)
    return report


# ------------------------------------------------------------------ pole vectors
def _world(node, frame):
    return om.MMatrix(cmds.getAttr(node + ".worldMatrix[0]", time=frame))


def _translation(matrix):
    return om.MVector(matrix[12], matrix[13], matrix[14])


def _rotation(matrix):
    return om.MTransformationMatrix(matrix).asRotateMatrix()


def _key_locator(locator, frames, points):
    """Key translate X/Y/Z in one go per channel; fall back to setKeyframe if the API refuses."""
    try:
        import maya.api.OpenMayaAnim as oma

        unit = om.MTime.uiUnit()
        times = om.MTimeArray()
        for frame in frames:
            times.append(om.MTime(frame, unit))
        selection = om.MSelectionList()
        for axis, index in (("X", 0), ("Y", 1), ("Z", 2)):
            selection.clear()
            selection.add(f"{locator}.translate{axis}")
            curve = oma.MFnAnimCurve()
            curve.create(selection.getPlug(0), oma.MFnAnimCurve.kAnimCurveTL)
            curve.addKeys(times, om.MDoubleArray([p[index] for p in points]))
    except Exception:  # API differences between Maya versions
        for frame, point in zip(frames, points):
            for axis, value in zip("XYZ", (point[0], point[1], point[2])):
                cmds.setKeyframe(locator, attribute="translate" + axis, time=frame,
                                 value=om.MDistance(value).asUnits(om.MDistance.uiUnit()))
    cmds.currentTime(frames[0], edit=True, update=True)


def pole_locator(entry, namespace, frames, rig_start=None, ratio=1.0):
    """A locator keyed in front of the source's elbow/knee, for the rig's pole control to follow.

    Bent limb: on the side the mid joint bends towards. Straight limb (a T pose, a locked knee):
    the rig's own rest pole direction, carried along by the source's upper bone. The two blend
    by how bent the limb is, so the pole never jumps or flips at a straight pose.
    The rest point is back-solved through to_rig_space so it lands exactly on the rig's pole,
    leaving no constant offset to tilt the bend plane later.
    """
    start, mid, end = source(entry["start"]), source(entry["mid"]), source(entry["end"])
    pole = namespace + entry["pole"]
    rest = frames[0]
    if rig_start:
        rest_point = _translation(_world(start, rest)) + (_pivot(pole) - _pivot(rig_start)) / ratio
    else:
        rest_point = _pivot(pole)
    rest_offset = rest_point - _translation(_world(mid, rest))
    distance = max(rest_offset.length(), 1e-3)
    local = rest_offset.normal() * _rotation(_world(start, rest)).inverse()
    points = []
    for frame in frames:
        m_start = _world(start, frame)
        ps, pm, pe = _translation(m_start), _translation(_world(mid, frame)), _translation(_world(end, frame))
        length = (pm - ps).length() + (pe - pm).length()
        line = pe - ps
        along = ((pm - ps) * line) / (line * line) if line.length() > 1e-6 else 0.0
        bend = pm - (ps + line * along)
        carried = (local * _rotation(m_start)).normal()
        weight = min(1.0, max(0.0, (bend.length() / length - 0.03) / 0.07)) if length > 1e-6 else 0.0
        direction = carried * (1.0 - weight) + (bend.normal() * weight if bend.length() > 1e-6 else carried * weight)
        if direction.length() < 1e-6:
            direction = carried
        point = pm + direction.normal() * distance
        points.append((point.x, point.y, point.z))
    locator = cmds.spaceLocator(name=f"{SOURCE_NS}:pole_{entry['name']}")[0]
    _key_locator(locator, frames, points)
    return locator


def _rig_start(profile, namespace, entry):
    """The rig control at the limb's root (upper arm / thigh), if the FK chain is mapped."""
    for chain in expand(profile)[1]:
        for joint, control in chain["links"]:
            if joint == entry["start"] and cmds.objExists(namespace + control):
                return namespace + control
    return None


def limb_ratio(entry, rig_start, control):
    """Rig root-to-end distance over the source's, at rest."""
    start, end = source(entry["start"]), source(entry["end"])
    source_length = (_pos(end) - _pos(start)).length()
    anchor = _pivot(rig_start) if rig_start else _pos(start)
    return (_pivot(control) - anchor).length() / source_length if source_length > 1e-6 else 1.0


def to_rig_space(point, entry, rig_start, ratio, label):
    """A transform at rig limb root + (source point - source limb root) * ratio, evaluated live.

    The IK end and the pole both go through this, so the hand/foot keeps the source's reach for
    the rig's own limb length wherever the rig's shoulder/hip is, and the pole stays in the plane
    the rig's limb actually bends in. (A fixed offset in the source hand's frame would swing
    around whenever the hand turns.)
    """
    start = source(entry["start"])
    name = f"{SOURCE_NS}:{label}_{entry['name']}"
    root = cmds.createNode("transform", name=name, skipSelect=True)
    if rig_start:
        cmds.pointConstraint(rig_start, root, maintainOffset=False)
    else:
        cmds.pointConstraint(start, root, maintainOffset=True)
    tip = cmds.createNode("transform", name=name + "_target", parent=root, skipSelect=True)
    a = cmds.createNode("decomposeMatrix", name=name + "_root_dm", skipSelect=True)
    b = cmds.createNode("decomposeMatrix", name=name + "_point_dm", skipSelect=True)
    cmds.connectAttr(start + ".worldMatrix[0]", a + ".inputMatrix")
    cmds.connectAttr(point + ".worldMatrix[0]", b + ".inputMatrix")
    difference = cmds.createNode("plusMinusAverage", name=name + "_vec", skipSelect=True)
    cmds.setAttr(difference + ".operation", 2)
    cmds.connectAttr(b + ".outputTranslate", difference + ".input3D[0]")
    cmds.connectAttr(a + ".outputTranslate", difference + ".input3D[1]")
    scale = cmds.createNode("multiplyDivide", name=name + "_ratio", skipSelect=True)
    cmds.setAttr(scale + ".input2", ratio, ratio, ratio)
    cmds.connectAttr(difference + ".output3D", scale + ".input1")
    cmds.connectAttr(scale + ".output", tip + ".translate")
    return tip


# ------------------------------------------------------------------ connect / bake
def connect(profile, namespace, frames=None):
    root, chains, _ = expand(profile)
    made = []

    def tag(constraint):
        cmds.addAttr(constraint, longName=TAG, attributeType="bool", defaultValue=1, keyable=False)
        made.append(constraint)

    control = namespace + root["control"]
    if not cmds.objExists(control):
        raise ApplyError(f"找不到 root 控制器 {control}。")
    # Record the channels to bake before constraining: afterwards they are connected.
    plugs = _channels(control, "parent")
    tag(cmds.parentConstraint(source(root["source"]), control, maintainOffset=True,
                              skipTranslate=_skip(control, "translate"), skipRotate=_skip(control, "rotate"))[0])
    kinds, skipped = {control: "parent"}, []
    for chain in chains:
        for joint, name in chain["links"]:
            control = namespace + name
            if control in kinds or not (cmds.objExists(control) and cmds.objExists(source(joint))):
                continue
            skip = _skip(control, "rotate")
            if skip != "none" and len(skip) == 3:
                skipped.append(control)
                continue
            plugs += _channels(control, "orient")
            tag(cmds.orientConstraint(source(joint), control, maintainOffset=True, skip=skip)[0])
            kinds[control] = "orient"

    for entry in expand_ik(profile):
        if not all(cmds.objExists(source(entry[k])) for k in ("start", "mid", "end")):
            continue
        rig_start = _rig_start(profile, namespace, entry)
        control = namespace + entry["control"] if entry["control"] else None
        ratio = limb_ratio(entry, rig_start, control) if control and cmds.objExists(control) else 1.0
        if control and cmds.objExists(control) and control not in kinds:
            skip_t, skip_r = _skip(control, "translate"), _skip(control, "rotate")
            locked_t = skip_t != "none" and len(skip_t) == 3
            locked_r = skip_r != "none" and len(skip_r) == 3
            if locked_t and locked_r:
                skipped.append(control)
            else:
                plugs += _channels(control, "parent")
                if not locked_t:
                    tip = to_rig_space(source(entry["end"]), entry, rig_start, ratio, "reach")
                    tag(cmds.pointConstraint(tip, control, maintainOffset=True, skip=skip_t)[0])
                if not locked_r:
                    tag(cmds.orientConstraint(source(entry["end"]), control, maintainOffset=True, skip=skip_r)[0])
                kinds[control] = "parent"
        pole = namespace + entry["pole"] if entry["pole"] else None
        if pole and cmds.objExists(pole) and pole not in kinds and frames:
            skip_t = _skip(pole, "translate")
            if skip_t != "none" and len(skip_t) == 3:
                skipped.append(pole)
            else:
                locator = pole_locator(entry, namespace, frames, rig_start, ratio)
                target = to_rig_space(locator, entry, rig_start, ratio, "pole_target")
                plugs += _channels(pole, "point")
                tag(cmds.pointConstraint(target, pole, maintainOffset=not rig_start, skip=skip_t)[0])
                kinds[pole] = "point"
        if entry["toe"]:
            joint, toe = entry["toe"][0], namespace + entry["toe"][1]
            if cmds.objExists(toe) and cmds.objExists(source(joint)) and toe not in kinds:
                skip = _skip(toe, "rotate")
                if skip != "none" and len(skip) == 3:
                    skipped.append(toe)
                else:
                    plugs += _channels(toe, "orient")
                    tag(cmds.orientConstraint(source(joint), toe, maintainOffset=True, skip=skip)[0])
                    kinds[toe] = "orient"
    return made, kinds, skipped, plugs


def _constraints():
    return [c for c in cmds.ls(type="constraint") if cmds.attributeQuery(TAG, node=c, exists=True)]


def _remove_new_keys(controls, curves_before):
    controls = set(controls)
    ours = [curve for curve in set(cmds.ls(type="animCurve")) - curves_before
            if set(cmds.listConnections(curve, s=False, d=True, skipConversionNodes=True) or []) & controls]
    if ours:
        cmds.delete(ours)
    return len(ours)


def key_report(controls):
    times = []
    curves = set()
    for control in controls:
        for curve in cmds.listConnections(control, s=True, d=False, type="animCurve") or []:
            if cmds.nodeType(curve).startswith("animCurveT"):
                curves.add(curve)
    for curve in curves:
        times.extend(cmds.keyframe(curve, q=True, timeChange=True) or [])
    return {"curves": len(curves), "keys": len(times),
            "fractional_keys": sum(1 for t in times if abs(t - round(t)) > 1e-4),
            "first": min(times) if times else None, "last": max(times) if times else None}


def apply(main_namespace, profile, fbx, target_unit=None, log=print, on_connected=None, on_baked=None):
    """Retarget ``fbx`` (FlowHMR ``flowhmr`` profile, T pose at frame -1) onto the rig in
    ``main_namespace`` ("" or "Ns:") using ``profile``. Returns a summary dict."""
    started = time.time()
    if isinstance(profile, (str, Path)):
        profile = load_profile(profile)
    namespace = main_namespace
    controls = mapped_controls(profile, namespace)
    if not controls:
        raise ApplyError("設定檔裡的控制器在場景中都找不到。")
    animated = _animated(controls)
    if animated:
        raise ApplyError(f"有 {len(animated)} 個對應的控制器已經有動畫（例如 {', '.join(animated[:3])}）。")
    _, _, hold = expand(profile)
    hold.update(switch_holds(profile))  # each limb's FK/IK switch, set to the chosen mode
    rest_plugs = [p for c in controls for p in _channels(c, "parent")] if profile.get("rest", "zero") == "zero" else []
    saved = _remember(rest_plugs + [namespace + plug for plug in hold])
    timing = timing_mod.Timing()
    target = target_unit or timing.unit
    curves_before = set(cmds.ls(type="animCurve"))
    log(f"通用引擎：{profile.get('name', '?')} → {namespace or '(無 namespace)'}，{len(controls)} 個控制器")
    try:
        timing.prepare()
        top = import_source(fbx)
        timing.imported()
        source_keys = cmds.keyframe(source("Hips") + ".rotateX", q=True, timeChange=True) or []
        if not source_keys:
            raise ApplyError("FlowHMR FBX 沒有動作 key。")
        start, end = min(source_keys), max(source_keys)
        cmds.playbackOptions(minTime=start, maxTime=end, animationStartTime=start, animationEndTime=end)
        cmds.currentTime(start, edit=True)
        for plug, value in hold.items():
            if _settable(namespace + plug):
                cmds.setAttr(namespace + plug, value)
        for plug in rest_plugs:
            cmds.setAttr(plug, 0)
        calibration = calibrate(profile, namespace, top, frame=start)
        log(f"校正：縮放 {calibration['scale']}，對齊 {len(calibration['aligned'])} 段骨頭")
        frames = list(range(int(round(start)), int(round(end)) + 1))
        constraints, kinds, skipped, plugs = connect(profile, namespace, frames)
        log(f"已接上 {len(constraints)} 個控制器")
        if skipped:
            log(f"略過 {len(skipped)} 個已被 rig 內部驅動或全部鎖住的控制器：{', '.join(c.split(':')[-1] for c in skipped)}")
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
        remove_source()
        curves = sorted({c for control in kinds for c in cmds.listConnections(control, s=True, d=False, type="animCurveTA") or []})
        flips = count_flips(curves)
        if curves:
            cmds.filterCurve(curves, filter="euler")
        log(f"烘焙完成（{time.time() - bake_started:.1f} 秒），修正 {flips} 條 euler 翻轉曲線")
        if on_baked:
            on_baked(namespace)
        timing.finish(target)
        keys = key_report(list(kinds))
        cmds.currentTime(0, edit=True)
        return {"namespace": namespace, "profile": profile.get("name"), "driven_controls": len(kinds),
                "skipped_controls": [c.split(":")[-1] for c in skipped],
                "calibration": calibration, "source_frames": len(source_keys), "bake_range_30fps": (start, end),
                "euler_flips_fixed": flips, "keys": keys, "time_unit": cmds.currentUnit(q=True, time=True),
                "fps": timing_mod.scene_fps(), "seconds": round(time.time() - started, 2)}
    except Exception as error:
        problems = []
        for step in (lambda: cmds.delete(_constraints()) if _constraints() else None, remove_source,
                     lambda: _remove_new_keys(controls, curves_before), lambda: _put_back(saved), timing.abort):
            try:
                step()
            except Exception as cleanup_error:
                problems.append(str(cleanup_error))
        log(f"通用引擎失敗，已清理：{error}" + (f"（清理問題：{'；'.join(problems)}）" if problems else ""))
        if isinstance(error, ApplyError):
            raise
        raise ApplyError(str(error)) from error
    finally:
        cmds.flushUndo()
