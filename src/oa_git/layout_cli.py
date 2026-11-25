"""Standalone Git-like CLI for OpenAccess layout workflows (``python -m oa_git.layout_cli``).

Exit codes:
  0 - Success (and for ``diff --exit-code``, no changes detected)
  1 - Integrity failure, live lock block, merge conflict, or ``diff --exit-code`` changes
  2 - Usage error, guard violation, or runtime/backend error
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
import subprocess
import sys
import tempfile
from typing import Any

from .guards import probe_locks
from .layout_backend import LayoutHandler, load_layout_backend
from .layout_model import (
    LAYOUT_VIEW_TYPE,
    BridgeExecutionError,
    CellViewRef,
    LiveLockError,
    OaGitError,
    SchemaValidationError,
    SnapshotIntegrityError,
)
from .layout_snapshot import (
    decode_definitions_jsonl,
    layout_snapshot_dir_for,
    read_layout_snapshot,
    verify_layout_snapshot_integrity,
    write_layout_snapshot,
)
from .model import CONFIG_SCHEMA_V2
from .project_state import (
    DEFAULT_DAEMON_CONFIG,
    IndexEntry,
    classify_cellview_state,
    discover_project_root,
    fingerprint_source_files,
    load_index,
    load_project_config,
    update_index_entry,
)
from .view_contract import OaSession

DEFAULT_SNAPSHOTS_DIR = "snapshots"
DEFAULT_BACKEND = "virtuoso"
DEFAULT_TIMEOUT_S = 180

_ALLOWED_LAYOUT_SNAPSHOT_FILES = frozenset(
    {"objects.jsonl", "definitions.jsonl", "omissions.json"}
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _print_json(value: Any) -> None:
    sys.stdout.write(_json_dump(value))


def _flatten_option(values: Sequence[Sequence[str]] | None) -> list[str]:
    flattened: list[str] = []
    for group in values or ():
        flattened.extend(group)
    return flattened


def _unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _resolve_local_path(value: Path | str) -> Path:
    return Path(value).expanduser().resolve()


def _resolve_under_root(root: Path, value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _resolve_cli_input_path(
    value: Path | str, root: Path, *, explicit_root: bool
) -> Path:
    """Resolve an explicit CLI input path (cwd-first unless ``--root`` was specified)."""

    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    if explicit_root:
        candidate = root / raw
        if candidate.exists():
            return candidate.resolve()
        return candidate.resolve()
    if raw.exists():
        return raw.resolve()
    candidate = root / raw
    if candidate.exists():
        return candidate.resolve()
    return raw.resolve()


def _resolve_cli_output_path(
    value: Path | str, root: Path, *, explicit_root: bool
) -> Path:
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    if explicit_root:
        return (root / raw).resolve()
    return raw.resolve()


def _relative_posix(root: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(target)


def _parse_library_map(value: str, *, flag: str = "--oa-lib-map") -> tuple[str, Path]:
    library, separator, directory = value.partition("=")
    if not separator or not library.strip() or not directory.strip():
        raise SchemaValidationError(f"expected LIB=DIR for {flag}, got {value!r}")
    return library.strip(), Path(directory.strip())


def _configured_libraries(config: Mapping[str, Any]) -> list[str]:
    values = config.get("allow_libraries", [])
    if isinstance(values, str):
        return [values]
    return list(values or ())


def _discover_project_and_config(
    explicit_root: Path | str | None,
    *,
    allow_missing: bool = True,
    fallback_to_git: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Discover project root upward (.oagit/config.json v2 -> .oagit.json v1 -> fallback)."""

    explicit = None if explicit_root is None else Path(explicit_root)
    try:
        root, _cfg_path, _schema = discover_project_root(
            start=Path.cwd(),
            explicit_root=explicit,
        )
    except SchemaValidationError:
        if not allow_missing:
            raise
        if explicit_root is not None:
            return _resolve_local_path(explicit_root), {}
        cwd = Path.cwd().resolve()
        if fallback_to_git:
            try:
                return _git_toplevel(cwd), {}
            except BridgeExecutionError:
                return cwd, {}
        return cwd, {}
    return root, load_project_config(root)


def _validate_timeout(timeout_s: int) -> int:
    if type(timeout_s) is not int or timeout_s <= 0:
        raise SchemaValidationError("timeout_s must be a positive integer")
    return timeout_s


