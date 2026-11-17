# Unattended access: no 1Password prompts

## Why

- **The prompts:** by default gpu-ops reads the RunPod key through the 1Password desktop app, which may ask you to approve.
  - Each `gpu-ops` command is a new process, so each can prompt.
  - The scheduled reaper, and anything that runs while you're away, would just wait and then fail with exit 3.
- **The fix:** a **1Password service account**, a non-human 1Password identity that can read one vault without any prompt.
  - Its token lives in **Windows Credential Manager**, encrypted to your Windows login, never in a file.
  - gpu-ops reads the token at run time and passes it to `op` only through that process's environment.

## What it can and can't reach

| | |
|---|---|
| Can read | Only the vault you grant it, which should hold only the RunPod key |
| Can't write | Grant it read-only access |
| Can't reach | Your Private vault, or any other vault. Service accounts can't be given access to Private/Personal vaults at all, which is why the key has to move to its own vault |

If the token leaks, revoke the service account in 1Password; nothing else needs changing.

## Setup (you do this; about 10 minutes)

1. **Create a vault for automation keys.**
   - In 1Password, create a vault named `gpu-ops`.
   - Move the **Runpod API Keys** item into it.
2. **Create the service account.**
   - On 1Password.com go to **Developer → Directory → Service Accounts**, or **Developer → Infrastructure Secrets**.
   - Name it `gpu-ops`.
   - Give it **read** access to the `gpu-ops` vault only.
   - 1Password shows the token **once**. Keep that page open for step 3.
3. **Store the token in Windows Credential Manager.** In PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\Users\cuzel\src\gpu-ops\bin\gpuops-credential.ps1 set
   ```
   Paste the token when asked. The input is hidden, and the script rejects anything that doesn't start with `ops_`.
4. **Point gpu-ops at the moved item.**
   - In WSL, edit `~/.config/gpu-ops/providers.toml`:
     ```toml
     [runpod]
     api_key_ref = "op://gpu-ops/Runpod API Keys/credential"
     ```
   - Or ask Claude to make this one-line change. AGENTS.md forbids agents from editing it on their own.
5. **Check it.** Lock 1Password, then run `gpu-ops ps`. It should answer with no prompt.

## Day to day

| What you want | How |
|---|---|
| Check a token is stored | `bin/gpuops-credential.ps1 test` |
| Remove it | `bin/gpuops-credential.ps1 remove`. gpu-ops goes back to desktop-app prompts |
| Turn it off for one command | `GPUOPS_NO_SERVICE_ACCOUNT=1 gpu-ops …` |

- **If the service account read fails** (revoked token, item moved), gpu-ops prints the reason on stderr and falls back to the desktop app.
- **Rate limits:** 1Password's published limit for individual accounts is 1,000 service-account reads a day ([rate limits](https://developer.1password.com/docs/service-accounts/rate-limits/)). Each gpu-ops command uses one. A reaper every 30 minutes uses about 50 a day.
