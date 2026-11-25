"""Rig-agnostic checks for flowhmr_bridge.generic, driven by a profile's "test" section.

Run in a throwaway scene whose rig has no animation:

    import sys; sys.path.insert(0, r"<path to this repository>")
    import stage_generic_test as st
    st.prepare("<FlowHMR job id>", "zoo_hive_biped", rig_ns="char:")
    st.run("profile")        # rig rest as the profile says
    st.run("arms_down")      # rig rest with both FK shoulders turned down 50 degrees
    st.failure_test()
    st.run("profile", time_unit="ntscf")   # leave a result to look at
    st.snapshot([0, 61, 151, 241])
    st.finish()              # writes stage_report_<profile>.json
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

STATE = {}


def _sides(profile, text):
    sides = profile.get("sides") or [["", ""]]
    if "{s}" not in text and "{Side}" not in text:
        return [("", "")]
    return [tuple(side) for side in sides]


def _expand_test(profile):
    test = profile["test"]
    pairs, segments = {}, []
    for joint, src in test["pairs"].items():
        for short, word in _sides(profile, joint + src):
            pairs[generic._expand_text(joint, short, word)] = generic._expand_text(src, short, word)
    for a, b in test["segments"]:
        for short, word in _sides(profile, a + b):
            segments.append((generic._expand_text(a, short, word), generic._expand_text(b, short, word)))
    return pairs, segments


def prepare(job_id, profile_name, rig_ns):
    profile = generic.load_profile(profile_name)
    pairs, segments = _expand_test(profile)
    root, chains, hold = generic.expand(profile)
    wanted = [root["control"]] + [c for chain in chains for _, c in chain["links"]]
    controls = generic.mapped_controls(profile, rig_ns)
    animated = generic._animated(controls)
    if animated:
        raise RuntimeError(f"{len(animated)} mapped controls already have keys; use a clean scene.")
    fbx = FlowHMRClient().download_fbx(job_id, adv.staging_root() / f"{job_id}_flowhmr_t",
                                       profile="flowhmr", mesh=0, first_pose="t")
    STATE.clear()
    STATE.update(ns=rig_ns, profile=profile, name=profile_name, pairs=pairs, segments=segments,
                 controls=controls, fbx=fbx, rows={}, report={})
    STATE["report"]["prepare"] = {
        "controls": len(controls), "missing_controls": sorted({rig_ns + c for c in wanted} - set(controls)),
        "missing_measure_joints": [j for j in pairs if not cmds.objExists(rig_ns + j)],
        "hold": {plug: cmds.getAttr(rig_ns + plug) for plug in hold if cmds.objExists(rig_ns + plug)},
        "unit": cmds.currentUnit(q=True, time=True)}
    return STATE["report"]["prepare"]


def _pos(node):
    return cmds.xform(node, q=True, ws=True, translation=True)


def _angle(u, v):
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
    if nu < 1e-6 or nv < 1e-6:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(u, v)) / (nu * nv)))))


def _vec(row, a, b):
    return [y - x for x, y in zip(row[a], row[b])] if a in row and b in row else None


def _stats(values):
    values = [v for v in values if v is not None]
    return {"mean": round(sum(values) / len(values), 2), "max": round(max(values), 2)} if values else None


def sample(frames, with_source):
    ns, rows = STATE["ns"], {}
    for frame in frames:
        cmds.currentTime(frame, edit=True)
        row = {j: _pos(ns + j) for j in STATE["pairs"] if cmds.objExists(ns + j)}
        if with_source:
            row.update({"src:" + j: _pos(generic.source(s)) for j, s in STATE["pairs"].items()
                        if cmds.objExists(generic.source(s))})
        rows[frame] = row
    return rows


def errors_vs_source(rows):
    """Rig bone direction vs source bone direction. Only limb and finger bones are aligned; a
    segment that starts at a generic.KEEP_REST bone (spine, collar, foot) shows the rig's own
    rest offset from the source here by design, not a retarget error."""
    rest, motion = {}, {}
    for frame, row in rows.items():
        for a, b in STATE["segments"]:
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
        for a, b in STATE["segments"]:
            u, v = _vec(row, a, b), _vec(other or {}, a, b)
            if u and v:
                per.setdefault(f"{a}>{b}", []).append(_angle(u, v))
    return {k: _stats(v) for k, v in per.items()}


def drift(rows_a, rows_b):
    worst = 0.0
    for frame, row in rows_a.items():
        for joint, value in row.items():
            if not joint.startswith("src:") and joint in rows_b.get(frame, {}):
                worst = max(worst, math.dist(value, rows_b[frame][joint]))
    return round(worst, 4)


def clear_keys():
    """Remove this rig's FlowHMR layers (each apply bakes onto one), then any plain keys."""
    from flowhmr_bridge import layers
    for layer in reversed(layers.flowhmr_layers(STATE["ns"])):
        layers.remove_layer(layer, restore_range=False)
    curves = set()
    for control in STATE["controls"]:
        for curve in cmds.listConnections(control, s=True, d=False, type="animCurve") or []:
            if cmds.nodeType(curve).startswith("animCurveT"):
                curves.add(curve)
    if curves:
        cmds.delete(list(curves))
    return len(curves)


