# Job spec reference

A **job spec** is a TOML file in your project that says what to run, on what GPU, with which files, and the most it may cost.
`gpu-ops schema` prints the same rules as JSON Schema, and `gpu-ops validate SPEC` checks a file.
Unknown fields are errors, so a typo never silently does nothing.

Paths are relative to the spec file. `C:/…` paths work, and become `/mnt/c/…` in WSL.

## Fields

### Top level

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Short id: lowercase letters, digits and `-`, up to 31 characters. Becomes part of the machine name. |
| `project` | yes | Used to group spend reports. |
| `kind` | no | `fine-tune`, `batch-inference`, `serve` or `other` (default). Used in spend reports and future offer choice. |

### `[image]`

| Field | Required | Meaning |
|---|---|---|
| `ref` | yes | Image pinned by digest: `registry/name@sha256:<64 hex>`. Get one with `gpu-ops pin-image name:tag`. Debian or Ubuntu based in v1. |

### `[run]`

| Field | Required | Meaning |
|---|---|---|
| `setup` | no | List of shell lines run before `command`, e.g. `pip install`. Timed separately as **overhead**. |
| `command` | yes | One shell line that does the work. |
| `disk_gb` | no | Container disk, 10–2000, default 50. It is wiped at teardown. |
| `ports` | no | Must be empty in v1 (serving comes later). |

How `setup` and `command` run:
- With bash and `set -euo pipefail`, in `$GPUOPS_WORK`.
- These variables are set: `$GPUOPS_IN`, `$GPUOPS_OUT`, `$GPUOPS_WORK`, `$GPUOPS_RUN_ID`.
- The deadline is enforced on the machine, and the command is killed at it (exit 8).
- **All `setup` lines run in one shell, one after another. `command` then runs in a separate, new shell.** So:
  - Variables you `export` in `setup` are **not** visible in `command`. Use `[env]`, or set them in `command`.
  - **Start servers in `command`, not `setup`.** For a job that needs a background service (e.g. `ollama serve`), put the server start, its health check and the job itself in `command`, for example `ollama serve >$GPUOPS_WORK/ollama.log 2>&1 & until curl -sf localhost:11434; do sleep 1; done; ...`. A background process started in `setup` is not guaranteed to survive into `command`.
- **`$GPUOPS_IN` is writable.** Inputs are verified right after upload, before `setup` runs, so changing them afterwards is harmless. Still prefer `$GPUOPS_WORK` for scratch files.

### `[env]`

- Plain `NAME = "value"` pairs, never secrets.
- `$GPUOPS_IN` and similar are expanded, for example `DATA_DIR = "$GPUOPS_IN/data"`.

### `[secrets]`

- `NAME = "op://vault/item/field"` pairs.
- They are read from 1Password at run time and passed only into the job's process environment.
- They are never written to disk, logs, the run record, or the provider's machine configuration.
- They are redacted from `job.log`.

### `[[inputs]]` (repeatable)

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Uploaded to `$GPUOPS_IN/<name>/`. |
| `from` | yes | A local file or directory. |
| `include` | no | Only these files **or directories** (directories are sent with everything beneath them), relative to `from`. Keeps unrelated files from being uploaded. |

Every input file is hashed. The hashes are checked again on the machine after upload, and their combined hash is the run record's `inputs_hash`.

### `[[outputs]]` (repeatable)

| Field | Required | Meaning |
|---|---|---|
| `path` | yes | A file or directory under `$GPUOPS_OUT`. It is brought back to `runs/<run-id>/out/<path>`. |
| `required` | no | Default true. A missing required output is a job failure (exit 6). |

- Only listed outputs come back, so large checkpoints you don't list are not downloaded.
- Each file is hashed on the machine and checked after download (exit 7 on a mismatch).

### `[gpu]`

