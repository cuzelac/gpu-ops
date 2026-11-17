# AGENTS.md: contract for AI coding agents using gpu-ops

gpu-ops rents a cloud GPU, runs a job, brings the results home, and deletes the machine.
**Real runs cost the human money.** Read this whole file before your first call.

## Rules

1. **Always `validate`, then `plan`, before any `run`.** Both are free and create nothing.
2. **Never edit the policy or provider files** in `~/.config/gpu-ops/` (`policy.toml`, `providers.toml`).
   - If a plan is refused by policy (exit 3), tell the human the numbers. Don't work around them.
3. **Never put a secret in a job spec.**
   - Secrets are `op://…` references under `[secrets]`. A literal value is rejected.
4. **On exit 10, act at once.** Run `gpu-ops kill --all --json`, then tell the human. A machine may still be billing.
5. **Prefer `--json`** and branch on `exit_code`, never on message text.
   - Output is short by default. Use `--get` to pull single fields, and `--full` only when you need the whole record.
   - If the job writes its own metrics to a file, put that file in `[report]` and read `report` in the result,
     instead of opening files under `runs/<id>/out`.
6. **`gpu-ops run` (without `--dry-run`) is set to ask the human** in Claude Code projects that use gpu-ops.
   - That is expected. Don't try to get around it.

## Calling it

- **In WSL:** `gpu-ops …`
- **From Git Bash or PowerShell on Windows:** `C:\Users\cuzel\src\gpu-ops\bin\gpu-ops …` (or `bin\gpu-ops.ps1`).
  - It runs the same command inside WSL, in the current directory.
  - Spec paths may be relative, `/mnt/c/…` or `C:/…`.

## Commands

| Command | Cost | What it does |
|---|---|---|
| `gpu-ops schema` | free | The job spec's JSON Schema, under `schema`. Read it to write a spec. |
| `gpu-ops validate SPEC` | free | Checks the spec and that its inputs exist. |
| `gpu-ops plan SPEC` | free | Picks an offer and prices the worst case against policy. Creates nothing. Same as `run --dry-run`. |
| `gpu-ops run SPEC` | **money** | The full run: create, upload, setup, command, download and verify, then delete the machine. Blocks until done. Prints a compact result; `--full` prints the whole record, `--progress` streams the job's output to stderr. |
| `gpu-ops logs RUN_ID` | free | Lines from a finished run's `job.log`. `--tail N` (default 200, `0` = all), `--grep REGEX`, `--phase setup\|command`. |
| `gpu-ops schema --get PATH` | free | `--get` drills into the schema, e.g. `--get 'schema.$defs.LimitsSpec.properties'`. |
| `gpu-ops ps` | free | Live gpu-ops machines, as the provider reports them. |
| `gpu-ops kill ID… \| --all` | stops spend | Deletes gpu-ops machines. Never touches machines without the `gpuops-` prefix. |
| `gpu-ops reap [--dry-run]` | stops spend | Deletes gpu-ops machines more than 10 minutes past their deadline. |
| `gpu-ops spend [--reconcile]` | free | Spend and usage report. `--reconcile` fetches the provider's billed amounts. |
| `gpu-ops pin-image REF` | free | Turns `name:tag` into the `name@sha256:…` form a spec requires. |

## Exit codes (stable)

| Code | Meaning | What to do |
|---|---|---|
| 0 | OK | |
| 1 | Internal error (a gpu-ops bug) | Report it to the human, with the JSON. |
| 2 | Spec invalid | Fix the spec. `error` lists each field. |
| 3 | Refused by policy, or a 1Password read failed | Tell the human. A 1Password approval prompt may be waiting for them. |
| 4 | No offer or capacity | Loosen `[gpu]` (tier, `min_vram_gb`, `allow`), or try later. |
| 5 | Provider/API error before the job ran | Retry once later. If it repeats, tell the human. |
| 6 | Job failed, or required outputs missing | Read `runs/<id>/job.log`. |
| 7 | Upload/download failed, or checksums did not match | Outputs in `runs/<id>/out` are **unverified**. Do not use them. |
| 8 | Time or cost cap hit; the job was killed | Raise the limits in the spec, within policy, after asking the human. |
| 10 | **Teardown failed. A machine may still be billing** | `gpu-ops kill --all --json`, then tell the human immediately. |

When several things fail, 10 always wins. Otherwise the first failure is reported.

## JSON shapes

Every command prints one object, which always has `ok` (bool) and `exit_code` (int) — `schema` included, with the schema itself under `schema`. On failure it also has `error` (str). One function supplies those two keys for every command, so they cannot go missing.

**`--get PATH[,PATH]` works on every command** and prints only those fields, as JSON, with or without `--json`.
Use it instead of parsing the result yourself:

```bash
gpu-ops plan job.toml --get plan.offer.price_per_hr,plan.worst_case_usd
gpu-ops run job.toml --get exit_code,cost_estimate_usd,report.lora/train-record.json.eval_loss
```

A path is dotted; list items are indexed (`instances.0.id`); a key that itself contains dots (an output or
report file name) matches as a whole, and you can keep drilling past it. **A path that does not exist is
exit 2**, and the error says which fields were available — *except* when the command itself already failed:
then its own exit code stands, the whole result is printed, and the `--get` complaint goes to stderr. A
mistyped field name can never hide an exit 10.

**stderr is quiet.** Phase lines go to stderr only when it is a terminal, or with `run --progress`; the job's
own output only with `--progress`. `runs/<id>/job.log` is always written either way — read it with `gpu-ops logs`.

