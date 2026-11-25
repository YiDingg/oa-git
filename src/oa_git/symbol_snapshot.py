"""Deterministic canonical codec, v2 manifest integration, wire helpers, and ViewHandler for Symbol snapshots."""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .model import (
    SCHEMA_VERSION_V2,
    SYMBOL_SCHEMA_V1,
    VALID_ORIENTS,
    VALID_PORT_DIRECTIONS,
    BridgeExecutionError,
    CellViewRef,
    DisallowedLibraryError,
    FailClosedError,
    LiveLockError,
    Manifest,
    OmissionRecord,
    ParamValue,
    RestoreReceipt,
    RoundtripMismatchError,
    SchemaValidationError,
    SnapshotIntegrityError,
    UnsupportedObjectError,
    ViewType,
)
from .symbol_model import (
    BBox,
    Point2D,
    SymbolGraphics,
    SymbolLabel,
    SymbolLabelType,
    SymbolPin,
    SymbolPinFigure,
    SymbolShape,
    SymbolSnapshot,
    VALID_PIN_FIGURE_KINDS,
    VALID_SYMBOL_LABEL_TYPES,
    VALID_SYMBOL_SHAPE_KINDS,
)
from .view_contract import (
    build_manifest_v2,
    register_view_handler,
    verify_manifest_v2_integrity,
)


# ---------------------------------------------------------------------------
# Strict Validation & Quantization Primitives
# ---------------------------------------------------------------------------


def _error(message: str) -> SchemaValidationError:
    return SchemaValidationError(message)


