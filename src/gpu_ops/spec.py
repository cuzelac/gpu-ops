"""Job spec: the TOML file a project writes to ask for a GPU job.

Validated with pydantic so `gpu-ops schema` can print a JSON Schema for agents.
Every model forbids unknown fields: a typo is an error, not a silently ignored setting.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ExitCode, GpuOpsError
from .paths import to_local_path

_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ImageSpec(_Strict):
    ref: str = Field(description="Container image pinned by digest, e.g. docker.io/library/ubuntu@sha256:<64 hex>. "
                                 "Use `gpu-ops pin-image <ref>` to get one.")

    @field_validator("ref")
    @classmethod
    def _pinned(cls, v: str) -> str:
        if not _DIGEST.search(v):
            raise ValueError("image must be pinned by digest (…@sha256:<64 hex>); run `gpu-ops pin-image <ref>`")
        return v


class RunSpec(_Strict):
    setup: list[str] = Field(default_factory=list, description="Shell lines run before `command` (e.g. pip install). "
                                                               "Counted against the time cap; timed separately as overhead.")
    command: str = Field(description="Shell line that does the work. Runs with bash, `set -euo pipefail`, "
                                     "in $GPUOPS_WORK, with $GPUOPS_IN / $GPUOPS_OUT / $GPUOPS_RUN_ID set.")
    ports: list[str] = Field(default_factory=list, description="Reserved for serving jobs (not in v1). Must be empty.")
    disk_gb: int = Field(default=50, ge=10, le=2000, description="Container disk in GB (wiped at teardown).")

    @field_validator("ports")
    @classmethod
    def _no_ports_yet(cls, v: list[str]) -> list[str]:
        if v:
            raise ValueError("exposed ports (serving jobs) are not supported in v1")
        return v


class InputSpec(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$", description="Uploaded to $GPUOPS_IN/<name>/")
    from_: str = Field(alias="from", description="Local file or directory. Relative to the spec file; C:/ paths allowed.")
    include: list[str] = Field(default_factory=list, description="Optional: only these files (relative to `from`).")


class OutputSpec(_Strict):
    path: str = Field(description="File or directory under $GPUOPS_OUT, brought back to runs/<run-id>/out/<path>.")
    required: bool = Field(default=True, description="Run fails (exit 6) if a required output is missing.")

    @field_validator("path")
    @classmethod
    def _relative(cls, v: str) -> str:
        p = Path(v)
        if p.is_absolute() or ".." in p.parts or v.strip() in ("", "."):
            raise ValueError("output path must be relative to $GPUOPS_OUT and must not contain '..'")
        return v.strip("/")


class ReportSpec(_Strict):
    """Small output files whose contents come back inside the run result."""
    files: list[str] = Field(default_factory=list,
                             description="Paths under $GPUOPS_OUT, each also listed in [[outputs]]. Their contents "
                                         "are included in the run result: a .json file as an object, anything else "
                                         "as text.")
    max_bytes: int = Field(default=16384, ge=1, le=65536,
                           description="Per file. A larger file is truncated, with \"truncated\": true.")

    @field_validator("files")
    @classmethod
    def _relative(cls, v: list[str]) -> list[str]:
        for item in v:
            p = Path(item)
            if p.is_absolute() or ".." in p.parts or item.strip() in ("", "."):
                raise ValueError("report file must be a relative path under $GPUOPS_OUT, without '..'")
        return [item.strip("/") for item in v]


class GpuSpec(_Strict):
    min_vram_gb: int = Field(ge=1, description="Minimum VRAM per GPU. A floor, not a target.")
    count: int = Field(default=1, ge=1, le=8)
    tier: Literal["any", "secure", "community"] = "any"
    select: Literal["cheapest-hour", "cheapest-job", "fastest"] = Field(
        default="cheapest-hour",
        description="cheapest-job / fastest need per-GPU timing records; until they exist gpu-ops falls back to "
                    "cheapest-hour and says so.")
    allow: list[str] = Field(default_factory=list, description="Optional: only these GPU ids.")
    prefer: list[str] = Field(default_factory=list, description="Optional: chosen if within 10% of the cheapest.")
    max_price_per_hr: float = Field(default=0, ge=0, description="0 = no ceiling beyond the cost cap.")
    providers: list[str] = Field(default_factory=list, description="Optional: restrict to these providers.")


class LimitsSpec(_Strict):
    max_minutes: float = Field(gt=0, description="Wall-clock cap for the whole run, provisioning to teardown.")
    max_cost_usd: float = Field(gt=0, description="Cost cap for the whole run.")


class JobSpec(_Strict):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$", description="Short id; part of the instance name.")
    project: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$", description="For spend reports.")
    kind: Literal["fine-tune", "batch-inference", "serve", "other"] = "other"
    image: ImageSpec
    run: RunSpec
    env: dict[str, str] = Field(default_factory=dict, description="Plain values only, never secrets. "
                                                                  "$GPUOPS_IN etc. are expanded.")
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    report: ReportSpec | None = Field(default=None, description="Optional: output files to include in the run result, "
                                                                 "so the caller sees the job's own metrics.")
    gpu: GpuSpec
    limits: LimitsSpec
    secrets: dict[str, str] = Field(default_factory=dict, description="NAME = \"op://vault/item/field\". Read at run "
                                                                      "time; never written to disk, logs or the provider.")

    @field_validator("env", "secrets")
    @classmethod
    def _env_names(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(f"invalid environment variable name: {k!r}")
            if k.startswith("GPUOPS_"):
                raise ValueError(f"{k}: names starting with GPUOPS_ are reserved")
        return v

    @field_validator("secrets")
    @classmethod
    def _op_refs(cls, v: dict[str, str]) -> dict[str, str]:
        for k, ref in v.items():
            if not ref.startswith("op://"):
                raise ValueError(f"secret {k} must be a 1Password reference (op://…), never a literal value")
        return v

    @model_validator(mode="after")
    def _cross(self) -> "JobSpec":
        clash = set(self.env) & set(self.secrets)
        if clash:
            raise ValueError(f"names in both env and secrets: {sorted(clash)}")
        names = [i.name for i in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("input names must be unique")
        if self.report:
            outs = [o.path for o in self.outputs]
            for f in self.report.files:
                if not any(f == o or f.startswith(o + "/") for o in outs):
                    raise ValueError(f"report.files entry {f!r} must also be listed in [[outputs]] "
                                     f"(directly, or inside one of {outs})")
        return self


class LoadedSpec(BaseModel):
    """A validated spec plus where it came from and its inputs resolved to local paths."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    spec: JobSpec
    path: Path
    input_paths: dict[str, Path]


def load_spec(path: str | Path, *, check_inputs: bool = True) -> LoadedSpec:
    path = Path(path).resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"job spec not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"{path}: invalid TOML: {e}")
    try:
        spec = JobSpec.model_validate(raw)
    except ValidationError as e:
        lines = [f"  {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"{path}: invalid job spec:\n" + "\n".join(lines))

    input_paths: dict[str, Path] = {}
    for inp in spec.inputs:
        p = to_local_path(inp.from_, path.parent)
        if check_inputs:
            if not p.exists():
                raise GpuOpsError(ExitCode.SPEC_INVALID, f"input {inp.name!r}: {p} does not exist")
            for rel in inp.include:
                if not (p / rel).exists():
                    raise GpuOpsError(ExitCode.SPEC_INVALID, f"input {inp.name!r}: {p / rel} does not exist")
        input_paths[inp.name] = p
    return LoadedSpec(spec=spec, path=path, input_paths=input_paths)


def json_schema() -> dict:
    return JobSpec.model_json_schema(by_alias=True)
