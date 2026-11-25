"""Mapping slot round-trips (no Maya):  python -I tests/test_mapping.py -v"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "flowhmr_bridge"))
import mapping  # noqa: E402  (imported directly: the package __init__ pulls in nothing Maya-only, but generic does)

PROFILES = ROOT / "flowhmr_bridge" / "profiles"


def _normal(profile):
    root, chains, hold = mapping.expand(profile)
    links = {(chain["name"], tuple(map(tuple, chain["links"])), chain["align_from"], tuple(chain["end_aim"] or ()))
             for chain in chains}
    return root["control"], links, hold


class RoundTrip(unittest.TestCase):
    def test_builtin_profiles_survive_slots(self):
        for path in sorted(PROFILES.glob("*.json")):
            with self.subTest(profile=path.name):
                profile = json.loads(path.read_text(encoding="utf-8"))
                slots, rest, hold, extra = mapping.slots_from_profile(profile)
                rebuilt = mapping.build_profile(profile["name"], slots, rest, hold, extra)
                self.assertEqual(_normal(rebuilt), _normal(profile))
                self.assertEqual(rebuilt.get("test"), profile.get("test"))

    def test_ik_round_trip(self):
        slots = {"root": "COG", "arm_L": "fk_arm_L", "ikhand_L": "ik_hand_L", "armpole_L": "pv_arm_L",
                 "ikfoot_R": "ik_foot_R", "legpole_R": "pv_leg_R", "iktoe_R": "ik_toe_R"}
        limbs = {"arm_L": {"mode": "ik", "switch": "arm_L_settings.ikfk", "fk": 1, "ik": 0},
                 "leg_L": {"mode": "fk", "switch": "", "fk": "", "ik": ""}}
        profile = mapping.build_profile("x", slots, limbs=limbs)
        self.assertEqual(sorted(e["name"] for e in profile["ik"]), ["arm_L", "leg_R"])
        leg = next(e for e in mapping.expand_ik(profile) if e["name"] == "leg_R")
        self.assertEqual((leg["start"], leg["mid"], leg["end"], leg["toe"]),
                         ("RightUpLeg", "RightLeg", "RightFoot", ["RightToeBase", "ik_toe_R"]))
        self.assertEqual(profile["limbs"], {"arm_L": limbs["arm_L"]})
        self.assertEqual(mapping.switch_holds(profile), {"arm_L_settings.ikfk": 0})
        back, _, _, extra = mapping.slots_from_profile(profile)
        self.assertEqual({k: back[k] for k in slots}, slots)
        self.assertNotIn("ik", extra)
        templated = {"name": "t", "root": {"source": "Hips", "control": "c"}, "sides": [["L", "Left"], ["R", "Right"]],
                     "ik": [{"name": "leg_{s}", "start": "{Side}UpLeg", "mid": "{Side}Leg", "end": "{Side}Foot",
                             "control": "IKLeg_{s}", "pole": "PoleLeg_{s}"}],
                     "limbs": {"leg_{s}": {"mode": "ik", "switch": "FKIKLeg_{s}.FKIKBlend", "fk": 0, "ik": 10}}}
        self.assertEqual([e["control"] for e in mapping.expand_ik(templated)], ["IKLeg_L", "IKLeg_R"])
        self.assertEqual(mapping.switch_holds(templated), {"FKIKLeg_L.FKIKBlend": 10, "FKIKLeg_R.FKIKBlend": 10})

    def test_root_is_required(self):
        with self.assertRaises(ValueError):
            mapping.build_profile("x", {"pelvis": "hips_ctrl"})

    def test_slot_keys_unique_and_sources_valid(self):
        keys = [s[0] for s in mapping.SLOTS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(mapping.SLOTS), 8 + 2 * (9 + 15 + 5))
        self.assertFalse(set(mapping.SOURCE_SLOT.values()) & mapping.IK_SLOTS)

    def test_mirror_candidates(self):
        self.assertIn("char:arm_R_shldr_fk_anim", mapping.mirror_candidates("char:arm_L_shldr_fk_anim"))
        self.assertIn("FKShoulder_R", mapping.mirror_candidates("FKShoulder_L"))
        self.assertIn("RightArm_ctrl", mapping.mirror_candidates("LeftArm_ctrl"))
        self.assertIn("r_elbow_ctl", mapping.mirror_candidates("l_elbow_ctl"))
        self.assertEqual(mapping.mirror_candidates("spine_C_cog_anim"), [])


if __name__ == "__main__":
    unittest.main()
