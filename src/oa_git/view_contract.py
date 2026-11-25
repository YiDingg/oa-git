"""Frozen shared view contract, handler registry, and universal v2 manifest helpers.

This module defines the stable seam shared across Track 1 (schematic, symbol,
batch/daemon) and Track 2 (layout) so parallel tracks integrate with zero merge
conflicts.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .model import (
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    SUPPORTED_SCHEMA_VERSIONS,
    VALID_VIEW_TYPES,
    CellViewRef,
    Manifest,
    SchemaValidationError,
    SnapshotIntegrityError,
    ViewType,
)

RESERVED_VIEW_TYPES: tuple[ViewType, ...] = ("schematic", "symbol", "layout")


# ---------------------------------------------------------------------------
# Session & Batch Orchestration Contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OaSession:
    """Identifies an active local or remote Virtuoso execution session."""

    session_id: str
    transport: str = "one_shot"  # "daemon" | "one_shot" | "one_shot_fallback" | "fixture"
    daemon_id: str | None = None
    reused: bool = False
    cds_lib: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "transport": self.transport,
            "daemon_id": self.daemon_id,
            "reused": self.reused,
            "cds_lib": self.cds_lib,
            "metadata": dict(sorted(self.metadata.items())),
        }


@dataclass(frozen=True)
class BatchCaptureItem:
    """Per-CellView outcome within a single-session batch capture."""

    cellview: CellViewRef
    snapshot: Any | None
    skipped: bool
    reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cellview": self.cellview.to_dict(),
            "ok": self.error is None and (self.skipped or self.snapshot is not None),
            "skipped": self.skipped,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass(frozen=True)
class BatchCaptureResult:
    """Result envelope returned by OaBackend.capture_batch."""

    items: tuple[BatchCaptureItem, ...]
    discovered: tuple[CellViewRef, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    session: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(item.error is None for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "items": [item.to_dict() for item in self.items],
            "discovered": [cv.to_dict() for cv in self.discovered],
            "cycles": [list(c) for c in self.cycles],
            "session": dict(self.session),
        }


# ---------------------------------------------------------------------------
# Frozen Backend & View Handler Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillRequestRunner(Protocol):
    """Transport-agnostic seam for executing SKILL actions (schematic/symbol/layout)."""

    def run_skill_request(
        self,
        action: str,
        request: Mapping[str, Any],
        *,
        cds_lib: Path | None = None,
        session: OaSession | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ViewHandler(Protocol):
    """Frozen view-type plugin contract shared by schematic, symbol, and layout."""

    view_type: ViewType
    capture_action: str
    restore_action: str

    def source_file_candidates(self, cell: str, view: str) -> tuple[str, ...]:
        """Return relative paths under the OA library root for dirty-index checks."""
        ...

    def read_snapshot(self, snapshot_dir: Path, *, verify: bool = True) -> Any: ...

    def write_snapshot(self, snapshot: Any, dest_dir: Path) -> Path: ...

    def diff(self, old: Any, new: Any) -> Any: ...

    def merge(self, base: Any, ours: Any, theirs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# View Handler Registry
# ---------------------------------------------------------------------------

_VIEW_HANDLERS: dict[str, ViewHandler] = {}
_INITIAL_VIEW_HANDLERS: dict[str, ViewHandler] = _VIEW_HANDLERS
_DEFAULT_HANDLER_MODULES: dict[str, str] = {
    "schematic": "oa_git.snapshot",
    "symbol": "oa_git.symbol_snapshot",
    "layout": "oa_git.layout_backend",
}
_DEFAULT_HANDLERS_ATTEMPTED: set[str] = set()


def ensure_default_view_handlers(view_types: Sequence[str] | None = None) -> None:
    """Lazily import default view-handler modules when their handlers are not yet registered.

    Preserves explicit test isolation when ``_VIEW_HANDLERS`` has been replaced
    via monkeypatching or when a module has already been loaded and its handler
    subsequently removed.
    """

    if _VIEW_HANDLERS is not _INITIAL_VIEW_HANDLERS:
        return
    targets = tuple(RESERVED_VIEW_TYPES) if view_types is None else tuple(view_types)
    for view_type in targets:
        if view_type in _VIEW_HANDLERS or view_type in _DEFAULT_HANDLERS_ATTEMPTED:
            continue
        module_name = _DEFAULT_HANDLER_MODULES.get(view_type)
        if module_name is None:
            continue
        _DEFAULT_HANDLERS_ATTEMPTED.add(view_type)
        try:
            importlib.import_module(module_name)
        except Exception:
            _DEFAULT_HANDLERS_ATTEMPTED.discard(view_type)
            raise


def infer_view_type(
    view_name: str, *, manifest_view_type: str | None = None
) -> ViewType | None:
    """Infer canonical ``ViewType`` from a manifest ``view_type`` or OA view name."""

    if isinstance(manifest_view_type, str) and manifest_view_type in VALID_VIEW_TYPES:
        return manifest_view_type  # type: ignore[return-value]
    if not isinstance(view_name, str):
        return None
    normalized = view_name.strip()
    if normalized == "schematic" or normalized.startswith("schematic"):
        return "schematic"
    if normalized == "symbol" or normalized.startswith("symbol"):
        return "symbol"
    if normalized == "layout" or normalized.startswith("layout"):
        return "layout"
    return None


def register_view_handler(view_type: ViewType, handler: ViewHandler) -> None:
    """Register a concrete handler for one of the reserved view types."""

    if view_type not in VALID_VIEW_TYPES:
        raise SchemaValidationError(
            f"Unsupported view_type {view_type!r}; expected one of {sorted(VALID_VIEW_TYPES)}"
        )
    if getattr(handler, "view_type", None) != view_type:
        raise SchemaValidationError(
            f"Handler view_type {getattr(handler, 'view_type', None)!r} does not match {view_type!r}"
        )
    _VIEW_HANDLERS[view_type] = handler


def get_view_handler(view_type: str) -> ViewHandler:
    """Look up a registered view handler or fail closed."""

    if view_type not in VALID_VIEW_TYPES:
        raise SchemaValidationError(f"Unsupported view_type: {view_type!r}")
    if view_type not in _VIEW_HANDLERS:
        ensure_default_view_handlers((view_type,))
    if view_type not in _VIEW_HANDLERS:
        raise SchemaValidationError(
            f"No handler registered for view_type {view_type!r}"
        )
    return _VIEW_HANDLERS[view_type]


def has_view_handler(view_type: str) -> bool:
    return view_type in _VIEW_HANDLERS


def list_view_handlers() -> tuple[str, ...]:
    ensure_default_view_handlers()
    return tuple(sorted(_VIEW_HANDLERS))


# ---------------------------------------------------------------------------
# Universal Manifest v2 Digest & Integrity Helpers (shared by all view tracks)
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: Any) -> bytes:
    """Encode payload into deterministic compact UTF-8 JSON bytes."""

    return (
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _pretty_canonical_json_bytes(payload: Any) -> bytes:
    """Encode payload into deterministic 2-space pretty UTF-8 JSON bytes."""

    return (
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).rstrip("\n")
        + "\n"
    ).encode("utf-8")


def _parse_manifest_json_bytes(raw_bytes: bytes) -> dict[str, Any]:
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotIntegrityError(f"manifest.json is not valid UTF-8: {exc}") from exc

    def _reject_constant(value: str) -> Any:
        raise SnapshotIntegrityError(f"Non-finite manifest JSON constant {value}")

    def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise SnapshotIntegrityError(f"Duplicate manifest JSON key {key!r}")
            out[key] = value
        return out

    try:
        data = json.loads(
            raw_text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except SnapshotIntegrityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"Cannot parse manifest.json: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaValidationError("manifest.json root must be a JSON object")
    return data


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compute_content_sha256(file_contents: Mapping[str, bytes]) -> str:
    """Hash all canonical artifact bytes sorted by filename (excluding manifest.json & omissions.json)."""

    hasher = hashlib.sha256()
    for name in sorted(file_contents):
        if name in ("manifest.json", "omissions.json"):
            continue
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(file_contents[name])
        hasher.update(b"\x00")
    return hasher.hexdigest()


def compute_manifest_v2_full_sha256(
    *,
    view_type: ViewType,
    cellview: CellViewRef,
    files: Mapping[str, str],
    omissions_count: int,
    metadata: Mapping[str, Any],
) -> str:
    """Compute canonical v2 full_sha256 binding view_type, cellview, files, omissions, metadata."""

    binding = {
        "schema": SCHEMA_VERSION_V2,
        "view_type": view_type,
        "cellview": cellview.to_dict(),
        "files": dict(sorted(files.items())),
        "omissions_count": omissions_count,
        "metadata": dict(sorted(metadata.items())),
    }
    return sha256_bytes(canonical_json_bytes(binding))


def build_manifest_v2(
    *,
    view_type: ViewType,
    cellview: CellViewRef,
    file_contents: Mapping[str, bytes],
    omissions_count: int = 0,
    metadata: Mapping[str, Any] | None = None,
    electrical_sha256: str | None = None,
    placement_sha256: str | None = None,
) -> Manifest:
    """Build a canonical oa-git/2 Manifest for any view type (schematic, symbol, layout)."""

    if view_type not in VALID_VIEW_TYPES:
        raise SchemaValidationError(f"Unsupported view_type: {view_type!r}")
    if view_type == "schematic":
        if type(electrical_sha256) is not str or not electrical_sha256:
            raise SchemaValidationError(
                "Schematic manifest requires a non-empty string electrical_sha256"
            )
        if type(placement_sha256) is not str or not placement_sha256:
            raise SchemaValidationError(
                "Schematic manifest requires a non-empty string placement_sha256"
            )
    else:
        if electrical_sha256 is not None:
            raise SchemaValidationError(
                f"Non-schematic manifest ({view_type!r}) must have electrical_sha256=None"
            )
        if placement_sha256 is not None:
            raise SchemaValidationError(
                f"Non-schematic manifest ({view_type!r}) must have placement_sha256=None"
            )
    if type(omissions_count) is not int or omissions_count < 0:
        raise SchemaValidationError(f"Invalid omissions_count: {omissions_count!r}")
    files = {name: sha256_bytes(raw) for name, raw in sorted(file_contents.items())}
    if omissions_count > 0 and "omissions.json" not in files:
        raise SchemaValidationError(
            "omissions.json must be present in files when omissions_count > 0"
        )
    if omissions_count == 0 and "omissions.json" in files:
        raise SchemaValidationError(
            "omissions.json must not be present in files when omissions_count == 0"
        )
    meta = dict(sorted((metadata or {}).items()))
    content_sha = compute_content_sha256(file_contents)
    full_sha = compute_manifest_v2_full_sha256(
        view_type=view_type,
        cellview=cellview,
        files=files,
        omissions_count=omissions_count,
        metadata=meta,
    )
    return Manifest(
        schema=SCHEMA_VERSION_V2,
        view_type=view_type,
        cellview=cellview,
        electrical_sha256=electrical_sha256,
        placement_sha256=placement_sha256,
        full_sha256=full_sha,
        files=files,
        omissions_count=omissions_count,
        metadata=meta,
        content_sha256=content_sha,
    )


def read_manifest_file(snapshot_dir: Path) -> Manifest:
    """Read and parse manifest.json (v1 or v2) without assuming schematic-only view."""

    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise SnapshotIntegrityError(f"Missing or symlinked snapshot directory: {snapshot_dir}")
    manifest_path = snapshot_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SnapshotIntegrityError(f"Missing or invalid manifest.json in {snapshot_dir}")
    try:
        raw_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Cannot read manifest.json: {exc}") from exc
    data = _parse_manifest_json_bytes(raw_bytes)
    return Manifest.from_dict(data)


def verify_manifest_v2_integrity(snapshot_dir: Path, manifest: Manifest) -> Manifest:
    """Verify canonical manifest bytes, schema invariants, file hashes, content_sha256, omissions, and full_sha256 for oa-git/2."""

    if not isinstance(manifest, Manifest):
        raise SchemaValidationError("manifest must be a Manifest")
    if manifest.schema != SCHEMA_VERSION_V2:
        raise SchemaValidationError(
            f"Expected {SCHEMA_VERSION_V2}, got {manifest.schema!r}"
        )
    if manifest.view_type not in VALID_VIEW_TYPES:
        raise SchemaValidationError(f"Unsupported view_type: {manifest.view_type!r}")
    if manifest.view_type == "schematic":
        if type(manifest.electrical_sha256) is not str or not manifest.electrical_sha256:
            raise SchemaValidationError(
                "Schematic manifest requires a non-empty string electrical_sha256"
            )
        if type(manifest.placement_sha256) is not str or not manifest.placement_sha256:
            raise SchemaValidationError(
                "Schematic manifest requires a non-empty string placement_sha256"
            )
    else:
        if manifest.electrical_sha256 is not None:
            raise SchemaValidationError(
                f"Non-schematic manifest ({manifest.view_type!r}) must have electrical_sha256=None"
            )
        if manifest.placement_sha256 is not None:
            raise SchemaValidationError(
                f"Non-schematic manifest ({manifest.view_type!r}) must have placement_sha256=None"
            )

    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise SnapshotIntegrityError(f"Missing or symlinked snapshot directory: {snapshot_dir}")
    manifest_path = snapshot_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SnapshotIntegrityError(f"Missing or invalid manifest.json in {snapshot_dir}")
    try:
        raw_manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Cannot read manifest.json: {exc}") from exc

    disk_data = _parse_manifest_json_bytes(raw_manifest_bytes)
    disk_manifest = Manifest.from_dict(disk_data)
    if disk_manifest != manifest:
        raise SnapshotIntegrityError(
            "manifest.json on disk does not match manifest being verified"
        )
    canonical_compact = canonical_json_bytes(manifest.to_dict())
    canonical_pretty = _pretty_canonical_json_bytes(manifest.to_dict())
    if raw_manifest_bytes not in (canonical_compact, canonical_pretty):
        raise SnapshotIntegrityError("manifest.json is not in canonical form")

    file_bytes: dict[str, bytes] = {}
    for name, expected_hash in sorted(manifest.files.items()):
        if "/" in name or "\\" in name or name in (".", "..", "manifest.json"):
            raise SnapshotIntegrityError(f"Unsafe manifest file entry: {name!r}")
        file_path = snapshot_dir / name
        if file_path.is_symlink() or not file_path.is_file():
            raise SnapshotIntegrityError(f"Missing or symlinked snapshot file: {file_path}")
        raw = file_path.read_bytes()
        actual_hash = sha256_bytes(raw)
        if actual_hash != expected_hash:
            raise SnapshotIntegrityError(
                f"SHA-256 mismatch for {name}: expected {expected_hash}, got {actual_hash}"
            )
        file_bytes[name] = raw

    omissions_path = snapshot_dir / "omissions.json"
    if manifest.omissions_count > 0:
        if "omissions.json" not in file_bytes:
            raise SnapshotIntegrityError(
                "omissions_count > 0 but omissions.json missing from manifest.files"
            )
    else:
        if (
            "omissions.json" in file_bytes
            or omissions_path.exists()
            or omissions_path.is_symlink()
        ):
            raise SnapshotIntegrityError(
                "Stale or symlinked omissions.json present when omissions_count == 0"
            )

    expected_content_sha = compute_content_sha256(file_bytes)
    if manifest.content_sha256 != expected_content_sha:
        raise SnapshotIntegrityError(
            f"content_sha256 mismatch: expected {expected_content_sha}, got {manifest.content_sha256}"
        )
    expected_full_sha = compute_manifest_v2_full_sha256(
        view_type=manifest.view_type,
        cellview=manifest.cellview,
        files=manifest.files,
        omissions_count=manifest.omissions_count,
        metadata=manifest.metadata,
    )
    if manifest.full_sha256 != expected_full_sha:
        raise SnapshotIntegrityError(
            f"full_sha256 mismatch: expected {expected_full_sha}, got {manifest.full_sha256}"
        )
    return manifest


__all__ = [
    "RESERVED_VIEW_TYPES",
    "OaSession",
    "BatchCaptureItem",
    "BatchCaptureResult",
    "SkillRequestRunner",
    "ViewHandler",
    "ensure_default_view_handlers",
    "infer_view_type",
    "register_view_handler",
    "get_view_handler",
    "has_view_handler",
    "list_view_handlers",
    "canonical_json_bytes",
    "sha256_bytes",
    "compute_content_sha256",
    "compute_manifest_v2_full_sha256",
    "build_manifest_v2",
    "read_manifest_file",
    "verify_manifest_v2_integrity",
]
