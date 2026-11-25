"""Explicit command-line interface for :mod:`oa_git`.

The CLI is intentionally a thin adapter around the pure snapshot, diff, merge,
guard, project_state, daemon, batch, and backend modules.  It keeps
configuration discovery, dirty-index updates, and Git revision handling here so
that the domain modules remain usable without a command line process.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, redirect_stderr
import inspect
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

from .batch_backend import (
    DEFAULT_MAX_HIER_DEPTH,
    DEFAULT_SKIP_LIBRARIES,
    filter_dirty_targets,
)
from .bridge import load_backend
from .daemon import VirtuosoDaemonClient
from .diff import diff_schematics, format_diff_text
from .guards import probe_locks
from .layout_backend import FixtureLayoutBackend, VirtuosoLayoutBackend
from .layout_diff import (
    diff_layouts,
    format_layout_diff_stat,
    format_layout_diff_text,
)
from .layout_merge import format_layout_merge_conflicts, merge_layouts
from .layout_model import LayoutManifest, LayoutSnapshot
from .layout_snapshot import (
    read_layout_snapshot,
    verify_layout_snapshot_integrity,
    write_layout_snapshot,
)
from .merge import format_merge_conflicts, merge_schematics
from .model import (
    CONFIG_SCHEMA_V1,
    CONFIG_SCHEMA_V2,
    SCHEMA_VERSION_V2,
    BridgeExecutionError,
    CdfCouplingRule,
    CdfParameterError,
    CellViewRef,
    DisallowedLibraryError,
    LiveLockError,
    Manifest,
    OaGitError,
    SchemaValidationError,
    SchematicSnapshot,
    UnsupportedObjectError,
)
from .project_state import (
    DEFAULT_CDF_CONFIG,
    DEFAULT_DAEMON_CONFIG,
    DEFAULT_VIRTUOSO_CMD,
    IndexEntry,
    classify_cellview_state,
    default_source_file_candidates,
    discover_project_root,
    explain_cellview_state,
    fingerprint_source_files,
    load_index,
    load_project_config,
    update_index_entry,
    write_project_config,
)
from .snapshot import (
    read_snapshot,
    resolve_cdf_callback_order,
    snapshot_dir_for,
    verify_snapshot_integrity,
    write_snapshot,
)
from .symbol_diff import diff_symbols, format_symbol_diff_text
from .symbol_merge import format_symbol_merge_conflicts, merge_symbols
from .symbol_model import SymbolSnapshot
from .symbol_snapshot import read_symbol_snapshot, write_symbol_snapshot
from .view_contract import (
    OaSession,
    get_view_handler,
    read_manifest_file,
    verify_manifest_v2_integrity,
)
from .workspace import (
    WorkspaceDiscovery,
    check_gitignore_health,
    detect_virtuoso_cmd,
    discover_workspace_libraries,
    ensure_git_repo,
    is_inside_git_repo,
    update_managed_gitignore,
)


CONFIG_SCHEMA = CONFIG_SCHEMA_V1
DEFAULT_SNAPSHOTS_DIR = "snapshots"
DEFAULT_BACKEND = "virtuoso"
DEFAULT_TIMEOUT_S = 180


def _json_dump(value: Any) -> str:
    """Render deterministic, human-readable JSON for CLI responses."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _print_json(value: Any) -> None:
    sys.stdout.write(_json_dump(value))


def _flatten_option(values: Sequence[Sequence[str]] | None) -> list[str]:
    flattened: list[str] = []
    for group in values or ():
        flattened.extend(group)
    return flattened


def _root_path(value: Path | str | None) -> Path:
    return Path(value or ".").expanduser().resolve()