def _zero():
    for control in STATE["controls"]:
        for plug in generic._channels(control, "parent"):
            cmds.setAttr(plug, 0)


def run(rest="profile", time_unit=None, frames=range(0, 151, 10)):
    clear_keys()
    _zero()
    ns, profile = STATE["ns"], STATE["profile"]
    if rest == "arms_down":
        profile = copy.deepcopy(profile)
        profile["rest"] = "current"
        _, _, hold = generic.expand(profile)
        for plug, value in hold.items():
            cmds.setAttr(ns + plug, value)
        cmds.currentTime(-1, edit=True)
        arms = profile["test"]["arm_rest_controls"]
        cmds.rotate(0, 0, -50, ns + arms["L"], relative=True, worldSpace=True)
        cmds.rotate(0, 0, 50, ns + arms["R"], relative=True, worldSpace=True)
    connected, baked, logs = {}, {}, []
    frames = [-1] + list(frames)
    result = generic.apply(ns, profile, STATE["fbx"], target_unit=time_unit, log=logs.append,
                           on_connected=lambda _: connected.update(sample(frames, True)),
                           on_baked=lambda _: baked.update(sample(frames, False)))
    STATE["rows"][rest] = baked
    report = {"result": {k: result[k] for k in ("driven_controls", "calibration", "euler_flips_fixed", "keys",
                                                  "fps", "seconds")},
              "vs_source": errors_vs_source(connected),
              "bake_drift_cm": drift({f: r for f, r in connected.items()}, baked), "log": logs}
    if rest != "profile" and "profile" in STATE["rows"]:
        report["vs_profile_rest"] = compare({f: r for f, r in baked.items() if f >= 0}, STATE["rows"]["profile"])
    STATE["report"][f"run_{rest}" + (f"_{time_unit}" if time_unit else "")] = report
    if rest == "arms_down":
        clear_keys()
        _zero()
    return report


def failure_test():
    clear_keys()
    _zero()
    ns, profile = STATE["ns"], STATE["profile"]
    _, _, hold = generic.expand(profile)
    hold_plug = ns + next(iter(hold))
    probe = STATE["controls"][1] + ".rotateZ"
    cmds.setAttr(probe, 12.5)
    watch = [hold_plug, probe, STATE["controls"][0] + ".translateY"]

    def state():
        return {"curves": len(cmds.ls(type="animCurve")), "unit": cmds.currentUnit(q=True, time=True),
                "range": [cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)],
                "values": [round(cmds.getAttr(p), 4) for p in watch]}

    before = state()

    def boom(_):
        raise RuntimeError("generic failure test")

    try:
        generic.apply(ns, profile, STATE["fbx"], log=lambda *_: None, on_baked=boom)
        raised = False
    except adv.ApplyError as error:
        raised = str(error)
    after = state()
    cmds.setAttr(probe, 0)
    report = {"raised": raised, "restored": before == after, "before": before, "after": after,
              "source_left": cmds.namespace(exists=generic.SOURCE_NS), "constraints_left": len(generic._constraints())}
    STATE["report"]["failure"] = report
    return report


def snapshot(frames, name="pose", size=(960, 540), fit=None):
    ns = STATE["ns"]
    fit = fit or [ns + c for c in STATE["profile"]["test"].get("fit", [])] or STATE["controls"]
    folder = Path(STATE["fbx"]).parent
    panel = next((p for p in cmds.getPanel(visiblePanels=True) or [] if cmds.getPanel(typeOf=p) == "modelPanel"), None)
    if panel is None:
        raise RuntimeError("No visible model panel for a playblast.")
    original = cmds.modelPanel(panel, q=True, camera=True)
    camera = cmds.camera(name="flowhmrStageCam")[0]
    cmds.setAttr(camera + ".rotate", -8, 25, 0)
    paths = []
    try:
        cmds.lookThru(panel, camera)
        for frame in frames:
            cmds.currentTime(frame, edit=True)
            cmds.select(fit, replace=True)
            cmds.viewFit(camera, fitFactor=0.7)
            cmds.select(clear=True)
            out = folder / f"{STATE['name']}_{name}_{frame}.png"
            cmds.playblast(frame=[frame], format="image", compression="png", completeFilename=str(out), viewer=False,
                           showOrnaments=False, offScreen=True, percent=100, widthHeight=size, forceOverwrite=True)
            paths.append(str(out))
    finally:
        cmds.lookThru(panel, original)
        cmds.delete(camera)
    return paths


def finish():
    path = Path(STATE["fbx"]).parent / f"stage_report_{STATE['name']}.json"
    path.write_text(json.dumps(STATE["report"], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path)
