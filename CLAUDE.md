# gpu-ops — notes for developing this repo

Users of the CLI (including agents running GPU jobs) read AGENTS.md; this file is for changing the code.

## Where to work
- `C:\Users\cuzel\src\gpu-ops` is the **live install**: the WSL `gpu-ops` command (editable `uv tool` install) and the scheduled reaper run its files directly. Keep it on a clean, pushed `main`; never switch branches there. Code changes go through a worktree; a doc-only one-liner may be committed in place.
- Do all changes in a git worktree, one per branch: `git worktree add ../gpu-ops-wt/<branch> -b <branch> origin/main`; remove it after the PR merges (`git worktree remove`). Add and remove them with **Windows** git: worktrees are registered by absolute path, so one created as `C:/…` is invisible to WSL git ("not a working tree").

## Build & test
- Code lives on the Windows side; run Python/tests inside WSL from your worktree, with a per-branch venv off /mnt/c:
  `MSYS_NO_PATHCONV=1 wsl -e bash -lc 'cd /mnt/c/Users/cuzel/src/gpu-ops-wt/<branch> && UV_PROJECT_ENVIRONMENT=$HOME/.cache/gpu-ops-venv-<branch> uv run pytest'`
- From Git Bash always set `MSYS_NO_PATHCONV=1` before `wsl`, or `/mnt/c/...` args get rewritten.
- To try your branch's CLI without touching the installed one: `uv run gpu-ops …` inside your worktree (same `UV_PROJECT_ENVIRONMENT`).
- Test new behaviour against the fake provider (`GPUOPS_ENABLE_FAKE=1`, `providers/fake.py`) + `LocalRemote`; see `tests/test_runner.py`.
- `teardown()` swallows its own exceptions, so `terminate_failures` never reaches `_release`'s error path; use the fake's `list_raises` to exercise it.

## Rules
- Never run a real `gpu-ops run` (or anything that creates a pod) without the human's explicit OK; `validate`, `plan`, `ps`, `reap --dry-run` are free.
- Never edit `~/.config/gpu-ops/` (policy, providers) without the human's OK.
- Exit codes in `errors.py` are a public contract: never renumber.
- When behaviour changes, update AGENTS.md, the README status table, and docs/design.md.
- Put a new invariant on a type or a validator, never in a convention a caller must remember; see docs/design.md §5.
- Assert the result payload, not just `exit_code`: a stub carries the right code, so an exit-code-only test passes against one.

## Gotchas
- RunPod: use REST v2 only (`https://api.runpod.io/v2/openapi.json`; each op lists its key scope in `x-runpod-scope`). Catalog `maxCount` is a ceiling, not stock — stock needs `include=AVAILABILITY&product=POD`.
- SSH sessions on a pod don't inherit the container env; bootstrap saves `RUNPOD_*` to `/gpuops/.provider_env` for the dead-man timer.
- Claude desktop sessions (MSIX) see a private copy of files they write under `AppData\Local`; read Windows-side files such as `reaper.log` via WSL `/mnt/c/...`.
- To prove a 1Password read used the service account, set `GPUOPS_OP_BIN=/bin/false` so the desktop fallback can't succeed.
- `failed to re-initialize service account session` from `op` = the system clock is wrong (seen after a crash left Windows' clock hours behind; WSL inherits it). Comparing WSL against Windows proves nothing; check against real time and resync NTP.
- Repo files are LF. Writing one with Windows Python (`Path.write_text`) rewrites it CRLF and the diff becomes the whole file; write bytes, or normalize `\r\n` → `\n` afterwards.
- Backgrounding a remote job: in `cd X && nohup setsid cmd > log 2>&1 &` the `&` applies to the whole **AND-list**, so bash forks a subshell that waits for the job while still holding the caller's stdout pipe — `Remote.run` then blocks for the job's whole length and `--detach` silently isn't detached. Put the `cd` on its own line so `&` applies to one simple command. `tests/test_resident.py` asserts the launch returns in under 5 s.
