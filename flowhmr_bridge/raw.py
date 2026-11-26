"""Build world-space ADV control targets from the original SMPL-H preview stream."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path

import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma
import maya.cmds as cmds

from . import character, layers
from .adv import ApplyError


def _foot_geometry(ankles, index):
    """Rest vertices under foot influences, in the triangle-corner order used by FBX geometry."""
    feet = [path for path in index if any(path == ankle or path.startswith(ankle + "|") for ankle in ankles)]
    clusters = {}
    for path in feet:
        for cluster in cmds.listConnections(path + ".worldMatrix[0]", s=False, d=True, type="skinCluster") or []:
            clusters.setdefault(cluster, set()).add(path)
    geometry = {"positions": [], "joints": [], "weights": [], "corners": []}
    for cluster, influences in clusters.items():
        selection = om.MSelectionList()
        selection.add(cluster)
        skin = oma.MFnSkinCluster(selection.getDependNode(0))
        bones = skin.influenceObjects()
        ids = [index.get(b.fullPathName(), -1) for b in bones]
        vertices = {}
        for bone in bones:
            if bone.fullPathName() not in influences:
                continue
            selected, _ = skin.getPointsAffectedByInfluence(bone)
            for i in range(selected.length()):
                mesh, component = selected.getComponent(i)
                if mesh.hasFn(om.MFn.kMesh) and mesh.isVisible():
                    vertices.setdefault(mesh.fullPathName(), set()).update(om.MFnSingleIndexedComponent(component).getElements())
        for path, vertex_ids in vertices.items():
            selection.clear()
            selection.add(path)
            dag = selection.getDagPath(0)
            mesh = om.MFnMesh(dag)
            points = mesh.getPoints(om.MSpace.kWorld)
            chosen = sorted(vertex_ids)
            component = om.MFnSingleIndexedComponent()
            verts = component.create(om.MFn.kMeshVertComponent)
            component.addElements(chosen)
            weights, count = skin.getWeights(dag, verts)
            lookup = {}
            for row, vertex in enumerate(chosen):
                weighted = [(w, ids[i]) for i, w in enumerate(weights[row * count:(row + 1) * count]) if w > 0]
                top = sorted(weighted, key=lambda pair: -pair[0])[:4]
                total = sum(w for w, _ in top)
                if not total or any(b < 0 for _, b in top):
                    continue
                top += [(0, 0)] * (4 - len(top))
                lookup[vertex] = len(geometry["positions"]) // 3
                point = points[vertex]
                geometry["positions"].extend((point.x / 100, point.y / 100, point.z / 100))
                geometry["joints"].extend(b for _, b in top)
                geometry["weights"].extend(w / total for w, _ in top)
            geometry["corners"].extend(lookup[v] for v in mesh.getTriangles()[1] if v in lookup)
    return geometry if geometry["corners"] else None


def _scene_skeleton(namespace):
    """ADV's 52 SMPL-H slots. Keep intermediate Part bones and the pelvis ancestors in FK."""
    slots = ["Root_M", "Hip_L", "Hip_R", "Spine1_M", "Knee_L", "Knee_R", "Spine2_M",
             "Ankle_L", "Ankle_R", "Chest_M", "Toes_L", "Toes_R", "Neck_M",
             "Scapula_L", "Scapula_R", "Head_M", "Shoulder_L", "Shoulder_R",
             "Elbow_L", "Elbow_R", "Wrist_L", "Wrist_R"]
    slots.extend(f"{digit}Finger{part}_{side}" for side in ("L", "R")
                 for digit in ("Index", "Middle", "Pinky", "Ring", "Thumb") for part in (1, 2, 3))
    if not cmds.objExists(namespace + slots[6]):
        parts = cmds.ls(namespace + "Spine1Part*_M", type="joint") or []
        candidates = []
        for part in parts:
            name = character._short(part)
            match = re.fullmatch(r"Spine1Part(\d+)_M", name)
            if match and cmds.objExists(namespace + "FK" + name):
                candidates.append((int(match[1]), name))
        if candidates:
            slots[6] = max(candidates)[1]
    paths = []
    ancestors = set()
    for name in slots:
        matches = cmds.ls(namespace + name, type="joint", long=True) or []
        if len(matches) > 1:
            raise ApplyError(f"ADV 關節名稱重複：{name}")
        path = matches[0] if matches else None
        paths.append(path)
        while path and path not in ancestors:
            if cmds.nodeType(path) == "joint":
                ancestors.add(path)
            path = path.rpartition("|")[0]
    ankles = [path for path in paths[7:9] if path]
    for ankle in ankles:
        ancestors.update(cmds.listRelatives(ankle, allDescendents=True, type="joint", fullPath=True) or [])
    ordered = sorted(ancestors, key=lambda path: (path.count("|"), path))
    index = {path: i for i, path in enumerate(ordered)}
    bones = []
    for path in ordered:
        matrix = list(cmds.getAttr(path + ".worldMatrix[0]"))
        matrix[12:15] = [v / 100.0 for v in matrix[12:15]]
        parent = path.rpartition("|")[0]
        while parent and parent not in index:
            parent = parent.rpartition("|")[0]
        bones.append(dict(name=character._short(path), parent=index.get(parent, -1), matrix=matrix))
    mapping = [index.get(path, -1) for path in paths]
    carrier = mapping[0]
    if carrier < 0:
        raise ApplyError("找不到 ADV 骨盆 Root_M。")
    while bones[carrier]["parent"] >= 0:
        carrier = bones[carrier]["parent"]
    return dict(bones=bones, map=mapping, carrier=carrier, feet=_foot_geometry(ankles, index))


