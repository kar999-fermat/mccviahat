# Faster, more reliable 405B setup

Branch: `faster-405b` (Ayush Kar). Changes only the model setup/loading scripts and
adds checks; the prompts, collector and feature extraction are unchanged. The 70B
setup keeps its current loading behaviour, so new 70B traces stay comparable with
existing ones.

## The three problems and what changed

### 1. Fetching 405B is very slow
`model_fetch.sh` downloaded the six shards one after another, one connection each.

- Missing shards now download **in parallel with `aria2c`** (8 connections per
  shard, resumable). It installs automatically; if it can't, the script falls back
  to parallel `curl`.
- Every shard is checked against the **size and SHA256 published by Hugging Face**
  before it gets its final name. Before, any non-empty file counted as done, so a
  cut-off download could have been loaded silently.
- A wrong-size shard is detected and resumed instead of being treated as complete.

### 2. Loading 405B on several nodes in one cluster is slow
Every node pulled 246 GB from Hugging Face over the same uplink, and `/mydata`
disappears when an experiment ends, so every reservation started from zero.

- **New `scripts/model/serve_model.sh`**: once one node has the shards, it serves
  them to the other nodes over the cluster network (nginx in Docker, read-only,
  resumable). The other nodes run
  `MCC_MODEL_MIRROR=http://<that-node>:8888 bash scripts/model/model_fetch.sh 405b`.
- **Still to decide (needs project storage):** a CloudLab *long-term dataset*
  holding the 405B shards, mounted on each node. The scripts already support it:
  `MCC_MODEL_DIR=<mount point> bash scripts/model/model_fetch.sh 405b` uses the shards
  in place. That would remove the 246 GB download from every reservation.

### 3. The server reset before inference takes forever
Each `sudo reboot` clears the page cache, so each trace re-reads 246 GB from the
node's disk (likely spinning HDDs on c6420). Changes:

- **The 405B health window was shorter than a cold load could take.** It was
  180 × 10 s = 30 min, while reading 246 GB at HDD speed is about 25–30 min, so
  resets could time out right at the end of loading. It is now 400 × 10 s, with
  `RESET_TIMEOUT` 4200 s.
- **New `LOAD_MODE` setting (405B default: `no-mmap`).** The server only reports
  healthy after every weight is in RAM, so no trace starts while weights are still
  being paged in from disk. Other values for comparison: `mmap` (old behaviour),
  `mlock`, `dio` (DirectIO, skips the page cache). Set with
  `MCC_LOAD_MODE=<mode> bash scripts/model/model_fetch.sh 405b`.
- **`reset_server.sh` now reports** elapsed time and the lowest free RAM during the
  load, stops waiting right away if the container dies (e.g. out of memory) and
  prints its last log lines, and appends a row to `~/mcc_reset_times.csv` so load
  times can be compared.
- **The runner now shows reset progress live.** Before, it captured the output,
  so a 30+ min 405B load looked frozen.
- Optional extra server flags are saved in the config, e.g.
  `MCC_SERVER_ARGS="--no-repack" bash scripts/model/model_fetch.sh 405b`. This is
  worth trying if free RAM gets close to zero during the load: on the 70B traces
  the server already holds ~76 GB for a ~42 GB model file, and the same ratio for
  405B would be more than the node's RAM.

## Reproducibility additions
- **llama.cpp is pinned** (`LLAMA_CPP_REF`, default tag `b12060`) in the Dockerfile.
  Before, every build cloned whatever was newest that day, so nodes built on
  different days could run different inference code. Note: in this llama.cpp version the
  old `--no-mmap`/`--mlock` flags became `--load-mode`; the scripts use the new
  flag. **Romain: if you know the llama.cpp version used for the existing 70B
  data, we could pin that instead.**
- `trace_meta.json` now also records `load_mode`, `server_extra_args`, `reset_s`,
  `hostname`, `kernel`, `cpu_model`, `git_commit`, `git_branch` and `llama_cpp_ref`.
- **New `scripts/run/check_trace.py`** checks a recording before it's handed over:
  files present, all prompts OK, server RAM not growing during prompt 1 (that would
  mean weights were still loading), first-prompt slowdown, run-label naming.
  On the existing 70B joy traces: RAM is flat (76 GB) from the start of the trace,
  and prompt 1 is ~5 s slower than the rest (normal warm-up), so the 70B data
  doesn't show any weights still loading during recording.
- `scripts/model/model_config.sh` is now in `.gitignore` (it is written per node).

## How it was tested
- The download logic was run against a local mirror with small stand-in shards:
  parallel download, a shard with a bad checksum (caught, deleted, the other
  shards still verified), a cut-short shard (detected and resumed), a
  half-finished `.part` (resumed), and a re-run with everything present (skips).
  The `curl` fallback was exercised; the `aria2c` path still needs its first real
  run on a node.
- `llama_up.sh` / `reset_server.sh` were run with a stand-in Docker to check the
  exact flags for each load mode, the memlock limit for `mlock`, timing/CSV
  logging and the container-died path. The flags were checked against the
  pinned llama.cpp source.
- The runner was run end to end with a stand-in server and collector; the new
  `trace_meta.json` fields are written correctly.
- **Not yet done:** a real 405B load on a c6420. The next step is to time
  `mmap` vs `no-mmap` vs `dio` after a reboot each, on the same node.
