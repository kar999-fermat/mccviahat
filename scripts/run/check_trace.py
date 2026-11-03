#!/usr/bin/env python3
"""
check_trace.py: sanity-check full-trace recordings before handing them over.

Usage:
    python3 scripts/run/check_trace.py data/raw/fulltrace/405b/232_1N
    python3 scripts/run/check_trace.py data/raw/fulltrace/batch_007/*      # many at once

For each recording folder it checks:
  * all expected files exist and are non-empty
  * every prompt returned (prompt_log.json "ok") and the count matches
  * the server's memory did not keep growing during the first prompt
    (growth means weights were still being read from disk while the trace was
    recording, which would add disk I/O and page faults to the hardware data)
  * the first prompt is not far slower than the rest
  * the run label follows the node#_<k><N|E|MP> naming

Prints PASS / WARN / FAIL per recording; exits 1 if any recording FAILs.
Uses only the Python standard library.
"""

import csv
import json
import re
import statistics
import sys
from pathlib import Path

REQUIRED = ["perf_stat.csv", "perf_stat.txt", "proc_sample.csv",
            "prompt_log.json", "trace_meta.json", "collector_meta.json"]
PAGE_BYTES = 4096
RSS_GROWTH_WARN_GB = 2.0      # growth during prompt 1 that suggests weights still loading
FIRST_PROMPT_WARN_RATIO = 1.5
LABEL_RE = re.compile(r"^\d+_\d+(N|E|MP)$")


def check(run_dir: Path) -> tuple[str, list[str]]:
    fails, warns, info = [], [], []

    for name in REQUIRED:
        f = run_dir / name
        if not f.exists() or f.stat().st_size == 0:
            fails.append(f"missing or empty: {name}")
    if fails:
        return "FAIL", fails

    meta = json.loads((run_dir / "trace_meta.json").read_text())
    log = json.loads((run_dir / "prompt_log.json").read_text())

    n_expected = meta.get("n_prompts")
    if len(log) != n_expected:
        fails.append(f"prompt_log has {len(log)} prompts, trace_meta says {n_expected}")
    bad = [p["prompt_index"] for p in log if not p.get("ok")]
    if bad or not meta.get("all_ok", False):
        fails.append(f"failed prompts: {bad or 'all_ok is false'}")

    label = meta.get("run_label", run_dir.name)
    if not LABEL_RE.match(label):
        warns.append(f"run label '{label}' does not follow node#_<k><N|E|MP> (e.g. 232_1N)")

    elapsed = [p["elapsed_ms"] / 1000 for p in log]
    if len(elapsed) >= 3:
        rest = statistics.median(elapsed[1:])
        ratio = elapsed[0] / rest if rest else 0
        info.append(f"prompt 1 {elapsed[0]:.0f}s vs median {rest:.0f}s (x{ratio:.2f})")
        if ratio > FIRST_PROMPT_WARN_RATIO:
            warns.append(f"first prompt is x{ratio:.1f} slower than the rest")

    # Server memory (RSS) at trace start vs end of the first prompt.
    rows = list(csv.DictReader(open(run_dir / "proc_sample.csv")))
    if rows and log:
        def rss_gb_at(t_ns: int) -> float:
            best = rows[0]
            for r in rows:
                if int(r["timestamp_ns"]) <= t_ns:
                    best = r
                else:
                    break
            return int(best["proc_rss_pages"]) * PAGE_BYTES / 1e9
        start = rss_gb_at(int(rows[0]["timestamp_ns"]))
        after1 = rss_gb_at(log[0]["t_request_end_ns"])
        end = rss_gb_at(int(rows[-1]["timestamp_ns"]))
        info.append(f"server RAM {start:.0f} GB at start, {after1:.0f} GB after prompt 1, {end:.0f} GB at end")
        if after1 - start > RSS_GROWTH_WARN_GB:
            warns.append(f"server RAM grew {after1 - start:.1f} GB during prompt 1: "
                         "weights were probably still loading while recording")

    for k in ("model_size", "load_mode", "reset_s", "llama_cpp_ref", "hostname"):
        if k in meta:
            info.append(f"{k}={meta[k]}")

    status = "FAIL" if fails else ("WARN" if warns else "PASS")
    return status, fails + warns + info


def main() -> None:
    dirs = [Path(a) for a in sys.argv[1:] if Path(a).is_dir()]
    if not dirs:
        raise SystemExit(__doc__)
    any_fail = False
    for d in sorted(dirs):
        status, notes = check(d)
        any_fail |= status == "FAIL"
        print(f"[{status}] {d}")
        for n in notes:
            print(f"    - {n}")
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
