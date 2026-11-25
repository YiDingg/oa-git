"""Git-like project root discovery, v2 configuration, and atomic dirty index.

This module implements Section 1 of the oa-git v0.2 specification:
- Hierarchical project root discovery (.oagit/config.json with v1 .oagit.json fallback)
- Normalized project configuration read/write (schema oa-git-config/2, 0700 .oagit/)
- File-locked (fcntl.flock), atomically written dirty index (.oagit/index.json, oa-git-index/1)
- Fast source-file fingerprinting and fail-closed cellview state classification
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator

from .model import (
    CONFIG_SCHEMA_V1,
    CONFIG_SCHEMA_V2,
    INDEX_SCHEMA_V1,
    SCHEMA_VERSION_V2,
    SPOOL_PROTOCOL_V1,
    CdfCouplingRule,
    CellViewRef,
    Manifest,
    SchemaValidationError,
    SnapshotIntegrityError,
)
from .snapshot import (
    resolve_cdf_callback_order,
    snapshot_dir_for,
    verify_snapshot_integrity,
)
from .view_contract import (
    get_view_handler,
    has_view_handler,
    infer_view_type,
    read_manifest_file,
    verify_manifest_v2_integrity,
)

OAGIT_DIR_NAME = ".oagit"
CONFIG_V2_FILENAME = "config.json"
CONFIG_V1_FILENAME = ".oagit.json"
INDEX_FILENAME = "index.json"
INDEX_LOCK_FILENAME = "index.lock"

DEFAULT_SNAPSHOTS_DIR = "snapshots"
DEFAULT_BACKEND = "virtuoso"
DEFAULT_VIRTUOSO_CMD = "virtuoso"
DEFAULT_REMOTE_WORKDIR = "/tmp/oagit"
DEFAULT_TIMEOUT_S = 180

DEFAULT_DAEMON_CONFIG: dict[str, Any] = {
    "enabled": True,
    "idle_timeout_s": 3600,
    "startup_timeout_s": 30,
    "poll_ms": 100,
    "protocol": SPOOL_PROTOCOL_V1,
}

DEFAULT_CDF_CONFIG: dict[str, Any] = {
    "max_passes": 3,
    "coupling_rules": {},
}

VALID_BACKENDS = frozenset({"virtuoso", "fixture"})
VALID_CELLVIEW_STATES = frozenset({"clean", "dirty", "missing", "unknown"})

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "token",
        "api_token",
        "password",
        "passwd",
        "secret",
        "private_key",
        "ssh_private_key",
    }
)
_ALLOWED_CONFIG_V1_KEYS = frozenset(
    {
        "schema",
        "snapshots_dir",
        "cds_lib",
        "allow_libraries",
        "backend",
        "virtuoso_cmd",
        "ssh_host",
        "remote_workdir",
        "timeout_s",
    }
)
_ALLOWED_CONFIG_V2_KEYS = _ALLOWED_CONFIG_V1_KEYS | frozenset(
    {"oa_libs", "daemon", "cdf"}
)
_ALLOWED_DAEMON_KEYS = frozenset(
    {"enabled", "idle_timeout_s", "startup_timeout_s", "poll_ms", "protocol"}
)
_ALLOWED_CDF_KEYS = frozenset({"max_passes", "coupling_rules"})


# ---------------------------------------------------------------------------
# Internal filesystem & formatting helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def ensure_oagit_dir(root: Path) -> Path:
    """Create ``<root>/.oagit`` with strict 0700 permissions."""

    root_path = Path(root).expanduser().resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    oagit_dir = root_path / OAGIT_DIR_NAME
    oagit_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(oagit_dir, 0o700)
    return oagit_dir


def _atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    """Atomically write UTF-8 text via temp file + flush + fsync + os.replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"unable to read {label} ({path}): {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaValidationError(f"{label} must contain a JSON object")
    return data


