"""Phase 0 measurements around flowhmr_bridge.adv.apply — the same code the bridge panel runs.

Run in a throwaway scene whose ADV rig has no animation:

    import sys
    sys.path.insert(0, r"<path to this repository>")
    import phase0_adv_test as p0
    p0.run(rig="char:Main")                                   # newest finished FlowHMR job
    p0.run(rig="Main", fbx=r"D:/clips/a.fbx", time_unit="ntscf")
    p0.snapshot([61, 181], name="check")                      # playblast stills after a run

Writes phase0_report.json next to the staged FBX: retarget/direction error while connected,
drift from the bake, drift from the frame-rate change.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import maya.cmds as cmds

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from flowhmr_bridge import adv  # noqa: E402
from flowhmr_bridge.client import FlowHMRClient  # noqa: E402
from flowhmr_bridge.timing import scene_fps  # noqa: E402

SOURCE = f"{adv.MOCAP_NS}:"
# ADV deform joint -> FlowHMR maya_adv source joint, as ADV's MoCapLibrary pairs them.
PAIRS = {"Root_M": "Hips", "Chest_M": "Spine3", "Neck_M": "Neck", "Head_M": "Head"}
for _s, _side in (("R", "Right"), ("L", "Left")):
    for _adv, _src in (("Hip", "UpLeg"), ("Knee", "Leg"), ("Ankle", "Foot"), ("Toes", "ToeBase"),
                       ("Shoulder", "Arm"), ("Elbow", "ForeArm"), ("Wrist", "Hand")):
        PAIRS[f"{_adv}_{_s}"] = f"{_side}{_src}"
SEGMENTS = [("Root_M", "Chest_M"), ("Neck_M", "Head_M")] + [
    (f"{a}_{s}", f"{b}_{s}") for s in "RL"
    for a, b in (("Hip", "Knee"), ("Knee", "Ankle"), ("Shoulder", "Elbow"), ("Elbow", "Wrist"))]

_state = {}


def _frames(start, end, step):
    frames = list(range(int(math.ceil(start)), int(math.floor(end)) + 1, step))
    if frames[-1] != int(math.floor(end)):
        frames.append(int(math.floor(end)))
    return frames


def _sample(ns, times, with_source):
    rows = {}
    for t in times:
        cmds.currentTime(t, edit=True)
        row = {}
        for joint, source in PAIRS.items():
            if cmds.objExists(ns + joint):
                row[joint] = cmds.xform(ns + joint, q=True, ws=True, t=True)
            if with_source and cmds.objExists(SOURCE + source):
                row["src:" + joint] = cmds.xform(SOURCE + source, q=True, ws=True, t=True)
        rows[round(t, 6)] = row
    return rows


def _distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _angle(u, v):
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(x * x for x in v))
    if nu < 1e-6 or nv < 1e-6:
        return None
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v)) / (nu * nv)))))


def _stats(per):
    return {k: {"mean": round(sum(v) / len(v), 3), "max": round(max(v), 3)} for k, v in per.items() if v}


def _retarget_error(rows):
    per = {j: [] for j in PAIRS}
    for row in rows.values():
        for joint in PAIRS:
            if joint in row and "src:" + joint in row:
                per[joint].append(_distance(row[joint], row["src:" + joint]))
    return _stats(per)


def _direction_error(rows):
    per, rest = {}, {}
    for t, row in rows.items():
        for a, b in SEGMENTS:
            if not all(k in row for k in (a, b, "src:" + a, "src:" + b)):
                continue
            angle = _angle([y - x for x, y in zip(row[a], row[b])],
                           [y - x for x, y in zip(row["src:" + a], row["src:" + b])])
            if angle is None:
                continue
            if t < 0:
                rest[f"{a}->{b}"] = round(angle, 2)
            else:
                per.setdefault(f"{a}->{b}", []).append(angle)
    return {"frame_-1": rest, "motion": _stats(per)}


def _drift(before, after, scale=1.0):
    per = {j: [] for j in PAIRS}
    for t, row in before.items():
        other = after.get(round(t * scale, 6))
        if other is None:
            continue
        for joint in PAIRS:
            if joint in row and joint in other:
                per[joint].append(_distance(row[joint], other[joint]))
    return _stats(per)


def run(rig="Main", job_id=None, fbx=None, time_unit=None, sample_step=10):
    if fbx is None:
        client = FlowHMRClient()
        job_id = job_id or next(job["id"] for job in client.jobs() if job["usable"])
        fbx = client.download_fbx(job_id, adv.staging_root() / job_id)
    report, rows = {"job_id": job_id, "rig": rig}, {}

    def connected(ns):
        start, end = cmds.playbackOptions(q=True, min=True), cmds.playbackOptions(q=True, max=True)
        rows["frames"] = sorted({int(start), *_frames(max(start, 0), end, sample_step)})
        rows["connected"] = _sample(ns, rows["frames"], with_source=True)
        report["retarget_error_cm"] = _retarget_error(rows["connected"])
        report["direction_error_deg"] = _direction_error(rows["connected"])

    def baked(ns):
        rows["baked"] = _sample(ns, rows["frames"], with_source=False)
        report["bake_drift_cm"] = _drift(rows["connected"], rows["baked"])

    result = adv.apply(rig, fbx, target_unit=time_unit, on_connected=connected, on_baked=baked)
    ratio = scene_fps() / 30.0
    restored = _sample(result["namespace"], [t * ratio for t in rows["frames"]], with_source=False)
    report["restore_drift_cm"] = _drift(rows["baked"], restored, scale=ratio)
    report["flips_after"] = adv.count_flips(adv.rotate_curves(result["namespace"]))
    report["apply"] = result
    folder = Path(result["fbx"]).parent
    _state.update(ns=result["namespace"], folder=folder)
    path = folder / "phase0_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report


def snapshot(frames, name="pose", size=(960, 540), fit=None):
    """Playblast stills through a temporary camera; the panel camera is put back."""
    ns, folder = _state["ns"], _state["folder"]
    fit = fit or [ns + "Group"]
    panel = next((p for p in cmds.getPanel(visiblePanels=True) or []
                  if cmds.getPanel(typeOf=p) == "modelPanel"), None)
    if panel is None:
        raise RuntimeError("No visible model panel for a playblast.")
    original = cmds.modelPanel(panel, q=True, camera=True)
    camera = cmds.camera(name="flowhmrPhase0Cam")[0]
    paths = []
    try:
        cmds.lookThru(panel, camera)
        for frame in frames:
            cmds.currentTime(frame, edit=True)
            cmds.select(fit, replace=True)
            cmds.viewFit(camera, fitFactor=0.9)
            cmds.select(clear=True)
            out = folder / f"{name}_{frame}.png"
            cmds.playblast(frame=[frame], format="image", compression="png", completeFilename=str(out),
                           viewer=False, showOrnaments=False, offScreen=True, percent=100,
                           widthHeight=size, forceOverwrite=True)
            paths.append(str(out))
    finally:
        cmds.lookThru(panel, original)
        cmds.delete(camera)
    return paths
