"""Stage 1 checks for flowhmr_bridge.generic on an ADV rig, using ADV's own result as the reference.

Run step by step in a throwaway scene (each call returns its part of the report):

    import sys; sys.path.insert(0, r"<path to this repository>")
    import stage1_generic_test as s1
    s1.prepare("<FlowHMR job id>", rig_ns="char:")      # backs up the rig's current keys
    s1.run_adv()                 # reference: ADV MoCapLibrary result for the same clip
    s1.run_generic("t")          # generic engine, rig rest = zero pose
    s1.run_generic("arms_down")  # generic engine, rig rest = arms rotated down 50 degrees
    s1.failure_test()
    s1.finish()                  # restores the backed-up keys, writes stage1_report.json
"""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import maya.cmds as cmds

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from flowhmr_bridge import adv, generic  # noqa: E402
from flowhmr_bridge.client import FlowHMRClient  # noqa: E402

# ADV deform joint -> flowhmr-profile source joint
PAIRS = {"Root_M": "Hips", "Chest_M": "Spine2", "Neck_M": "Neck", "Head_M": "Head"}
for _s, _w in (("R", "Right"), ("L", "Left")):
    PAIRS.update({f"Hip_{_s}": f"{_w}UpLeg", f"Knee_{_s}": f"{_w}Leg", f"Ankle_{_s}": f"{_w}Foot",
                  f"Toes_{_s}": f"{_w}ToeBase", f"Scapula_{_s}": f"{_w}Shoulder", f"Shoulder_{_s}": f"{_w}Arm",
                  f"Elbow_{_s}": f"{_w}ForeArm", f"Wrist_{_s}": f"{_w}Hand",
                  f"IndexFinger1_{_s}": f"{_w}HandIndex1", f"IndexFinger2_{_s}": f"{_w}HandIndex2",
                  f"ThumbFinger1_{_s}": f"{_w}HandThumb1", f"ThumbFinger2_{_s}": f"{_w}HandThumb2"})
SEGMENTS = [("Root_M", "Chest_M"), ("Chest_M", "Neck_M"), ("Neck_M", "Head_M")]
for _s in "RL":
    SEGMENTS += [(f"Hip_{_s}", f"Knee_{_s}"), (f"Knee_{_s}", f"Ankle_{_s}"), (f"Ankle_{_s}", f"Toes_{_s}"),
                 (f"Scapula_{_s}", f"Shoulder_{_s}"), (f"Shoulder_{_s}", f"Elbow_{_s}"), (f"Elbow_{_s}", f"Wrist_{_s}"),
                 (f"Wrist_{_s}", f"IndexFinger1_{_s}"), (f"IndexFinger1_{_s}", f"IndexFinger2_{_s}"),
                 (f"ThumbFinger1_{_s}", f"ThumbFinger2_{_s}")]
STATE = {}


def _pos(node):
    return cmds.xform(node, q=True, ws=True, translation=True)


def _angle(u, v):
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
    if nu < 1e-6 or nv < 1e-6:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v)) / (nu * nv)))))


def _vec(row, a, b):
    return [y - x for x, y in zip(row[a], row[b])] if a in row and b in row else None


def sample(frames, source_prefix=None):
    ns, rows = STATE["ns"], {}
    for frame in frames:
        cmds.currentTime(frame, edit=True)
        row = {joint: _pos(ns + joint) for joint in PAIRS if cmds.objExists(ns + joint)}
        if source_prefix:
            row.update({"src:" + joint: _pos(source_prefix + name) for joint, name in PAIRS.items()
                        if cmds.objExists(source_prefix + name)})
        rows[frame] = row
    return rows


def _stats(values):
    values = [v for v in values if v is not None]
    return {"mean": round(sum(values) / len(values), 2), "max": round(max(values), 2)} if values else None


def errors_vs_source(rows):
    rest, motion = {}, {}
    for frame, row in rows.items():
        for a, b in SEGMENTS:
            rig, src = _vec(row, a, b), _vec(row, "src:" + a, "src:" + b)
            if rig and src:
                angle = _angle(rig, src)
                if frame < 0:
                    rest[f"{a}>{b}"] = round(angle, 2)
                else:
                    motion.setdefault(f"{a}>{b}", []).append(angle)
    return {"frame_-1": rest, "motion": {k: _stats(v) for k, v in motion.items()}}


def compare(rows_a, rows_b):
    per = {}
    for frame, row in rows_a.items():
        other = rows_b.get(frame)
        if other is None:
            continue
        for a, b in SEGMENTS:
            u, v = _vec(row, a, b), _vec(other, a, b)
            if u and v:
                per.setdefault(f"{a}>{b}", []).append(_angle(u, v))
    return {k: _stats(v) for k, v in per.items()}


def drift(rows_a, rows_b):
    worst = 0.0
    for frame, row in rows_a.items():
        for joint in PAIRS:
            if joint in row and joint in rows_b.get(frame, {}):
                worst = max(worst, math.dist(row[joint], rows_b[frame][joint]))
    return round(worst, 4)


def clear_keys():
    """Delete the rig's FlowHMR layers (each apply bakes onto one) and the time-based keys on
    its ControlSet (left by a previous run)."""
    from flowhmr_bridge import layers
    ns = STATE["ns"]
    for layer in reversed(layers.flowhmr_layers(ns)):
        layers.remove_layer(layer, restore_range=False)
    curves = set()
    for member in cmds.sets(ns + "ControlSet", q=True) or []:
        for curve in cmds.listConnections(member, s=True, d=False, type="animCurve") or []:
            if cmds.nodeType(curve).startswith("animCurveT"):
                curves.add(curve)
    if curves:
        cmds.delete(list(curves))
    return len(curves)


