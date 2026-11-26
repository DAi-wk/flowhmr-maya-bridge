"""Print a custom_hmr.py result: probe table and per-joint errors against the solver targets.

    python research/summarize.py RESULT.json [--probe] [--all]
"""
import json
import sys


def main(path, show_probe=False, show_all=False):
    result = json.load(open(path, encoding="utf-8"))
    print(f"{result['profile']}: {result['controls']} controls, {result['skin_joints']} skin joints, "
          f"rest vs bind max {result['bind_gap_max'][0]:.3f} deg / {result['bind_gap_max'][1]:.3f} cm")
    if show_probe:
        print("\ncontrol -> primary joint | per axis: ratio, deviation from rigid follow (deg); other responders")
        for control, entry in result["probe"].items():
            primary = entry["primary"]
            cells, others = [], set()
            for axis, responses in entry["axes"].items():
                mine = next((r for r in responses if r[0] == primary), None)
                cells.append(f"{axis}:{mine[1]:.2f}/{mine[2]:.2f}" if mine else f"{axis}:-")
                others.update(f"{r[0]}({r[1]:.2f})" for r in responses if r[0] != primary)
            print(f"  {control:22} -> {str(primary):18} {' '.join(cells):36} {', '.join(sorted(others))[:110]}")
    if "errors" not in result:
        return
    print("\nslot map:", {k: v for k, v in result["slot_map"].items() if v})
    if result.get("unpaired"):
        print("unpaired controls:", result["unpaired"])
    errors = result["errors"]
    rows = sorted(errors.items(), key=lambda kv: -kv[1][0])
    worst_deg = max(r[0] for r in errors.values())
    worst_cm = max(r[1] for r in errors.values())
    print(f"\n{len(errors)} bones, worst {worst_deg:.4f} deg / {worst_cm:.4f} cm")
    print(f"  {'bone':22} {'max deg':>9} {'max cm':>9} {'mean deg':>9} {'mean cm':>9}")
    for name, (dmax, cmax, dmean, cmean) in rows if show_all else rows[:25]:
        flag = "" if name in result["tracked"] else "  (untracked)"
        print(f"  {name:22} {dmax:9.3f} {cmax:9.3f} {dmean:9.3f} {cmean:9.3f}{flag}")
    if result.get("reference"):
        ref = result["reference"]
        name = max(ref, key=lambda n: ref[n][0])
        far = max(ref, key=lambda n: ref[n][1])
        print(f"\nvs FlowHMR's own character export, {len(ref)} bones: worst {ref[name][0]:.4f} deg ({name}), "
              f"{ref[far][1]:.4f} cm ({far})")


if __name__ == "__main__":
    main(sys.argv[1], "--probe" in sys.argv, "--all" in sys.argv)
