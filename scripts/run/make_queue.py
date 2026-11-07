#!/usr/bin/env python3
"""
make_queue.py: write the list of traces for scripts/run/auto_queue.sh to run.

Usage (on a CloudLab node, after model_fetch.sh + llama_build.sh):
    python3 scripts/run/make_queue.py --rounds 3 --batch batch_009_70b --install
    sudo reboot          # the first trace starts automatically after the reboot

What it does
  * Node number comes from the hostname (clnode182 -> 182).
  * Each round contains every condition once (N, E, MP by default), in a
    random order that is drawn separately for each round, e.g.
        round 1: MP, N, E      round 2: E, MP, N      round 3: N, E, MP
    Run labels follow the project naming: 182_1mp, 182_1n, 182_1e, 182_2e, ...
  * The seed and the drawn order are saved to <output-root>/queue_plan_<node>.csv
    so the order travels with the data and can be reproduced (--seed).
  * Works for any model: auto_queue.sh uses whatever model_fetch.sh last set
    (70b or 405b). Use a different --batch per model.
  * --install adds the @reboot cron entry that starts auto_queue.sh.

Queue file format (~/mcc_queue.txt), one trace per line:
    <prompt json> <label> <run label> <output root>
"""

import argparse
import csv
import os
import random
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONDITIONS = {
    "n":  ("prompts/20base/independentN.json",  "neutral"),
    "e":  ("prompts/20base/independentE.json",  "emotional"),
    "mp": ("prompts/20base/independentMP.json", "mechanicalprime"),
}


def node_number() -> str:
    host = socket.getfqdn() or socket.gethostname()
    m = re.search(r"clnode(\d+)", host)
    if not m:
        raise SystemExit(f"Can't find a node number in hostname '{host}'. Pass --node.")
    return m.group(1)


def install_cron(script: Path) -> None:
    line = f"@reboot {script}"
    try:
        current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise SystemExit("crontab not found on this machine.")
    if line in current.splitlines():
        print("cron entry already present.")
        return
    new = (current.rstrip("\n") + "\n" if current.strip() else "") + line + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    print(f"Installed cron entry: {line}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, required=True, help="how many rounds (each round = every condition once)")
    ap.add_argument("--batch", required=True, help="batch folder name, e.g. batch_009_70b")
    ap.add_argument("--conditions", default="n,e,mp", help="which conditions per round (default: n,e,mp)")
    ap.add_argument("--start-round", type=int, default=1, help="first round number (default 1), e.g. 3 to continue")
    ap.add_argument("--node", default=None, help="node number (default: from hostname)")
    ap.add_argument("--seed", type=int, default=None, help="random seed (default: random, saved in the plan)")
    ap.add_argument("--json-n", default=None, help="override the Neutral prompt file")
    ap.add_argument("--json-e", default=None, help="override the Emotional prompt file")
    ap.add_argument("--json-mp", default=None, help="override the MechanicalPrime prompt file")
    ap.add_argument("--queue", default=str(Path.home() / "mcc_queue.txt"))
    ap.add_argument("--install", action="store_true", help="also add the @reboot cron entry")
    ap.add_argument("--force", action="store_true", help="overwrite a queue file that still has lines in it")
    args = ap.parse_args()

    conds = [c.strip().lower() for c in args.conditions.split(",") if c.strip()]
    for c in conds:
        if c not in CONDITIONS:
            raise SystemExit(f"Unknown condition '{c}'. Use any of: {', '.join(CONDITIONS)}")
    files = {c: CONDITIONS[c][0] for c in CONDITIONS}
    for c, override in (("n", args.json_n), ("e", args.json_e), ("mp", args.json_mp)):
        if override:
            files[c] = override
    for c in conds:
        if not (REPO / files[c]).exists():
            raise SystemExit(f"Prompt file not found: {files[c]}")

    node = args.node or node_number()
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 10**9)
    rng = random.Random(seed)
    output_root = f"data/raw/fulltrace/{args.batch}"

    queue = Path(args.queue)
    if queue.exists() and queue.read_text().strip() and not args.force:
        raise SystemExit(f"{queue} still has traces in it:\n{queue.read_text()}"
                         "Use --force to replace it.")

    rows, lines = [], []
    pos = 0
    for k in range(args.start_round, args.start_round + args.rounds):
        order = conds[:]
        rng.shuffle(order)
        for i, c in enumerate(order, 1):
            pos += 1
            run_label = f"{node}_{k}{c}"
            json_path, label = files[c], CONDITIONS[c][1]
            lines.append(f"{json_path} {label} {run_label} {output_root}")
            rows.append({"position": pos, "round": k, "order_in_round": i, "condition": c,
                         "label": label, "run_label": run_label, "prompt_json": json_path})

    queue.write_text("\n".join(lines) + "\n")

    plan_dir = REPO / output_root
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / f"queue_plan_{node}.csv"
    with open(plan, "w", newline="") as f:
        f.write(f"# node={node} seed={seed} created={time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"Node {node}, seed {seed}. {len(lines)} traces queued in {queue}:")
    for r in rows:
        print(f"  round {r['round']}  #{r['order_in_round']}  {r['run_label']:<10} {r['label']}")
    print(f"Plan saved to {plan.relative_to(REPO)} (copied along with the data).")

    script = REPO / "scripts" / "run" / "auto_queue.sh"
    if args.install:
        install_cron(script)
        print("\nNext: sudo reboot   (the first trace starts automatically after the cool-down)")
    else:
        print("\nTo start: add --install (or install the cron entry yourself), then sudo reboot")


if __name__ == "__main__":
    main()
