"""Tiny smoke-test script: proves code + data went up, ran on the GPU machine, and results came back."""

import hashlib
import json
import os
import platform
import subprocess
import time

out_dir = os.environ["GPUOPS_OUT"]
in_dir = os.environ["GPUOPS_IN"]

smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                     capture_output=True, text=True)
with open(os.path.join(in_dir, "code", "hello.py"), "rb") as f:
    self_hash = hashlib.sha256(f.read()).hexdigest()

result = {
    "run_id": os.environ.get("GPUOPS_RUN_ID"),
    "host": platform.node(),
    "python": platform.python_version(),
    "gpu": smi.stdout.strip() or f"nvidia-smi failed: {smi.stderr.strip()}",
    "uploaded_script_sha256": self_hash,
    "finished_at_unix": time.time(),
}
with open(os.path.join(out_dir, "hello.json"), "w") as f:
    json.dump(result, f, indent=2)
print(json.dumps(result, indent=2))
