"""Mapping profiles as fixed body slots (pure Python, no Maya), for the custom-mapping editor.

A profile on disk is a list of chains (see generic.py). The editor works on one slot per
FlowHMR joint role instead; build_profile() turns slots into chains and
slots_from_profile() reads any profile back into slots.
"""
from __future__ import annotations

import json
import re

SIDES = (("L", "Left", "左"), ("R", "Right", "右"))
FINGERS = (("thumb", "Thumb", "拇指"), ("index", "Index", "食指"), ("middle", "Middle", "中指"),
           ("ring", "Ring", "無名指"), ("pinky", "Pinky", "小指"))


def _slots():
    slots = [
        # key, FlowHMR joint, label, group
        ("root", "Hips", "Root / COG（位移＋旋轉）", "軀幹"),
        ("pelvis", "Hips", "骨盆", "軀幹"),
        ("spine_base", "Hips", "脊椎底節（跟骨盆同一個來源）", "軀幹"),
        ("spine", "Spine", "脊椎 1", "軀幹"),
        ("spine1", "Spine1", "脊椎 2", "軀幹"),
        ("spine2", "Spine2", "胸", "軀幹"),
        ("neck", "Neck", "頸", "軀幹"),
        ("head", "Head", "頭", "軀幹"),
    ]
    for short, word, label in SIDES:
        slots += [
            (f"clavicle_{short}", f"{word}Shoulder", f"{label}鎖骨", f"{label}手臂"),
            (f"arm_{short}", f"{word}Arm", f"{label}上臂", f"{label}手臂"),
            (f"forearm_{short}", f"{word}ForeArm", f"{label}前臂", f"{label}手臂"),
            (f"hand_{short}", f"{word}Hand", f"{label}手腕", f"{label}手臂"),
            (f"handaim_{short}", f"{word}HandMiddle1", f"{label}手掌方向參考（中指根部的控制器，可留空）", f"{label}手臂"),
            (f"upleg_{short}", f"{word}UpLeg", f"{label}大腿", f"{label}腿"),
            (f"leg_{short}", f"{word}Leg", f"{label}小腿", f"{label}腿"),
            (f"foot_{short}", f"{word}Foot", f"{label}腳踝", f"{label}腿"),
            (f"toe_{short}", f"{word}ToeBase", f"{label}腳尖", f"{label}腿"),
        ]
        for key, finger, finger_label in FINGERS:
            for i in (1, 2, 3):
                slots.append((f"{key}{i}_{short}", f"{word}Hand{finger}{i}", f"{label}{finger_label} {i}", f"{label}手指"))
    # IK slots go last so chain assignment over the FK slots runs straight from arm to leg.
    for short, word, label in SIDES:
        slots += [
            (f"ikhand_{short}", f"{word}Hand", f"{label}IK 手腕控制器", f"{label}手臂"),
            (f"armpole_{short}", f"{word}ForeArm", f"{label}手肘 Pole", f"{label}手臂"),
            (f"ikfoot_{short}", f"{word}Foot", f"{label}IK 腳控制器", f"{label}腿"),
            (f"legpole_{short}", f"{word}Leg", f"{label}膝蓋 Pole", f"{label}腿"),
            (f"iktoe_{short}", f"{word}ToeBase", f"{label}IK 腳尖（可留空）", f"{label}腿"),
        ]
    return slots


SLOTS = _slots()
IK_KINDS = ("ikhand", "armpole", "ikfoot", "legpole", "iktoe")
IK_SLOTS = {f"{kind}_{s}" for kind in IK_KINDS for s, _, _ in SIDES}
SLOT_SOURCE = {key: source for key, source, _, _ in SLOTS}
SOURCE_SLOT = {source: key for key, source, _, _ in SLOTS if source != "Hips" and key not in IK_SLOTS}
# limb -> (start, mid, end joint words, IK control kind, pole kind, IK toe kind, label)
IK_LIMBS = {"arm": ("Arm", "ForeArm", "Hand", "ikhand", "armpole", None, "手臂"),
            "leg": ("UpLeg", "Leg", "Foot", "ikfoot", "legpole", "iktoe", "腿")}
