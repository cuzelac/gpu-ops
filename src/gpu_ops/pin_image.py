"""Resolve an image tag to its digest by asking the registry (anonymous pull token).

Works for public images on Docker Hub, GHCR and other registries that use the standard
Bearer-token challenge. The digest returned is the multi-arch index digest when there is one,
which is what `docker pull <ref>@sha256:…` expects.
"""

from __future__ import annotations

import re

import httpx

from .errors import ExitCode, GpuOpsError

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def split_ref(ref: str) -> tuple[str, str, str]:
    """-> (registry, repository, tag)."""
    ref = ref.split("@", 1)[0]
    first, _, rest = ref.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", ref
    if registry == "docker.io" and "/" not in path:
        path = "library/" + path
    m = re.match(r"^(.*?)(?::([^:/]+))?$", path)
    assert m
    return registry, m.group(1), m.group(2) or "latest"


def pin(ref: str, client: httpx.Client | None = None) -> str:
    registry, repo, tag = split_ref(ref)
    api = "registry-1.docker.io" if registry == "docker.io" else registry
    url = f"https://{api}/v2/{repo}/manifests/{tag}"
    c = client or httpx.Client(timeout=30, follow_redirects=True)
    r = c.head(url, headers={"Accept": ACCEPT})
    if r.status_code == 401:
        chal = r.headers.get("www-authenticate", "")
        params = dict(re.findall(r'(\w+)="([^"]*)"', chal))
        if "realm" not in params:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"{registry}: unexpected auth challenge: {chal!r}")
        tok = c.get(params["realm"], params={"service": params.get("service", ""),
                                             "scope": params.get("scope", f"repository:{repo}:pull")})
        if tok.status_code != 200:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"{registry}: token request failed: HTTP {tok.status_code}")
        token = tok.json().get("token") or tok.json().get("access_token")
        r = c.head(url, headers={"Accept": ACCEPT, "Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"{ref}: registry answered HTTP {r.status_code} (private, or no such tag?)")
    digest = r.headers.get("docker-content-digest")
    if not digest or not digest.startswith("sha256:"):
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"{ref}: registry did not return a digest")
    return f"{registry}/{repo}@{digest}"