def _resolve_settings(
    args: argparse.Namespace,
    root: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge explicit CLI overrides > discovered project config > defaults."""

    explicit_root = getattr(args, "root", None) is not None

    cli_backend = getattr(args, "backend", None)
    backend = cli_backend if cli_backend is not None else config.get("backend", DEFAULT_BACKEND)
    if backend not in {"fixture", "virtuoso"}:
        raise SchemaValidationError(f"unsupported backend {backend!r}")

    cli_timeout = getattr(args, "timeout_s", None)
    timeout_raw = cli_timeout if cli_timeout is not None else config.get("timeout_s", DEFAULT_TIMEOUT_S)
    timeout_s = _validate_timeout(timeout_raw)

    cli_ssh_host = getattr(args, "ssh_host", None)
    ssh_host = cli_ssh_host if cli_ssh_host is not None else config.get("ssh_host", None)

    cli_virtuoso_cmd = getattr(args, "virtuoso_cmd", None)
    virtuoso_cmd = (
        cli_virtuoso_cmd if cli_virtuoso_cmd is not None else config.get("virtuoso_cmd", None)
    )

    cli_remote_workdir = getattr(args, "remote_workdir", None)
    remote_workdir = (
        cli_remote_workdir
        if cli_remote_workdir is not None
        else config.get("remote_workdir", None)
    )

    cli_snapshots_dir = getattr(args, "snapshots_dir", None)
    if cli_snapshots_dir is not None:
        snapshots_dir = _resolve_cli_output_path(
            cli_snapshots_dir, root, explicit_root=explicit_root
        )
    else:
        snapshots_dir = _resolve_under_root(
            root, config.get("snapshots_dir", DEFAULT_SNAPSHOTS_DIR)
        )

    cli_cds_lib = getattr(args, "cds_lib", None)
    cds_lib_raw = cli_cds_lib if cli_cds_lib is not None else config.get("cds_lib", None)
    if cds_lib_raw is None:
        cds_lib: Path | str | None = None
    elif backend == "virtuoso" and ssh_host:
        cds_lib = str(cds_lib_raw)
    elif cli_cds_lib is not None:
        cds_lib = _resolve_cli_input_path(cli_cds_lib, root, explicit_root=explicit_root)
    else:
        cds_lib = _resolve_under_root(root, cds_lib_raw)

    cli_fixture = getattr(args, "fixture", None)
    fixture = (
        _resolve_cli_input_path(cli_fixture, root, explicit_root=explicit_root)
        if cli_fixture is not None
        else None
    )

    oa_libs: dict[str, Path] = {
        str(lib): _resolve_under_root(root, lib_dir)
        for lib, lib_dir in sorted((config.get("oa_libs") or {}).items())
    }

    daemon_cfg = dict(DEFAULT_DAEMON_CONFIG)
    if isinstance(config.get("daemon"), Mapping):
        daemon_cfg.update(config["daemon"])
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

    return {
        "root": root,
        "explicit_root": explicit_root,
        "snapshots_dir": snapshots_dir,
        "cds_lib": cds_lib,
        "oa_libs": oa_libs,
        "allow_libraries": _configured_libraries(config),
        "backend": backend,
        "fixture": fixture,
        "virtuoso_cmd": virtuoso_cmd,
        "ssh_host": ssh_host,
        "remote_workdir": remote_workdir,
        "timeout_s": timeout_s,
        "daemon_enabled": daemon_enabled,
        "daemon_idle_timeout_s": int(daemon_cfg.get("idle_timeout_s", 3600)),
        "daemon_startup_timeout_s": int(daemon_cfg.get("startup_timeout_s", 30)),
        "daemon_poll_ms": int(daemon_cfg.get("poll_ms", 100)),
        "force_one_shot": one_shot or no_daemon,
    }


def _build_backend(settings: Mapping[str, Any]):
    timeout_s = _validate_timeout(settings["timeout_s"])
    skill_runner = None
    if (
        settings["backend"] == "virtuoso"
        and settings.get("daemon_enabled", False)
        and settings.get("cds_lib") is not None
    ):
        from .virtuoso_backend import VirtuosoSkillBackend

        skill_runner = VirtuosoSkillBackend(
            virtuoso_cmd=settings.get("virtuoso_cmd"),
            ssh_host=settings.get("ssh_host"),
            remote_workdir=settings.get("remote_workdir"),
            timeout_s=timeout_s,
            daemon_enabled=True,
            daemon_idle_timeout_s=int(settings.get("daemon_idle_timeout_s", 3600)),
            daemon_startup_timeout_s=int(settings.get("daemon_startup_timeout_s", 30)),
            daemon_poll_ms=int(settings.get("daemon_poll_ms", 100)),
            project_root=settings.get("root"),
        )
    return load_layout_backend(
        settings["backend"],
        fixture_path=settings.get("fixture"),
        virtuoso_cmd=settings.get("virtuoso_cmd"),
        ssh_host=settings.get("ssh_host"),
        remote_workdir=settings.get("remote_workdir"),
        timeout_s=timeout_s,
        skill_runner=skill_runner,
    )


def _call_backend_capture(
    backend: Any,
    cellview: CellViewRef,
    *,
    cds_lib: Path | str | None,
    session: OaSession | None,
):
    kwargs: dict[str, Any] = {"cds_lib": cds_lib}
    try:
        sig = inspect.signature(backend.capture_layout)
        if "session" in sig.parameters:
            kwargs["session"] = session
    except (TypeError, ValueError):
        pass
    return backend.capture_layout(cellview, **kwargs)


def _call_backend_restore(
    backend: Any,
    snapshot: Any,
    target: CellViewRef,
    *,
    cds_lib: Path | str | None,
    allow_libraries: Sequence[str],
    overwrite: bool,
    allow_omissions: bool,
    oa_view_dir: Path | None,
    verify_readback: bool,
    session: OaSession | None,
):
    kwargs: dict[str, Any] = {
        "cds_lib": cds_lib,
        "allow_libraries": allow_libraries,
        "overwrite": overwrite,
        "allow_omissions": allow_omissions,
        "oa_view_dir": oa_view_dir,
        "verify_readback": verify_readback,
    }
    try:
        sig = inspect.signature(backend.restore_layout)
        if "session" in sig.parameters:
            kwargs["session"] = session
    except (TypeError, ValueError):
        pass
    return backend.restore_layout(snapshot, target, **kwargs)


# ---------------------------------------------------------------------------
# Layout Discovery & Dirty-Index Helpers
# ---------------------------------------------------------------------------


def _peek_manifest_raw(snapshot_dir: Path) -> dict[str, Any] | None:
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_layout_index_entry(
    root: Path,
    snapshots_dir: Path,
    entry: IndexEntry,
) -> bool:
    candidates: list[Path] = []
    if entry.snapshot:
        candidates.append(_resolve_under_root(root, entry.snapshot))
    candidates.append(layout_snapshot_dir_for(snapshots_dir, entry.cellview))
    for snap_dir in candidates:
        raw = _peek_manifest_raw(snap_dir)
        if raw is not None and "view_type" in raw:
            return raw.get("view_type") == LAYOUT_VIEW_TYPE
    if entry.cellview.view in {"schematic", "symbol"}:
        return False
    if any(fp.path.endswith("/layout.oa") for fp in entry.source_files):
        return True
    return entry.cellview.view == LAYOUT_VIEW_TYPE or "layout" in entry.cellview.view.lower()


def _discover_layout_snapshot_dirs(snapshots_dir: Path) -> list[tuple[Path, CellViewRef | None]]:
    """Find snapshot directories under ``snapshots_dir`` belonging to ``view_type=='layout'``."""

    if not snapshots_dir.is_dir():
        return []
    found: list[tuple[Path, CellViewRef | None]] = []
    manifest_dirs = sorted(
        {m.parent for m in snapshots_dir.rglob("manifest.json") if m.is_file()},
        key=str,
    )
    for snap_dir in manifest_dirs:
        raw = _peek_manifest_raw(snap_dir)
        if raw is not None:
            view_type = raw.get("view_type")
            if view_type is not None and view_type != LAYOUT_VIEW_TYPE:
                continue
            if view_type != LAYOUT_VIEW_TYPE:
                # Legacy schematic v1 manifests have no view_type; skip non-layout directories.
                if "layout" not in snap_dir.name.lower():
                    continue
            cv_raw = raw.get("cellview")
            cv: CellViewRef | None = None
            if isinstance(cv_raw, Mapping):
                try:
                    cv = CellViewRef.from_dict(dict(cv_raw))
                except SchemaValidationError:
                    cv = None
            found.append((snap_dir, cv))
        else:
            # Malformed manifest.json: include if path looks like a layout snapshot dir.
            if "layout" in snap_dir.name.lower():
                cv = None
                try:
                    rel = snap_dir.relative_to(snapshots_dir)
                    if len(rel.parts) == 3:
                        cv = CellViewRef(rel.parts[0], rel.parts[1], rel.parts[2])
                except (ValueError, SchemaValidationError):
                    cv = None
                found.append((snap_dir, cv))
    return found


def _record_layout_index_after_capture(
    root: Path,
    *,
    config: Mapping[str, Any],
    oa_libs: Mapping[str, Path],
    snapshot: Any,
    destination: Path,
    handler: LayoutHandler,
) -> None:
    if not oa_libs and config.get("schema") != CONFIG_SCHEMA_V2 and not (root / ".oagit").is_dir():
        return
    cv: CellViewRef = snapshot.manifest.cellview
    lib_root = oa_libs.get(cv.library)
    if lib_root is not None and lib_root.is_dir():
        candidates = handler.source_file_candidates(cv.cell, cv.view)
        prev_entries = load_index(root)
        prev_entry = prev_entries.get(cv.key)
        fps = fingerprint_source_files(
            lib_root,
            candidates,
            previous=prev_entry.source_files if prev_entry is not None else (),
        )
        state = "clean" if (fps and fps[0].exists) else "unknown"
    else:
        fps = ()
        state = "unknown"
    entry = IndexEntry(
        cellview=cv,
        source_files=fps,
        dependencies=(),
        snapshot=_relative_posix(root, destination),
        snapshot_full_sha256=snapshot.manifest.full_sha256,
        state=state,
    )
    update_index_entry(root, entry)


# ---------------------------------------------------------------------------
# Git Revision Extraction Helpers (for `diff`)
# ---------------------------------------------------------------------------


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


def _validate_git_revision(revision: str) -> str:
    if not isinstance(revision, str) or not revision:
        raise SchemaValidationError("Git revision must be a non-empty string")
    if revision.startswith("-") or any(
        ord(char) < 32 or ord(char) == 127 for char in revision
    ):
        raise SchemaValidationError(f"unsafe Git revision {revision!r}")
    return revision


def _verify_git_revision(git_root: Path, revision: str) -> str:
    revision = _validate_git_revision(revision)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(git_root),
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


def _git_show(git_root: Path, revision_path: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "show", "--end-of-options", revision_path],
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


def _git_layout_snapshot(
    root: Path,
    snapshots_dir: Path,
    revision: str,
    cellview: CellViewRef,
    stack: ExitStack,
):
    git_root = _git_toplevel(root)
    revision = _verify_git_revision(git_root, revision)
    rel_raw = os.path.relpath(layout_snapshot_dir_for(snapshots_dir, cellview), git_root)
    rel_posix = Path(rel_raw).as_posix()
    if rel_posix == ".":
        rel_posix = ""
    if rel_posix == ".." or rel_posix.startswith("../") or "/../" in rel_posix:
        raise SchemaValidationError(
            f"snapshots_dir {snapshots_dir} is outside Git repository {git_root}"
        )

    manifest_suffix = f"{rel_posix}/manifest.json" if rel_posix else "manifest.json"
    manifest_bytes = _git_show(git_root, f"{revision}:{manifest_suffix}")
    try:
        manifest_data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(
            f"Git revision {revision!r} contains an invalid manifest.json"
        ) from exc
    if not isinstance(manifest_data, Mapping):
        raise SchemaValidationError(
            f"Git revision {revision!r} manifest.json must contain a JSON object"
        )
    view_type = manifest_data.get("view_type")
    if view_type != LAYOUT_VIEW_TYPE:
        raise SchemaValidationError(
            f"Git revision {revision!r} snapshot has view_type={view_type!r}; expected 'layout'"
        )
    manifest_files = manifest_data.get("files")
    if not isinstance(manifest_files, Mapping) or "objects.jsonl" not in manifest_files:
        raise SchemaValidationError(
            f"Git revision {revision!r} layout manifest.files must include 'objects.jsonl'"
        )

    temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="oagit-layout-diff-")))
    (temporary / "manifest.json").write_bytes(manifest_bytes)

    for name in sorted(manifest_files):
        if (
            not isinstance(name, str)
            or "/" in name
            or "\\" in name
            or name in {".", ".."}
            or name not in _ALLOWED_LAYOUT_SNAPSHOT_FILES
        ):
            raise SchemaValidationError(
                f"unsafe or unsupported manifest file name in Git revision: {name!r}"
            )
        suffix = f"{rel_posix}/{name}" if rel_posix else name
        (temporary / name).write_bytes(_git_show(git_root, f"{revision}:{suffix}"))

    snapshot = read_layout_snapshot(temporary, verify=True)
    if snapshot.manifest.cellview != cellview:
        raise SchemaValidationError(
            f"Git revision {revision!r} snapshot cellview {snapshot.manifest.cellview.key!r} "
            f"does not match {cellview.key!r}"
        )
    return snapshot


# ---------------------------------------------------------------------------
# Subcommand Handlers
# ---------------------------------------------------------------------------


def _cmd_capture(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root, allow_missing=True)
    settings = _resolve_settings(args, root, config)
    snapshots_dir: Path = settings["snapshots_dir"]
    oa_libs: dict[str, Path] = dict(settings["oa_libs"])
    index_entries = load_index(root)
    handler = LayoutHandler()

    raw_specs: list[str] = list(getattr(args, "cellviews", None) or ())
    changed_only = bool(getattr(args, "changed", False))

    if raw_specs:
        requested_cvs = [CellViewRef.parse(spec) for spec in raw_specs]
    elif changed_only:
        tracked: dict[str, CellViewRef] = {}
        for key, idx_entry in sorted(index_entries.items()):
            if _is_layout_index_entry(root, snapshots_dir, idx_entry):
                tracked[key] = idx_entry.cellview
        for _snap_dir, snap_cv in _discover_layout_snapshot_dirs(snapshots_dir):
            if snap_cv is not None:
                tracked.setdefault(snap_cv.key, snap_cv)
        requested_cvs = [tracked[key] for key in sorted(tracked)]
        if not requested_cvs:
            raise SchemaValidationError(
                "no CELLVIEW specified and no tracked layout entries found in "
                ".oagit/index.json or snapshots_dir"
            )
    else:
        raise SchemaValidationError(
            "at least one CELLVIEW is required (or pass --changed to capture dirty tracked layouts)"
        )

    if args.out is not None and len(requested_cvs) != 1:
        raise SchemaValidationError("--out can only be used when capturing a single CellView")

    skipped_records: list[dict[str, Any]] = []
    dirty_targets: list[CellViewRef] = []

    if changed_only:
        for cv in requested_cvs:
            canonical_dest = layout_snapshot_dir_for(snapshots_dir, cv)
            custom_out = (
                _resolve_cli_output_path(args.out, root, explicit_root=settings["explicit_root"])
                if args.out is not None
                else canonical_dest
            )
            if custom_out == canonical_dest:
                candidates = handler.source_file_candidates(cv.cell, cv.view)
                state, _fps = classify_cellview_state(
                    cv,
                    oa_libs=oa_libs,
                    snapshots_dir=snapshots_dir,
                    index_entries=index_entries,
                    candidates=candidates,
                    verify_snapshot_fn=verify_layout_snapshot_integrity,
                )
            else:
                state = "dirty"
            if state == "clean":
                skipped_records.append(
                    {
                        "cellview": cv.to_dict(),
                        "key": cv.key,
                        "reason": "clean",
                        "snapshot_dir": str(canonical_dest),
                    }
                )
            else:
                dirty_targets.append(cv)
    else:
        dirty_targets = list(requested_cvs)

    # Zero backend / Virtuoso calls when all requested targets are clean.
    if not dirty_targets:
        result: dict[str, Any] = {
            "ok": True,
            "captured": [],
            "skipped": skipped_records,
            "transport": "index_skip",
        }
        if args.json:
            _print_json(result)
        else:
            for rec in skipped_records:
                print(f"Skipped (clean): {rec['key']}")
        return 0

    backend = _build_backend(settings)
    session = (
        OaSession(session_id="layout-cli-oneshot", transport="one_shot")
        if settings["force_one_shot"]
        else None
    )

    captured_records: list[dict[str, Any]] = []
    for cv in dirty_targets:
        snapshot = _call_backend_capture(
            backend, cv, cds_lib=settings["cds_lib"], session=session
        )
        destination = (
            _resolve_cli_output_path(args.out, root, explicit_root=settings["explicit_root"])
            if args.out is not None
            else layout_snapshot_dir_for(snapshots_dir, cv)
        )
        write_layout_snapshot(snapshot, destination)
        _record_layout_index_after_capture(
            root,
            config=config,
            oa_libs=oa_libs,
            snapshot=snapshot,
            destination=destination,
            handler=handler,
        )
        captured_records.append(
            {
                "snapshot_dir": str(destination),
                "schema": snapshot.manifest.schema,
                "view_type": snapshot.manifest.view_type,
                "cellview": snapshot.manifest.cellview.to_dict(),
                "content_sha256": snapshot.manifest.content_sha256,
                "full_sha256": snapshot.manifest.full_sha256,
                "object_count": len(snapshot.objects),
                "omissions_count": snapshot.manifest.omissions_count,
            }
        )

    transport = getattr(backend, "last_transport", None) or (
        "fixture" if settings["backend"] == "fixture" else "one_shot"
    )
    result = {
        "ok": True,
        "captured": captured_records,
        "skipped": skipped_records,
        "transport": transport,
    }
    if len(captured_records) == 1:
        result.update(captured_records[0])

    if args.json:
        _print_json(result)
    else:
        for sk in skipped_records:
            print(f"Skipped (clean): {sk['key']}")
        for rec in captured_records:
            cv_d = rec["cellview"]
            cv_key = f"{cv_d['library']}/{cv_d['cell']}/{cv_d['view']}"
            print(f"Captured layout {cv_key} to {rec['snapshot_dir']}")
            print(f"Content SHA-256: {rec['content_sha256']}")
            print(f"Full SHA-256: {rec['full_sha256']}")
            print(f"Objects: {rec['object_count']}, Omissions: {rec['omissions_count']}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root, allow_missing=True)
    settings = _resolve_settings(args, root, config)
    snapshot_dir = _resolve_cli_input_path(
        args.snapshot_dir, root, explicit_root=settings["explicit_root"]
    )
    snapshot = read_layout_snapshot(snapshot_dir, verify=True)
    target = (
        snapshot.manifest.cellview
        if args.target is None
        else CellViewRef.parse(args.target)
    )
    allow_libraries = _unique(
        list(settings["allow_libraries"]) + _flatten_option(args.allow_library)
    )
    if args.oa_view_dir is not None:
        oa_view_dir: Path | None = _resolve_cli_input_path(
            args.oa_view_dir, root, explicit_root=settings["explicit_root"]
        )
    else:
        lib_dir = settings["oa_libs"].get(target.library)
        oa_view_dir = (
            lib_dir / target.cell / target.view
            if (lib_dir is not None and lib_dir.is_dir())
            else None
        )

    backend = _build_backend(settings)
    session = (
        OaSession(session_id="layout-cli-oneshot", transport="one_shot")
        if settings["force_one_shot"]
        else None
    )
    receipt = _call_backend_restore(
        backend,
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
    result = receipt.to_dict()
    result["ok"] = True
    if args.json:
        _print_json(result)
    else:
        print(f"Restored layout {receipt.target.key}")
        print(f"Published: {receipt.published}")
        print(f"PCell verified: {receipt.pcell_verified}")
        print(f"Connectivity verified: {receipt.connectivity_verified}")
        print(f"Readback verified: {receipt.readback_verified}")
        if receipt.transport != "one_shot":
            print(f"Transport: {receipt.transport}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    root, config = _discover_project_and_config(args.root, allow_missing=True)
    settings = _resolve_settings(args, root, config)
    snapshots_dir: Path = settings["snapshots_dir"]
    oa_libs: dict[str, Path] = dict(settings["oa_libs"])
    index_entries = load_index(root)
    handler = LayoutHandler()

    requested_cvs = [CellViewRef.parse(spec) for spec in (getattr(args, "cellviews", None) or ())]
    requested_keys = {cv.key for cv in requested_cvs}

    cli_lib_maps: list[tuple[str, Path]] = []
    for raw_mapping in _flatten_option(getattr(args, "oa_lib_map", None)):
        library, raw_directory = _parse_library_map(raw_mapping)
        resolved_dir = _resolve_cli_input_path(
            raw_directory, root, explicit_root=settings["explicit_root"]
        )
        cli_lib_maps.append((library, resolved_dir))
        oa_libs[library] = resolved_dir

    # Discover layout snapshots and tracked index entries.
    discovered_snaps: dict[str, Path] = {}
    orphan_snap_dirs: list[Path] = []
    for snap_dir, snap_cv in _discover_layout_snapshot_dirs(snapshots_dir):
        if snap_cv is not None:
            discovered_snaps[snap_cv.key] = snap_dir
        else:
            orphan_snap_dirs.append(snap_dir)

    tracked_cvs: dict[str, CellViewRef] = {}
    if requested_cvs:
        for cv in requested_cvs:
            tracked_cvs[cv.key] = cv
    else:
        for key, snap_dir in sorted(discovered_snaps.items()):
            tracked_cvs[key] = CellViewRef.parse(key)
        for key, idx_entry in sorted(index_entries.items()):
            if _is_layout_index_entry(root, snapshots_dir, idx_entry):
                tracked_cvs.setdefault(key, idx_entry.cellview)

    layout_entries: list[dict[str, Any]] = []
    for key in sorted(tracked_cvs):
        cv = tracked_cvs[key]
        snap_dir = discovered_snaps.get(key) or layout_snapshot_dir_for(snapshots_dir, cv)
        snap_exists = snap_dir.is_dir() and (snap_dir / "manifest.json").is_file()

        integrity_ok = True
        object_count: int | None = None
        omissions_count: int | None = None
        error_type: str | None = None
        error_msg: str | None = None

        if snap_exists:
            try:
                manifest = verify_layout_snapshot_integrity(snap_dir)
                snapshot = read_layout_snapshot(snap_dir, verify=False)
            except (SnapshotIntegrityError, SchemaValidationError) as exc:
                integrity_ok = False
                error_type = type(exc).__name__
                error_msg = str(exc)
            else:
                object_count = len(snapshot.objects)
                omissions_count = manifest.omissions_count

        candidates = handler.source_file_candidates(cv.cell, cv.view)
        index_state, _fps = classify_cellview_state(
            cv,
            oa_libs=oa_libs,
            snapshots_dir=snapshots_dir,
            index_entries=index_entries,
            candidates=candidates,
            verify_snapshot_fn=verify_layout_snapshot_integrity,
        )

        locked = False
        live_locks = 0
        lock_files: list[str] = []
        lib_dir = oa_libs.get(cv.library)
        if lib_dir is not None and lib_dir.is_dir():
            view_dir = lib_dir / cv.cell / cv.view
            try:
                lock_report = probe_locks(view_dir)
            except LiveLockError as exc:
                locked = True
                live_locks = 1
                error_type = error_type or type(exc).__name__
                error_msg = error_msg or str(exc)
            else:
                if lock_report.has_locks:
                    locked = True
                    lock_files = [
                        str(p) for p in (*lock_report.cdslck_files, *lock_report.lck_files)
                    ]
                    live_locks = len(lock_files)

        if not integrity_ok:
            status_label = "FAILED"
        elif locked:
            status_label = "LOCKED"
        else:
            status_label = index_state

        entry: dict[str, Any] = {
            "key": cv.key,
            "cellview": cv.to_dict(),
            "snapshot_dir": str(snap_dir),
            "snapshot_exists": snap_exists,
            "ok": integrity_ok and not locked,
            "integrity_ok": integrity_ok,
            "status": status_label,
            "index_state": index_state,
            "object_count": object_count,
            "omissions_count": omissions_count,
            "has_omissions": (omissions_count > 0) if omissions_count is not None else None,
            "locked": locked,
            "live_locks": live_locks,
            "lock_files": lock_files,
        }
        if error_type is not None:
            entry["error_type"] = error_type
            entry["error"] = error_msg
        layout_entries.append(entry)

    # Also include malformed layout snapshot directories whose cellview could not be parsed.
    if not requested_keys:
        for orphan_dir in orphan_snap_dirs:
            try:
                verify_layout_snapshot_integrity(orphan_dir)
            except (SnapshotIntegrityError, SchemaValidationError) as exc:
                layout_entries.append(
                    {
                        "key": _relative_posix(snapshots_dir, orphan_dir),
                        "cellview": None,
                        "snapshot_dir": str(orphan_dir),
                        "snapshot_exists": True,
                        "ok": False,
                        "integrity_ok": False,
                        "status": "FAILED",
                        "index_state": "unknown",
                        "object_count": None,
                        "omissions_count": None,
                        "has_omissions": None,
                        "locked": False,
                        "live_locks": 0,
                        "lock_files": [],
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

    # Library-level lock probes (for mapped OA libs when checking whole project or explicit --oa-lib-map).
    lock_entries: list[dict[str, Any]] = []
    if not requested_cvs:
        probed: set[tuple[str, str]] = set()
        lock_targets: list[tuple[str, Path]] = list(cli_lib_maps)
        for lib_name, lib_dir in sorted(oa_libs.items()):
            if (lib_name, str(lib_dir)) not in {(l, str(d)) for l, d in lock_targets}:
                if lib_dir.exists():
                    lock_targets.append((lib_name, lib_dir))
        for library, directory in lock_targets:
            pair = (library, str(directory))
            if pair in probed:
                continue
            probed.add(pair)
            try:
                report = probe_locks(directory)
            except LiveLockError as exc:
                lock_entries.append(
                    {
                        "library": library,
                        "directory": str(directory),
                        "blocking": True,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            else:
                lock_entries.append(
                    {
                        "library": library,
                        "directory": str(directory),
                        "report": report.to_dict(),
                        "blocking": report.has_locks,
                    }
                )

    if bool(getattr(args, "changed", False)):
        displayed_entries = [e for e in layout_entries if e["status"] != "clean"]
    else:
        displayed_entries = layout_entries

    ok = all(e["integrity_ok"] and not e["locked"] for e in layout_entries) and not any(
        entry["blocking"] for entry in lock_entries
    )
    payload: dict[str, Any] = {
        "ok": ok,
        "root": str(root),
        "snapshots_dir": str(snapshots_dir),
        "layouts": displayed_entries,
        "locks": lock_entries,
    }

    if args.json:
        _print_json(payload)
    else:
        if not displayed_entries:
            print(
                "No changed layouts."
                if bool(getattr(args, "changed", False))
                else "No layout snapshots found."
            )
        else:
            col_w = max(len(e["key"]) for e in displayed_entries) + 2
            for entry in displayed_entries:
                status_tag = entry["status"]
                key = entry["key"]
                if status_tag == "FAILED":
                    detail = f"(integrity error: {entry.get('error', 'verification failed')})"
                elif status_tag == "LOCKED":
                    n = entry["live_locks"]
                    noun = "live lock" if n == 1 else "live locks"
                    detail = f"({n} {noun})"
                elif entry["snapshot_exists"] and entry["object_count"] is not None:
                    detail = (
                        f"({entry['object_count']} objs, "
                        f"{entry['omissions_count']} omissions)"
                    )
                else:
                    detail = "(no snapshot)"
                print(f"{status_tag:<8s} {key:<{col_w}s} {detail}")
        # Surface any blocking library locks not already represented by a per-cellview LOCKED line.
        per_cv_lock_files = {f for e in displayed_entries for f in e.get("lock_files", ())}
        for lock_entry in lock_entries:
            if not lock_entry["blocking"]:
                continue
            report_dict = lock_entry.get("report")
            if isinstance(report_dict, Mapping):
                all_files = list(report_dict.get("cdslck_files", ())) + list(
                    report_dict.get("lck_files", ())
                )
                extra_files = [f for f in all_files if f not in per_cv_lock_files]
                if not extra_files:
                    continue
                n = len(extra_files)
                noun = "live lock" if n == 1 else "live locks"
                print(
                    f"LOCKED   {lock_entry['library']} ({lock_entry['directory']}) ({n} {noun})"
                )
            else:
                print(
                    f"LOCKED   {lock_entry['library']} ({lock_entry['directory']}) "
                    f"({lock_entry.get('error', 'lock check failed')})"
                )

    return 0 if ok else 1


def _cmd_diff(args: argparse.Namespace) -> int:
    from .layout_diff import diff_layouts, format_layout_diff_stat, format_layout_diff_text

    root, config = _discover_project_and_config(
        args.root, allow_missing=True, fallback_to_git=True
    )
    settings = _resolve_settings(args, root, config)
    snapshots_dir: Path = settings["snapshots_dir"]

    old_local = _local_snapshot_candidate(args.old, root)
    new_local = _local_snapshot_candidate(args.new, root)
    old_snap = read_layout_snapshot(old_local, verify=True) if old_local else None
    new_snap = read_layout_snapshot(new_local, verify=True) if new_local else None

    cellview = CellViewRef.parse(args.cellview) if args.cellview else None
    if cellview is None and (old_snap is None or new_snap is None):
        local_snaps = [s for s in (old_snap, new_snap) if s is not None]
        if len(local_snaps) == 1:
            cellview = local_snaps[0].manifest.cellview
        else:
            discovered = [
                cv
                for _snap_dir, cv in _discover_layout_snapshot_dirs(snapshots_dir)
                if cv is not None
            ]
            unique_cvs = sorted({cv.key: cv for cv in discovered}.items())
            if len(unique_cvs) == 1:
                cellview = unique_cvs[0][1]
            else:
                raise SchemaValidationError(
                    "unable to infer layout CellView for Git revision diff; "
                    "pass --cellview <lib>/<cell>/<view>"
                )

    with ExitStack() as stack:
        if old_snap is None:
            assert cellview is not None
            old_snap = _git_layout_snapshot(root, snapshots_dir, args.old, cellview, stack)
        if new_snap is None:
            assert cellview is not None
            new_snap = _git_layout_snapshot(root, snapshots_dir, args.new, cellview, stack)

        diff = diff_layouts(old_snap, new_snap)

        if args.json:
            payload: dict[str, Any] = {"ok": True, **diff.to_dict()}
            if args.connectivity_only:
                payload["connectivity_only"] = True
            if args.stat:
                payload["summary"] = diff.summary()
                payload["stat"] = format_layout_diff_stat(
                    diff, connectivity_only=args.connectivity_only
                )
            _print_json(payload)
        elif args.stat:
            print(format_layout_diff_stat(diff, connectivity_only=args.connectivity_only))
        else:
            print(format_layout_diff_text(diff, connectivity_only=args.connectivity_only))

        changed = (
            diff.has_connectivity_changes if args.connectivity_only else diff.has_changes
        )
        return 1 if args.exit_code and changed else 0


def _cmd_merge(args: argparse.Namespace) -> int:
    from .layout_merge import format_layout_merge_conflicts, merge_layouts

    base = read_layout_snapshot(_resolve_local_path(args.base_dir), verify=True)
    ours = read_layout_snapshot(_resolve_local_path(args.ours_dir), verify=True)
    theirs = read_layout_snapshot(_resolve_local_path(args.theirs_dir), verify=True)
    result = merge_layouts(
        base,
        ours,
        theirs,
        check_spatial_overlap=not args.allow_spatial_overlap,
    )
    if not result.ok:
        if args.json:
            _print_json(result.to_dict())
        else:
            print(format_layout_merge_conflicts(result))
        return 1
    if result.snapshot is None:  # pragma: no cover
        raise BridgeExecutionError("layout merge returned ok=True without a snapshot")
    out_dir = _resolve_local_path(args.out_dir)
    write_layout_snapshot(result.snapshot, out_dir)
    if args.json:
        payload = dict(result.to_dict())
        payload["out_dir"] = str(out_dir)
        _print_json(payload)
    else:
        print(f"Merged layout snapshots into {out_dir}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    root, _config = _discover_project_and_config(args.root, allow_missing=True)
    snapshot_dir = _resolve_cli_input_path(
        args.snapshot_dir, root, explicit_root=args.root is not None
    )
    try:
        manifest = verify_layout_snapshot_integrity(snapshot_dir)
        snapshot = read_layout_snapshot(snapshot_dir, verify=False)
    except (SnapshotIntegrityError, SchemaValidationError) as exc:
        if args.json:
            _print_json(
                {
                    "ok": False,
                    "snapshot_dir": str(snapshot_dir),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        else:
            print(f"FAILED {snapshot_dir}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    definitions_count = (
        len(decode_definitions_jsonl((snapshot_dir / "definitions.jsonl").read_bytes()))
        if "definitions.jsonl" in manifest.files
        else 0
    )
    result = {
        "ok": True,
        "snapshot_dir": str(snapshot_dir),
        "schema": manifest.schema,
        "view_type": manifest.view_type,
        "cellview": manifest.cellview.to_dict(),
        "content_sha256": manifest.content_sha256,
        "full_sha256": manifest.full_sha256,
        "files": dict(sorted(manifest.files.items())),
        "object_count": len(snapshot.objects),
        "definitions_count": definitions_count,
        "omissions_count": manifest.omissions_count,
        "has_omissions": manifest.omissions_count > 0,
    }
    if args.json:
        _print_json(result)
    else:
        print(
            f"OK {snapshot_dir} ({manifest.cellview.key}, "
            f"objects={len(snapshot.objects)}, definitions={definitions_count}, "
            f"omissions={manifest.omissions_count})"
        )
    return 0


def _add_backend_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cds-lib", type=str, default=None)
    parser.add_argument(
        "--backend", choices=("fixture", "virtuoso"), default=None
    )
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--virtuoso-cmd", default=None)
    parser.add_argument("--ssh-host", default=None)
    parser.add_argument("--remote-workdir", default=None)
    parser.add_argument("--timeout-s", type=int, default=None)
    parser.add_argument("--one-shot", action="store_true")
    parser.add_argument("--no-daemon", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oagit-layout",
        description="Standalone oa-git CLI for OpenAccess layout snapshots",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="capture layout CellView(s) to snapshots")
    capture.add_argument(
        "cellviews",
        nargs="*",
        metavar="CELLVIEW",
        help="CellView reference(s) <lib>/<cell>/<view> (optional with --changed)",
    )
    capture.add_argument("--root", type=Path, default=None)
    capture.add_argument("--out", type=Path, default=None)
    capture.add_argument("--snapshots-dir", type=Path, default=None)
    capture.add_argument(
        "--changed",
        action="store_true",
        help="only capture layouts whose local OA source files changed since last capture",
    )
    _add_backend_options(capture)
    capture.add_argument("--json", action="store_true")
    capture.set_defaults(handler=_cmd_capture)

    restore = commands.add_parser(
        "restore", help="restore a layout snapshot into an OA CellView"
    )
    restore.add_argument("snapshot_dir", type=Path)
    restore.add_argument("--root", type=Path, default=None)
    restore.add_argument("--target", default=None)
    restore.add_argument("--allow-library", action="append", nargs="+", default=None)
    restore.add_argument("--overwrite", action="store_true")
    restore.add_argument("--allow-omissions", action="store_true")
    restore.add_argument("--skip-readback", action="store_true")
    restore.add_argument("--oa-view-dir", type=Path, default=None)
    _add_backend_options(restore)
    restore.add_argument("--json", action="store_true")
    restore.set_defaults(handler=_cmd_restore)

    status = commands.add_parser(
        "status", help="show compact status of layout snapshots, dirty index, and locks"
    )
    status.add_argument(
        "cellviews",
        nargs="*",
        metavar="CELLVIEW",
        help="optional CellView filter(s) <lib>/<cell>/<view>",
    )
    status.add_argument("--root", type=Path, default=None)
    status.add_argument("--snapshots-dir", type=Path, default=None)
    status.add_argument(
        "--oa-lib-map",
        action="append",
        nargs="+",
        default=None,
        metavar="LIB=DIR",
    )
    status.add_argument(
        "--changed",
        action="store_true",
        help="only show dirty, missing, unknown, locked, or failed layouts",
    )
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_cmd_status)

    diff = commands.add_parser(
        "diff", help="semantic 3-tier diff of two layout snapshots or Git revisions"
    )
    diff.add_argument("old", help="old snapshot directory or Git revision (e.g. HEAD~1)")
    diff.add_argument("new", help="new snapshot directory or Git revision (e.g. HEAD)")
    diff.add_argument("--cellview", default=None, help="CellView <lib>/<cell>/<view> for Git diff")
    diff.add_argument("--root", type=Path, default=None)
    diff.add_argument("--snapshots-dir", type=Path, default=None)
    diff.add_argument("--connectivity-only", action="store_true")
    diff.add_argument("--stat", action="store_true", help="print compact Git-diffstat summary")
    diff.add_argument("--exit-code", action="store_true")
    diff.add_argument("--json", action="store_true")
    diff.set_defaults(handler=_cmd_diff)

    merge = commands.add_parser("merge", help="three-way merge of layout snapshots")
    merge.add_argument("base_dir", type=Path)
    merge.add_argument("ours_dir", type=Path)
    merge.add_argument("theirs_dir", type=Path)
    merge.add_argument("-o", "--out", dest="out_dir", type=Path, required=True)
    merge.add_argument("--allow-spatial-overlap", action="store_true")
    merge.add_argument("--json", action="store_true")
    merge.set_defaults(handler=_cmd_merge)

    verify = commands.add_parser("verify", help="verify layout snapshot Manifest v2 integrity")
    verify.add_argument("snapshot_dir", type=Path)
    verify.add_argument("--root", type=Path, default=None)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=_cmd_verify)

    return parser


def _emit_error(error: Exception, *, json_mode: bool) -> None:
    if json_mode:
        _print_json(
            {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
    else:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``oa_git.layout_cli`` and return its process exit code."""

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
        code = int(exc.code or 0)
        if json_requested and code:
            _print_json(
                {
                    "ok": False,
                    "error_type": "ArgumentError",
                    "error": parse_error.getvalue().strip() or "argument parsing failed",
                }
            )
        return code

    json_mode = bool(getattr(args, "json", False))
    try:
        return int(args.handler(args))
    except (SnapshotIntegrityError, LiveLockError) as exc:
        _emit_error(exc, json_mode=json_mode)
        return 1
    except OaGitError as exc:
        _emit_error(exc, json_mode=json_mode)
        return 2
    except (OSError, ValueError, TypeError, RecursionError, ImportError) as exc:
        error = BridgeExecutionError(str(exc))
        _emit_error(error, json_mode=json_mode)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
