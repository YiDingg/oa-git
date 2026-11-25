"""Domain data contracts for Cadence OpenAccess layout snapshots (oa-git v0.2).

This module defines the immutable domain types, Manifest v2 contract, PDK metadata
filters, and restore receipt structures for the Layout Track.  It reuses core
identifiers and the Fail-Closed exception hierarchy from :mod:`oa_git.model`
without modifying any shared Track 1 modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

from .model import (
    BridgeExecutionError,
    CdfParameterError,
    CellViewRef,
    DisallowedLibraryError,
    FailClosedError,
    LiveLockError,
    OaGitError,
    OmissionRecord,
    ParamValue,
    RoundtripMismatchError,
    SchemaValidationError,
    SnapshotIntegrityError,
    UnsupportedObjectError,
    VALID_ORIENTS,
    VALID_PORT_DIRECTIONS,
)

LAYOUT_MANIFEST_SCHEMA = "oa-git/2"
LAYOUT_VIEW_TYPE: Literal["layout"] = "layout"
LAYOUT_OBJECT_SCHEMA = "oa-git-layout/1"

VALID_SIG_TYPES = frozenset(
    {
        "signal",
        "power",
        "supply",
        "ground",
        "clock",
        "tieOff",
        "tieHi",
        "tieLo",
        "analog",
        "scan",
        "reset",
    }
)
VALID_PIN_SHAPE_KINDS = frozenset({"rect", "polygon", "ellipse", "path_seg", "path"})
VALID_PATH_STYLES = frozenset(
    {"truncateExtend", "extendExtend", "roundRound", "varExtendExtend"}
)
VALID_PATHSEG_STYLES = frozenset({"truncate", "extend", "octagon", "variable", "custom"})
VALID_VIA_KINDS = frozenset({"stdVia", "customVia"})
VALID_LABEL_TYPES = frozenset({"normalLabel", "NLPLabel", "ILLabel"})
VALID_BOUNDARY_KINDS = frozenset(
    {"prBoundary", "areaBoundary", "snapBoundary", "clusterBoundary"}
)
VALID_BLOCKAGE_TYPES = frozenset(
    {
        "routing",
        "placement",
        "via",
        "pin",
        "fill",
        "slot",
        "feedthru",
        "screen",
        "area",
        "wiring",
    }
)
VALID_ACCESS_DIRS = frozenset({"top", "bottom", "left", "right", "any"})
VALID_PCELL_PARAM_TYPES = frozenset(
    {"string", "int", "float", "boolean", "cyclic", "radio"}
)
VALID_PROP_TYPES = frozenset({"string", "int", "float", "boolean"})
VALID_DEFINITION_KINDS = frozenset({"pcell_params", "via_params", "props"})

# Static PDK / EDA metadata keys stripped to prevent massive snapshot bloat
# (e.g., lxComponentType alone accounts for 45-65% of real TSMC snapshots).
STRIPPED_PDK_METADATA_KEYS = frozenset(
    {
        "lxComponentType",
        "description",
        "pdkVersion",
        "macroArgumentStyle",
        "lxInternal",
        "pul_schematic_instance_properties",
        "pul_layout_instance_properties",
        "lastSchematicExtraction",
        "schLastCheckTime",
        "schGeometryLastUpdated",
        "schGeometryVersion",
        "schVersion",
        "connUpdateTime",
        "connectivityLastUpdated",
        "instancesLastUpdated",
        "instancesLastChanged",
        "interfaceLastChanged",
        "lastSavedExtractCounter",
        "viewSubType",
        "pin#",
        "inst#",
        "instance#",
        "net#",
    }
)

# Virtuoso Layout Editor GUI/display settings stored on cv~>prop that carry no
# physical design intent and fluctuate across interactive sessions.
STRIPPED_CELLVIEW_DISPLAY_PROPS = frozenset(
    {
        "accessEdgesOn",
        "arrayDisplay",
        "autoZoomMode",
        "autoZoomPan",
        "autoZoomScale",
        "cursorShape",
        "dimGridLines",
        "dimmingAutoEnabled",
        "dimmingIntensity",
        "dimmingOn",
        "dimmingScope",
        "displayFigGroupName",
        "displayInstanceOrientation",
        "displayPRBoundaryAtCurrentLevelOnly",
        "displayPRHighestResolution",
        "displayPinNames",
        "displayResolution",
        "displayRowName",
        "displayRowSite",
        "displaySelectionHighlightWhenDimmed",
        "displayStretchHandles",
        "displayViaShapes",
        "dotStyleMajorGrid",
        "dotsOn",
        "drawAxesOn",
        "drawDottedGridOn",
        "drawGridOn",
        "drawInstancePins",
        "drawSurroundingOn",
        "drawTraversedInstBbox",
        "dynamicHilightOn",
        "filterSize",
        "filterSizeDrawingStyle",
        "ghostOfDraggedObjects",
        "gridMultiple",
        "gridSpacing",
        "iconsOn",
        "instLabel",
        "instanceDrawingMode",
        "leDispNetExpr",
        "lppVisibilityMode",
        "mergeEnable",
        "mergeScope",
        "netsOn",
        "openToStopLevel",
        "originMarkersOn",
        "pathCL",
        "scrollPercent",
        "segSnapMode",
        "selDisplayInstContent",
        "selectionHintOn",
        "slotEnable",
        "snapMode",
        "snapPatternDrawingStyle",
        "startLevel",
        "stopLevel",
        "textJustificationOn",
        "transparentGroup",
        "trimEnable",
        "useTrueBBox",
        "xSnapSpacing",
        "ySnapSpacing",
    }
)

# Layers / purposes classified as non-electrical annotations in semantic diff
# when not attached to an electrical net.
AUXILIARY_LAYERS = frozenset(
    {
        "text",
        "annotate",
        "border",
        "prBoundary",
        "marker",
        "hilite",
        "background",
        "grid",
        "device",
        "y0",
        "y1",
        "y2",
        "y3",
        "y4",
        "y5",
        "y6",
        "y7",
        "y8",
        "y9",
    }
)
AUXILIARY_PURPOSES = frozenset(
    {
        "label",
        "annotate",
        "boundary",
        "comment",
        "note",
        "marker",
        "warning",
        "error",
        "drawing0",
    }
)

Point2D = tuple[float, float]
BBox = tuple[Point2D, Point2D]
ViaParamValue = Union[str, int, float, bool, tuple[Union[int, float], ...]]


def is_stripped_metadata_key(key: str) -> bool:
    """Return True if *key* is static PDK bloat or Virtuoso GUI display noise."""

    return key in STRIPPED_PDK_METADATA_KEYS or key in STRIPPED_CELLVIEW_DISPLAY_PROPS


# ---------------------------------------------------------------------------
# Unified Manifest v2 (oa-git/2) Contract for Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutManifest:
    """Manifest v2 (``oa-git/2``) compatible with :mod:`oa_git.view_contract`."""

    schema: str
    view_type: Literal["layout"]
    cellview: CellViewRef
    content_sha256: str
    full_sha256: str
    files: dict[str, str]
    electrical_sha256: None = None
    placement_sha256: None = None
    omissions_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "view_type": self.view_type,
            "cellview": self.cellview.to_dict(),
            "electrical_sha256": self.electrical_sha256,
            "placement_sha256": self.placement_sha256,
            "content_sha256": self.content_sha256,
            "full_sha256": self.full_sha256,
            "files": dict(sorted(self.files.items())),
            "omissions_count": self.omissions_count,
            "metadata": dict(sorted(self.metadata.items())),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LayoutManifest:
        if not isinstance(data, dict):
            raise SchemaValidationError("manifest must be a JSON object")
        schema = str(data.get("schema", ""))
        if schema != LAYOUT_MANIFEST_SCHEMA:
            raise SchemaValidationError(
                f"Unsupported layout manifest schema {schema!r}; "
                f"expected {LAYOUT_MANIFEST_SCHEMA!r}"
            )
        view_type = data.get("view_type")
        if view_type != LAYOUT_VIEW_TYPE:
            raise SchemaValidationError(
                f"Expected view_type='layout', got {view_type!r}"
            )
        if data.get("electrical_sha256") is not None:
            raise SchemaValidationError(
                "Layout manifest electrical_sha256 must be null"
            )
        if data.get("placement_sha256") is not None:
            raise SchemaValidationError(
                "Layout manifest placement_sha256 must be null"
            )
        raw_content = data.get("content_sha256")
        if not isinstance(raw_content, str) or not raw_content:
            raise SchemaValidationError(
                "Layout manifest requires non-empty content_sha256"
            )
        raw_full = data.get("full_sha256")
        if not isinstance(raw_full, str) or not raw_full:
            raise SchemaValidationError(
                "Layout manifest requires non-empty full_sha256"
            )
        raw_omissions = data.get("omissions_count", 0)
        if type(raw_omissions) is not int or raw_omissions < 0:
            raise SchemaValidationError(
                f"Manifest omissions_count must be non-negative int, got {raw_omissions!r}"
            )
        raw_cv = data.get("cellview")
        if not isinstance(raw_cv, dict) or not {"library", "cell", "view"}.issubset(raw_cv):
            raise SchemaValidationError(
                "Layout manifest requires cellview object with library, cell, view"
            )
        raw_files = data.get("files")
        if not isinstance(raw_files, dict):
            raise SchemaValidationError("Layout manifest 'files' must be a JSON object")
        for k, v in raw_files.items():
            if type(k) is not str or type(v) is not str:
                raise SchemaValidationError("Layout manifest 'files' keys and values must be strings")
        raw_meta = data.get("metadata")
        if raw_meta is None:
            raw_meta = {}
        elif not isinstance(raw_meta, dict):
            raise SchemaValidationError("Layout manifest 'metadata' must be a JSON object")
        return cls(
            schema=schema,
            view_type="layout",
            cellview=CellViewRef.from_dict(raw_cv),
            content_sha256=raw_content,
            full_sha256=raw_full,
            files=dict(raw_files),
            electrical_sha256=None,
            placement_sha256=None,
            omissions_count=raw_omissions,
            metadata=dict(raw_meta),
        )


# ---------------------------------------------------------------------------
# Deduplicated Definition Records (definitions.jsonl)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutDefinition:
    """Content-addressed deduplicated definition stored in ``definitions.jsonl``."""

    def_id: str
    kind: Literal["pcell_params", "via_params", "props"]
    payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical OpenAccess Layout Objects (objects.jsonl)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutNet:
    """Physical layout electrical net."""

    name: str
    is_global: bool = False
    num_bits: int = 1
    sig_type: str = "signal"
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["net"] = field(default="net", init=False)


@dataclass(frozen=True)
class LayoutPin:
    """Layout terminal pin figure and connectivity contract."""

    name: str
    net: str
    layer: str
    purpose: str = "pin"
    pin_name: str = ""
    direction: str = "inputOutput"
    shape_kind: Literal["rect", "polygon", "ellipse", "path_seg", "path"] = "rect"
    bbox: BBox | None = None
    points: tuple[Point2D, ...] = ()
    begin_pt: Point2D | None = None
    end_pt: Point2D | None = None
    width: float = 0.0
    begin_style: str = "truncate"
    end_style: str = "truncate"
    begin_ext: float = 0.0
    end_ext: float = 0.0
    custom_style: tuple[float, ...] = ()
    path_style: str = "truncateExtend"
    access_dir: tuple[str, ...] = ()
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["pin"] = field(default="pin", init=False)


@dataclass(frozen=True)
class LayoutRect:
    """Axis-aligned layout rectangle on ``(layer, purpose)``."""

    layer: str
    purpose: str
    bbox: BBox
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["rect"] = field(default="rect", init=False)


@dataclass(frozen=True)
class LayoutPolygon:
    """Closed polygon on ``(layer, purpose)``."""

    layer: str
    purpose: str
    points: tuple[Point2D, ...]
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["polygon"] = field(default="polygon", init=False)


@dataclass(frozen=True)
class LayoutPath:
    """Multi-segment OA path on ``(layer, purpose)``."""

    layer: str
    purpose: str
    points: tuple[Point2D, ...]
    width: float
    path_style: str = "truncateExtend"
    begin_ext: float = 0.0
    end_ext: float = 0.0
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["path"] = field(default="path", init=False)


@dataclass(frozen=True)
class LayoutPathSeg:
    """Two-point OA pathSeg (routing segment) on ``(layer, purpose)``."""

    layer: str
    purpose: str
    begin_pt: Point2D
    end_pt: Point2D
    width: float
    begin_style: str = "truncate"
    end_style: str = "truncate"
    begin_ext: float = 0.0
    end_ext: float = 0.0
    custom_style: tuple[float, ...] = ()
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["path_seg"] = field(default="path_seg", init=False)


@dataclass(frozen=True)
class LayoutEllipse:
    """OA ellipse shape defined by bounding box."""

    layer: str
    purpose: str
    bbox: BBox
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["ellipse"] = field(default="ellipse", init=False)


@dataclass(frozen=True)
class LayoutLine:
    """Open OA line polyline."""

    layer: str
    purpose: str
    points: tuple[Point2D, ...]
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["line"] = field(default="line", init=False)


@dataclass(frozen=True)
class LayoutVia:
    """OA ``stdVia`` or ``customVia`` with typed override parameters."""

    via_def: str
    origin: Point2D
    orient: str = "R0"
    via_kind: Literal["stdVia", "customVia"] = "stdVia"
    net: str | None = None
    name: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["via"] = field(default="via", init=False)


@dataclass(frozen=True)
class LayoutLabel:
    """Layout text label (normal, NLP, or IL label)."""

    text: str
    xy: Point2D
    layer: str
    purpose: str = "drawing"
    justify: str = "centerCenter"
    orient: str = "R0"
    font: str = "stick"
    height: float = 0.1
    label_type: str = "normalLabel"
    net: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["label"] = field(default="label", init=False)


@dataclass(frozen=True)
class LayoutInstance:
    """Hierarchical or PCell layout instance."""

    name: str
    lib: str
    cell: str
    view: str = "layout"
    xy: Point2D = (0.0, 0.0)
    orient: str = "R0"
    is_pcell: bool = False
    pcell_params: dict[str, ParamValue] = field(default_factory=dict)
    pcell_param_types: dict[str, str] = field(default_factory=dict)
    connections: dict[str, str] = field(default_factory=dict)
    props: dict[str, ParamValue] = field(default_factory=dict)
    prop_types: dict[str, str] = field(default_factory=dict)
    obj_type: Literal["instance"] = field(default="instance", init=False)


@dataclass(frozen=True)
class LayoutMosaic:
    """Simple array (mosaic) layout instance."""

    name: str
    lib: str
    cell: str
    view: str = "layout"
    xy: Point2D = (0.0, 0.0)
    orient: str = "R0"
    rows: int = 1
    columns: int = 1
    u_x: float = 0.0
    u_y: float = 0.0
    is_pcell: bool = False
    pcell_params: dict[str, ParamValue] = field(default_factory=dict)
    pcell_param_types: dict[str, str] = field(default_factory=dict)
    props: dict[str, ParamValue] = field(default_factory=dict)
    prop_types: dict[str, str] = field(default_factory=dict)
    obj_type: Literal["mosaic"] = field(default="mosaic", init=False)


@dataclass(frozen=True)
class LayoutBlockage:
    """Routing, placement, via, fill, or area blockage."""

    blockage_type: str
    points: tuple[Point2D, ...]
    layer: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["blockage"] = field(default="blockage", init=False)


@dataclass(frozen=True)
class LayoutBoundary:
    """PR, area, snap, or cluster boundary."""

    boundary_kind: Literal[
        "prBoundary", "areaBoundary", "snapBoundary", "clusterBoundary"
    ]
    points: tuple[Point2D, ...]
    name: str | None = None
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["boundary"] = field(default="boundary", init=False)


@dataclass(frozen=True)
class LayoutFigGroup:
    """Named layout figure group container."""

    name: str
    origin: Point2D = (0.0, 0.0)
    orient: str = "R0"
    group_type: str = "default"
    props: dict[str, ParamValue] = field(default_factory=dict)
    obj_type: Literal["fig_group"] = field(default="fig_group", init=False)


LayoutObject = Union[
    LayoutNet,
    LayoutPin,
    LayoutInstance,
    LayoutMosaic,
    LayoutVia,
    LayoutRect,
    LayoutPathSeg,
    LayoutPath,
    LayoutPolygon,
    LayoutEllipse,
    LayoutLine,
    LayoutLabel,
    LayoutBlockage,
    LayoutBoundary,
    LayoutFigGroup,
]


@dataclass(frozen=True)
class LayoutSnapshot:
    """Complete canonical snapshot of an OpenAccess layout CellView."""

    manifest: LayoutManifest
    objects: tuple[LayoutObject, ...] = ()
    cell_properties: dict[str, ParamValue] = field(default_factory=dict)
    omissions: tuple[OmissionRecord, ...] = ()

    def objects_by_type(self, obj_type: str) -> tuple[LayoutObject, ...]:
        return tuple(obj for obj in self.objects if obj.obj_type == obj_type)


@dataclass(frozen=True)
class LayoutRestoreReceipt:
    """Structured evidence produced by an OA layout restore operation."""

    target: CellViewRef
    published: bool
    draft_cell: str | None = None
    pcell_verified: bool = False
    connectivity_verified: bool = False
    readback_verified: bool = False
    object_counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    transport: str = "one_shot"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": self.target.to_dict(),
            "published": self.published,
            "draft_cell": self.draft_cell,
            "pcell_verified": self.pcell_verified,
            "connectivity_verified": self.connectivity_verified,
            "readback_verified": self.readback_verified,
            "object_counts": dict(sorted(self.object_counts.items())),
            "details": dict(sorted(self.details.items())),
        }
        if self.transport != "one_shot":
            payload["transport"] = self.transport
        return payload


__all__ = [
    "AUXILIARY_LAYERS",
    "AUXILIARY_PURPOSES",
    "BBox",
    "BridgeExecutionError",
    "CdfParameterError",
    "CellViewRef",
    "DisallowedLibraryError",
    "FailClosedError",
    "LAYOUT_MANIFEST_SCHEMA",
    "LAYOUT_OBJECT_SCHEMA",
    "LAYOUT_VIEW_TYPE",
    "LayoutBlockage",
    "LayoutBoundary",
    "LayoutDefinition",
    "LayoutEllipse",
    "LayoutFigGroup",
    "LayoutInstance",
    "LayoutLabel",
    "LayoutLine",
    "LayoutManifest",
    "LayoutMosaic",
    "LayoutNet",
    "LayoutObject",
    "LayoutPath",
    "LayoutPathSeg",
    "LayoutPin",
    "LayoutPolygon",
    "LayoutRect",
    "LayoutRestoreReceipt",
    "LayoutSnapshot",
    "LayoutVia",
    "LiveLockError",
    "OaGitError",
    "OmissionRecord",
    "ParamValue",
    "Point2D",
    "RoundtripMismatchError",
    "STRIPPED_CELLVIEW_DISPLAY_PROPS",
    "STRIPPED_PDK_METADATA_KEYS",
    "SchemaValidationError",
    "SnapshotIntegrityError",
    "UnsupportedObjectError",
    "VALID_ACCESS_DIRS",
    "VALID_BLOCKAGE_TYPES",
    "VALID_BOUNDARY_KINDS",
    "VALID_DEFINITION_KINDS",
    "VALID_LABEL_TYPES",
    "VALID_ORIENTS",
    "VALID_PATHSEG_STYLES",
    "VALID_PATH_STYLES",
    "VALID_PCELL_PARAM_TYPES",
    "VALID_PIN_SHAPE_KINDS",
    "VALID_PORT_DIRECTIONS",
    "VALID_PROP_TYPES",
    "VALID_SIG_TYPES",
    "VALID_VIA_KINDS",
    "ViaParamValue",
    "is_stripped_metadata_key",
]
