#!/usr/bin/env python3
"""Quick OVERLAP check for the multi-Xtion fan — run this BEFORE calibrating.

The #1 reason multi-camera fusion / room build comes out "all over the place"
is that adjacent cameras don't share enough field of view, so the markerless
calibration can't solve and the clouds can't be stitched. This tool tells you,
per adjacent pair, how much they actually overlap — so you can AIM the cameras
until every pair reads GOOD, and only THEN calibrate.

It captures one denoised cloud per camera (sequentially — 3 Xtions can't stream
together on one USB-2 bus) and runs the same FPFH+RANSAC->ICP registration that
calibrate_multi.py uses, but only REPORTS the fitness; it writes no extrinsics.

  .venv/bin/python overlap_check.py                  # live: capture + check all adjacent pairs
  .venv/bin/python overlap_check.py camera1 camera2  # live: just one pair
  .venv/bin/python overlap_check.py --offline [DIR]  # re-check already-saved <ns>.ply clouds

Fitness = fraction of points that find a match after registration:
  >= 0.40  GOOD       enough overlap to calibrate & fuse
  0.25-0.40 MARGINAL  toe the cameras in a bit more / aim at more 3-D structure
  <  0.25  POOR       adjacent cameras barely share a view — re-aim them

Aim at a STRUCTURED scene ~1-1.5 m away (a cluttered shelf or room corner, NOT a
blank wall), with each adjacent pair overlapping ~30-50%.
"""
import os
import sys

from calibrate_multi import HERE, roster, capture_all, register

GOOD = 0.40
MARGINAL = 0.25


def verdict(fit):
    if fit >= GOOD:
        return "GOOD", "enough overlap to calibrate & fuse"
    if fit >= MARGINAL:
        return "MARGINAL", "toe the cameras in a bit more, or aim at more structure"
    return "POOR", "adjacent cameras barely share a view — re-aim them at the same spot"


def cloud_path(ns, offline_dir):
    if offline_dir:
        return os.path.join(offline_dir, f"{ns}.ply")
    return os.path.join(HERE, "output", f"cal_{ns}.ply")


def main(argv):
    offline_dir = None
    if argv and argv[0] in ("--offline", "-o"):
        offline_dir = (argv[1] if len(argv) > 1 else
                       os.path.join(HERE, "output", "sweeps", "sweep_00_cams"))
        argv = argv[2:] if len(argv) > 1 else argv[1:]

    cams = roster()
    names = [c["ns"] for c in cams]

    if len(argv) == 2:                       # explicit single pair
        pairs = [(argv[0], argv[1])]
        to_capture = [c for c in cams if c["ns"] in argv]
    else:                                    # all adjacent pairs in roster (fan) order
        pairs = list(zip(names, names[1:]))
        to_capture = cams

    if len(pairs) == 0:
        print("[overlap] need >= 2 cameras in cameras.json"); return

    if not offline_dir:
        print(f"[overlap] capturing {len(to_capture)} camera(s), one at a time…")
        capture_all(to_capture)

    print("\n=== OVERLAP CHECK ===")
    worst = 1.0
    checked = 0
    for a, b in pairs:
        pa, pb = cloud_path(a, offline_dir), cloud_path(b, offline_dir)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            print(f"  {a} <-> {b}: missing cloud(s) — skipped "
                  f"({'capture failed?' if not offline_dir else 'no saved cloud'})")
            continue
        _, fit, rmse = register(pa, pb)
        v, hint = verdict(fit)
        worst = min(worst, fit)
        checked += 1
        print(f"  {a} <-> {b}:  fitness={fit:.2f}  rmse={rmse*1000:.0f}mm  ->  {v}")
        print(f"               {hint}")
    print("=====================")

    if checked == 0:
        print("No pairs could be checked — see capture messages above.")
    elif worst >= GOOD:
        print("All adjacent pairs GOOD. Next:  .venv/bin/python calibrate_multi.py")
    else:
        print("Re-aim the weak pair(s) so neighbors overlap ~30-50% on a textured")
        print("scene ~1-1.5 m away, then run this again.")
        print("(Re-check without re-capturing the same clouds:  --offline)")


if __name__ == "__main__":
    main(sys.argv[1:])
