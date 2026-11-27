"""Test 自訂對應＋HMR 預覽求解 (custom_preview.apply) end to end in a fresh mayapy process.

    mayapy stage_custom_hmr_test.py --scene rig.ma --profile profile.json (--job JOB_ID | --cache DIR)
        [--reference hmr_character.fbx] [--stack] [--fail probe|connect] [--refine]

The rig is referenced into a namespace and never saved. Checks, through the real apply:
- the final animation layer against FlowHMR's own character export (--reference), every frame;
- muting the layer gives back the rig's settings (hmr.hold values such as bias);
- --stack: a second apply over the first layer gives the same result;
- --fail: an error after the probe or after connecting leaves the scene as it was.
"""
import argparse
import json
import math
import re
from pathlib import Path


def run(args):
    import maya.api.OpenMaya as om
    import maya.cmds as cmds
    from flowhmr_bridge import character, custom_preview, generic, layers, mapping
    from flowhmr_bridge.adv import ApplyError
    from flowhmr_bridge.client import FlowHMRClient

    if args.open:
        cmds.file(str(args.scene.resolve()), open=True, force=True, prompt=False)
        ns = ""
    else:
        cmds.file(str(args.scene.resolve()), reference=True, namespace=args.namespace, prompt=False)
        ns = args.namespace + ":"
    cmds.currentUnit(time="ntsc")
    profile = generic.load_profile(args.profile)
    if args.cache:
        meta = json.loads((args.cache / "source_meta.json").read_text())
        source = dict(root=FlowHMRClient(root=args.root).root, meta=meta,
                      data=(args.cache / "source.bin").read_bytes(), model=None, offsets=None)
    else:
        source = FlowHMRClient(root=args.root).preview_source(args.job, use_character=args.use_character)
    frames = source["meta"]["frames"]
    if args.saved:
        return corrections(args, ns, profile, source, cmds, om, character, custom_preview)
    _, followers, hold = mapping.expand_hmr(profile)
    settings = [ns + p for p in hold if cmds.objExists(ns + p)]
    driven = list(dict.fromkeys(generic.mapped_controls(profile, ns)
                                + [ns + c for _, c, _ in followers if cmds.objExists(ns + c)]))
    cmds.currentTime(0, update=True)
    original = {p: cmds.getAttr(p) for p in settings}

    def snapshot():
        cmds.currentTime(0, update=True)
        state = {}
        for control in driven:
            for plug in generic._plugs(control, "parent"):
                state[plug] = (round(cmds.getAttr(plug), 6), cmds.connectionInfo(plug, sourceFromDestination=True))
        for plug in settings:
            state[plug] = (round(cmds.getAttr(plug), 6), cmds.connectionInfo(plug, sourceFromDestination=True))
        state["layers"] = tuple(cmds.ls(type="animLayer") or [])
        state["namespaces"] = tuple(sorted(cmds.namespaceInfo(":", listOnlyNamespaces=True) or []))
        state["fps"] = cmds.currentUnit(q=True, time=True)
        state["nodes"] = frozenset(cmds.ls())  # nothing added or left behind
        return state

    if args.fail:
        before = snapshot()
        original_fn = {"probe": "scene_skeleton", "connect": "compare"}[args.fail]
        real = getattr(custom_preview, original_fn)

        def broken(*a, **k):
            raise ApplyError(f"forced failure in {original_fn}")

        setattr(custom_preview, original_fn, broken)
        try:
            custom_preview.apply(ns, profile, source, label="fail_test", overwrite=True, refine=args.refine)
            raise AssertionError("apply did not fail")
        except ApplyError as error:
            assert "forced failure" in str(error), error
        finally:
            setattr(custom_preview, original_fn, real)
        after = snapshot()
        diff = {k: (before[k], after.get(k)) for k in before if k != "nodes" and before[k] != after.get(k)}
        added, removed = sorted(after["nodes"] - before["nodes"]), sorted(before["nodes"] - after["nodes"])
        assert not diff and not added and not removed, f"scene not restored: {list(diff.items())[:5]} +{added[:5]} -{removed[:5]}"
        print(f"forced failure after {args.fail}: scene restored ({len(before) - 1} channels and settings of "
              f"{len(driven)} controls, {len(before['nodes'])} nodes)", flush=True)
        return

    hinged = args.z_only or args.z_only_knees
    baseline = None
    if hinged:
        # The same apply without the single-axis rewrite, sampled on every skin joint and then
        # removed: what the rewritten limbs are checked against.
        base = custom_preview.apply(ns, profile, source, label="baseline", overwrite=True, refine=args.refine)
        tree = custom_preview.deformation_tree(ns)
        baseline = {p: [] for p in tree}
        for frame in range(frames):
            cmds.currentTime(frame, update=True)
            for p in tree:
                baseline[p].append(om.MMatrix(cmds.getAttr(p + ".worldMatrix[0]")))
        layers.remove_layer(base["layer"], restore_range=False)
    runs = 2 if args.stack else 1
    result = None
    for i in range(runs):
        result = custom_preview.apply(ns, profile, source, label=f"custom_hmr_{i}", overwrite=True, refine=args.refine,
                                      z_only=args.z_only, z_only_knees=args.z_only_knees)
        if hinged:
            print(f"z-only {i}: {result['z_only']}", flush=True)
            for label, report in (result["z_only"] or {}).items():
                if "middle" not in report:
                    continue
                check_hinge(label, report, baseline, frames, args.max_jump, cmds, om, character)
        print(f"apply {i}: layer {result['layer']}, connected {result['check']}, layer {result['baked_check']}", flush=True)
    assert not cmds.namespace(exists=character.CHAR_NS)
    assert layers.layer_info(result["layer"])["mode"] == "custom_hmr"
    cmds.setAttr(result["layer"] + ".mute", True)
    cmds.currentTime(0, update=True)
    changed = {p: (v, cmds.getAttr(p)) for p, v in original.items() if abs(cmds.getAttr(p) - v) >= 1e-6}
    cmds.setAttr(result["layer"] + ".mute", False)
    cmds.currentTime(0, update=True)
    if args.stack:
        # Muting the top layer shows the first apply, which holds the same settings.
        print(f"muted top layer: {len(changed)} settings differ from the rig's own values (expected: held by layer 0)")
    else:
        assert not changed, f"Muting changed the rig settings: {changed}"
        print(f"muted layer: {len(original)} held settings back to the rig's values", flush=True)
    summary = {"result": {k: v for k, v in result.items() if k not in ("keys",)}}
    if args.reference:
        cmds.loadPlugin("fbxmaya", quiet=True)
        reference = character.import_character(args.reference.resolve())
        # The animated joints and the Part (twist/split) joints between them, as stage_raw_test does.
        # ADV's *Partial joints are half-way helpers outside the exported skeleton's motion.
        names = [n for n in reference if cmds.objExists(ns + n)
                 and (character.moving_joints({n: reference[n]}) or re.search(r"Part\d+_", n))]
        worst = {n: [0.0, 0.0] for n in names}
        for frame in range(frames):
            cmds.currentTime(frame, update=True)
            for n in names:
                a = om.MMatrix(cmds.getAttr(ns + n + ".worldMatrix[0]"))
                b = om.MMatrix(cmds.getAttr(reference[n] + ".worldMatrix[0]"))
                worst[n][0] = max(worst[n][0], math.sqrt(sum((a[i] - b[i]) ** 2 for i in (12, 13, 14))))
                worst[n][1] = max(worst[n][1], character._angle(character._rotation(a), character._rotation(b)))
        # Single-axis elbows / knees: the twist along those bones is the rig's own and, near
        # straight, the elbow / knee may move a little (checked against the baseline above).
        patterns = [p for p, on in ((character.ARM_TWIST, args.z_only), (character.LEG_TWIST, args.z_only_knees)) if on]
        twisted = {n for n in worst if any(p.fullmatch(n) for p in patterns)}
        if twisted:
            print(f"{len(twisted)} twist joints (not checked here): up to {max(worst[n][1] for n in twisted):.2f} deg, "
                  f"{max(worst[n][0] for n in twisted):.4f} cm", flush=True)
        deg = max((n for n in worst if n not in twisted), key=lambda n: worst[n][1])
        cm = max((n for n in worst if n not in twisted), key=lambda n: worst[n][0])
        print(f"vs FlowHMR export, {len(names)} joints x {frames} frames: worst {worst[deg][1]:.6f} deg ({deg}), "
              f"{worst[cm][0]:.6f} cm ({cm})", flush=True)
        summary["reference"] = {n: [round(c, 6), round(d, 6)] for n, (c, d) in worst.items()}
        character.remove_character()
        assert worst[deg][1] <= args.max_rotation and worst[cm][0] <= args.max_position
    if args.out:
        args.out.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")