def prepare(job_id, rig_ns="char:"):
    STATE.clear()
    STATE.update(ns=rig_ns, main=rig_ns + "Main", job=job_id, report={})
    client = FlowHMRClient()
    root = adv.staging_root()
    STATE["adv_fbx"] = client.download_fbx(job_id, root / f"{job_id}_adv")
    STATE["generic_fbx"] = client.download_fbx(job_id, root / f"{job_id}_flowhmr_t", profile="flowhmr", mesh=0, first_pose="t")
    animated = adv.animated_controls(rig_ns)
    STATE["backup"] = adv.backup_animation(STATE["main"], rig_ns, label="stage1 test") if animated else None
    STATE["unit_before"] = cmds.currentUnit(q=True, time=True)
    STATE["report"]["prepare"] = {"backup": STATE["backup"], "animated_before": len(animated),
                                  "unit": STATE["unit_before"]}
    return STATE["report"]["prepare"]


def _frames():
    return STATE.setdefault("frames", list(range(0, 151, 10)))


def run_adv():
    clear_keys()
    rows = {}
    result = adv.apply(STATE["main"], STATE["adv_fbx"], log=lambda *_: None,
                       on_baked=lambda ns: rows.update(sample(_frames())))
    STATE["rows_adv"] = rows
    STATE["report"]["adv"] = {"driven": result["driven_controls"], "keys": result["keys"]["keys"]}
    return STATE["report"]["adv"]


def run_generic(rest="t"):
    clear_keys()
    profile = generic.load_profile("adv_generic_test")
    ns = STATE["ns"]
    controls = generic.mapped_controls(profile, ns)
    for control in controls:
        for plug in generic._channels(control, "parent"):
            cmds.setAttr(plug, 0)
    if rest == "arms_down":
        profile = copy.deepcopy(profile)
        profile["rest"] = "current"
        cmds.currentTime(-1, edit=True)
        cmds.rotate(0, 0, -50, ns + "FKShoulder_L", relative=True, worldSpace=True)
        cmds.rotate(0, 0, 50, ns + "FKShoulder_R", relative=True, worldSpace=True)
    connected, baked, logs = {}, {}, []
    frames = [-1] + _frames()
    result = generic.apply(ns, profile, STATE["generic_fbx"], log=logs.append,
                           on_connected=lambda _: connected.update(sample(frames, generic.source(""))),
                           on_baked=lambda _: baked.update(sample(frames)))
    STATE[f"rows_generic_{rest}"] = baked
    report = {"result": {k: result[k] for k in ("driven_controls", "calibration", "euler_flips_fixed", "keys", "fps")},
              "vs_source": errors_vs_source(connected),
              "bake_drift_cm": drift({f: {j: v for j, v in r.items() if not j.startswith("src:")} for f, r in connected.items()}, baked),
              "vs_adv": compare({f: r for f, r in baked.items() if f >= 0}, STATE.get("rows_adv", {})),
              "log": logs}
    if rest != "t" and "rows_generic_t" in STATE:
        report["vs_generic_t"] = compare({f: r for f, r in baked.items() if f >= 0}, STATE["rows_generic_t"])
    STATE["report"][f"generic_{rest}"] = report
    if rest == "arms_down":
        clear_keys()
        for side in "LR":
            for plug in generic._channels(ns + f"FKShoulder_{side}", "orient"):
                cmds.setAttr(plug, 0)
    return report


def failure_test():
    clear_keys()
    ns = STATE["ns"]
    profile = generic.load_profile("adv_generic_test")
    watch = [ns + "FKIKLeg_L.FKIKBlend", ns + "FKShoulder_L.rotateZ", ns + "RootX_M.translateY"]
    cmds.setAttr(ns + "FKShoulder_L.rotateZ", 12.5)
    before = {"curves": len(cmds.ls(type="animCurve")), "unit": cmds.currentUnit(q=True, time=True),
              "range": [cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)],
              "values": [round(cmds.getAttr(p), 4) for p in watch]}

    def boom(_):
        raise RuntimeError("stage1 failure test")

    try:
        generic.apply(ns, profile, STATE["generic_fbx"], log=lambda *_: None, on_baked=boom)
        raised = False
    except adv.ApplyError as error:
        raised = str(error)
    after = {"curves": len(cmds.ls(type="animCurve")), "unit": cmds.currentUnit(q=True, time=True),
             "range": [cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)],
             "values": [round(cmds.getAttr(p), 4) for p in watch]}
    cmds.setAttr(ns + "FKShoulder_L.rotateZ", 0)
    report = {"raised": raised, "restored": before == after, "before": before, "after": after,
              "source_left": cmds.namespace(exists=generic.SOURCE_NS),
              "constraints_left": len(generic._constraints())}
    STATE["report"]["failure"] = report
    return report


def finish():
    cleared = clear_keys()
    restored = adv.restore_backup(STATE["backup"]) if STATE.get("backup") else None
    STATE["report"]["finish"] = {"cleared_test_curves": cleared, "restore": restored,
                                 "unit": cmds.currentUnit(q=True, time=True)}
    path = Path(STATE["generic_fbx"]).parent / "stage1_report.json"
    path.write_text(json.dumps(STATE["report"], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    STATE["report"]["path"] = str(path)
    return STATE["report"]["finish"]
