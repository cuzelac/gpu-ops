# gpu-ops design

Signed off 2026-11-11. This is the design the code follows; change it here first, then in code.

## Context

- **Step 1 is done.** The research picked RunPod as the first adapter, and your restricted key passed read-only checks on 2026-11-11 (see `docs/research/2026-11-provider-comparison.md`).
- **Who uses gpu-ops:** mainly AI coding agents; you also use it by hand on the CLI.
- **Budget:** usually under $50 a month, occasionally around $150.
- **The repo stays project-agnostic.** Projects bring a small job spec, and nothing project-specific goes in the core.

**Decisions taken as defaults at sign-off (revisit here if they change):**
1. **Spend gate for agents:** a Claude Code ask-rule.
   - In each calling project, `gpu-ops run` without `--dry-run` asks you first.
   - `validate`, dry-run, `ps`, `spend`, `kill` and `reap` are auto-allowed.
2. **Scheduled reaper:** a Windows scheduled task runs `gpu-ops reap` every 30 minutes. I install it only after you approve the install.
3. **Default tier:** the cheapest offer from either tier. A spec can demand `secure`.

## Terms (one-line definitions, reused word for word in `docs/glossary.md`)

- **Job spec:** a TOML file in your project that says what to run, on what GPU, with which files, and the most it may cost.
- **Provider:** a company that rents GPUs, such as RunPod.
- **Adapter:** the gpu-ops code that talks to one provider's API.
- **Offer:** one rentable GPU option, with a type, a count, a tier and an hourly price.
- **Instance:** one rented machine running your container. RunPod calls it a pod.
- **Run:** one execution of a job spec, from creating the instance to deleting it.
- **Session:** one instance kept alive across several runs, so that you pay setup and teardown once.
- **Overhead:** the paid time in a run that is not your command: provisioning, bootstrap, upload, setup, download and teardown.
- **Run record:** the JSON file that says what a run did, what it cost and what it produced.
- **Ledger:** gpu-ops' local list of instances it believes are alive.
- **Dead-man timer:** a timer on the instance that deletes the instance at its deadline even if your PC is off.
- **Reaper:** a command that deletes any gpu-ops instance past its deadline.
- **Policy:** your user-level limits (per job, per month, allowed providers) that a job spec can tighten but never loosen.
- **Teardown:** deleting the instance and confirming with the provider that it is gone.
- **Manifest:** a list of files with their SHA-256 hashes, used to prove what arrived is what was sent.

## 1. Job spec

- **Format:** TOML, read with Python's built-in `tomllib` and validated with pydantic.
- **Self-describing:** `gpu-ops schema` prints the JSON Schema, so an agent can write a valid spec without reading the source.
- **Paths:** paths in the spec are relative to the spec file. Windows paths such as `C:/...` are converted with `wslpath`.

```toml
name    = "hs-qlora-v1b"          # becomes part of the instance name
project = "hs-ai-coach"           # for spend reports
kind    = "fine-tune"             # fine-tune | batch-inference | serve | other (spend reports)

[image]
ref = "docker.io/runpod/pytorch@sha256:…"   # digest required; `gpu-ops pin-image <ref>` fills it in

[run]
setup   = ["pip install -r $GPUOPS_IN/code/requirements.txt"]   # runs before command; counted in the time cap
command = "python -u $GPUOPS_IN/code/train_qlora.py"   # one shell line, run with bash
ports   = []                      # e.g. ["8000/http"] for a serving job (later)

[env]                             # plain values only; never secrets
DATA_DIR = "$GPUOPS_IN/data"
OUT_DIR  = "$GPUOPS_OUT/lora"
EPOCHS   = "2"

[[inputs]]                        # local path -> /gpuops/in/<name>; s3:// reserved for later
name = "code"
from = "../tools/local-model"
include = ["train_qlora.py", "requirements.txt"]

[[inputs]]
name = "data"
from = "C:/Users/cuzel/models/round-a/a2/data-v1b"

[[outputs]]                       # paths under /gpuops/out, pulled to runs/<run-id>/out/
path = "lora/adapter"
required = true
[[outputs]]
path = "lora/train-record.json"
required = true                   # success = required outputs exist and checksums match, not exit code alone

[gpu]
min_vram_gb = 24
count = 1
tier = "any"                      # any | secure | community
select = "cheapest-hour"          # cheapest-hour | cheapest-job | fastest   (see "Choosing an offer")
allow = []                        # optional: only these GPU ids
prefer = []                       # optional: tried first if the price is within 10% of the cheapest
max_price_per_hr = 0              # optional ceiling; 0 = no ceiling beyond the cost cap

[limits]                          # both required; no "unlimited"
max_minutes  = 60
max_cost_usd = 2.00

[secrets]                         # resolved at run time via op.exe; never written to disk, logs or the provider's pod config
# HF_TOKEN = "op://Private/HuggingFace/token"
```