LIMB_KEYS = [f"{limb}_{s}" for limb in IK_LIMBS for s, _, _ in SIDES]
SLOT_LABEL = {key: label for key, _, label, _ in SLOTS}
SLOT_ORDER = [key for key, _, _, _ in SLOTS]
# Like Unity Humanoid's required bones; the spine needs at least one of SPINE_SLOTS.
REQUIRED = {"root", "head"} | {f"{k}_{s}" for s, _, _ in SIDES
                               for k in ("arm", "forearm", "hand", "upleg", "leg", "foot")}
SPINE_SLOTS = ("spine", "spine1", "spine2")


def missing_required(slots):
    missing = [key for key in SLOT_ORDER if key in REQUIRED and not slots.get(key)]
    if not any(slots.get(key) for key in SPINE_SLOTS):
        missing.append("spine")
    return missing


# ------------------------------------------------------------------ profile chains
def _expand_text(text, short, word):
    return text.replace("{s}", short).replace("{Side}", word)


def expand(profile):
    """Resolve side placeholders: returns (root, chains, hold) with plain names."""
    sides = profile.get("sides") or [["", ""]]
    chains = []
    for chain in profile.get("chains", []):
        text = json.dumps(chain)
        variants = sides if "{s}" in text or "{Side}" in text else [["", ""]]
        for short, word in variants:
            chains.append({
                "name": _expand_text(chain["name"], short, word),
                "align_from": chain.get("align_from", 0),
                "links": [[_expand_text(s, short, word), _expand_text(c, short, word)] for s, c in chain["links"]],
                "end_aim": [_expand_text(x, short, word) for x in chain["end_aim"]] if chain.get("end_aim") else None,
            })
    hold = {}
    for plug, value in (profile.get("hold") or {}).items():
        for short, word in (sides if "{s}" in plug or "{Side}" in plug else [["", ""]]):
            hold[_expand_text(plug, short, word)] = value
    return profile["root"], chains, hold


def _variants(profile, item):
    text = json.dumps(item, ensure_ascii=False)
    sides = profile.get("sides") or [["", ""]]
    return sides if "{s}" in text or "{Side}" in text else [["", ""]]


def expand_ik(profile):
    """IK entries with plain names: {name, start, mid, end, control, pole, toe: [source, control] | None}."""
    entries = []
    for entry in profile.get("ik", []):
        for short, word in _variants(profile, entry):
            fix = lambda v: _expand_text(v, short, word) if isinstance(v, str) else v  # noqa: E731
            entries.append({"name": fix(entry["name"]), "start": fix(entry["start"]), "mid": fix(entry["mid"]),
                            "end": fix(entry["end"]), "control": fix(entry.get("control") or ""),
                            "pole": fix(entry.get("pole") or ""),
                            "toe": [fix(x) for x in entry["toe"]] if entry.get("toe") else None})
    return entries


def limb_settings(profile):
    """{"arm_L": {"mode": "fk"|"ik", "switch": "ctrl.attr", "fk": value, "ik": value}} with plain names."""
    settings = {}
    for key, value in (profile.get("limbs") or {}).items():
        for short, word in _variants(profile, {key: value}):
            settings[_expand_text(key, short, word)] = {k: _expand_text(v, short, word) if isinstance(v, str) else v
                                                        for k, v in value.items()}
    return settings


def switch_holds(profile):
    """Switch values for each limb's chosen mode, to merge into the hold values."""
    holds = {}
    for setting in limb_settings(profile).values():
        plug, mode = setting.get("switch"), setting.get("mode", "fk")
        if plug and setting.get(mode) not in (None, ""):
            holds[plug] = setting[mode]
    return holds


def _side_of(source):
    return "L" if source.startswith("Left") else "R" if source.startswith("Right") else None


