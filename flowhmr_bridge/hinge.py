"""Single-axis elbows and knees: rewrite a baked FK limb so the middle control (elbow, knee)
bends on one axis alone, as animators key it (Z on AdvancedSkeleton rigs).

The middle control keeps only its bend axis. What its other two axes did becomes a roll of the
start control (upper arm, thigh) around the upper bone plus the bend, so the three joints stay where
they were and the end control (wrist, ankle) keeps its world rotation: hand and foot positions and
orientations are unchanged, only the twist along the two bones is shared out differently (by the rig).
The bend axis is the control axis the limb actually bends around in the motion (bend_axis): Z on
ADV, Y on the Hive test rig's elbows.

At a small bend the roll that keeps the middle joint exactly in place is ill-conditioned: HMR's
sideways jitter turns into roll swings of tens of degrees between frames. There the roll follows
its neighbourhood (eased_rolls) and the whole limb is also turned about the start-to-end joint line
(_ease): start and end joints stay exact, the middle joint moves a little on its circle.

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
REST_SHARE = 0.9  # an unclear motion is settled by the rig's own pre-bend if it is this clear
TRUST_BEND = 30.0  # degrees: from this bend on a frame keeps its own (exact) roll
DATA_WEIGHT = 1.0  # how hard a frame just under TRUST_BEND holds its own roll against its neighbours
ROLL_NOISE = 1.0  # degrees of HMR's sideways jitter; the roll it causes grows as 1 / sin(bend)
SHIFT_SHARE = 0.05  # safety bound: easing moves the middle joint at most this share of the limb length
LIMBS = {  # log words: joint, upper bone, lower bone, what stays unchanged
    "arm": ("手肘", "上臂", "前臂", "肩、腕位置和手掌方向"),
    "leg": ("膝蓋", "大腿", "小腿", "髖、腳踝位置和腳掌方向"),
}


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
    return {"xy": max(others), "roll": roll, "error": error}  # roll: signed degrees about the upper bone


def _swivel(controls, joints, angle):
    """Turn the whole limb by ``angle`` degrees about the line from the start to the end joint:
    those two stay, the middle joint moves on its circle and the middle control keeps its single
    axis. The end control keeps its world rotation."""
    start, _, end = controls
    p_s, _, p_e = [_pos(n) for n in joints]
    line = p_e - p_s
    if line.length() < 1e-9 or abs(angle) < 1e-9:
        return
    end_world = _rotation(end)
    end_ref = cmds.getAttr(end + ".rotate")[0]
    turn = om.MQuaternion(math.radians(angle), line.normal()).asMatrix()
    set_world_rotation(start, _rotation(start) * turn, cmds.getAttr(start + ".rotate")[0])
    set_world_rotation(end, end_world, end_ref)


def _smooth_segment(values, weights, left, right):
    """Minimise sum w (x - value)^2 + sum (x[i] - x[i-1])^2 over one run of frames, with the
    frames just outside it fixed at ``left`` / ``right`` (None at the ends of the clip): the
    tridiagonal system, solved directly (Thomas)."""
    n = len(values)
    lower, diag, upper, rhs = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(n):
        diag[i] = weights[i] + 1e-9
        rhs[i] = weights[i] * values[i]
        for side, fixed in ((-1, left), (1, right)):
            j = i + side
            if 0 <= j < n:
                diag[i] += 1.0
                (lower if side < 0 else upper)[i] = -1.0
            elif fixed is not None:
                diag[i] += 1.0
                rhs[i] += fixed
    for i in range(1, n):
        m = lower[i] / diag[i - 1]
        diag[i] -= m * upper[i - 1]
        rhs[i] -= m * rhs[i - 1]
    x = [0.0] * n
    x[-1] = rhs[-1] / diag[-1]
    for i in range(n - 2, -1, -1):
        x[i] = (rhs[i] - upper[i] * x[i + 1]) / diag[i]
    return x


def eased_rolls(rolls, bends):
    """The roll each frame should end up with. Frames bent at least TRUST_BEND keep their own
    (exact) roll. Below that, HMR's sideways jitter turns into large roll swings (the roll error
    grows as 1 / sin(bend)), so each run of such frames is smoothed: a frame holds its own roll with
    a weight going as sin^2 of its bend, against a pull towards its neighbours, so a stretch that
    stays straight becomes a line between the bent frames around it. A frame whose roll is far
    beyond the jitter its bend explains (a one-frame glitch) is then weighted down and the run
    solved again."""
    n = len(rolls)
    base = [DATA_WEIGHT * (math.sin(math.radians(b)) / math.sin(math.radians(TRUST_BEND))) ** 2 for b in bends]
    result = list(rolls)
    i = 0
    while i < n:
        if bends[i] >= TRUST_BEND:
            i += 1
            continue
        j = i
        while j < n and bends[j] < TRUST_BEND:
            j += 1
        left = rolls[i - 1] if i > 0 else None
        right = rolls[j] if j < n else None
        values, weights = rolls[i:j], base[i:j]
        if left is None and right is None and sum(weights) < 1e-6:
            result[i:j] = [0.0] * (j - i)  # never bends: no roll at all
            i = j
            continue
        x = _smooth_segment(values, weights, left, right)
        for _ in range(2):
            noise = [3.0 * ROLL_NOISE / max(math.sin(math.radians(b)), 1e-3) for b in bends[i:j]]
            weights = [w / (1.0 + ((v - s) / e) ** 2) for w, v, s, e in zip(base[i:j], values, x, noise)]
            x = _smooth_segment(values, weights, left, right)
        result[i:j] = x
        i = j
    return result


def _ease(controls, joints, targets, roll, wanted):
    """Swivel the cleaned limb so its upper-bone roll goes from ``roll`` towards ``wanted``
    (degrees), moving the middle joint at most SHIFT_SHARE of the limb length. Returns the swivel
    in degrees."""
    p_s, p_m, p_e = targets
    upper, lower, line = p_m - p_s, p_e - p_m, p_e - p_s
    if upper.length() < 1e-9 or line.length() < 1e-9:
        return 0.0
    a, e = upper.normal(), line.normal()
    # Roll about a then swivel about e: the swivel that leaves the rotation closest to ``wanted``
    # about a (quaternion w of swivel * roll(delta), maximised).
    delta = math.radians(roll - wanted)
    angle = -2.0 * math.atan((e * a) * math.tan(delta / 2.0))
    radius = (upper - e * (upper * e)).length()  # middle joint from the start-end line
    limit = SHIFT_SHARE * (upper.length() + lower.length())
    if radius > 1e-9 and 2.0 * radius > limit:
        cap = 2.0 * math.asin(limit / (2.0 * radius))
        angle = max(-cap, min(cap, angle))
    _swivel(controls, joints, math.degrees(angle))
    return math.degrees(angle)


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


def rest_axis(controls, joints):
    """The axis of the rig's own pre-bend (middle control at rotate 0), in the same frame as
    bend_axis. Returns (axis or None, share, bend in degrees)."""
    middle = controls[1]
    saved = cmds.getAttr(middle + ".rotate")[0]
    try:
        cmds.setAttr(middle + ".rotate", 0.0, 0.0, 0.0)
        p_s, p_m, p_e = [_pos(n) for n in joints]
        upper, lower = p_m - p_s, p_e - p_m
        if upper.length() < 1e-9 or lower.length() < 1e-9:
            return None, 0.0, 0.0
        bend = math.degrees(upper.angle(lower))
        normal = upper ^ lower
        if normal.length() < 1e-9:
            return None, 0.0, bend
        normal = normal.normal() * _parent_inverse(middle)
    finally:
        cmds.setAttr(middle + ".rotate", *saved)
    values = [abs(normal.x), abs(normal.y), abs(normal.z)]
    index = max(range(3), key=values.__getitem__)
    return "xyz"[index], values[index] / sum(values), bend


def _axis(limb, controls, joints, frames, axis):
    """(axis to clean with, None) or (None, why not)."""
    if axis:
        return axis, None
    found, share = bend_axis(controls, joints, frames)
    if found is not None and share >= AXIS_SHARE:
        return found, None
    # HMR's knees also bend sideways, so the motion alone can be unclear, and a standing clip may
    # never bend enough to tell: then the rig's own pre-bend decides.
    rest, rest_share, rest_bend = rest_axis(controls, joints)
    clear = rest is not None and rest_share >= REST_SHARE and rest_bend >= 0.5
    if found is None:
        if clear:
            return rest, None
        return None, f"{LIMBS[limb][0]}在這段動作裡彎得不夠多，rig 本身也沒有明確的預彎，判斷不出彎曲軸"
    if clear and rest == found:
        return found, None
    return None, f"{LIMBS[limb][0]}的彎曲軸不明確（{found.upper()} 只佔 {share:.0%}）"


def _key(frame, nodes):
    for node in nodes:
        for a in "XYZ":
            cmds.setKeyframe(node, attribute="rotate" + a, time=frame, value=cmds.getAttr(f"{node}.rotate{a}"))


def clean_baked(chains, frames, axis=None):
    """Rewrite baked FK limbs frame by frame and key the result. ``chains``: [(label, limb,
    controls, joints)] with limb "arm" or "leg"; ``axis`` forces the bend axis (else bend_axis).
    The bake keys every frame, so a key written at one frame does not change the pose read at the
    next. Returns {label: stats}."""
    report = {}
    previous = cmds.currentTime(q=True)
    try:
        for label, limb, controls, joints in chains:
            bend_on, why = _axis(limb, controls, joints, frames, axis)
            if why:
                report[label] = {"limb": limb, "skipped": why}
                continue
            cleaned, xy, error = 0, 0.0, 0.0
            rolls, bends, targets = [], [], []
            for frame in frames:
                cmds.currentTime(frame, update=True)
                points = [_pos(n) for n in joints]
                targets.append(points)
                bends.append(math.degrees((points[1] - points[0]).angle(points[2] - points[1])))
                result = clean_pose(controls, joints, bend_on)
                rolls.append(result["roll"] if result else 0.0)
                if result is None:
                    continue
                _key(frame, controls)
                cleaned += 1
                xy, error = max(xy, result["xy"]), max(error, result["error"])
            # Near straight: swivel the limb so the roll follows the bent frames (see _ease).
            wanted = eased_rolls(rolls, bends)
            eased, shift, final = 0, 0.0, list(rolls)
            for n, frame in enumerate(frames):
                if abs(wanted[n] - rolls[n]) < 1e-3:
                    continue
                cmds.currentTime(frame, update=True)
                if abs(_ease(controls, joints, targets[n], rolls[n], wanted[n])) < 1e-6:
                    continue
                _key(frame, (controls[0], controls[2]))
                eased += 1
                final[n] = wanted[n]
                p_s, p_m, p_e = [_pos(j) for j in joints]
                shift = max(shift, (p_m - targets[n][1]).length())
                error = max(error, (p_s - targets[n][0]).length(), (p_e - targets[n][2]).length())
            report[label] = {"limb": limb, "axis": bend_on, "middle": controls[1], "joints": list(joints), "frames": cleaned,
                             "max_xy": round(xy, 2), "max_roll": round(max((abs(r) for r in final), default=0.0), 2),
                             "max_error_cm": round(error, 5), "eased_frames": eased, "max_shift_cm": round(shift, 3)}
    finally:
        cmds.currentTime(previous, update=True)
    return report


def describe(report):
    """Log lines, one per limb kind: what changed and what stays."""
    lines = []
    for limb, (joint, upper, lower, kept) in LIMBS.items():
        entries = [(label, r) for label, r in (report or {}).items() if r.get("limb") == limb]
        if not entries:
            continue
        parts = []
        for label, r in entries:
            if "skipped" in r:
                parts.append(f"{label}沒有修正：{r['skipped']}")
                continue
            text = (f"{label}只轉 {r['axis'].upper()}，{r['frames']} 幀（其他兩軸最多 {r['max_xy']}°，"
                    f"{upper}多轉最多 {r['max_roll']}°，位置誤差 {r['max_error_cm']} cm")
            if r.get("eased_frames"):
                text += f"；彎得少的 {r['eased_frames']} 幀{joint}最多移動 {r['max_shift_cm']} cm，避免{upper}扭轉跳動"
            parts.append(text + "）")
        line = f"{joint}只轉單軸：" + "；".join(parts) + "。"
        if any("skipped" not in r for _, r in entries):
            line += f"{upper}、{lower}的扭轉改由 rig 分配，和預覽不同；{kept}不變。"
        lines.append(line)
    return "\n".join(lines)


def cleaned(report):
    """Any limb actually rewritten."""
    return any("skipped" not in r for r in (report or {}).values())
