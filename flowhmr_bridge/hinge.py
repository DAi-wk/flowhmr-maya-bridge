"""Single-axis elbows: rewrite a baked FK arm so the forearm control bends on one axis alone, as
animators key it (Z on AdvancedSkeleton rigs).

The middle control (elbow) keeps only its bend axis. What its other two axes did becomes a roll of
the start control (upper arm) around the upper bone plus the bend, so the three joints stay where
they were and the end control (wrist) keeps its world rotation: arm positions and the hand are
unchanged, only the twist along the upper arm and forearm is shared out differently (by the rig).
The bend axis is the control axis the arm actually bends around in the motion (bend_axis): Z on
ADV, Y on the Hive test rig.

The solve follows clean_middle_xy of the standalone IK/FK switch tool for AdvancedSkeleton rigs
(generalised from Z to any axis): it only reads and writes the controls and joints given, no rig
scripts.
"""
from __future__ import annotations

import math

import maya.api.OpenMaya as om
import maya.cmds as cmds

XY_EPS = 1e-4  # degrees: a middle control already within this of single-axis is left alone
AXIS_SHARE = 0.6  # the bend axis must carry this share of the bend plane normal (bend_axis)


# ------------------------------------------------------------------ matrices
def _world(node):
    return om.MMatrix(cmds.xform(node, q=True, ws=True, matrix=True))


def _orthonormal(matrix):
    """Rotation basis without scale, translation kept."""
    x = om.MVector(matrix[0], matrix[1], matrix[2])
    y = om.MVector(matrix[4], matrix[5], matrix[6])
    if x.length() < 1e-9 or y.length() < 1e-9:
        return matrix
    x = x.normal()
    z = x ^ y
    if z.length() < 1e-9:
        return matrix
    z = z.normal()
    y = (z ^ x).normal()
    return om.MMatrix([x.x, x.y, x.z, 0.0, y.x, y.y, y.z, 0.0, z.x, z.y, z.z, 0.0, matrix[12], matrix[13], matrix[14], 1.0])


def _rotation(node):
    matrix = list(_orthonormal(_world(node)))
    matrix[12:15] = [0.0, 0.0, 0.0]
    return om.MMatrix(matrix)


def _parent_inverse(node):
    # From the DAG: under parallel evaluation .parentInverseMatrix can lag a setAttr on an ancestor.
    selection = om.MSelectionList()
    selection.add(node)
    return selection.getDagPath(0).exclusiveMatrixInverse()


def set_world_rotation(node, world, reference=None):
    """Write ``node``'s rotate so its world rotation becomes ``world`` (jointOrient included),
    taking the Euler solution closest to ``reference`` (degrees) so curves do not jump 360."""
    local = world * _parent_inverse(node)
    if cmds.nodeType(node) == "joint":
        orient = [math.radians(v) for v in cmds.getAttr(node + ".jointOrient")[0]]
        local = local * om.MEulerRotation(orient).asMatrix().inverse()
    order = cmds.getAttr(node + ".rotateOrder")
    euler = om.MTransformationMatrix(_orthonormal(local)).rotation()
    if euler.order != order:
        euler.reorderIt(order)
    if reference is not None:
        euler.setToClosestSolution(om.MEulerRotation(*[math.radians(v) for v in reference], order))
    cmds.setAttr(node + ".rotate", math.degrees(euler.x), math.degrees(euler.y), math.degrees(euler.z))


def _axes(node):
    m = _rotation(node)
    return om.MVector(m[0], m[1], m[2]), om.MVector(m[4], m[5], m[6]), om.MVector(m[8], m[9], m[10])


def _rotate_vector(v, axis, angle):
    """Rodrigues rotation of ``v`` around the unit ``axis``."""
    c, s = math.cos(angle), math.sin(angle)
    return v * c + (axis ^ v) * s + axis * ((axis * v) * (1.0 - c))


def _wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def _pos(node):
    return om.MVector(cmds.xform(node, q=True, ws=True, translation=True))


