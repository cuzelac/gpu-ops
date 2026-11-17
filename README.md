# gpu-ops

Rent a cloud GPU, run a job, bring the results home, and shut it down. Every time, even when something fails.

- **Who it's for:** your projects describe a job in a small TOML file, the **job spec**. gpu-ops does the rest, and the core stays project-agnostic.
- **AI agents:** read [AGENTS.md](AGENTS.md).
- **Humans:** read on.

## Status (2026-11-16)

| Piece | State |
|---|---|
| Core: spec, policy, planner, run lifecycle, teardown | Built. 61 tests pass against a fake provider |
| RunPod adapter | Built. Uses live stock, falls back past out-of-stock GPUs |
| Smoke job | **Passed on RunPod 2026-11-12**: run `20261112T140915-7a06`, PRO 6000 MIG 24GB (secure), 54 s billed, est. $0.0103. Outputs verified, teardown confirmed, 0 pods left |
| Kill test | **Passed 2026-11-12.** Two jobs; both gpu-ops processes killed with `kill -9` mid-command. Job A's dead-man timer deleted it on its own (gone 15 s after its deadline); `kill --all` deleted job B; 0 pods left |
| Dead-man timer on RunPod | **Works.** RunPod injects `RUNPOD_POD_ID`/`RUNPOD_API_KEY`; the fix was saving them at boot, because SSH sessions don't inherit the container's environment |
| `reap` deleting a real machine | Not exercised live (the dead-man got there first). Same teardown code as `kill`, plus unit tests |
| Scheduled reaper (Windows Task Scheduler) | **Installed and running 2026-11-16.** Every 30 min while you're logged on, hidden; first scheduled pass 16:50 succeeded. Failure notification tested |
| `spend --reconcile` against RunPod billing | Not verified: RunPod returned no billing records for these runs yet (lag?) |
| Unattended 1Password access (service account) | **Verified 2026-11-16.** `ps` and `plan` read the RunPod key from the `gpu-ops` vault with the desktop-app route disabled; no prompt, ~4.6 s per command |
| HS-AI-Coach QLoRA job | Not run yet. Needs your OK, because it spends money |
| Sessions, detached runs, serving, volumes, HTML spend report, second provider | Designed, not built ([docs/design.md](docs/design.md)) |

## Install (once, inside WSL)

```bash
cd /mnt/c/Users/cuzel/src/gpu-ops
uv tool install --editable .     # puts `gpu-ops` on your WSL PATH
gpu-ops init                     # writes ~/.config/gpu-ops/policy.toml and providers.toml. Review both.
```

- `providers.toml` holds the 1Password reference to the RunPod key, never the key itself.
- Out of the box, gpu-ops reads the key through the Windows 1Password app (`op.exe`), so 1Password may ask you to approve each command.

From Windows (Git Bash or PowerShell), call `bin/gpu-ops` or `bin/gpu-ops.ps1`. They run the same command inside WSL.

## Running while you're away (no 1Password prompts)

Each `gpu-ops` command is a new process, so each one can trigger a 1Password approval prompt. That's fine at the desk, but it breaks anything unattended:
- **The scheduled reaper** would sit waiting on a prompt, then fail with exit 3.
- **`ps` / `kill` from a script** would do the same.

The fix is a **1Password service account**: a non-human 1Password identity that can read one vault with no prompt.

**How it works**
- **One vault, read-only.** The RunPod key moves to its own `gpu-ops` vault, and the service account gets read-only access to that vault only. It can't see your Private vault or anything else.
- **Token storage.** The service account's token is kept in **Windows Credential Manager** under `gpu-ops/op-service-account`, encrypted to your Windows login. It is never in a file, a log or a commit.
- **How gpu-ops uses it.** Each command fetches the token, passes it to `op` only through that process's environment (never its arguments), and forgets it when the command ends.
- **Fallback.** If no token is stored, or the service-account read fails, gpu-ops says why on stderr and falls back to the 1Password app prompt.

