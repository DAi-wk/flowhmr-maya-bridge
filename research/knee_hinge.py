"""Research: the FK knee bending on Z only, as animators key it (flowhmr_bridge.hinge on the legs).

Runs the raw mode (HMR's preview solve) on an ADV rig in mayapy and, for both legs, reports:
- how far HMR's knee is from a Z-only hinge (the knee control's other two axes) and how far the
  leg bends (0 = straight);
- the bend axis the motion shows (share of the bend plane normal, absolute and signed) and the
  rig's own pre-bend (knee control at rotate 0);
- the single-axis rewrite without and with easing near straight: thigh roll it adds, the largest
  frame-to-frame turn of the hip control (before / after), the knee shift, ankle and toe errors.

    mayapy research/knee_hinge.py --scene RIG.ma --cache CACHE_DIR --out RESULT.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def stats(values):
    values = sorted(abs(v) for v in values)
    if not values:
        return {}
    return {"mean": round(sum(values) / len(values), 2), "p95": round(values[int(0.95 * (len(values) - 1))], 2),
            "max": round(values[-1], 2)}


def main(args):
    import maya.api.OpenMaya as om
    import maya.cmds as cmds
    from flowhmr_bridge import character, hinge
    from flowhmr_bridge.client import FlowHMRClient

    meta = json.loads((args.cache / "source_meta.json").read_text())
    source = dict(root=FlowHMRClient().root, model=None, offsets=None, meta=meta,
                  data=(args.cache / "source.bin").read_bytes())
    frames = list(range(meta["frames"]))

    def pos(node):
        return om.MVector(cmds.xform(node, q=True, ws=True, t=True))

    def turn(a, b):
        q = om.MTransformationMatrix(a * b.inverse()).rotation(asQuaternion=True)
        return math.degrees(2 * math.acos(min(1.0, abs(q.w))))

    def legs():
        for side in ("L", "R"):
            yield side, tuple(f"FK{j}_{side}" for j in ("Hip", "Knee", "Ankle")), \
                tuple(f"FKX{j}_{side}" for j in ("Hip", "Knee", "Ankle")), f"FKXToes_{side}"

    def bake():
        cmds.file(str(args.scene.resolve()), open=True, force=True, prompt=False)
        cmds.currentUnit(time="ntsc")
        character.apply("Main", preview_source=source, label="knee_study", overwrite=True, log=lambda *_: None)

    def sample(controls, joints, toe):
        rows = []
        for f in frames:
            cmds.currentTime(f, update=True)
            rows.append({"hip": hinge._rotation(controls[0]), "knee": cmds.getAttr(controls[1] + ".rotate")[0],
                         "points": [pos(j) for j in joints] + [pos(toe)], "ankle": hinge._rotation(controls[2])})
        return rows

    result = {side: {} for side in ("L", "R")}
    for eased in (False, True):
        bake()
        real = hinge.eased_rolls
        if not eased:
            hinge.eased_rolls = lambda rolls, bends: list(rolls)
        try:
            for side, controls, joints, toe in legs():
                before = sample(controls, joints, toe)
                entry = result[side]
                if not eased:
                    bends = [math.degrees((r["points"][1] - r["points"][0]).angle(r["points"][2] - r["points"][1]))
                             for r in before]
                    found, share = hinge.bend_axis(controls, joints, frames)
                    rest, rest_share, rest_bend = hinge.rest_axis(controls, joints)
                    entry.update({"bend": stats(bends), "near_straight_frames": sum(b < 15 for b in bends),
                                  "knee_other_axes": stats([max(abs(r["knee"][0]), abs(r["knee"][1])) for r in before]),
                                  "motion_axis": [found, round(share, 3)], "rest_axis": [rest, round(rest_share, 3)],
                                  "rest_bend": round(rest_bend, 2),
                                  "hip_turn_per_frame": stats([turn(b["hip"], a["hip"]) for a, b in zip(before, before[1:])])})
                report = hinge.clean_baked([(side, "leg", controls, joints)], frames, axis="z")[side]
                after = sample(controls, joints, toe)
                key = "eased" if eased else "exact"
                entry[key] = {
                    "report": {k: v for k, v in report.items() if k not in ("joints", "middle")},
                    "thigh_roll_added": stats([turn(a["hip"], b["hip"]) for a, b in zip(after, before)]),
                    "hip_turn_per_frame": stats([turn(b["hip"], a["hip"]) for a, b in zip(after, after[1:])]),
                    "knee_shift_cm": stats([(a["points"][1] - b["points"][1]).length() for a, b in zip(after, before)]),
                    "ankle_toe_cm": round(max(max((a["points"][n] - b["points"][n]).length() for n in (0, 2, 3))
                                              for a, b in zip(after, before)), 6),
                    "ankle_deg": round(max(turn(a["ankle"], b["ankle"]) for a, b in zip(after, before)), 6),
                }
        finally:
            hinge.eased_rolls = real
    args.out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    for side, values in result.items():
        print(side, json.dumps(values, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import maya.standalone
    maya.standalone.initialize()
    try:
        main(args)
    finally:
        maya.standalone.uninitialize()