- **`plan`:**
  ```json
  {"ok": true, "exit_code": 0, "dry_run": true, "job": "smoke",
   "plan": {"offer": {"provider": "runpod", "gpu_id": "NVIDIA RTX A5000", "gpu_name": "RTX A5000", "vram_gb": 24,
                      "count": 1, "tier": "community", "price_per_hr": 0.16, "available": 10},
            "run_minutes": 20.0, "worst_case_usd": 0.0533, "month_spent_usd": 0.0,
            "alternatives": [...], "notes": []}}
  ```
- **`run`** prints this, and nothing more, unless you ask for more:
  ```json
  {"ok": true, "exit_code": 0, "run_id": "20261112T140915-7a06", "status": "ok", "error": null,
   "gpu": "RTX A5000", "tier": "community", "price_per_hr": 0.16, "minutes": 0.9,
   "cost_estimate_usd": 0.0103, "teardown_confirmed": true, "outputs_verified": true,
   "outputs_dir": "/home/you/.local/state/gpu-ops/runs/20261112T140915-7a06/out",
   "outputs": {"nvidia-smi.txt": 1673, "hello.json": 88}, "deadman": "armed", "notes": [],
   "report": {"lora/train-record.json": {"eval_loss": 0.42, "best_epoch": 2}}, "log_tail": null}
  ```

  **Every key is always there.** `report` is `null` when the spec has no `[report]` table and `log_tail` is
  `null` when the run succeeded, so you can read any field without guarding for its absence.

  | Field | Meaning |
  |---|---|
  | `status` | Whether the run succeeded |
  | `error` | What went wrong, if anything |
  | `cost_estimate_usd` | Estimated cost of the run |
  | `teardown_confirmed` | **Must be true.** The provider confirmed the machine is gone |
  | `outputs_verified` | Outputs match their checksums |
  | `outputs` | Each **declared** output: a file's size in bytes, a directory as `{"bytes": n, "files": m}`. The result never grows with the number of files the job wrote. `bytes` is `null` when something could not be read, so `0` always means empty. The files are in `outputs_dir` |
  | `deadman` | `armed` or `unarmed` |
  | `notes` | Anything gpu-ops wants you to know (capacity fallback, a missing report file, …) |
  | `report` | **Look here first for the job's own results.** `null` when the spec has no `[report]` table |
  | `log_tail` | The last 20 lines of `job.log` when the run failed; `null` when it succeeded |

  - **`report`** holds one entry per `[report]` file: a `.json` file as the parsed object, anything else as
    `{"text": "…"}`. A file over `max_bytes` is `{"truncated": true, "bytes": N, "text": "…"}`, one that is
    missing is `null`, and a `.json` file that does not parse is `{"text": "…", "parse_error": "…"}`.
    If the outputs failed verification (exit 7) there is no `report` at all — gpu-ops never reads unverified data.
  - **`--full`** gives `{"ok", "exit_code", "run_id", "run_dir", "record": {...}}` instead: the whole record,
    including `phases`, `outputs_manifest` (path → hash), `inputs_hash`, `inputs_file_count` and `worst_case_usd`.
  - Per-file **input** hashes are not in the record at all; they are in `<run_dir>/inputs-manifest.json`.
  - Outputs land in `<run_dir>/out/<output path>`.
- **`logs`:** `{"ok", "exit_code", "run_id", "path", "lines_in_file", "lines_matched", "lines": [...]}`.
  Without `--json` it prints just the lines. A `RUN_ID` that is not a run id is exit 2, and nothing is created.
- **`spend --reconcile`:** adds `reconciled_now` (int) and `reconcile_skipped` (list of strings). A record it
  cannot read is skipped and listed there; the report is still produced.
- **`ps`:** `{"instances": [{"provider", "id", "name", "status", "deadline", "minutes_to_deadline", "age_minutes", "cost_so_far_usd", ...}]}`
- **`spend`:** `total_usd`, `trailing_3mo_monthly_usd`, `overhead_share`, `rent_vs_buy{...}`, `by{month,provider,project,kind,gpu}`, `warning`.

## Writing a job spec

- See [docs/job-spec.md](docs/job-spec.md) for every field, and `examples/` for working specs.
- **Where it lives:** in the calling project, not in this repo.
- **What it must state:**
  - an image pinned by digest
  - `[limits] max_minutes` and `max_cost_usd`
  - `[gpu] min_vram_gb`
- **What happens on the machine:**
  - Your command runs with bash in `$GPUOPS_WORK`.
  - Inputs are under `$GPUOPS_IN/<input name>/`.
  - Anything you want back must be written under `$GPUOPS_OUT` and listed in `[[outputs]]`.

## Known limits (v1)

- **Images:** Debian or Ubuntu based only. The machine installs `openssh-server`, `rsync` and `curl` at boot if they are missing.
- **No serving jobs, sessions, detached runs or network volumes yet.**
- **1Password prompts:** without a service account ([docs/unattended.md](docs/unattended.md)), each gpu-ops call reads the provider key through the Windows 1Password app.
  - A call can fail with exit 3 ("authorization timeout") if nobody approves the prompt.
  - Tell the human rather than retrying in a loop.
- **Dead-man timer:** verified on RunPod 2026-11-12. If `record.deadman` is `unarmed`, `record.notes` says what was missing.
- **Killed runs:** a run whose gpu-ops process died (exit code null, status `started`) is counted in `spend` at its upper bound until reconciled. Check `gpu-ops ps` for leftovers.