def _sha256_file(path: Path, chunk_size: int = 65536) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _validate_rel_candidate(rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path or "\x00" in rel_path:
        raise SchemaValidationError(f"invalid source file candidate path: {rel_path!r}")
    candidate = Path(rel_path)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in rel_path:
        raise SchemaValidationError(
            f"source file candidate must be a safe relative POSIX path: {rel_path!r}"
        )
    return rel_path


def _unique_strings(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _relativize_local_path(root: Path, value: str | Path) -> str:
    raw = str(value)
    p = Path(value)
    if not p.is_absolute():
        return raw
    try:
        resolved_root = root.expanduser().resolve()
        resolved_val = p.expanduser().resolve()
        rel = resolved_val.relative_to(resolved_root)
        return rel.as_posix() or "."
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Root discovery
# ---------------------------------------------------------------------------


def discover_project_root(
    start: Path | None = None,
    *,
    explicit_root: Path | None = None,
) -> tuple[Path, Path, str]:
    """Discover the oa-git project root, config path, and config schema version.

    Search order:
    1. If ``explicit_root`` is provided, inspect ONLY that directory for
       ``.oagit/config.json`` (v2) then ``.oagit.json`` (v1).
    2. Walk upward from ``start`` (or cwd) for ``.oagit/config.json`` (v2).
    3. Fallback: walk upward from ``start`` (or cwd) for legacy ``.oagit.json`` (v1)
       without modifying or deleting it.
    4. Fail closed with ``SchemaValidationError`` suggesting ``oagit init``.
    """

    if explicit_root is not None:
        root = Path(explicit_root).expanduser().resolve()
        v2_path = root / OAGIT_DIR_NAME / CONFIG_V2_FILENAME
        if v2_path.is_file():
            return root, v2_path, CONFIG_SCHEMA_V2
        v1_path = root / CONFIG_V1_FILENAME
        if v1_path.is_file():
            return root, v1_path, CONFIG_SCHEMA_V1
        raise SchemaValidationError(
            f"No oa-git configuration found in {root}; run 'oagit init' to initialize this project."
        )

    origin = (Path.cwd() if start is None else Path(start)).expanduser().resolve()
    if origin.is_file():
        origin = origin.parent

    ancestors = (origin, *origin.parents)
    for candidate in ancestors:
        v2_path = candidate / OAGIT_DIR_NAME / CONFIG_V2_FILENAME
        if v2_path.is_file():
            return candidate, v2_path, CONFIG_SCHEMA_V2

    for candidate in ancestors:
        v1_path = candidate / CONFIG_V1_FILENAME
        if v1_path.is_file():
            return candidate, v1_path, CONFIG_SCHEMA_V1

    raise SchemaValidationError(
        f"No oa-git configuration (.oagit/config.json or .oagit.json) found above {origin}; "
        "run 'oagit init' to create one."
    )


# ---------------------------------------------------------------------------
# Configuration normalization, read, and write
# ---------------------------------------------------------------------------


def _normalize_daemon_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        return dict(DEFAULT_DAEMON_CONFIG)
    if not isinstance(raw, Mapping):
        raise SchemaValidationError("config.daemon must be a mapping")
    unknown = set(raw) - _ALLOWED_DAEMON_KEYS
    if unknown:
        raise SchemaValidationError(f"config.daemon contains unsupported keys: {sorted(unknown)!r}")
    enabled = raw.get("enabled", DEFAULT_DAEMON_CONFIG["enabled"])
    if type(enabled) is not bool:
        raise SchemaValidationError("config.daemon.enabled must be a bool")
    idle_timeout_s = raw.get("idle_timeout_s", DEFAULT_DAEMON_CONFIG["idle_timeout_s"])
    if type(idle_timeout_s) is not int or idle_timeout_s < 0:
        raise SchemaValidationError("config.daemon.idle_timeout_s must be an integer >= 0")
    startup_timeout_s = raw.get("startup_timeout_s", DEFAULT_DAEMON_CONFIG["startup_timeout_s"])
    if type(startup_timeout_s) is not int or startup_timeout_s <= 0:
        raise SchemaValidationError("config.daemon.startup_timeout_s must be a positive integer")
    poll_ms = raw.get("poll_ms", DEFAULT_DAEMON_CONFIG["poll_ms"])
    if type(poll_ms) is not int or poll_ms <= 0:
        raise SchemaValidationError("config.daemon.poll_ms must be a positive integer")
    protocol = raw.get("protocol", DEFAULT_DAEMON_CONFIG["protocol"])
    if not isinstance(protocol, str) or protocol != SPOOL_PROTOCOL_V1:
        raise SchemaValidationError(
            f"config.daemon.protocol must be {SPOOL_PROTOCOL_V1!r}, got {protocol!r}"
        )
    return {
        "enabled": enabled,
        "idle_timeout_s": idle_timeout_s,
        "startup_timeout_s": startup_timeout_s,
        "poll_ms": poll_ms,
        "protocol": protocol,
    }


def _normalize_cdf_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {"max_passes": DEFAULT_CDF_CONFIG["max_passes"], "coupling_rules": {}}
    if not isinstance(raw, Mapping):
        raise SchemaValidationError("config.cdf must be a mapping")
    unknown = set(raw) - _ALLOWED_CDF_KEYS
    if unknown:
        raise SchemaValidationError(f"config.cdf contains unsupported keys: {sorted(unknown)!r}")
    max_passes = raw.get("max_passes", DEFAULT_CDF_CONFIG["max_passes"])
    if type(max_passes) is not int or max_passes <= 0:
        raise SchemaValidationError("config.cdf.max_passes must be a positive integer")
    raw_rules = raw.get("coupling_rules", {})
    if not isinstance(raw_rules, Mapping):
        raise SchemaValidationError("config.cdf.coupling_rules must be a mapping")
    coupling_rules: dict[str, list[dict[str, Any]]] = {}
    for cell_key, groups in sorted(raw_rules.items()):
        if not isinstance(cell_key, str) or not cell_key.strip():
            raise SchemaValidationError(f"invalid coupling_rules key: {cell_key!r}")
        if isinstance(groups, (str, bytes, bytearray)) or not isinstance(groups, Sequence):
            raise SchemaValidationError(f"config.cdf.coupling_rules[{cell_key!r}] must be a list")
        parsed_rules: list[CdfCouplingRule] = []
        for idx, group in enumerate(groups):
            if isinstance(group, CdfCouplingRule):
                parsed_rules.append(group)
            elif isinstance(group, Mapping):
                parsed_rules.append(CdfCouplingRule.from_dict(group))
            else:
                raise SchemaValidationError(
                    f"config.cdf.coupling_rules[{cell_key!r}][{idx}] must be a mapping"
                )
        resolve_cdf_callback_order((), coupling_rules=tuple(parsed_rules))
        coupling_rules[cell_key] = [rule.to_dict() for rule in parsed_rules]
    return {
        "max_passes": max_passes,
        "coupling_rules": coupling_rules,
    }


def _normalize_config_mapping(
    raw: Mapping[str, Any],
    *,
    root: Path | None = None,
    target_schema: str | None = None,
    relativize: bool = False,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise SchemaValidationError("configuration payload must be a mapping")

    for forbidden in _FORBIDDEN_CONFIG_KEYS:
        if forbidden in raw:
            raise SchemaValidationError(
                f"configuration must not store sensitive credential field {forbidden!r}"
            )

    declared_schema = raw.get("schema")
    if target_schema is not None:
        effective_schema = target_schema
    elif declared_schema in (CONFIG_SCHEMA_V1, CONFIG_SCHEMA_V2):
        effective_schema = str(declared_schema)
    elif declared_schema is None:
        effective_schema = CONFIG_SCHEMA_V2
    else:
        raise SchemaValidationError(
            f"unsupported oa-git config schema {declared_schema!r}; "
            f"expected {CONFIG_SCHEMA_V1!r} or {CONFIG_SCHEMA_V2!r}"
        )

    allowed_keys = (
        _ALLOWED_CONFIG_V1_KEYS
        if declared_schema == CONFIG_SCHEMA_V1 and target_schema is None
        else _ALLOWED_CONFIG_V2_KEYS
    )
    unknown_keys = set(raw) - allowed_keys
    if unknown_keys:
        raise SchemaValidationError(
            f"configuration contains unsupported keys: {sorted(unknown_keys)!r}"
        )

    snapshots_raw = raw.get("snapshots_dir", DEFAULT_SNAPSHOTS_DIR)
    if isinstance(snapshots_raw, Path):
        snapshots_dir = str(snapshots_raw)
    elif isinstance(snapshots_raw, str) and snapshots_raw.strip():
        snapshots_dir = snapshots_raw
    else:
        raise SchemaValidationError("config.snapshots_dir must be a non-empty string or Path")
    if relativize and root is not None:
        snapshots_dir = _relativize_local_path(root, snapshots_dir)

    ssh_host_raw = raw.get("ssh_host", None)
    if ssh_host_raw is not None:
        if not isinstance(ssh_host_raw, str) or not ssh_host_raw.strip():
            raise SchemaValidationError("config.ssh_host must be a non-empty string or null")
        ssh_host: str | None = ssh_host_raw
    else:
        ssh_host = None

    cds_lib_raw = raw.get("cds_lib", None)
    if cds_lib_raw is None:
        cds_lib: str | None = None
    elif isinstance(cds_lib_raw, Path):
        cds_lib = str(cds_lib_raw)
    elif isinstance(cds_lib_raw, str) and cds_lib_raw.strip():
        cds_lib = cds_lib_raw
    else:
        raise SchemaValidationError("config.cds_lib must be a non-empty string, Path, or null")
    if cds_lib is not None and relativize and root is not None and ssh_host is None:
        cds_lib = _relativize_local_path(root, cds_lib)

    raw_oa_libs = raw.get("oa_libs", {})
    if raw_oa_libs is None:
        raw_oa_libs = {}
    if not isinstance(raw_oa_libs, Mapping):
        raise SchemaValidationError("config.oa_libs must be a mapping of library -> directory")
    oa_libs: dict[str, str] = {}
    for lib_name, lib_path in sorted(raw_oa_libs.items()):
        if not isinstance(lib_name, str) or not lib_name.strip():
            raise SchemaValidationError(f"invalid library name in config.oa_libs: {lib_name!r}")
        if isinstance(lib_path, Path):
            lib_path_str = str(lib_path)
        elif isinstance(lib_path, str) and lib_path.strip():
            lib_path_str = lib_path
        else:
            raise SchemaValidationError(
                f"invalid path for config.oa_libs[{lib_name!r}]: {lib_path!r}"
            )
        if relativize and root is not None:
            lib_path_str = _relativize_local_path(root, lib_path_str)
        oa_libs[lib_name] = lib_path_str

    raw_allow = raw.get("allow_libraries", [])
    if isinstance(raw_allow, str):
        raw_allow = [raw_allow]
    if isinstance(raw_allow, (bytes, bytearray)) or not isinstance(raw_allow, Sequence):
        raise SchemaValidationError("config.allow_libraries must be a list of strings")
    allow_list: list[str] = []
    for item in raw_allow:
        if not isinstance(item, str) or not item.strip():
            raise SchemaValidationError("config.allow_libraries entries must be non-empty strings")
        allow_list.append(item)
    allow_libraries = _unique_strings(allow_list)

    backend = raw.get("backend", DEFAULT_BACKEND)
    if not isinstance(backend, str) or backend not in VALID_BACKENDS:
        raise SchemaValidationError(
            f"unsupported backend {backend!r}; expected one of {sorted(VALID_BACKENDS)}"
        )

    virtuoso_cmd_raw = raw.get("virtuoso_cmd", DEFAULT_VIRTUOSO_CMD)
    if virtuoso_cmd_raw is None:
        virtuoso_cmd = DEFAULT_VIRTUOSO_CMD
    elif isinstance(virtuoso_cmd_raw, str) and virtuoso_cmd_raw.strip():
        virtuoso_cmd = virtuoso_cmd_raw
    else:
        raise SchemaValidationError("config.virtuoso_cmd must be a non-empty string")

    remote_workdir_raw = raw.get("remote_workdir", DEFAULT_REMOTE_WORKDIR)
    if remote_workdir_raw is None:
        remote_workdir = DEFAULT_REMOTE_WORKDIR
    elif isinstance(remote_workdir_raw, str) and remote_workdir_raw.strip():
        remote_workdir = remote_workdir_raw
    else:
        raise SchemaValidationError("config.remote_workdir must be a non-empty string")

    timeout_s = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if type(timeout_s) is not int or timeout_s <= 0:
        raise SchemaValidationError("config.timeout_s must be a positive integer")

    daemon_cfg = _normalize_daemon_config(raw.get("daemon"))
    cdf_cfg = _normalize_cdf_config(raw.get("cdf"))

    return {
        "schema": effective_schema,
        "snapshots_dir": snapshots_dir,
        "cds_lib": cds_lib,
        "oa_libs": oa_libs,
        "allow_libraries": allow_libraries,
        "backend": backend,
        "virtuoso_cmd": virtuoso_cmd,
        "ssh_host": ssh_host,
        "remote_workdir": remote_workdir,
        "timeout_s": timeout_s,
        "daemon": daemon_cfg,
        "cdf": cdf_cfg,
    }


def write_project_config(
    root: Path,
    config: Mapping[str, Any],
    *,
    force: bool = False,
) -> Path:
    """Write ``<root>/.oagit/config.json`` (schema ``oa-git-config/2``) atomically.

    Creates ``<root>/.oagit`` with 0700 permissions. Refuses to overwrite an
    existing v2 config unless ``force=True``. Never deletes legacy ``.oagit.json``.
    """

    root_path = Path(root).expanduser().resolve()
    config_path = root_path / OAGIT_DIR_NAME / CONFIG_V2_FILENAME
    if config_path.exists() and not force:
        raise SchemaValidationError(
            f"configuration already exists at {config_path}; pass force=True (--force) to overwrite"
        )
    normalized = _normalize_config_mapping(
        config,
        root=root_path,
        target_schema=CONFIG_SCHEMA_V2,
        relativize=True,
    )
    ensure_oagit_dir(root_path)
    _atomic_write_text(config_path, _canonical_json(normalized), mode=0o600)
    return config_path


def load_project_config(root: Path) -> dict[str, Any]:
    """Load and normalize v2 ``.oagit/config.json`` or legacy v1 ``.oagit.json``."""

    root_path = Path(root).expanduser().resolve()
    v2_path = root_path / OAGIT_DIR_NAME / CONFIG_V2_FILENAME
    v1_path = root_path / CONFIG_V1_FILENAME

    if v2_path.is_file():
        raw = _read_json_object(v2_path, ".oagit/config.json")
        if raw.get("schema") != CONFIG_SCHEMA_V2:
            raise SchemaValidationError(
                f"unsupported .oagit/config.json schema {raw.get('schema')!r}; "
                f"expected {CONFIG_SCHEMA_V2!r}"
            )
        return _normalize_config_mapping(raw, root=root_path)

    if v1_path.is_file():
        raw = _read_json_object(v1_path, ".oagit.json")
        if raw.get("schema") != CONFIG_SCHEMA_V1:
            raise SchemaValidationError(
                f"unsupported .oagit.json schema {raw.get('schema')!r}; "
                f"expected {CONFIG_SCHEMA_V1!r}"
            )
        return _normalize_config_mapping(raw, root=root_path)

    raise SchemaValidationError(
        f"No oa-git configuration found under {root_path}; run 'oagit init' to create one."
    )


# ---------------------------------------------------------------------------
# Dirty index dataclasses & file-locked atomic I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFileFingerprint:
    """Stat + SHA-256 fingerprint for one candidate OA source file."""

    path: str
    exists: bool
    size: int
    mtime_ns: int | None
    sha256: str | None

    def __post_init__(self) -> None:
        _validate_rel_candidate(self.path)
        if type(self.exists) is not bool:
            raise SchemaValidationError("SourceFileFingerprint.exists must be a bool")
        if type(self.size) is not int or self.size < 0:
            raise SchemaValidationError("SourceFileFingerprint.size must be a non-negative int")
        if not self.exists:
            if self.size != 0 or self.mtime_ns is not None or self.sha256 is not None:
                raise SchemaValidationError(
                    "Missing SourceFileFingerprint must have size=0, mtime_ns=None, sha256=None"
                )
        else:
            if type(self.mtime_ns) is not int:
                raise SchemaValidationError("Existing SourceFileFingerprint requires int mtime_ns")
            if not isinstance(self.sha256, str) or not _HASH_RE.fullmatch(self.sha256):
                raise SchemaValidationError(
                    "Existing SourceFileFingerprint requires 64-char lowercase hex sha256"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceFileFingerprint:
        if not isinstance(data, Mapping):
            raise SchemaValidationError("SourceFileFingerprint entry must be a mapping")
        return cls(
            path=data.get("path"),  # type: ignore[arg-type]
            exists=data.get("exists"),  # type: ignore[arg-type]
            size=data.get("size"),  # type: ignore[arg-type]
            mtime_ns=data.get("mtime_ns"),
            sha256=data.get("sha256"),
        )


@dataclass(frozen=True)
class IndexEntry:
    """Cached local fingerprint & snapshot binding for one CellView."""

    cellview: CellViewRef
    source_files: tuple[SourceFileFingerprint, ...]
    dependencies: tuple[str, ...] = ()
    snapshot: str = ""
    snapshot_full_sha256: str | None = None
    state: str = "clean"
    hierarchy_complete: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.cellview, CellViewRef):
            raise SchemaValidationError("IndexEntry.cellview must be a CellViewRef")
        if self.state not in VALID_CELLVIEW_STATES:
            raise SchemaValidationError(
                f"Invalid IndexEntry.state {self.state!r}; expected one of {sorted(VALID_CELLVIEW_STATES)}"
            )
        if type(self.hierarchy_complete) is not bool:
            raise SchemaValidationError("IndexEntry.hierarchy_complete must be a bool")
        if self.snapshot_full_sha256 is not None:
            if (
                not isinstance(self.snapshot_full_sha256, str)
                or not _HASH_RE.fullmatch(self.snapshot_full_sha256)
            ):
                raise SchemaValidationError(
                    "IndexEntry.snapshot_full_sha256 must be a 64-char lowercase hex digest or None"
                )
        normalized_deps = tuple(sorted(set(self.dependencies)))
        for dep in normalized_deps:
            CellViewRef.parse(dep)
        object.__setattr__(self, "dependencies", normalized_deps)
        object.__setattr__(self, "source_files", tuple(self.source_files))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cellview": self.cellview.to_dict(),
            "source_files": [fp.to_dict() for fp in self.source_files],
            "dependencies": list(self.dependencies),
            "snapshot": self.snapshot,
            "snapshot_full_sha256": self.snapshot_full_sha256,
            "state": self.state,
            "hierarchy_complete": self.hierarchy_complete,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IndexEntry:
        if not isinstance(data, Mapping):
            raise SchemaValidationError("IndexEntry must be a mapping")
        cv_raw = data.get("cellview")
        if not isinstance(cv_raw, Mapping):
            raise SchemaValidationError("IndexEntry.cellview must be a mapping")
        raw_sources = data.get("source_files", [])
        if isinstance(raw_sources, (str, bytes, bytearray)) or not isinstance(raw_sources, Sequence):
            raise SchemaValidationError("IndexEntry.source_files must be a sequence")
        raw_deps = data.get("dependencies", [])
        if isinstance(raw_deps, (str, bytes, bytearray)) or not isinstance(raw_deps, Sequence):
            raise SchemaValidationError("IndexEntry.dependencies must be a sequence")
        deps_out: list[str] = []
        for idx, dep in enumerate(raw_deps):
            if type(dep) is not str:
                raise SchemaValidationError(f"IndexEntry.dependencies[{idx}] must be a string")
            deps_out.append(dep)
        raw_snapshot = data.get("snapshot", "")
        if type(raw_snapshot) is not str:
            raise SchemaValidationError("IndexEntry.snapshot must be a string")
        raw_state = data.get("state", "clean")
        if type(raw_state) is not str:
            raise SchemaValidationError("IndexEntry.state must be a string")
        raw_hier_complete = data.get("hierarchy_complete", False)
        if type(raw_hier_complete) is not bool:
            raise SchemaValidationError("IndexEntry.hierarchy_complete must be a bool")
        return cls(
            cellview=CellViewRef.from_dict(dict(cv_raw)),
            source_files=tuple(SourceFileFingerprint.from_dict(item) for item in raw_sources),
            dependencies=tuple(deps_out),
            snapshot=raw_snapshot,
            snapshot_full_sha256=data.get("snapshot_full_sha256"),
            state=raw_state,
            hierarchy_complete=raw_hier_complete,
        )


def default_source_file_candidates(
    cellview: CellViewRef, view_type: str | None = None
) -> tuple[str, ...]:
    """Return fixed relative OA source file candidates under the library root."""

    eff = view_type or infer_view_type(cellview.view)
    if eff == "schematic":
        if cellview.view == "schematic":
            if has_view_handler("schematic"):
                handler = get_view_handler("schematic")
                return tuple(handler.source_file_candidates(cellview.cell, cellview.view))
            return (
                f"{cellview.cell}/schematic/sch.oa",
                f"{cellview.cell}/symbol/symbol.oa",
                f"{cellview.cell}/symbol/master.tag",
            )
        return (
            f"{cellview.cell}/{cellview.view}/sch.oa",
            f"{cellview.cell}/{cellview.view}/master.tag",
        )
    if eff == "symbol":
        if has_view_handler("symbol"):
            handler = get_view_handler("symbol")
            return tuple(handler.source_file_candidates(cellview.cell, cellview.view))
        return (
            f"{cellview.cell}/{cellview.view}/symbol.oa",
            f"{cellview.cell}/{cellview.view}/master.tag",
        )
    if eff == "layout":
        if has_view_handler("layout"):
            handler = get_view_handler("layout")
            return tuple(handler.source_file_candidates(cellview.cell, cellview.view))
        return (
            f"{cellview.cell}/{cellview.view}/layout.oa",
            f"{cellview.cell}/{cellview.view}/master.tag",
        )
    return (f"{cellview.cell}/{cellview.view}/master.tag",)


def fingerprint_source_files(
    lib_root: Path,
    candidates: Sequence[str],
    previous: Sequence[SourceFileFingerprint] = (),
) -> tuple[SourceFileFingerprint, ...]:
    """Compute stat + SHA-256 fingerprints for candidate OA files.

    Missing candidate files are recorded with ``exists=False, size=0,
    mtime_ns=None, sha256=None`` so deletions are never misclassified as clean.
    Fast path: if ``exists``, ``size``, and ``mtime_ns`` match ``previous``, the
    prior SHA-256 digest is reused without reading or hashing the file.
    """

    root = Path(lib_root)
    prev_by_path = {fp.path: fp for fp in previous}
    seen_paths: set[str] = set()
    results: list[SourceFileFingerprint] = []

    for raw_rel in candidates:
        rel_path = _validate_rel_candidate(raw_rel)
        if rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)

        full_path = root / rel_path
        if not full_path.exists() or not full_path.is_file():
            results.append(
                SourceFileFingerprint(
                    path=rel_path,
                    exists=False,
                    size=0,
                    mtime_ns=None,
                    sha256=None,
                )
            )
            continue

        st = full_path.stat()
        size = int(st.st_size)
        mtime_ns = int(st.st_mtime_ns)
        prev = prev_by_path.get(rel_path)
        if (
            prev is not None
            and prev.exists
            and prev.size == size
            and prev.mtime_ns == mtime_ns
            and prev.sha256 is not None
        ):
            digest = prev.sha256
        else:
            digest = _sha256_file(full_path)

        results.append(
            SourceFileFingerprint(
                path=rel_path,
                exists=True,
                size=size,
                mtime_ns=mtime_ns,
                sha256=digest,
            )
        )

    return tuple(results)


def _default_verify_snapshot(snapshot_dir: Path) -> Manifest:
    manifest = read_manifest_file(snapshot_dir)
    if manifest.view_type == "schematic":
        return verify_snapshot_integrity(snapshot_dir)
    if manifest.view_type == "layout":
        from .layout_snapshot import verify_layout_snapshot_integrity

        verify_layout_snapshot_integrity(snapshot_dir)
        return manifest
    if manifest.schema == SCHEMA_VERSION_V2:
        return verify_manifest_v2_integrity(snapshot_dir, manifest)
    raise SnapshotIntegrityError(
        f"Unsupported snapshot view_type={manifest.view_type!r} with schema={manifest.schema!r}"
    )


def _peek_snapshot_view_type(snap_dir: Path) -> str | None:
    manifest_path = snap_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        return read_manifest_file(snap_dir).view_type
    except (SnapshotIntegrityError, SchemaValidationError, OSError):
        pass
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(raw, Mapping):
        vt = raw.get("view_type")
        if isinstance(vt, str) and vt in ("schematic", "symbol", "layout"):
            return vt
    return None


def _fingerprints_content_equal(
    current: Sequence[SourceFileFingerprint],
    indexed: Sequence[SourceFileFingerprint],
) -> bool:
    """Compare candidate fingerprints by (path, exists, size, sha256).

    An mtime-only change with identical size and SHA-256 content still matches
    so no unnecessary Virtuoso launch is triggered.
    """

    if len(current) != len(indexed):
        return False
    indexed_by_path = {fp.path: fp for fp in indexed}
    if len(indexed_by_path) != len(current):
        return False
    for cur in current:
        prev = indexed_by_path.get(cur.path)
        if prev is None:
            return False
        if (
            cur.exists != prev.exists
            or cur.size != prev.size
            or cur.sha256 != prev.sha256
        ):
            return False
    return True


def _classify_cellview_state_with_reason(
    cellview: CellViewRef,
    *,
    oa_libs: Mapping[str, str | Path],
    snapshots_dir: Path,
    index_entries: Mapping[str, IndexEntry],
    candidates: Sequence[str] | None = None,
    verify_snapshot_fn: Callable[[Path], Manifest] | None = None,
) -> tuple[str, str, tuple[SourceFileFingerprint, ...]]:
    """Classify a CellView and return ``(state, reason, current_fingerprints)``."""

    lib_root_raw = oa_libs.get(cellview.library)
    if lib_root_raw is None:
        reason = (
            "oa_libs not configured"
            if not oa_libs
            else f"library {cellview.library!r} not in oa_libs"
        )
        return "unknown", reason, ()
    lib_root = Path(lib_root_raw)
    if not lib_root.is_dir():
        return "unknown", f"OA library directory missing ({lib_root})", ()

    snap_dir = snapshot_dir_for(Path(snapshots_dir), cellview)
    snap_exists = snap_dir.is_dir() and (snap_dir / "manifest.json").is_file()

    if candidates is not None:
        cand = tuple(candidates)
    else:
        peeked_view_type = _peek_snapshot_view_type(snap_dir) if snap_exists else None
        cand = default_source_file_candidates(cellview, view_type=peeked_view_type)

    entry = index_entries.get(cellview.key)
    prev_fps = entry.source_files if entry is not None else ()
    current_fps = fingerprint_source_files(lib_root, cand, previous=prev_fps)

    primary_missing = (not current_fps) or (not current_fps[0].exists)
    if primary_missing:
        return ("unknown" if snap_exists else "missing"), "primary OA file missing", current_fps

    if not snap_exists:
        return "dirty", "snapshot missing", current_fps

    if entry is None:
        return "dirty", "not indexed yet", current_fps

    verifier = verify_snapshot_fn or _default_verify_snapshot
    try:
        manifest = verifier(snap_dir)
    except (SnapshotIntegrityError, SchemaValidationError, OSError):
        return "dirty", "snapshot corrupted", current_fps

    if manifest.cellview != cellview or manifest.full_sha256 != entry.snapshot_full_sha256:
        return "dirty", "snapshot modified since last capture", current_fps

    if _fingerprints_content_equal(current_fps, entry.source_files):
        return "clean", "clean", current_fps

    return "dirty", "source modified", current_fps


def classify_cellview_state(
    cellview: CellViewRef,
    *,
    oa_libs: Mapping[str, str | Path],
    snapshots_dir: Path,
    index_entries: Mapping[str, IndexEntry],
    candidates: Sequence[str] | None = None,
    verify_snapshot_fn: Callable[[Path], Manifest] | None = None,
) -> tuple[str, tuple[SourceFileFingerprint, ...]]:
    """Classify a CellView as ``clean``, ``dirty``, ``missing``, or ``unknown``.

    Rules:
    - If the OA library path is unmapped or does not exist on disk -> ``unknown``.
    - If the primary OA source file is missing:
      - ``unknown`` when a snapshot already exists (never guess via Virtuoso).
      - ``missing`` when neither OA source nor snapshot exists.
    - ``clean`` ONLY when:
      1. An index entry exists for the cellview.
      2. All candidate source files match the index in ``exists``, ``size``, and
         ``sha256`` (mtime-only drift with identical sha256 still yields ``clean``).
      3. The snapshot directory exists and passes integrity verification.
      4. The verified manifest ``cellview`` and ``full_sha256`` match the index.
    - Otherwise -> ``dirty``.
    """

    state, _reason, current_fps = _classify_cellview_state_with_reason(
        cellview,
        oa_libs=oa_libs,
        snapshots_dir=snapshots_dir,
        index_entries=index_entries,
        candidates=candidates,
        verify_snapshot_fn=verify_snapshot_fn,
    )
    return state, current_fps


def explain_cellview_state(
    cellview: CellViewRef,
    *,
    oa_libs: Mapping[str, str | Path],
    snapshots_dir: Path,
    index_entries: Mapping[str, IndexEntry],
    candidates: Sequence[str] | None = None,
    verify_snapshot_fn: Callable[[Path], Manifest] | None = None,
) -> tuple[str, str]:
    """Return ``(state, reason)`` explaining why a CellView has its current status."""

    state, reason, _current_fps = _classify_cellview_state_with_reason(
        cellview,
        oa_libs=oa_libs,
        snapshots_dir=snapshots_dir,
        index_entries=index_entries,
        candidates=candidates,
        verify_snapshot_fn=verify_snapshot_fn,
    )
    return state, reason


@contextmanager
def _index_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    oagit_dir = root / OAGIT_DIR_NAME
    if exclusive:
        oagit_dir = ensure_oagit_dir(root)
    lock_path = oagit_dir / INDEX_LOCK_FILENAME
    flags = os.O_CREAT | os.O_RDWR
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_index_unlocked(index_path: Path) -> dict[str, IndexEntry]:
    if not index_path.is_file():
        return {}
    data = _read_json_object(index_path, ".oagit/index.json")
    if data.get("schema") != INDEX_SCHEMA_V1:
        raise SchemaValidationError(
            f"unsupported .oagit/index.json schema {data.get('schema')!r}; "
            f"expected {INDEX_SCHEMA_V1!r}"
        )
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, Mapping):
        raise SchemaValidationError(".oagit/index.json 'entries' must be a mapping")
    entries: dict[str, IndexEntry] = {}
    for key, value in raw_entries.items():
        entry = IndexEntry.from_dict(value)
        if key != entry.cellview.key:
            raise SchemaValidationError(
                f"index entry key {key!r} does not match cellview {entry.cellview.key!r}"
            )
        entries[key] = entry
    return entries


def _write_index_unlocked(index_path: Path, entries: Mapping[str, IndexEntry]) -> None:
    payload = {
        "schema": INDEX_SCHEMA_V1,
        "entries": {key: entries[key].to_dict() for key in sorted(entries)},
    }
    _atomic_write_text(index_path, _canonical_json(payload), mode=0o600)


def load_index(root: Path) -> dict[str, IndexEntry]:
    """Read ``<root>/.oagit/index.json`` under a shared ``fcntl.flock(LOCK_SH)``."""

    root_path = Path(root).expanduser().resolve()
    oagit_dir = root_path / OAGIT_DIR_NAME
    index_path = oagit_dir / INDEX_FILENAME
    if not oagit_dir.is_dir() or not index_path.is_file():
        return {}
    with _index_lock(root_path, exclusive=False):
        return _read_index_unlocked(index_path)


def save_index(root: Path, entries: Mapping[str, IndexEntry]) -> Path:
    """Atomically write ``<root>/.oagit/index.json`` under ``fcntl.flock(LOCK_EX)``."""

    root_path = Path(root).expanduser().resolve()
    oagit_dir = ensure_oagit_dir(root_path)
    index_path = oagit_dir / INDEX_FILENAME
    with _index_lock(root_path, exclusive=True):
        _write_index_unlocked(index_path, entries)
    return index_path


def update_index_entry(root: Path, entry: IndexEntry) -> None:
    """Atomically upsert one ``IndexEntry`` under an exclusive lock."""

    if not isinstance(entry, IndexEntry):
        raise SchemaValidationError("entry must be an IndexEntry")
    root_path = Path(root).expanduser().resolve()
    oagit_dir = ensure_oagit_dir(root_path)
    index_path = oagit_dir / INDEX_FILENAME
    with _index_lock(root_path, exclusive=True):
        entries = _read_index_unlocked(index_path)
        entries[entry.cellview.key] = entry
        _write_index_unlocked(index_path, entries)


def remove_index_entry(root: Path, key: str) -> bool:
    """Atomically remove one entry from ``.oagit/index.json`` under ``LOCK_EX``."""

    root_path = Path(root).expanduser().resolve()
    oagit_dir = root_path / OAGIT_DIR_NAME
    index_path = oagit_dir / INDEX_FILENAME
    if not oagit_dir.is_dir() or not index_path.is_file():
        return False
    with _index_lock(root_path, exclusive=True):
        entries = _read_index_unlocked(index_path)
        if key not in entries:
            return False
        del entries[key]
        _write_index_unlocked(index_path, entries)
        return True


__all__ = [
    "OAGIT_DIR_NAME",
    "CONFIG_V2_FILENAME",
    "CONFIG_V1_FILENAME",
    "INDEX_FILENAME",
    "INDEX_LOCK_FILENAME",
    "DEFAULT_SNAPSHOTS_DIR",
    "DEFAULT_BACKEND",
    "DEFAULT_VIRTUOSO_CMD",
    "DEFAULT_REMOTE_WORKDIR",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_DAEMON_CONFIG",
    "DEFAULT_CDF_CONFIG",
    "VALID_CELLVIEW_STATES",
    "SourceFileFingerprint",
    "IndexEntry",
    "ensure_oagit_dir",
    "discover_project_root",
    "write_project_config",
    "load_project_config",
    "default_source_file_candidates",
    "fingerprint_source_files",
    "classify_cellview_state",
    "explain_cellview_state",
    "load_index",
    "save_index",
    "update_index_entry",
    "remove_index_entry",
]
