# Runbook: is anything still billing, and how do I stop it?

## 1. Ask gpu-ops (it asks the provider, not its own notes)

```bash
gpu-ops ps
```

- **Delete one machine:** `gpu-ops kill <id>`.
- **Delete every gpu-ops machine:** `gpu-ops kill --all`. This only touches names starting with `gpuops-`; your own pods are never touched.
- **Delete only machines past their deadline:** `gpu-ops reap`. Add `--dry-run` to see the list first.

From Windows (Git Bash or PowerShell), use `C:\Users\cuzel\src\gpu-ops\bin\gpu-ops` with the same arguments.

## 2. If gpu-ops itself can't run

For example, 1Password won't unlock or WSL is broken:

1. Open the RunPod console, **Pods** page: https://console.runpod.io/pods
2. Find any pod whose name starts with `gpuops-`. The `d<number>` in the name is its deadline, as Unix time.
3. Use the pod's menu and choose **Terminate**. **Stop** is not enough: a stopped pod still bills for its disk.
4. Check **Billing** in the console. The current spend rate should drop to $0.00/hr for compute.

## 3. After a `TEARDOWN FAILED` (exit 10)

- **gpu-ops tried to delete the machine for 5 minutes and could not confirm it was gone.**
- **Run `gpu-ops kill --all`.** If that also fails, use the console as in step 2.
- **Afterwards, check the run's record.**
  - Its ledger entry stays `live` until `gpu-ops ps` shows nothing.
  - The record is at `~/.local/state/gpu-ops/runs/<run-id>/record.json` (inside WSL).

## 4. The backstop

- **Keep the RunPod balance small and leave auto-pay off.** When the balance reaches $0, RunPod stops every pod, so the worst case is losing that balance.

## 5. If the reaper sent a notification

- **"N machine(s) … deleted":**
  - The run that started them didn't clean up, and neither did its dead-man timer. Find the run in `gpu-ops spend` / `~/.local/state/gpu-ops/runs/` and look at its `record.json` and `job.log`.
  - Then run `gpu-ops ps` to confirm nothing is left.
- **"reaper FAILED (exit 3)":**
  - It couldn't read the RunPod key through the 1Password service account. **Check the system clock first.** A wrong clock (e.g. after a crash) breaks service-account sign-in with `failed to re-initialize service account session`.
  - Then check the token with `bin\gpuops-credential.ps1 test`, and usage with `op service-account ratelimit gpu-ops`. More in [docs/unattended.md](unattended.md).
  - The reaper never falls back to a 1Password password prompt; it only logs and notifies.
  - Until it's fixed, check `gpu-ops ps` yourself.
- **Any other failure:**
  - Run `gpu-ops ps`, and use the console (step 2) if gpu-ops can't reach RunPod.
  - The log says what happened: `C:\Users\cuzel\AppData\Local\gpu-ops\reaper.log`. From a Claude Code desktop session, read it via WSL; see README, "Scheduled reaper".

## Where things are

| What | Where |
|---|---|
| Policy and provider settings (WSL) | `~/.config/gpu-ops/policy.toml`, `~/.config/gpu-ops/providers.toml` |
| Run records, logs, outputs (WSL) | `~/.local/state/gpu-ops/runs/<run-id>/` |
| Ledger (WSL) | `~/.local/state/gpu-ops/ledger.jsonl` |
| Reaper log (Windows) | `C:\Users\cuzel\AppData\Local\gpu-ops\reaper.log` |
| Reaper errors from inside WSL | `~/.local/state/gpu-ops/reaper-stderr.log` |
| Reaper task | Task Scheduler → `gpu-ops` folder → `gpu-ops reaper` |
