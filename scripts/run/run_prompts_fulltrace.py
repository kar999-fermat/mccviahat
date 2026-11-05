#!/usr/bin/env python3
"""
run_prompts_fulltrace.py

Full-trace runner for HAT substrate analysis.

Unlike run_prompts_isolated.py, this runner does NOT restart the Docker
container or collector between prompts. Instead:

  1. Reset the LLM server once at the start
  2. Start the collector once
  3. Send all prompts back-to-back, recording per-prompt timestamps
  4. Stop the collector after the last prompt returns
  5. Write a single trace directory with one perf_stat.csv covering
     the entire sequence, plus a prompt_log.json with per-prompt timing

This tests whether the substrate signal accumulates over repeated
emotional (or neutral) computations.

Directory layout:
  data/raw/fulltrace/incoming/<run_label>/
      perf_stat.txt
      perf_stat.csv
      hat_interrupts.csv
      proc_sample.csv
      kernel_log.txt
      collector_meta.json
      prompt_log.json       ← NEW: per-prompt timing within the trace
      trace_meta.json       ← NEW: overall trace metadata

Usage:
    python run_prompts_fulltrace.py \\
        --json prompts/20base/emotional.json \\
        --label emotional \\
        --run_label emotional_trace_001
"""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Model config loader (same as run_prompts_isolated.py)
# ─────────────────────────────────────────────────────────────────────────────

_MODEL_DEFAULTS = {
    "MODEL_SIZE":       "7b",
    "REQUEST_TIMEOUT":  "600",
    "RESET_TIMEOUT":    "90",
    "BASELINE_S":       "2",
    "PROC_INTERVAL_S":  "0.1",
}