def slots_from_profile(profile):
    """(assignments {slot: control}, rest, hold, extra keys to keep) from any profile."""
    root, chains, hold = expand(profile)
    slots = {"root": root["control"]}
    for chain in chains:
        for source, control in chain["links"]:
            if source == "Hips":
                key = "spine_base" if "spine" in chain["name"].lower() else "pelvis"
            else:
                key = SOURCE_SLOT.get(source)
            if key and key not in slots:
                slots[key] = control
        aim = chain.get("end_aim")
        if aim:
            key = SOURCE_SLOT.get(aim[0])
            side = key.rsplit("_", 1)[1] if key and "_" in key else None
            if side:
                slots.setdefault(f"handaim_{side}", aim[1])
    for entry in expand_ik(profile):
        side = _side_of(entry["end"])
        limb = "leg" if entry["end"].endswith("Foot") else "arm" if entry["end"].endswith("Hand") else None
        if not side or not limb:
            continue
        _, _, _, ik_kind, pole_kind, toe_kind, _ = IK_LIMBS[limb]
        if entry["control"]:
            slots.setdefault(f"{ik_kind}_{side}", entry["control"])
        if entry["pole"]:
            slots.setdefault(f"{pole_kind}_{side}", entry["pole"])
        if entry["toe"] and toe_kind:
            slots.setdefault(f"{toe_kind}_{side}", entry["toe"][1])
    extra = {k: v for k, v in profile.items()
             if k not in ("name", "rest", "root", "hold", "chains", "sides", "ik", "limbs")}
    return slots, profile.get("rest", "zero"), hold, extra


def build_profile(name, slots, rest="zero", hold=None, extra=None, limbs=None):
    """Chains in the order the engine needs (spine, arms, legs, fingers; parents first)."""
    slots = {k: v.strip() for k, v in slots.items() if v and v.strip()}
    if not slots.get("root"):
        raise ValueError("Root / COG 必須指定一個控制器。")
    chains = []
    if slots.get("pelvis"):
        chains.append({"name": "pelvis", "links": [["Hips", slots["pelvis"]]]})
    spine = [["Hips", slots["spine_base"]]] if slots.get("spine_base") else []
    spine += [[SLOT_SOURCE[k], slots[k]] for k in ("spine", "spine1", "spine2", "neck", "head") if slots.get(k)]
    if spine:
        chains.append({"name": "spine", "align_from": 1 if slots.get("spine_base") else 0, "links": spine})
    for short, word, _ in SIDES:
        arm = [[SLOT_SOURCE[f"{k}_{short}"], slots[f"{k}_{short}"]]
               for k in ("clavicle", "arm", "forearm", "hand") if slots.get(f"{k}_{short}")]
        if arm:
            chain = {"name": f"arm_{short}", "links": arm}
            aim = slots.get(f"handaim_{short}") or slots.get(f"middle1_{short}")
            if aim and slots.get(f"hand_{short}"):
                chain["end_aim"] = [f"{word}HandMiddle1", aim]
            chains.append(chain)
        leg = [[SLOT_SOURCE[f"{k}_{short}"], slots[f"{k}_{short}"]]
               for k in ("upleg", "leg", "foot", "toe") if slots.get(f"{k}_{short}")]
        if leg:
            chains.append({"name": f"leg_{short}", "links": leg})
    for short, _, _ in SIDES:
        for key, _, _ in FINGERS:
            links = [[SLOT_SOURCE[f"{key}{i}_{short}"], slots[f"{key}{i}_{short}"]]
                     for i in (1, 2, 3) if slots.get(f"{key}{i}_{short}")]
            if links:
                chains.append({"name": f"{key}_{short}", "links": links})
    ik = []
    for short, word, _ in SIDES:
        for limb, (start, mid, end, ik_kind, pole_kind, toe_kind, _) in IK_LIMBS.items():
            control, pole = slots.get(f"{ik_kind}_{short}"), slots.get(f"{pole_kind}_{short}")
            toe = slots.get(f"{toe_kind}_{short}") if toe_kind else None
            if control or pole or toe:
                entry = {"name": f"{limb}_{short}", "start": word + start, "mid": word + mid, "end": word + end,
                         "control": control or "", "pole": pole or ""}
                if toe:
                    entry["toe"] = [f"{word}ToeBase", toe]
                ik.append(entry)
    profile = {"name": name, "rest": rest, "root": {"source": "Hips", "control": slots["root"]},
               "hold": dict(hold or {}), "chains": chains}
    if ik:
        profile["ik"] = ik
    limbs = {k: v for k, v in (limbs or {}).items() if v.get("switch") or v.get("mode", "fk") != "fk"}
    if limbs:
        profile["limbs"] = limbs
    profile.update(extra or {})
    return profile


