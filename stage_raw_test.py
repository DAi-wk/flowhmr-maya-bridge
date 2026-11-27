"""Compare a raw bake against an independently exported HMR character, in a fresh mayapy process.

mayapy stage_raw_test.py --scene rig.ma --main Main --job JOB_ID --reference preview.fbx
"""
import argparse
from pathlib import Path


def run(args):
    import maya.cmds as cmds
    import maya.api.OpenMaya as om
    import maya.mel as mel
    from flowhmr_bridge import adv, character, layers, raw
    from flowhmr_bridge.client import FlowHMRClient

    main = args.main
    if args.namespace:
        cmds.file(str(args.scene.resolve()), reference=True, namespace=args.namespace, prompt=False)
        main = args.namespace + ":" + main
    else:
        cmds.file(str(args.scene.resolve()), open=True, force=True, prompt=False)
    cmds.currentUnit(time="ntsc")
    if args.cache:  # a saved rig-only stream (source_meta.json + source.bin), FlowHMR need not run
        import json
        source = dict(root=FlowHMRClient(root=args.root).root, model=None, offsets=None,
                      meta=json.loads((args.cache / "source_meta.json").read_text()),
                      data=(args.cache / "source.bin").read_bytes())
    else:
        source = FlowHMRClient(root=args.root).preview_source(args.job, use_character=not args.rig_only)
    # The raw mode needs no AdvancedSkeleton scripts (adv.apply_build_pose reads the rig's own
    # build pose record), so this process never sources them.
    assert "found in" not in mel.eval('whatIs "asGoToBuildPose"')
    namespace = adv.inspect_rig(main, overwrite=True)["namespace"]
    settings = list(raw.neck_twist_weights(namespace).values()) + list(raw.spine_holds(namespace))
    cmds.currentTime(0)
    original = {p: cmds.getAttr(p) for p in settings}
    result = character.apply(main, preview_source=source, label="raw_parity", overwrite=True, z_only=args.z_only,
                             z_only_knees=args.z_only_knees)
    assert not cmds.namespace(exists=character.CHAR_NS)
    assert layers.layer_info(result["layer"])["mode"] == "raw"
    cmds.setAttr(result["layer"] + ".mute", True)
    cmds.currentTime(0, update=True)
    changed = {p: (v, cmds.getAttr(p)) for p, v in original.items() if abs(cmds.getAttr(p) - v) >= 1e-6}
    assert not changed, f"Muting changed the rig settings: {changed}"
    cmds.setAttr(result["layer"] + ".mute", False)
    cmds.currentTime(0, update=True)
    frames = source["meta"]["frames"]
    assert result["bake_range_30fps"] == (0, frames - 1)
    if not args.reference:
        check = result["baked_check"]
        print(f"{frames} frames, character={source.get('model_name')}, final layer sample {check}", flush=True)
        assert check["cm"] <= args.max_position
        assert max(check["deg"], check["split_deg"]) <= args.max_rotation
        return
    cmds.loadPlugin("fbxmaya", quiet=True)
    reference = character.import_character(args.reference.resolve())
    moving = character.moving_joints(reference)
    names = set(moving)
    for joint in moving:
        base, sep, side = joint.rpartition("_")
        if sep:
            names.update(n for n in reference if n.startswith(base + "Part") and n.endswith("_" + side)
                         and n[len(base) + 4:-(len(side) + 1)].isdigit())
    maximum = {joint: [0.0, 0.0] for joint in names}
    assert character.key_range(reference) == (0, frames - 1)
    hinged = [(limb, names) for limb, names, on in (("arm", ("Shoulder", "Elbow"), args.z_only),
                                                     ("leg", ("Hip", "Knee"), args.z_only_knees)) if on]
    middle_xy = {limb: 0.0 for limb, _ in hinged}
    # Frame-to-frame turn of the elbow / knee joint, rig vs the export: a jump the export does not
    # have is a twist pop from the single-axis rewrite. (That joint carries the whole upper-bone
    # roll; ADV shares it out of the shoulder / hip joint over the Part joints.)
    starts = {f"{names[1]}_{side}": [] for _, names in hinged for side in ("L", "R")}
    previous = {}
    for frame in range(frames):
        cmds.currentTime(frame)
        for limb, names in hinged:
            for side in ("L", "R"):
                r = cmds.getAttr(f"{namespace}FK{names[1]}_{side}.rotate")[0]
                middle_xy[limb] = max(middle_xy[limb], abs(r[0]), abs(r[1]))
        for joint, errors in maximum.items():
            a = cmds.getAttr(namespace + joint + ".worldMatrix[0]")
            b = cmds.getAttr(reference[joint] + ".worldMatrix[0]")
            errors[0] = max(errors[0], (om.MVector(*a[12:15]) - om.MVector(*b[12:15])).length())
            errors[1] = max(errors[1], character._angle(character._rotation(a), character._rotation(b)))
        for joint, jumps in starts.items():
            now = (character._rotation(cmds.getAttr(namespace + joint + ".worldMatrix[0]")),
                   character._rotation(cmds.getAttr(reference[joint] + ".worldMatrix[0]")))
            if joint in previous:
                jumps.append(character._angle(previous[joint][0], now[0]) - character._angle(previous[joint][1], now[1]))
            previous[joint] = now
    for joint, (cm, deg) in sorted(maximum.items()):
        print(f"{joint:24} {cm:8.4f} cm  {deg:8.4f} deg", flush=True)
    # Single-axis elbows / knees: the twist along those bones is the rig's own and, near
    # straight, the elbow / knee may move a little; everything else stays exact.
    patterns = [p for p, on in ((character.ARM_TWIST, args.z_only), (character.LEG_TWIST, args.z_only_knees)) if on]
    twisted = {j for j in maximum if any(p.fullmatch(j) for p in patterns)}
    rotation = max(deg for j, (_, deg) in maximum.items() if j not in twisted)
    position = max(cm for j, (cm, _) in maximum.items() if j not in twisted)
    print(f"{frames} frames, {len(moving)} animated joints, {len(maximum)} including Part joints, "
          f"worst position {position:.6f} cm, rotation {rotation:.6f} deg"
          + (f" (without {len(twisted)} twist joints: up to {max(maximum[j][1] for j in twisted):.2f} deg, "
             f"{max(maximum[j][0] for j in twisted):.4f} cm)" if twisted else ""), flush=True)
    if hinged:
        print("z-only:", result["z_only"], flush=True)
        for limb, names in hinged:
            print(f"FK{names[1]} other axes at most {middle_xy[limb]:.5f} deg", flush=True)
            assert middle_xy[limb] <= 1e-3, (limb, middle_xy[limb])
        shift = max(r.get("max_shift_cm", 0.0) for r in result["z_only"].values())
        moved = max(maximum[j][0] for j in twisted)
        assert moved <= shift + 1e-3, (moved, shift)
        for joint, jumps in starts.items():
            print(f"{joint}: frame-to-frame turn at most {max(jumps):.2f} deg more than the export", flush=True)
            assert max(jumps) <= args.max_jump, (joint, max(jumps))
    assert rotation <= args.max_rotation, (rotation, args.max_rotation)
    assert position <= args.max_position, (position, args.max_position)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--main", default="Main")
    parser.add_argument("--job")
    parser.add_argument("--cache", type=Path, help="folder with source_meta.json and source.bin instead of --job")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--namespace", help="reference the scene into this namespace")
    parser.add_argument("--rig-only", action="store_true", help="measure the build-pose solve without saved character data")
    parser.add_argument("--reference", type=Path, help="independent character export for a full frame-by-frame check")
    parser.add_argument("--z-only", action="store_true", help="Z-only FK elbows; arm twist joints are reported, not checked")
    parser.add_argument("--z-only-knees", action="store_true", help="Z-only FK knees; leg twist joints are reported, not checked")
    parser.add_argument("--max-jump", type=float, default=15.0,
                        help="degrees per frame the elbow / knee joint may turn beyond the export (twist pops)")
    parser.add_argument("--max-rotation", type=float, default=0.05, help="degrees on the tested rig")
    parser.add_argument("--max-position", type=float, default=0.01, help="centimetres on the tested rig")
    args = parser.parse_args()
    if not (args.job or args.cache):
        parser.error("--job or --cache is required")
    import maya.standalone
    maya.standalone.initialize()
    try:
        run(args)
    finally:
        maya.standalone.uninitialize()