| Field | Default | Meaning |
|---|---|---|
| `min_vram_gb` | required | Minimum VRAM per GPU. **A floor, not a target.** |
| `count` | 1 | GPUs per machine (1–8). |
| `tier` | `any` | `any`, `secure` or `community`. See the [glossary](glossary.md). |
| `select` | `cheapest-hour` | `cheapest-job` and `fastest` need timing records from earlier runs. Until those exist, gpu-ops falls back to `cheapest-hour` and says so. |
| `allow` | `[]` | Only these GPU ids. Ids come from `gpu-ops plan` output (`gpu_id`). |
| `prefer` | `[]` | Chosen if within 10% of the cheapest. |
| `max_price_per_hr` | 0 | 0 means no ceiling beyond the cost cap. |
| `providers` | `[]` | Restrict to these providers, within what the policy allows. |

### `[limits]` (both required)

| Field | Meaning |
|---|---|
| `max_minutes` | Wall-clock cap for the whole run, from provisioning to teardown. |
| `max_cost_usd` | Cost cap for the whole run. |

How the deadline is set:
- The run's deadline is the shorter of `max_minutes` and the minutes that `max_cost_usd` buys at the chosen price.
- The policy can refuse the run: per-job cap, monthly cap, longest allowed job, and number of machines at once.

## Examples

### Smoke test (`examples/smoke/job.toml`)

```toml
name = "smoke"
project = "gpu-ops"

[image]
ref = "docker.io/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"

[run]
command = "nvidia-smi | tee $GPUOPS_OUT/nvidia-smi.txt && python $GPUOPS_IN/code/hello.py"
disk_gb = 20

[[inputs]]
name = "code"
from = "."
include = ["hello.py"]

[[outputs]]
path = "nvidia-smi.txt"
[[outputs]]
path = "hello.json"

[gpu]
min_vram_gb = 16

[limits]
max_minutes = 20
max_cost_usd = 0.25
```

### Fine-tune (QLoRA), shape of the HS-AI-Coach job

```toml
name = "hs-qlora-v1b"
project = "hs-ai-coach"
kind = "fine-tune"

[image]
ref = "docker.io/runpod/pytorch@sha256:…"     # gpu-ops pin-image runpod/pytorch:<tag>

[run]
setup = ["pip install -r $GPUOPS_IN/code/requirements.txt"]
command = "python -u $GPUOPS_IN/code/train_qlora.py"

[env]
DATA_DIR = "$GPUOPS_IN/data"
OUT_DIR = "$GPUOPS_OUT/lora"
EPOCHS = "2"

[[inputs]]
name = "code"
from = ".."
include = ["train_qlora.py", "requirements.txt"]

[[inputs]]
name = "data"
from = "C:/Users/cuzel/models/round-a/a2/data-v1b"

[[outputs]]
path = "lora/adapter"
[[outputs]]
path = "lora/train-record.json"

[gpu]
min_vram_gb = 24

[limits]
max_minutes = 60
max_cost_usd = 2.0
```

### Batch inference with a large model (shape only; not built or tested yet)

```toml
name = "teacher-sweep"
project = "hs-ai-coach"
kind = "batch-inference"

[image]
ref = "docker.io/vllm/vllm-openai@sha256:…"

[run]
command = "python $GPUOPS_IN/code/sweep.py --model $MODEL --prompts $GPUOPS_IN/prompts/prompts.jsonl --out $GPUOPS_OUT/answers.jsonl"

[env]
MODEL = "Qwen/…"                                # pulled from the Hugging Face Hub on the machine

[secrets]
HF_TOKEN = "op://Private/HuggingFace/token"     # only if the model is gated

[[inputs]]
name = "code"
from = "."
include = ["sweep.py"]

[[inputs]]
name = "prompts"
from = "data"
include = ["prompts.jsonl"]

[[outputs]]
path = "answers.jsonl"

[gpu]
min_vram_gb = 80
count = 2

[limits]
max_minutes = 180
max_cost_usd = 20.0
```