# ------------------------------------------------------------------ automap (name based, like Unity's Automap)
SKIP_TOKENS = {
    "upvec", "twist", "offset", "space", "srt", "grp", "group", "zero", "null", "gimbal",
    "tweak", "tweaker", "squash", "vis", "settings", "panel", "switch", "world", "eye", "eyes", "jaw", "tongue",
    "teeth", "lip", "lips", "brow", "cloth", "hair", "skirt", "weapon", "prop", "end", "tip", "top", "roll", "heel",
    "bank", "inner", "outer", "piv", "pivot", "tap", "mid", "fkik", "ikfk", "cns", "ikcns", "ikrot", "master",
    "global", "god", "main", "all", "fly", "extra", "motion", "sdk", "driver", "bend", "ribbon", "curve", "loc",
    "locator", "tgt", "target", "aim", "ref", "follow", "orient", "parent", "constraint", "swinger",
    "crv", "spline", "cv", "guide", "proxy", "shape", "dyn", "dynamic", "sim", "sub"}
LEFT, RIGHT = {"l", "left", "lf", "lft"}, {"r", "right", "rt", "rgt"}
FINGER_WORDS = {"thumb": "thumb", "index": "index", "pointer": "index", "middle": "middle", "ring": "ring",
                "pinky": "pinky", "pinkie": "pinky", "little": "pinky"}
KEYWORDS = (  # token set, kind, score
    ({"cog", "com", "body"}, "root", 3),
    ({"pelvis", "hips"}, "pelvis", 3),
    ({"spine", "chest", "torso", "abdomen", "abs", "back"}, "spine", 2),
    ({"neck"}, "neck", 3), ({"head"}, "head", 3),
    ({"clavicle", "clav", "scapula", "collar"}, "clavicle", 3),
    ({"shoulder", "shldr", "upperarm", "uparm", "humerus"}, "arm", 2),
    ({"elbow", "forearm", "lowerarm", "lwrarm"}, "forearm", 3),
    ({"wrist", "hand"}, "hand", 3),
    ({"thigh", "upleg", "upperleg", "femur"}, "upleg", 3),
    ({"knee", "calf", "shin", "lowerleg"}, "leg", 3),
    ({"foot", "ankle"}, "foot", 2),
    ({"toe", "toes", "ball", "toebase"}, "toe", 3),
)
SINGLE = ("root", "pelvis", "neck", "head", "clavicle", "arm", "forearm", "hand", "upleg", "leg", "foot", "toe",
          "ikhand", "armpole", "ikfoot", "legpole", "iktoe")
POLE_WORDS = {"pv", "pole", "upv", "polevector"}
IK_REJECT = {"rot", "hybrid", "khybrid", "spine", "chest", "neck", "head", "hip", "shldr", "shoulder", "upr",
             "clavicle", "clav", "scapula"}


def _classify_ik(words, side, finger):
    """IK end controls and pole vectors of arms and legs (finger IK is not mapped)."""
    if side == "C" or finger or words & IK_REJECT:
        return None
    if words & POLE_WORDS:
        if words & {"arm", "elbow"}:
            return ("armpole", side, 3, None)
        if words & {"leg", "knee"}:
            return ("legpole", side, 3, None)
        return None
    if words & {"toe", "toes", "ball", "toebase"}:
        return ("iktoe", side, 3, None)
    if words & {"wrist", "hand", "arm"}:
        return ("ikhand", side, 3, None)
    if words & {"foot", "ankle", "leg"}:
        return ("ikfoot", side, 3, None)
    return None


def tokens(name):
    """'FKShoulder_L' -> ['fk', 'shoulder', 'l']; 'arm_L0_fk0_ctl' -> ['arm', 'l', '0', 'fk', '0', 'ctl']."""
    short = name.rsplit("|", 1)[-1].rsplit(":", 1)[-1]
    found = []
    for part in re.split(r"[^A-Za-z0-9]+", short):
        found += re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
    return [t.lower() for t in found]