**Instance layout:**
- `/gpuops/in/<name>/` holds the uploaded inputs.
- `/gpuops/out/` holds outputs.
- `/gpuops/work/` is scratch space.
- `GPUOPS_IN`, `GPUOPS_OUT` and `GPUOPS_RUN_ID` are set for the command.

## 2. Architecture

```
src/gpu_ops/
  cli.py        init | schema | validate | pin-image | plan | run | ps | kill | reap | spend   (every command: --json)
  spec.py       pydantic models for the job spec; path resolution
  policy.py     ~/.config/gpu-ops/policy.toml: per-job max, monthly max, allowed providers, max concurrent, card price
  planner.py    gather offers from every configured adapter -> pick cheapest that fits -> worst-case cost
  runner.py     the lifecycle below, inside try/finally
  remote.py     Remote protocol: run(cmd), put(local, remote), get(remote, local); SshRemote uses system ssh + rsync
  ledger.py     ~/.local/state/gpu-ops/ledger.jsonl and runs/<run-id>/ (record.json, logs, out/)
  manifest.py   SHA-256 manifests, inputs hash
  secrets.py    op.exe read, redaction filter on all log output
  pricing.py    cost estimate from price × billed seconds
  providers/base.py     Provider protocol + Offer/Instance dataclasses
  providers/fake.py     in-memory provider with injectable failures (tests, dry-run demos)
  providers/runpod.py   REST v2 via httpx
```

**Provider protocol:**
- **Methods:** `offers(req)`, `create(name, offer, image, cmd, env, ports, disk_gb, public_key)`, `get(id)`, `list(prefix)`, `terminate(id)` (idempotent), `billed_cost(id, since)` and `self_terminate_snippet()`.
- **No `stop`.** The RunPod FAQ says a stopped pod can come back with no GPU, so gpu-ops only ever deletes instances.
- **Multi-provider ready:**
  - `planner` collects offers from every adapter allowed by policy and the spec.
  - `ps`, `kill` and `reap` loop over all adapters.
  - A second adapter adds one file under `providers/` and touches no core code.
  - Each adapter also gets a `prepare_models` hook for its cheapest way to stage weights. In v1 the hook does nothing, and weights are pulled from the HF Hub on the instance.
- **RunPod specifics found in the live check:**
  - Skip any catalog price on a tier whose `maxCount` is 0. Those are placeholder prices.
  - Names carry the `gpuops-` prefix. Tags can't be set through the API yet.
  - The per-job SSH key goes in the `PUBLIC_KEY` env var.
  - REST v2 only. v1 retires on 15 Nov 2026.

**Dependencies:** httpx, pydantic, pytest. No Typer: argparse keeps the dependency list short. SSH and rsync are the system binaries, run in WSL.

## 3. Run lifecycle (`gpu-ops run job.toml`)

1. **Validate:** spec, policy, image digest present, inputs exist.
2. **Hash inputs:** build the inputs manifest and the inputs hash.
3. **Plan:**
   - Take the cheapest fitting offer.
   - Worst case = price × `max_minutes`.
   - Refuse with exit 3 if the worst case exceeds the spec cap, the policy per-job cap, or what is left of the policy monthly cap (this month's records plus this job's worst case).
   - Refuse with exit 3 if the policy concurrency limit is reached.
   - **`--dry-run` stops here** and prints the plan as JSON.
4. **Deadline:** `min(max_minutes, max_cost_usd / price_per_hr × 60)` from now.
5. **Name:** `gpuops-<run-id>-d<deadline-epoch>`. The deadline is in the name, so the reaper works even if the local ledger is lost.
6. **Ledger first:** write a `creating` entry before calling create, and update it with the instance id afterwards.
7. **Create the instance:**
   - Generate a throwaway ed25519 key in `$XDG_RUNTIME_DIR` (memory-backed). Delete it at teardown.
   - The pod `cmd` is a small bootstrap. It installs `openssh-server` and `rsync` if the image lacks them, writes `PUBLIC_KEY`, starts sshd, then sleeps.
   - v1 supports Debian and Ubuntu based images only.