def check_hinge(label, report, baseline, frames, max_jump, cmds, om, character):
    """One rewritten limb against the baseline apply: the middle control keeps one axis, the end
    joint keeps its world position and rotation, the middle joint moves at most what the report
    says, and the middle joint (carrying the whole upper-bone roll) turns no faster from frame to
    frame than in the baseline (+max_jump)."""
    k = "xyz".index(report["axis"])
    _, middle, end = report["joints"]
    other = shift = end_cm = end_deg = 0.0
    jump = None
    previous = None
    for frame in range(frames):
        cmds.currentTime(frame, update=True)
        r = cmds.getAttr(report["middle"] + ".rotate")[0]
        other = max([other] + [abs(r[n]) for n in range(3) if n != k])
        now = {p: om.MMatrix(cmds.getAttr(p + ".worldMatrix[0]")) for p in (middle, end)}
        base = {p: baseline[p][frame] for p in now}

        def cm(a, b):
            return math.sqrt(sum((a[i] - b[i]) ** 2 for i in (12, 13, 14)))

        shift = max(shift, cm(now[middle], base[middle]))
        end_cm = max(end_cm, cm(now[end], base[end]))
        end_deg = max(end_deg, character._angle(character._rotation(now[end]), character._rotation(base[end])))
        if previous:
            extra = (character._angle(character._rotation(previous[0]), character._rotation(now[middle]))
                     - character._angle(character._rotation(previous[1]), character._rotation(base[middle])))
            jump = extra if jump is None else max(jump, extra)
        previous = (now[middle], base[middle])
    print(f"{label}: {report['middle']} keeps only {report['axis'].upper()} (other axes at most {other:.5f} deg); "
          f"end joint off by {end_cm:.5f} cm / {end_deg:.5f} deg, middle joint moved at most {shift:.4f} cm, "
          f"middle joint turns at most {jump:.2f} deg/frame more than without the rewrite", flush=True)
    assert other <= 1e-3, other
    assert end_cm <= 1e-3 and end_deg <= 1e-2, (end_cm, end_deg)
    assert shift <= report["max_shift_cm"] + 1e-3, (shift, report["max_shift_cm"])
    assert jump <= max_jump, jump