def classify(name):
    """(kind, side, score, extra) for one control name, or None. Finger kinds carry (finger, number)."""
    toks = tokens(name)
    short = name.rsplit("|", 1)[-1].rsplit(":", 1)[-1]
    if not toks or SKIP_TOKENS & set(toks):
        return None
    side = "L" if LEFT & set(toks) else "R" if RIGHT & set(toks) else "C"
    match = re.search(r"fk(\d+)", name.rsplit(":", 1)[-1].lower())
    fk = int(match.group(1)) if match else None
    words = set(toks)
    numbers = [int(t) for t in toks if t.isdigit()]

    fingers = [FINGER_WORDS[t] for t in toks if t in FINGER_WORDS]
    # "IKhybridSpine1_M" splits as I + Khybrid, so also catch an "ik" glued to the next word.
    if "ik" in words or re.match(r"(?:.*_)?ik", short.lower()) or words & POLE_WORDS:
        return _classify_ik(words, side, bool(fingers) or "finger" in words)
    if fingers and side != "C":
        number = fk + 1 if fk is not None else (numbers[-1] if numbers else None)
        return ("finger", side, 3, (fingers[-1], number)) if number is not None else None
    if "finger" in words and side != "C" and fk is not None:
        # mGear: finger_L0..L3_fk0..2 -> index, middle, ring, pinky
        after = toks.index("l" if side == "L" else "r") + 1 if ("l" in toks or "r" in toks) else None
        component = int(toks[after]) if after is not None and after < len(toks) and toks[after].isdigit() else None
        order = ("index", "middle", "ring", "pinky")
        if component is not None and component < len(order):
            return ("finger", side, 2, (order[component], fk + 1))
        return None

    if "arm" in words and side != "C":
        if fk is not None and fk <= 2:
            return (("arm", "forearm", "hand")[fk], side, 4, None)
    if "leg" in words and side != "C":
        if fk is not None and fk <= 2:
            return (("upleg", "leg", "foot")[fk], side, 4, None)
        if words & {"upr", "upper"}:
            return ("upleg", side, 3, None)
        if words & {"lwr", "lower"}:
            return ("leg", side, 3, None)
    if "foot" in words and fk is not None and side != "C":
        return ("toe", side, 3, None)  # mGear foot_L0_fk0 is the ball
    if "root" in words and side == "C":
        # ADV FKRoot_M bends the lower spine without moving the legs (RootX_M carries the hips),
        # so it is the first spine bone, as in ADV's own MoCap mapping (FKRoot_M <- Spine).
        return ("spine", side, 3, None) if "fk" in words else ("root", side, 2, None)

    best = None
    for position, token in enumerate(toks):
        for words_set, kind, score in KEYWORDS:
            if token in words_set:
                if kind in ("clavicle", "arm", "forearm", "hand", "upleg", "leg", "foot", "toe") and side == "C":
                    continue
                if kind in ("root", "pelvis", "spine", "neck", "head") and side != "C":
                    continue
                if best is None or (score, position) >= (best[1], best[2]):
                    best = (kind, score, position)
        if token == "hip":
            kind, score = ("upleg", 3) if side != "C" else ("pelvis", 2)
            if best is None or (score, position) >= (best[1], best[2]):
                best = (kind, score, position)
    if best is None:
        return None
    if best[0] == "arm" and words & {"arm"} and words & {"lwr", "lower"}:
        return ("forearm", side, 3, None)
    return (best[0], side, best[1], None)


