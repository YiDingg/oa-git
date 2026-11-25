"""Domain data contracts and Fail-Closed exception hierarchy for oa-git.

This module defines the shared immutable data structures and exceptions used across
all oa-git modules (snapshot codec, semantic diff, three-way merge, safety guards,
OA backends, symbol/layout view handlers, daemon/batch orchestration, and CLI).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA_VERSION_V1 = "oa-git/1"
SCHEMA_VERSION_V2 = "oa-git/2"
SCHEMA_VERSION = SCHEMA_VERSION_V2
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION_V1, SCHEMA_VERSION_V2})

CONFIG_SCHEMA_V1 = "oa-git-config/1"
CONFIG_SCHEMA_V2 = "oa-git-config/2"
INDEX_SCHEMA_V1 = "oa-git-index/1"
BATCH_PROTOCOL_V1 = "oagit-batch/1"
SPOOL_PROTOCOL_V1 = "oagit-spool/1"
SYMBOL_SCHEMA_V1 = "oa-git-symbol/1"

ViewType = Literal["schematic", "symbol", "layout"]
VALID_VIEW_TYPES = frozenset({"schematic", "symbol", "layout"})
ParamValue = str | int | float | bool
DerivedStatus = Literal["valid", "stale", "unavailable"]
DerivedSource = Literal["cdf-readback", "legacy-import"]

VALID_DERIVED_STATUSES = frozenset({"valid", "stale", "unavailable"})
VALID_DERIVED_SOURCES = frozenset({"cdf-readback", "legacy-import"})
VALID_PORT_DIRECTIONS = frozenset(
    {"input", "output", "inputOutput", "switch", "jumper", "unused"}
)
VALID_ORIENTS = frozenset(
    {"R0", "R90", "R180", "R270", "MX", "MXR90", "MY", "MYR90"}
)
VALID_CDF_TYPES = frozenset({"string", "int", "float", "boolean", "cyclic", "radio"})
VALID_ANNOTATION_KINDS = frozenset({"rect", "polygon", "line"})


# ---------------------------------------------------------------------------
# Exception Hierarchy
# ---------------------------------------------------------------------------


class OaGitError(Exception):
    """Base error for all oa-git failures."""


class SchemaValidationError(OaGitError):
    """Raised when snapshot or config data violates the oa-git schema."""


class SnapshotIntegrityError(OaGitError):
    """Raised when snapshot files fail manifest SHA-256 verification."""


class FailClosedError(OaGitError):
    """Base class for safety-enforced Fail-Closed aborts."""


class LiveLockError(FailClosedError):
    """Raised when active Cadence *.cdslck* or *.lck locks block an operation."""


class DisallowedLibraryError(FailClosedError):
    """Raised when a restore targets an OA library outside the explicit allowlist."""


class UnsupportedObjectError(FailClosedError):
    """Raised when unsupported OA objects or omissions block strict restore."""


class CdfParameterError(FailClosedError):
    """Raised when CDF parameter validation or callback execution fails."""


class RoundtripMismatchError(FailClosedError):
    """Raised when post-restore readback diverges from the target snapshot."""


class BridgeExecutionError(FailClosedError):
    """Raised when the OA backend / Virtuoso SKILL bridge fails."""


# ---------------------------------------------------------------------------
# Core Reference & Manifest (v1 backward-compatible + v2 universal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellViewRef:
    """Uniquely identifies a Cadence OpenAccess CellView."""

    library: str
    cell: str
    view: str

    def __post_init__(self) -> None:
        for attr in ("library", "cell", "view"):
            val = getattr(self, attr)
            if (
                not isinstance(val, str)
                or not val
                or val in {".", ".."}
                or val != val.strip()
                or "/" in val
                or "\\" in val
                or any(unicodedata.category(char) in {"Cc", "Cf"} for char in val)
            ):
                raise SchemaValidationError(f"Invalid CellViewRef.{attr}: {val!r}")

    @property
    def key(self) -> str:
        return f"{self.library}/{self.cell}/{self.view}"

    @classmethod
    def parse(cls, spec: str) -> CellViewRef:
        if not isinstance(spec, str):
            raise SchemaValidationError(
                f"Expected '<library>/<cell>/<view>', got {spec!r}"
            )
        parts = spec.split("/")
        if len(parts) != 3 or any(not p for p in parts):
            raise SchemaValidationError(
                f"Expected '<library>/<cell>/<view>', got {spec!r}"
            )
        return cls(library=parts[0], cell=parts[1], view=parts[2])

    def to_dict(self) -> dict[str, str]:
        return {"library": self.library, "cell": self.cell, "view": self.view}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CellViewRef:
        if not isinstance(data, Mapping):
            raise SchemaValidationError(
                f"CellViewRef must be a mapping, got {type(data).__name__}"
            )
        if any(type(k) is not str for k in data):
            raise SchemaValidationError("CellViewRef keys must be strings")
        expected = {"library", "cell", "view"}
        unknown = set(data) - expected
        if unknown:
            raise SchemaValidationError(
                f"CellViewRef contains unsupported keys: {sorted(unknown)!r}"
            )
        missing = expected - set(data)
        if missing:
            raise SchemaValidationError(
                f"CellViewRef is missing required keys: {sorted(missing)!r}"
            )
        return cls(
            library=data["library"],
            cell=data["cell"],
            view=data["view"],
        )


@dataclass(frozen=True)
class OmissionRecord:
    """Structured record of an OA object or property outside the supported subset."""

    kind: str
    identifier: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "identifier": self.identifier,
            "reason": self.reason,
        }
        if self.details:
            out["details"] = self.details
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OmissionRecord:
        if not isinstance(data, Mapping):
            raise SchemaValidationError(
                f"OmissionRecord must be a mapping, got {type(data).__name__}"
            )
        if any(type(k) is not str for k in data):
            raise SchemaValidationError("OmissionRecord keys must be strings")
        allowed = {"kind", "identifier", "reason", "details"}
        unknown = set(data) - allowed
        if unknown:
            raise SchemaValidationError(
                f"OmissionRecord contains unsupported keys: {sorted(unknown)!r}"
            )
        missing = {"kind", "identifier", "reason"} - set(data)
        if missing:
            raise SchemaValidationError(
                f"OmissionRecord is missing required keys: {sorted(missing)!r}"
            )
        for key in ("kind", "identifier", "reason"):
            if type(data[key]) is not str or not data[key].strip():
                raise SchemaValidationError(
                    f"OmissionRecord.{key} must be a non-empty string"
                )
        raw_details = data.get("details", {})
        if not isinstance(raw_details, Mapping) or any(
            type(k) is not str for k in raw_details
        ):
            raise SchemaValidationError(
                "OmissionRecord.details must be a mapping with string keys"
            )
        return cls(
            kind=data["kind"],
            identifier=data["identifier"],
            reason=data["reason"],
            details=dict(raw_details),
        )


_ALLOWED_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "view_type",
        "cellview",
        "electrical_sha256",
        "placement_sha256",
        "full_sha256",
        "files",
        "omissions_count",
        "metadata",
        "content_sha256",
    }
)


@dataclass(frozen=True)
class Manifest:
    """Unified snapshot manifest shared by schematic, symbol, and layout views."""

    schema: str
    view_type: ViewType
    cellview: CellViewRef
    electrical_sha256: str | None
    placement_sha256: str | None
    full_sha256: str
    files: dict[str, str]
    omissions_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    content_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "view_type": self.view_type,
            "cellview": self.cellview.to_dict(),
            "electrical_sha256": self.electrical_sha256,
            "placement_sha256": self.placement_sha256,
            "full_sha256": self.full_sha256,
            "files": dict(sorted(self.files.items())),
            "omissions_count": self.omissions_count,
            "metadata": dict(sorted(self.metadata.items())),
        }
        if self.schema == SCHEMA_VERSION_V2 or self.content_sha256 is not None:
            payload["content_sha256"] = self.content_sha256
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Manifest:
        if not isinstance(data, Mapping):
            raise SchemaValidationError(
                f"Manifest must be a mapping, got {type(data).__name__}"
            )
        if any(type(k) is not str for k in data):
            raise SchemaValidationError("Manifest keys must be strings")
        unknown = set(data) - _ALLOWED_MANIFEST_KEYS
        if unknown:
            raise SchemaValidationError(
                f"Manifest contains unsupported keys: {sorted(unknown)!r}"
            )
        raw_schema = data.get("schema")
        if type(raw_schema) is not str or raw_schema not in SUPPORTED_SCHEMA_VERSIONS:
            raise SchemaValidationError(
                f"Unsupported manifest schema {raw_schema!r}; expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        view_type = data.get("view_type")
        if type(view_type) is not str or view_type not in VALID_VIEW_TYPES:
            raise SchemaValidationError(f"Unsupported view_type: {view_type!r}")
        if "cellview" not in data:
            raise SchemaValidationError("Manifest is missing required key 'cellview'")
        cellview = CellViewRef.from_dict(data["cellview"])

        raw_elec = data.get("electrical_sha256")
        raw_place = data.get("placement_sha256")
        raw_content = data.get("content_sha256")
        if view_type == "schematic":
            if type(raw_elec) is not str or not raw_elec:
                raise SchemaValidationError(
                    "Schematic manifest requires a non-empty string electrical_sha256"
                )
            if type(raw_place) is not str or not raw_place:
                raise SchemaValidationError(
                    "Schematic manifest requires a non-empty string placement_sha256"
                )
        else:
            if raw_elec is not None:
                raise SchemaValidationError(
                    f"Non-schematic manifest ({view_type!r}) must have electrical_sha256=None"
                )
            if raw_place is not None:
                raise SchemaValidationError(
                    f"Non-schematic manifest ({view_type!r}) must have placement_sha256=None"
                )

        if raw_schema == SCHEMA_VERSION_V2:
            if type(raw_content) is not str or not raw_content:
                raise SchemaValidationError(
                    "oa-git/2 manifest requires a non-empty string content_sha256"
                )
        elif raw_content is not None:
            raise SchemaValidationError(
                "oa-git/1 manifest must not declare content_sha256"
            )

        raw_full = data.get("full_sha256")
        if type(raw_full) is not str or not raw_full:
            raise SchemaValidationError(
                "Manifest requires a non-empty string full_sha256"
            )

        if "files" not in data:
            raise SchemaValidationError("Manifest is missing required key 'files'")
        raw_files = data["files"]
        if not isinstance(raw_files, Mapping):
            raise SchemaValidationError("Manifest.files must be a mapping")
        files: dict[str, str] = {}
        for k, v in raw_files.items():
            if type(k) is not str or not k:
                raise SchemaValidationError(
                    f"Manifest.files key must be a non-empty string, got {k!r}"
                )
            if type(v) is not str or not v:
                raise SchemaValidationError(
                    f"Manifest.files[{k!r}] must be a non-empty string, got {v!r}"
                )
            files[k] = v

        raw_omissions = data.get("omissions_count", 0)
        if type(raw_omissions) is not int or raw_omissions < 0:
            raise SchemaValidationError(
                f"Manifest omissions_count must be a non-negative int, got {raw_omissions!r}"
            )

        raw_metadata = data.get("metadata", {})
        if not isinstance(raw_metadata, Mapping) or any(
            type(k) is not str for k in raw_metadata
        ):
            raise SchemaValidationError(
                "Manifest.metadata must be a mapping with string keys"
            )

        return cls(
            schema=raw_schema,
            view_type=view_type,  # type: ignore[arg-type]
            cellview=cellview,
            electrical_sha256=raw_elec,
            placement_sha256=raw_place,
            full_sha256=raw_full,
            files=files,
            omissions_count=raw_omissions,
            metadata=dict(raw_metadata),
            content_sha256=raw_content,
        )


# ---------------------------------------------------------------------------
# CDF Derived State & Coupling Rules (v0.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedState:
    """Read-only PDK callback observation state separated from editable params."""

    status: DerivedStatus = "unavailable"
    values: dict[str, ParamValue] = field(default_factory=dict)
    input_sha256: str | None = None
    callback_order: tuple[str, ...] = ()
    source: DerivedSource = "cdf-readback"

    def __post_init__(self) -> None:
        if self.status not in VALID_DERIVED_STATUSES:
            raise SchemaValidationError(f"Invalid DerivedState.status: {self.status!r}")
        if self.source not in VALID_DERIVED_SOURCES:
            raise SchemaValidationError(f"Invalid DerivedState.source: {self.source!r}")
        if self.status in ("stale", "unavailable"):
            if self.values:
                raise SchemaValidationError(
                    f"DerivedState with status={self.status!r} must have empty values"
                )
            if self.input_sha256 is not None:
                raise SchemaValidationError(
                    f"DerivedState with status={self.status!r} must have input_sha256=None"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "values": dict(sorted(self.values.items())),
            "input_sha256": self.input_sha256,
            "callback_order": list(self.callback_order),
            "source": self.source,
        }


_ALLOWED_COUPLING_RULE_KEYS = frozenset(
    {"name", "members", "primary", "derived", "order", "dependencies"}
)


def _require_nonempty_str(value: Any, path: str) -> str:
    if type(value) is not str or not value.strip():
        raise SchemaValidationError(f"{path} must be a non-empty string")
    return value


def _require_str_tuple(value: Any, path: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise SchemaValidationError(f"{path} must be a sequence of strings")
    return tuple(
        _require_nonempty_str(item, f"{path}[{idx}]")
        for idx, item in enumerate(value)
    )


@dataclass(frozen=True)
class CdfCouplingRule:
    """Declarative coupling group governing multi-pass CDF callback convergence."""

    name: str
    members: tuple[str, ...]
    primary: tuple[str, ...]
    derived: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_str(self.name, "CdfCouplingRule.name")
        object.__setattr__(
            self, "members", _require_str_tuple(self.members, "CdfCouplingRule.members")
        )
        object.__setattr__(
            self, "primary", _require_str_tuple(self.primary, "CdfCouplingRule.primary")
        )
        object.__setattr__(
            self, "derived", _require_str_tuple(self.derived, "CdfCouplingRule.derived")
        )
        object.__setattr__(
            self, "order", _require_str_tuple(self.order, "CdfCouplingRule.order")
        )
        if not isinstance(self.dependencies, Mapping):
            raise SchemaValidationError("CdfCouplingRule.dependencies must be a mapping")
        normalized_deps: dict[str, tuple[str, ...]] = {}
        for key, raw_val in self.dependencies.items():
            k = _require_nonempty_str(key, "CdfCouplingRule.dependencies key")
            normalized_deps[k] = _require_str_tuple(
                raw_val, f"CdfCouplingRule.dependencies[{k!r}]"
            )
        object.__setattr__(self, "dependencies", normalized_deps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "members": list(self.members),
            "primary": list(self.primary),
            "derived": list(self.derived),
            "order": list(self.order),
            "dependencies": {k: list(v) for k, v in sorted(self.dependencies.items())},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CdfCouplingRule:
        if not isinstance(data, Mapping):
            raise SchemaValidationError(
                f"CdfCouplingRule must be a mapping, got {type(data).__name__}"
            )
        if any(type(k) is not str for k in data):
            raise SchemaValidationError("CdfCouplingRule keys must be strings")
        unknown = set(data) - _ALLOWED_COUPLING_RULE_KEYS
        if unknown:
            raise SchemaValidationError(
                f"CdfCouplingRule contains unsupported keys: {sorted(unknown)!r}"
            )
        if "name" not in data:
            raise SchemaValidationError("CdfCouplingRule is missing required key 'name'")
        name = _require_nonempty_str(data["name"], "CdfCouplingRule.name")
        members = (
            _require_str_tuple(data["members"], "CdfCouplingRule.members")
            if "members" in data
            else ()
        )
        primary = (
            _require_str_tuple(data["primary"], "CdfCouplingRule.primary")
            if "primary" in data
            else ()
        )
        derived = (
            _require_str_tuple(data["derived"], "CdfCouplingRule.derived")
            if "derived" in data
            else ()
        )
        order = (
            _require_str_tuple(data["order"], "CdfCouplingRule.order")
            if "order" in data
            else ()
        )
        deps: dict[str, tuple[str, ...]] = {}
        if "dependencies" in data:
            deps_raw = data["dependencies"]
            if not isinstance(deps_raw, Mapping):
                raise SchemaValidationError(
                    "CdfCouplingRule.dependencies must be a mapping"
                )
            for k, v in deps_raw.items():
                key_str = _require_nonempty_str(k, "CdfCouplingRule.dependencies key")
                deps[key_str] = _require_str_tuple(
                    v, f"CdfCouplingRule.dependencies[{key_str!r}]"
                )
        return cls(
            name=name,
            members=members,
            primary=primary,
            derived=derived,
            order=order,
            dependencies=deps,
        )


# ---------------------------------------------------------------------------
# Schematic Electrical Truth (circuit.yaml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Port:
    """Schematic top-level terminal/port."""

    name: str
    direction: str = "inputOutput"


@dataclass(frozen=True)
class Net:
    """Schematic electrical net."""

    name: str
    is_global: bool = False
    num_bits: int = 1


@dataclass(frozen=True)
class Instance:
    """Schematic device or hierarchy instance (electrical & CDF view)."""

    name: str
    lib: str
    cell: str
    view: str = "symbol"
    connections: dict[str, str] = field(default_factory=dict)
    params: dict[str, ParamValue] = field(default_factory=dict)
    param_types: dict[str, str] = field(default_factory=dict)
    derived_params: dict[str, ParamValue] = field(default_factory=dict)
    props: dict[str, ParamValue] = field(default_factory=dict)
    derived: DerivedState | None = None

    def __post_init__(self) -> None:
        if self.derived is not None and not self.derived_params and self.derived.values:
            object.__setattr__(self, "derived_params", dict(self.derived.values))
        elif self.derived is None and self.derived_params:
            object.__setattr__(
                self,
                "derived",
                DerivedState(
                    status="valid",
                    values=dict(self.derived_params),
                    input_sha256=None,
                    callback_order=(),
                    source="legacy-import",
                ),
            )

    @property
    def effective_derived(self) -> DerivedState:
        """Return canonical DerivedState even when constructed with legacy defaults."""

        if self.derived is not None:
            return self.derived
        return DerivedState(
            status="unavailable",
            values={},
            input_sha256=None,
            callback_order=(),
            source="cdf-readback",
        )


@dataclass(frozen=True)
class SchematicElectrical:
    """Pure electrical truth of a schematic CellView."""

    ports: tuple[Port, ...] = ()
    nets: tuple[Net, ...] = ()
    instances: tuple[Instance, ...] = ()
    properties: dict[str, ParamValue] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schematic Placement Geometry (placement.yaml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstancePlacement:
    """Visual location and orientation of a schematic instance."""

    name: str
    xy: tuple[float, float]
    orient: str = "R0"


@dataclass(frozen=True)
class PinPlacement:
    """Visual schematic pin glyph and its master reference."""

    name: str
    net: str
    direction: str
    lib: str
    cell: str
    view: str
    xy: tuple[float, float]
    orient: str = "R0"


@dataclass(frozen=True)
class WireLabel:
    """Net label attached to a wire segment."""

    text: str
    xy: tuple[float, float]
    justify: str = "centerCenter"
    orient: str = "R0"
    font: str = "stick"
    height: float = 0.0625


@dataclass(frozen=True)
class WireSegment:
    """Schematic wire polyline with optional attached labels."""

    points: tuple[tuple[float, float], ...]
    net: str | None = None
    width: float = 0.0
    route_method: str = "full"
    labels: tuple[WireLabel, ...] = ()


@dataclass(frozen=True)
class NoteLabel:
    """Free-standing schematic text/note label."""

    text: str
    xy: tuple[float, float]
    layer: str = "text"
    purpose: str = "drawing"
    justify: str = "lowerLeft"
    orient: str = "R0"
    font: str = "stick"
    height: float = 0.0625


@dataclass(frozen=True)
class AnnotationShape:
    """Non-wire decorative annotation shape (rect/polygon/line) on a schematic."""

    kind: str
    layer: str
    purpose: str = "drawing"
    points: tuple[tuple[float, float], ...] = ()
    width: float = 0.0


@dataclass(frozen=True)
class SchematicPlacement:
    """Purely visual geometry of a schematic CellView."""

    instances: tuple[InstancePlacement, ...] = ()
    pins: tuple[PinPlacement, ...] = ()
    wires: tuple[WireSegment, ...] = ()
    labels: tuple[NoteLabel, ...] = ()
    sheet: dict[str, Any] = field(default_factory=dict)
    annotations: tuple[AnnotationShape, ...] = ()


# ---------------------------------------------------------------------------
# Full Schematic Snapshot & Backend Receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchematicSnapshot:
    """Complete canonical snapshot of a schematic CellView."""

    manifest: Manifest
    electrical: SchematicElectrical
    placement: SchematicPlacement
    omissions: tuple[OmissionRecord, ...] = ()


@dataclass(frozen=True)
class RestoreReceipt:
    """Structured evidence produced by an OA backend restore operation."""

    target: CellViewRef
    published: bool
    draft_cell: str | None = None
    sch_check_errors: int = 0
    sch_check_warnings: int = 0
    cdf_verified: bool = False
    readback_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    derived_status: str | None = None
    derived_refresh: dict[str, Any] | None = None
    cdf_passes: int | None = None
    callback_order: dict[str, list[str]] | None = None
    transport: str = "one_shot"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": self.target.to_dict(),
            "published": self.published,
            "draft_cell": self.draft_cell,
            "sch_check_errors": self.sch_check_errors,
            "sch_check_warnings": self.sch_check_warnings,
            "cdf_verified": self.cdf_verified,
            "readback_verified": self.readback_verified,
            "details": self.details,
        }
        if self.derived_status is not None:
            payload["derived_status"] = self.derived_status
        if self.derived_refresh is not None:
            payload["derived_refresh"] = self.derived_refresh
        if self.cdf_passes is not None:
            payload["cdf_passes"] = self.cdf_passes
        if self.callback_order is not None:
            payload["callback_order"] = self.callback_order
        if self.transport != "one_shot":
            payload["transport"] = self.transport
        return payload