def _solve(source, skeleton, helper_names):
    static = Path(source["root"]) / "tools/web/static"
    if not (static / "js/fbx-character-retarget.js").is_file():
        raise ApplyError("FlowHMR 資料夾內找不到角色預覽求解器，請檢查面板上方的路徑。")
    node = shutil.which("node")
    if not node:
        raise ApplyError("原始動作模式需要 Node.js 22.15 以上，請確認 Maya 的 PATH 可以找到 node。")
    with tempfile.TemporaryDirectory(prefix="flowhmr_raw_") as tmp:
        directory = Path(tmp)
        config = dict(meta=source["meta"], modelName=source.get("model_name"),
                      offsets=source["offsets"], skeleton=skeleton, helperNames=helper_names)
        (directory / "input.json").write_text(json.dumps(config), encoding="utf-8")
        (directory / "motion.bin").write_bytes(source["data"])
        if source["model"] is not None:
            (directory / "bind.fbx").write_bytes(source["model"])
        run = subprocess.run([node, str(Path(__file__).with_name("preview_solve.mjs")), str(static.resolve()), tmp],
                             capture_output=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if run.returncode:
            detail = run.stderr.decode("utf-8", errors="replace")
            raise ApplyError("FlowHMR 原始動作求解失敗：" + detail[-2200:])
        tracks = json.loads((directory / "tracks.json").read_text(encoding="utf-8"))
        samples = array("f")
        with (directory / "samples.bin").open("rb") as handle:
            samples.fromfile(handle, len(tracks["records"]) * source["meta"]["frames"] * 7)
        return tracks, samples


def _check_target(namespace, tracks, pelvis_name=character.PELVIS):
    """Verify which skin skeleton the saved mapping and bind offsets belong to. ``pelvis_name``
    None accepts the solver's own pelvis (any rig); ADV insists on Root_M."""
    if pelvis_name and tracks["pelvis"] != pelvis_name:
        raise ApplyError(f"預覽角色的骨盆 {tracks['pelvis']} 和 ADV 的 {pelvis_name} 不同。")
    pelvis_name = tracks["pelvis"]
    if len(cmds.ls(namespace + pelvis_name, type="joint") or []) != 1:
        raise ApplyError(f"rig 找不到唯一的骨盆關節 {pelvis_name}。")
    names = {r["name"] for r in tracks["records"]}
    # A rigid placement of the entire Maya character does not change its bind skeleton.
    pelvis = cmds.getAttr(namespace + pelvis_name + ".worldMatrix[0]")
    base = next(r["rest"] for r in tracks["records"] if r["name"] == pelvis_name)
    rig_origin, rig_inverse = om.MVector(*pelvis[12:15]), character._rotation(pelvis).inverse()
    src_origin, src_inverse = om.MVector(*base[:3]), om.MQuaternion(*base[3:7]).inverse()
    for record in tracks["records"]:
        name = record["name"]
        found = cmds.ls(namespace + name, long=True, type="joint") or []
        if len(found) != 1:
            raise ApplyError(f"rig 找不到唯一的預覽關節 {name}。")
        path = found[0].rpartition("|")[0]
        while path and character._short(path) not in names:
            path = path.rpartition("|")[0]
        if (character._short(path) or None) != record["parent"]:
            raise ApplyError(f"rig 關節 {name} 的父骨與預覽骨架不同。")
        matrix = cmds.getAttr(found[0] + ".worldMatrix[0]")
        rest = record["rest"]
        rig_position = (om.MVector(*matrix[12:15]) - rig_origin).rotateBy(rig_inverse)
        src_position = (om.MVector(*rest[:3]) - src_origin).rotateBy(src_inverse)
        distance = (rig_position - src_position).length()
        angle = character._angle(character._rotation(matrix) * rig_inverse, om.MQuaternion(*rest[3:7]) * src_inverse)
        if distance > 0.1 or angle > 0.2:
            raise ApplyError(f"{name} 的 build pose 和預覽角色相差 {distance:.2f} cm、{angle:.2f}°。"
                             "請確認使用相同的蒙皮骨架、骨長及 FBX 綁定軸向。")


def _key_targets(records, samples, frame_count):
    """Independent world-space transforms. Maya animation API uses radians and centimetres."""
    times = om.MTimeArray()
    for frame in range(frame_count):
        times.append(om.MTime(frame, om.MTime.uiUnit()))
    joints, moving = {}, {}
    for i, record in enumerate(records):
        name = record["name"]
        node = cmds.createNode("transform", name=character.char(f"target_{i}"), skipSelect=True)
        joints[name] = node
        if not record["auxiliary"]:
            moving[name] = (True, record["translates"])
        values = [[] for _ in range(6)]
        previous = None
        for frame in range(frame_count):
            at = 7 * (i * frame_count + frame)
            rotation = om.MQuaternion(*samples[at + 3:at + 7]).asEulerRotation()
            if previous is not None:
                rotation = rotation.closestSolution(previous)
            previous = rotation
            for axis, v in zip(values, (*samples[at:at + 3], rotation.x, rotation.y, rotation.z)):
                axis.append(v)
        selection = om.MSelectionList()
        selection.add(node)
        fn = om.MFnDependencyNode(selection.getDependNode(0))
        for attr, axis in zip(("tx", "ty", "tz", "rx", "ry", "rz"), values):
            curve = oma.MFnAnimCurve()
            curve.create(fn.findPlug(attr, False), curve.kAnimCurveTL if attr[0] == "t" else curve.kAnimCurveTA)
            curve.addKeys(times, om.MDoubleArray(axis), curve.kTangentLinear, curve.kTangentLinear)
    return joints, moving


def spine_holds(namespace):
    """Use the ADV bias endpoint that puts the full FK rotation on the main spine/neck joint."""
    holds = {}
    for control in cmds.sets(namespace + "ControlSet", q=True) or []:
        if not character._short(control).startswith("FK") or not cmds.objExists(control + ".bias"):
            continue
        base, sep, side = control.rpartition("_")
        ramp = f"{base}StartBiasRV_{side}"
        plug = control + ".bias"
        if not sep or not cmds.objExists(ramp + ".inputValue") or cmds.getAttr(plug, lock=True):
            continue
        if cmds.connectionInfo(plug, isDestination=True) and not layers.animation_source(plug):
            continue
        convert = cmds.listConnections(ramp + ".inputValue", s=True, d=False, type="unitConversion") or []
        if not convert or cmds.connectionInfo(convert[0] + ".input", sourceFromDestination=True) != plug:
            continue
        scale = cmds.getAttr(convert[0] + ".conversionFactor")
        if scale:
            holds[plug] = cmds.getAttr(ramp + ".inputMax") / scale
    return holds


def neck_twist_weights(namespace):
    """ADV can also twist the skin constraint *after* the neck Part FK control.

    Hold its static multiplier on the override layer when that Part receives a
    complete preview orientation. Verify the connection before including it.
    """
    found = {}
    for joint in cmds.ls(namespace + "NeckPart*_M", type="joint") or []:
        name = character._short(joint)
        multiplier = namespace + "twistAmountDivide" + name
        weight = multiplier + ".input2"
        if not cmds.objExists(weight) or cmds.nodeType(multiplier) != "multDoubleLinear":
            continue
        if cmds.getAttr(weight, lock=True):
            continue
        if cmds.connectionInfo(weight, isDestination=True) and not layers.animation_source(weight):
            continue
        constraints = cmds.listConnections(joint + ".rx", s=True, d=False, type="orientConstraint") or []
        offsets = cmds.listConnections(multiplier + ".output", s=False, d=True, plugs=True,
                                       skipConversionNodes=True) or []
        if any(c + ".offsetX" in offsets for c in constraints):
            found[name] = weight
    return found


def targets(namespace, source, log):
    saved = source["model"] is not None
    skeleton = None if saved else _scene_skeleton(namespace)
    if saved:
        log("套用預覽已儲存的角色對應與足底蒙皮：" + source["model_name"])
    else:
        sole = "場景中的足部蒙皮" if skeleton["feet"] else "求解器估計的鞋底"
        log(f"以原始動作、ADV build pose 和{sole}求解。")
    # ADV's Micro controllers sit after the FK and bend/twist systems. They can
    # put the residual twist back on the exact skin joints represented in the preview.
    micro, fk_parts = {}, {}
    for control in cmds.sets(namespace + "ControlSet", q=True) or []:
        name = character._short(control)
        if name.startswith("Micro") and cmds.objExists(namespace + name[5:] + ".twistAmount"):
            micro[name[5:]] = control
        elif re.fullmatch(r"FK\w+Part\d+_\w+", name) and cmds.objExists(namespace + name[2:]):
            fk_parts[name[2:]] = control
    tracks, samples = _solve(source, skeleton, sorted(micro.keys() | fk_parts.keys()))
    try:
        _check_target(namespace, tracks)
    except ApplyError as error:
        if not saved:
            raise
        raise ApplyError(f"{error} HMR 為這個人物儲存的預覽角色（{source['model_name']}）不是這個 rig 的蒙皮骨架。"
                         "請在 HMR 改用這個 rig 的蒙皮 FBX 並儲存，或取消勾選「使用 HMR 已儲存的角色設定」"
                         "（HMR 的校正值就不會套用）。") from None
    joints, moving = _key_targets(tracks["records"], samples, source["meta"]["frames"])
    correctives = [(name, micro[name], "orient") for name in joints if name in micro]
    correctives += [(name, fk_parts[name], "orient") for name in joints if name not in moving and name in fk_parts]
    return joints, moving, correctives
