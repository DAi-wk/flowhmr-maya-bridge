"""Name-based automap on ADV, Zoo Hive and mGear naming (no Maya):
    python -I tests/test_automap.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "flowhmr_bridge"))
import mapping  # noqa: E402


def rig(entries):
    return [{"name": name, "y": y} for name, y in entries]


def sided(template, heights):
    return [(template.format(s=s), y) for s in "LR" for y in heights]


ADV = rig([("Main", 0), ("RootX_M", 100), ("FKRoot_M", 100), ("HipSwinger_M", 102), ("FKSpine1_M", 112),
           ("FKChest_M", 125), ("FKNeck_M", 145), ("FKHead_M", 152), ("IKSpine1_M", 110), ("FKIKSpine_M", 90),
           ("IKhybridSpine1_M", 100), ("IKcvSpine1_M", 108), ("FKSubSpine1_M", 112), ("FKSubChest_M", 125),
           ("abdomen_3_ctrl", 105),
           ("Fingers_L", 95), ("FKExtraIndexFinger1_L", 95), ("RollHeel_L", 2), ("PoleLeg_L", 50), ("IKLeg_L", 8)]
          + [(f"FK{part}_{s}", y) for s in "LR" for part, y in (
              ("Scapula", 140), ("Shoulder", 138), ("Elbow", 120), ("Wrist", 100), ("Hip", 95), ("Knee", 50),
              ("Ankle", 8), ("Toes", 2))]
          + [(f"FK{f}Finger{i}_{s}", 95) for s in "LR" for f in ("Thumb", "Index", "Middle", "Ring", "Pinky")
             for i in (1, 2, 3)])
HIVE = rig([("spine_C_cog_anim", 90.1), ("spine_C_cogGimbal_anim", 90.1), ("spine_C_hips_anim", 95.5),
            ("spine_C_hipSwing_anim", 102.7), ("spine_C_ctrl00_anim", 89.9), ("spine_C_ctrl01_anim", 102.7),
            ("spine_C_ctrl02_anim", 114.3), ("spine_C_tweaker00_anim", 95), ("spine_C_worldUpVec_anim", 100),
            ("spineSpline_crv", 85),
            ("head_C_neck_anim", 124.3), ("head_C_head_anim", 130.8), ("god_C_rootMotion_anim", 0),
            ("god_C_godnode_anim", 0)]
           + [(n.format(s=s), y) for s in "LR" for n, y in (
               ("clavicle_{s}_00_anim", 124.7), ("arm_{s}_shldr_fk_anim", 120), ("arm_{s}_elbow_fk_anim", 120),
               ("arm_{s}_wrist_fk_anim", 120), ("arm_{s}_wrist_ik_anim", 120), ("arm_{s}_elbowPv_pv_anim", 120),
               ("arm_{s}_uprTwist00_anim", 120), ("leg_{s}_upr_fk_anim", 85), ("leg_{s}_knee_fk_anim", 46.7),
               ("leg_{s}_foot_fk_anim", 8.6), ("leg_{s}_ball_fk_anim", 0), ("leg_{s}_foot_ik_anim", 8.6),
               ("leg_{s}_heel_ik_anim", 0), ("leg_{s}_hip_ik_anim", 85))]
           + [(f"finger_{f}_{s}_0{i}_anim{x}", 119) for s in "LR" for f in ("pointer", "middle", "ring", "pinky")
              for i in range(4) for x in ("", "_ctrl")]
           + [(f"finger_thumb_{s}_0{i}_anim", 119) for s in "LR" for i in range(3)])
MGEAR = rig([("world_ctl", 0), ("global_C0_ctl", 0), ("local_C0_ctl", 0), ("body_C0_ctl", 100),
             ("spine_C0_ik0_ctl", 100), ("spine_C0_fk0_ctl", 104), ("spine_C0_fk1_ctl", 112),
             ("spine_C0_fk2_ctl", 120), ("spine_C0_fk3_ctl", 128), ("neck_C0_fk0_ctl", 145),
             ("neck_C0_head_ctl", 152), ("neck_C0_ik_ctl", 150)]
            + [(n.format(s=s), y) for s in "LR" for n, y in (
                ("shoulder_{s}0_ctl", 140), ("arm_{s}0_fk0_ctl", 138), ("arm_{s}0_fk1_ctl", 120),
                ("arm_{s}0_fk2_ctl", 100), ("arm_{s}0_ik_ctl", 100), ("arm_{s}0_upv_ctl", 120), ("arm_{s}0_mid_ctl", 120),
                ("leg_{s}0_fk0_ctl", 95), ("leg_{s}0_fk1_ctl", 50), ("leg_{s}0_fk2_ctl", 8), ("leg_{s}0_ik_ctl", 8),
                ("foot_{s}0_fk0_ctl", 2), ("foot_{s}0_heel_ctl", 0))]
            + [(f"thumb_{s}0_fk{i}_ctl", 95) for s in "LR" for i in range(3)]
            + [(f"finger_{s}{c}_fk{i}_ctl", 95) for s in "LR" for c in range(4) for i in range(3)])


class Automap(unittest.TestCase):
    def check(self, slots, expected):
        for key, name in expected.items():
            self.assertEqual(slots.get(key), name, key)
        self.assertEqual(mapping.missing_required(slots), [])

    def test_adv(self):
        slots, _ = mapping.automap(ADV)
        self.check(slots, {"root": "RootX_M", "pelvis": "FKRoot_M", "spine1": "FKSpine1_M", "spine2": "FKChest_M",
                           "neck": "FKNeck_M", "head": "FKHead_M", "clavicle_L": "FKScapula_L", "arm_L": "FKShoulder_L",
                           "forearm_R": "FKElbow_R", "hand_R": "FKWrist_R", "upleg_L": "FKHip_L", "leg_L": "FKKnee_L",
                           "foot_R": "FKAnkle_R", "toe_R": "FKToes_R", "thumb1_L": "FKThumbFinger1_L",
                           "pinky3_R": "FKPinkyFinger3_R"})
        self.assertNotIn("spine_base", slots)

    def test_hive(self):
        slots, _ = mapping.automap(HIVE)
        self.check(slots, {"root": "spine_C_cog_anim", "pelvis": "spine_C_hips_anim", "spine_base": "spine_C_ctrl00_anim",
                           "spine1": "spine_C_ctrl01_anim", "spine2": "spine_C_ctrl02_anim", "neck": "head_C_neck_anim",
                           "head": "head_C_head_anim", "clavicle_R": "clavicle_R_00_anim", "arm_L": "arm_L_shldr_fk_anim",
                           "forearm_L": "arm_L_elbow_fk_anim", "hand_L": "arm_L_wrist_fk_anim",
                           "upleg_R": "leg_R_upr_fk_anim", "leg_R": "leg_R_knee_fk_anim", "foot_L": "leg_L_foot_fk_anim",
                           "toe_L": "leg_L_ball_fk_anim", "index1_L": "finger_pointer_L_01_anim",
                           "index3_L": "finger_pointer_L_03_anim", "thumb1_R": "finger_thumb_R_00_anim"})

    def test_mgear(self):
        slots, _ = mapping.automap(MGEAR)
        self.check(slots, {"root": "body_C0_ctl", "neck": "neck_C0_fk0_ctl", "head": "neck_C0_head_ctl",
                           "spine": "spine_C0_fk0_ctl", "spine2": "spine_C0_fk3_ctl", "clavicle_L": "shoulder_L0_ctl",
                           "arm_L": "arm_L0_fk0_ctl", "forearm_L": "arm_L0_fk1_ctl", "hand_L": "arm_L0_fk2_ctl",
                           "upleg_R": "leg_R0_fk0_ctl", "leg_R": "leg_R0_fk1_ctl", "foot_R": "leg_R0_fk2_ctl",
                           "toe_R": "foot_R0_fk0_ctl", "thumb2_L": "thumb_L0_fk1_ctl", "index1_L": "finger_L0_fk0_ctl",
                           "pinky3_R": "finger_R3_fk2_ctl"})

    def test_ik_and_poles(self):
        adv = mapping.automap(ADV + rig([(f"{n}_{s}", 50) for s in "LR" for n in ("IKArm", "PoleArm", "IKToes")]))[0]
        self.assertEqual([adv.get(k) for k in ("ikhand_L", "armpole_R", "ikfoot_L", "legpole_L", "iktoe_R")],
                         ["IKArm_L", "PoleArm_R", "IKLeg_L", "PoleLeg_L", "IKToes_R"])
        extra = rig([(n.format(s=s), 50) for s in "LR" for n in (
            "leg_{s}_kneePv_pv_anim", "leg_{s}_ball_ik_anim", "arm_{s}_shldr_ik_anim", "arm_{s}_wrist_ik_anim_ctrl",
            "leg_{s}_toeTip_ik_anim")])
        hive = mapping.automap(HIVE + extra)[0]
        self.assertEqual([hive.get(k) for k in ("ikhand_L", "armpole_L", "ikfoot_R", "legpole_R", "iktoe_L")],
                         ["arm_L_wrist_ik_anim", "arm_L_elbowPv_pv_anim", "leg_R_foot_ik_anim",
                          "leg_R_kneePv_pv_anim", "leg_L_ball_ik_anim"])
        mgear = mapping.automap(MGEAR + rig([("leg_L0_upv_ctl", 50), ("leg_R0_upv_ctl", 50), ("arm_L0_ikcns_ctl", 1)]))[0]
        self.assertEqual([mgear.get(k) for k in ("ikhand_L", "armpole_L", "ikfoot_R", "legpole_R")],
                         ["arm_L0_ik_ctl", "arm_L0_upv_ctl", "leg_R0_ik_ctl", "leg_R0_upv_ctl"])
        # custom finger IK systems and IK spines stay out
        self.assertIsNone(mapping.classify("index_IK_Handle_jnt_L_ctrl"))
        self.assertIsNone(mapping.classify("index_pole_vector_jnt_L_ctrl"))
        self.assertIsNone(mapping.classify("IKSpine1_M"))

    def test_switch_guess(self):
        self.assertTrue(mapping.is_switch_attr("FKIKBlend"))
        self.assertTrue(mapping.is_switch_attr("ikfk"))
        self.assertTrue(mapping.is_switch_attr("arm_blend"))
        self.assertFalse(mapping.is_switch_attr("ikSpace"))
        self.assertEqual(mapping.switch_values("FKIKBlend", 0, 10), {"fk": 0, "ik": 10})
        self.assertEqual(mapping.switch_values("ikfk", 0, 1), {"fk": 1, "ik": 0})
        self.assertEqual(mapping.switch_values("arm_blend", 0, 1), {"fk": 0, "ik": 1})

    def test_tokens(self):
        self.assertEqual(mapping.tokens("Ns:FKShoulder_L"), ["fk", "shoulder", "l"])
        self.assertEqual(mapping.tokens("arm_L0_fk0_ctl"), ["arm", "l", "0", "fk", "0", "ctl"])
        self.assertEqual(mapping.tokens("FKIKArm_R"), ["fkik", "arm", "r"])


if __name__ == "__main__":
    unittest.main()