8. **Wait until ready:** poll until running and SSH answers. Give up after 10 minutes, counted against the time cap.
9. **Arm the dead-man timer:**
   - Start a background `sleep <seconds left>; <self-terminate>` on the instance, using RunPod's injected `RUNPOD_POD_ID` and the pod-scoped key, if one exists.
   - The run record says `deadman: armed` or `deadman: unarmed`. It is never silently skipped.
   - If it is unarmed (no injected key), the run continues only for jobs of 2 hours or less, unless the policy allows more.
10. **Upload:** rsync the inputs, then verify the manifest on the instance.
11. **Execute:**
    - Run `setup`, then `command`, over SSH.
    - Secrets go into that process's environment only.
    - Output streams to `runs/<id>/job.log`, which is redacted.
    - The local timer enforces the deadline and kills the command at it.
12. **Collect:**
    - Build a manifest of `/gpuops/out` on the instance, then rsync it down.
    - Verify every hash locally.
    - A missing required output, or any mismatch, is a failure. Downloaded files are kept and marked unverified.
13. **Teardown** (in `finally`, and on SIGINT and SIGTERM):
    - Call `terminate`, then confirm with `get` that the instance is gone. Retry with backoff for up to 5 minutes.
    - If that still fails: exit 10, print `TEARDOWN FAILED — INSTANCE MAY STILL BE BILLING: <id>`, and leave the ledger entry `live`.
14. **Record:** write `record.json` with:
    - provider, GPU, tier, data centre, image digest
    - start, end and billed seconds
    - estimated cost; the provider-reported cost is filled in later by `spend --reconcile`
    - inputs hash, output manifest, exit code
    - whether the dead-man timer was armed
    - the job kind and project

**Exit codes (documented in AGENTS.md):**

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Spec invalid |
| 3 | Refused by policy or limits |
| 4 | No offer or capacity |
| 5 | Provider or API error before the job ran |
| 6 | Job failed, or required outputs missing |
| 7 | Transfer or checksum failed |
| 8 | Time or cost cap hit; job killed |
| 10 | Teardown failed. Always the most important code, and returned even if another failure also happened |

### Choosing an offer: VRAM is a floor, not the target

- **Why the default can be wrong:** a bigger, faster card can finish sooner and cost less in total.
- **The three `select` modes:**
  - `cheapest-hour` (v1 default): the lowest $/hr that meets `min_vram_gb`.
  - `cheapest-job`: the lowest estimated *total* cost, which is $/hr × expected hours on that GPU.
  - `fastest`: the shortest expected time within the cost cap.
- **Where "expected hours" comes from:**
  - A per-GPU speed factor, measured from your own run records: same `project` and `kind`, command time per GPU.
  - Until a GPU has a record, its factor comes from `~/.config/gpu-ops/gpu-speed.toml`, which you can edit.
  - I won't seed that file with benchmark figures I haven't verified. It starts empty, so v1 falls back to `cheapest-hour` and says why.
- **Phasing:**
  - v1 builds the `select` field, the `allow` / `prefer` / `max_price_per_hr` filters and the per-GPU timing records.
  - `cheapest-job` and `fastest` switch on once records exist. A good reason to run the QLoRA proof on two different GPUs.

### Overhead and sessions

- **Every run record times each phase:** provisioning, bootstrap, upload, setup, command, download and teardown.
- **`spend` reports overhead** as a share of paid time, per project and job kind. That shows when the next two features would pay off.
- **Sessions (designed now, built after the QLoRA proof):**
  - Commands: `gpu-ops session start <spec>` → `gpu-ops session exec <session> <job.toml>` (repeatable) → `gpu-ops session end`.
  - Same caps, dead-man timer and reaper rules as a run. The deadline covers the whole session.
  - An **idle timeout** (default 15 min with no `exec`) ends the session, so a forgotten warm machine dies on its own.
  - **Rule of thumb, printed by `spend`:** keep the instance warm when the gap between jobs costs less than one round of overhead (gap × $/hr < overhead × $/hr). That is, when jobs come closer together than their own overhead.
- **What v1 does to make this possible:** the runner is built as three parts, *acquire* (create, bootstrap, arm), *execute* (upload, setup, command, download) and *release* (teardown). A session is acquire once, execute many times, release once, so adding it later needs no rewrite.
- **The other big overhead lever:** setup time. Installing pip packages on every run is slow. Later, a prebuilt image per project (same pinned deps, pushed to a registry and pinned by digest) cuts setup to seconds. The phase timings will show whether that is worth doing.

