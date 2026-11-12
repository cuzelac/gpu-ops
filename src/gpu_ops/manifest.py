"""SHA-256 manifests: proof that what arrived is what was sent.

A manifest is {relative_path: sha256_hex}, paths with forward slashes.
The remote side produces the same format with `sha256sum` (see REMOTE_MANIFEST_SCRIPT).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

Manifest = dict[str, str]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: Path, rels: list[str] | None = None) -> Manifest:
    """Hash every file under root (or only under the given relative files/dirs)."""
    root = Path(root)
    targets = [root / r for r in rels] if rels else [root]
    out: Manifest = {}
    for t in targets:
        if t.is_file():
            out[t.relative_to(root).as_posix() if t != root else t.name] = sha256_file(t)
        elif t.is_dir():
            for f in sorted(p for p in t.rglob("*") if p.is_file()):
                out[f.relative_to(root).as_posix()] = sha256_file(f)
    return out


def manifest_digest(m: Manifest) -> str:
    """One hash for a whole manifest (used as the run record's inputs hash)."""
    h = hashlib.sha256()
    for k in sorted(m):
        h.update(f"{m[k]}  {k}\n".encode())
    return h.hexdigest()


def parse_sha256sum(text: str) -> tuple[Manifest, list[str]]:
    """Parse remote output: '<hash>  ./path' lines and 'MISSING <path>' lines."""
    m: Manifest = {}
    missing: list[str] = []
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line:
            continue
        if line.startswith("MISSING "):
            missing.append(line[len("MISSING "):])
            continue
        digest, _, path = line.partition("  ")
        if len(digest) == 64 and path:
            m[path[2:] if path.startswith("./") else path] = digest
    return m, missing


def compare(expected: Manifest, actual: Manifest) -> list[str]:
    """Human-readable problems; empty list means identical."""
    problems = []
    for k, v in expected.items():
        if k not in actual:
            problems.append(f"missing locally: {k}")
        elif actual[k] != v:
            problems.append(f"checksum mismatch: {k}")
    for k in actual.keys() - expected.keys():
        problems.append(f"unexpected file: {k}")
    return problems


def remote_manifest_script(out_root: str, rels: list[str]) -> str:
    """Bash that prints a sha256sum manifest of the listed outputs, relative to out_root."""
    lines = ["set -u", f"cd {_q(out_root)} || exit 3"]
    for r in rels:
        qr = _q(r)
        lines.append(
            f"if [ -f {qr} ]; then sha256sum ./{qr}; "
            f"elif [ -d {qr} ]; then find ./{qr} -type f -print0 | sort -z | xargs -0 -r sha256sum; "
            f"else echo MISSING {qr}; fi")
    return "\n".join(lines) + "\n"


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)