def load_model_config(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        print(f"  ⚠  {config_path} not found — using built-in 7b defaults.")
        return dict(_MODEL_DEFAULTS)
    cfg = dict(_MODEL_DEFAULTS)
    pattern = re.compile(r'^([A-Z_]+)=["\']?([^"\'#\n]*)["\']?')
    for line in p.read_text().splitlines():
        m = pattern.match(line.strip())
        if m:
            cfg[m.group(1)] = m.group(2).strip()
    print(f"  Loaded model config: {config_path}  (MODEL_SIZE={cfg.get('MODEL_SIZE')})")
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Helpers (reused from run_prompts_isolated.py)
# ─────────────────────────────────────────────────────────────────────────────

def sh(cmd: list[str], check: bool = True, timeout: float = 30.0) -> str:
    return subprocess.check_output(cmd, text=True, timeout=timeout).strip() if check else ""


def get_container_pid(container: str) -> int:
    raw = sh(["docker", "inspect", "-f", "{{.State.Pid}}", container])
    if not raw.isdigit() or int(raw) <= 0:
        raise SystemExit(f"Could not resolve host PID for container '{container}': got '{raw}'")
    return int(raw)


def reset_server(reset_script: str, timeout: float = 90.0) -> float:
    """Restart the server; output streams to the terminal so a long 405B load
    shows progress. Returns the reset duration in seconds."""
    t0 = time.time()
    try:
        result = subprocess.run(["bash", reset_script], timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"reset_server.sh did not finish within {timeout:.0f}s")
    if result.returncode != 0:
        raise RuntimeError(f"reset_server.sh failed (exit {result.returncode}); see output above")
    return time.time() - t0


def node_info(container: str) -> dict:
    """Facts that help explain differences between runs later."""
    def first_line(cmd: list[str]) -> str:
        try:
            return subprocess.check_output(cmd, text=True, timeout=15,
                                           stderr=subprocess.DEVNULL).strip().splitlines()[0]
        except Exception:
            return ""
    cpu = ""
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    repo = Path(__file__).resolve().parents[2]
    image = first_line(["docker", "inspect", "-f", "{{.Config.Image}}", container])
    return {
        "hostname": first_line(["hostname", "-f"]),
        "kernel": first_line(["uname", "-r"]),
        "cpu_model": cpu,
        "git_commit": first_line(["git", "-C", str(repo), "rev-parse", "HEAD"]),
        "git_branch": first_line(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"]),
        "llama_cpp_ref": first_line(["docker", "inspect", "-f",
                                     '{{index .Config.Labels "llama_cpp_ref"}}', image or container]),
    }


def drop_caches() -> None:
    # After a 405B load the page cache can hold ~250 GB, and dropping it takes
    # far longer than the old 10 s limit (the run crashed with TimeoutExpired).
    t0 = time.time()
    try:
        subprocess.run(
            ["sudo", "-n", "sh", "-c", "sync && echo 3 > /proc/sys/vm/drop_caches"],
            check=True, capture_output=True, timeout=900.0,
        )
        print(f"  caches dropped in {time.time() - t0:.0f}s", flush=True)
    except subprocess.CalledProcessError as e:
        print(f"  ⚠  drop_caches failed: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        print("  ⚠  drop_caches still running after 900s; continuing anyway")


def start_collector(
    out_dir: Path,
    pid: int,
    duration_s: float,
    perf_interval_ms: int,
    proc_interval_s: float,
    collector_path: str,
    llm_cpus: str = "",
    perf_cpu: str = "",
) -> tuple[subprocess.Popen, object]:
    log_path = out_dir / "collector_stdout.log"
    log_f = open(log_path, "w", encoding="utf-8")
    cmd = [
        "python3", collector_path,
        "--out_dir", str(out_dir),
        "--pid", str(pid),
        "--duration_s", str(duration_s),
        "--perf_interval_ms", str(perf_interval_ms),
        "--proc_interval_s", str(proc_interval_s),
    ]
    if llm_cpus and perf_cpu:
        cmd += ["--llm_cpus", llm_cpus, "--perf_cpu", perf_cpu]
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
    return proc, log_f


def stop_collector(proc: subprocess.Popen, log_f, kill_timeout: float = 20.0) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=kill_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log_f.close()


def send_prompt(
    prompt_text: str,
    n_predict: int,
    server_url: str,
    timeout_s: float,
) -> tuple[bool, str]:
    payload = json.dumps({
        "prompt": prompt_text,
        "n_predict": n_predict,
        "ignore_eos": True,
        "logit_bias": [[2, -100.0]],
    })
    try:
        out = subprocess.check_output(
            [
                "curl", "-sS",
                "--max-time", str(int(timeout_s)),
                server_url,
                "-H", "Content-Type: application/json",
                "-d", payload,
            ],
            text=True,
            timeout=timeout_s + 5.0,
        )
        return True, out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run() -> None:
    _script_dir = Path(__file__).resolve().parent
    _model_dir = _script_dir.parent / "model"
    _default_config = str(_model_dir / "model_config.sh")

    import sys
    _cfg_path = _default_config
    for i, a in enumerate(sys.argv[1:]):
        if a == "--model_config" and i + 1 < len(sys.argv) - 1:
            _cfg_path = sys.argv[i + 2]

    cfg = load_model_config(_cfg_path)

    ap = argparse.ArgumentParser(
        description="Full-trace HAT runner: all prompts under one continuous collector.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--json", required=True,
                    help="Path to prompts JSON.")
    ap.add_argument("--label", required=True,
                    help="Condition label ('emotional' or 'neutral').")
    ap.add_argument("--run_label", required=True,
                    help="Name for this trace run (used as output directory name).")
    ap.add_argument("--model_config", default=_default_config,
                    help="Path to model_config.sh.")
    ap.add_argument("--container", default="mccviahat-llama",
                    help="Docker container name.")
    ap.add_argument("--reset_script",
                    default=str(_model_dir / "reset_server.sh"),
                    help="Path to reset_server.sh.")
    ap.add_argument("--collector",
                    default=str(
                        _script_dir.parent / "collectors" / "substrate_collector.py"
                    ),
                    help="Path to substrate_collector.py.")
    ap.add_argument("--server_url", default="http://localhost:8000/completion",
                    help="llama.cpp completion endpoint.")
    ap.add_argument("--n_predict", type=int, default=50,
                    help="Tokens to generate per prompt.")
    ap.add_argument("--baseline_s", type=float,
                    default=float(cfg["BASELINE_S"]),
                    help="Stabilisation sleep after initial reset (s).")
    ap.add_argument("--per_request_timeout_s", type=float,
                    default=float(cfg["REQUEST_TIMEOUT"]),
                    help="Hard timeout for each curl request (s).")
    ap.add_argument("--reset_timeout_s", type=float,
                    default=float(cfg["RESET_TIMEOUT"]),
                    help="Max time to wait for reset_server.sh (s).")
    ap.add_argument("--proc_interval_s", type=float,
                    default=float(cfg["PROC_INTERVAL_S"]),
                    help="/proc sampling interval for collector (s).")
    ap.add_argument("--perf_interval_ms", type=int, default=1,
                    help="perf stat bucket interval (ms).")
    ap.add_argument("--llm_cpus", type=str, default="",
                    help="CPU cores for LLM container (enables taskset).")
    ap.add_argument("--perf_cpu", type=str, default="",
                    help="CPU core for perf stat (enables taskset).")
    ap.add_argument("--inter_prompt_pause_s", type=float, default=0.0,
                    help="Optional pause between prompts (s). Default 0 = back-to-back.")
    ap.add_argument("--output-root", type=Path,
                    default=_script_dir.parents[1] / "data" / "raw" / "fulltrace" / "incoming",
                    help="Parent directory for raw recordings; choose a new batch folder for new experiments.")
    args = ap.parse_args()

    # ── Print config ──────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  FULL-TRACE MODE")
    print(f"  Model              : {cfg['MODEL_SIZE']}")
    print(f"  Condition          : {args.label}")
    print(f"  Run label          : {args.run_label}")
    print(f"  Inter-prompt pause : {args.inter_prompt_pause_s}s")
    print(f"{'='*60}\n")

    # ── Load prompts ──────────────────────────────────────────────────────────
    prompts: list[dict] = json.load(open(args.json, "r", encoding="utf-8"))
    if not isinstance(prompts, list) or len(prompts) == 0:
        raise SystemExit("JSON must be a non-empty list of objects.")
    n_prompts = len(prompts)
    print(f"Loaded {n_prompts} prompts  label={args.label}")

    # ── Output directory ──────────────────────────────────────────────────────
    out_dir = args.output_root / args.run_label
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {out_dir}\n")

    # ── 1. Reset server once ──────────────────────────────────────────────────
    print("→ resetting server ...", flush=True)
    try:
        reset_s = reset_server(args.reset_script, timeout=args.reset_timeout_s)
    except RuntimeError as e:
        raise SystemExit(f"Reset failed: {e}")
    print(f"  reset took {reset_s:.0f}s", flush=True)

    # ── 2. Drop caches + baseline sleep ───────────────────────────────────────
    print("→ dropping caches ...", flush=True)
    drop_caches()

    print(f"→ baseline sleep {args.baseline_s}s ...", flush=True)
    time.sleep(args.baseline_s)

    # ── 3. Resolve container PID ──────────────────────────────────────────────
    pid = get_container_pid(args.container)
    print(f"  Container '{args.container}' → host PID {pid}")

    # ── 4. Start collector ONCE ───────────────────────────────────────────────
    # Estimate total duration: n_prompts * per_request_timeout + buffer
    max_trace_duration_s = n_prompts * args.per_request_timeout_s + 60.0
    print(f"→ starting collector (max duration {max_trace_duration_s:.0f}s) ...", flush=True)
    collector_proc, collector_log_f = start_collector(
        out_dir=out_dir,
        pid=pid,
        duration_s=max_trace_duration_s,
        perf_interval_ms=args.perf_interval_ms,
        proc_interval_s=args.proc_interval_s,
        collector_path=args.collector,
        llm_cpus=args.llm_cpus,
        perf_cpu=args.perf_cpu,
    )

    # ── 5. Send all prompts back-to-back ──────────────────────────────────────
    prompt_log = []
    t_trace_start = time.time_ns()
    all_ok = True

    for i, prompt_obj in enumerate(prompts):
        instr = prompt_obj.get("instructions", "")
        if not isinstance(instr, str) or not instr.strip():
            raise SystemExit(f"Prompt {i}: missing or empty 'instructions' field.")

        print(f"  [{i+1}/{n_prompts}] sending prompt {i} ...", end=" ", flush=True)

        t_req_start = time.time_ns()
        ok, response_raw = send_prompt(
            prompt_text=instr,
            n_predict=args.n_predict,
            server_url=args.server_url,
            timeout_s=args.per_request_timeout_s,
        )
        t_req_end = time.time_ns()
        elapsed_ms = (t_req_end - t_req_start) / 1e6

        print(f"ok={ok}  elapsed={elapsed_ms:.0f}ms", flush=True)

        prompt_log.append({
            "prompt_index": i,
            "label": args.label,
            "ok": ok,
            "t_request_start_ns": t_req_start,
            "t_request_end_ns": t_req_end,
            "elapsed_ms": elapsed_ms,
            "t_offset_from_trace_start_ms": (t_req_start - t_trace_start) / 1e6,
        })

        if not ok:
            all_ok = False
            print(f"    ⚠ prompt {i} failed: {response_raw[:200]}")

        # Optional inter-prompt pause (default 0 = truly back-to-back)
        if args.inter_prompt_pause_s > 0 and i < n_prompts - 1:
            time.sleep(args.inter_prompt_pause_s)

    t_trace_end = time.time_ns()

    # ── 6. Stop collector ─────────────────────────────────────────────────────
    print("→ stopping collector ...", flush=True)
    stop_collector(collector_proc, collector_log_f)

    # ── 7. Write metadata ─────────────────────────────────────────────────────
    # Per-prompt timing log
    (out_dir / "prompt_log.json").write_text(
        json.dumps(prompt_log, indent=2), encoding="utf-8"
    )

    # Overall trace metadata
    trace_meta = {
        "mode": "fulltrace",
        "label": args.label,
        "run_label": args.run_label,
        "model_size": cfg["MODEL_SIZE"],
        "n_prompts": n_prompts,
        "n_predict": args.n_predict,
        "inter_prompt_pause_s": args.inter_prompt_pause_s,
        "all_ok": all_ok,
        "t_trace_start_ns": t_trace_start,
        "t_trace_end_ns": t_trace_end,
        "total_trace_ms": (t_trace_end - t_trace_start) / 1e6,
        "load_mode": cfg.get("LOAD_MODE", "mmap"),
        "server_extra_args": os.environ.get("MCC_SERVER_ARGS") or cfg.get("SERVER_ARGS", ""),
        "reset_s": round(reset_s, 1),
        **node_info(args.container),
    }
    (out_dir / "trace_meta.json").write_text(
        json.dumps(trace_meta, indent=2), encoding="utf-8"
    )

    total_s = (t_trace_end - t_trace_start) / 1e9
    n_ok = sum(1 for p in prompt_log if p["ok"])
    print(f"\n✓ Done. {n_ok}/{n_prompts} prompts OK in {total_s:.1f}s")
    print(f"  Trace output: {out_dir}/")
    print(f"  prompt_log.json: per-prompt timing offsets")
    print(f"  trace_meta.json: overall trace metadata")


if __name__ == "__main__":
    run()