**Long jobs:** v1 runs in the foreground only. A detached mode (`run --detach`, `status`, `collect`) comes after the QLoRA proof. Until then, start long runs from a WSL shell inside tmux, because WSL kills child processes when the launching `wsl.exe` exits.

## 4. Safety layers

Each layer covers the failure of the one before it.

| # | Layer | Covers | Status |
|---|---|---|---|
| 1 | Pre-flight cost check against spec, policy per-job and policy monthly caps | Over-budget jobs never start | Design |
| 2 | `try/finally` teardown plus signal handlers | Job fails, script errors, Ctrl-C | Design |
| 3 | Ledger written before create; deadline encoded in the name | Script killed mid-create | Design |
| 4 | Dead-man timer on the instance | PC off, asleep or crashed | **Unverified**: depends on RunPod injecting a key. Proved or disproved by the kill test |
| 5 | Scheduled reaper every 30 min (Windows Task Scheduler → WSL → `gpu-ops reap`) | Layer 4 unarmed, and the PC wakes up later | Default yes; I install it only after you approve |
| 6 | `gpu-ops ps` and `gpu-ops kill --all` ask the provider directly, not just the ledger | Any leftover | Design |
| 7 | Prepaid balance with auto-pay off | Everything above failing | You set this in the RunPod console |

**How the reaper works without the ledger:**
- The reaper never reads the ledger. It asks each provider for every instance whose name starts with `gpuops-`.
- It reads the deadline from the name (`gpuops-<run-id>-d<deadline-epoch>`), and deletes those more than 10 minutes past it.
- A `gpuops-` instance whose name can't be parsed is deleted if it is older than the policy's longest allowed job, based on the provider's `createdAt`.
- Instances without the prefix are never touched, so your manual pods are safe.
- The ledger only adds detail to the reaper's report, for example "this one belonged to run X of project Y". If the ledger is lost, the reaper loses no capability.

**Resolved 2026-11-12: unattended access uses a 1Password service account** (option 1 below), with its token in Windows Credential Manager. See `docs/unattended.md`. The desktop app remains the fallback. Verified live 2026-11-16: commands read the key with the desktop route disabled.

**Open item (historical): the scheduled reaper and 1Password.** Seen on 2026-11-11: a `gpu-ops plan` call from a new `wsl.exe` process failed with 1Password "authorization timeout" when nobody approved the prompt. gpu-ops reports this as exit 3, not a crash.
- An unattended task has to read the RunPod key. Through `op.exe` that needs the 1Password desktop app unlocked, and it may show an approval prompt.
- So the reaper probably fails exactly when you're away. I will test this, not assume it.
- If it does fail, the options for you to choose from then:
  1. A 1Password service account limited to a vault that holds only the RunPod key, with its token stored in Windows Credential Manager (encrypted to your Windows login).
  2. Accept that the reaper only works while 1Password is unlocked. The dead-man timer remains the main guard.
- Either way, a failed key read is logged and raises a Windows notification, never failing silently.

**Honest limit:**
- The policy file is a convention. Any process running as you can edit it.
- The real hard stops are your prepaid balance and the Claude Code ask-rule. AGENTS.md tells agents never to edit the policy.

## 5. Agent interface and docs

- **Machine-readable output:** every command accepts `--json` and returns one documented shape, `{ok, exit_code, run_id, ...}`.
- **No interactive prompts anywhere.**
- **Ask-rule snippet** (`docs/claude-settings-snippet.json`) for a calling project's `.claude/settings.json`:
  - Allowed without asking: `schema`, `validate`, `plan`, `pin-image`, `ps`, `spend`, `kill`, `reap`.
  - Ask first: `run`.
  - Free planning is its own command, `gpu-ops plan` (same as `run --dry-run`), because an ask rule for `gpu-ops run` would also catch `run --dry-run`.
  - I show you the exact snippet before adding it to HS-AI-Coach.
- **MCP:** not in v1. The core is a library, so an MCP server would be a thin wrapper added later.
- **Docs:**
  - `README.md`: what gpu-ops is, install, a 5-minute walkthrough.
  - `AGENTS.md`: commands, JSON shapes, exit codes, the policy, "always validate and dry-run first", "on exit 10 run `gpu-ops kill --all` and tell the human".
  - `docs/job-spec.md`: every field, with one example per job kind.
  - `docs/runbook.md`: how to find and kill anything billing, both with gpu-ops and in the RunPod console.
  - `docs/glossary.md`: the terms above.

## 6. Spend tracking and rent vs buy