def _normalize_float(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(f"{path} must be a mapping")
    for key in value:
        if type(key) is not str:
            raise _error(f"{path} keys must be strings")
    return value


def _require_sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _error(f"{path} must be a sequence")
    return value


def _check_keys(
    value: Mapping[str, Any],
    allowed: set[str],
    required: set[str],
    path: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise _error(f"{path} contains unsupported keys: {sorted(unknown)!r}")
    missing = required - set(value)
    if missing:
        raise _error(f"{path} is missing required keys: {sorted(missing)!r}")


def _require_text(
    value: Any,
    path: str,
    *,
    nonempty: bool = True,
    reject_control: bool = False,
) -> str:
    if type(value) is not str:
        raise _error(f"{path} must be a string")
    if nonempty and not value.strip():
        raise _error(f"{path} must not be empty")
    if reject_control and any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value):
        raise _error(f"{path} contains disallowed control characters: {value!r}")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise _error(f"{path} must be a bool")
    return value


def _require_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise _error(f"{path} must be an int")
    if minimum is not None and value < minimum:
        raise _error(f"{path} must be >= {minimum}")
    return value


def _require_finite_number(value: Any, path: str) -> int | float:
    if type(value) not in (int, float):
        raise _error(f"{path} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise _error(f"{path} must be finite")
    return value


def quantize_symbol_coord(value: float, precision: int = 6) -> float:
    """Quantize one finite coordinate to 6 decimals and normalize ``-0.0`` to ``0.0``."""

    if type(precision) is not int or precision < 0:
        raise _error("precision must be a non-negative int")
    number = _require_finite_number(value, "coordinate")
    try:
        as_float = float(number)
    except (OverflowError, ValueError) as exc:
        raise _error("coordinate must be representable as a finite float") from exc
    if not math.isfinite(as_float):
        raise _error("coordinate must be finite")
    return _normalize_float(float(round(as_float, precision)))


def quantize_symbol_xy(xy: Any, precision: int = 6, *, path: str = "xy") -> Point2D:
    """Quantize a 2D point ``(x, y)`` to 6 decimal places."""

    values = _require_sequence(xy, path)
    if len(values) != 2:
        raise _error(f"{path} must contain exactly two coordinates")
    return (
        quantize_symbol_coord(values[0], precision),
        quantize_symbol_coord(values[1], precision),
    )


def quantize_symbol_bbox(
    bbox: Any,
    precision: int = 6,
    *,
    path: str = "bbox",
    normalize_order: bool = True,
) -> BBox:
    """Quantize a 2-point bounding box.

    When ``normalize_order=True`` (axis-aligned boxes such as ``rect``, ``ellipse``,
    ``selection_box``), points are normalized to ``((min_x, min_y), (max_x, max_y))``.
    When ``normalize_order=False`` (ordered ``arc_bbox`` start/stop endpoints),
    point order is preserved verbatim.
    """

    values = _require_sequence(bbox, path)
    if len(values) != 2:
        raise _error(f"{path} must contain exactly two points")
    p0 = quantize_symbol_xy(values[0], precision, path=f"{path}[0]")
    p1 = quantize_symbol_xy(values[1], precision, path=f"{path}[1]")
    if not normalize_order:
        return (p0, p1)
    return (
        (min(p0[0], p1[0]), min(p0[1], p1[1])),
        (max(p0[0], p1[0]), max(p0[1], p1[1])),
    )


def _require_orientation(value: Any, path: str) -> str:
    orient = _require_text(value, path)
    if orient not in VALID_ORIENTS:
        raise _error(f"{path} has unsupported orientation {orient!r}")
    return orient


def _require_direction(value: Any, path: str) -> str:
    direction = _require_text(value, path)
    if direction not in VALID_PORT_DIRECTIONS:
        raise _error(f"{path} has unsupported port direction {direction!r}")
    return direction


def _canonical_value(
    value: Any, path: str, _active_ids: set[int] | None = None
) -> Any:
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error(f"{path} must contain only finite numbers")
        return _normalize_float(value)
    if type(value) is str:
        return value
    if isinstance(value, (Mapping, list, tuple)):
        active = _active_ids if _active_ids is not None else set()
        obj_id = id(value)
        if obj_id in active:
            raise _error(f"{path} contains a cyclic container reference")
        active.add(obj_id)
        try:
            if isinstance(value, Mapping):
                out: dict[str, Any] = {}
                keys = list(value)
                if any(type(k) is not str for k in keys):
                    raise _error(f"{path} keys must be strings")
                for k in sorted(keys):
                    out[k] = _canonical_value(value[k], f"{path}.{k}", active)
                return out
            return [
                _canonical_value(item, f"{path}[{idx}]", active)
                for idx, item in enumerate(value)
            ]
        finally:
            active.remove(obj_id)
    raise _error(f"{path} contains unsupported value type {type(value).__name__}")


def _canonicalize_scalar_property_value(item: Any, path: str) -> ParamValue:
    if type(item) not in (str, bool, int, float):
        raise _error(f"{path} must be a scalar ParamValue")
    if type(item) is float and not math.isfinite(item):
        raise _error(f"{path} must be finite")
    return _normalize_float(item) if type(item) is float else item


def canonicalize_symbol_properties(
    value: Any, *, path: str = "properties"
) -> dict[str, Any]:
    """Validate and sort cellview properties (scalars and scalar ILList sequences)."""

    mapping = _require_mapping(value, path)
    out: dict[str, Any] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        item = mapping[key]
        if type(item) in (list, tuple):
            out[key] = [
                _canonicalize_scalar_property_value(elem, f"{path}.{key}[{idx}]")
                for idx, elem in enumerate(item)
            ]
        else:
            out[key] = _canonicalize_scalar_property_value(item, f"{path}.{key}")
    return out


def canonicalize_port_order(
    port_order: Any, *, path: str = "port_order"
) -> tuple[str, ...]:
    """Validate declared port order without altering its positional sequence."""

    seq = _require_sequence(port_order, path)
    seen: set[str] = set()
    out: list[str] = []
    for idx, item in enumerate(seq):
        name = _require_text(item, f"{path}[{idx}]", reject_control=True)
        if name in seen:
            raise _error(f"{path} contains duplicate port name {name!r}")
        seen.add(name)
        out.append(name)
    return tuple(out)


def _canonical_polygon_points(points: tuple[Point2D, ...]) -> tuple[Point2D, ...]:
    """Canonicalize a closed polygon ring across cyclic rotations and winding reversal."""

    ring = list(points)
    if len(ring) >= 4 and ring[0] == ring[-1]:
        ring.pop()
    n = len(ring)
    if n < 3:
        raise _error("polygon must contain at least 3 distinct ring vertices")
    candidates: list[tuple[Point2D, ...]] = []
    for base in (ring, list(reversed(ring))):
        for shift in range(n):
            candidates.append(tuple(base[shift:] + base[:shift]))
    return min(candidates)


# ---------------------------------------------------------------------------
# Domain Canonicalizers
# ---------------------------------------------------------------------------


def canonicalize_pin_figure(figure: SymbolPinFigure, path: str) -> SymbolPinFigure:
    if not isinstance(figure, SymbolPinFigure):
        raise _error(f"{path} must be a SymbolPinFigure")
    kind = "rect" if figure.kind == "rectangle" else figure.kind
    if kind not in VALID_PIN_FIGURE_KINDS:
        raise _error(f"{path}.kind has unsupported pin figure kind {figure.kind!r}")

    if kind == "inst":
        lib = _require_text(figure.lib, f"{path}.lib", reject_control=True)
        cell = _require_text(figure.cell, f"{path}.cell", reject_control=True)
        view = _require_text(figure.view, f"{path}.view", reject_control=True)
        bbox = (
            None
            if figure.bbox is None
            else quantize_symbol_bbox(figure.bbox, path=f"{path}.bbox")
        )
        return SymbolPinFigure(
            kind="inst",
            layer=_require_text(figure.layer, f"{path}.layer"),
            purpose=_require_text(figure.purpose, f"{path}.purpose"),
            bbox=bbox,
            lib=lib,
            cell=cell,
            view=view,
            points=(),
        )

    layer = _require_text(figure.layer, f"{path}.layer")
    purpose = _require_text(figure.purpose, f"{path}.purpose")
    if figure.lib is not None or figure.cell is not None or figure.view is not None:
        raise _error(f"{path} with kind={kind!r} must not declare lib/cell/view master fields")
    bbox = (
        None
        if figure.bbox is None
        else quantize_symbol_bbox(figure.bbox, path=f"{path}.bbox")
    )
    points: tuple[Point2D, ...] = ()
    if kind == "polygon":
        raw_pts = _require_sequence(figure.points, f"{path}.points")
        if raw_pts:
            quantized = tuple(
                quantize_symbol_xy(p, path=f"{path}.points[{idx}]")
                for idx, p in enumerate(raw_pts)
            )
            points = _canonical_polygon_points(quantized)
    elif figure.points:
        raise _error(f"{path} with kind={kind!r} must not declare polygon points")

    return SymbolPinFigure(
        kind=kind,  # type: ignore[arg-type]
        layer=layer,
        purpose=purpose,
        bbox=bbox,
        lib=None,
        cell=None,
        view=None,
        points=points,
    )


def canonicalize_symbol_pins(pins: Sequence[SymbolPin]) -> tuple[SymbolPin, ...]:
    """Validate and sort symbol pins canonically by unique pin name."""

    seq = _require_sequence(pins, "pins")
    seen_names: set[str] = set()
    out: list[SymbolPin] = []
    for idx, pin in enumerate(seq):
        if not isinstance(pin, SymbolPin):
            raise _error(f"pins[{idx}] must be a SymbolPin")
        name = _require_text(pin.name, f"pins[{idx}].name", reject_control=True)
        if name != name.strip():
            raise _error(f"pins[{idx}].name must not have leading/trailing whitespace: {name!r}")
        if name in seen_names:
            raise _error(f"duplicate symbol pin name {name!r}")
        seen_names.add(name)
        direction = _require_direction(pin.direction, f"pins[{idx}].direction")
        xy = quantize_symbol_xy(pin.xy, path=f"pins[{idx}].xy")
        orient = _require_orientation(pin.orient, f"pins[{idx}].orient")
        pin_figure = canonicalize_pin_figure(pin.pin_figure, f"pins[{idx}].pin_figure")
        out.append(
            SymbolPin(
                name=name,
                direction=direction,
                xy=xy,
                orient=orient,
                pin_figure=pin_figure,
            )
        )
    return tuple(sorted(out, key=lambda p: p.name))


def canonicalize_symbol_shape(shape: SymbolShape, path: str) -> SymbolShape:
    if not isinstance(shape, SymbolShape):
        raise _error(f"{path} must be a SymbolShape")
    kind = "rect" if shape.kind == "rectangle" else shape.kind
    if kind not in VALID_SYMBOL_SHAPE_KINDS:
        raise _error(f"{path}.kind has unsupported shape kind {shape.kind!r}")
    layer = _require_text(shape.layer, f"{path}.layer")
    purpose = _require_text(shape.purpose, f"{path}.purpose")
    width = quantize_symbol_coord(shape.width)
    if width < 0.0:
        raise _error(f"{path}.width must be >= 0")
    fill = _require_bool(shape.fill, f"{path}.fill")

    if kind == "rect" and layer == "instance" and purpose == "drawing":
        raise _error(
            f"{path}: rect on instance:drawing is a selection_box and must not appear in graphics.shapes"
        )

    if kind in ("line", "path"):
        raw_pts = _require_sequence(shape.points, f"{path}.points")
        if len(raw_pts) < 2:
            raise _error(f"{path}.points must contain at least 2 points for {kind!r}")
        pts = tuple(
            quantize_symbol_xy(p, path=f"{path}.points[{idx}]")
            for idx, p in enumerate(raw_pts)
        )
        pts = min(pts, tuple(reversed(pts)))
        return SymbolShape(
            kind=kind,  # type: ignore[arg-type]
            layer=layer,
            purpose=purpose,
            points=pts,
            width=width,
            fill=False,
            bbox=None,
            arc_bbox=None,
            ellipse_bbox=None,
        )

    if kind == "polygon":
        raw_pts = _require_sequence(shape.points, f"{path}.points")
        if len(raw_pts) < 3:
            raise _error(f"{path}.points must contain at least 3 points for 'polygon'")
        pts = tuple(
            quantize_symbol_xy(p, path=f"{path}.points[{idx}]")
            for idx, p in enumerate(raw_pts)
        )
        pts = _canonical_polygon_points(pts)
        return SymbolShape(
            kind="polygon",
            layer=layer,
            purpose=purpose,
            points=pts,
            width=0.0,
            fill=fill,
            bbox=None,
            arc_bbox=None,
            ellipse_bbox=None,
        )

    if kind == "rect":
        raw_bbox = shape.bbox
        if raw_bbox is None:
            raw_pts = _require_sequence(shape.points, f"{path}.points")
            if len(raw_pts) == 2:
                raw_bbox = (raw_pts[0], raw_pts[1])
            else:
                raise _error(f"{path} ('rect') requires bbox or 2 corner points")
        bbox = quantize_symbol_bbox(raw_bbox, path=f"{path}.bbox")
        return SymbolShape(
            kind="rect",
            layer=layer,
            purpose=purpose,
            points=(),
            width=0.0,
            fill=fill,
            bbox=bbox,
            arc_bbox=None,
            ellipse_bbox=None,
        )

    if kind in ("ellipse", "circle"):
        raw_bbox = shape.bbox if shape.bbox is not None else shape.ellipse_bbox
        if raw_bbox is None:
            raw_pts = _require_sequence(shape.points, f"{path}.points")
            if len(raw_pts) == 2:
                raw_bbox = (raw_pts[0], raw_pts[1])
            else:
                raise _error(f"{path} ({kind!r}) requires bbox")
        bbox = quantize_symbol_bbox(raw_bbox, path=f"{path}.bbox")
        return SymbolShape(
            kind=kind,  # type: ignore[arg-type]
            layer=layer,
            purpose=purpose,
            points=(),
            width=0.0,
            fill=fill,
            bbox=bbox,
            arc_bbox=None,
            ellipse_bbox=None,
        )

    # kind == "arc"
    if shape.arc_bbox is None or shape.ellipse_bbox is None:
        raise _error(f"{path} ('arc') requires both arc_bbox and ellipse_bbox")
    arc_bbox = quantize_symbol_bbox(
        shape.arc_bbox, path=f"{path}.arc_bbox", normalize_order=False
    )
    ellipse_bbox = quantize_symbol_bbox(
        shape.ellipse_bbox, path=f"{path}.ellipse_bbox", normalize_order=True
    )
    return SymbolShape(
        kind="arc",
        layer=layer,
        purpose=purpose,
        points=(),
        width=0.0,
        fill=False,
        bbox=None,
        arc_bbox=arc_bbox,
        ellipse_bbox=ellipse_bbox,
    )


def _shape_sort_key(shape: SymbolShape) -> tuple[Any, ...]:
    return (
        shape.kind,
        shape.layer,
        shape.purpose,
        shape.points,
        shape.width,
        shape.fill,
        shape.bbox is not None,
        shape.bbox or ((0.0, 0.0), (0.0, 0.0)),
        shape.arc_bbox is not None,
        shape.arc_bbox or ((0.0, 0.0), (0.0, 0.0)),
        shape.ellipse_bbox is not None,
        shape.ellipse_bbox or ((0.0, 0.0), (0.0, 0.0)),
    )


def canonicalize_symbol_label(
    label: SymbolLabel,
    path: str,
    *,
    expected_type: SymbolLabelType,
) -> SymbolLabel:
    if not isinstance(label, SymbolLabel):
        raise _error(f"{path} must be a SymbolLabel")
    ltype = label.label_type
    if ltype not in VALID_SYMBOL_LABEL_TYPES:
        raise _error(f"{path}.label_type has unsupported value {ltype!r}")
    if ltype != expected_type:
        raise _error(f"{path}.label_type must be {expected_type!r}, got {ltype!r}")
    height = quantize_symbol_coord(label.height)
    if height < 0.0:
        raise _error(f"{path}.height must be >= 0")
    return SymbolLabel(
        text=_require_text(label.text, f"{path}.text", nonempty=False),
        xy=quantize_symbol_xy(label.xy, path=f"{path}.xy"),
        layer=_require_text(label.layer, f"{path}.layer"),
        purpose=_require_text(label.purpose, f"{path}.purpose"),
        justify=_require_text(label.justify, f"{path}.justify"),
        orient=_require_orientation(label.orient, f"{path}.orient"),
        font=_require_text(label.font, f"{path}.font"),
        height=height,
        label_type=ltype,
    )


def _label_sort_key(label: SymbolLabel) -> tuple[Any, ...]:
    return (
        label.label_type,
        label.text,
        label.xy,
        label.layer,
        label.purpose,
        label.justify,
        label.orient,
        label.font,
        label.height,
    )


def canonicalize_symbol_graphics(graphics: SymbolGraphics) -> SymbolGraphics:
    """Validate, quantize, and deterministically order symbol graphics."""

    if not isinstance(graphics, SymbolGraphics):
        raise _error("graphics must be a SymbolGraphics")

    shapes = tuple(
        sorted(
            (
                canonicalize_symbol_shape(item, f"graphics.shapes[{idx}]")
                for idx, item in enumerate(
                    _require_sequence(graphics.shapes, "graphics.shapes")
                )
            ),
            key=_shape_sort_key,
        )
    )
    labels = tuple(
        sorted(
            (
                canonicalize_symbol_label(
                    item, f"graphics.labels[{idx}]", expected_type="normalLabel"
                )
                for idx, item in enumerate(
                    _require_sequence(graphics.labels, "graphics.labels")
                )
            ),
            key=_label_sort_key,
        )
    )
    nlp_labels = tuple(
        sorted(
            (
                canonicalize_symbol_label(
                    item, f"graphics.nlp_labels[{idx}]", expected_type="NLPLabel"
                )
                for idx, item in enumerate(
                    _require_sequence(graphics.nlp_labels, "graphics.nlp_labels")
                )
            ),
            key=_label_sort_key,
        )
    )
    il_labels = tuple(
        sorted(
            (
                canonicalize_symbol_label(
                    item, f"graphics.il_labels[{idx}]", expected_type="ILLabel"
                )
                for idx, item in enumerate(
                    _require_sequence(graphics.il_labels, "graphics.il_labels")
                )
            ),
            key=_label_sort_key,
        )
    )
    selection_box = (
        None
        if graphics.selection_box is None
        else quantize_symbol_bbox(
            graphics.selection_box, path="graphics.selection_box"
        )
    )
    origin = quantize_symbol_xy(graphics.origin, path="graphics.origin")
    bbox = (
        None
        if graphics.bbox is None
        else quantize_symbol_bbox(graphics.bbox, path="graphics.bbox")
    )
    return SymbolGraphics(
        shapes=shapes,
        labels=labels,
        nlp_labels=nlp_labels,
        il_labels=il_labels,
        selection_box=selection_box,
        origin=origin,
        bbox=bbox,
    )


def _canonicalize_omissions(
    omissions: Sequence[OmissionRecord],
) -> tuple[OmissionRecord, ...]:
    seq = _require_sequence(omissions, "omissions")
    records: list[OmissionRecord] = []
    for idx, item in enumerate(seq):
        if not isinstance(item, OmissionRecord):
            raise _error(f"omissions[{idx}] must be an OmissionRecord")
        details = _canonical_value(
            _require_mapping(item.details, f"omissions[{idx}].details"),
            f"omissions[{idx}].details",
        )
        records.append(
            OmissionRecord(
                kind=_require_text(item.kind, f"omissions[{idx}].kind"),
                identifier=_require_text(item.identifier, f"omissions[{idx}].identifier"),
                reason=_require_text(item.reason, f"omissions[{idx}].reason"),
                details=details,
            )
        )
    return tuple(
        sorted(
            records,
            key=lambda r: (
                r.kind,
                r.identifier,
                r.reason,
                json.dumps(r.details, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            ),
        )
    )


# ---------------------------------------------------------------------------
# YAML & JSON Serialization
# ---------------------------------------------------------------------------


def _pin_figure_to_dict(fig: SymbolPinFigure) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": fig.kind}
    if fig.kind == "inst":
        out["lib"] = fig.lib
        out["cell"] = fig.cell
        out["view"] = fig.view
        out["layer"] = fig.layer
        out["purpose"] = fig.purpose
        if fig.bbox is not None:
            out["bbox"] = [list(fig.bbox[0]), list(fig.bbox[1])]
    else:
        out["layer"] = fig.layer
        out["purpose"] = fig.purpose
        if fig.bbox is not None:
            out["bbox"] = [list(fig.bbox[0]), list(fig.bbox[1])]
        if fig.points:
            out["points"] = [list(p) for p in fig.points]
    return out


def _shape_to_dict(shape: SymbolShape) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": shape.kind,
        "layer": shape.layer,
        "purpose": shape.purpose,
    }
    if shape.kind in ("line", "path", "polygon"):
        out["points"] = [list(p) for p in shape.points]
    if shape.kind in ("line", "path") and shape.width != 0.0:
        out["width"] = shape.width
    elif shape.kind == "path":
        out["width"] = shape.width
    if shape.kind in ("rect", "ellipse", "circle") and shape.bbox is not None:
        out["bbox"] = [list(shape.bbox[0]), list(shape.bbox[1])]
    if shape.kind == "arc":
        assert shape.arc_bbox is not None and shape.ellipse_bbox is not None
        out["arc_bbox"] = [list(shape.arc_bbox[0]), list(shape.arc_bbox[1])]
        out["ellipse_bbox"] = [
            list(shape.ellipse_bbox[0]),
            list(shape.ellipse_bbox[1]),
        ]
    if shape.fill:
        out["fill"] = True
    return out


def _label_to_dict(label: SymbolLabel) -> dict[str, Any]:
    return {
        "text": label.text,
        "xy": list(label.xy),
        "layer": label.layer,
        "purpose": label.purpose,
        "justify": label.justify,
        "orient": label.orient,
        "font": label.font,
        "height": label.height,
        "label_type": label.label_type,
    }


def symbol_to_dict(
    cellview: CellViewRef,
    pins: Sequence[SymbolPin],
    port_order: Sequence[str],
    graphics: SymbolGraphics,
    properties: Mapping[str, Any],
) -> dict[str, Any]:
    """Convert canonical symbol components into the ``symbol.yaml`` dict structure."""

    if not isinstance(cellview, CellViewRef):
        raise _error("cellview must be a CellViewRef")
    c_pins = canonicalize_symbol_pins(pins)
    c_port_order = canonicalize_port_order(port_order)
    c_graphics = canonicalize_symbol_graphics(graphics)
    c_props = canonicalize_symbol_properties(properties)

    graphics_dict: dict[str, Any] = {
        "shapes": [_shape_to_dict(s) for s in c_graphics.shapes],
        "labels": [_label_to_dict(lbl) for lbl in c_graphics.labels],
        "nlp_labels": [_label_to_dict(lbl) for lbl in c_graphics.nlp_labels],
        "il_labels": [_label_to_dict(lbl) for lbl in c_graphics.il_labels],
        "selection_box": (
            None
            if c_graphics.selection_box is None
            else [
                list(c_graphics.selection_box[0]),
                list(c_graphics.selection_box[1]),
            ]
        ),
        "origin": list(c_graphics.origin),
    }
    if c_graphics.bbox is not None:
        graphics_dict["bbox"] = [
            list(c_graphics.bbox[0]),
            list(c_graphics.bbox[1]),
        ]

    return {
        "schema": SYMBOL_SCHEMA_V1,
        "cellview": cellview.to_dict(),
        "pins": [
            {
                "name": pin.name,
                "direction": pin.direction,
                "xy": list(pin.xy),
                "orient": pin.orient,
                "pin_figure": _pin_figure_to_dict(pin.pin_figure),
            }
            for pin in c_pins
        ],
        "port_order": list(c_port_order),
        "graphics": graphics_dict,
        "properties": {
            k: list(v) if isinstance(v, list) else v for k, v in c_props.items()
        },
    }


def _yaml_dump(data: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )
    return rendered.rstrip("\n") + "\n"


def render_symbol_yaml(
    cellview: CellViewRef,
    pins: Sequence[SymbolPin],
    port_order: Sequence[str],
    graphics: SymbolGraphics,
    properties: Mapping[str, Any],
) -> str:
    """Render deterministic ``symbol.yaml`` text."""

    return _yaml_dump(symbol_to_dict(cellview, pins, port_order, graphics, properties))


def _render_omissions_json(omissions: tuple[OmissionRecord, ...]) -> str:
    records = _canonicalize_omissions(omissions)
    rendered = json.dumps(
        [r.to_dict() for r in records],
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return rendered.rstrip("\n") + "\n"


def _render_manifest_json(manifest: Manifest) -> str:
    rendered = json.dumps(
        manifest.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return rendered.rstrip("\n") + "\n"


def _pin_figure_from_dict(data: Any, path: str) -> SymbolPinFigure:
    mapping = _require_mapping(data, path)
    _check_keys(
        mapping,
        {"kind", "layer", "purpose", "bbox", "lib", "cell", "view", "points"},
        set(),
        path,
    )
    raw_kind = mapping.get("kind")
    if raw_kind is None:
        raw_kind = "inst" if ("lib" in mapping or "cell" in mapping) else "rect"
    kind = _require_text(raw_kind, f"{path}.kind")
    if kind == "rectangle":
        kind = "rect"
    raw_bbox = mapping.get("bbox")
    bbox = (
        None
        if raw_bbox is None
        else quantize_symbol_bbox(raw_bbox, path=f"{path}.bbox")
    )
    raw_points = mapping.get("points", ())
    pts_seq = _require_sequence(raw_points, f"{path}.points")
    points = tuple(
        quantize_symbol_xy(p, path=f"{path}.points[{idx}]")
        for idx, p in enumerate(pts_seq)
    )
    lib = mapping.get("lib")
    cell = mapping.get("cell")
    view = mapping.get("view")
    if lib is not None:
        lib = _require_text(lib, f"{path}.lib", reject_control=True)
    if cell is not None:
        cell = _require_text(cell, f"{path}.cell", reject_control=True)
    if view is not None:
        view = _require_text(view, f"{path}.view", reject_control=True)
    return canonicalize_pin_figure(
        SymbolPinFigure(
            kind=kind,  # type: ignore[arg-type]
            layer=_require_text(mapping.get("layer", "pin"), f"{path}.layer"),
            purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
            bbox=bbox,
            lib=lib,
            cell=cell,
            view=view,
            points=points,
        ),
        path,
    )


def _pin_from_dict(data: Any, path: str) -> SymbolPin:
    mapping = _require_mapping(data, path)
    _check_keys(
        mapping,
        {"name", "direction", "xy", "orient", "pin_figure"},
        {"name", "direction", "xy"},
        path,
    )
    fig_raw = mapping.get("pin_figure")
    pin_figure = (
        SymbolPinFigure()
        if fig_raw is None
        else _pin_figure_from_dict(fig_raw, f"{path}.pin_figure")
    )
    return SymbolPin(
        name=_require_text(mapping["name"], f"{path}.name", reject_control=True),
        direction=_require_direction(mapping["direction"], f"{path}.direction"),
        xy=quantize_symbol_xy(mapping["xy"], path=f"{path}.xy"),
        orient=_require_orientation(mapping.get("orient", "R0"), f"{path}.orient"),
        pin_figure=pin_figure,
    )


def _shape_from_dict(data: Any, path: str) -> SymbolShape:
    mapping = _require_mapping(data, path)
    _check_keys(
        mapping,
        {
            "kind",
            "layer",
            "purpose",
            "points",
            "width",
            "fill",
            "bbox",
            "arc_bbox",
            "ellipse_bbox",
        },
        {"kind", "layer"},
        path,
    )
    raw_kind = _require_text(mapping["kind"], f"{path}.kind")
    raw_pts = _require_sequence(mapping.get("points", ()), f"{path}.points")
    pts = tuple(
        quantize_symbol_xy(p, path=f"{path}.points[{idx}]")
        for idx, p in enumerate(raw_pts)
    )
    raw_bbox = mapping.get("bbox")
    raw_arc_bbox = mapping.get("arc_bbox")
    raw_ellipse_bbox = mapping.get("ellipse_bbox")
    return canonicalize_symbol_shape(
        SymbolShape(
            kind=raw_kind,  # type: ignore[arg-type]
            layer=_require_text(mapping["layer"], f"{path}.layer"),
            purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
            points=pts,
            width=quantize_symbol_coord(mapping.get("width", 0.0)),
            fill=_require_bool(mapping.get("fill", False), f"{path}.fill"),
            bbox=(
                None
                if raw_bbox is None
                else quantize_symbol_bbox(raw_bbox, path=f"{path}.bbox")
            ),
            arc_bbox=(
                None
                if raw_arc_bbox is None
                else quantize_symbol_bbox(
                    raw_arc_bbox, path=f"{path}.arc_bbox", normalize_order=False
                )
            ),
            ellipse_bbox=(
                None
                if raw_ellipse_bbox is None
                else quantize_symbol_bbox(
                    raw_ellipse_bbox, path=f"{path}.ellipse_bbox", normalize_order=True
                )
            ),
        ),
        path,
    )


def _label_from_dict(
    data: Any, path: str, *, expected_type: SymbolLabelType
) -> SymbolLabel:
    mapping = _require_mapping(data, path)
    _check_keys(
        mapping,
        {
            "text",
            "xy",
            "layer",
            "purpose",
            "justify",
            "orient",
            "font",
            "height",
            "label_type",
        },
        {"text", "xy"},
        path,
    )
    raw_type = mapping.get("label_type", expected_type)
    ltype = _require_text(raw_type, f"{path}.label_type")
    if ltype not in VALID_SYMBOL_LABEL_TYPES:
        raise _error(f"{path}.label_type has unsupported value {ltype!r}")
    return canonicalize_symbol_label(
        SymbolLabel(
            text=_require_text(mapping["text"], f"{path}.text", nonempty=False),
            xy=quantize_symbol_xy(mapping["xy"], path=f"{path}.xy"),
            layer=_require_text(mapping.get("layer", "pin"), f"{path}.layer"),
            purpose=_require_text(mapping.get("purpose", "label"), f"{path}.purpose"),
            justify=_require_text(
                mapping.get("justify", "centerCenter"), f"{path}.justify"
            ),
            orient=_require_orientation(mapping.get("orient", "R0"), f"{path}.orient"),
            font=_require_text(mapping.get("font", "stick"), f"{path}.font"),
            height=quantize_symbol_coord(mapping.get("height", 0.0625)),
            label_type=ltype,  # type: ignore[arg-type]
        ),
        path,
        expected_type=expected_type,
    )


def _graphics_from_dict(data: Any, path: str = "graphics") -> SymbolGraphics:
    mapping = _require_mapping(data, path)
    _check_keys(
        mapping,
        {
            "shapes",
            "labels",
            "nlp_labels",
            "il_labels",
            "selection_box",
            "origin",
            "bbox",
        },
        set(),
        path,
    )
    shapes = tuple(
        _shape_from_dict(item, f"{path}.shapes[{idx}]")
        for idx, item in enumerate(
            _require_sequence(mapping.get("shapes", ()), f"{path}.shapes")
        )
    )
    labels = tuple(
        _label_from_dict(item, f"{path}.labels[{idx}]", expected_type="normalLabel")
        for idx, item in enumerate(
            _require_sequence(mapping.get("labels", ()), f"{path}.labels")
        )
    )
    nlp_labels = tuple(
        _label_from_dict(item, f"{path}.nlp_labels[{idx}]", expected_type="NLPLabel")
        for idx, item in enumerate(
            _require_sequence(mapping.get("nlp_labels", ()), f"{path}.nlp_labels")
        )
    )
    il_labels = tuple(
        _label_from_dict(item, f"{path}.il_labels[{idx}]", expected_type="ILLabel")
        for idx, item in enumerate(
            _require_sequence(mapping.get("il_labels", ()), f"{path}.il_labels")
        )
    )
    raw_sb = mapping.get("selection_box")
    selection_box = (
        None
        if raw_sb is None
        else quantize_symbol_bbox(raw_sb, path=f"{path}.selection_box")
    )
    raw_origin = mapping.get("origin", (0.0, 0.0))
    origin = quantize_symbol_xy(raw_origin, path=f"{path}.origin")
    raw_bbox = mapping.get("bbox")
    bbox = (
        None
        if raw_bbox is None
        else quantize_symbol_bbox(raw_bbox, path=f"{path}.bbox")
    )
    return canonicalize_symbol_graphics(
        SymbolGraphics(
            shapes=shapes,
            labels=labels,
            nlp_labels=nlp_labels,
            il_labels=il_labels,
            selection_box=selection_box,
            origin=origin,
            bbox=bbox,
        )
    )


def symbol_from_dict(
    data: Mapping[str, Any],
) -> tuple[
    CellViewRef,
    tuple[SymbolPin, ...],
    tuple[str, ...],
    SymbolGraphics,
    dict[str, Any],
]:
    """Parse and canonicalize a ``symbol.yaml`` mapping."""

    mapping = _require_mapping(data, "symbol")
    _check_keys(
        mapping,
        {"schema", "cellview", "pins", "port_order", "graphics", "properties"},
        {"schema", "cellview"},
        "symbol",
    )
    schema = _require_text(mapping["schema"], "symbol.schema")
    if schema != SYMBOL_SCHEMA_V1:
        raise _error(
            f"unsupported symbol schema {schema!r}; expected {SYMBOL_SCHEMA_V1!r}"
        )
    cv_raw = _require_mapping(mapping["cellview"], "symbol.cellview")
    _check_keys(cv_raw, {"library", "cell", "view"}, {"library", "cell", "view"}, "symbol.cellview")
    cellview = CellViewRef(
        library=_require_text(cv_raw["library"], "symbol.cellview.library"),
        cell=_require_text(cv_raw["cell"], "symbol.cellview.cell"),
        view=_require_text(cv_raw["view"], "symbol.cellview.view"),
    )
    pins = canonicalize_symbol_pins(
        tuple(
            _pin_from_dict(item, f"symbol.pins[{idx}]")
            for idx, item in enumerate(
                _require_sequence(mapping.get("pins", ()), "symbol.pins")
            )
        )
    )
    port_order = canonicalize_port_order(
        mapping.get("port_order", ()), path="symbol.port_order"
    )
    graphics = _graphics_from_dict(mapping.get("graphics", {}), path="symbol.graphics")
    properties = canonicalize_symbol_properties(
        mapping.get("properties", {}), path="symbol.properties"
    )
    return cellview, pins, port_order, graphics, properties


# ---------------------------------------------------------------------------
# Snapshot Builder & Atomic Symlink-Safe Disk Codec
# ---------------------------------------------------------------------------


def build_symbol_snapshot(
    cellview: CellViewRef,
    *,
    pins: Sequence[SymbolPin] = (),
    port_order: Sequence[str] = (),
    graphics: SymbolGraphics | None = None,
    properties: Mapping[str, Any] | None = None,
    omissions: Sequence[OmissionRecord] = (),
    metadata: Mapping[str, Any] | None = None,
) -> SymbolSnapshot:
    """Build a canonical SymbolSnapshot and its content-addressed ``oa-git/2`` manifest."""

    if not isinstance(cellview, CellViewRef):
        raise _error("cellview must be a CellViewRef")
    c_pins = canonicalize_symbol_pins(pins)
    c_port_order = canonicalize_port_order(port_order)
    c_graphics = canonicalize_symbol_graphics(
        graphics if graphics is not None else SymbolGraphics()
    )
    c_props = canonicalize_symbol_properties(properties if properties is not None else {})
    c_omissions = _canonicalize_omissions(omissions)
    c_meta = _canonical_value(
        _require_mapping(metadata if metadata is not None else {}, "metadata"),
        "metadata",
    )

    symbol_bytes = render_symbol_yaml(
        cellview, c_pins, c_port_order, c_graphics, c_props
    ).encode("utf-8")
    file_contents: dict[str, bytes] = {"symbol.yaml": symbol_bytes}
    if c_omissions:
        file_contents["omissions.json"] = _render_omissions_json(c_omissions).encode(
            "utf-8"
        )

    manifest = build_manifest_v2(
        view_type="symbol",
        cellview=cellview,
        file_contents=file_contents,
        omissions_count=len(c_omissions),
        metadata=c_meta,
    )
    return SymbolSnapshot(
        manifest=manifest,
        cellview=cellview,
        pins=c_pins,
        port_order=c_port_order,
        graphics=c_graphics,
        properties=c_props,
        omissions=c_omissions,
    )


def _canonical_symbol_snapshot(snapshot: SymbolSnapshot) -> SymbolSnapshot:
    if not isinstance(snapshot, SymbolSnapshot):
        raise _error("snapshot must be a SymbolSnapshot")
    if not isinstance(snapshot.manifest, Manifest):
        raise _error("snapshot.manifest must be a Manifest")
    if snapshot.manifest.view_type != "symbol":
        raise _error(
            f"snapshot manifest has unsupported view_type {snapshot.manifest.view_type!r}"
        )
    if snapshot.manifest.cellview != snapshot.cellview:
        raise _error("snapshot.cellview does not match snapshot.manifest.cellview")
    return build_symbol_snapshot(
        snapshot.cellview,
        pins=snapshot.pins,
        port_order=snapshot.port_order,
        graphics=snapshot.graphics,
        properties=snapshot.properties,
        omissions=snapshot.omissions,
        metadata=snapshot.manifest.metadata,
    )


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise SchemaValidationError("YAML mapping keys must be hashable") from exc
        if duplicate:
            raise SchemaValidationError(f"duplicate YAML mapping key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _read_regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SnapshotIntegrityError(f"missing or invalid snapshot file: {path.name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SnapshotIntegrityError(f"unable to read snapshot file {path}") from exc


def _load_yaml(path: Path) -> Any:
    raw = _read_regular_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaValidationError(f"{path.name} is not valid UTF-8") from exc
    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)
    except SchemaValidationError:
        raise
    except yaml.YAMLError as exc:
        raise SchemaValidationError(f"invalid YAML in {path.name}") from exc
    except RecursionError as exc:
        raise SchemaValidationError(f"cyclic YAML container in {path.name}") from exc


def _load_json(path: Path, *, integrity_errors: bool = False) -> Any:
    raw = _read_regular_bytes(path)
    err_cls: type[Exception] = (
        SnapshotIntegrityError if integrity_errors else SchemaValidationError
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise err_cls(f"{path.name} is not valid UTF-8") from exc

    def reject_constant(value: str) -> Any:
        raise err_cls(f"non-finite JSON constant {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise err_cls(f"duplicate JSON mapping key {k!r}")
            out[k] = v
        return out

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (SchemaValidationError, SnapshotIntegrityError):
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise err_cls(f"invalid JSON in {path.name}") from exc


def write_symbol_snapshot(snapshot: SymbolSnapshot, dest_dir: Path) -> Path:
    """Atomically write ``symbol.yaml``, ``manifest.json``, and optional ``omissions.json``."""

    if not isinstance(dest_dir, Path):
        raise _error("dest_dir must be a Path")
    canonical = _canonical_symbol_snapshot(snapshot)
    rendered_files: dict[str, str] = {
        "symbol.yaml": render_symbol_yaml(
            canonical.cellview,
            canonical.pins,
            canonical.port_order,
            canonical.graphics,
            canonical.properties,
        ),
        "manifest.json": _render_manifest_json(canonical.manifest),
    }
    if canonical.omissions:
        rendered_files["omissions.json"] = _render_omissions_json(canonical.omissions)

    if dest_dir.is_symlink():
        raise _error(f"snapshot destination must not be a symlink: {dest_dir}")
    if dest_dir.exists() and not dest_dir.is_dir():
        raise _error(f"snapshot destination is not a directory: {dest_dir}")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    managed_names = {"symbol.yaml", "omissions.json", "manifest.json"}
    if dest_dir.exists():
        for child in dest_dir.iterdir():
            if child.is_symlink():
                raise _error(f"snapshot destination contains a symlink: {child.name}")
            if child.name in managed_names and not child.is_file():
                raise _error(f"snapshot destination entry is not a file: {child.name}")

    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.staging-", dir=str(dest_dir.parent))
    )
    backup: Path | None = None
    try:
        for name, text in rendered_files.items():
            (staging / name).write_bytes(text.encode("utf-8"))

        if not dest_dir.exists():
            if dest_dir.is_symlink():
                raise _error(f"snapshot destination became a symlink: {dest_dir}")
            os.replace(staging, dest_dir)
            staging = None
            return dest_dir

        backup = Path(
            tempfile.mkdtemp(prefix=f".{dest_dir.name}.backup-", dir=str(dest_dir.parent))
        )
        existing_names = {
            name for name in managed_names if (dest_dir / name).exists()
        }
        for name in existing_names:
            shutil.copy2(dest_dir / name, backup / name)

        commit_names = ["symbol.yaml"]
        if canonical.omissions:
            commit_names.append("omissions.json")
        commit_names.append("manifest.json")
        replaced: list[str] = []
        try:
            for name in commit_names:
                os.replace(staging / name, dest_dir / name)
                replaced.append(name)
            if not canonical.omissions and "omissions.json" in existing_names:
                (dest_dir / "omissions.json").unlink()
        except BaseException:
            for name in reversed(replaced):
                old_path = backup / name
                new_path = dest_dir / name
                if old_path.exists():
                    os.replace(old_path, new_path)
                else:
                    new_path.unlink(missing_ok=True)
            if (
                not canonical.omissions
                and "omissions.json" in existing_names
                and not (dest_dir / "omissions.json").exists()
            ):
                os.replace(backup / "omissions.json", dest_dir / "omissions.json")
            raise
        return dest_dir
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def _omissions_from_json(data: Any) -> tuple[OmissionRecord, ...]:
    seq = _require_sequence(data, "omissions")
    records: list[OmissionRecord] = []
    for idx, item in enumerate(seq):
        mapping = _require_mapping(item, f"omissions[{idx}]")
        _check_keys(
            mapping,
            {"kind", "identifier", "reason", "details"},
            {"kind", "identifier", "reason"},
            f"omissions[{idx}]",
        )
        details = _canonical_value(
            _require_mapping(mapping.get("details", {}), f"omissions[{idx}].details"),
            f"omissions[{idx}].details",
        )
        records.append(
            OmissionRecord(
                kind=_require_text(mapping["kind"], f"omissions[{idx}].kind"),
                identifier=_require_text(
                    mapping["identifier"], f"omissions[{idx}].identifier"
                ),
                reason=_require_text(mapping["reason"], f"omissions[{idx}].reason"),
                details=details,
            )
        )
    return _canonicalize_omissions(tuple(records))


def read_symbol_snapshot(snapshot_dir: Path, *, verify: bool = True) -> SymbolSnapshot:
    """Read a symbol snapshot, validating ``oa-git/2`` manifest integrity and schema."""

    if type(verify) is not bool:
        raise _error("verify must be a bool")
    if not isinstance(snapshot_dir, Path) or snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise SnapshotIntegrityError(f"snapshot directory does not exist: {snapshot_dir}")

    manifest_raw = _load_json(snapshot_dir / "manifest.json", integrity_errors=True)
    if not isinstance(manifest_raw, Mapping):
        raise SchemaValidationError("manifest.json root must be a JSON object")
    manifest = Manifest.from_dict(dict(manifest_raw))
    if manifest.schema != SCHEMA_VERSION_V2:
        raise SchemaValidationError(
            f"symbol snapshot requires manifest schema {SCHEMA_VERSION_V2!r}, got {manifest.schema!r}"
        )
    if manifest.view_type != "symbol":
        raise SchemaValidationError(
            f"expected view_type='symbol', got {manifest.view_type!r}"
        )
    allowed_files = {"symbol.yaml", "omissions.json"}
    unknown_files = set(manifest.files) - allowed_files
    if unknown_files or "symbol.yaml" not in manifest.files:
        raise SnapshotIntegrityError(
            f"invalid manifest.files for symbol snapshot: {sorted(manifest.files)!r}"
        )

    if verify:
        verify_manifest_v2_integrity(snapshot_dir, manifest)

    symbol_data = _load_yaml(snapshot_dir / "symbol.yaml")
    cellview, pins, port_order, graphics, properties = symbol_from_dict(
        _require_mapping(symbol_data, "symbol.yaml")
    )
    if cellview != manifest.cellview:
        raise SnapshotIntegrityError(
            f"symbol.yaml cellview {cellview.key!r} does not match manifest cellview {manifest.cellview.key!r}"
        )

    if manifest.omissions_count > 0:
        omissions = _omissions_from_json(_load_json(snapshot_dir / "omissions.json"))
    else:
        omissions = ()
    if len(omissions) != manifest.omissions_count:
        raise SchemaValidationError(
            f"manifest omissions_count={manifest.omissions_count} does not match omissions.json"
        )

    return SymbolSnapshot(
        manifest=manifest,
        cellview=cellview,
        pins=pins,
        port_order=port_order,
        graphics=graphics,
        properties=properties,
        omissions=omissions,
    )


# ---------------------------------------------------------------------------
# SKILL Wire Helpers (capture_symbol & restore_symbol)
# ---------------------------------------------------------------------------


_SYMBOL_CAPTURE_KEYS = {
    "ok",
    "error",
    "error_code",
    "cellview",
    "modified_but_not_saved",
    "metadata",
    "pins",
    "port_order",
    "graphics",
    "properties",
    "omissions",
}


def raw_symbol_capture_to_snapshot(payload: Mapping[str, Any]) -> SymbolSnapshot:
    """Validate a ``capture_symbol`` wire JSON payload and build a canonical SymbolSnapshot."""

    data = _require_mapping(payload, "symbol capture response")
    if "ok" in data and not data["ok"]:
        detail = data.get("error") or "symbol capture backend returned ok=false"
        raise BridgeExecutionError(str(detail))
    if data.get("modified_but_not_saved") is True:
        raise FailClosedError(
            "symbol capture response reports modified_but_not_saved=true"
        )
    _check_keys(
        data,
        _SYMBOL_CAPTURE_KEYS,
        {"ok", "cellview", "modified_but_not_saved", "pins", "graphics"},
        "symbol capture response",
    )
    _require_bool(data["ok"], "symbol capture response.ok")
    _require_bool(
        data["modified_but_not_saved"],
        "symbol capture response.modified_but_not_saved",
    )
    if data.get("error") is not None:
        _require_text(data["error"], "symbol capture response.error", nonempty=False)

    cv_raw = _require_mapping(data["cellview"], "symbol capture response.cellview")
    _check_keys(
        cv_raw,
        {"library", "cell", "view"},
        {"library", "cell", "view"},
        "symbol capture response.cellview",
    )
    cellview = CellViewRef(
        library=_require_text(cv_raw["library"], "cellview.library"),
        cell=_require_text(cv_raw["cell"], "cellview.cell"),
        view=_require_text(cv_raw["view"], "cellview.view"),
    )
    pins = tuple(
        _pin_from_dict(item, f"capture.pins[{idx}]")
        for idx, item in enumerate(
            _require_sequence(data["pins"], "capture.pins")
        )
    )
    port_order = canonicalize_port_order(
        data.get("port_order", ()), path="capture.port_order"
    )
    graphics = _graphics_from_dict(data["graphics"], path="capture.graphics")
    properties = canonicalize_symbol_properties(
        data.get("properties", {}), path="capture.properties"
    )
    omissions = _omissions_from_json(data.get("omissions", ()))
    metadata = dict(
        _require_mapping(data.get("metadata", {}), "capture.metadata")
    )
    return build_symbol_snapshot(
        cellview,
        pins=pins,
        port_order=port_order,
        graphics=graphics,
        properties=properties,
        omissions=omissions,
        metadata=metadata,
    )


def _skill_property_type(value: Any) -> str:
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "string"
    if type(value) in (list, tuple):
        for idx, elem in enumerate(value):
            _canonicalize_scalar_property_value(elem, f"ILList[{idx}]")
        return "ILList"
    raise SchemaValidationError(
        f"symbol property value must be a scalar ParamValue or scalar list, got {type(value).__name__}"
    )


def build_symbol_restore_request_dict(
    snapshot: SymbolSnapshot,
    target: CellViewRef,
    *,
    draft_cell: str,
    overwrite: bool = False,
    allow_libraries: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a ``to_skill_literal``-ready request dict for ``restore_symbol.il``."""

    canonical = _canonical_symbol_snapshot(snapshot)
    if not isinstance(target, CellViewRef):
        raise _error("target must be a CellViewRef")
    if (
        type(draft_cell) is not str
        or not draft_cell.strip()
        or "/" in draft_cell
        or "\\" in draft_cell
    ):
        raise _error("draft_cell must be a non-empty cell name")
    if type(overwrite) is not bool:
        raise _error("overwrite must be a bool")
    try:
        libraries = tuple(allow_libraries)
    except TypeError as exc:
        raise _error("allow_libraries must be iterable") from exc
    if any(type(lib) is not str or not lib.strip() for lib in libraries):
        raise _error("allow_libraries must contain non-empty strings")

    sym_dict = symbol_to_dict(
        canonical.cellview,
        canonical.pins,
        canonical.port_order,
        canonical.graphics,
        canonical.properties,
    )
    props_triples = [
        [
            key,
            list(val) if type(val) in (list, tuple) else val,
            _skill_property_type(val),
        ]
        for key, val in sorted(canonical.properties.items())
    ]
    return {
        "action": "restore_symbol",
        "target": target.to_dict(),
        "draft_cell": draft_cell,
        "overwrite": overwrite,
        "allow_libraries": list(libraries),
        "pins": sym_dict["pins"],
        "port_order": sym_dict["port_order"],
        "graphics": sym_dict["graphics"],
        "properties": props_triples,
    }


_SYMBOL_RESTORE_ERROR_TYPES: dict[str, type[FailClosedError]] = {
    "disallowed_library": DisallowedLibraryError,
    "live_lock": LiveLockError,
    "unsupported_object": UnsupportedObjectError,
    "pin_contract_mismatch": RoundtripMismatchError,
    "symbol_check_failed": RoundtripMismatchError,
}

_SYMBOL_RESTORE_KEYS = {
    "ok",
    "error",
    "error_code",
    "target",
    "published",
    "draft_cell",
    "pin_contract_verified",
    "readback_verified",
    "pin_mismatches",
    "symbol_check",
}


def parse_symbol_restore_response(
    payload: Mapping[str, Any], target: CellViewRef
) -> RestoreReceipt:
    """Validate a ``restore_symbol`` response and map wire errors to Fail-Closed exceptions."""

    data = _require_mapping(payload, "symbol restore response")
    unknown = set(data) - _SYMBOL_RESTORE_KEYS
    if unknown:
        raise _error(
            f"symbol restore response contains unsupported keys: {sorted(unknown)!r}"
        )
    if "ok" not in data:
        raise _error("symbol restore response missing 'ok'")
    ok = _require_bool(data["ok"], "symbol restore response.ok")
    if not isinstance(target, CellViewRef):
        raise _error("target must be a CellViewRef")

    if not ok:
        raw_error = data.get("error")
        code = data.get("error_code")
        if isinstance(raw_error, Mapping):
            if code is None and isinstance(raw_error.get("code"), str):
                code = raw_error["code"]
            detail = str(raw_error.get("message") or raw_error)
        else:
            if raw_error is not None:
                _require_text(raw_error, "symbol restore response.error", nonempty=False)
            detail = str(raw_error) if raw_error else "symbol restore backend returned ok=false"
        if code is not None and type(code) is not str:
            raise _error("symbol restore response.error_code must be a string or null")
        err_cls = _SYMBOL_RESTORE_ERROR_TYPES.get(code or "", BridgeExecutionError)
        msg = f"{code}: {detail}" if code else detail
        raise err_cls(msg)

    cv_raw = _require_mapping(data.get("target"), "symbol restore response.target")
    _check_keys(
        cv_raw,
        {"library", "cell", "view"},
        {"library", "cell", "view"},
        "symbol restore response.target",
    )
    resp_target = CellViewRef(
        library=_require_text(cv_raw["library"], "target.library"),
        cell=_require_text(cv_raw["cell"], "target.cell"),
        view=_require_text(cv_raw["view"], "target.view"),
    )
    if resp_target != target:
        raise RoundtripMismatchError(
            f"symbol restore target {resp_target.key!r} does not match {target.key!r}"
        )
    published = _require_bool(
        data.get("published", False), "symbol restore response.published"
    )
    draft_cell = data.get("draft_cell")
    if draft_cell is not None:
        _require_text(draft_cell, "symbol restore response.draft_cell")
    pin_contract_verified = _require_bool(
        data.get("pin_contract_verified", False),
        "symbol restore response.pin_contract_verified",
    )
    readback_verified = _require_bool(
        data.get("readback_verified", False),
        "symbol restore response.readback_verified",
    )
    mismatches = _require_sequence(
        data.get("pin_mismatches", ()), "symbol restore response.pin_mismatches"
    )
    if mismatches:
        raise RoundtripMismatchError(
            f"symbol restore reported pin mismatches: {list(mismatches)!r}"
        )
    sym_check = _require_mapping(
        data.get("symbol_check", {}), "symbol restore response.symbol_check"
    )
    _check_keys(sym_check, {"errors", "warnings"}, set(), "symbol_check")
    check_errors = _require_int(sym_check.get("errors", 0), "symbol_check.errors", minimum=0)
    check_warnings = _require_int(
        sym_check.get("warnings", 0), "symbol_check.warnings", minimum=0
    )
    if check_errors > 0:
        raise RoundtripMismatchError(
            f"symbol restore check reported {check_errors} error(s)"
        )

    return RestoreReceipt(
        target=target,
        published=published,
        draft_cell=draft_cell,
        sch_check_errors=check_errors,
        sch_check_warnings=check_warnings,
        cdf_verified=False,
        readback_verified=readback_verified,
        details={
            "pin_contract_verified": pin_contract_verified,
            "pin_mismatches": list(mismatches),
        },
    )


# ---------------------------------------------------------------------------
# Roundtrip Verification
# ---------------------------------------------------------------------------


def _roundtrip_to_plain(value: Any) -> Any:
    """Recursively convert dataclasses, tuples, and mappings into JSON-serializable values."""

    if is_dataclass(value):
        return {
            f.name: _roundtrip_to_plain(getattr(value, f.name))
            for f in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(k): _roundtrip_to_plain(v)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_roundtrip_to_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class SymbolRoundtripIssue:
    domain: Literal["pin_contract", "pin_geometry", "graphics"]
    path: str
    expected: Any
    actual: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "path": self.path,
            "expected": _roundtrip_to_plain(self.expected),
            "actual": _roundtrip_to_plain(self.actual),
            "message": self.message,
        }


@dataclass(frozen=True)
class SymbolRoundtripReport:
    ok: bool
    pin_contract_ok: bool
    pin_geometry_ok: bool
    graphics_ok: bool
    issues: tuple[SymbolRoundtripIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pin_contract_ok": self.pin_contract_ok,
            "pin_geometry_ok": self.pin_geometry_ok,
            "graphics_ok": self.graphics_ok,
            "issues": [i.to_dict() for i in self.issues],
        }


def _coord_close(a: float, b: float, tol: float) -> bool:
    return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tol)


def _xy_close(a: Point2D, b: Point2D, tol: float) -> bool:
    return _coord_close(a[0], b[0], tol) and _coord_close(a[1], b[1], tol)


def _bbox_close(a: BBox | None, b: BBox | None, tol: float) -> bool:
    if a is None or b is None:
        return a is b
    return _xy_close(a[0], b[0], tol) and _xy_close(a[1], b[1], tol)


def _points_close(
    a: Sequence[Point2D], b: Sequence[Point2D], tol: float, *, allow_reverse: bool = False
) -> bool:
    if len(a) != len(b):
        return False
    if all(_xy_close(p, q, tol) for p, q in zip(a, b)):
        return True
    if allow_reverse and all(_xy_close(p, q, tol) for p, q in zip(a, reversed(b))):
        return True
    return False


def _polygon_close(a: Sequence[Point2D], b: Sequence[Point2D], tol: float) -> bool:
    if len(a) != len(b):
        return False
    n = len(a)
    if n == 0:
        return True
    b_list = list(b)
    for seq in (b_list, list(reversed(b_list))):
        for shift in range(n):
            rotated = seq[shift:] + seq[:shift]
            if all(_xy_close(p, q, tol) for p, q in zip(a, rotated)):
                return True
    return False


def _pin_figure_match(a: SymbolPinFigure, b: SymbolPinFigure, tol: float) -> bool:
    if (
        a.kind != b.kind
        or a.layer != b.layer
        or a.purpose != b.purpose
        or a.lib != b.lib
        or a.cell != b.cell
        or a.view != b.view
    ):
        return False
    if not _bbox_close(a.bbox, b.bbox, tol):
        return False
    if a.kind == "polygon":
        return _polygon_close(a.points, b.points, tol)
    return True


def _is_square_bbox(bbox: BBox | None, tol: float) -> bool:
    if bbox is None:
        return False
    dx = abs(bbox[1][0] - bbox[0][0])
    dy = abs(bbox[1][1] - bbox[0][1])
    return _coord_close(dx, dy, tol)


def _shape_match(a: SymbolShape, b: SymbolShape, tol: float) -> bool:
    if (
        a.layer != b.layer
        or a.purpose != b.purpose
        or a.fill != b.fill
        or not _coord_close(a.width, b.width, tol)
    ):
        return False
    if a.kind != b.kind:
        if {a.kind, b.kind} == {"circle", "ellipse"}:
            return (
                _bbox_close(a.bbox, b.bbox, tol)
                and _is_square_bbox(a.bbox, tol)
                and _is_square_bbox(b.bbox, tol)
            )
        return False
    if a.kind in ("line", "path"):
        return _points_close(a.points, b.points, tol, allow_reverse=True)
    if a.kind == "polygon":
        return _polygon_close(a.points, b.points, tol)
    if a.kind in ("rect", "ellipse", "circle"):
        return _bbox_close(a.bbox, b.bbox, tol)
    if a.kind == "arc":
        return _bbox_close(a.arc_bbox, b.arc_bbox, tol) and _bbox_close(
            a.ellipse_bbox, b.ellipse_bbox, tol
        )
    return False


def _label_match(a: SymbolLabel, b: SymbolLabel, tol: float) -> bool:
    return (
        a.label_type == b.label_type
        and a.text == b.text
        and a.layer == b.layer
        and a.purpose == b.purpose
        and a.justify == b.justify
        and a.orient == b.orient
        and a.font == b.font
        and _xy_close(a.xy, b.xy, tol)
        and _coord_close(a.height, b.height, tol)
    )


def _strict_scalar_equal(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if type(a) is list:
        return len(a) == len(b) and all(
            _strict_scalar_equal(left, right) for left, right in zip(a, b)
        )
    return a == b


def verify_symbol_roundtrip(
    expected: SymbolSnapshot,
    actual: SymbolSnapshot,
    *,
    coord_tol: float = 1e-4,
    raise_on_mismatch: bool = True,
) -> SymbolRoundtripReport:
    """Compare readback SymbolSnapshot against expected canonical snapshot."""

    if not math.isfinite(coord_tol) or coord_tol < 0:
        raise ValueError("coord_tol must be a finite non-negative number")

    exp = _canonical_symbol_snapshot(expected)
    act = _canonical_symbol_snapshot(actual)

    contract_issues: list[SymbolRoundtripIssue] = []
    geom_issues: list[SymbolRoundtripIssue] = []
    graphics_issues: list[SymbolRoundtripIssue] = []

    exp_pins = {p.name: p for p in exp.pins}
    act_pins = {p.name: p for p in act.pins}
    for name in sorted(set(exp_pins) | set(act_pins)):
        if name not in act_pins:
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract", f"pins[{name!r}]", exp_pins[name], None, "Missing pin"
                )
            )
            continue
        if name not in exp_pins:
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract",
                    f"pins[{name!r}]",
                    None,
                    act_pins[name],
                    "Unexpected pin",
                )
            )
            continue
        ep, ap = exp_pins[name], act_pins[name]
        if ep.direction != ap.direction:
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract",
                    f"pins[{name!r}].direction",
                    ep.direction,
                    ap.direction,
                    "Pin direction differs",
                )
            )
        if not _pin_figure_match(ep.pin_figure, ap.pin_figure, coord_tol):
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract",
                    f"pins[{name!r}].pin_figure",
                    ep.pin_figure,
                    ap.pin_figure,
                    "Pin figure differs",
                )
            )
        if not _xy_close(ep.xy, ap.xy, coord_tol):
            geom_issues.append(
                SymbolRoundtripIssue(
                    "pin_geometry",
                    f"pins[{name!r}].xy",
                    ep.xy,
                    ap.xy,
                    "Pin coordinates differ",
                )
            )
        if ep.orient != ap.orient:
            geom_issues.append(
                SymbolRoundtripIssue(
                    "pin_geometry",
                    f"pins[{name!r}].orient",
                    ep.orient,
                    ap.orient,
                    "Pin orientation differs",
                )
            )

    if exp.port_order != act.port_order:
        contract_issues.append(
            SymbolRoundtripIssue(
                "pin_contract",
                "port_order",
                exp.port_order,
                act.port_order,
                "portOrder sequence differs",
            )
        )

    for key in sorted(set(exp.properties) | set(act.properties)):
        if key not in act.properties:
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract",
                    f"properties[{key!r}]",
                    exp.properties[key],
                    None,
                    "Missing cellview property",
                )
            )
        elif key not in exp.properties:
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract",
                    f"properties[{key!r}]",
                    None,
                    act.properties[key],
                    "Unexpected cellview property",
                )
            )
        elif not _strict_scalar_equal(exp.properties[key], act.properties[key]):
            contract_issues.append(
                SymbolRoundtripIssue(
                    "pin_contract",
                    f"properties[{key!r}]",
                    exp.properties[key],
                    act.properties[key],
                    "Cellview property differs",
                )
            )

    if not _bbox_close(exp.graphics.selection_box, act.graphics.selection_box, coord_tol):
        graphics_issues.append(
            SymbolRoundtripIssue(
                "graphics",
                "graphics.selection_box",
                exp.graphics.selection_box,
                act.graphics.selection_box,
                "Selection box differs",
            )
        )
    if not _xy_close(exp.graphics.origin, act.graphics.origin, coord_tol):
        graphics_issues.append(
            SymbolRoundtripIssue(
                "graphics",
                "graphics.origin",
                exp.graphics.origin,
                act.graphics.origin,
                "Symbol origin differs",
            )
        )

    unmatched_shapes = list(act.graphics.shapes)
    for idx, es in enumerate(exp.graphics.shapes):
        for j, acs in enumerate(unmatched_shapes):
            if _shape_match(es, acs, coord_tol):
                unmatched_shapes.pop(j)
                break
        else:
            graphics_issues.append(
                SymbolRoundtripIssue(
                    "graphics", f"graphics.shapes[{idx}]", es, None, "Missing shape"
                )
            )
    for idx, extra in enumerate(unmatched_shapes):
        graphics_issues.append(
            SymbolRoundtripIssue(
                "graphics",
                f"graphics.shapes[extra:{idx}]",
                None,
                extra,
                "Unexpected shape",
            )
        )

    for bucket in ("labels", "nlp_labels", "il_labels"):
        exp_lbls: tuple[SymbolLabel, ...] = getattr(exp.graphics, bucket)
        act_lbls = list(getattr(act.graphics, bucket))
        for idx, el in enumerate(exp_lbls):
            for j, al in enumerate(act_lbls):
                if _label_match(el, al, coord_tol):
                    act_lbls.pop(j)
                    break
            else:
                graphics_issues.append(
                    SymbolRoundtripIssue(
                        "graphics",
                        f"graphics.{bucket}[{idx}]",
                        el,
                        None,
                        f"Missing {bucket} entry",
                    )
                )
        for idx, extra in enumerate(act_lbls):
            graphics_issues.append(
                SymbolRoundtripIssue(
                    "graphics",
                    f"graphics.{bucket}[extra:{idx}]",
                    None,
                    extra,
                    f"Unexpected {bucket} entry",
                )
            )

    all_issues = tuple(contract_issues + geom_issues + graphics_issues)
    report = SymbolRoundtripReport(
        ok=not all_issues,
        pin_contract_ok=not contract_issues,
        pin_geometry_ok=not geom_issues,
        graphics_ok=not graphics_issues,
        issues=all_issues,
    )
    if all_issues and raise_on_mismatch:
        preview = "; ".join(
            f"{i.domain}:{i.path} ({i.message})" for i in all_issues[:3]
        )
        if len(all_issues) > 3:
            preview += f"; ... and {len(all_issues) - 3} more"
        raise RoundtripMismatchError(
            f"Symbol roundtrip mismatch ({len(all_issues)} issue(s)): {preview}"
        )
    return report


# ---------------------------------------------------------------------------
# ViewHandler Implementation & Registration
# ---------------------------------------------------------------------------


class SymbolViewHandler:
    """ViewHandler plugin for Cadence OpenAccess ``schematicSymbol`` views."""

    view_type: ViewType = "symbol"
    capture_action: str = "capture_symbol"
    restore_action: str = "restore_symbol"

    def source_file_candidates(self, cell: str, view: str = "symbol") -> tuple[str, ...]:
        view_name = view or "symbol"
        return (
            f"{cell}/{view_name}/symbol.oa",
            f"{cell}/{view_name}/master.tag",
        )

    def read_snapshot(self, snapshot_dir: Path, *, verify: bool = True) -> SymbolSnapshot:
        return read_symbol_snapshot(snapshot_dir, verify=verify)

    def write_snapshot(self, snapshot: SymbolSnapshot, dest_dir: Path) -> Path:
        return write_symbol_snapshot(snapshot, dest_dir)

    def diff(self, old: SymbolSnapshot, new: SymbolSnapshot) -> Any:
        from .symbol_diff import diff_symbols

        return diff_symbols(old, new)

    def merge(
        self, base: SymbolSnapshot, ours: SymbolSnapshot, theirs: SymbolSnapshot
    ) -> Any:
        from .symbol_merge import merge_symbols

        return merge_symbols(base, ours, theirs)


register_view_handler("symbol", SymbolViewHandler())


__all__ = [
    "quantize_symbol_coord",
    "quantize_symbol_xy",
    "quantize_symbol_bbox",
    "canonicalize_pin_figure",
    "canonicalize_symbol_pins",
    "canonicalize_symbol_shape",
    "canonicalize_symbol_label",
    "canonicalize_symbol_graphics",
    "canonicalize_symbol_properties",
    "canonicalize_port_order",
    "symbol_to_dict",
    "symbol_from_dict",
    "render_symbol_yaml",
    "build_symbol_snapshot",
    "write_symbol_snapshot",
    "read_symbol_snapshot",
    "raw_symbol_capture_to_snapshot",
    "build_symbol_restore_request_dict",
    "parse_symbol_restore_response",
    "SymbolRoundtripIssue",
    "SymbolRoundtripReport",
    "verify_symbol_roundtrip",
    "SymbolViewHandler",
]