def corrections(args, ns, profile, source, cmds, om, character, custom_preview):
    """Rig-only apply, then HMR's saved character with its 校正值 (plus --corrections) on top;
    prints each joint's change between the two (local rotation and translation, world for the
    pelvis), which should be exactly the corrections."""
    import copy

    saved = json.loads((args.saved / "saved.json").read_text(encoding="utf-8"))
    offsets = copy.deepcopy(saved["offsets"])
    extra = json.loads(args.corrections.read_text(encoding="utf-8")) if args.corrections else {}
    for key in ("rotations", "translations"):
        offsets["retarget"].setdefault(key, {}).update(extra.get("retarget", {}).get(key, {}))
    for key in ("rootRotation", "rootTranslation"):
        if key in extra:
            offsets[key] = extra[key]
    print("corrections:", offsets["retarget"]["rotations"], offsets["retarget"]["translations"],
          offsets.get("rootRotation"), offsets.get("rootTranslation"), flush=True)
    with_model = dict(source, model=(args.saved / "model.fbx").read_bytes(), model_name=saved["model_name"],
                      offsets=offsets)
    base = custom_preview.apply(ns, profile, source, label="rig_only", overwrite=True)
    top = custom_preview.apply(ns, profile, with_model, label="saved", overwrite=True)
    print(f"saved character: connected {top['check']['deg']} deg / {top['check']['cm']} cm", flush=True)
    names = sorted({n for n in mapping_joints(profile, ns, cmds)})

    def pose(frame):
        cmds.currentTime(frame, update=True)
        out = {}
        for n in names:
            m = om.MMatrix(cmds.getAttr(ns + n + ".worldMatrix[0]"))
            parent = cmds.listRelatives(ns + n, parent=True, fullPath=True)
            p = om.MMatrix(cmds.getAttr(parent[0] + ".worldMatrix[0]")) if parent else om.MMatrix()
            out[n] = (m, m * p.inverse())
        return out

    worst = {n: [0.0, 0.0, 0.0, 0.0] for n in names}  # local deg, local cm, world deg, world cm
    for frame in (0, 60, 150, 240, 302):
        b = pose(frame)
        cmds.setAttr(top["layer"] + ".mute", True)
        a = pose(frame)
        cmds.setAttr(top["layer"] + ".mute", False)
        for n in names:
            (wa, la), (wb, lb) = a[n], b[n]
            row = worst[n]
            row[0] = max(row[0], character._angle(character._rotation(la), character._rotation(lb)))
            row[1] = max(row[1], math.sqrt(sum((la[i] - lb[i]) ** 2 for i in (12, 13, 14))))
            row[2] = max(row[2], character._angle(character._rotation(wa), character._rotation(wb)))
            row[3] = max(row[3], math.sqrt(sum((wa[i] - wb[i]) ** 2 for i in (12, 13, 14))))
    for n, (ld, lc, wd, wc) in sorted(worst.items(), key=lambda kv: -kv[1][0]):
        if ld > 0.01 or lc > 0.01:
            print(f"  {n:22} local {ld:8.3f} deg {lc:7.3f} cm   world {wd:8.3f} deg {wc:7.3f} cm", flush=True)
    unchanged = [n for n, (ld, lc, _, _) in worst.items() if ld <= 0.01 and lc <= 0.01]
    print(f"{len(unchanged)} of {len(names)} joints keep their local transform", flush=True)


