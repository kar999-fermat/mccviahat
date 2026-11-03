# MCCviaHAT: start-to-finish guide (70B and 405B)

Everything from putting the new code on GitHub to having the data in the
notebook. Commands marked **💻 laptop** run in a terminal on your Mac. Commands
marked **🖥 node** run on the CloudLab node, after you SSH in.

Throughout, replace:
- `YOURCLOUDLAB` with your CloudLab username
- `232` with your node's number (from its hostname, e.g. `clnode232.clemson.cloudlab.us`)

---

## Part 0. Put the new code on your own branch (once) 💻 laptop

1. Download `faster-405b-changed-files.zip` (it goes to **Downloads**).
2. On GitHub, open https://github.com/salviromain/mccviahat and click **Fork → Create fork**
   (this makes `github.com/kar999-fermat/mccviahat`).
3. In Terminal:

```bash
cd ~/Desktop/mccviahat                      # wherever your copy lives
git switch main
git pull origin main
git switch -c faster-405b
unzip -o ~/Downloads/faster-405b-changed-files.zip

# quick check that nothing is broken
for f in scripts/model/*.sh; do bash -n "$f" && echo "OK $f"; done
python3 -m py_compile scripts/run/run_prompts_fulltrace.py scripts/run/check_trace.py && echo "Python OK"

# upload to your fork (code only; never `git add -A`, never commit your HF token)
git remote add fork https://github.com/kar999-fermat/mccviahat.git
git add scripts docker .gitignore CHANGES_405B.md RUN_GUIDE_START_TO_FINISH.md
git commit -m "Faster 405B fetch and loading"
git push -u fork faster-405b
```

If GitHub asks for a password, use a personal access token (GitHub → Settings →
Developer settings → Personal access tokens). If `git remote add` says the remote
exists, skip that line. `git switch main` always takes you back to Romain's code.

---

## Part 1. Reserve nodes (CloudLab website)

1. CloudLab → **Experiments → Reserve Nodes**.
2. Project **MCCviaHAT**, cluster **Cloudlab Clemson**, type **c6420**.
3. **Ask for 2–3 nodes, not 13.** Small, short requests get approved faster.
4. Fill in **Number of Days** (1), leave Start/End empty, click **Search**. It finds the earliest slot.
5. Reason, for example:
   > Collecting hardware performance traces (Neutral, Emotional, MechanicalPrime prompts) on Llama 70B and 405B for Prof. Ouri Wolfson's MCCviaHAT research at UIC. Each node runs three traces with a reboot between runs.

**Time budget.** A 70B trace takes about 20 min plus a few minutes of loading.
Expect a 405B trace to take several times longer, likely a couple of hours,
plus about 30 min of loading after every reboot. That's an estimate until your
first real run.

## Part 2. Start the experiment (CloudLab website)

Follow the video on page 4 of Romain's slides, and select your reserved c6420
nodes. If you'll use more than one node for 405B, it helps to put them in **one
experiment** so they share a fast network, but separate experiments in the same
cluster also work.

Find each node's SSH command under **List View**.

## Part 3. Connect 💻 laptop

```bash
ssh YOURCLOUDLAB@clnode232.clemson.cloudlab.us
```

**Use `tmux` for anything long.** It keeps the command running if your SSH
connection drops, which has happened to you before:

```bash
tmux new -s mcc          # start a session (on the node)
# ...run commands...
# if SSH drops, reconnect and run:
tmux attach -t mcc
```

## Part 4. Set up the node (once per node, per reservation) 🖥 node

```bash
git clone https://github.com/kar999-fermat/mccviahat.git
cd mccviahat
git switch faster-405b
bash scripts/node/bootstrap_node.sh
newgrp docker
export HF_TOKEN="PASTE_YOUR_TOKEN"      # only needed for downloading from Hugging Face
```

Check the hardware once. Note the results; they go in your update to Romain:

```bash
lsblk -d -o NAME,ROTA,SIZE     # ROTA=1 means a spinning hard drive
free -g                        # total RAM (405B needs ~300+ GB)
nproc                          # CPU threads (scripts use CPUs 0-61)
```

### 4a. For 70B

```bash
bash scripts/model/model_fetch.sh 70b
bash scripts/model/llama_build.sh
```

### 4b. For 405B: first node (downloads from Hugging Face)

```bash
tmux new -s mcc
bash scripts/model/model_fetch.sh 405b      # all 6 shards in parallel, then checksum checks
bash scripts/model/llama_build.sh
```

If it stops or reports an error, **run the same command again**. Finished
shards are kept and partial ones resume.

### 4c. For 405B: other nodes (copy from the first node, much faster)

On the **first** node, once its download finished:

```bash
bash scripts/model/serve_model.sh           # prints the exact command for the other nodes
```

