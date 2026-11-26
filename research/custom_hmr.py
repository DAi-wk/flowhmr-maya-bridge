"""Research: how close can a custom mapping profile get to FlowHMR's character preview?

Runs in a disposable mayapy process. The rig scene is referenced, never saved.

    mayapy research/custom_hmr.py --scene RIG.ma --namespace R --profile PROFILE.json \
        --source CACHE_DIR --out RESULT.json [--holds H.json] [--followers F.json] [--refine N]

1. Build pose: the profile's own rest rule ("zero": mapped controls at 0) and hold values.
2. Probe: turn each mapped control a few degrees per axis and record which skin joints respond
   (local rotation change / control turn) and whether they follow the control rigidly.
3. Map: each profile slot -> the skin joint its control drives most (from the probe). These are
   the 52 SMPL-H slots handed to FlowHMR's own solver, with the rig's skin skeleton at rest.
4. Solve with FlowHMR's createFbxCharacterRetarget (raw._solve), key world targets for every
   skeleton bone (unmapped bones keep their rest local transform, as the solver does).
5. Drive each control from its joint's target (follower trick, character.connect), bake, and
   measure every skeleton bone against the targets.

Only generic knowledge is used: the profile, the probe and optional profile-style additions
(--holds, --followers). Nothing reads ADV- or Hive-specific names.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from array import array
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# FlowHMR joint names in SMPL-H slot order (the flowhmr FBX profile uses these names).
SMPLH = ["Hips", "LeftUpLeg", "RightUpLeg", "Spine", "LeftLeg", "RightLeg", "Spine1", "LeftFoot", "RightFoot",
         "Spine2", "LeftToeBase", "RightToeBase", "Neck", "LeftShoulder", "RightShoulder", "Head",
         "LeftArm", "RightArm", "LeftForeArm", "RightForeArm", "LeftHand", "RightHand"]
for _side in ("Left", "Right"):
    for _digit in ("Index", "Middle", "Pinky", "Ring", "Thumb"):
        SMPLH += [f"{_side}Hand{_digit}{i}" for i in (1, 2, 3)]
SLOT = {name: i for i, name in enumerate(SMPLH)}
STEP = 5.0


def main(args):
    import maya.api.OpenMaya as om
    import maya.api.OpenMayaAnim as oma
    import maya.cmds as cmds
    from flowhmr_bridge import character, generic, mapping, raw

    def world(node):
        return om.MMatrix(cmds.getAttr(node + ".worldMatrix[0]"))

    def quat(matrix):
        return om.MTransformationMatrix(matrix).rotation(asQuaternion=True)

    def angle(a, b):
        d = a.inverse() * b
        return math.degrees(2 * math.acos(min(1.0, abs(d.w))))

    def short(node):
        return node.split("|")[-1].split(":")[-1]

    started = time.time()
    cmds.file(str(args.scene.resolve()), reference=True, namespace=args.namespace, prompt=False)
    cmds.currentUnit(time="ntsc")
    ns = args.namespace + ":"
    profile = generic.load_profile(args.profile)
    extra_holds = json.loads(args.holds.read_text(encoding="utf-8")) if args.holds else {}
    extra_followers = json.loads(args.followers.read_text(encoding="utf-8")) if args.followers else []

    # ---------------------------------------------------------------- build pose
    controls = generic.mapped_controls(profile, ns)
    kinds = generic.control_kinds(profile)
    root_control = ns + generic.expand(profile)[0]["control"]
    cmds.currentTime(0, update=True)
    if profile.get("rest", "zero") == "zero":
        for control in controls:
            for plug in generic._plugs(control, "parent"):
                if generic._free(plug):
                    cmds.setAttr(plug, 0)
    holds = dict(generic.expand(profile)[2])
    holds.update(mapping.switch_holds(profile))
    holds.update(extra_holds)
    holds = {ns + plug: value for plug, value in holds.items()}
    for plug, value in holds.items():
        if generic._settable(plug):
            cmds.setAttr(plug, value)
    cmds.currentTime(0, update=True)

    # ---------------------------------------------------------------- skin joints
    skin = set()
    for cluster in cmds.ls(ns + "*", type="skinCluster") or []:
        for influence in cmds.skinCluster(cluster, q=True, influence=True) or []:
            path = (cmds.ls(influence, long=True) or [None])[0]
            if path and cmds.nodeType(path) == "joint":
                skin.add(path)
    # The deformation skeleton: the largest joint-only tree among the skin influences. Helper
    # hierarchies (ribbons, bendy limbs, face rigs under groups) follow it and are left out, as a
    # skinned FBX character's extra bones are by FlowHMR's solver.
    def tree_top(path):
        top = path
        parent = path.rpartition("|")[0]
        while parent and cmds.nodeType(parent) == "joint":
            top, parent = parent, parent.rpartition("|")[0]
        return top

    trees = {}
    for path in skin:
        trees.setdefault(tree_top(path), []).append(path)
    main_top = max(trees, key=lambda t: len(trees[t]))
    # Every joint of that tree, skinned or not (ADV's spine Part joints carry no weights).
    skin = [main_top] + (cmds.listRelatives(main_top, allDescendents=True, type="joint", fullPath=True) or [])
    skin = sorted(skin, key=lambda p: (p.count("|"), p))
    skin_set = set(skin)
    parent_of = {p: (p.rpartition("|")[0] if p != main_top else None) for p in skin}
    # How far the rest pose is from the skin bind pose (bindPreMatrix), per joint.
    bind_gap = {}
    for cluster in cmds.ls(ns + "*", type="skinCluster") or []:
        for i in cmds.getAttr(cluster + ".matrix", multiIndices=True) or []:
            src = cmds.listConnections(f"{cluster}.matrix[{i}]", s=True, d=False) or []
            if not src:
                continue
            path = (cmds.ls(src[0], long=True) or [None])[0]
            if path not in skin_set:
                continue
            bind = om.MMatrix(cmds.getAttr(f"{cluster}.bindPreMatrix[{i}]")).inverse()
            now = world(path)
            gap = (angle(quat(bind), quat(now)), math.dist([bind[12], bind[13], bind[14]], [now[12], now[13], now[14]]))
            old = bind_gap.get(short(path), (0, 0))
            bind_gap[short(path)] = (max(old[0], gap[0]), max(old[1], gap[1]))

    # ---------------------------------------------------------------- probe
    def locals_now():
        worlds = {p: world(p) for p in skin}
        for p in skin:
            if parent_of[p] and parent_of[p] not in worlds:
                worlds[parent_of[p]] = world(parent_of[p])
        return worlds, {p: quat(worlds[p] * worlds[parent_of[p]].inverse()) if parent_of[p] else quat(worlds[p])
                        for p in skin}

    base_world, base_local = locals_now()
    probe = {}
    for control in controls:
        entry = {"kind": kinds.get(control[len(ns):]), "axes": {}}
        c0 = quat(world(control))
        for axis in "XYZ":
            plug = f"{control}.rotate{axis}"
            if not generic._free(plug):
                continue
            value = cmds.getAttr(plug)
            cmds.setAttr(plug, value + STEP)
            c1 = quat(world(control))
            worlds, local = locals_now()
            cmds.setAttr(plug, value)
            turn = c0.inverse() * c1
            responses = []
            for p in skin:
                local_change = angle(base_local[p], local[p])
                if local_change < 0.01:
                    continue
                joint_turn = quat(base_world[p]).inverse() * quat(worlds[p])
                responses.append([short(p), round(local_change / STEP, 4), round(angle(turn, joint_turn), 3),
                                  p.count("|"), round(angle(quat(base_world[p]), quat(worlds[p])) / STEP, 4)])
            entry["axes"][axis] = responses
        cmds.currentTime(0, update=True)
        # Primary joint: the joint nearest the root that turns in world space with the control on
        # at least two of the probed axes (one axis alone is a twist or bend share handed down
        # the chain; a joint that counter-rotates to stay put, like the legs under a hip-swing
        # control, does not turn at all). Ties go to the larger local share.
        axes_hit, score, depth = {}, {}, {}
        for responses in entry["axes"].values():
            for name, ratio, _, d, world_ratio in responses:
                if world_ratio < 0.3:
                    continue
                axes_hit[name] = axes_hit.get(name, 0) + 1
                score[name] = score.get(name, 0) + ratio
                depth[name] = d
        need = min(2, len(entry["axes"]))
        score = {n: s for n, s in score.items() if axes_hit[n] >= need}
        entry["primary"] = min(score, key=lambda n: (depth[n], -score[n])) if score else None
        probe[short(control)] = entry
    probe_seconds = round(time.time() - started, 1)

    result = {"profile": profile.get("name"), "controls": len(controls), "skin_joints": len(skin),
              "bind_gap_max": max(bind_gap.values(), default=(0, 0)), "probe": probe, "probe_seconds": probe_seconds}
    tree_index = {p: i for i, p in enumerate(skin)}
    result["tree"] = [dict(name=short(p), parent=tree_index.get(p.rpartition("|")[0], -1),
                           position=[v / 100.0 for v in cmds.xform(p, q=True, ws=True, translation=True)])
                      for p in skin]
    if args.probe_only:
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return

    # ---------------------------------------------------------------- slot map
    by_name = {short(p): p for p in skin}
    root, chains, _ = generic.expand(profile)
    slot_joint = {}
    pairs = []  # (joint short name, control, kind)
    primary_root = probe.get(short(root_control), {}).get("primary")
    if primary_root:
        slot_joint[0] = primary_root
        pairs.append((primary_root, root_control, "parent"))
    for chain in chains:
        for source, control_name in chain["links"]:
            control = ns + control_name
            info = probe.get(control_name)
            if not info or not info["primary"]:
                continue
            slot = SLOT.get(source)
            if slot is not None and slot not in slot_joint:
                slot_joint[slot] = info["primary"]
            pairs.append((info["primary"], control, "orient"))
    if args.joints:
        # Slot -> skin joint chosen outside the probe (FlowHMR's own suggestion, or by hand).
        for source, joint in json.loads(args.joints.read_text(encoding="utf-8")).items():
            if source in SLOT:
                if joint and joint in by_name:
                    slot_joint[SLOT[source]] = joint
                else:
                    slot_joint.pop(SLOT[source], None)
    for joint, control_name, *kind in extra_followers:
        if cmds.objExists(ns + control_name) and cmds.objExists(ns + joint):
            pairs.append((joint, ns + control_name, kind[0] if kind else "orient"))
    result["slot_map"] = {SMPLH[i]: slot_joint.get(i) for i in range(len(SMPLH))}
    result["pairs"] = [[j, short(c), k] for j, c, k in pairs]
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    seen = {}
    for slot, joint in sorted(slot_joint.items()):
        if joint in seen:
            # The solver maps each bone once: keep the first slot (nearer the root in SMPL-H order).
            print(f"slot {SMPLH[slot]} -> {joint} already used by {SMPLH[seen[joint]]}; left unmapped", flush=True)
            result.setdefault("duplicate_slots", []).append([SMPLH[slot], joint, SMPLH[seen[joint]]])
        else:
            seen[joint] = slot
    paths = [by_name.get(slot_joint.get(i)) if seen.get(slot_joint.get(i)) == i else None for i in range(len(SMPLH))]

    # Skeleton for the solver, as raw._scene_skeleton builds it, from generic slot paths.
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
    index = {p: i for i, p in enumerate(ordered)}
    bones = []
    rest_world = {}
    for p in ordered:
        matrix = list(cmds.getAttr(p + ".worldMatrix[0]"))
        rest_world[short(p)] = om.MMatrix(matrix)
        matrix[12:15] = [v / 100.0 for v in matrix[12:15]]
        parent = p.rpartition("|")[0]
        while parent and parent not in index:
            parent = parent.rpartition("|")[0]
        bones.append(dict(name=short(p), parent=index.get(parent, -1), matrix=matrix))
    bone_map = [index.get(p, -1) if p else -1 for p in paths]
    carrier = bone_map[0]
    while bones[carrier]["parent"] >= 0:
        carrier = bones[carrier]["parent"]
    skeleton = dict(bones=bones, map=bone_map, carrier=carrier, feet=raw._foot_geometry(ankles, index))
    missing = [SMPLH[i] for i in (0, 1, 2, 4, 5, 7, 8, 15, 16, 17, 18, 19, 20, 21) if bone_map[i] < 0]
    if missing:
        raise SystemExit(f"required slots unmapped: {missing}")

    # ---------------------------------------------------------------- solve
    meta = json.loads((args.source / "source_meta.json").read_text())
    source = dict(root=str(args.flowhmr), meta=meta, data=(args.source / "source.bin").read_bytes(),
                  model=None, offsets=None)
    frames = meta["frames"]
    solve_started = time.time()
    tracks, samples = raw._solve(source, skeleton, [])
    result["solve_seconds"] = round(time.time() - solve_started, 1)
    records = {r["name"]: (i, r) for i, r in enumerate(tracks["records"])}

    # Expected world of every skeleton bone per frame: tracked bones from the solver, the rest
    # keep their rest local transform under their (expected) parent, as the solver's pose() does.
    expected = {b["name"]: [None] * frames for b in bones}
    for f in range(frames):
        for b in bones:
            name = b["name"]
            if name in records:
                i, _ = records[name]
                at = 7 * (i * frames + f)
                q = om.MQuaternion(*samples[at + 3:at + 7])
                m = om.MTransformationMatrix(q.asMatrix())
                m.setTranslation(om.MVector(*samples[at:at + 3]), om.MSpace.kWorld)
                expected[name][f] = m.asMatrix()
            else:
                parent = bones[b["parent"]]["name"] if b["parent"] >= 0 else None
                if parent is None:
                    expected[name][f] = rest_world[name]
                else:
                    local = rest_world[name] * rest_world[parent].inverse()
                    expected[name][f] = local * expected[parent][f]

    # Keyed world targets for every bone (unit: cm, radians).
    cmds.namespace(add=character.CHAR_NS)
    times = om.MTimeArray()
    for f in range(frames):
        times.append(om.MTime(f, om.MTime.uiUnit()))
    targets = {}
    for b in bones:
        node = cmds.createNode("transform", name=character.char("t_" + b["name"]), skipSelect=True)
        targets[b["name"]] = node
        values = [[] for _ in range(6)]
        previous = None
        for f in range(frames):
            m = om.MTransformationMatrix(expected[b["name"]][f])
            r = m.rotation()
            if previous is not None:
                r = r.closestSolution(previous)
            previous = r
            t = m.translation(om.MSpace.kWorld)
            for axis, v in zip(values, (t.x, t.y, t.z, r.x, r.y, r.z)):
                axis.append(v)
        sel = om.MSelectionList()
        sel.add(node)
        fn = om.MFnDependencyNode(sel.getDependNode(0))
        for attr, axis in zip(("tx", "ty", "tz", "rx", "ry", "rz"), values):
            curve = oma.MFnAnimCurve()
            curve.create(fn.findPlug(attr, False), curve.kAnimCurveTL if attr[0] == "t" else curve.kAnimCurveTA)
            curve.addKeys(times, om.MDoubleArray(axis), curve.kTangentLinear, curve.kTangentLinear)

    # ---------------------------------------------------------------- drive and bake
    usable = [(j, c, k) for j, c, k in pairs if j in targets]
    result["pairs"] = [[j, short(c), k] for j, c, k in usable]
    result["unpaired"] = [short(c) for j, c, k in pairs if j not in targets]
    cmds.currentTime(0, update=True)

    def refine_solve(refine, channels, probe, bones, expected, frames, ns, args):
        """Per frame, damped Gauss-Newton on the rotate channels of the controls that do not turn
        their joint 1:1, against the world targets of every bone they move (rotation in degrees,
        position in cm). Warm-started from the previous frame; each frame is keyed when solved."""
        import numpy as np

        names = {b["name"] for b in bones}
        parent = {b["name"]: (bones[b["parent"]]["name"] if b["parent"] >= 0 else None) for b in bones}
        objective = set()
        for control in refine:
            info = probe[short(control)]
            objective.add(info["primary"])
            for responses in info["axes"].values():
                objective.update(r[0] for r in responses if r[1] >= 0.01)
        objective &= names
        objective |= {n for n in names if parent[n] in objective}  # the next bone, for positions
        objective = sorted(objective)
        nodes = {n: (cmds.ls(ns + n, long=True) or [None])[0] for n in objective}

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
            for n in objective:
                a, e = world(nodes[n]), expected[n][f]
                out += rotvec(quat(e).inverse() * quat(a))
                out += [args.position_weight * (a[i] - e[i]) for i in (12, 13, 14)]
            return np.array(out)

        report = {"objective": objective, "channels": len(channels), "before": [], "after": []}
        started_refine = time.time()
        x = np.array([cmds.getAttr(plug) for plug in channels])
        report["evaluation"] = cmds.evaluationManager(q=True, mode=True)
        for f in range(frames):
            cmds.currentTime(f, update=True)
            r = residual(x, f)
            report["before"].append(float(np.abs(r).max()))
            damping = 1e-3
            for _ in range(args.refine):
                jac = np.empty((len(r), len(x)))
                for i in range(len(x)):
                    step = x.copy()
                    step[i] += 0.5
                    jac[:, i] = (residual(step, f) - r) / 0.5
                a = jac.T @ jac
                g = jac.T @ r
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
                    residual(x, f)
                    break
            residual(x, f)
            for plug, value in zip(channels, x):
                node, attr = plug.split(".")
                cmds.setKeyframe(node, attribute=attr, time=f, value=float(value))
            report["after"].append(float(np.abs(r).max()))
        report["seconds"] = round(time.time() - started_refine, 1)
        report["before_max"], report["after_max"] = max(report["before"]), max(report["after"])
        report["before"] = report["after"] = None
        return report

    def rigid(info):
        """The control turns its primary joint 1:1 on every probed axis."""
        for responses in info["axes"].values():
            mine = next((r for r in responses if r[0] == info["primary"]), None)
            if not mine or abs(mine[1] - 1) > 0.05 or mine[2] > 0.5:
                return False
        return True

    def spread(info):
        """No probed axis turns the primary joint 1:1: the control's bend is shared out over
        several joints (a spine bias, a ribbon). A control with some 1:1 axes and one shared twist
        axis keeps its follower: its world rotation is right, only the twist share differs."""
        for responses in info["axes"].values():
            mine = next((r for r in responses if r[0] == info["primary"]), None)
            if mine and abs(mine[1] - 1) <= 0.05 and mine[2] <= 0.5:
                return False
        return True

    refine = {c for j, c, k in usable if args.refine and k == "orient" and spread(probe[short(c)])}
    refine |= {ns + name for name in args.refine_also if cmds.objExists(ns + name)}
    attrs = ("rotate", "translate") if args.refine_translate else ("rotate",)
    refine_channels = [f"{c}.{attr}{a}" for c in sorted(refine) for attr in attrs for a in "XYZ"
                       if generic._free(f"{c}.{attr}{a}")]
    result["refined_controls"] = sorted(short(c) for c in refine)
    bake = lambda plugs: cmds.bakeResults(  # noqa: E731
        plugs, simulation=True, time=(0, frames - 1), sampleBy=1, disableImplicitControl=True,
        preserveOutsideKeys=False, sparseAnimCurveBake=False, removeBakedAttributeFromLayer=False,
        bakeOnOverrideLayer=False, controlPoints=False, shape=False)
    # Controls that do not turn their joint 1:1 are solved numerically instead (below), with the
    # rigid controls held on their world targets by their live constraints.
    usable_now = [(j, c, k) for j, c, k in usable if c not in refine]
    constraints, driven_kinds, skipped, plugs = character.connect(ns, targets, usable_now)
    plugs = sorted(set(plugs))
    if refine:
        result["refine"] = refine_solve(refine, refine_channels, probe, bones, expected, frames, ns, args)
        plugs = sorted(set(plugs) | set(refine_channels))
    cmds.refresh(suspend=True)
    try:
        bake(plugs)
    finally:
        cmds.refresh(suspend=False)
    cmds.delete(character._constraints())

    # ---------------------------------------------------------------- measure
    def measure(step):
        errors = {b["name"]: [0.0, 0.0, 0.0, 0.0, 0] for b in bones}  # max deg, max cm, sum deg, sum cm, n
        for f in range(0, frames, step):
            cmds.currentTime(f, update=True)
            for b in bones:
                name = b["name"]
                a = world(by_name[name]) if name in by_name else world((cmds.ls(ns + name, long=True) or [None])[0])
                e = expected[name][f]
                d = angle(quat(a), quat(e))
                c = math.dist([a[12], a[13], a[14]], [e[12], e[13], e[14]])
                row = errors[name]
                row[0], row[1] = max(row[0], d), max(row[1], c)
                row[2] += d
                row[3] += c
                row[4] += 1
        return {n: [round(r[0], 4), round(r[1], 4), round(r[2] / r[4], 4), round(r[3] / r[4], 4)]
                for n, r in errors.items()}

    result["errors"] = measure(args.step)
    result["tracked"] = sorted(records)
    if args.reference:
        # Independent check: the character FBX FlowHMR itself exported for this rig's skeleton.
        cmds.loadPlugin("fbxmaya", quiet=True)
        cmds.currentTime(0, update=True)
        reference = character.import_character(args.reference.resolve())
        names = [b["name"] for b in bones if b["name"] in reference]
        worst = {n: [0.0, 0.0] for n in names}
        for f in range(0, frames, args.step):
            cmds.currentTime(f, update=True)
            for n in names:
                a = world((cmds.ls(ns + n, long=True) or [None])[0])
                e = world(reference[n])
                worst[n][0] = max(worst[n][0], angle(quat(a), quat(e)))
                worst[n][1] = max(worst[n][1], math.dist([a[12], a[13], a[14]], [e[12], e[13], e[14]]))
        result["reference"] = {n: [round(d, 4), round(c, 4)] for n, (d, c) in worst.items()}
    result["seconds"] = round(time.time() - started, 1)
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--namespace", default="R")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="folder with source_meta.json and source.bin")
    parser.add_argument("--flowhmr", type=Path, default=REPO.parent / "FlowHMR")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holds", type=Path, help="extra hold values {plug: value}, names without namespace")
    parser.add_argument("--followers", type=Path, help="extra [[joint, control], ...] followers")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--refine", type=int, default=0, help="Gauss-Newton iterations per frame for non-1:1 controls")
    parser.add_argument("--refine-translate", action="store_true", help="also solve the translate channels")
    parser.add_argument("--refine-also", nargs="*", default=[], help="controls to solve even if they look 1:1")
    parser.add_argument("--position-weight", type=float, default=1.0, help="residual weight of 1 cm against 1 degree")
    parser.add_argument("--joints", type=Path, help="{FlowHMR joint name: skin joint} overriding the probe's slot map")
    parser.add_argument("--reference", type=Path, help="FlowHMR's own character FBX export for this rig, to compare")
    parser.add_argument("--step", type=int, default=1, help="measure every Nth frame")
    args = parser.parse_args()
    import maya.standalone
    maya.standalone.initialize()
    try:
        main(args)
    finally:
        maya.standalone.uninitialize()
