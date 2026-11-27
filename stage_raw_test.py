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
    result = character.apply(main, preview_source=source, label="raw_parity", overwrite=True, z_only=args.z_only)
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
    elbow_xy = 0.0
    for frame in range(frames):
        cmds.currentTime(frame)
        if args.z_only:
            for side in ("L", "R"):
                r = cmds.getAttr(f"{namespace}FKElbow_{side}.rotate")[0]
                elbow_xy = max(elbow_xy, abs(r[0]), abs(r[1]))
        for joint, errors in maximum.items():
            a = cmds.getAttr(namespace + joint + ".worldMatrix[0]")
            b = cmds.getAttr(reference[joint] + ".worldMatrix[0]")
            errors[0] = max(errors[0], (om.MVector(*a[12:15]) - om.MVector(*b[12:15])).length())
            errors[1] = max(errors[1], character._angle(character._rotation(a), character._rotation(b)))
    for joint, (cm, deg) in sorted(maximum.items()):
        print(f"{joint:24} {cm:8.4f} cm  {deg:8.4f} deg", flush=True)
    # Z-only elbows: the upper arm / forearm twist is the rig's own, everything else stays exact.
    twisted = {j for j in maximum if args.z_only and character.ARM_TWIST.fullmatch(j)}
    rotation = max(deg for j, (_, deg) in maximum.items() if j not in twisted)
    position = max(cm for cm, _ in maximum.values())
    print(f"{frames} frames, {len(moving)} animated joints, {len(maximum)} including Part joints, "
          f"worst position {position:.6f} cm, rotation {rotation:.6f} deg"
          + (f" (without {len(twisted)} arm twist joints, up to {max(maximum[j][1] for j in twisted):.2f} deg; "
             f"FK elbow X/Y at most {elbow_xy:.5f} deg)" if twisted else ""), flush=True)
    if args.z_only:
        print("z-only:", result["z_only"], flush=True)
        assert elbow_xy <= 1e-3, elbow_xy
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
