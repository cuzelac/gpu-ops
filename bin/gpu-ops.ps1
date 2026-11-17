# Windows-side wrapper (PowerShell): runs gpu-ops inside WSL, in the current directory.
# Inside WSL it calls the real tool by full path (see bin/gpu-ops for why).
# Install the WSL side once:  uv tool install --editable /mnt/c/Users/cuzel/src/gpu-ops
#
# Quoting: Windows PowerShell 5.1 does not escape double quotes inside an argument it passes to a
# native program, so they are written as \" here; wsl.exe's command-line parser turns them back into ".
$script = 't=$HOME/.local/bin/gpu-ops; [ -x \"$t\" ] || { echo \"gpu-ops is not installed inside WSL (expected $t). Run: uv tool install --editable /mnt/c/Users/cuzel/src/gpu-ops\" >&2; exit 127; }; exec \"$t\" \"$@\"'
& wsl.exe -e bash -lc $script gpu-ops @args
exit $LASTEXITCODE