def automap(candidates):
    """candidates: [{"name": str, "y": height}] -> (slots, report). Height orders the spine."""
    scored = []
    for item in candidates:
        result = classify(item["name"])
        if result:
            kind, side, score, extra = result
            fk_bonus = 1 if "fk" in tokens(item["name"]) else 0
            scored.append({"name": item["name"], "y": item.get("y", 0.0), "kind": kind, "side": side,
                           "score": score, "rank": (score, fk_bonus, -len(item["name"])), "extra": extra})
    slots, used = {}, set()

    def take(kind, side, slot):
        pool = [c for c in scored if c["kind"] == kind and c["side"] == side and c["name"] not in used]
        if pool:
            best = max(pool, key=lambda c: c["rank"])
            slots[slot] = best["name"]
            used.add(best["name"])

    for kind in SINGLE:
        if kind in ("root", "pelvis", "neck", "head"):
            take(kind, "C", kind)
        else:
            for short, _, _ in SIDES:
                take(kind, short, f"{kind}_{short}")
    for short, _, _ in SIDES:  # mGear: shoulder_L0 is the clavicle when arm_L0_fk0 took the upper arm
        if not slots.get(f"clavicle_{short}"):
            spare = [c for c in scored if c["kind"] == "arm" and c["side"] == short and c["name"] not in used
                     and set(tokens(c["name"])) & {"shoulder", "shldr"}]
            if spare:
                slots[f"clavicle_{short}"] = spare[0]["name"]
                used.add(spare[0]["name"])

    spine = sorted((c for c in scored if c["kind"] == "spine" and c["name"] not in used), key=lambda c: c["y"])
    fk_spine = [c for c in spine if "fk" in tokens(c["name"])]
    if fk_spine:  # a rig with an FK spine family: ignore extras such as a belly jiggle control
        spine = fk_spine
    heights = [c["y"] for c in scored if c["kind"] in ("head",)] + [c["y"] for c in scored if c["kind"] in ("toe", "foot")]
    span = (max(heights) - min(heights)) if len(heights) > 1 else 1.0
    # A spine control at hip height only follows the pelvis when the rig has its own pelvis
    # control (Hive's ribbon base under hips_anim); otherwise it is the lower back (ADV FKRoot_M).
    hips_y = next((c["y"] for c in scored if c["name"] == slots.get("pelvis")), None)
    if spine and hips_y is not None and spine[0]["y"] <= hips_y + 0.02 * span:
        slots["spine_base"] = spine.pop(0)["name"]
    if len(spine) > 3:
        spine = [spine[round(i * (len(spine) - 1) / 2)] for i in range(3)]
    for key, item in zip({1: ["spine2"], 2: ["spine1", "spine2"], 3: ["spine", "spine1", "spine2"]}.get(len(spine), []), spine):
        slots[key] = item["name"]

    for short, _, _ in SIDES:
        for finger, _, _ in FINGERS:
            by_number = {}
            for c in scored:
                if c["kind"] == "finger" and c["side"] == short and c["extra"][0] == finger:
                    number = c["extra"][1]
                    if number not in by_number or c["rank"] > by_number[number]["rank"]:
                        by_number[number] = c
            chain = [by_number[n] for n in sorted(by_number)]
            if len(chain) >= 4:
                chain = chain[-3:]  # 00 is a metacarpal
            for i, item in enumerate(chain[:3], start=1):
                slots[f"{finger}{i}_{short}"] = item["name"]
    report = {key: next(c["kind"] for c in scored if c["name"] == name) for key, name in slots.items()}
    return slots, report


# ------------------------------------------------------------------ FK/IK switches
SWITCH_PATTERN = re.compile(r"(?i)(^|_)(fk_?ik|ik_?fk)(_?blend|_?switch)?$|(^|_)(arm|leg|limb)?_?blend$|ikblend$")


def is_switch_attr(attr):
    return bool(SWITCH_PATTERN.search(attr))


def switch_values(attr, minimum=None, maximum=None):
    """Guess {fk, ik} values from the name: 'ikfk' runs IK -> FK, 'FKIKBlend' / '*_blend' run FK -> IK."""
    low, high = (0 if minimum is None else minimum), (1 if maximum is None else maximum)
    name = attr.lower().replace("_", "")
    if name.startswith("ikfk"):
        return {"fk": high, "ik": low}
    return {"fk": low, "ik": high}


# ------------------------------------------------------------------ mirroring
MIRROR_PAIRS = (("_L_", "_R_"), ("_L", "_R"), ("L_", "R_"), ("Left", "Right"), ("left", "right"),
                ("_l_", "_r_"), ("_l", "_r"), ("l_", "r_"), ("_Lf", "_Rt"), ("Lf", "Rt"))


def mirror_candidates(name):
    """Right-side guesses for a left-side control name, most specific first."""
    short = name.rsplit(":", 1)[-1]
    prefix = name[: len(name) - len(short)]
    seen, result = set(), []
    for left, right in MIRROR_PAIRS:
        if left not in short:
            continue
        index = short.rfind(left)
        for candidate in (short.replace(left, right), short[:index] + right + short[index + len(left):]):
            if candidate != short and candidate not in seen:
                seen.add(candidate)
                result.append(prefix + candidate)
    return result
