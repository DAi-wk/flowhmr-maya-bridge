"""Write what a profile would have to declare for an ADV rig to match raw mode:
hold values (spine/neck bias endpoints, neck twist multipliers) and extra followers
(Micro controls and FK Part controls -> their skin joints).

    mayapy research/adv_extras.py --scene RIG.ma --namespace R --holds H.json --followers F.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(args):
    import maya.cmds as cmds
    from flowhmr_bridge import character, raw

    cmds.file(str(args.scene.resolve()), reference=True, namespace=args.namespace, prompt=False)
    ns = args.namespace + ":"
    holds = {plug[len(ns):]: value for plug, value in raw.spine_holds(ns).items()}
    holds.update({plug[len(ns):]: 0 for plug in raw.neck_twist_weights(ns).values()})
    followers = []
    for control in cmds.sets(ns + "ControlSet", q=True) or []:
        name = character._short(control)
        if name.startswith("Micro") and cmds.objExists(ns + name[5:] + ".twistAmount"):
            followers.append([name[5:], name])
        elif re.fullmatch(r"FK\w+Part\d+_\w+", name) and cmds.objExists(ns + name[2:]):
            followers.append([name[2:], name])
    args.holds.write_text(json.dumps(holds, indent=1), encoding="utf-8")
    args.followers.write_text(json.dumps(sorted(followers), indent=1), encoding="utf-8")
    print(len(holds), "holds,", len(followers), "followers", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--namespace", default="R")
    parser.add_argument("--holds", type=Path, required=True)
    parser.add_argument("--followers", type=Path, required=True)
    args = parser.parse_args()
    import maya.standalone
    maya.standalone.initialize()
    try:
        main(args)
    finally:
        maya.standalone.uninitialize()
