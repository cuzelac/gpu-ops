# gpu-ops

Rent a cloud GPU, run a job, bring the results home, and shut it down. Every time, even when something fails.

- **Who it's for:** your projects describe a job in a small TOML file, the **job spec**. gpu-ops does the rest, and the core stays project-agnostic.
- **AI agents:** read [AGENTS.md](AGENTS.md).
- **Humans:** read on.

## Status (2026-11-12)

| Piece | State |
|---|---|
| Core: spec, policy, planner, run lifecycle, teardown | Built. 56 tests pass against a fake provider |
| RunPod adapter | Built. Uses live stock, falls back past out-of-stock GPUs |
| Smoke job | **Passed on RunPod 2026-11-12**: run `20261112T140915-7a06`, PRO 6000 MIG 24GB (secure), 54 s billed, est. $0.0103. Outputs verified, teardown confirmed, 0 pods left |
| Kill test | **Passed 2026-11-12.** Two jobs; both gpu-ops processes killed with `kill -9` mid-command. Job A's dead-man timer deleted it on its own (gone 15 s after its deadline); `kill --all` deleted job B; 0 pods left |
| Dead-man timer on RunPod | **Works.** RunPod injects `RUNPOD_POD_ID`/`RUNPOD_API_KEY`; the fix was saving them at boot, because SSH sessions don't inherit the container's environment |
| `reap` deleting a real machine | Not exercised live (the dead-man got there first). Same teardown code as `kill`, plus unit tests |
| `spend --reconcile` against RunPod billing | Not verified: RunPod returned no billing records for these runs yet (lag?) |
| HS-AI-Coach QLoRA job | Not run yet. Needs your OK, because it spends money |
| Sessions, detached runs, serving, volumes, HTML spend report, second provider | Designed, not built ([docs/design.md](docs/design.md)) |

## Install (once, inside WSL)

```bash
cd /mnt/c/Users/cuzel/src/gpu-ops
uv tool install --editable .     # puts `gpu-ops` on your WSL PATH
gpu-ops init                     # writes ~/.config/gpu-ops/policy.toml and providers.toml. Review both.
```

- `providers.toml` holds the 1Password reference to the RunPod key, never the key itself.
- gpu-ops reads keys through the Windows `op.exe`, so 1Password may ask you to approve.

From Windows (Git Bash or PowerShell), call `bin/gpu-ops` or `bin/gpu-ops.ps1`. They run the same command inside WSL.

## Five-minute walkthrough

1. **Check a spec.** Free.
   ```bash
   gpu-ops validate examples/smoke/job.toml
   ```
2. **See what it would rent, and the worst-case cost.** Free; nothing is created.
   ```bash
   gpu-ops plan examples/smoke/job.toml
   ```
   On 2026-11-11 this chose an RTX A5000 (community) at $0.16/hr, with a worst case of $0.05.
3. **Run it.** This costs money.
   ```bash
   gpu-ops run examples/smoke/job.toml
   ```
   What happens:
   - A machine is created.
   - The inputs are uploaded and checked.
   - The command runs, and the outputs come back and are checked.
   - The machine is deleted and the deletion confirmed.
   - The cost is printed, and the record is saved under `~/.local/state/gpu-ops/runs/<run-id>/`.
4. **Check that nothing is left, and see the spend so far.**
   ```bash
   gpu-ops ps
   gpu-ops spend
   ```

## Safety, in one paragraph

- **Before creating anything,** gpu-ops refuses runs whose worst case breaks your spec's caps or your policy (per job, per month, machines at once).
- **Teardown always runs:** on success, on failure, on errors, on Ctrl-C and on SIGTERM. It then asks the provider to confirm the machine is gone.
- **The deadline is in the machine's name,** so `gpu-ops reap` and the RunPod console can spot overdue machines with no local state.
- **A dead-man timer** on the machine deletes it at the deadline even if your PC is off. This is not yet verified on RunPod.
- **The final backstop** is a small prepaid balance with auto-pay off.
- Details: [docs/design.md](docs/design.md) §4, and [docs/runbook.md](docs/runbook.md) for what to do when something is still billing.

## Docs

| Doc | What it covers |
|---|---|
| [AGENTS.md](AGENTS.md) | The contract for coding agents: commands, JSON, exit codes, rules |
| [docs/job-spec.md](docs/job-spec.md) | Every spec field, with examples |
| [docs/design.md](docs/design.md) | How it works, and why |
| [docs/runbook.md](docs/runbook.md) | Finding and killing anything still billing |
| [docs/glossary.md](docs/glossary.md) | One-line definitions |
| [docs/research/2026-11-provider-comparison.md](docs/research/2026-11-provider-comparison.md) | Provider research and prices |
| [docs/claude-settings-snippet.json](docs/claude-settings-snippet.json) | Permission rules for a calling project: free commands allowed, `run` asks you |

## Development

```bash
uv sync && uv run pytest
```

Tests run in WSL. They use the fake provider (`GPUOPS_ENABLE_FAKE=1`) and spend nothing.
