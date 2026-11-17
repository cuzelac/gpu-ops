# gpu-ops — notes for developing this repo

Users of the CLI (including agents running GPU jobs) read AGENTS.md; this file is for changing the code.

## Build & test
- Code lives on the Windows side; run Python/tests inside WSL, with the venv off /mnt/c:
  `MSYS_NO_PATHCONV=1 wsl -e bash -lc 'cd /mnt/c/Users/cuzel/src/gpu-ops && UV_PROJECT_ENVIRONMENT=$HOME/.cache/gpu-ops-venv uv run pytest'`
- From Git Bash always set `MSYS_NO_PATHCONV=1` before `wsl`, or `/mnt/c/...` args get rewritten.
- `gpu-ops` on the WSL PATH is an editable `uv tool` install of this repo; code edits apply immediately.
- Test new behaviour against the fake provider (`GPUOPS_ENABLE_FAKE=1`, `providers/fake.py`) + `LocalRemote`; see `tests/test_runner.py`.

## Rules
- Never run a real `gpu-ops run` (or anything that creates a pod) without the human's explicit OK; `validate`, `plan`, `ps`, `reap --dry-run` are free.
- Never edit `~/.config/gpu-ops/` (policy, providers) without the human's OK.
- Exit codes in `errors.py` are a public contract: never renumber.
- When behaviour changes, update AGENTS.md, the README status table, and docs/design.md.

## Gotchas
- RunPod: use REST v2 only (`https://api.runpod.io/v2/openapi.json`; each op lists its key scope in `x-runpod-scope`). Catalog `maxCount` is a ceiling, not stock — stock needs `include=AVAILABILITY&product=POD`.
- SSH sessions on a pod don't inherit the container env; bootstrap saves `RUNPOD_*` to `/gpuops/.provider_env` for the dead-man timer.
- Claude desktop sessions (MSIX) see a private copy of files they write under `AppData\Local`; read Windows-side files such as `reaper.log` via WSL `/mnt/c/...`.
- To prove a 1Password read used the service account, set `GPUOPS_OP_BIN=/bin/false` so the desktop fallback can't succeed.
