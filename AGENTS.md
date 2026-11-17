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
| `gpu-ops schema` | free | Prints the job spec's JSON Schema. Read it to write a spec. |
| `gpu-ops validate SPEC` | free | Checks the spec and that its inputs exist. |
| `gpu-ops plan SPEC` | free | Picks an offer and prices the worst case against policy. Creates nothing. Same as `run --dry-run`. |
| `gpu-ops run SPEC` | **money** | The full run: create, upload, setup, command, download and verify, then delete the machine. Blocks until done. |
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

Every command prints one object, which always has `ok` (bool) and `exit_code` (int). On failure it also has `error` (str).

- **`plan`:**
  ```json
  {"ok": true, "exit_code": 0, "dry_run": true, "job": "smoke",
   "plan": {"offer": {"provider": "runpod", "gpu_id": "NVIDIA RTX A5000", "gpu_name": "RTX A5000", "vram_gb": 24,
                      "count": 1, "tier": "community", "price_per_hr": 0.16, "available": 10},
            "run_minutes": 20.0, "worst_case_usd": 0.0533, "month_spent_usd": 0.0,
            "alternatives": [...], "notes": []}}
  ```
- **`run`:**
  - Fields: `{"ok", "exit_code", "run_id", "run_dir", "record": {...}}`.
  - Useful record fields:

    | Field | Meaning |
    |---|---|
    | `status` | Whether the run succeeded |
    | `error` | What went wrong, if anything |
    | `cost_estimate_usd` | Estimated cost of the run |
    | `teardown_confirmed` | **Must be true.** The provider confirmed the machine is gone |
    | `outputs_verified` | Outputs match their checksums |
    | `outputs_manifest` | Each output file and its hash |
    | `deadman` | `armed` or `unarmed` |
    | `phases` | Seconds spent in each phase |

  - Outputs land in `<run_dir>/out/<output path>`.
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