- **`gpu-ops spend`** reads `runs/*/record.json` and shows:
  - Dollars and GPU-hours by month, provider, project and job kind.
  - The trailing three-month monthly rate.
  - Cumulative spend against the card price in your policy, and the months to break even at the current rate.
  - How many jobs a single 96 GB card could not have run (more than 96 GB, or more than one GPU).
  - A warning when the trailing rate passes your `warn_monthly_usd`.
- **`spend --reconcile`** fills in the provider-reported cost from `GET /v2/billing/pods?podId=…`, and shows any gap between estimate and bill rather than hiding it.
- **The JSON shape is the stable interface** that the later report page reads, so v1 already collects everything the page needs.
- **After v1: a spend report page.**
  - `gpu-ops spend --html` writes one self-contained page with charts and plain-language descriptions:
    - monthly spend
    - spend by project and job kind
    - rent-vs-buy break-even progress
    - overhead share
    - GPU-hours by card
  - It can be published as a private Artifact link to view on any device.
- **Two numbers I need from you:** `card_price_usd` and `warn_monthly_usd`, for the policy file. Tell me when convenient; placeholders until then.

## 7. Data movement

- **v1:**
  - rsync over SSH in both directions.
  - SHA-256 manifests at both ends.
  - Weights are pulled from the HF Hub on the instance. RunPod charges no egress, so this costs only GPU time.
- **Later, per provider, behind `prepare_models`:**
  - A RunPod network volume loaded through its S3 API.
  - Object-storage `s3://` inputs and outputs.
  - The spend report will show when download time starts costing more than the $0.07/GB-month to keep weights on a volume.

## Step 3: build order (each step verified before the next)

1. **Repo scaffold and docs.** pyproject, uv, the docs above, and the policy file with placeholders. **No spend.**
2. **Core against the fake provider and a local fake remote.** Tests for:
   - Spec and policy validation, and refusal with exit 3.
   - Dry-run.
   - Teardown on: job failure, script SIGTERM mid-run, transfer failure, checksum mismatch, and a cap hit.
   - Exit 10 when terminate keeps failing.
   - Reaper deadline parsing.

   **No spend.**
3. **RunPod adapter, read-only parts:** `offers` (placeholder-price filter), `list`, `billed_cost`, and `gpu-ops run --dry-run` against live prices. **No spend.**
4. **Smoke job.** I ask first; expected about $0.03.
   - The cheapest offer of at least 16 GB runs `nvidia-smi` and a tiny script, and writes a file.
   - The file comes back with a matching checksum.
   - Then teardown, cost printed, and `ps` empty.
   - This also reports whether RunPod injects a key, which arms layer 4.
5. **Kill test.** I ask first; under $0.10.
   - Start a job, `kill -9` the gpu-ops process, and confirm the dead-man timer deletes the pod.
   - Separately, confirm `kill --all` and `reap` work.
   - Then the reaper scheduled task, if you approve it.
6. **HS-AI-Coach QLoRA.** I ask first; about $0.35–0.55.
   - Add two files to HS-AI-Coach with your OK: `tools/local-model/gpu-ops/qlora.job.toml` and `requirements.txt` frozen from `~/qlora-venv`.
   - `train_qlora.py` runs unchanged.

## Verification

- `uv run pytest` passes, covering the fake-provider teardown cases listed in step 2.
- `gpu-ops run examples/smoke/job.toml --dry-run --json` shows a live RunPod offer and worst-case cost, and creates nothing.
- The smoke job's returned file hash matches. `record.json` has an estimated cost, and after `spend --reconcile` a billed cost too. `gpu-ops ps` and the RunPod console both show 0 pods.
- The kill test leaves 0 pods without help from my PC, or I report that layer 4 is unarmed.
- QLoRA: `adapter/adapter_model.safetensors` and `train-record.json` exist locally with matching hashes. Eval loss is in the same range as the local run (0.94 / 0.90 / 0.96 by epoch). This is a sanity check, not an exact match.
- Agent test: a fresh agent given only `AGENTS.md` writes a spec that passes `validate` and `--dry-run`.
- I will say plainly what was run and seen, versus what was only written.

## Not in v1

- Sessions (warm instances across runs). The runner's acquire, execute and release split is in v1.
- Offer selection by `cheapest-job` or `fastest`. The fields and timing records are in v1.
- The HTML spend report page.
- Prebuilt per-project images.
- Detached runs.
- Serving jobs with exposed ports.
- Network volumes and S3 inputs.
- A second adapter: Modal or Vast.
- MCP.
- Non-Debian images.
- Commits happen only when you ask.