On **each other** node (after Part 4's setup lines):

```bash
# quick check that the first node is reachable (should print HTTP/1.1 200 OK):
curl -sI http://clnode231.clemson.cloudlab.us:8888/Meta-Llama-3.1-405B-Instruct-Q4_K_M-00001-of-00006.gguf | head -1

MCC_MODEL_MIRROR=http://clnode231.clemson.cloudlab.us:8888 bash scripts/model/model_fetch.sh 405b
bash scripts/model/llama_build.sh
```

(Use the first node's real hostname. If the check fails, just use Hugging Face on
that node, as in 4b.)

When every node has the model, **stop the mirror on the first node** before
recording anything there:

```bash
bash scripts/model/serve_model.sh stop
```

### Switching models
`model_fetch.sh` decides which model every script uses. To switch on a node that
already has both, run `bash scripts/model/model_fetch.sh 70b` (or `405b`). It
won't re-download.

---

## Part 5. Record traces (Romain's task for this week) 🖥 node

**Naming (from Romain's Nov 2 email):** `node#_1N`, `node#_1E`, `node#_1MP`, then
`node#_2N`, `node#_2E`, `node#_2MP` for a second set on the same node and day.

**Folders:** one batch folder per model, e.g. `batch_007_70b` and
`batch_007_405b`. *Confirm the folder names and the MechanicalPrime `--label`
with Romain; `mechanicalprime` is used below.*

**Reboot after every trace.** That's essential.

```bash
cd ~/mccviahat
# 1) Neutral
python3 scripts/run/run_prompts_fulltrace.py \
  --json prompts/20base/independentN.json \
  --label neutral --run_label 232_1N \
  --output-root data/raw/fulltrace/batch_007_70b
python3 scripts/run/check_trace.py data/raw/fulltrace/batch_007_70b/232_1N
sudo reboot
```

Wait about 5 minutes, SSH back in (Part 3), then:

```bash
cd ~/mccviahat
# 2) Emotional
python3 scripts/run/run_prompts_fulltrace.py \
  --json prompts/20base/independentE.json \
  --label emotional --run_label 232_1E \
  --output-root data/raw/fulltrace/batch_007_70b
python3 scripts/run/check_trace.py data/raw/fulltrace/batch_007_70b/232_1E
sudo reboot
```

SSH back in, then:

```bash
cd ~/mccviahat
# 3) MechanicalPrime
python3 scripts/run/run_prompts_fulltrace.py \
  --json prompts/20base/independentMP.json \
  --label mechanicalprime --run_label 232_1MP \
  --output-root data/raw/fulltrace/batch_007_70b
python3 scripts/run/check_trace.py data/raw/fulltrace/batch_007_70b/232_1MP
sudo reboot
```

Repeat with `_2N`, `_2E`, `_2MP` if there's time. **For 405B**, run
`bash scripts/model/model_fetch.sh 405b` once first (it only rewrites the config)
and use `--output-root data/raw/fulltrace/batch_007_405b`. Run 405B traces inside
`tmux`, since each takes a long time.

What the checker means:
- **PASS:** good to send.
- **WARN:** read the note. A naming warning is easy to fix by renaming the folder. "RAM grew during prompt 1" means weights were still loading during recording; tell Romain.
- **FAIL:** a prompt failed or files are missing; re-record that trace (after a reboot).

After a reboot:
- The Docker group, the model and the config are all still there. No need to repeat Part 4.
- For 405B, check `ls /mydata/model_cache/llama/405b` still lists 6 files.
- `HF_TOKEN` is gone, which is fine, since it's only needed for downloading.

---

## Part 6. Copy the data to your laptop 💻 laptop

```bash
rsync -avP "YOURCLOUDLAB@clnode232.clemson.cloudlab.us:~/mccviahat/data/raw/fulltrace/batch_007_70b/" \
  "$HOME/Desktop/mccviahat/data/raw/fulltrace/batch_007_70b/"
```

Repeat for each node and for `batch_007_405b`. Keep the trailing `/` on both paths.

## Part 7. Features and notebook 💻 laptop

These are Romain's Oct 29 steps:

```bash
cd ~/Desktop/mccviahat
.mccvenv/bin/python scripts/run/extract_features_fulltrace.py \
  data/raw/fulltrace/batch_007_70b \
  --output-dir data/features/recomputed/fulltrace/batch_007_70b
```

Then run the cells of `analysis/notebooks/fulltrace_analysis_matched.ipynb`
(Romain said he'll fold the new data into the analysis himself, so check with
him before changing the notebook).

## Part 8. Finish

When you're done, **terminate the experiment** on CloudLab (Experiment page →
Terminate) so the nodes go back to the pool. That also helps your next
reservation get approved.

---

## Appendix: measuring the 405B improvement for Romain 🖥 node

Load times only compare fairly **right after a reboot** (cold disk cache), on
the **same node**. Every reset adds a row to `~/mcc_reset_times.csv`.

```bash
cd ~/mccviahat
MCC_LOAD_MODE=mmap    bash scripts/model/model_fetch.sh 405b && bash scripts/model/reset_server.sh
sudo reboot     # SSH back in
MCC_LOAD_MODE=no-mmap bash scripts/model/model_fetch.sh 405b && bash scripts/model/reset_server.sh
sudo reboot     # SSH back in
MCC_LOAD_MODE=dio     bash scripts/model/model_fetch.sh 405b && bash scripts/model/reset_server.sh
cat ~/mcc_reset_times.csv
```

Each reset prints its time and the **lowest free RAM during loading**. If free RAM
drops near 0 GB or the container dies, try again with weight repacking off:

```bash
MCC_LOAD_MODE=no-mmap MCC_SERVER_ARGS="--no-repack" bash scripts/model/model_fetch.sh 405b
bash scripts/model/reset_server.sh
```

**Before recording real data,** set the load mode back to the one you and Romain
choose (default `no-mmap`: `bash scripts/model/model_fetch.sh 405b`), and keep it
the same for every 405B trace. Each trace records its load mode in
`trace_meta.json`.

Send Romain a small table: load mode, load time, lowest free RAM, plus the fetch
time `model_fetch.sh` printed ("Download finished in N min").