def _discover_project_and_config(
    explicit_root: Path | str | None,
    *,
    allow_missing: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Discover project root upward (.oagit/config.json then legacy .oagit.json)."""

    explicit = None if explicit_root is None else Path(explicit_root)
    try:
        root, _cfg_path, _schema = discover_project_root(
            start=Path.cwd(),
            explicit_root=explicit,
        )
    except SchemaValidationError:
        if not allow_missing:
            raise
        return _root_path(explicit_root), {}
    return root, load_project_config(root)


def _diff_root_path(value: Path | str | None) -> tuple[Path, dict[str, Any]]:
    if value is not None:
        explicit = Path(value)
        try:
            root, _cfg_path, _schema = discover_project_root(explicit_root=explicit)
            return root, load_project_config(root)
        except SchemaValidationError:
            return _root_path(explicit), {}
    try:
        root, _cfg_path, _schema = discover_project_root(start=Path.cwd())
        return root, load_project_config(root)
    except SchemaValidationError:
        start = Path.cwd().resolve()
        try:
            return _git_toplevel(start), {}
        except BridgeExecutionError:
            return start, {}


def _resolve_path(root: Path, value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _optional_path(root: Path, value: Path | str | None) -> Path | None:
    return None if value is None else _resolve_path(root, value)


def _config_path(root: Path) -> Path:
    return root / ".oagit.json"


def _setting(args: argparse.Namespace, config: Mapping[str, Any], name: str, default: Any) -> Any:
    """Get a CLI override, then config value, then a command default."""

    value = getattr(args, name, None)
    if value is not None:
        return value
    return config.get(name, default)


def _resolve_oa_libs(root: Path, config: Mapping[str, Any]) -> dict[str, Path]:
    raw_libs = config.get("oa_libs") or {}
    return {
        str(lib): _resolve_path(root, lib_dir)
        for lib, lib_dir in sorted(raw_libs.items())
    }


def _parse_coupling_rules(
    config: Mapping[str, Any],
) -> dict[str, tuple[CdfCouplingRule, ...]]:
    cdf_cfg = config.get("cdf") or {}
    raw_rules = cdf_cfg.get("coupling_rules") or {}
    if not isinstance(raw_rules, Mapping):
        raise SchemaValidationError("config.cdf.coupling_rules must be a mapping")
    parsed: dict[str, tuple[CdfCouplingRule, ...]] = {}
    for master_key, groups in sorted(raw_rules.items()):
        if isinstance(groups, (str, bytes, bytearray)) or not isinstance(groups, Sequence):
            raise SchemaValidationError(
                f"config.cdf.coupling_rules[{master_key!r}] must be a sequence"
            )
        rule_objs: list[CdfCouplingRule] = []
        for idx, group in enumerate(groups):
            if isinstance(group, CdfCouplingRule):
                rule_objs.append(group)
            elif isinstance(group, Mapping):
                rule_objs.append(CdfCouplingRule.from_dict(group))
            else:
                raise SchemaValidationError(
                    f"config.cdf.coupling_rules[{master_key!r}][{idx}] must be a mapping"
                )
        rules_tuple = tuple(rule_objs)
        resolve_cdf_callback_order((), coupling_rules=rules_tuple)
        parsed[str(master_key)] = rules_tuple
    return parsed


def _settings(args: argparse.Namespace, root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    snapshots_value = _setting(args, config, "snapshots_dir", DEFAULT_SNAPSHOTS_DIR)
    backend = _setting(args, config, "backend", DEFAULT_BACKEND)
    timeout_s = _setting(args, config, "timeout_s", DEFAULT_TIMEOUT_S)
    if backend not in {"fixture", "virtuoso"}:
        raise SchemaValidationError(f"unsupported backend {backend!r}")
    if type(timeout_s) is not int or timeout_s <= 0:
        raise SchemaValidationError("timeout_s must be a positive integer")
    fixture_value = getattr(args, "fixture", None)
    ssh_host = _setting(args, config, "ssh_host", None)
    cds_lib_raw = _setting(args, config, "cds_lib", None)
    if cds_lib_raw is None:
        cds_lib: Path | str | None = None
    elif backend == "virtuoso" and ssh_host:
        cds_lib = str(cds_lib_raw)
    else:
        cds_lib = _resolve_path(root, cds_lib_raw)

    daemon_cfg = dict(DEFAULT_DAEMON_CONFIG)
    if isinstance(config.get("daemon"), Mapping):
        daemon_cfg.update(config["daemon"])
    # Legacy v1 .oagit.json had no daemon setting; keep v1 default non-daemon
    # while v2 .oagit/config.json defaults to daemon.enabled=True.
    default_daemon_enabled = (
        bool(daemon_cfg.get("enabled", True))
        if config.get("schema") == CONFIG_SCHEMA_V2
        else False
    )
    one_shot = bool(getattr(args, "one_shot", False))
    no_daemon = bool(getattr(args, "no_daemon", False))
    daemon_enabled = (
        False
        if (backend == "fixture" or one_shot or no_daemon)
        else default_daemon_enabled
    )

    cdf_cfg = dict(DEFAULT_CDF_CONFIG)
    if isinstance(config.get("cdf"), Mapping):
        cdf_cfg.update(config["cdf"])

    snapshots_dir = _resolve_path(root, snapshots_value)
    oa_libs = _resolve_oa_libs(root, config)
    workspace_discovery: WorkspaceDiscovery | None = None
    if ssh_host is None and isinstance(cds_lib, Path) and cds_lib.is_file():
        workspace_discovery = discover_workspace_libraries(
            root,
            cds_lib=cds_lib,
            snapshots_dir=snapshots_dir,
        )
        for lib_name, lib_path_str in workspace_discovery.oa_libs.items():
            if lib_name not in oa_libs:
                oa_libs[lib_name] = _resolve_path(root, lib_path_str)

    return {
        "root": root,
        "snapshots_dir": snapshots_dir,
        "cds_lib": cds_lib,
        "oa_libs": oa_libs,
        "workspace_discovery": workspace_discovery,
        "backend": backend,
        "fixture": _optional_path(root, fixture_value),
        "virtuoso_cmd": _setting(args, config, "virtuoso_cmd", None),
        "ssh_host": ssh_host,
        "remote_workdir": _setting(args, config, "remote_workdir", None),
        "timeout_s": timeout_s,
        "daemon_enabled": daemon_enabled,
        "daemon_idle_timeout_s": int(daemon_cfg.get("idle_timeout_s", 3600)),
        "daemon_startup_timeout_s": int(daemon_cfg.get("startup_timeout_s", 30)),
        "daemon_poll_ms": int(daemon_cfg.get("poll_ms", 100)),
        "max_passes": int(cdf_cfg.get("max_passes", 3)),
        "coupling_rules": _parse_coupling_rules(config),
    }


def _configured_libraries(config: Mapping[str, Any]) -> list[str]:
    values = config.get("allow_libraries", [])
    if isinstance(values, str):
        return [values]
    return list(values or ())


def _unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _parse_cellview(value: str) -> CellViewRef:
    return CellViewRef.parse(value)


def _looks_like_cellview(value: str) -> bool:
    if not isinstance(value, str) or value.count("/") != 2 or "\\" in value:
        return False
    if value.startswith(("/", ".", "~")):
        return False
    try:
        CellViewRef.parse(value)
    except SchemaValidationError:
        return False
    return True


def _resolve_snapshot_input(
    value: str | Path,
    *,
    root: Path,
    snapshots_dir: Path,
    prefer_cwd: bool = False,
) -> Path:
    """Resolve a snapshot directory path or ``<lib>/<cell>/<view>`` shorthand."""

    raw_str = str(value)
    raw = Path(raw_str).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    candidates = [raw, root / raw] if prefer_cwd else [root / raw, raw]
    is_cellview = _looks_like_cellview(raw_str)
    first_existing: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            resolved = candidate.resolve()
            if first_existing is None:
                first_existing = resolved
            if not is_cellview or (candidate.is_dir() and (candidate / "manifest.json").is_file()):
                return resolved
    if is_cellview:
        shorthand = snapshot_dir_for(snapshots_dir, CellViewRef.parse(raw_str)).resolve()
        if shorthand.is_dir() and (shorthand / "manifest.json").is_file():
            return shorthand
        if first_existing is not None:
            return first_existing
        return shorthand
    return (raw if prefer_cwd else (root / raw)).resolve()


def _resolve_existing_cellview_shorthand(value: str, snapshots_dir: Path) -> Path | None:
    """Resolve ``<lib>/<cell>/<view>`` to an existing local snapshot directory, else None."""

    if not _looks_like_cellview(value):
        return None
    candidate = snapshot_dir_for(snapshots_dir, CellViewRef.parse(value))
    if candidate.is_dir() and (candidate / "manifest.json").is_file():
        return candidate.resolve()
    return None


def _parse_library_map(value: str, *, flag: str = "--oa-lib-map") -> tuple[str, Path]:
    library, separator, directory = value.partition("=")
    if not separator or not library.strip() or not directory.strip():
        raise SchemaValidationError(
            f"expected LIB=DIR for {flag}, got {value!r}"
        )
    return library.strip(), Path(directory.strip())


def _backend(settings: Mapping[str, Any], *, inline_readback: bool = False):
    return load_backend(
        settings["backend"],
        fixture_path=settings["fixture"],
        virtuoso_cmd=settings["virtuoso_cmd"],
        ssh_host=settings["ssh_host"],
        remote_workdir=settings["remote_workdir"],
        timeout_s=settings["timeout_s"],
        daemon_enabled=bool(settings.get("daemon_enabled", False)),
        daemon_idle_timeout_s=int(settings.get("daemon_idle_timeout_s", 3600)),
        daemon_startup_timeout_s=int(settings.get("daemon_startup_timeout_s", 30)),
        daemon_poll_ms=int(settings.get("daemon_poll_ms", 100)),
        project_root=settings.get("root"),
        inline_readback=inline_readback,
    )


def _daemon_client(
    root: Path,
    settings: Mapping[str, Any],
    *,
    idle_timeout_s: int | None = None,
) -> VirtuosoDaemonClient:
    if settings["backend"] == "fixture":
        raise SchemaValidationError("daemon is only supported when backend='virtuoso'")
    cds_lib = settings.get("cds_lib")
    if cds_lib is None:
        raise SchemaValidationError(
            "daemon requires cds_lib (configure cds_lib in .oagit/config.json or pass --cds-lib)"
        )
    return VirtuosoDaemonClient(
        project_root=root,
        cds_lib=cds_lib,
        virtuoso_cmd=settings.get("virtuoso_cmd") or "virtuoso",
        ssh_host=settings.get("ssh_host"),
        remote_workdir=settings.get("remote_workdir") or "/tmp/oagit",
        idle_timeout_s=(
            settings["daemon_idle_timeout_s"]
            if idle_timeout_s is None
            else idle_timeout_s
        ),
        startup_timeout_s=settings["daemon_startup_timeout_s"],
        poll_ms=settings["daemon_poll_ms"],
        timeout_s=settings["timeout_s"],
    )


# ---------------------------------------------------------------------------
# Multi-view Snapshot Verification, Detection & I/O Dispatch
# ---------------------------------------------------------------------------

_ALLOWED_LAYOUT_GIT_FILES = frozenset(
    {"objects.jsonl", "definitions.jsonl", "omissions.json"}
)


def _verify_any_snapshot(snapshot_dir: Path) -> Manifest | LayoutManifest:
    manifest = read_manifest_file(snapshot_dir)
    if manifest.view_type == "schematic":
        return verify_snapshot_integrity(snapshot_dir)
    if manifest.view_type == "symbol":
        return read_symbol_snapshot(snapshot_dir, verify=True).manifest
    if manifest.view_type == "layout":
        return verify_layout_snapshot_integrity(snapshot_dir)
    if manifest.schema == SCHEMA_VERSION_V2:
        return verify_manifest_v2_integrity(snapshot_dir, manifest)
    raise SchemaValidationError(
        f"unsupported snapshot view_type={manifest.view_type!r} with schema={manifest.schema!r}"
    )


def _read_any_snapshot(
    snapshot_dir: Path, *, verify: bool = True
) -> SchematicSnapshot | SymbolSnapshot | LayoutSnapshot | Any:
    manifest = read_manifest_file(snapshot_dir)
    if manifest.view_type == "schematic":
        return read_snapshot(snapshot_dir, verify=verify)
    if manifest.view_type == "symbol":
        return read_symbol_snapshot(snapshot_dir, verify=verify)
    if manifest.view_type == "layout":
        return read_layout_snapshot(snapshot_dir, verify=verify)
    return get_view_handler(manifest.view_type).read_snapshot(snapshot_dir, verify=verify)


def _write_any_snapshot(snapshot: Any, dest_dir: Path) -> Path:
    if isinstance(snapshot, SchematicSnapshot):
        return write_snapshot(snapshot, dest_dir)
    if isinstance(snapshot, SymbolSnapshot):
        return write_symbol_snapshot(snapshot, dest_dir)
    if isinstance(snapshot, LayoutSnapshot) or snapshot.manifest.view_type == "layout":
        return write_layout_snapshot(snapshot, dest_dir)
    return get_view_handler(snapshot.manifest.view_type).write_snapshot(snapshot, dest_dir)


def _detect_capture_view_type(
    cv: CellViewRef,
    *,
    snapshots_dir: Path,
    explicit_view_type: str | None = None,
) -> str:
    """Resolve the semantic view type (``schematic``, ``symbol``, or ``layout``) for capture."""

    if explicit_view_type is not None and explicit_view_type != "auto":
        if explicit_view_type not in {"schematic", "symbol", "layout"}:
            raise SchemaValidationError(
                f"unsupported --view-type {explicit_view_type!r}; "
                "expected 'auto', 'schematic', 'symbol', or 'layout'"
            )
        return explicit_view_type
    if cv.view == "layout" or cv.view.startswith("layout"):
        return "layout"
    if cv.view == "symbol" or cv.view.startswith("symbol"):
        return "symbol"
    snap_dir = snapshot_dir_for(snapshots_dir, cv)
    if snap_dir.is_dir() and (snap_dir / "manifest.json").is_file():
        try:
            manifest = read_manifest_file(snap_dir)
            if manifest.view_type in {"schematic", "symbol", "layout"}:
                return manifest.view_type
        except OaGitError:
            pass
    return "schematic"


def _source_file_candidates_for_view(
    cv: CellViewRef,
    *,
    snapshots_dir: Path | None = None,
    view_type: str | None = None,
    index_entry: IndexEntry | None = None,
) -> tuple[str, ...]:
    """Return OA source file candidates respecting ``view_type`` for custom view names."""

    eff_vt = view_type
    if eff_vt is None and index_entry is not None:
        if any(fp.path.endswith("/layout.oa") for fp in index_entry.source_files):
            eff_vt = "layout"
        elif any(fp.path.endswith("/symbol.oa") for fp in index_entry.source_files) and not any(
            fp.path.endswith("/sch.oa") for fp in index_entry.source_files
        ):
            eff_vt = "symbol"
    if eff_vt is None and snapshots_dir is not None:
        eff_vt = _detect_capture_view_type(cv, snapshots_dir=snapshots_dir)

    return default_source_file_candidates(cv, view_type=eff_vt)


def _resolve_layout_backend(backend: Any | None, settings: Mapping[str, Any]) -> Any:
    """Return a layout-capable backend, falling back cleanly for mock/legacy backends."""

    if backend is not None and (
        callable(getattr(backend, "capture_layout", None))
        or callable(getattr(backend, "restore_layout", None))
    ):
        return backend
    if settings["backend"] == "fixture":
        return FixtureLayoutBackend(fixture_path=settings["fixture"])
    skill_runner = (
        backend
        if (backend is not None and callable(getattr(backend, "run_skill_request", None)))
        else None
    )
    return VirtuosoLayoutBackend(
        virtuoso_cmd=settings.get("virtuoso_cmd"),
        ssh_host=settings.get("ssh_host"),
        remote_workdir=settings.get("remote_workdir"),
        timeout_s=settings["timeout_s"],
        skill_runner=skill_runner,
    )


def _call_capture_layout(
    layout_backend: Any,
    cellview: CellViewRef,
    *,
    cds_lib: Path | str | None,
    session: OaSession | None,
) -> LayoutSnapshot:
    fn = layout_backend.capture_layout
    kwargs: dict[str, Any] = {"cds_lib": cds_lib}
    try:
        sig = inspect.signature(fn)
        if "session" in sig.parameters or any(
            p.kind is p.VAR_KEYWORD for p in sig.parameters.values()
        ):
            kwargs["session"] = session
    except (TypeError, ValueError):
        pass
    return fn(cellview, **kwargs)


def _call_restore_layout(
    layout_backend: Any,
    snapshot: LayoutSnapshot,
    target: CellViewRef,
    *,
    cds_lib: Path | str | None,
    allow_libraries: Sequence[str],
    overwrite: bool,
    allow_omissions: bool,
    oa_view_dir: Path | None,
    verify_readback: bool,
    session: OaSession | None,
) -> Any:
    fn = layout_backend.restore_layout
    kwargs: dict[str, Any] = {
        "cds_lib": cds_lib,
        "allow_libraries": allow_libraries,
        "overwrite": overwrite,
        "allow_omissions": allow_omissions,
        "oa_view_dir": oa_view_dir,
        "verify_readback": verify_readback,
    }
    target_positional_only = False
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if "session" in params or any(p.kind is p.VAR_KEYWORD for p in params.values()):
            kwargs["session"] = session
        target_param = params.get("target")
        if target_param is not None and target_param.kind is inspect.Parameter.POSITIONAL_ONLY:
            target_positional_only = True
    except (TypeError, ValueError):
        pass
    if target_positional_only:
        return fn(snapshot, target, **kwargs)
    return fn(snapshot, target=target, **kwargs)


def _relative_posix(root: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(target)


def _schematic_direct_child_keys(
    snapshot: SchematicSnapshot,
    *,
    skip_libraries: Sequence[str] = DEFAULT_SKIP_LIBRARIES,
) -> tuple[str, set[str], set[str]]:
    """Return ``(own_symbol_key, child_schematic_keys, child_symbol_keys)``."""

    skip_set = set(skip_libraries)
    cv = snapshot.manifest.cellview
    own_sym = CellViewRef(cv.library, cv.cell, "symbol").key
    child_schs: set[str] = set()
    child_syms: set[str] = set()
    for inst in snapshot.electrical.instances:
        if inst.lib in skip_set:
            continue
        child_schs.add(CellViewRef(inst.lib, inst.cell, "schematic").key)
        child_syms.add(CellViewRef(inst.lib, inst.cell, "symbol").key)
    return own_sym, child_schs, child_syms


def _extract_schematic_dependencies(
    snapshot: SchematicSnapshot,
    *,
    captured_keys: set[str],
    skip_libraries: Sequence[str] = DEFAULT_SKIP_LIBRARIES,
) -> tuple[str, ...]:
    own_sym, child_schs, child_syms = _schematic_direct_child_keys(
        snapshot, skip_libraries=skip_libraries
    )
    deps: set[str] = set()
    if own_sym in captured_keys:
        deps.add(own_sym)
    for key in child_schs | child_syms:
        if key in captured_keys:
            deps.add(key)
    return tuple(sorted(deps))


def _compute_hier_completeness(
    captured_schematics: Mapping[str, SchematicSnapshot],
    *,
    captured_keys: set[str],
    skipped_keys: set[str],
    failed_keys: set[str],
    discovered_keys: set[str],
    include_symbols: bool,
    batch_has_errors: bool,
    skip_libraries: Sequence[str] = DEFAULT_SKIP_LIBRARIES,
) -> dict[str, bool]:
    """Determine ``hierarchy_complete`` for each captured schematic in a ``--hier`` batch.

    A parent schematic is marked ``hierarchy_complete=True`` ONLY when there are no
    batch-level errors AND all direct and transitive discovered children succeeded
    (none failed or went uncaptured).
    """

    if batch_has_errors:
        return {key: False for key in captured_schematics}

    available_keys = captured_keys | skipped_keys
    direct_edges: dict[str, set[str]] = {}
    directly_incomplete: set[str] = set()

    for key, snap in captured_schematics.items():
        own_sym, child_schs, child_syms = _schematic_direct_child_keys(
            snap, skip_libraries=skip_libraries
        )
        candidate_children: set[str] = set(child_schs)
        if include_symbols:
            candidate_children.add(own_sym)
            candidate_children.update(child_syms)
        else:
            if own_sym in (failed_keys | discovered_keys | available_keys):
                candidate_children.add(own_sym)
            for sym_key in child_syms:
                if sym_key in (failed_keys | discovered_keys | available_keys):
                    candidate_children.add(sym_key)

        edges: set[str] = set()
        for child_key in candidate_children:
            if child_key in failed_keys:
                directly_incomplete.add(key)
            elif child_key in discovered_keys and child_key not in available_keys:
                directly_incomplete.add(key)
            elif child_key in captured_schematics:
                edges.add(child_key)
        direct_edges[key] = edges

    completeness: dict[str, bool] = {}
    for root_key in captured_schematics:
        visited: set[str] = set()
        stack: list[str] = [root_key]
        complete = True
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            if cur in directly_incomplete:
                complete = False
                break
            stack.extend(direct_edges.get(cur, ()))
        completeness[root_key] = complete
    return completeness


def _record_index_after_capture(
    root: Path,
    *,
    config: Mapping[str, Any],
    oa_libs: Mapping[str, Path],
    snapshot: Any,
    destination: Path,
    dependencies: tuple[str, ...] | None = None,
    hierarchy_complete: bool | None = None,
) -> None:
    if not oa_libs and config.get("schema") != CONFIG_SCHEMA_V2 and not (root / ".oagit").is_dir():
        return
    cv: CellViewRef = snapshot.manifest.cellview
    prev_entries = load_index(root)
    prev_entry = prev_entries.get(cv.key)
    lib_root = oa_libs.get(cv.library)
    if lib_root is not None and lib_root.is_dir():
        candidates = _source_file_candidates_for_view(
            cv, view_type=snapshot.manifest.view_type
        )
        fps = fingerprint_source_files(
            lib_root,
            candidates,
            previous=prev_entry.source_files if prev_entry is not None else (),
        )
        state = "clean" if (fps and fps[0].exists) else "unknown"
    else:
        fps = ()
        state = "unknown"

    if dependencies is None:
        effective_deps = prev_entry.dependencies if prev_entry is not None else ()
    else:
        effective_deps = dependencies

    if hierarchy_complete is None:
        if snapshot.manifest.view_type in {"symbol", "layout"} or cv.view == "symbol":
            effective_hier_complete = True
        elif (
            prev_entry is not None
            and prev_entry.snapshot_full_sha256 == snapshot.manifest.full_sha256
        ):
            effective_hier_complete = prev_entry.hierarchy_complete
        else:
            effective_hier_complete = False
    else:
        effective_hier_complete = hierarchy_complete

    entry = IndexEntry(
        cellview=cv,
        source_files=fps,
        dependencies=effective_deps,
        snapshot=_relative_posix(root, destination),
        snapshot_full_sha256=snapshot.manifest.full_sha256,
        state=state,
        hierarchy_complete=effective_hier_complete,
    )
    update_index_entry(root, entry)


# ---------------------------------------------------------------------------
# Subcommand Handlers
# ---------------------------------------------------------------------------


def _collect_repo_local_oa_ignore_paths(
    root: Path,
    oa_libs: Mapping[str, str],
    discovery: WorkspaceDiscovery | None,
    *,
    snapshots_dir: Path,
) -> list[str]:
    """Collect sorted root-relative paths of repo-local OA libraries for .gitignore."""

    resolved_root = root.resolve()
    internal_dirs = (
        snapshots_dir.resolve(),
        (resolved_root / ".git").resolve(),
        (resolved_root / ".oagit").resolve(),
    )
    paths: set[str] = set()
    if discovery is not None:
        paths.update(discovery.repo_local_ignore_paths)
    for raw_dir in oa_libs.values():
        resolved = _resolve_path(resolved_root, raw_dir)
        if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
            continue
        if any(
            resolved == internal or resolved.is_relative_to(internal)
            for internal in internal_dirs
        ):
            continue
        paths.add(resolved.relative_to(resolved_root).as_posix())
    return sorted(paths)


def _init(args: argparse.Namespace) -> int:
    root = _root_path(args.root)
    raw_timeout_s = getattr(args, "timeout_s", None)
    if raw_timeout_s is not None and (type(raw_timeout_s) is not int or raw_timeout_s <= 0):
        raise SchemaValidationError("timeout_s must be a positive integer")
    root.mkdir(parents=True, exist_ok=True)
    cli_allow_libraries = _unique(_flatten_option(args.allow_library))

    oa_lib_pairs = _flatten_option(getattr(args, "oa_lib", None))
    cli_oa_libs: dict[str, str] = {}
    for raw_pair in oa_lib_pairs:
        lib_name, lib_path = _parse_library_map(raw_pair, flag="--oa-lib")
        cli_oa_libs[lib_name] = str(lib_path)

    daemon_flag = getattr(args, "daemon", None)
    daemon_idle = getattr(args, "daemon_idle_timeout", None)
    daemon_startup = getattr(args, "daemon_startup_timeout", None)
    daemon_poll = getattr(args, "daemon_poll_ms", None)
    max_passes = getattr(args, "max_passes", None)
    explicit_v2 = bool(getattr(args, "v2", False))
    explicit_v1 = bool(getattr(args, "v1", False))
    sync_mode = bool(getattr(args, "sync", False))
    no_git = bool(getattr(args, "no_git", False))
    no_auto_allow = bool(getattr(args, "no_auto_allow", False))
    no_auto_discover = bool(getattr(args, "no_auto_discover", False))

    has_v2_only_flags = (
        explicit_v2
        or sync_mode
        or bool(cli_oa_libs)
        or daemon_flag is not None
        or daemon_idle is not None
        or daemon_startup is not None
        or daemon_poll is not None
        or max_passes is not None
    )
    if explicit_v1 and has_v2_only_flags:
        raise SchemaValidationError(
            "--v1 cannot be combined with v2-only flags (--v2, --sync, --oa-lib, --daemon, --max-passes)"
        )

    use_v2 = not explicit_v1
    v2_path = root / ".oagit" / "config.json"
    v1_path = _config_path(root)

    discovery: WorkspaceDiscovery | None = None
    git_initialized = False
    gitignore_path_str: str | None = None
    gitignore_updated = False
    synced_existing = False

    if use_v2 and sync_mode and (v2_path.is_file() or v1_path.is_file()):
        if not v2_path.is_file() and v1_path.is_file():
            raise SchemaValidationError(
                f"legacy v1 configuration found at {v1_path}; "
                "--sync requires .oagit/config.json (pass --force to migrate to v2)"
            )
        existing = load_project_config(root)
        synced_existing = True
        eff_ssh_host = args.ssh_host if args.ssh_host is not None else existing.get("ssh_host")
        eff_snapshots_raw = (
            str(args.snapshots_dir)
            if args.snapshots_dir is not None
            else str(existing.get("snapshots_dir", DEFAULT_SNAPSHOTS_DIR))
        )
        snapshots_dir = _resolve_path(root, eff_snapshots_raw)
        eff_backend = args.backend if args.backend is not None else existing.get("backend", DEFAULT_BACKEND)
        eff_timeout_s = raw_timeout_s if raw_timeout_s is not None else existing.get("timeout_s", DEFAULT_TIMEOUT_S)
        eff_virtuoso_cmd = (
            args.virtuoso_cmd
            if args.virtuoso_cmd is not None
            else existing.get("virtuoso_cmd", DEFAULT_VIRTUOSO_CMD)
        )
        eff_remote_workdir = (
            args.remote_workdir
            if args.remote_workdir is not None
            else existing.get("remote_workdir")
        )

        if args.cds_lib is not None:
            eff_cds_lib: str | None = str(args.cds_lib)
        elif existing.get("cds_lib") is not None:
            eff_cds_lib = str(existing["cds_lib"])
        elif not no_auto_discover and not eff_ssh_host and (root / "cds.lib").is_file():
            eff_cds_lib = "cds.lib"
        else:
            eff_cds_lib = None

        if not no_auto_discover and not eff_ssh_host:
            cds_lib_arg = None if eff_cds_lib is None else _resolve_path(root, eff_cds_lib)
            discovery = discover_workspace_libraries(
                root,
                cds_lib=cds_lib_arg,
                snapshots_dir=snapshots_dir,
            )
            if eff_cds_lib is None and discovery.cds_lib is not None:
                eff_cds_lib = _relative_posix(root, discovery.cds_lib)

        merged_oa_libs: dict[str, str] = {}
        if discovery is not None:
            merged_oa_libs.update(discovery.oa_libs)
        merged_oa_libs.update(existing.get("oa_libs") or {})
        merged_oa_libs.update(cli_oa_libs)

        merged_allow = list(existing.get("allow_libraries") or ())
        if discovery is not None and not no_auto_allow:
            merged_allow.extend(discovery.allow_libraries)
        merged_allow.extend(cli_allow_libraries)
        merged_allow = _unique(merged_allow)

        daemon_cfg = dict(existing.get("daemon") or DEFAULT_DAEMON_CONFIG)
        if daemon_flag is not None:
            daemon_cfg["enabled"] = bool(daemon_flag)
        if daemon_idle is not None:
            daemon_cfg["idle_timeout_s"] = daemon_idle
        if daemon_startup is not None:
            daemon_cfg["startup_timeout_s"] = daemon_startup
        if daemon_poll is not None:
            daemon_cfg["poll_ms"] = daemon_poll

        cdf_cfg = dict(existing.get("cdf") or DEFAULT_CDF_CONFIG)
        if max_passes is not None:
            cdf_cfg["max_passes"] = max_passes

        raw_config: dict[str, Any] = {
            "schema": CONFIG_SCHEMA_V2,
            "snapshots_dir": eff_snapshots_raw,
            "cds_lib": eff_cds_lib,
            "oa_libs": merged_oa_libs,
            "allow_libraries": merged_allow,
            "backend": eff_backend,
            "virtuoso_cmd": eff_virtuoso_cmd,
            "ssh_host": eff_ssh_host,
            "remote_workdir": eff_remote_workdir,
            "timeout_s": eff_timeout_s,
            "daemon": daemon_cfg,
            "cdf": cdf_cfg,
        }
        config_path = write_project_config(root, raw_config, force=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        config = load_project_config(root)

        if not no_git:
            git_initialized, _git_top = ensure_git_repo(root)
            ignore_paths = _collect_repo_local_oa_ignore_paths(
                root,
                config.get("oa_libs") or {},
                discovery,
                snapshots_dir=snapshots_dir,
            )
            gi_path, gitignore_updated = update_managed_gitignore(root, ignore_paths)
            gitignore_path_str = str(gi_path)
    elif use_v2:
        if (v2_path.exists() or v1_path.exists()) and not args.force:
            existing_path = v2_path if v2_path.exists() else v1_path
            raise SchemaValidationError(
                f"configuration already exists: {existing_path}; "
                "pass --sync to refresh cds.lib/.gitignore or --force to overwrite"
            )
        eff_snapshots_raw = (
            DEFAULT_SNAPSHOTS_DIR if args.snapshots_dir is None else str(args.snapshots_dir)
        )
        snapshots_dir = _resolve_path(root, eff_snapshots_raw)
        eff_backend = DEFAULT_BACKEND if args.backend is None else args.backend
        eff_timeout_s = DEFAULT_TIMEOUT_S if raw_timeout_s is None else raw_timeout_s
        eff_ssh_host = args.ssh_host

        if not no_auto_discover and not eff_ssh_host:
            if args.cds_lib is not None:
                eff_cds_lib = str(args.cds_lib)
            elif (root / "cds.lib").is_file():
                eff_cds_lib = "cds.lib"
            else:
                eff_cds_lib = None

            cds_lib_arg = None if eff_cds_lib is None else _resolve_path(root, eff_cds_lib)
            discovery = discover_workspace_libraries(
                root,
                cds_lib=cds_lib_arg,
                snapshots_dir=snapshots_dir,
            )
            oa_libs = dict(discovery.oa_libs)
            oa_libs.update(cli_oa_libs)
            if no_auto_allow:
                allow_libraries = list(cli_allow_libraries)
            else:
                allow_libraries = _unique(list(discovery.allow_libraries) + cli_allow_libraries)

            if args.virtuoso_cmd is not None:
                eff_virtuoso_cmd = args.virtuoso_cmd
            elif eff_backend == "virtuoso":
                eff_virtuoso_cmd = detect_virtuoso_cmd() or DEFAULT_VIRTUOSO_CMD
            else:
                eff_virtuoso_cmd = DEFAULT_VIRTUOSO_CMD
        else:
            eff_cds_lib = None if args.cds_lib is None else str(args.cds_lib)
            oa_libs = dict(cli_oa_libs)
            allow_libraries = list(cli_allow_libraries)
            eff_virtuoso_cmd = (
                args.virtuoso_cmd if args.virtuoso_cmd is not None else DEFAULT_VIRTUOSO_CMD
            )

        daemon_cfg = dict(DEFAULT_DAEMON_CONFIG)
        if daemon_flag is not None:
            daemon_cfg["enabled"] = bool(daemon_flag)
        if daemon_idle is not None:
            daemon_cfg["idle_timeout_s"] = daemon_idle
        if daemon_startup is not None:
            daemon_cfg["startup_timeout_s"] = daemon_startup
        if daemon_poll is not None:
            daemon_cfg["poll_ms"] = daemon_poll

        cdf_cfg = {
            "max_passes": DEFAULT_CDF_CONFIG["max_passes"] if max_passes is None else max_passes,
            "coupling_rules": {},
        }
        raw_config = {
            "schema": CONFIG_SCHEMA_V2,
            "snapshots_dir": eff_snapshots_raw,
            "cds_lib": eff_cds_lib,
            "oa_libs": oa_libs,
            "allow_libraries": allow_libraries,
            "backend": eff_backend,
            "virtuoso_cmd": eff_virtuoso_cmd,
            "ssh_host": eff_ssh_host,
            "remote_workdir": args.remote_workdir,
            "timeout_s": eff_timeout_s,
            "daemon": daemon_cfg,
            "cdf": cdf_cfg,
        }
        config_path = write_project_config(root, raw_config, force=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        config = load_project_config(root)

        if not no_git:
            git_initialized, _git_top = ensure_git_repo(root)
            ignore_paths = _collect_repo_local_oa_ignore_paths(
                root,
                config.get("oa_libs") or {},
                discovery,
                snapshots_dir=snapshots_dir,
            )
            gi_path, gitignore_updated = update_managed_gitignore(root, ignore_paths)
            gitignore_path_str = str(gi_path)
    else:
        if (v1_path.exists() or v2_path.exists()) and not args.force:
            existing_path = v2_path if v2_path.exists() else v1_path
            raise SchemaValidationError(
                f"configuration already exists: {existing_path}; "
                "pass --sync to refresh cds.lib/.gitignore or --force to overwrite"
            )
        eff_snapshots_raw = (
            DEFAULT_SNAPSHOTS_DIR if args.snapshots_dir is None else str(args.snapshots_dir)
        )
        snapshots_dir = _resolve_path(root, eff_snapshots_raw)
        eff_backend = DEFAULT_BACKEND if args.backend is None else args.backend
        eff_timeout_s = DEFAULT_TIMEOUT_S if raw_timeout_s is None else raw_timeout_s
        config = {
            "schema": CONFIG_SCHEMA,
            "snapshots_dir": eff_snapshots_raw,
            "cds_lib": None if args.cds_lib is None else str(args.cds_lib),
            "allow_libraries": cli_allow_libraries,
            "backend": eff_backend,
            "virtuoso_cmd": args.virtuoso_cmd,
            "ssh_host": args.ssh_host,
            "remote_workdir": args.remote_workdir,
            "timeout_s": eff_timeout_s,
        }
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        v1_path.write_text(_json_dump(config), encoding="utf-8")
        config_path = v1_path

    discovered_summary: dict[str, Any] = {
        "cds_lib": (
            None
            if (discovery is None or discovery.cds_lib is None)
            else str(discovery.cds_lib)
        ),
        "oa_libs": dict(discovery.oa_libs) if discovery is not None else {},
        "allow_libraries": list(discovery.allow_libraries) if discovery is not None else [],
        "excluded_count": len(discovery.excluded) if discovery is not None else 0,
    }
    result: dict[str, Any] = {
        "ok": True,
        "config_path": str(config_path),
        "snapshots_dir": str(snapshots_dir),
        "config": config,
        "git_initialized": git_initialized,
        "gitignore_path": gitignore_path_str,
        "gitignore_updated": gitignore_updated,
        "discovered": discovered_summary,
    }
    if synced_existing:
        result["synced"] = True

    if args.json:
        _print_json(result)
    else:
        action = "Synced" if synced_existing else "Initialized"
        print(f"{action} oa-git configuration at {config_path}")
        print(f"Snapshots directory: {snapshots_dir}")
        if use_v2:
            print(f"cds.lib: {config.get('cds_lib') or 'none'}")
            print(f"Virtuoso cmd: {config.get('virtuoso_cmd') or DEFAULT_VIRTUOSO_CMD}")
            cfg_oa_libs = config.get("oa_libs") or {}
            if cfg_oa_libs:
                oa_summary = ", ".join(f"{k}={v}" for k, v in sorted(cfg_oa_libs.items()))
            else:
                oa_summary = "none"
            print(f"OA libraries: {oa_summary}")
            cfg_allow = config.get("allow_libraries") or []
            print(f"Allow libraries: {', '.join(cfg_allow) if cfg_allow else 'none'}")
            if discovery is not None and discovery.excluded:
                print(f"Excluded cds.lib entries: {len(discovery.excluded)}")
            if git_initialized:
                print(f"Git repository: initialized at {root}")
            if gitignore_path_str is not None:
                gi_state = "updated" if gitignore_updated else "up to date"
                print(f".gitignore: {gi_state} ({gitignore_path_str})")
            elif no_git:
                print(".gitignore: skipped (--no-git)")
        else:
            print(
                "Tip: omit --v1 (or pass --v2) to use .oagit/config.json with auto-discovery, dirty-index tracking, and lazy daemon."
            )
    return 0


def _git_status(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BridgeExecutionError(f"unable to run git status: {exc}") from exc
    if result.returncode == 0:
        return result.stdout.splitlines()
    if "not a git repository" in result.stderr.lower():
        return []
    raise BridgeExecutionError(result.stderr.strip() or "git status failed")


def _status(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root)
    settings = _settings(args, root, config)
    snapshots_dir: Path = settings["snapshots_dir"]
    oa_libs: dict[str, Path] = dict(settings["oa_libs"])
    index_entries = load_index(root)

    requested_cvs = [_parse_cellview(spec) for spec in (getattr(args, "cellviews", None) or ())]
    requested_keys = {cv.key for cv in requested_cvs}

    cli_lib_maps: list[tuple[str, Path]] = []
    for raw_mapping in _flatten_option(args.oa_lib_map):
        library, raw_directory = _parse_library_map(raw_mapping)
        resolved_dir = _resolve_path(root, raw_directory)
        cli_lib_maps.append((library, resolved_dir))
        oa_libs[library] = resolved_dir

    snapshot_entries: list[dict[str, Any]] = []
    discovered_snap_cvs: dict[str, CellViewRef] = {}
    discovered_snap_view_types: dict[str, str] = {}
    if snapshots_dir.is_dir():
        paths = sorted(
            {
                manifest.parent
                for manifest in snapshots_dir.rglob("manifest.json")
                if manifest.is_file() or manifest.is_symlink()
            },
            key=str,
        )
    else:
        paths = []

    for snapshot_dir in paths:
        entry: dict[str, Any] = {
            "snapshot_dir": str(snapshot_dir),
            "ok": False,
            "view_type": None,
            "cellview": None,
            "omissions_count": None,
            "has_omissions": None,
        }
        try:
            manifest = _verify_any_snapshot(snapshot_dir)
        except OaGitError as exc:
            entry.update({"error_type": type(exc).__name__, "error": str(exc)})
            if requested_keys:
                # Best effort filter by path suffix if manifest failed
                try:
                    raw_m = read_manifest_file(snapshot_dir)
                    if raw_m.cellview.key not in requested_keys:
                        continue
                except OaGitError:
                    pass
        else:
            if requested_keys and manifest.cellview.key not in requested_keys:
                continue
            discovered_snap_cvs[manifest.cellview.key] = manifest.cellview
            discovered_snap_view_types[manifest.cellview.key] = manifest.view_type
            entry.update(
                {
                    "ok": True,
                    "view_type": manifest.view_type,
                    "cellview": manifest.cellview.to_dict(),
                    "omissions_count": manifest.omissions_count,
                    "has_omissions": manifest.omissions_count > 0,
                }
            )
            if oa_libs:
                candidates = _source_file_candidates_for_view(
                    manifest.cellview,
                    snapshots_dir=snapshots_dir,
                    view_type=manifest.view_type,
                    index_entry=index_entries.get(manifest.cellview.key),
                )
                idx_state, _ = classify_cellview_state(
                    manifest.cellview,
                    oa_libs=oa_libs,
                    snapshots_dir=snapshots_dir,
                    index_entries=index_entries,
                    candidates=candidates,
                    verify_snapshot_fn=_verify_any_snapshot,
                )
                entry["index_state"] = idx_state
        snapshot_entries.append(entry)

    # Compute dirty-index states across requested CellViews, indexed entries, and snapshots.
    index_states: list[dict[str, Any]] = []
    if oa_libs or index_entries or requested_cvs:
        tracked_map: dict[str, CellViewRef] = {}
        if requested_cvs:
            for cv in requested_cvs:
                tracked_map[cv.key] = cv
        else:
            for key, idx_entry in sorted(index_entries.items()):
                tracked_map[key] = idx_entry.cellview
            for key, cv in sorted(discovered_snap_cvs.items()):
                tracked_map.setdefault(key, cv)
        for key in sorted(tracked_map):
            cv = tracked_map[key]
            candidates = _source_file_candidates_for_view(
                cv,
                snapshots_dir=snapshots_dir,
                view_type=discovered_snap_view_types.get(key),
                index_entry=index_entries.get(key),
            )
            state, reason = explain_cellview_state(
                cv,
                oa_libs=oa_libs,
                snapshots_dir=snapshots_dir,
                index_entries=index_entries,
                candidates=candidates,
                verify_snapshot_fn=_verify_any_snapshot,
            )
            if getattr(args, "changed", False) and state == "clean":
                continue
            index_states.append(
                {
                    "cellview": cv.to_dict(),
                    "key": cv.key,
                    "state": state,
                    "reason": reason,
                    "snapshot_dir": str(snapshot_dir_for(snapshots_dir, cv)),
                }
            )

    if getattr(args, "changed", False) and oa_libs:
        snapshot_entries = [
            e for e in snapshot_entries if (not e["ok"] or e.get("index_state") != "clean")
        ]

    lock_entries: list[dict[str, Any]] = []
    probed_dirs: set[tuple[str, str]] = set()
    lock_targets: list[tuple[str, Path]] = list(cli_lib_maps)
    for lib_name, lib_dir in sorted(oa_libs.items()):
        if (lib_name, str(lib_dir)) not in {(l, str(d)) for l, d in lock_targets}:
            if lib_dir.exists():
                lock_targets.append((lib_name, lib_dir))
    for library, directory in lock_targets:
        pair = (library, str(directory))
        if pair in probed_dirs:
            continue
        probed_dirs.add(pair)
        report = probe_locks(directory)
        lock_entries.append(
            {
                "library": library,
                "directory": str(directory),
                "report": report.to_dict(),
                "blocking": report.has_locks,
            }
        )

    git_status = _git_status(root)
    workspace_discovery: WorkspaceDiscovery | None = settings.get("workspace_discovery")
    gitignore_health: dict[str, list[str]] = {
        "unignored_oa_libs": [],
        "ignored_snapshot_paths": [],
    }
    unsynced_oa_libs: list[str] = []
    status_warnings: list[str] = []
    status_hints: list[str] = []
    if workspace_discovery is not None:
        if is_inside_git_repo(root) is not None:
            gitignore_health = check_gitignore_health(
                root,
                snapshots_dir=snapshots_dir,
                discovery=workspace_discovery,
            )
        configured_oa_libs = config.get("oa_libs") or {}
        unsynced_set: set[str] = set(gitignore_health["unignored_oa_libs"])
        for ws_entry in workspace_discovery.entries:
            if (
                ws_entry.classification == "design_local"
                and ws_entry.name not in configured_oa_libs
            ):
                unsynced_set.add(ws_entry.name)
        unsynced_oa_libs = sorted(unsynced_set)
        for snap_path in gitignore_health["ignored_snapshot_paths"]:
            status_warnings.append(
                f"snapshot path '{snap_path}' is ignored by .gitignore "
                "(use leading '/<lib>/' for OA libraries)"
            )
        is_v2_cfg = config.get("schema") == CONFIG_SCHEMA_V2
        for lib_name in unsynced_oa_libs:
            if is_v2_cfg:
                status_hints.append(
                    f"new OA library '{lib_name}' in cds.lib is not synced; run 'oagit init --sync'"
                )
            else:
                status_hints.append(
                    f"new OA library '{lib_name}' in cds.lib is not synced; run 'oagit init --force' to migrate to v2"
                )

    ok = all(entry["ok"] for entry in snapshot_entries) and not any(
        entry["blocking"] for entry in lock_entries
    )
    result: dict[str, Any] = {
        "ok": ok,
        "root": str(root),
        "snapshots_dir": str(snapshots_dir),
        "snapshots": snapshot_entries,
        "index_states": index_states,
        "locks": lock_entries,
        "git_status": git_status,
        "gitignore_health": {
            "unignored_oa_libs": gitignore_health["unignored_oa_libs"],
            "ignored_snapshot_paths": gitignore_health["ignored_snapshot_paths"],
            "unsynced_oa_libs": unsynced_oa_libs,
        },
        "warnings": status_warnings,
        "hints": status_hints,
    }
    if args.json:
        _print_json(result)
    else:
        _print_status_text(
            root=root,
            snapshots_dir=snapshots_dir,
            snapshot_entries=snapshot_entries,
            index_states=index_states,
            lock_entries=lock_entries,
            git_status=git_status,
            ok=ok,
            warnings=status_warnings,
            hints=status_hints,
        )
    return 0 if ok else 1


def _compact_snapshot_key(
    entry: Mapping[str, Any],
    *,
    root: Path,
    snapshots_dir: Path,
) -> str:
    cv = entry.get("cellview")
    if isinstance(cv, Mapping) and all(k in cv for k in ("library", "cell", "view")):
        return f"{cv['library']}/{cv['cell']}/{cv['view']}"
    snap_path = Path(str(entry["snapshot_dir"]))
    try:
        return snap_path.resolve().relative_to(snapshots_dir.resolve()).as_posix()
    except ValueError:
        return _relative_posix(root, snap_path)


def _print_status_text(
    *,
    root: Path,
    snapshots_dir: Path,
    snapshot_entries: Sequence[Mapping[str, Any]],
    index_states: Sequence[Mapping[str, Any]],
    lock_entries: Sequence[Mapping[str, Any]],
    git_status: Sequence[str],
    ok: bool,
    warnings: Sequence[str] = (),
    hints: Sequence[str] = (),
) -> None:
    broken = [e for e in snapshot_entries if not e["ok"]]
    healthy = [e for e in snapshot_entries if e["ok"]]
    snap_by_key: dict[str, Mapping[str, Any]] = {}
    for e in healthy:
        snap_by_key[_compact_snapshot_key(e, root=root, snapshots_dir=snapshots_dir)] = e

    if broken:
        print(f"Broken snapshots ({len(broken)}):")
        for entry in broken:
            label = _compact_snapshot_key(entry, root=root, snapshots_dir=snapshots_dir)
            print(f"  FAILED  {label}: {entry.get('error', 'integrity verification failed')}")

    has_dirty = False
    if index_states:
        dirty_items = [i for i in index_states if i["state"] in {"dirty", "missing"}]
        unknown_items = [i for i in index_states if i["state"] == "unknown"]
        clean_items = [i for i in index_states if i["state"] == "clean"]
        has_dirty = any(i["state"] == "dirty" for i in dirty_items)

        def _fmt_index_line(item: Mapping[str, Any], *, include_reason: bool) -> str:
            key = str(item["key"])
            snap_info = snap_by_key.get(key)
            view_type = (
                snap_info.get("view_type")
                if snap_info is not None
                else item.get("cellview", {}).get("view")
            )
            view_tag = f" [{view_type}]" if view_type else ""
            om_count = (snap_info.get("omissions_count") or 0) if snap_info is not None else 0
            om_tag = f" (omissions={om_count})" if om_count > 0 else ""
            reason = item.get("reason")
            reason_tag = f"  ({reason})" if include_reason and reason and reason != item["state"] else ""
            return f"  {item['state']:7s} {key}{view_tag}{om_tag}{reason_tag}"

        if dirty_items:
            print(f"Modified / Dirty ({len(dirty_items)}):")
            for item in dirty_items:
                print(_fmt_index_line(item, include_reason=True))
        if unknown_items:
            print(f"Unknown ({len(unknown_items)}):")
            for item in unknown_items:
                print(_fmt_index_line(item, include_reason=True))
        if clean_items:
            print(f"Clean ({len(clean_items)}):")
            for item in clean_items:
                print(_fmt_index_line(item, include_reason=False))
    else:
        if healthy:
            print(f"Snapshots ({len(healthy)}):")
            for entry in healthy:
                label = _compact_snapshot_key(entry, root=root, snapshots_dir=snapshots_dir)
                view_tag = f" [{entry['view_type']}]" if entry.get("view_type") else ""
                om_count = entry.get("omissions_count") or 0
                om_tag = f" (omissions={om_count})" if om_count > 0 else ""
                print(f"  OK      {label}{view_tag}{om_tag}")
        elif not broken:
            print("Snapshots: none")

    if lock_entries:
        print("Locks:")
        for entry in lock_entries:
            state = "BLOCKING" if entry["blocking"] else "clear"
            rel_dir = _relative_posix(root, Path(str(entry["directory"])))
            print(f"  {state:8s} {entry['library']} ({rel_dir})")
    else:
        print("Locks: none")

    if not git_status:
        print("Git status: clean")
    else:
        print("Git status:")
        for line in git_status:
            print(f"  {line}")

    for warning in warnings:
        print(f"Warning: {warning}")
    for hint in hints:
        print(f"Hint: {hint}")
    if has_dirty:
        print("Hint: run 'oagit capture --changed'")
    if not ok:
        print("Status checks failed")


def _captured_item_dict(snapshot: Any, destination: Path) -> dict[str, Any]:
    manifest = snapshot.manifest
    item: dict[str, Any] = {
        "snapshot_dir": str(destination),
        "view_type": manifest.view_type,
        "cellview": manifest.cellview.to_dict(),
        "electrical_sha256": manifest.electrical_sha256,
        "placement_sha256": manifest.placement_sha256,
        "content_sha256": manifest.content_sha256,
        "full_sha256": manifest.full_sha256,
        "omissions_count": manifest.omissions_count,
    }
    if manifest.view_type == "layout" and isinstance(snapshot, LayoutSnapshot):
        item["object_count"] = len(snapshot.objects)
    return item


def _is_skipped_target_strictly_clean(
    root_cv: CellViewRef,
    *,
    oa_libs: Mapping[str, Path],
    snapshots_dir: Path,
    index_entries: Mapping[str, IndexEntry],
    hier: bool,
    include_symbols: bool,
) -> bool:
    """Defense-in-depth clean-skip gate for ``--hier`` and ``--include-symbols``."""

    def _state(cv: CellViewRef) -> str:
        state, _ = classify_cellview_state(
            cv,
            oa_libs=oa_libs,
            snapshots_dir=snapshots_dir,
            index_entries=index_entries,
            verify_snapshot_fn=_verify_any_snapshot,
        )
        return state

    root_entry = index_entries.get(root_cv.key)
    if root_entry is None or _state(root_cv) != "clean":
        return False

    schematic_cvs: list[CellViewRef] = []
    if root_cv.view == "schematic":
        schematic_cvs.append(root_cv)

    if hier:
        if root_cv.view == "schematic" and not root_entry.hierarchy_complete:
            return False
        visited: set[str] = {root_cv.key}
        queue: list[str] = list(root_entry.dependencies)
        while queue:
            dep_key = queue.pop(0)
            if dep_key in visited:
                continue
            visited.add(dep_key)
            dep_entry = index_entries.get(dep_key)
            if dep_entry is None:
                return False
            try:
                dep_cv = CellViewRef.parse(dep_key)
            except SchemaValidationError:
                return False
            if _state(dep_cv) != "clean":
                return False
            if dep_cv.view == "schematic":
                if not dep_entry.hierarchy_complete:
                    return False
                schematic_cvs.append(dep_cv)
            queue.extend(dep_entry.dependencies)

    if include_symbols:
        for sch_cv in schematic_cvs:
            sym_cv = CellViewRef(sch_cv.library, sch_cv.cell, "symbol")
            if sym_cv.key not in index_entries or _state(sym_cv) != "clean":
                return False

    return True


def _capture_single_non_layout(
    backend: Any,
    target: CellViewRef,
    *,
    view_type: str,
    cds_lib: Path | str | None,
    session: OaSession | None,
) -> Any:
    if view_type == "schematic":
        sig = inspect.signature(backend.capture_schematic)
        if "session" in sig.parameters:
            return backend.capture_schematic(target, cds_lib=cds_lib, session=session)
        return backend.capture_schematic(target, cds_lib=cds_lib)
    if view_type == "symbol":
        if not hasattr(backend, "capture_symbol"):
            raise BridgeExecutionError("configured backend does not support symbol capture")
        return backend.capture_symbol(target, cds_lib=cds_lib, session=session)
    raise SchemaValidationError(f"unsupported capture view {target.view!r}")


def _capture(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root)
    settings = _settings(args, root, config)
    snapshots_dir: Path = settings["snapshots_dir"]
    oa_libs: dict[str, Path] = settings["oa_libs"]
    index_entries = load_index(root)

    raw_specs: list[str] = list(getattr(args, "cellviews", None) or ())
    hier = bool(getattr(args, "hier", False))
    include_symbols = bool(getattr(args, "include_symbols", False))
    changed_only = bool(getattr(args, "changed", False))
    recompute_derived = bool(getattr(args, "recompute_derived", False))
    max_depth = int(getattr(args, "max_depth", DEFAULT_MAX_HIER_DEPTH))
    explicit_view_type = str(getattr(args, "view_type", "auto") or "auto")

    if raw_specs:
        requested_cvs = [_parse_cellview(spec) for spec in raw_specs]
    else:
        tracked: dict[str, CellViewRef] = {
            key: index_entries[key].cellview for key in sorted(index_entries)
        }
        if snapshots_dir.is_dir():
            for manifest_path in sorted(snapshots_dir.rglob("manifest.json"), key=str):
                try:
                    m = read_manifest_file(manifest_path.parent)
                    tracked.setdefault(m.cellview.key, m.cellview)
                except OaGitError:
                    continue
        requested_cvs = [tracked[key] for key in sorted(tracked)]
        if not requested_cvs:
            raise SchemaValidationError(
                "no CELLVIEW specified and no tracked entries or snapshots found; "
                "pass <lib>/<cell>/<view> (e.g. 'oagit capture work/amp/schematic')"
            )
        changed_only = True

    if args.out is not None and (len(requested_cvs) != 1 or hier or include_symbols):
        raise SchemaValidationError(
            "--out can only be used when capturing a single CellView without --hier or --include-symbols"
        )

    # Resolve view_type for every requested CellView (auto or explicit --view-type).
    deduped_requested: list[CellViewRef] = []
    seen_req: set[str] = set()
    target_view_types: dict[str, str] = {}
    for cv in requested_cvs:
        if cv.key in seen_req:
            continue
        seen_req.add(cv.key)
        deduped_requested.append(cv)
        if explicit_view_type != "auto":
            vt = _detect_capture_view_type(
                cv, snapshots_dir=snapshots_dir, explicit_view_type=explicit_view_type
            )
        else:
            idx_entry = index_entries.get(cv.key)
            if idx_entry is not None and any(
                fp.path.endswith("/layout.oa") for fp in idx_entry.source_files
            ):
                vt = "layout"
            else:
                vt = _detect_capture_view_type(
                    cv, snapshots_dir=snapshots_dir, explicit_view_type=explicit_view_type
                )
        target_view_types[cv.key] = vt

    skipped_records: list[dict[str, Any]] = []
    if changed_only:
        sch_sym_requested = [
            cv for cv in deduped_requested if target_view_types[cv.key] != "layout"
        ]
        layout_requested = [
            cv for cv in deduped_requested if target_view_types[cv.key] == "layout"
        ]
        dirty_keys: set[str] = set()
        skip_reasons: dict[str, str] = {}

        if sch_sym_requested:
            fdt_kwargs: dict[str, Any] = {
                "oa_libs": oa_libs,
                "snapshots_dir": snapshots_dir,
                "index_entries": index_entries,
                "hier": hier,
                "verify_snapshot_fn": _verify_any_snapshot,
            }
            if "include_symbols" in inspect.signature(filter_dirty_targets).parameters:
                fdt_kwargs["include_symbols"] = include_symbols
            sch_dirty, raw_skipped_items = filter_dirty_targets(
                sch_sym_requested,
                **fdt_kwargs,
            )
            for cv in sch_dirty:
                dirty_keys.add(cv.key)
            for item in raw_skipped_items:
                if (hier or include_symbols) and not _is_skipped_target_strictly_clean(
                    item.cellview,
                    oa_libs=oa_libs,
                    snapshots_dir=snapshots_dir,
                    index_entries=index_entries,
                    hier=hier,
                    include_symbols=include_symbols,
                ):
                    dirty_keys.add(item.cellview.key)
                else:
                    skip_reasons[item.cellview.key] = item.reason or "clean"

        for cv in layout_requested:
            canonical_dest = snapshot_dir_for(snapshots_dir, cv)
            custom_out = (
                _resolve_path(root, args.out) if args.out is not None else canonical_dest
            )
            if custom_out != canonical_dest:
                dirty_keys.add(cv.key)
                continue
            candidates = _source_file_candidates_for_view(
                cv,
                snapshots_dir=snapshots_dir,
                view_type="layout",
                index_entry=index_entries.get(cv.key),
            )
            state, _fps = classify_cellview_state(
                cv,
                oa_libs=oa_libs,
                snapshots_dir=snapshots_dir,
                index_entries=index_entries,
                candidates=candidates,
                verify_snapshot_fn=_verify_any_snapshot,
            )
            if state == "clean":
                skip_reasons[cv.key] = "clean"
            else:
                dirty_keys.add(cv.key)

        dirty_targets: list[CellViewRef] = []
        for cv in deduped_requested:
            if cv.key in dirty_keys:
                dirty_targets.append(cv)
            elif cv.key in skip_reasons:
                skipped_records.append(
                    {
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "reason": skip_reasons[cv.key],
                        "snapshot_dir": str(snapshot_dir_for(snapshots_dir, cv)),
                    }
                )
    else:
        dirty_targets = list(deduped_requested)

    # Zero backend / Virtuoso calls when all requested targets are clean!
    if not dirty_targets:
        result: dict[str, Any] = {
            "ok": True,
            "captured": [],
            "skipped": skipped_records,
            "failed": [],
            "discovered": [],
            "cycles": [],
            "transport": "index_skip",
        }
        if args.json:
            _print_json(result)
        else:
            for rec in skipped_records:
                print(f"Skipped (clean): {rec['key']}")
            print(
                f"Captured 0, skipped {len(skipped_records)}, failed 0 (transport=index_skip)"
            )
        return 0

    sch_sym_dirty = [cv for cv in dirty_targets if target_view_types[cv.key] != "layout"]
    layout_dirty = [cv for cv in dirty_targets if target_view_types[cv.key] == "layout"]
    all_layout = bool(layout_dirty) and not sch_sym_dirty

    try:
        backend = _backend(settings)
    except (SchemaValidationError, BridgeExecutionError):
        if all_layout and settings["backend"] == "fixture":
            backend = FixtureLayoutBackend(fixture_path=settings["fixture"])
        else:
            raise

    session = (
        OaSession(session_id="cli-oneshot", transport="one_shot")
        if (getattr(args, "one_shot", False) or getattr(args, "no_daemon", False))
        else None
    )

    use_single_target = len(dirty_targets) == 1 and (
        all_layout or not (hier or include_symbols or recompute_derived)
    )
    captured_records: list[dict[str, Any]] = []
    failed_records: list[dict[str, Any]] = []
    discovered_list: list[dict[str, Any]] = []
    cycles_list: list[list[str]] = []
    session_meta: dict[str, Any] = {}

    if use_single_target:
        target = dirty_targets[0]
        target_vt = target_view_types[target.key]
        if target_vt == "layout":
            layout_backend = _resolve_layout_backend(backend, settings)
            snapshot: Any = _call_capture_layout(
                layout_backend, target, cds_lib=settings["cds_lib"], session=session
            )
            transport = (
                getattr(layout_backend, "last_transport", None)
                or getattr(backend, "last_transport", None)
                or ("fixture" if settings["backend"] == "fixture" else "one_shot")
            )
        else:
            snapshot = _capture_single_non_layout(
                backend,
                target,
                view_type=target_vt,
                cds_lib=settings["cds_lib"],
                session=session,
            )
            transport = getattr(backend, "last_transport", None) or (
                "fixture" if settings["backend"] == "fixture" else "one_shot"
            )

        destination = (
            _resolve_path(root, args.out)
            if args.out is not None
            else snapshot_dir_for(snapshots_dir, target)
        )
        _write_any_snapshot(snapshot, destination)
        _record_index_after_capture(
            root,
            config=config,
            oa_libs=oa_libs,
            snapshot=snapshot,
            destination=destination,
            dependencies=None,
            hierarchy_complete=None,
        )
        rec = _captured_item_dict(snapshot, destination)
        captured_records.append(rec)
        result = {
            "ok": True,
            **rec,
            "captured": captured_records,
            "skipped": skipped_records,
            "failed": [],
            "discovered": [],
            "cycles": [],
            "transport": transport,
        }
        if args.json:
            _print_json(result)
        else:
            for sk in skipped_records:
                print(f"Skipped (clean): {sk['key']}")
            print(f"Captured {snapshot.manifest.cellview.key} to {destination}")
            if snapshot.manifest.view_type == "schematic":
                print(f"Electrical SHA-256: {snapshot.manifest.electrical_sha256}")
                print(f"Placement SHA-256: {snapshot.manifest.placement_sha256}")
            else:
                print(f"Content SHA-256: {snapshot.manifest.content_sha256}")
            print(f"Omissions: {snapshot.manifest.omissions_count}")
            print(
                f"Captured {len(captured_records)}, skipped {len(skipped_records)}, "
                f"failed 0 (transport={transport})"
            )
        return 0

    # Multi-target / batch capture (schematic + symbol batch AND/OR layout targets)
    single_custom_out = (
        args.out is not None
        and len(deduped_requested) == 1
        and not hier
        and not include_symbols
    )
    if sch_sym_dirty:
        use_sch_batch = (
            len(sch_sym_dirty) > 1 or hier or include_symbols or recompute_derived
        )
        if use_sch_batch:
            batch_result = backend.capture_batch(
                sch_sym_dirty,
                cds_lib=settings["cds_lib"],
                hier=hier,
                max_depth=max_depth,
                recompute_derived=recompute_derived,
                include_symbols=include_symbols,
                session=session,
            )
            captured_keys = {
                item.cellview.key
                for item in batch_result.items
                if item.error is None and item.snapshot is not None
            }
            skipped_keys = {
                item.cellview.key for item in batch_result.items if item.skipped
            }
            failed_keys = {
                item.cellview.key
                for item in batch_result.items
                if not item.skipped and (item.error is not None or item.snapshot is None)
            }
            discovered_keys = {cv.key for cv in batch_result.discovered}
            captured_schematics = {
                item.cellview.key: item.snapshot
                for item in batch_result.items
                if item.error is None and isinstance(item.snapshot, SchematicSnapshot)
            }
            hier_completeness = (
                _compute_hier_completeness(
                    captured_schematics,
                    captured_keys=captured_keys,
                    skipped_keys=skipped_keys,
                    failed_keys=failed_keys,
                    discovered_keys=discovered_keys,
                    include_symbols=include_symbols,
                    batch_has_errors=bool(batch_result.session.get("errors")),
                )
                if hier
                else {}
            )
            for item in batch_result.items:
                if item.skipped:
                    skipped_records.append(
                        {
                            "cellview": item.cellview.to_dict(),
                            "key": item.cellview.key,
                            "reason": item.reason or "clean",
                            "snapshot_dir": str(snapshot_dir_for(snapshots_dir, item.cellview)),
                        }
                    )
                    continue
                if item.error is not None or item.snapshot is None:
                    failed_records.append(
                        {
                            "cellview": item.cellview.to_dict(),
                            "key": item.cellview.key,
                            "reason": item.reason,
                            "error": item.error or "capture failed",
                        }
                    )
                    continue
                destination = (
                    _resolve_path(root, args.out)
                    if single_custom_out and args.out is not None
                    else snapshot_dir_for(snapshots_dir, item.cellview)
                )
                _write_any_snapshot(item.snapshot, destination)
                if isinstance(item.snapshot, SchematicSnapshot):
                    if hier:
                        deps: tuple[str, ...] | None = _extract_schematic_dependencies(
                            item.snapshot, captured_keys=captured_keys | skipped_keys
                        )
                        hier_complete_val: bool | None = hier_completeness.get(
                            item.cellview.key, False
                        )
                    else:
                        prev_entry = index_entries.get(item.cellview.key)
                        prev_deps = set(prev_entry.dependencies) if prev_entry is not None else set()
                        own_sym = CellViewRef(
                            item.cellview.library, item.cellview.cell, "symbol"
                        ).key
                        if own_sym in captured_keys:
                            prev_deps.add(own_sym)
                        deps = tuple(sorted(prev_deps))
                        hier_complete_val = None
                else:
                    deps = ()
                    hier_complete_val = True
                _record_index_after_capture(
                    root,
                    config=config,
                    oa_libs=oa_libs,
                    snapshot=item.snapshot,
                    destination=destination,
                    dependencies=deps,
                    hierarchy_complete=hier_complete_val,
                )
                captured_records.append(_captured_item_dict(item.snapshot, destination))

            discovered_list = [cv.to_dict() for cv in batch_result.discovered]
            cycles_list = [list(c) for c in batch_result.cycles]
            session_meta = dict(batch_result.session)
        else:
            cv = sch_sym_dirty[0]
            try:
                snap = _capture_single_non_layout(
                    backend,
                    cv,
                    view_type=target_view_types[cv.key],
                    cds_lib=settings["cds_lib"],
                    session=session,
                )
            except OaGitError as exc:
                failed_records.append(
                    {
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "reason": "capture_failed",
                        "error": str(exc),
                    }
                )
            else:
                destination = snapshot_dir_for(snapshots_dir, cv)
                _write_any_snapshot(snap, destination)
                _record_index_after_capture(
                    root,
                    config=config,
                    oa_libs=oa_libs,
                    snapshot=snap,
                    destination=destination,
                    dependencies=None,
                    hierarchy_complete=None,
                )
                captured_records.append(_captured_item_dict(snap, destination))

    layout_backend_used: Any | None = None
    if layout_dirty:
        layout_backend_used = _resolve_layout_backend(backend, settings)
        for cv in layout_dirty:
            try:
                snap = _call_capture_layout(
                    layout_backend_used, cv, cds_lib=settings["cds_lib"], session=session
                )
            except OaGitError as exc:
                failed_records.append(
                    {
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "reason": "capture_failed",
                        "error": str(exc),
                    }
                )
                continue
            destination = (
                _resolve_path(root, args.out)
                if single_custom_out and args.out is not None
                else snapshot_dir_for(snapshots_dir, cv)
            )
            _write_any_snapshot(snap, destination)
            _record_index_after_capture(
                root,
                config=config,
                oa_libs=oa_libs,
                snapshot=snap,
                destination=destination,
                dependencies=(),
                hierarchy_complete=True,
            )
            captured_records.append(_captured_item_dict(snap, destination))

    transport = str(
        session_meta.get("transport")
        or (getattr(layout_backend_used, "last_transport", None) if layout_backend_used is not None else None)
        or getattr(backend, "last_transport", None)
        or ("fixture" if settings["backend"] == "fixture" else "one_shot")
    )
    ok = not failed_records
    result = {
        "ok": ok,
        "captured": captured_records,
        "skipped": skipped_records,
        "failed": failed_records,
        "discovered": discovered_list,
        "cycles": cycles_list,
        "session": session_meta,
        "transport": transport,
    }
    if len(captured_records) == 1 and not failed_records and not skipped_records:
        result.update(captured_records[0])

    if args.json:
        _print_json(result)
    else:
        for sk in skipped_records:
            print(f"Skipped (clean): {sk['key']}")
        for cap in captured_records:
            cv_dict = cap["cellview"]
            cv_key = f"{cv_dict['library']}/{cv_dict['cell']}/{cv_dict['view']}"
            om_suffix = f" (omissions={cap['omissions_count']})" if cap["omissions_count"] else ""
            print(f"Captured {cv_key} to {cap['snapshot_dir']}{om_suffix}")
        for fail in failed_records:
            print(f"Failed {fail['key']}: {fail['error']}")
        print(
            f"Captured {len(captured_records)}, skipped {len(skipped_records)}, "
            f"failed {len(failed_records)} (transport={transport})"
        )
    return 0 if ok else 1


def _restore(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root)
    settings = _settings(args, root, config)
    snapshot_dir = _resolve_snapshot_input(
        args.snapshot_dir,
        root=root,
        snapshots_dir=settings["snapshots_dir"],
        prefer_cwd=False,
    )
    snapshot = _read_any_snapshot(snapshot_dir, verify=True)
    target = snapshot.manifest.cellview if args.target is None else _parse_cellview(args.target)
    allow_libraries = _unique(
        _configured_libraries(config) + _flatten_option(args.allow_library)
    )
    oa_view_dir = _optional_path(root, args.oa_view_dir)
    if oa_view_dir is None and isinstance(
        snapshot, (SchematicSnapshot, SymbolSnapshot, LayoutSnapshot)
    ):
        lib_dir = settings["oa_libs"].get(target.library)
        if lib_dir is not None and lib_dir.is_dir():
            oa_view_dir = lib_dir / target.cell / target.view

    inline_flag = getattr(args, "inline_readback", None)
    if inline_flag is not None:
        inline_readback = bool(inline_flag)
    else:
        # Default to single-session inline readback for one-shot Virtuoso runs
        # so CLI restore only launches Virtuoso ONCE.
        inline_readback = (
            settings["backend"] == "virtuoso" and not settings["daemon_enabled"]
        )

    try:
        backend = _backend(settings, inline_readback=inline_readback)
    except (SchemaValidationError, BridgeExecutionError):
        if isinstance(snapshot, LayoutSnapshot) and settings["backend"] == "fixture":
            backend = FixtureLayoutBackend(fixture_path=settings["fixture"])
        else:
            raise
    session = (
        OaSession(session_id="cli-oneshot", transport="one_shot")
        if (getattr(args, "one_shot", False) or getattr(args, "no_daemon", False))
        else None
    )

    if isinstance(snapshot, SchematicSnapshot):
        kwargs: dict[str, Any] = {
            "cds_lib": settings["cds_lib"],
            "allow_libraries": allow_libraries,
            "overwrite": args.overwrite,
            "allow_omissions": args.allow_omissions,
            "oa_view_dir": oa_view_dir,
            "verify_readback": not args.skip_readback,
            "check_placement": not args.skip_placement_check,
        }
        sig = inspect.signature(backend.restore_schematic)
        if "recompute_derived" in sig.parameters:
            kwargs["recompute_derived"] = bool(getattr(args, "recompute_derived", False))
        if "max_passes" in sig.parameters:
            kwargs["max_passes"] = settings["max_passes"]
        if "coupling_rules" in sig.parameters:
            kwargs["coupling_rules"] = settings["coupling_rules"]
        if "inline_readback" in sig.parameters:
            kwargs["inline_readback"] = inline_readback
        if "session" in sig.parameters:
            kwargs["session"] = session
        receipt = backend.restore_schematic(snapshot, target, **kwargs)
    elif isinstance(snapshot, SymbolSnapshot):
        if not hasattr(backend, "restore_symbol"):
            raise BridgeExecutionError("configured backend does not support symbol restore")
        kwargs = {
            "cds_lib": settings["cds_lib"],
            "allow_libraries": allow_libraries,
            "overwrite": args.overwrite,
            "allow_omissions": args.allow_omissions,
            "oa_view_dir": oa_view_dir,
            "verify_readback": not args.skip_readback,
        }
        sig = inspect.signature(backend.restore_symbol)
        if "inline_readback" in sig.parameters:
            kwargs["inline_readback"] = inline_readback
        if "session" in sig.parameters:
            kwargs["session"] = session
        receipt = backend.restore_symbol(snapshot, target, **kwargs)
    elif isinstance(snapshot, LayoutSnapshot):
        layout_backend = _resolve_layout_backend(backend, settings)
        receipt = _call_restore_layout(
            layout_backend,
            snapshot,
            target,
            cds_lib=settings["cds_lib"],
            allow_libraries=allow_libraries,
            overwrite=args.overwrite,
            allow_omissions=args.allow_omissions,
            oa_view_dir=oa_view_dir,
            verify_readback=not args.skip_readback,
            session=session,
        )
    else:
        raise SchemaValidationError(
            f"unsupported snapshot view_type for restore: {snapshot.manifest.view_type!r}"
        )

    result = receipt.to_dict()
    result["ok"] = True
    result["view_type"] = snapshot.manifest.view_type
    if snapshot.manifest.view_type == "layout" and "transport" not in result:
        result["transport"] = getattr(receipt, "transport", "one_shot")
    if args.json:
        _print_json(result)
    else:
        print(f"Restored {receipt.target.key}")
        print(f"Published: {receipt.published}")
        if snapshot.manifest.view_type == "layout":
            print(f"PCell verified: {receipt.pcell_verified}")
            print(f"Connectivity verified: {receipt.connectivity_verified}")
        print(f"Readback verified: {receipt.readback_verified}")
        if snapshot.manifest.view_type == "schematic":
            print(f"CDF verified: {receipt.cdf_verified}")
        if receipt.transport != "one_shot":
            print(f"Transport: {receipt.transport}")
    return 0


def _local_snapshot_candidate(value: str, root: Path) -> Path | None:
    raw = Path(value).expanduser()
    candidates = [raw] if raw.is_absolute() else [raw, root / raw]
    for candidate in candidates:
        if candidate.exists():
            if not candidate.is_dir() or not (candidate / "manifest.json").is_file():
                raise SchemaValidationError(
                    f"local snapshot directory must contain manifest.json: {candidate}"
                )
            return candidate.resolve()
    return None


def _git_toplevel(root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BridgeExecutionError(f"unable to run git rev-parse: {exc}") from exc
    if result.returncode != 0:
        raise BridgeExecutionError(
            result.stderr.strip() or f"{root} is not inside a Git repository"
        )
    return Path(result.stdout.strip()).resolve()


def _git_show(root: Path, revision_path: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "show", "--end-of-options", revision_path],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise BridgeExecutionError(f"unable to run git show: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BridgeExecutionError(
            f"unable to read {revision_path!r} from Git: {detail or 'git show failed'}"
        )
    return result.stdout


def _validate_git_revision(revision: str) -> str:
    if not isinstance(revision, str) or not revision:
        raise SchemaValidationError("Git revision must be a non-empty string")
    if revision.startswith("-") or any(
        ord(char) < 32 or ord(char) == 127 for char in revision
    ):
        raise SchemaValidationError(f"unsafe Git revision {revision!r}")
    return revision


def _verify_git_revision(root: Path, revision: str) -> str:
    revision = _validate_git_revision(revision)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BridgeExecutionError(f"unable to run git rev-parse: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise BridgeExecutionError(
            f"unable to resolve Git revision {revision!r}: {detail or 'git rev-parse failed'}"
        )
    return revision


def _git_snapshot(
    root: Path,
    snapshots_dir: Path,
    revision: str,
    cellview: CellViewRef,
    stack: ExitStack,
) -> Any:
    git_root = _git_toplevel(root)
    revision = _verify_git_revision(git_root, revision)
    relative_root = os.path.relpath(snapshot_dir_for(snapshots_dir, cellview), git_root)
    if relative_root == ".":
        relative_root = ""
    manifest_suffix = f"{relative_root}/manifest.json" if relative_root else "manifest.json"
    manifest_bytes = _git_show(git_root, f"{revision}:{manifest_suffix}")
    try:
        manifest_data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(
            f"Git revision {revision!r} contains an invalid manifest.json"
        ) from exc
    manifest_files = manifest_data.get("files", {}) if isinstance(manifest_data, Mapping) else {}
    if not isinstance(manifest_files, Mapping):
        raise SchemaValidationError("Git manifest.files must be a mapping")
    view_type = manifest_data.get("view_type", "schematic") if isinstance(manifest_data, Mapping) else "schematic"

    temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="oagit-diff-")))
    (temporary / "manifest.json").write_bytes(manifest_bytes)
    if view_type == "symbol":
        required_files = ["symbol.yaml"]
    elif view_type == "schematic":
        required_files = ["circuit.yaml", "placement.yaml"]
    elif view_type == "layout":
        if "objects.jsonl" not in manifest_files:
            raise SchemaValidationError(
                f"Git revision {revision!r} layout manifest.files must include 'objects.jsonl'"
            )
        for name in sorted(manifest_files):
            if name not in _ALLOWED_LAYOUT_GIT_FILES:
                raise SchemaValidationError(
                    f"unsafe or unsupported layout manifest file name in Git revision: {name!r}"
                )
        required_files = [
            name for name in sorted(manifest_files) if name != "omissions.json"
        ]
    else:
        required_files = [
            name for name in sorted(manifest_files) if name != "omissions.json"
        ]

    for name in required_files:
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise SchemaValidationError(f"unsafe manifest file name in Git revision: {name!r}")
        suffix = f"{relative_root}/{name}" if relative_root else name
        (temporary / name).write_bytes(_git_show(git_root, f"{revision}:{suffix}"))
    if "omissions.json" in manifest_files:
        suffix = f"{relative_root}/omissions.json" if relative_root else "omissions.json"
        (temporary / "omissions.json").write_bytes(_git_show(git_root, f"{revision}:{suffix}"))

    snapshot = _read_any_snapshot(temporary, verify=True)
    if snapshot.manifest.cellview != cellview:
        raise SchemaValidationError(
            f"Git revision {revision!r} snapshot cellview {snapshot.manifest.cellview.key!r} "
            f"does not match {cellview.key!r}"
        )
    return snapshot


def _diff_json(diff: Any, *, electrical_only: bool) -> dict[str, Any]:
    result = {"ok": True, **diff.to_dict()}
    if electrical_only:
        result.pop("placement", None)
    return result


def _symbol_diff_json(diff: Any, *, contract_only: bool) -> dict[str, Any]:
    result = {"ok": True, "view_type": "symbol", **diff.to_dict()}
    if contract_only:
        result.pop("pin_geometry_changes", None)
        result.pop("graphics_changes", None)
    return result


def _layout_diff_json(
    diff: Any, *, connectivity_only: bool, stat: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "view_type": "layout", **diff.to_dict()}
    if connectivity_only:
        result.pop("geometry", None)
        result.pop("annotation", None)
        result["connectivity_only"] = True
    if stat:
        result["summary"] = diff.summary()
        result["stat"] = format_layout_diff_stat(
            diff, connectivity_only=connectivity_only
        )
    return result


def _format_schematic_diff_stat(diff: Any, *, electrical_only: bool) -> str:
    header = (
        f"{diff.old_cellview.key} -> {diff.new_cellview.key}"
        if diff.cellview_changed
        else diff.new_cellview.key
    )
    changed = diff.has_electrical_changes if electrical_only else not diff.is_empty
    if not changed:
        return f"{header} [schematic]: no changes"
    e = diff.electrical
    elec_count = (
        len(e.ports)
        + len(e.nets)
        + len(e.instances_added)
        + len(e.instances_removed)
        + len(e.master_changes)
        + len(e.param_changes)
        + len(e.connectivity_changes)
        + len(e.instance_prop_changes)
        + len(e.property_changes)
        + len(e.constraint_changes)
    )
    parts = [f"electrical={elec_count}"]
    if not electrical_only:
        p = diff.placement
        place_count = (
            len(p.instance_moves)
            + len(p.pin_changes)
            + len(p.wires_added)
            + len(p.wires_removed)
            + len(p.labels_added)
            + len(p.labels_removed)
            + len(p.sheet_changes)
            + len(p.annotations_added)
            + len(p.annotations_removed)
        )
        parts.append(f"placement={place_count}")
    if diff.has_derived_changes:
        parts.append(f"derived={len(diff.derived_observation_changes)}")
    if diff.cellview_changed:
        parts.append("cellview_changed=yes")
    return f"{header} [schematic]: {', '.join(parts)}"


def _format_symbol_diff_stat(diff: Any, *, contract_only: bool) -> str:
    header = (
        f"{diff.old_cellview.key} -> {diff.new_cellview.key}"
        if diff.cellview_changed
        else diff.new_cellview.key
    )
    changed = (
        (diff.has_pin_contract_changes or diff.cellview_changed)
        if contract_only
        else not diff.is_empty
    )
    if not changed:
        return f"{header} [symbol]: no changes"
    pc = diff.pin_contract_changes
    contract_count = (
        len(pc.pins)
        + (1 if pc.port_order_change is not None else 0)
        + len(pc.property_changes)
    )
    parts = [f"contract={contract_count}"]
    if not contract_only:
        geom_count = len(diff.pin_geometry_changes.moves)
        g = diff.graphics_changes
        gfx_count = (
            len(g.shapes_added)
            + len(g.shapes_removed)
            + len(g.labels_added)
            + len(g.labels_removed)
            + len(g.nlp_labels_added)
            + len(g.nlp_labels_removed)
            + len(g.il_labels_added)
            + len(g.il_labels_removed)
            + (1 if g.selection_box_change is not None else 0)
            + (1 if g.origin_change is not None else 0)
        )
        parts.append(f"geometry={geom_count}")
        parts.append(f"graphics={gfx_count}")
    if diff.cellview_changed:
        parts.append("cellview_changed=yes")
    return f"{header} [symbol]: {', '.join(parts)}"


def _discover_git_cellviews(
    root: Path, snapshots_dir: Path, revision: str
) -> dict[str, CellViewRef]:
    """Discover CellViews stored under ``snapshots_dir`` at a Git revision."""

    git_root = _git_toplevel(root)
    revision = _verify_git_revision(git_root, revision)
    rel_snapshots = Path(os.path.relpath(snapshots_dir.resolve(), git_root)).as_posix()
    if rel_snapshots == ".." or rel_snapshots.startswith("../"):
        return {}
    cmd = ["git", "-C", str(git_root), "ls-tree", "-r", "-z", "--name-only", revision]
    if rel_snapshots and rel_snapshots != ".":
        cmd.extend(["--", rel_snapshots])
    try:
        result = subprocess.run(cmd, check=False, capture_output=True)
    except OSError as exc:
        raise BridgeExecutionError(f"unable to run git ls-tree: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise BridgeExecutionError(
            f"unable to list Git tree at {revision!r}: {detail or 'git ls-tree failed'}"
        )

    prefix = "" if rel_snapshots in {"", "."} else f"{rel_snapshots}/"
    discovered: dict[str, CellViewRef] = {}
    for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
        if not entry:
            continue
        if not entry.endswith("/manifest.json"):
            continue
        dir_path = entry[: -len("/manifest.json")]
        if prefix:
            if not dir_path.startswith(prefix):
                continue
            rel_cv = dir_path[len(prefix) :]
        else:
            rel_cv = dir_path
        parts = rel_cv.split("/")
        if len(parts) != 3:
            continue
        try:
            cv = CellViewRef(parts[0], parts[1], parts[2])
        except SchemaValidationError:
            continue
        discovered[cv.key] = cv
    return discovered


def _discover_local_cellviews(snapshots_dir: Path) -> dict[str, CellViewRef]:
    """Discover CellViews present in the local ``snapshots_dir`` tree."""

    discovered: dict[str, CellViewRef] = {}
    if not snapshots_dir.is_dir():
        return discovered
    for manifest_path in sorted(snapshots_dir.rglob("manifest.json"), key=str):
        if not (manifest_path.is_file() or manifest_path.is_symlink()):
            continue
        snap_dir = manifest_path.parent
        try:
            cv = read_manifest_file(snap_dir).cellview
        except OaGitError:
            try:
                rel_parts = snap_dir.relative_to(snapshots_dir).parts
                if len(rel_parts) == 3:
                    cv = CellViewRef(rel_parts[0], rel_parts[1], rel_parts[2])
                else:
                    continue
            except (ValueError, SchemaValidationError):
                continue
        discovered[cv.key] = cv
    return discovered


def _existing_snapshot_dir(value: str, root: Path) -> Path | None:
    raw = Path(value).expanduser()
    candidates = [raw] if raw.is_absolute() else [raw, root / raw]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "manifest.json").is_file():
            return candidate.resolve()
    return None


def _compute_single_diff(
    old_snapshot: Any,
    new_snapshot: Any,
    *,
    electrical_only: bool,
    contract_only: bool,
    connectivity_only: bool = False,
    stat: bool = False,
) -> tuple[dict[str, Any], str, bool]:
    old_type = old_snapshot.manifest.view_type
    new_type = new_snapshot.manifest.view_type
    if old_type != new_type:
        raise SchemaValidationError(
            f"cannot diff mismatched view types: {old_type!r} vs {new_type!r}"
        )

    if old_type == "schematic":
        eff_elec_only = bool(electrical_only or connectivity_only)
        diff = diff_schematics(old_snapshot, new_snapshot)
        payload = _diff_json(diff, electrical_only=eff_elec_only)
        text = (
            _format_schematic_diff_stat(diff, electrical_only=eff_elec_only)
            if stat
            else format_diff_text(diff, electrical_only=eff_elec_only)
        )
        if stat:
            payload["summary"] = diff.summary()
            payload["stat"] = text
        changed = diff.has_electrical_changes if eff_elec_only else not diff.is_empty
        return payload, text, changed

    if old_type == "symbol":
        eff_contract_only = bool(contract_only or electrical_only or connectivity_only)
        sym_diff = diff_symbols(old_snapshot, new_snapshot)
        payload = _symbol_diff_json(sym_diff, contract_only=eff_contract_only)
        text = (
            _format_symbol_diff_stat(sym_diff, contract_only=eff_contract_only)
            if stat
            else format_symbol_diff_text(sym_diff, contract_only=eff_contract_only)
        )
        if stat:
            payload["summary"] = sym_diff.summary()
            payload["stat"] = text
        changed = (
            (sym_diff.has_pin_contract_changes or sym_diff.cellview_changed)
            if eff_contract_only
            else not sym_diff.is_empty
        )
        return payload, text, changed

    if old_type == "layout":
        eff_conn_only = bool(connectivity_only or electrical_only)
        ldiff = diff_layouts(old_snapshot, new_snapshot)
        payload = _layout_diff_json(
            ldiff, connectivity_only=eff_conn_only, stat=stat
        )
        text = (
            format_layout_diff_stat(ldiff, connectivity_only=eff_conn_only)
            if stat
            else format_layout_diff_text(ldiff, connectivity_only=eff_conn_only)
        )
        changed = (
            (ldiff.has_connectivity_changes or ldiff.cellview_changed)
            if eff_conn_only
            else ldiff.has_changes
        )
        return payload, text, changed

    raise SchemaValidationError(f"unsupported view_type for diff: {old_type!r}")


def _load_diff_side(
    mode: str,
    val: Any,
    *,
    root: Path,
    snapshots_dir: Path,
    cellview: CellViewRef,
    stack: ExitStack,
) -> Any:
    if mode == "local_path":
        return _read_any_snapshot(val, verify=True)
    if mode == "working":
        return _read_any_snapshot(snapshot_dir_for(snapshots_dir, cellview), verify=True)
    if mode == "git_rev":
        return _git_snapshot(root, snapshots_dir, val, cellview, stack)
    raise SchemaValidationError(f"unsupported diff side mode {mode!r}")


def _parse_diff_targets(
    raw_targets: Sequence[str],
    *,
    root: Path,
    snapshots_dir: Path,
) -> tuple[tuple[str, Any], tuple[str, Any]]:
    if len(raw_targets) > 2:
        raise SchemaValidationError(
            f"diff accepts at most 2 targets, got {len(raw_targets)}"
        )
    if len(raw_targets) == 0:
        return ("git_rev", "HEAD"), ("working", None)
    if len(raw_targets) == 1:
        arg = raw_targets[0]
        raw_p = Path(arg).expanduser()
        path_exists = raw_p.exists() or (not raw_p.is_absolute() and (root / raw_p).exists())
        if not path_exists and ".." in arg:
            if "..." in arg or arg.count("..") > 1:
                raise SchemaValidationError(
                    f"unsupported Git revision range {arg!r}; expected OLD..NEW or OLD.."
                )
            left, right = arg.split("..", 1)
            old_rev = _validate_git_revision(left or "HEAD")
            if right == "":
                return ("git_rev", old_rev), ("working", None)
            new_rev = _validate_git_revision(right)
            return ("git_rev", old_rev), ("git_rev", new_rev)
        snap_dir = _existing_snapshot_dir(arg, root)
        if snap_dir is None:
            snap_dir = _resolve_existing_cellview_shorthand(arg, snapshots_dir)
        if snap_dir is not None:
            return ("git_rev", "HEAD"), ("local_path", snap_dir)
        return ("git_rev", _validate_git_revision(arg)), ("working", None)

    raw_old, raw_new = raw_targets
    sides: list[tuple[str, Any]] = []
    for raw_target in (raw_old, raw_new):
        existing_snap = _existing_snapshot_dir(raw_target, root)
        if existing_snap is not None:
            sides.append(("local_path", existing_snap))
            continue
        shorthand_path = _resolve_existing_cellview_shorthand(raw_target, snapshots_dir)
        if shorthand_path is not None:
            sides.append(("local_path", shorthand_path))
            continue
        local_path = _local_snapshot_candidate(raw_target, root)
        if local_path is not None:
            sides.append(("local_path", local_path))
            continue
        sides.append(("git_rev", raw_target))
    return sides[0], sides[1]


def _diff(args: argparse.Namespace) -> int:
    root, config = _diff_root_path(args.root)
    settings = _settings(args, root, config)
    snapshots_dir: Path = settings["snapshots_dir"]
    raw_targets: list[str] = list(getattr(args, "targets", None) or ())
    (old_mode, old_val), (new_mode, new_val) = _parse_diff_targets(
        raw_targets, root=root, snapshots_dir=snapshots_dir
    )
    explicit_cv = _parse_cellview(args.cellview) if args.cellview else None
    electrical_only = bool(args.electrical_only)
    contract_only = bool(getattr(args, "contract_only", False))
    connectivity_only = bool(getattr(args, "connectivity_only", False))
    stat = bool(getattr(args, "stat", False))

    with ExitStack() as stack:
        # Case A: at least one side is a concrete local snapshot path -> single diff
        if old_mode == "local_path" or new_mode == "local_path":
            old_snapshot = (
                _read_any_snapshot(old_val, verify=True) if old_mode == "local_path" else None
            )
            new_snapshot = (
                _read_any_snapshot(new_val, verify=True) if new_mode == "local_path" else None
            )
            cellview = explicit_cv
            if cellview is None:
                local_snaps = [s for s in (old_snapshot, new_snapshot) if s is not None]
                if len(local_snaps) == 1:
                    cellview = local_snaps[0].manifest.cellview
                elif len(local_snaps) == 2 and local_snaps[0].manifest.cellview == local_snaps[1].manifest.cellview:
                    cellview = local_snaps[0].manifest.cellview
            if old_snapshot is None:
                assert cellview is not None
                old_snapshot = _load_diff_side(
                    old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=cellview, stack=stack
                )
            if new_snapshot is None:
                assert cellview is not None
                new_snapshot = _load_diff_side(
                    new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=cellview, stack=stack
                )
            payload, text, changed = _compute_single_diff(
                old_snapshot,
                new_snapshot,
                electrical_only=electrical_only,
                contract_only=contract_only,
                connectivity_only=connectivity_only,
                stat=stat,
            )
            if args.json:
                _print_json(payload)
            else:
                print(text)
            return 1 if args.exit_code and changed else 0

        # Case B: explicit --cellview for Git-rev / working diff -> single diff
        if explicit_cv is not None:
            if new_mode == "working":
                old_cvs = _discover_git_cellviews(root, snapshots_dir, old_val)
                new_cvs = _discover_local_cellviews(snapshots_dir)
                in_old = explicit_cv.key in old_cvs
                in_new = explicit_cv.key in new_cvs
                if in_new and not in_old:
                    new_snapshot = _load_diff_side(
                        new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=explicit_cv, stack=stack
                    )
                    view_type = new_snapshot.manifest.view_type
                    if args.json:
                        _print_json(
                            {
                                "ok": True,
                                "cellview": explicit_cv.to_dict(),
                                "key": explicit_cv.key,
                                "view_type": view_type,
                                "status": "added",
                                "changed": True,
                                "old_cellview": None,
                                "new_cellview": explicit_cv.to_dict(),
                            }
                        )
                    else:
                        print(f"Added {explicit_cv.key} [{view_type}]")
                    return 1 if args.exit_code else 0
                if in_old and not in_new:
                    old_snapshot = _load_diff_side(
                        old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=explicit_cv, stack=stack
                    )
                    view_type = old_snapshot.manifest.view_type
                    if args.json:
                        _print_json(
                            {
                                "ok": True,
                                "cellview": explicit_cv.to_dict(),
                                "key": explicit_cv.key,
                                "view_type": view_type,
                                "status": "removed",
                                "changed": True,
                                "old_cellview": explicit_cv.to_dict(),
                                "new_cellview": None,
                            }
                        )
                    else:
                        print(f"Removed {explicit_cv.key} [{view_type}]")
                    return 1 if args.exit_code else 0
            old_snapshot = _load_diff_side(
                old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=explicit_cv, stack=stack
            )
            new_snapshot = _load_diff_side(
                new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=explicit_cv, stack=stack
            )
            payload, text, changed = _compute_single_diff(
                old_snapshot,
                new_snapshot,
                electrical_only=electrical_only,
                contract_only=contract_only,
                connectivity_only=connectivity_only,
                stat=stat,
            )
            if args.json:
                _print_json(payload)
            else:
                print(text)
            return 1 if args.exit_code and changed else 0

        # Case C: auto-discover cellviews across Git rev(s) and/or working snapshots/
        old_cvs = (
            _discover_git_cellviews(root, snapshots_dir, old_val)
            if old_mode == "git_rev"
            else _discover_local_cellviews(snapshots_dir)
        )
        new_cvs = (
            _discover_git_cellviews(root, snapshots_dir, new_val)
            if new_mode == "git_rev"
            else _discover_local_cellviews(snapshots_dir)
        )
        all_keys = sorted(set(old_cvs) | set(new_cvs))

        if not all_keys:
            if args.json:
                _print_json(
                    {
                        "ok": True,
                        "multi": True,
                        "count": 0,
                        "changed_count": 0,
                        "diffs": [],
                    }
                )
            else:
                print("No changes.")
            return 0

        if len(all_keys) == 1:
            key = all_keys[0]
            in_old = key in old_cvs
            in_new = key in new_cvs
            cv = new_cvs[key] if in_new else old_cvs[key]
            if in_old and in_new:
                old_snapshot = _load_diff_side(
                    old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                new_snapshot = _load_diff_side(
                    new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                payload, text, changed = _compute_single_diff(
                    old_snapshot,
                    new_snapshot,
                    electrical_only=electrical_only,
                    contract_only=contract_only,
                    connectivity_only=connectivity_only,
                    stat=stat,
                )
                if args.json:
                    _print_json(payload)
                else:
                    print(text)
                return 1 if args.exit_code and changed else 0

            if in_new:
                new_snapshot = _load_diff_side(
                    new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                view_type = new_snapshot.manifest.view_type
                if args.json:
                    _print_json(
                        {
                            "ok": True,
                            "cellview": cv.to_dict(),
                            "key": cv.key,
                            "view_type": view_type,
                            "status": "added",
                            "changed": True,
                            "old_cellview": None,
                            "new_cellview": cv.to_dict(),
                        }
                    )
                else:
                    print(f"Added {cv.key} [{view_type}]")
                return 1 if args.exit_code else 0

            old_snapshot = _load_diff_side(
                old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
            )
            view_type = old_snapshot.manifest.view_type
            if args.json:
                _print_json(
                    {
                        "ok": True,
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "view_type": view_type,
                        "status": "removed",
                        "changed": True,
                        "old_cellview": cv.to_dict(),
                        "new_cellview": None,
                    }
                )
            else:
                print(f"Removed {cv.key} [{view_type}]")
            return 1 if args.exit_code else 0

        # Multi-cellview diff (> 1 discovered cellviews)
        diff_items: list[dict[str, Any]] = []
        text_sections: list[str] = []
        for key in all_keys:
            in_old = key in old_cvs
            in_new = key in new_cvs
            cv = new_cvs[key] if in_new else old_cvs[key]
            if in_old and in_new:
                old_snapshot = _load_diff_side(
                    old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                new_snapshot = _load_diff_side(
                    new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                single_payload, single_text, changed = _compute_single_diff(
                    old_snapshot,
                    new_snapshot,
                    electrical_only=electrical_only,
                    contract_only=contract_only,
                    connectivity_only=connectivity_only,
                    stat=stat,
                )
                view_type = old_snapshot.manifest.view_type
                item: dict[str, Any] = {
                    "cellview": cv.to_dict(),
                    "key": cv.key,
                    "view_type": view_type,
                    "status": "modified" if changed else "unchanged",
                    "changed": changed,
                }
                for field_name, field_val in single_payload.items():
                    if field_name != "ok":
                        item[field_name] = field_val
                diff_items.append(item)
                if changed:
                    text_sections.append(
                        f"=== {cv.key} [{view_type}] (modified) ===\n{single_text}"
                    )
            elif in_new:
                new_snapshot = _load_diff_side(
                    new_mode, new_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                view_type = new_snapshot.manifest.view_type
                diff_items.append(
                    {
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "view_type": view_type,
                        "status": "added",
                        "changed": True,
                        "old_cellview": None,
                        "new_cellview": cv.to_dict(),
                    }
                )
                text_sections.append(
                    f"=== {cv.key} [{view_type}] (added) ===\nAdded {cv.key} [{view_type}]"
                )
            else:
                old_snapshot = _load_diff_side(
                    old_mode, old_val, root=root, snapshots_dir=snapshots_dir, cellview=cv, stack=stack
                )
                view_type = old_snapshot.manifest.view_type
                diff_items.append(
                    {
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "view_type": view_type,
                        "status": "removed",
                        "changed": True,
                        "old_cellview": cv.to_dict(),
                        "new_cellview": None,
                    }
                )
                text_sections.append(
                    f"=== {cv.key} [{view_type}] (removed) ===\nRemoved {cv.key} [{view_type}]"
                )

        changed_count = sum(1 for item in diff_items if item["changed"])
        if args.json:
            _print_json(
                {
                    "ok": True,
                    "multi": True,
                    "count": len(diff_items),
                    "changed_count": changed_count,
                    "diffs": diff_items,
                }
            )
        else:
            if not text_sections:
                print("No changes.")
            else:
                print("\n\n".join(text_sections))
        return 1 if args.exit_code and changed_count > 0 else 0


def _merge(args: argparse.Namespace) -> int:
    explicit_root = getattr(args, "root", None)
    root, config = _discover_project_and_config(explicit_root, allow_missing=True)
    snapshots_dir = _resolve_path(
        root, config.get("snapshots_dir", DEFAULT_SNAPSHOTS_DIR)
    )
    prefer_cwd = explicit_root is None

    base_path = _resolve_snapshot_input(
        args.base_dir, root=root, snapshots_dir=snapshots_dir, prefer_cwd=prefer_cwd
    )
    ours_path = _resolve_snapshot_input(
        args.ours_dir, root=root, snapshots_dir=snapshots_dir, prefer_cwd=prefer_cwd
    )
    theirs_path = _resolve_snapshot_input(
        args.theirs_dir, root=root, snapshots_dir=snapshots_dir, prefer_cwd=prefer_cwd
    )

    base = _read_any_snapshot(base_path, verify=True)
    ours = _read_any_snapshot(ours_path, verify=True)
    theirs = _read_any_snapshot(theirs_path, verify=True)

    view_types = (base.manifest.view_type, ours.manifest.view_type, theirs.manifest.view_type)
    if not (view_types[0] == view_types[1] == view_types[2]):
        raise SchemaValidationError(
            f"cannot merge snapshots with mismatched view types: {view_types!r}"
        )
    view_type = view_types[0]
    coupling_rules = _parse_coupling_rules(config) or None

    raw_out = Path(args.out_dir).expanduser()
    out_dir = (
        raw_out.resolve()
        if raw_out.is_absolute() or explicit_root is None
        else (root / raw_out).resolve()
    )

    if view_type == "schematic":
        result = merge_schematics(base, ours, theirs, coupling_rules=coupling_rules)
        if not result.ok:
            if args.json:
                _print_json(result.to_dict())
            else:
                print(format_merge_conflicts(result))
            return 1
        if result.snapshot is None:  # pragma: no cover - defensive contract check
            raise BridgeExecutionError("merge returned ok=True without a snapshot")
        write_snapshot(result.snapshot, out_dir)
        if args.json:
            _print_json(result.to_dict())
        else:
            print(f"Merged snapshots into {out_dir}")
        return 0

    if view_type == "symbol":
        sym_result = merge_symbols(base, ours, theirs)
        if not sym_result.ok:
            if args.json:
                _print_json(sym_result.to_dict())
            else:
                print(format_symbol_merge_conflicts(sym_result))
            return 1
        if sym_result.snapshot is None:  # pragma: no cover - defensive contract check
            raise BridgeExecutionError("symbol merge returned ok=True without a snapshot")
        write_symbol_snapshot(sym_result.snapshot, out_dir)
        if args.json:
            _print_json(sym_result.to_dict())
        else:
            print(f"Merged symbol snapshots into {out_dir}")
        return 0

    if view_type == "layout":
        layout_result = merge_layouts(
            base,
            ours,
            theirs,
            check_spatial_overlap=not bool(getattr(args, "allow_spatial_overlap", False)),
        )
        if not layout_result.ok:
            if args.json:
                payload = layout_result.to_dict()
                payload["view_type"] = "layout"
                _print_json(payload)
            else:
                print(format_layout_merge_conflicts(layout_result))
            return 1
        if layout_result.snapshot is None:  # pragma: no cover - defensive contract check
            raise BridgeExecutionError("layout merge returned ok=True without a snapshot")
        write_layout_snapshot(layout_result.snapshot, out_dir)
        if args.json:
            payload = layout_result.to_dict()
            payload["view_type"] = "layout"
            payload["snapshot_dir"] = str(out_dir)
            payload["content_sha256"] = layout_result.snapshot.manifest.content_sha256
            payload["full_sha256"] = layout_result.snapshot.manifest.full_sha256
            _print_json(payload)
        else:
            print(f"Merged layout snapshots into {out_dir}")
        return 0

    raise SchemaValidationError(f"unsupported view_type for merge: {view_type!r}")


def _daemon(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root)
    settings = _settings(args, root, config)
    idle_override = getattr(args, "idle_timeout_s", None)
    if idle_override is not None and (type(idle_override) is not int or idle_override < 0):
        raise SchemaValidationError("--idle-timeout-s must be an integer >= 0")

    client = _daemon_client(root, settings, idle_timeout_s=idle_override)
    action = args.daemon_action
    if action == "status":
        info = client.status()
        payload = {"ok": True, **info}
        if args.json:
            _print_json(payload)
        else:
            print(
                f"Daemon {info['daemon_id']}: state={info['state']}, "
                f"running={info['running']}, pid={info['pid']}"
            )
        return 0
    if action == "start":
        info = client.start(idle_timeout_s=idle_override)
        payload = {"ok": True, **info}
        if args.json:
            _print_json(payload)
        else:
            verb = "Reused" if info.get("reused") else "Started"
            print(f"{verb} daemon {info['daemon_id']} (pid={info['pid']})")
        return 0
    if action == "stop":
        info = client.stop()
        payload = {"ok": True, **info}
        if args.json:
            _print_json(payload)
        else:
            print(
                f"Stopped daemon {info['daemon_id']} (was_running={info['was_running']})"
            )
        return 0
    raise SchemaValidationError(f"unsupported daemon action {action!r}")


def _add_common_backend_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cds-lib",
        type=Path,
        metavar="PATH",
        help="path to Cadence cds.lib (local path, or remote path when --ssh-host is set)",
    )
    parser.add_argument(
        "--backend",
        choices=("virtuoso", "fixture"),
        help="OA bridge backend (virtuoso or fixture)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        metavar="DIR",
        help="local fixture snapshot root (for --backend fixture)",
    )
    parser.add_argument(
        "--virtuoso-cmd",
        metavar="CMD",
        help="Virtuoso executable command (default: virtuoso)",
    )
    parser.add_argument(
        "--ssh-host",
        metavar="HOST",
        help="SSH host alias from ~/.ssh/config for remote Virtuoso execution",
    )
    parser.add_argument(
        "--remote-workdir",
        metavar="DIR",
        help="remote temporary work directory for Virtuoso runs",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        metavar="SECONDS",
        help="backend command timeout in seconds",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oagit",
        description="Git-friendly semantic version control CLI for Cadence OpenAccess CellViews",
    )
    parser.add_argument(
        "-C",
        "--root",
        dest="root_override",
        type=Path,
        default=None,
        metavar="DIR",
        help="run as if oagit were started in DIR",
    )
    parser.add_argument(
        "--json",
        dest="root_json",
        action="store_true",
        default=False,
        help="emit machine-readable JSON output",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create or sync an oa-git project configuration")
    init.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="project root directory to initialize (default: current directory)",
    )
    init.add_argument(
        "--snapshots-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="directory for canonical CellView snapshots (default: snapshots)",
    )
    init.add_argument(
        "--cds-lib",
        type=Path,
        metavar="PATH",
        help="path to Cadence cds.lib (auto-detected from ./cds.lib if present)",
    )
    init.add_argument(
        "--oa-lib",
        action="append",
        nargs="+",
        default=None,
        metavar="LIB=DIR",
        help="map OA library name to local directory for dirty-index tracking",
    )
    init.add_argument(
        "--allow-library",
        action="append",
        nargs="+",
        default=None,
        metavar="LIB",
        help="allowlisted target OA library for restore operations",
    )
    init.add_argument(
        "--backend",
        choices=("virtuoso", "fixture"),
        default=None,
        help="OA bridge backend (default: virtuoso)",
    )
    init.add_argument(
        "--virtuoso-cmd",
        default=None,
        metavar="CMD",
        help="Virtuoso executable command (auto-detected from PATH/CDSHOME if omitted)",
    )
    init.add_argument(
        "--ssh-host",
        metavar="HOST",
        help="SSH host alias for remote Virtuoso execution",
    )
    init.add_argument(
        "--remote-workdir",
        metavar="DIR",
        help="remote temporary work directory on --ssh-host",
    )
    init.add_argument(
        "--timeout-s",
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"backend timeout in seconds (default: {DEFAULT_TIMEOUT_S})",
    )
    init.add_argument(
        "--daemon",
        dest="daemon",
        action="store_true",
        default=None,
        help="enable lazy headless Virtuoso daemon",
    )
    init.add_argument(
        "--no-daemon",
        dest="daemon",
        action="store_false",
        help="disable lazy headless Virtuoso daemon in v2 config",
    )
    init.add_argument(
        "--daemon-idle-timeout",
        "--idle-timeout-s",
        dest="daemon_idle_timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="daemon idle auto-exit timeout in seconds (default: 3600; 0 disables auto-exit)",
    )
    init.add_argument(
        "--daemon-startup-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="daemon startup wait timeout in seconds",
    )
    init.add_argument(
        "--daemon-poll-ms",
        type=int,
        default=None,
        metavar="MS",
        help="daemon spool polling interval in milliseconds",
    )
    init.add_argument(
        "--max-passes",
        type=int,
        default=None,
        metavar="N",
        help="maximum CDF callback convergence passes on restore",
    )
    init.add_argument(
        "--sync",
        action="store_true",
        help="idempotently refresh oa_libs, allow_libraries, and .gitignore from cds.lib",
    )
    init.add_argument(
        "--no-git",
        action="store_true",
        help="skip automatic git init and managed .gitignore updates",
    )
    init.add_argument(
        "--no-auto-allow",
        action="store_true",
        help="do not auto-populate allow_libraries from cds.lib",
    )
    init.add_argument(
        "--no-auto-discover",
        action="store_true",
        help="skip cds.lib and PATH Virtuoso auto-detection",
    )
    init.add_argument(
        "--v2",
        action="store_true",
        help="write v2 configuration at .oagit/config.json (default)",
    )
    init.add_argument(
        "--v1",
        action="store_true",
        help="write legacy v1 configuration at .oagit.json",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing oa-git configuration",
    )
    init.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    init.set_defaults(handler=_init)

    status = commands.add_parser(
        "status", help="verify snapshots, dirty index, OA locks, and Git state"
    )
    status.add_argument(
        "cellviews",
        nargs="*",
        metavar="LIB/CELL/VIEW",
        help="optional CellViews to inspect",
    )
    status.add_argument(
        "--changed",
        action="store_true",
        help="show only non-clean or broken CellViews",
    )
    status.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="explicit project root directory",
    )
    status.add_argument(
        "--snapshots-dir",
        type=Path,
        metavar="DIR",
        help="override snapshots directory",
    )
    status.add_argument(
        "--oa-lib-map",
        "--oa-lib",
        dest="oa_lib_map",
        action="append",
        nargs="+",
        default=None,
        metavar="LIB=DIR",
        help="extra OA library directory mapping(s) for status and lock probing",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    status.set_defaults(handler=_status)

    capture = commands.add_parser(
        "capture", help="capture one or more CellViews into canonical snapshots"
    )
    capture.add_argument(
        "cellviews",
        nargs="*",
        metavar="LIB/CELL/VIEW",
        help="CellViews to capture (omit to incrementally capture changed tracked views)",
    )
    capture.add_argument(
        "--view-type",
        choices=("auto", "schematic", "symbol", "layout"),
        default="auto",
        help="explicit OA view type for custom view names (default: auto)",
    )
    capture.add_argument(
        "--hier",
        action="store_true",
        help="recursively capture instantiated sub-cell schematics in a single session",
    )
    capture.add_argument(
        "--include-symbols",
        action="store_true",
        help="also capture companion symbol views alongside schematics",
    )
    capture.add_argument(
        "--changed",
        action="store_true",
        help="skip clean CellViews locally using .oagit/index.json fingerprints",
    )
    capture.add_argument(
        "--recompute-derived",
        action="store_true",
        help="refresh derived CDF observations during batch capture",
    )
    capture.add_argument(
        "--one-shot",
        action="store_true",
        help="bypass daemon and run a single one-shot Virtuoso invocation",
    )
    capture.add_argument(
        "--no-daemon",
        action="store_true",
        help="disable lazy daemon for this command",
    )
    capture.add_argument(
        "--max-depth",
        type=int,
        default=DEFAULT_MAX_HIER_DEPTH,
        metavar="N",
        help=f"maximum hierarchy traversal depth with --hier (default: {DEFAULT_MAX_HIER_DEPTH})",
    )
    capture.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="explicit project root directory",
    )
    capture.add_argument(
        "--snapshots-dir",
        type=Path,
        metavar="DIR",
        help="override snapshots directory",
    )
    capture.add_argument(
        "-o",
        "--out",
        type=Path,
        metavar="DIR",
        help="custom output directory for single-CellView capture",
    )
    _add_common_backend_options(capture)
    capture.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    capture.set_defaults(handler=_capture)

    restore = commands.add_parser(
        "restore", help="restore a snapshot safely into an allowlisted OA CellView"
    )
    restore.add_argument(
        "snapshot_dir",
        metavar="SNAPSHOT_OR_CELLVIEW",
        help="snapshot directory path or <lib>/<cell>/<view> shorthand",
    )
    restore.add_argument(
        "--target",
        metavar="LIB/CELL/VIEW",
        help="override target OA CellView (defaults to snapshot cellview)",
    )
    restore.add_argument(
        "--allow-library",
        action="append",
        nargs="+",
        default=None,
        metavar="LIB",
        help="additional allowlisted destination OA library",
    )
    restore.add_argument(
        "--overwrite",
        action="store_true",
        help="replace target OA CellView if it already exists",
    )
    restore.add_argument(
        "--recompute-derived",
        action="store_true",
        help="re-run PDK CDF callbacks to recompute derived parameters on restore",
    )
    restore.add_argument(
        "--one-shot",
        action="store_true",
        help="bypass daemon and run a single one-shot Virtuoso invocation",
    )
    restore.add_argument(
        "--no-daemon",
        action="store_true",
        help="disable lazy daemon for this command",
    )
    restore.add_argument(
        "--inline-readback",
        dest="inline_readback",
        action="store_true",
        default=None,
        help="perform readback verification inside the same Virtuoso session",
    )
    restore.add_argument(
        "--no-inline-readback",
        dest="inline_readback",
        action="store_false",
        help="run readback verification in a separate Virtuoso invocation",
    )
    restore.add_argument(
        "--skip-readback",
        action="store_true",
        help="skip post-restore OA readback verification",
    )
    restore.add_argument(
        "--skip-placement-check",
        action="store_true",
        help="skip placement geometry comparison during readback verification",
    )
    restore.add_argument(
        "--allow-omissions",
        action="store_true",
        help="allow restoring snapshots that recorded unsupported-object omissions",
    )
    restore.add_argument(
        "--oa-view-dir",
        type=Path,
        metavar="DIR",
        help="local OA view directory to probe for live .cdslck locks before restore",
    )
    restore.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="explicit project root directory",
    )
    _add_common_backend_options(restore)
    restore.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    restore.set_defaults(handler=_restore)

    diff = commands.add_parser(
        "diff", help="semantically compare local snapshots or Git revisions"
    )
    diff.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="0 args: HEAD vs working snapshots; 1 arg: REV or OLD..NEW; 2 args: OLD NEW (paths, lib/cell/view, or Git revs)",
    )
    diff.add_argument(
        "--electrical-only",
        action="store_true",
        help="show only electrical/connectivity/contract changes across schematic, symbol, and layout",
    )
    diff.add_argument(
        "--contract-only",
        action="store_true",
        help="show only symbol pin-contract changes",
    )
    diff.add_argument(
        "--connectivity-only",
        action="store_true",
        help="show only layout connectivity/device changes (also enabled by --electrical-only)",
    )
    diff.add_argument(
        "--stat",
        action="store_true",
        help="print compact summary statistics per changed CellView",
    )
    diff.add_argument(
        "--cellview",
        metavar="LIB/CELL/VIEW",
        help="limit Git revision diff to a specific CellView",
    )
    diff.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="explicit project or Git root directory",
    )
    diff.add_argument(
        "--snapshots-dir",
        type=Path,
        metavar="DIR",
        help="override snapshots directory",
    )
    diff.add_argument(
        "--exit-code",
        action="store_true",
        help="exit with code 1 when differences are found, 0 otherwise",
    )
    diff.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    diff.set_defaults(handler=_diff)

    merge = commands.add_parser(
        "merge", help="domain-aware three-way merge of snapshots"
    )
    merge.add_argument(
        "base_dir",
        metavar="BASE",
        help="common ancestor snapshot directory or <lib>/<cell>/<view> shorthand",
    )
    merge.add_argument(
        "ours_dir",
        metavar="OURS",
        help="local branch snapshot directory or <lib>/<cell>/<view> shorthand",
    )
    merge.add_argument(
        "theirs_dir",
        metavar="THEIRS",
        help="incoming branch snapshot directory or <lib>/<cell>/<view> shorthand",
    )
    merge.add_argument(
        "-o",
        "--out",
        dest="out_dir",
        type=Path,
        required=True,
        metavar="DIR",
        help="destination directory for merged snapshot (written only when conflict-free)",
    )
    merge.add_argument(
        "--allow-spatial-overlap",
        action="store_true",
        help="allow cross-branch overlapping mask shapes/vias when merging layout snapshots",
    )
    merge.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="explicit project root directory",
    )
    merge.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    merge.set_defaults(handler=_merge)

    daemon = commands.add_parser(
        "daemon", help="manage lazy headless Virtuoso daemon"
    )
    daemon.add_argument(
        "daemon_action",
        nargs="?",
        choices=("start", "stop", "status"),
        default="status",
        help="daemon action: start, stop, or status (default: status)",
    )
    daemon.add_argument(
        "--idle-timeout-s",
        type=int,
        default=None,
        metavar="SECONDS",
        help="override daemon idle auto-exit timeout in seconds (default: 3600; 0 disables auto-exit)",
    )
    daemon.add_argument(
        "-C",
        "--root",
        type=Path,
        default=None,
        metavar="DIR",
        help="explicit project root directory",
    )
    _add_common_backend_options(daemon)
    daemon.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON output",
    )
    daemon.set_defaults(handler=_daemon)
    return parser


def _error_hint(error: Exception) -> str | None:
    msg = str(error)
    msg_lower = msg.lower()
    if isinstance(error, DisallowedLibraryError):
        match = re.search(r"library\s+'([^']+)'|library\s+\"([^\"]+)\"", msg, re.IGNORECASE)
        lib = (match.group(1) or match.group(2)) if match else "<lib>"
        return (
            f"pass --allow-library {lib} or add {lib!r} to allow_libraries in .oagit/config.json"
        )
    if isinstance(error, LiveLockError):
        return "close the OA CellView in Virtuoso and wait for active .cdslck/.lck locks to clear"
    if isinstance(error, UnsupportedObjectError):
        return "inspect omissions.json in the snapshot directory or pass --allow-omissions"
    if isinstance(error, CdfParameterError):
        if "cdf_order_cycle" in msg_lower:
            return "fix cyclic dependencies under cdf.coupling_rules in .oagit/config.json"
        return "pass --recompute-derived if primary CDF parameters were merged or edited"
    if isinstance(error, BridgeExecutionError) and (
        "already exists" in msg_lower
        or "target_exists" in msg_lower
        or "overwrite" in msg_lower
    ):
        return "pass --overwrite to replace the existing target OA CellView"
    if isinstance(error, SchemaValidationError):
        if "oagit init" in msg_lower and "already exists" not in msg_lower:
            return "run 'oagit init' in your project root"
        if "already exists" in msg_lower and "--force" in msg_lower:
            return "pass --sync to refresh cds.lib/.gitignore or --force to overwrite"
    return None


def _emit_error(error: Exception, *, json_mode: bool) -> None:
    hint = _error_hint(error)
    if json_mode:
        payload: dict[str, Any] = {
            "ok": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
        if hint:
            payload["hint"] = hint
        _print_json(payload)
    else:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        if hint:
            print(f"Hint: {hint}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``oagit`` and return its documented process exit code."""

    parser = _build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_argv
    parse_error = io.StringIO()
    try:
        if json_requested:
            with redirect_stderr(parse_error):
                args = parser.parse_args(raw_argv)
        else:
            args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        if json_requested and exc.code:
            _print_json(
                {
                    "ok": False,
                    "error_type": "ArgumentError",
                    "error": parse_error.getvalue().strip() or "argument parsing failed",
                }
            )
        return int(exc.code or 0)
    args.json = bool(getattr(args, "json", False) or getattr(args, "root_json", False))
    if getattr(args, "root", None) is None and getattr(args, "root_override", None) is not None:
        args.root = args.root_override
    json_mode = args.json
    try:
        return int(args.handler(args))
    except OaGitError as exc:
        _emit_error(exc, json_mode=json_mode)
        return 2
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        error = BridgeExecutionError(str(exc))
        _emit_error(error, json_mode=json_mode)
        return 2


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