# ------------------------------------------------------------------ solve
def _pass(controls, joints, targets, ref, k):
    """One analytic step: roll the start control, leave the middle control only axis ``k``
    (0, 1, 2 = X, Y, Z). Bending about k turns axis i towards axis j (right-handed)."""
    i, j = (k + 1) % 3, (k + 2) % 3
    start, middle, _ = controls
    p_s, p_m, p_e = [_pos(n) for n in joints]
    goal = targets[2] - p_m
    v = p_e - p_m
    d = [v * axis for axis in _axes(middle)]  # end joint in the middle control's frame: fixed while it rotates
    a = p_m - p_s
    if a.length() < 1e-9:
        raise RuntimeError(f"{start} 的起點和中段關節重疊。")
    a = a.normal()
    cmds.setAttr(middle + ".rotate", 0.0, 0.0, 0.0)  # the frame the single-axis bend happens in
    axes = _axes(middle)
    pk = axes[k]
    # Roll phi around the upper bone so the bend axis leaves the goal with the same
    # out-of-plane part the end joint has in the middle frame (d[k]).
    pk_par = a * (pk * a)
    pk_perp = pk - pk_par
    A, B, C = pk_perp * goal, (a ^ pk_perp) * goal, pk_par * goal
    radius = math.hypot(A, B)
    candidates = [0.0]
    if radius > 1e-9:
        base = math.atan2(B, A)
        offset = math.acos(max(-1.0, min(1.0, (d[k] - C) / radius)))
        candidates = [_wrap(base + offset), _wrap(base - offset)]
    best = None
    for phi in candidates:
        qi, qj = _rotate_vector(axes[i], a, phi), _rotate_vector(axes[j], a, phi)
        theta = math.degrees(math.atan2(goal * qj, goal * qi) - math.atan2(d[j], d[i]))
        theta = ref + math.degrees(_wrap(math.radians(theta - ref)))
        cost = abs(phi) + abs(math.radians(theta - ref))  # the other root bends the joint backwards
        if best is None or cost < best[0]:
            best = (cost, phi, theta)
    _, phi, theta = best
    world = _rotation(start)
    rows = [_rotate_vector(om.MVector(world[n * 4], world[n * 4 + 1], world[n * 4 + 2]), a, phi) for n in range(3)]
    rolled = om.MMatrix([rows[0].x, rows[0].y, rows[0].z, 0.0, rows[1].x, rows[1].y, rows[1].z, 0.0,
                         rows[2].x, rows[2].y, rows[2].z, 0.0, 0.0, 0.0, 0.0, 1.0])
    set_world_rotation(start, rolled, cmds.getAttr(start + ".rotate")[0])
    values = [0.0, 0.0, 0.0]
    values[k] = theta
    cmds.setAttr(middle + ".rotate", *values)
    return math.degrees(phi)


def clean_pose(controls, joints, axis="z"):
    """Leave the middle control only its bend axis (default Z) at the current time.
    ``controls``: (start, middle, end) FK controls; ``joints``: the three joints they move, whose
    world positions are kept. Returns {"xy", "roll", "error"} or None when it already was."""
    k = "xyz".index(axis)
    start, middle, end = controls
    r = cmds.getAttr(middle + ".rotate")[0]
    others = [abs(r[n]) for n in range(3) if n != k]
    if max(others) < XY_EPS:
        return None
    targets = [_pos(n) for n in joints]
    end_world = _rotation(end)
    end_ref = cmds.getAttr(end + ".rotate")[0]
    roll = 0.0
    for _ in range(3):
        roll += _pass(controls, joints, targets, r[k], k)
        if max((_pos(n) - t).length() for n, t in zip(joints, targets)) < 1e-5:
            break
    set_world_rotation(end, end_world, end_ref)
    error = max((_pos(n) - t).length() for n, t in zip(joints, targets))
    return {"xy": max(others), "roll": abs(roll), "error": error}


