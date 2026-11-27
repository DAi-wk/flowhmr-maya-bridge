"""Research: what if the forearm (FK elbow control) only rotated around Z, as animators key it?

Runs the raw mode (HMR's preview solve) on an ADV rig in mayapy, then for every frame decomposes
the elbow control's rotation into a Z-only hinge:

- d: forearm direction the preview wants, in the elbow control's parent space (rigid with the
  upper arm control). f0: forearm direction at rotate 0. a: upper-arm axis (shoulder -> elbow).
- A Z-only elbow can only put the forearm on the cone f0 * Rz(theta) around Z. Turning the whole
  arm about the upper-arm axis a (a shoulder twist delta) keeps shoulder and elbow positions, so
  theta is chosen to keep the angle between forearm and upper arm, then delta swings that cone
  onto d: elbow and wrist positions stay exact.
- What the preview had on the elbow's X/Y beyond that is a roll psi of the forearm about its own
  axis, which would move to the wrist (FKWrist X), and the shoulder control takes delta.

    mayapy research/elbow_hinge.py --scene RIG.ma --cache CACHE_DIR --out RESULT.json [--namespace R]
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
    from flowhmr_bridge import character
    from flowhmr_bridge.client import FlowHMRClient

    if args.namespace:
        cmds.file(str(args.scene.resolve()), reference=True, namespace=args.namespace, prompt=False)
        ns = args.namespace + ":"
    else:
        cmds.file(str(args.scene.resolve()), open=True, force=True, prompt=False)
        ns = ""
    cmds.currentUnit(time="ntsc")
    meta = json.loads((args.cache / "source_meta.json").read_text())
    source = dict(root=FlowHMRClient().root, model=None, offsets=None, meta=meta,
                  data=(args.cache / "source.bin").read_bytes())
    character.apply(ns + "Main", preview_source=source, label="hinge_study", overwrite=True, log=lambda *_: None)
    frames = meta["frames"]

    def world(node):
        return om.MMatrix(cmds.getAttr(node + ".worldMatrix[0]"))

    def pos(matrix):
        return om.MVector(matrix[12], matrix[13], matrix[14])

    def rot(matrix):
        return om.MTransformationMatrix(matrix).asRotateMatrix()

    def signed(u, v, axis):
        return math.degrees(math.atan2((u ^ v) * axis, u * v))

    result = {}
    for side in ("L", "R"):
        shoulder, elbow, wrist = (f"{ns}FK{j}_{side}" for j in ("Shoulder", "Elbow", "Wrist"))
        parent = cmds.listRelatives(elbow, parent=True, fullPath=True)[0]
        order = cmds.getAttr(elbow + ".rotateOrder")
        rows = {k: [] for k in ("rx", "ry", "rz", "delta", "psi", "theta_change", "check_deg", "straight",
                                "bend", "off_hinge")}
        samples = []
        previous_delta = None
        jumps = []
        for f in range(frames):
            cmds.currentTime(f, update=True)
            p_inv = rot(world(parent)).inverse()
            ms, me, mw = world(shoulder), world(elbow), world(wrist)
            a = ((pos(me) - pos(ms)) * p_inv).normal()
            d = ((pos(mw) - pos(me)) * p_inv).normal()
            rx, ry, rz = cmds.getAttr(elbow + ".rotate")[0]
            local = om.MEulerRotation(math.radians(rx), math.radians(ry), math.radians(rz), order).asMatrix()
            # forearm direction at rotate 0 (rigid with the elbow control)
            f0 = (d * local.inverse()).normal()
            z = om.MVector(0, 0, 1)
            # theta: keep the forearm/upper-arm angle, A cos + B sin = target
            target = d * a
            fz = z * (f0 * z)
            fx = f0 - fz
            fy = z ^ fx
            A, B, C = fx * a, fy * a, fz * a
            r = math.hypot(A, B)
            if r < 1e-9:
                continue
            phase = math.atan2(B, A)
            arg = max(-1.0, min(1.0, (target - C) / r))
            options = [phase + math.acos(arg), phase - math.acos(arg)]
            theta = min(options, key=lambda t: abs(math.remainder(t - math.radians(rz), 2 * math.pi)))
            hinge = om.MEulerRotation(0, 0, theta).asMatrix()
            g = f0 * hinge
            # delta: swing g onto d about the upper-arm axis
            gp, dp = g - a * (g * a), d - a * (d * a)
            delta = signed(gp, dp, a) if gp.length() > 1e-9 and dp.length() > 1e-9 else 0.0
            twist = om.MQuaternion(math.radians(delta), a).asMatrix()
            moved = hinge * twist
            check = math.degrees((f0 * moved).angle(d))
            # psi: what is left of the preview's elbow rotation after the hinge, a roll about the forearm
            leftover = om.MTransformationMatrix(moved.inverse() * local).rotation(asQuaternion=True)
            psi = math.degrees(2 * math.acos(min(1.0, abs(leftover.w))))
            bend = math.degrees(math.acos(max(-1.0, min(1.0, target))))  # 0 = straight arm
            rows["rx"].append(rx)
            rows["ry"].append(ry)
            rows["rz"].append(rz)
            rows["delta"].append(delta)
            rows["psi"].append(psi)
            rows["theta_change"].append(math.degrees(theta) - rz)
            rows["check_deg"].append(check)
            rows["straight"].append(bend < 15)
            rows["bend"].append(bend)
            # how far the preview's elbow rotation is from any rotation about Z alone (any Euler form)
            q = om.MTransformationMatrix(local).rotation(asQuaternion=True)
            rows["off_hinge"].append(math.degrees(2 * math.acos(min(1.0, math.hypot(q.w, q.z)))))
            if f % 50 == 0:
                samples.append({"frame": f, "rotate": [round(rx, 1), round(ry, 1), round(rz, 1)], "bend": round(bend, 1),
                                "off_hinge": round(rows["off_hinge"][-1], 1), "delta": round(delta, 1),
                                "psi": round(psi, 1), "theta": round(math.degrees(theta), 1)})
            if previous_delta is not None:
                jumps.append(abs(math.remainder(delta - previous_delta, 360)))
            previous_delta = delta
        straight = [i for i, s in enumerate(rows["straight"]) if s]
        result[side] = {
            "rotate_order": order, "bend": stats(rows["bend"]), "off_hinge": stats(rows["off_hinge"]),
            "samples": samples,
            "elbow_rotate_x": stats(rows["rx"]), "elbow_rotate_y": stats(rows["ry"]), "elbow_rotate_z": stats(rows["rz"]),
            "shoulder_twist_added": stats(rows["delta"]), "forearm_roll_to_wrist": stats(rows["psi"]),
            "elbow_z_change": stats(rows["theta_change"]), "direction_check_deg": stats(rows["check_deg"]),
            "delta_jump_per_frame": stats(jumps), "near_straight_frames": len(straight),
            "delta_jump_near_straight": stats([jumps[i - 1] for i in straight if 0 < i <= len(jumps)]),
        }
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    for side, values in result.items():
        print(side, json.dumps(values, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--namespace", default="R")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    import maya.standalone
    maya.standalone.initialize()
    try:
        main(args)
    finally:
        maya.standalone.uninitialize()