def mapping_joints(profile, ns, cmds):
    """The ADV body joints to report (this check is written for the ADV test rig)."""
    from flowhmr_bridge import custom_preview
    tree = custom_preview.deformation_tree(ns)
    names = [p.split("|")[-1].split(":")[-1] for p in tree]
    keep = {"Root_M", "Spine1_M", "Spine1Part2_M", "Chest_M", "Neck_M", "Head_M", "root"}
    for side in ("L", "R"):
        keep |= {f"{j}_{side}" for j in ("Scapula", "Shoulder", "Elbow", "Wrist", "Hip", "Knee", "Ankle", "Toes",
                                         "MiddleFinger1", "ThumbFinger1")}
    return [n for n in names if n in keep and names.count(n) == 1]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--namespace", default="R")
    parser.add_argument("--open", action="store_true", help="open the scene (no namespace) instead of referencing it")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--job")
    parser.add_argument("--cache", type=Path, help="folder with source_meta.json and source.bin (rig-only source)")
    parser.add_argument("--use-character", action="store_true", help="with --job: HMR's saved character and 校正值")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--stack", action="store_true")
    parser.add_argument("--fail", choices=("probe", "connect"))
    parser.add_argument("--refine", action="store_true")
    parser.add_argument("--z-only", action="store_true", help="Z-only FK elbows (arm twist joints reported, not checked)")
    parser.add_argument("--z-only-knees", action="store_true", help="Z-only FK knees (leg twist joints reported, not checked)")
    parser.add_argument("--max-jump", type=float, default=15.0,
                        help="degrees per frame the elbow / knee joint may turn beyond the apply without the rewrite")
    parser.add_argument("--saved", type=Path, help="folder with model.fbx and saved.json (HMR's saved character)")
    parser.add_argument("--corrections", type=Path, help="extra 校正值 to add to the saved ones (JSON)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-rotation", type=float, default=0.05)
    parser.add_argument("--max-position", type=float, default=0.01)
    args = parser.parse_args()
    if not (args.job or args.cache):
        parser.error("--job or --cache is required")
    import maya.standalone
    maya.standalone.initialize()
    try:
        run(args)
    finally:
        maya.standalone.uninitialize()