def usable(controls):
    """Why the chain cannot be cleaned, or None: every rotate channel must be free and the
    scale uniform (a non-uniform scale would bend the solve)."""
    for node in controls:
        if not cmds.objExists(node):
            return f"找不到 {node}"
        for axis in "XYZ":
            plug = f"{node}.rotate{axis}"
            if cmds.getAttr(plug, lock=True):
                return f"{plug} 鎖住了"
        scale = cmds.getAttr(node + ".scale")[0]
        if min(abs(v) for v in scale) < 1e-8 or max(scale) - min(scale) > 1e-5:
            return f"{node} 用了非等比縮放"
    return None


def bend_axis(controls, joints, frames, min_bend=20.0):
    """The middle control's own axis the limb actually bends around in this motion: the bend
    plane normal (upper bone x lower bone) in the control's parent frame, summed over frames bent
    more than ``min_bend`` degrees. Returns ("x"|"y"|"z" or None, share of the dominant axis)."""
    middle = controls[1]
    total = om.MVector(0, 0, 0)
    previous = cmds.currentTime(q=True)
    try:
        for frame in frames[::max(1, len(frames) // 60)]:
            cmds.currentTime(frame, update=True)
            p_s, p_m, p_e = [_pos(n) for n in joints]
            upper, lower = p_m - p_s, p_e - p_m
            if upper.length() < 1e-9 or lower.length() < 1e-9:
                continue
            if math.degrees(upper.angle(lower)) < min_bend:
                continue
            normal = (upper ^ lower).normal() * _parent_inverse(middle)
            normal = om.MVector(normal.x, normal.y, normal.z)
            total += om.MVector(abs(normal.x), abs(normal.y), abs(normal.z))
    finally:
        cmds.currentTime(previous, update=True)
    if total.length() < 1e-9:
        return None, 0.0
    values = [total.x, total.y, total.z]
    index = max(range(3), key=values.__getitem__)
    return "xyz"[index], values[index] / sum(values)


def clean_baked(chains, frames):
    """Rewrite baked FK arms frame by frame and key the result. ``chains``: [(label, controls,
    joints)]. The bake keys every frame, so a key written at one frame does not change the pose
    read at the next. Returns {label: stats}."""
    report = {}
    previous = cmds.currentTime(q=True)
    try:
        for label, controls, joints in chains:
            axis, share = bend_axis(controls, joints, frames)
            if axis is None:
                axis = "z"  # never bent enough to tell: the ADV convention
            elif share < AXIS_SHARE:
                report[label] = {"skipped": f"手肘的彎曲軸不明確（{axis.upper()} 只佔 {share:.0%}）"}
                continue
            plugs = [f"{c}.rotate{a}" for c in controls for a in "XYZ"]
            cleaned, xy, roll, error = 0, 0.0, 0.0, 0.0
            for frame in frames:
                cmds.currentTime(frame, update=True)
                result = clean_pose(controls, joints, axis)
                if result is None:
                    continue
                for plug in plugs:
                    node, attr = plug.rsplit(".", 1)
                    cmds.setKeyframe(node, attribute=attr, time=frame, value=cmds.getAttr(plug))
                cleaned += 1
                xy, roll, error = max(xy, result["xy"]), max(roll, result["roll"]), max(error, result["error"])
            report[label] = {"axis": axis, "middle": controls[1], "frames": cleaned, "max_xy": round(xy, 2), "max_roll": round(roll, 2),
                             "max_error_cm": round(error, 5)}
    finally:
        cmds.currentTime(previous, update=True)
    return report


def describe(report):
    parts = [f"{label}沒有修正：{r['skipped']}" if "skipped" in r else
             f"{label}只轉 {r['axis'].upper()}，{r['frames']} 幀（其他兩軸最多 {r['max_xy']}°，上臂多轉最多 {r['max_roll']}°，"
             f"位置誤差 {r['max_error_cm']} cm）"
             for label, r in report.items()]
    return "手肘只轉單軸：" + "；".join(parts)


def cleaned(report):
    """Any arm actually rewritten (for the follow-up note in the log)."""
    return any("skipped" not in r for r in (report or {}).values())
