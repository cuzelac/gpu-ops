# Windows-side wrapper (PowerShell): runs gpu-ops inside WSL, in the current directory.
# The WSL side needs `gpu-ops` on its PATH:  uv tool install --editable /mnt/c/Users/cuzel/src/gpu-ops
& wsl.exe -e bash -lc 'exec gpu-ops "$@"' gpu-ops @args
exit $LASTEXITCODE
