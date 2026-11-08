#!/usr/bin/env python3
"""merge_config.py TARGET SOURCE — update TARGET (JSON) with SOURCE's keys,
creating it when missing. deploy_tablet.sh uses it to refresh the PC address
and token in /etc/xtion-tablet.json without losing local edits."""
import json
import os
import sys


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    target, source = argv[1], argv[2]
    cfg = {}
    if os.path.isfile(target):
        with open(target) as fh:
            cfg = json.load(fh)
    with open(source) as fh:
        cfg.update(json.load(fh))
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with open(target, "w") as fh:
        json.dump(cfg, fh, indent=1)
        fh.write("\n")
    print(f"   {target}: {', '.join(sorted(k for k in json.load(open(source))))} updated")


if __name__ == "__main__":
    main(sys.argv)