**Setup (about 10 minutes)**
1. **Vault:** in 1Password, create a vault named `gpu-ops` and move the **Runpod API Keys** item into it.
2. **Service account:** on 1Password.com, create a service account named `gpu-ops`, with **read** access to the `gpu-ops` vault only. Its token is shown once.
3. **Store the token:** run this in PowerShell and paste the token (input is hidden):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Users\cuzel\src\gpu-ops\bin\gpuops-credential.ps1 set
   ```
4. **Point gpu-ops at the moved item:** in `~/.config/gpu-ops/providers.toml`, set `api_key_ref = "op://gpu-ops/Runpod API Keys/credential"`.
5. **Check:** lock 1Password and run `gpu-ops ps`. It should answer with no prompt.

**The trade-off**
- **Who can read the stored token:** Windows protects it from other users and machines, but any program running as *you* could read it. That's the same protection as browser-saved passwords.
- **Limiting the damage:** that's why the service account is read-only on one vault holding only a RunPod key limited to pods, catalog and billing.

**Undo**
- **Remove the stored token:** `bin\gpuops-credential.ps1 remove`. gpu-ops goes back to prompts.
- **Kill the token everywhere:** revoke the service account in 1Password.
- **Skip it for one command:** `GPUOPS_NO_SERVICE_ACCOUNT=1 gpu-ops …`

Full detail, including rate limits: [docs/unattended.md](docs/unattended.md).

## Scheduled reaper

A Windows scheduled task, `\gpu-ops\gpu-ops reaper`, runs `gpu-ops reap` every 30 minutes. It deletes any `gpuops-…` machine more than 10 minutes past the deadline in its name. It's the backstop for when both a run's own teardown and the on-machine dead-man timer fail.

**How it runs**
- **As you, only while you're logged on.** That's how it reaches the service-account token without Task Scheduler storing your Windows password. While you're logged off, the dead-man timer is the guard.
- **Hidden** (`conhost --headless`), so no window appears.
- **One copy at a time,** stopped after 10 minutes. It runs on battery, and catches up after sleep.
- **Quiet unless something happens:** it shows a **Windows notification** only if it deleted something or failed. They appear under "Windows PowerShell".

**Managing it** (in PowerShell):

```powershell
$t = 'C:\Users\cuzel\src\gpu-ops\bin\gpuops-reaper-task.ps1'
powershell -ExecutionPolicy Bypass -File $t status      # last run, result (0 = ok), next run
powershell -ExecutionPolicy Bypass -File $t show        # exactly what install registers
powershell -ExecutionPolicy Bypass -File $t uninstall   # remove it
powershell -ExecutionPolicy Bypass -File $t install     # (re)install
```

**Doing a pass by hand:** `bin\gpuops-reaper.ps1`. Add `-DryRun` to only report, or `-TestToast` to check that notifications reach you.

**Log:** `C:\Users\cuzel\AppData\Local\gpu-ops\reaper.log`, one line per pass, last 2000 kept.

> **If you read the log from a Claude Code desktop session, read it through WSL:**
> `cat /mnt/c/Users/cuzel/AppData/Local/gpu-ops/reaper.log`
>
> The desktop app runs as a packaged (MSIX) app. When a process inside it writes under `AppData\Local`, Windows redirects the write to a private copy (`AppData\Local\Packages\Claude_…\LocalCache\Local\…`). Once that copy exists, the session sees it instead of the real file. File Explorer, a normal terminal and WSL all see the real file.

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
- **A dead-man timer** on the machine deletes it at the deadline even if your PC is off. Verified on RunPod in the 2026-11-12 kill test.
- **The final backstop** is a small prepaid balance with auto-pay off.
- Details: [docs/design.md](docs/design.md) §4, and [docs/runbook.md](docs/runbook.md) for what to do when something is still billing.

## Docs

| Doc | What it covers |
|---|---|
| [AGENTS.md](AGENTS.md) | The contract for coding agents: commands, JSON, exit codes, rules |
| [docs/job-spec.md](docs/job-spec.md) | Every spec field, with examples |
| [docs/design.md](docs/design.md) | How it works, and why |
| [docs/runbook.md](docs/runbook.md) | Finding and killing anything still billing |
| [docs/unattended.md](docs/unattended.md) | No 1Password prompts: service account + Windows Credential Manager |
| [docs/glossary.md](docs/glossary.md) | One-line definitions |
| [docs/research/2026-11-provider-comparison.md](docs/research/2026-11-provider-comparison.md) | Provider research and prices |
| [docs/claude-settings-snippet.json](docs/claude-settings-snippet.json) | Permission rules for a calling project: free commands allowed, `run` asks you |

## Development

```bash
uv sync && uv run pytest
```

Tests run in WSL. They use the fake provider (`GPUOPS_ENABLE_FAKE=1`) and spend nothing.
