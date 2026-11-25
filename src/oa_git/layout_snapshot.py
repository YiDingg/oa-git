"""Deterministic canonical JSONL + Manifest v2 codec for OpenAccess layouts.

Storage layout under ``<snapshots_root>/<library>/<cell>/<view>/``::

    manifest.json      # oa-git/2 manifest with view_type="layout", content_sha256, full_sha256
    objects.jsonl      # Deterministically sorted, one canonical layout object per line
    definitions.jsonl  # Optional deduplicated PCell/via param definitions (written iff non-empty)
    omissions.json     # Optional unsupported object log (written iff omissions_count > 0)

All digest calculations exactly match ``oa_git.view_contract`` so Track 1 and
Track 2 integrate with zero conflicts and byte-identical integrity verification.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .layout_model import (
    BBox,
    CellViewRef,
    LAYOUT_MANIFEST_SCHEMA,
    LAYOUT_VIEW_TYPE,
    LayoutBlockage,
    LayoutBoundary,
    LayoutDefinition,
    LayoutEllipse,
    LayoutFigGroup,
    LayoutInstance,
    LayoutLabel,
    LayoutLine,
    LayoutManifest,
    LayoutMosaic,
    LayoutNet,
    LayoutObject,
    LayoutPath,
    LayoutPathSeg,
    LayoutPin,
    LayoutPolygon,
    LayoutRect,
    LayoutSnapshot,
    LayoutVia,
    OmissionRecord,
    ParamValue,
    Point2D,
    SchemaValidationError,
    SnapshotIntegrityError,
    VALID_ACCESS_DIRS,
    VALID_BLOCKAGE_TYPES,
    VALID_BOUNDARY_KINDS,
    VALID_DEFINITION_KINDS,
    VALID_LABEL_TYPES,
    VALID_ORIENTS,
    VALID_PATHSEG_STYLES,
    VALID_PATH_STYLES,
    VALID_PCELL_PARAM_TYPES,
    VALID_PIN_SHAPE_KINDS,
    VALID_PORT_DIRECTIONS,
    VALID_PROP_TYPES,
    VALID_SIG_TYPES,
    VALID_VIA_KINDS,
    is_stripped_metadata_key,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SNAPSHOT_FILES = frozenset(
    {"objects.jsonl", "definitions.jsonl", "omissions.json"}
)
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "view_type",
        "cellview",
        "electrical_sha256",
        "placement_sha256",
        "content_sha256",
        "full_sha256",
        "files",
        "omissions_count",
        "metadata",
    }
)
_TYPE_ORDER: dict[str, int] = {
    "net": 0,
    "pin": 1,
    "instance": 2,
    "mosaic": 3,
    "via": 4,
    "rect": 5,
    "path_seg": 6,
    "path": 7,
    "polygon": 8,
    "ellipse": 9,
    "line": 10,
    "label": 11,
    "blockage": 12,
    "boundary": 13,
    "fig_group": 14,
}


# ---------------------------------------------------------------------------
# Strict Validation & Quantization Primitives
# ---------------------------------------------------------------------------


def _err(message: str) -> SchemaValidationError:
    return SchemaValidationError(message)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _err(f"{path} must be a mapping")
    for key in value:
        if type(key) is not str:
            raise _err(f"{path} keys must be strings")
    return value


def _require_sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _err(f"{path} must be a sequence")
    return value


def _require_text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if type(value) is not str:
        raise _err(f"{path} must be a string")
    if nonempty and not value.strip():
        raise _err(f"{path} must not be empty")
    return value


def _require_optional_text(value: Any, path: str) -> str | None:
    if value is None:
        return None
    text = _require_text(value, path, nonempty=False)
    return text if text.strip() else None


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise _err(f"{path} must be a bool")
    return value


def _require_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise _err(f"{path} must be an int")
    if minimum is not None and value < minimum:
        raise _err(f"{path} must be >= {minimum}")
    return value


def _require_finite_number(value: Any, path: str) -> int | float:
    if type(value) not in (int, float):
        raise _err(f"{path} must be an int or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise _err(f"{path} must be finite")
    return value


def _normalize_float(value: float) -> float:
    return 0.0 if value == 0.0 else value


def quantize_layout_coord(value: float, precision: int = 6) -> float:
    """Quantize a layout coordinate (microns) with zero-normalized float output."""

    if type(precision) is not int or precision < 0:
        raise _err("precision must be a non-negative int")
    num = _require_finite_number(value, "coordinate")
    try:
        as_float = float(num)
    except (OverflowError, ValueError) as exc:
        raise _err("coordinate is not representable as a finite float") from exc
    if not math.isfinite(as_float):
        raise _err("coordinate must be finite")
    return _normalize_float(float(round(as_float, precision)))


def quantize_layout_xy(xy: Sequence[float], precision: int = 6) -> Point2D:
    values = _require_sequence(xy, "xy")
    if len(values) != 2:
        raise _err("xy must contain exactly two coordinates")
    return (
        quantize_layout_coord(values[0], precision),
        quantize_layout_coord(values[1], precision),
    )


def canonicalize_bbox(bbox: Sequence[Sequence[float]], path: str, precision: int = 6) -> BBox:
    raw = _require_sequence(bbox, path)
    if len(raw) != 2:
        raise _err(f"{path} must contain exactly two corner points")
    p0 = quantize_layout_xy(raw[0], precision)
    p1 = quantize_layout_xy(raw[1], precision)
    return ((min(p0[0], p1[0]), min(p0[1], p1[1])), (max(p0[0], p1[0]), max(p0[1], p1[1])))


def canonicalize_ring_points(
    points: Sequence[Sequence[float]],
    path: str,
    *,
    min_points: int = 3,
    precision: int = 6,
) -> tuple[Point2D, ...]:
    """Canonicalize a closed polygon/boundary ring deterministically.

    Removes an explicit duplicate closing vertex if present, drops consecutive
    duplicate vertices, and rotates the ring so it starts at the
    lexicographically smallest ``(x, y)`` vertex while preserving winding order.
    """

    raw = _require_sequence(points, path)
    pts = [quantize_layout_xy(pt, precision) for pt in raw]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts.pop()
    deduped: list[Point2D] = []
    for pt in pts:
        if not deduped or deduped[-1] != pt:
            deduped.append(pt)
    if len(deduped) >= 2 and deduped[0] == deduped[-1]:
        deduped.pop()
    if len(deduped) < min_points:
        raise _err(f"{path} must contain at least {min_points} distinct vertices")
    min_idx = min(range(len(deduped)), key=lambda i: deduped[i])
    rotated = deduped[min_idx:] + deduped[:min_idx]
    return tuple(rotated)


def canonicalize_polyline_points(
    points: Sequence[Sequence[float]],
    path: str,
    *,
    min_points: int = 2,
    precision: int = 6,
) -> tuple[Point2D, ...]:
    raw = _require_sequence(points, path)
    pts = tuple(quantize_layout_xy(pt, precision) for pt in raw)
    if len(pts) < min_points:
        raise _err(f"{path} must contain at least {min_points} points")
    return pts


def _canonical_json_value(value: Any, path: str, _active: set[int] | None = None) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _err(f"{path} must be finite")
        return _normalize_float(value)
    if isinstance(value, (Mapping, list, tuple)):
        active = _active if _active is not None else set()
        oid = id(value)
        if oid in active:
            raise _err(f"{path} contains a cyclic reference")
        active.add(oid)
        try:
            if isinstance(value, Mapping):
                out: dict[str, Any] = {}
                for key in sorted(value):
                    if type(key) is not str:
                        raise _err(f"{path} keys must be strings")
                    out[key] = _canonical_json_value(value[key], f"{path}.{key}", active)
                return out
            return [
                _canonical_json_value(item, f"{path}[{idx}]", active)
                for idx, item in enumerate(value)
            ]
        finally:
            active.remove(oid)
    raise _err(f"{path} has unsupported value type {type(value).__name__}")


def _canonical_param_float(value: float) -> float:
    """Canonicalize an SI/scalar float parameter preserving 12 significant digits.

    Unlike layout coordinates (in microns, quantized to 1e-6 um), PCell and user
    float parameters may be in SI units (e.g. l=6e-8 m, fw=2.4e-7 m, c=1.5e-15 F)
    and must NEVER be rounded to fixed 1e-6 decimal places.
    """

    if not math.isfinite(value):
        raise _err("parameter float must be finite")
    if value == 0.0:
        return 0.0
    return _normalize_float(float(f"{value:.12g}"))


_STD_VIA_MICRON_KEYS = frozenset(
    {
        "cutWidth",
        "cutHeight",
        "cutSpacing",
        "layer1Enc",
        "layer2Enc",
        "layer1Offset",
        "layer2Offset",
        "originOffset",
        "imp1Enc",
        "imp2Enc",
    }
)


def _canonical_param_map(
    value: Any,
    path: str,
    *,
    strip_metadata: bool = True,
) -> dict[str, ParamValue]:
    mapping = _require_mapping(value, path)
    out: dict[str, ParamValue] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        if strip_metadata and is_stripped_metadata_key(key):
            continue
        item = mapping[key]
        if type(item) not in (str, bool, int, float):
            raise _err(f"{path}.{key} must be a scalar ParamValue")
        if type(item) is float:
            if not math.isfinite(item):
                raise _err(f"{path}.{key} must be finite")
            item = _canonical_param_float(item)
        out[key] = item
    return out


def _canonical_via_params(value: Any, path: str) -> dict[str, Any]:
    mapping = _require_mapping(value, path)
    out: dict[str, Any] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        item = mapping[key]
        float_norm = (
            quantize_layout_coord if key in _STD_VIA_MICRON_KEYS else _canonical_param_float
        )
        if type(item) in (str, bool, int):
            out[key] = item
        elif type(item) is float:
            out[key] = float_norm(item)
        elif isinstance(item, (list, tuple)) and not isinstance(item, (str, bytes)):
            nums: list[int | float] = []
            for idx, elem in enumerate(item):
                if type(elem) is int:
                    nums.append(elem)
                elif type(elem) is float:
                    nums.append(float_norm(elem))
                else:
                    raise _err(
                        f"{path}.{key}[{idx}] must be an int or float in via parameter list"
                    )
            out[key] = nums
        else:
            raise _err(f"{path}.{key} has unsupported via parameter type {type(item).__name__}")
    return out


def _canonical_type_map(
    value: Any,
    valid_types: frozenset[str],
    keys: Iterable[str],
    path: str,
    *,
    infer_from: Mapping[str, ParamValue] | None = None,
) -> dict[str, str]:
    mapping = _require_mapping(value, path)
    key_set = set(keys)
    out: dict[str, str] = {}
    for key in sorted(key_set):
        if key in mapping:
            raw_type = _require_text(mapping[key], f"{path}.{key}")
            if raw_type not in valid_types:
                raise _err(f"{path}.{key} has unsupported type {raw_type!r}")
            out[key] = raw_type
        elif infer_from is not None and key in infer_from:
            val = infer_from[key]
            if type(val) is bool:
                out[key] = "boolean"
            elif type(val) is int:
                out[key] = "int"
            elif type(val) is float:
                out[key] = "float"
            else:
                out[key] = "string"
        else:
            raise _err(f"{path} is missing type entry for key {key!r}")
    extra = {k for k in mapping if not is_stripped_metadata_key(k)} - key_set
    if extra:
        raise _err(f"{path} contains extra keys not present in values: {sorted(extra)!r}")
    return out


def _canonical_string_map(value: Any, path: str) -> dict[str, str]:
    mapping = _require_mapping(value, path)
    out: dict[str, str] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        out[key] = _require_text(mapping[key], f"{path}.{key}")
    return out


def _require_orient(value: Any, path: str) -> str:
    orient = _require_text(value, path)
    if orient not in VALID_ORIENTS:
        raise _err(f"{path} has unsupported orientation {orient!r}")
    return orient


def _require_direction(value: Any, path: str) -> str:
    direction = _require_text(value, path)
    if direction not in VALID_PORT_DIRECTIONS:
        raise _err(f"{path} has unsupported direction {direction!r}")
    return direction


def _require_hash(value: Any, path: str) -> str:
    text = _require_text(value, path)
    if not _HASH_RE.fullmatch(text):
        raise _err(f"{path} must be a 64-character lowercase SHA-256 hex digest")
    return text


# ---------------------------------------------------------------------------
# Universal Manifest v2 Digest Helpers (Exact Match with oa_git.view_contract)
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: Any) -> bytes:
    """Encode payload into compact deterministic UTF-8 JSON bytes with trailing LF."""

    return (
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compute_content_sha256(file_contents: Mapping[str, bytes]) -> str:
    """Hash all canonical artifact bytes sorted by filename (excl. manifest/omissions)."""

    hasher = hashlib.sha256()
    for name in sorted(file_contents):
        if name in ("manifest.json", "omissions.json"):
            continue
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(file_contents[name])
        hasher.update(b"\x00")
    return hasher.hexdigest()


def compute_layout_full_sha256(
    *,
    cellview: CellViewRef,
    files: Mapping[str, str],
    omissions_count: int,
    metadata: Mapping[str, Any],
) -> str:
    """Compute canonical ``oa-git/2`` ``full_sha256`` matching ``view_contract``."""

    binding = {
        "schema": LAYOUT_MANIFEST_SCHEMA,
        "view_type": LAYOUT_VIEW_TYPE,
        "cellview": cellview.to_dict(),
        "files": dict(sorted(files.items())),
        "omissions_count": omissions_count,
        "metadata": dict(sorted(metadata.items())),
    }
    return sha256_bytes(canonical_json_bytes(binding))


# ---------------------------------------------------------------------------
# Canonicalization of Layout Objects
# ---------------------------------------------------------------------------


def _canonical_custom_style(
    value: Any, path: str, *, precision: int = 6
) -> tuple[float, ...]:
    if value is None:
        return ()
    raw = _require_sequence(value, path)
    if len(raw) == 0:
        return ()
    if len(raw) != 6:
        raise _err(f"{path} must be empty or contain exactly 6 custom pathSeg style floats")
    return tuple(quantize_layout_coord(item, precision) for item in raw)


def canonicalize_layout_object(obj: LayoutObject, *, precision: int = 6) -> LayoutObject:
    """Validate and normalize one layout object to canonical form."""

    if isinstance(obj, LayoutNet):
        name = _require_text(obj.name, "net.name")
        sig_type = _require_text(obj.sig_type, "net.sig_type")
        if sig_type not in VALID_SIG_TYPES:
            raise _err(f"net.sig_type has unsupported value {sig_type!r}")
        return LayoutNet(
            name=name,
            is_global=_require_bool(obj.is_global, "net.is_global"),
            num_bits=_require_int(obj.num_bits, "net.num_bits", minimum=1),
            sig_type=sig_type,
            props=_canonical_param_map(obj.props, "net.props"),
        )

    if isinstance(obj, LayoutPin):
        name = _require_text(obj.name, "pin.name")
        net = _require_text(obj.net, "pin.net")
        layer = _require_text(obj.layer, "pin.layer")
        purpose = _require_text(obj.purpose, "pin.purpose")
        pin_name = _require_text(obj.pin_name, "pin.pin_name", nonempty=False)
        direction = _require_direction(obj.direction, "pin.direction")
        shape_kind = _require_text(obj.shape_kind, "pin.shape_kind")
        if shape_kind not in VALID_PIN_SHAPE_KINDS:
            raise _err(f"pin.shape_kind has unsupported value {shape_kind!r}")
        bbox: BBox | None = None
        points: tuple[Point2D, ...] = ()
        begin_pt: Point2D | None = None
        end_pt: Point2D | None = None
        width = 0.0
        begin_style = "truncate"
        end_style = "truncate"
        begin_ext = 0.0
        end_ext = 0.0
        custom_style: tuple[float, ...] = ()
        path_style = "truncateExtend"
        if shape_kind in ("rect", "ellipse"):
            if obj.bbox is None:
                raise _err(f"pin with shape_kind={shape_kind!r} requires bbox")
            bbox = canonicalize_bbox(obj.bbox, "pin.bbox", precision)
        elif shape_kind == "polygon":
            points = canonicalize_ring_points(obj.points, "pin.points", precision=precision)
        elif shape_kind == "path_seg":
            if obj.begin_pt is None or obj.end_pt is None:
                raise _err("pin with shape_kind='path_seg' requires begin_pt and end_pt")
            p0 = quantize_layout_xy(obj.begin_pt, precision)
            p1 = quantize_layout_xy(obj.end_pt, precision)
            width = quantize_layout_coord(obj.width, precision)
            if width < 0.0:
                raise _err("pin.width must be >= 0")
            begin_style = _require_text(obj.begin_style, "pin.begin_style")
            end_style = _require_text(obj.end_style, "pin.end_style")
            if begin_style not in VALID_PATHSEG_STYLES:
                raise _err(f"pin.begin_style has unsupported value {begin_style!r}")
            if end_style not in VALID_PATHSEG_STYLES:
                raise _err(f"pin.end_style has unsupported value {end_style!r}")
            begin_ext = quantize_layout_coord(obj.begin_ext, precision)
            end_ext = quantize_layout_coord(obj.end_ext, precision)
            custom_style = _canonical_custom_style(
                obj.custom_style, "pin.custom_style", precision=precision
            )
            if p0 > p1:
                p0, p1 = p1, p0
                begin_style, end_style = end_style, begin_style
                begin_ext, end_ext = end_ext, begin_ext
                if len(custom_style) == 6:
                    custom_style = custom_style[3:] + custom_style[:3]
            begin_pt, end_pt = p0, p1
        else:  # path
            points = canonicalize_polyline_points(obj.points, "pin.points", precision=precision)
            width = quantize_layout_coord(obj.width, precision)
            if width < 0.0:
                raise _err("pin.width must be >= 0")
            path_style = _require_text(obj.path_style, "pin.path_style")
            if path_style not in VALID_PATH_STYLES:
                raise _err(f"pin.path_style has unsupported value {path_style!r}")
            begin_ext = quantize_layout_coord(obj.begin_ext, precision)
            end_ext = quantize_layout_coord(obj.end_ext, precision)
        access_dirs_raw = _require_sequence(obj.access_dir, "pin.access_dir")
        access_dirs: set[str] = set()
        for idx, d in enumerate(access_dirs_raw):
            d_str = _require_text(d, f"pin.access_dir[{idx}]")
            if d_str not in VALID_ACCESS_DIRS:
                raise _err(f"pin.access_dir[{idx}] has unsupported value {d_str!r}")
            access_dirs.add(d_str)
        return LayoutPin(
            name=name,
            net=net,
            layer=layer,
            purpose=purpose,
            pin_name=pin_name,
            direction=direction,
            shape_kind=shape_kind,  # type: ignore[arg-type]
            bbox=bbox,
            points=points,
            begin_pt=begin_pt,
            end_pt=end_pt,
            width=width,
            begin_style=begin_style,
            end_style=end_style,
            begin_ext=begin_ext,
            end_ext=end_ext,
            custom_style=custom_style,
            path_style=path_style,
            access_dir=tuple(sorted(access_dirs)),
            props=_canonical_param_map(obj.props, "pin.props"),
        )

    if isinstance(obj, LayoutRect):
        return LayoutRect(
            layer=_require_text(obj.layer, "rect.layer"),
            purpose=_require_text(obj.purpose, "rect.purpose"),
            bbox=canonicalize_bbox(obj.bbox, "rect.bbox", precision),
            net=_require_optional_text(obj.net, "rect.net"),
            props=_canonical_param_map(obj.props, "rect.props"),
        )

    if isinstance(obj, LayoutPolygon):
        return LayoutPolygon(
            layer=_require_text(obj.layer, "polygon.layer"),
            purpose=_require_text(obj.purpose, "polygon.purpose"),
            points=canonicalize_ring_points(obj.points, "polygon.points", precision=precision),
            net=_require_optional_text(obj.net, "polygon.net"),
            props=_canonical_param_map(obj.props, "polygon.props"),
        )

    if isinstance(obj, LayoutPath):
        width = quantize_layout_coord(obj.width, precision)
        if width < 0.0:
            raise _err("path.width must be >= 0")
        style = _require_text(obj.path_style, "path.path_style")
        if style not in VALID_PATH_STYLES:
            raise _err(f"path.path_style has unsupported value {style!r}")
        return LayoutPath(
            layer=_require_text(obj.layer, "path.layer"),
            purpose=_require_text(obj.purpose, "path.purpose"),
            points=canonicalize_polyline_points(obj.points, "path.points", precision=precision),
            width=width,
            path_style=style,
            begin_ext=quantize_layout_coord(obj.begin_ext, precision),
            end_ext=quantize_layout_coord(obj.end_ext, precision),
            net=_require_optional_text(obj.net, "path.net"),
            props=_canonical_param_map(obj.props, "path.props"),
        )

    if isinstance(obj, LayoutPathSeg):
        p0 = quantize_layout_xy(obj.begin_pt, precision)
        p1 = quantize_layout_xy(obj.end_pt, precision)
        width = quantize_layout_coord(obj.width, precision)
        if width < 0.0:
            raise _err("path_seg.width must be >= 0")
        b_style = _require_text(obj.begin_style, "path_seg.begin_style")
        e_style = _require_text(obj.end_style, "path_seg.end_style")
        if b_style not in VALID_PATHSEG_STYLES:
            raise _err(f"path_seg.begin_style has unsupported value {b_style!r}")
        if e_style not in VALID_PATHSEG_STYLES:
            raise _err(f"path_seg.end_style has unsupported value {e_style!r}")
        b_ext = quantize_layout_coord(obj.begin_ext, precision)
        e_ext = quantize_layout_coord(obj.end_ext, precision)
        custom_style = _canonical_custom_style(
            obj.custom_style, "path_seg.custom_style", precision=precision
        )
        if p0 > p1:
            p0, p1 = p1, p0
            b_style, e_style = e_style, b_style
            b_ext, e_ext = e_ext, b_ext
            if len(custom_style) == 6:
                custom_style = custom_style[3:] + custom_style[:3]
        return LayoutPathSeg(
            layer=_require_text(obj.layer, "path_seg.layer"),
            purpose=_require_text(obj.purpose, "path_seg.purpose"),
            begin_pt=p0,
            end_pt=p1,
            width=width,
            begin_style=b_style,
            end_style=e_style,
            begin_ext=b_ext,
            end_ext=e_ext,
            custom_style=custom_style,
            net=_require_optional_text(obj.net, "path_seg.net"),
            props=_canonical_param_map(obj.props, "path_seg.props"),
        )

    if isinstance(obj, LayoutEllipse):
        return LayoutEllipse(
            layer=_require_text(obj.layer, "ellipse.layer"),
            purpose=_require_text(obj.purpose, "ellipse.purpose"),
            bbox=canonicalize_bbox(obj.bbox, "ellipse.bbox", precision),
            net=_require_optional_text(obj.net, "ellipse.net"),
            props=_canonical_param_map(obj.props, "ellipse.props"),
        )

    if isinstance(obj, LayoutLine):
        return LayoutLine(
            layer=_require_text(obj.layer, "line.layer"),
            purpose=_require_text(obj.purpose, "line.purpose"),
            points=canonicalize_polyline_points(obj.points, "line.points", precision=precision),
            net=_require_optional_text(obj.net, "line.net"),
            props=_canonical_param_map(obj.props, "line.props"),
        )

    if isinstance(obj, LayoutVia):
        via_kind = _require_text(obj.via_kind, "via.via_kind")
        if via_kind not in VALID_VIA_KINDS:
            raise _err(f"via.via_kind has unsupported value {via_kind!r}")
        return LayoutVia(
            via_def=_require_text(obj.via_def, "via.via_def"),
            origin=quantize_layout_xy(obj.origin, precision),
            orient=_require_orient(obj.orient, "via.orient"),
            via_kind=via_kind,  # type: ignore[arg-type]
            net=_require_optional_text(obj.net, "via.net"),
            name=_require_optional_text(obj.name, "via.name"),
            params=_canonical_via_params(obj.params, "via.params"),
            props=_canonical_param_map(obj.props, "via.props"),
        )

    if isinstance(obj, LayoutLabel):
        label_type = _require_text(obj.label_type, "label.label_type")
        if label_type not in VALID_LABEL_TYPES:
            raise _err(f"label.label_type has unsupported value {label_type!r}")
        height = quantize_layout_coord(obj.height, precision)
        if height < 0.0:
            raise _err("label.height must be >= 0")
        return LayoutLabel(
            text=_require_text(obj.text, "label.text", nonempty=False),
            xy=quantize_layout_xy(obj.xy, precision),
            layer=_require_text(obj.layer, "label.layer"),
            purpose=_require_text(obj.purpose, "label.purpose"),
            justify=_require_text(obj.justify, "label.justify"),
            orient=_require_orient(obj.orient, "label.orient"),
            font=_require_text(obj.font, "label.font"),
            height=height,
            label_type=label_type,
            net=_require_optional_text(obj.net, "label.net"),
            props=_canonical_param_map(obj.props, "label.props"),
        )

    if isinstance(obj, LayoutInstance):
        pcell_params = _canonical_param_map(
            obj.pcell_params, "instance.pcell_params", strip_metadata=False
        )
        pcell_types = _canonical_type_map(
            obj.pcell_param_types,
            VALID_PCELL_PARAM_TYPES,
            pcell_params.keys(),
            "instance.pcell_param_types",
            infer_from=pcell_params,
        )
        props = _canonical_param_map(obj.props, "instance.props", strip_metadata=True)
        prop_types = _canonical_type_map(
            obj.prop_types,
            VALID_PROP_TYPES,
            props.keys(),
            "instance.prop_types",
            infer_from=props,
        )
        return LayoutInstance(
            name=_require_text(obj.name, "instance.name"),
            lib=_require_text(obj.lib, "instance.lib"),
            cell=_require_text(obj.cell, "instance.cell"),
            view=_require_text(obj.view, "instance.view"),
            xy=quantize_layout_xy(obj.xy, precision),
            orient=_require_orient(obj.orient, "instance.orient"),
            is_pcell=_require_bool(obj.is_pcell, "instance.is_pcell"),
            pcell_params=pcell_params,
            pcell_param_types=pcell_types,
            connections=_canonical_string_map(obj.connections, "instance.connections"),
            props=props,
            prop_types=prop_types,
        )

    if isinstance(obj, LayoutMosaic):
        pcell_params = _canonical_param_map(
            obj.pcell_params, "mosaic.pcell_params", strip_metadata=False
        )
        pcell_types = _canonical_type_map(
            obj.pcell_param_types,
            VALID_PCELL_PARAM_TYPES,
            pcell_params.keys(),
            "mosaic.pcell_param_types",
            infer_from=pcell_params,
        )
        props = _canonical_param_map(obj.props, "mosaic.props", strip_metadata=True)
        prop_types = _canonical_type_map(
            obj.prop_types,
            VALID_PROP_TYPES,
            props.keys(),
            "mosaic.prop_types",
            infer_from=props,
        )
        return LayoutMosaic(
            name=_require_text(obj.name, "mosaic.name"),
            lib=_require_text(obj.lib, "mosaic.lib"),
            cell=_require_text(obj.cell, "mosaic.cell"),
            view=_require_text(obj.view, "mosaic.view"),
            xy=quantize_layout_xy(obj.xy, precision),
            orient=_require_orient(obj.orient, "mosaic.orient"),
            rows=_require_int(obj.rows, "mosaic.rows", minimum=1),
            columns=_require_int(obj.columns, "mosaic.columns", minimum=1),
            u_x=quantize_layout_coord(obj.u_x, precision),
            u_y=quantize_layout_coord(obj.u_y, precision),
            is_pcell=_require_bool(obj.is_pcell, "mosaic.is_pcell"),
            pcell_params=pcell_params,
            pcell_param_types=pcell_types,
            props=props,
            prop_types=prop_types,
        )

    if isinstance(obj, LayoutBlockage):
        btype = _require_text(obj.blockage_type, "blockage.blockage_type")
        if btype not in VALID_BLOCKAGE_TYPES:
            raise _err(f"blockage.blockage_type has unsupported value {btype!r}")
        return LayoutBlockage(
            blockage_type=btype,
            points=canonicalize_ring_points(
                obj.points, "blockage.points", min_points=2, precision=precision
            ),
            layer=_require_optional_text(obj.layer, "blockage.layer"),
            props=_canonical_param_map(obj.props, "blockage.props"),
        )

    if isinstance(obj, LayoutBoundary):
        kind = _require_text(obj.boundary_kind, "boundary.boundary_kind")
        if kind not in VALID_BOUNDARY_KINDS:
            raise _err(f"boundary.boundary_kind has unsupported value {kind!r}")
        return LayoutBoundary(
            boundary_kind=kind,  # type: ignore[arg-type]
            points=canonicalize_ring_points(obj.points, "boundary.points", precision=precision),
            name=_require_optional_text(obj.name, "boundary.name"),
            props=_canonical_param_map(obj.props, "boundary.props"),
        )

    if isinstance(obj, LayoutFigGroup):
        return LayoutFigGroup(
            name=_require_text(obj.name, "fig_group.name"),
            origin=quantize_layout_xy(obj.origin, precision),
            orient=_require_orient(obj.orient, "fig_group.orient"),
            group_type=_require_text(obj.group_type, "fig_group.group_type"),
            props=_canonical_param_map(obj.props, "fig_group.props"),
        )

    raise _err(f"Unsupported layout object type: {type(obj).__name__}")


def _object_inline_dict(obj: LayoutObject) -> dict[str, Any]:
    """Convert one canonical LayoutObject to its full inline JSON dictionary."""

    if isinstance(obj, LayoutNet):
        d: dict[str, Any] = {
            "type": "net",
            "name": obj.name,
            "is_global": obj.is_global,
            "num_bits": obj.num_bits,
            "sig_type": obj.sig_type,
        }
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutPin):
        d = {
            "type": "pin",
            "name": obj.name,
            "pin_name": obj.pin_name,
            "net": obj.net,
            "direction": obj.direction,
            "layer": obj.layer,
            "purpose": obj.purpose,
            "shape_kind": obj.shape_kind,
        }
        if obj.bbox is not None:
            d["bbox"] = [list(obj.bbox[0]), list(obj.bbox[1])]
        if obj.points:
            d["points"] = [list(pt) for pt in obj.points]
        if obj.shape_kind == "path_seg" and obj.begin_pt is not None and obj.end_pt is not None:
            d["begin_pt"] = list(obj.begin_pt)
            d["end_pt"] = list(obj.end_pt)
            d["width"] = obj.width
            d["begin_style"] = obj.begin_style
            d["end_style"] = obj.end_style
            d["begin_ext"] = obj.begin_ext
            d["end_ext"] = obj.end_ext
            if obj.custom_style:
                d["custom_style"] = list(obj.custom_style)
        elif obj.shape_kind == "path":
            d["width"] = obj.width
            d["path_style"] = obj.path_style
            d["begin_ext"] = obj.begin_ext
            d["end_ext"] = obj.end_ext
        if obj.access_dir:
            d["access_dir"] = list(obj.access_dir)
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutRect):
        d = {
            "type": "rect",
            "layer": obj.layer,
            "purpose": obj.purpose,
            "bbox": [list(obj.bbox[0]), list(obj.bbox[1])],
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutPolygon):
        d = {
            "type": "polygon",
            "layer": obj.layer,
            "purpose": obj.purpose,
            "points": [list(pt) for pt in obj.points],
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutPath):
        d = {
            "type": "path",
            "layer": obj.layer,
            "purpose": obj.purpose,
            "points": [list(pt) for pt in obj.points],
            "width": obj.width,
            "path_style": obj.path_style,
            "begin_ext": obj.begin_ext,
            "end_ext": obj.end_ext,
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutPathSeg):
        d = {
            "type": "path_seg",
            "layer": obj.layer,
            "purpose": obj.purpose,
            "begin_pt": list(obj.begin_pt),
            "end_pt": list(obj.end_pt),
            "width": obj.width,
            "begin_style": obj.begin_style,
            "end_style": obj.end_style,
            "begin_ext": obj.begin_ext,
            "end_ext": obj.end_ext,
        }
        if obj.custom_style:
            d["custom_style"] = list(obj.custom_style)
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutEllipse):
        d = {
            "type": "ellipse",
            "layer": obj.layer,
            "purpose": obj.purpose,
            "bbox": [list(obj.bbox[0]), list(obj.bbox[1])],
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutLine):
        d = {
            "type": "line",
            "layer": obj.layer,
            "purpose": obj.purpose,
            "points": [list(pt) for pt in obj.points],
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutVia):
        d = {
            "type": "via",
            "via_def": obj.via_def,
            "via_kind": obj.via_kind,
            "origin": list(obj.origin),
            "orient": obj.orient,
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.name is not None:
            d["name"] = obj.name
        if obj.params:
            d["params"] = dict(obj.params)
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutLabel):
        d = {
            "type": "label",
            "text": obj.text,
            "xy": list(obj.xy),
            "layer": obj.layer,
            "purpose": obj.purpose,
            "justify": obj.justify,
            "orient": obj.orient,
            "font": obj.font,
            "height": obj.height,
            "label_type": obj.label_type,
        }
        if obj.net is not None:
            d["net"] = obj.net
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutInstance):
        d = {
            "type": "instance",
            "name": obj.name,
            "lib": obj.lib,
            "cell": obj.cell,
            "view": obj.view,
            "xy": list(obj.xy),
            "orient": obj.orient,
            "is_pcell": obj.is_pcell,
        }
        if obj.pcell_params:
            d["pcell_params"] = dict(obj.pcell_params)
            d["pcell_param_types"] = dict(obj.pcell_param_types)
        if obj.connections:
            d["connections"] = dict(obj.connections)
        if obj.props:
            d["props"] = dict(obj.props)
            d["prop_types"] = dict(obj.prop_types)
        return d

    if isinstance(obj, LayoutMosaic):
        d = {
            "type": "mosaic",
            "name": obj.name,
            "lib": obj.lib,
            "cell": obj.cell,
            "view": obj.view,
            "xy": list(obj.xy),
            "orient": obj.orient,
            "rows": obj.rows,
            "columns": obj.columns,
            "u_x": obj.u_x,
            "u_y": obj.u_y,
            "is_pcell": obj.is_pcell,
        }
        if obj.pcell_params:
            d["pcell_params"] = dict(obj.pcell_params)
            d["pcell_param_types"] = dict(obj.pcell_param_types)
        if obj.props:
            d["props"] = dict(obj.props)
            d["prop_types"] = dict(obj.prop_types)
        return d

    if isinstance(obj, LayoutBlockage):
        d = {
            "type": "blockage",
            "blockage_type": obj.blockage_type,
            "points": [list(pt) for pt in obj.points],
        }
        if obj.layer is not None:
            d["layer"] = obj.layer
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutBoundary):
        d = {
            "type": "boundary",
            "boundary_kind": obj.boundary_kind,
            "points": [list(pt) for pt in obj.points],
        }
        if obj.name is not None:
            d["name"] = obj.name
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    if isinstance(obj, LayoutFigGroup):
        d = {
            "type": "fig_group",
            "name": obj.name,
            "origin": list(obj.origin),
            "orient": obj.orient,
            "group_type": obj.group_type,
        }
        if obj.props:
            d["props"] = dict(obj.props)
        return d

    raise _err(f"Unsupported layout object type: {type(obj).__name__}")


def _object_sort_key(obj: LayoutObject) -> tuple[Any, ...]:
    """Deterministic total sort key grouping objects by type, layer, net, name, geometry."""

    type_rank = _TYPE_ORDER[obj.obj_type]
    layer = getattr(obj, "layer", None) or getattr(obj, "via_def", None) or ""
    purpose = getattr(obj, "purpose", None) or getattr(obj, "boundary_kind", None) or ""
    net = getattr(obj, "net", None) or ""
    name = getattr(obj, "name", None) or getattr(obj, "text", None) or ""
    geom: tuple[Any, ...]
    if hasattr(obj, "bbox") and getattr(obj, "bbox") is not None:
        bbox = getattr(obj, "bbox")
        geom = (bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1])
    elif hasattr(obj, "begin_pt") and getattr(obj, "begin_pt") is not None:
        bp = getattr(obj, "begin_pt")
        ep = getattr(obj, "end_pt")
        geom = (bp[0], bp[1], ep[0], ep[1])
    elif hasattr(obj, "xy"):
        xy = getattr(obj, "xy")
        geom = (xy[0], xy[1])
    elif hasattr(obj, "origin"):
        orig = getattr(obj, "origin")
        geom = (orig[0], orig[1])
    elif hasattr(obj, "points") and getattr(obj, "points"):
        pts = getattr(obj, "points")
        geom = (pts[0][0], pts[0][1], len(pts))
    else:
        geom = ()
    tie = json.dumps(
        _object_inline_dict(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return (type_rank, layer, purpose, net, name, geom, tie)


def canonicalize_layout_objects(
    objects: Sequence[LayoutObject], *, precision: int = 6
) -> tuple[LayoutObject, ...]:
    """Validate, canonicalize, and deterministically sort a sequence of layout objects."""

    raw = _require_sequence(objects, "objects")
    canonical = [canonicalize_layout_object(obj, precision=precision) for obj in raw]
    # Enforce uniqueness on keyed named entities (nets, instances, mosaics, fig_groups, prBoundary).
    seen_nets: set[str] = set()
    seen_instances: set[str] = set()
    seen_mosaics: set[str] = set()
    seen_fig_groups: set[str] = set()
    pr_boundary_count = 0
    for obj in canonical:
        if isinstance(obj, LayoutNet):
            if obj.name in seen_nets:
                raise _err(f"duplicate layout net name {obj.name!r}")
            seen_nets.add(obj.name)
        elif isinstance(obj, LayoutInstance):
            if obj.name in seen_instances:
                raise _err(f"duplicate layout instance name {obj.name!r}")
            seen_instances.add(obj.name)
        elif isinstance(obj, LayoutMosaic):
            if obj.name in seen_mosaics:
                raise _err(f"duplicate layout mosaic name {obj.name!r}")
            seen_mosaics.add(obj.name)
        elif isinstance(obj, LayoutFigGroup):
            if obj.name in seen_fig_groups:
                raise _err(f"duplicate layout fig_group name {obj.name!r}")
            seen_fig_groups.add(obj.name)
        elif isinstance(obj, LayoutBoundary) and obj.boundary_kind == "prBoundary":
            pr_boundary_count += 1
            if pr_boundary_count > 1:
                raise _err("layout cannot contain more than one prBoundary")
    canonical.sort(key=_object_sort_key)
    return tuple(canonical)


def _canonicalize_omissions(omissions: Sequence[OmissionRecord]) -> tuple[OmissionRecord, ...]:
    raw = _require_sequence(omissions, "omissions")
    records: list[OmissionRecord] = []
    for idx, rec in enumerate(raw):
        if not isinstance(rec, OmissionRecord):
            raise _err(f"omissions[{idx}] must be an OmissionRecord")
        details = _canonical_json_value(
            _require_mapping(rec.details, f"omissions[{idx}].details"),
            f"omissions[{idx}].details",
        )
        records.append(
            OmissionRecord(
                kind=_require_text(rec.kind, f"omissions[{idx}].kind"),
                identifier=_require_text(rec.identifier, f"omissions[{idx}].identifier"),
                reason=_require_text(rec.reason, f"omissions[{idx}].reason"),
                details=details,
            )
        )
    records.sort(
        key=lambda r: (
            r.kind,
            r.identifier,
            r.reason,
            json.dumps(r.details, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        )
    )
    return tuple(records)


# ---------------------------------------------------------------------------
# Definition Deduplication & JSONL Encoding / Decoding
# ---------------------------------------------------------------------------


def _make_def_id(kind: str, payload: Mapping[str, Any]) -> str:
    prefix = {"pcell_params": "pcell", "via_params": "via", "props": "props"}[kind]
    digest = sha256_bytes(canonical_json_bytes({"kind": kind, "payload": payload}))
    return f"{prefix}_{digest[:12]}"


def encode_layout_jsonl(
    objects: Sequence[LayoutObject],
    *,
    cell_properties: Mapping[str, ParamValue] | None = None,
) -> tuple[bytes, bytes | None, tuple[LayoutDefinition, ...]]:
    """Render canonical ``objects.jsonl`` and optional deduplicated ``definitions.jsonl``.

    Deduplication strategy:
    - All non-empty PCell parameter payloads (``pcell_params`` + ``pcell_param_types``)
      are stored in ``definitions.jsonl`` and referenced by ``pcell_def`` so
      repeated transistors/devices share one canonical definition.
    - Non-empty ``via.params`` payloads used by 2 or more vias are deduplicated
      into ``definitions.jsonl`` and referenced by ``params_def``.
    - Non-empty ``props`` dicts used by 2 or more objects are deduplicated into
      ``definitions.jsonl`` and referenced by ``props_def``.
    """

    canon_objects = canonicalize_layout_objects(objects)
    canon_cell_props = _canonical_param_map(
        cell_properties or {}, "cell_properties", strip_metadata=True
    )

    via_counts: Counter[str] = Counter()
    via_payloads: dict[str, dict[str, Any]] = {}
    props_counts: Counter[str] = Counter()
    props_payloads: dict[str, dict[str, Any]] = {}
    definitions_by_id: dict[str, LayoutDefinition] = {}

    # Pass 1: count repeatable via_params and props, collect all pcell_params.
    for obj in canon_objects:
        if isinstance(obj, (LayoutInstance, LayoutMosaic)) and obj.pcell_params:
            payload = {
                "params": dict(obj.pcell_params),
                "param_types": dict(obj.pcell_param_types),
            }
            def_id = _make_def_id("pcell_params", payload)
            definitions_by_id[def_id] = LayoutDefinition(
                def_id=def_id, kind="pcell_params", payload=payload
            )
        if isinstance(obj, LayoutVia) and obj.params:
            payload = {"params": dict(obj.params)}
            def_id = _make_def_id("via_params", payload)
            via_counts[def_id] += 1
            via_payloads[def_id] = payload
        if obj.props:
            if isinstance(obj, (LayoutInstance, LayoutMosaic)):
                payload = {"props": dict(obj.props), "prop_types": dict(obj.prop_types)}
            else:
                payload = {"props": dict(obj.props)}
            def_id = _make_def_id("props", payload)
            props_counts[def_id] += 1
            props_payloads[def_id] = payload

    for def_id, count in via_counts.items():
        if count >= 2:
            definitions_by_id[def_id] = LayoutDefinition(
                def_id=def_id, kind="via_params", payload=via_payloads[def_id]
            )
    for def_id, count in props_counts.items():
        if count >= 2:
            definitions_by_id[def_id] = LayoutDefinition(
                def_id=def_id, kind="props", payload=props_payloads[def_id]
            )

    # Pass 2: emit compact JSONL lines referencing deduplicated definitions.
    object_lines: list[str] = []
    for obj in canon_objects:
        record = _object_inline_dict(obj)
        if isinstance(obj, (LayoutInstance, LayoutMosaic)) and obj.pcell_params:
            payload = {
                "params": dict(obj.pcell_params),
                "param_types": dict(obj.pcell_param_types),
            }
            def_id = _make_def_id("pcell_params", payload)
            record.pop("pcell_params", None)
            record.pop("pcell_param_types", None)
            record["pcell_def"] = def_id
        if isinstance(obj, LayoutVia) and obj.params:
            payload = {"params": dict(obj.params)}
            def_id = _make_def_id("via_params", payload)
            if def_id in definitions_by_id:
                record.pop("params", None)
                record["params_def"] = def_id
        if obj.props:
            if isinstance(obj, (LayoutInstance, LayoutMosaic)):
                payload = {"props": dict(obj.props), "prop_types": dict(obj.prop_types)}
            else:
                payload = {"props": dict(obj.props)}
            def_id = _make_def_id("props", payload)
            if def_id in definitions_by_id:
                record.pop("props", None)
                record.pop("prop_types", None)
                record["props_def"] = def_id
        object_lines.append(
            json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        )

    if canon_cell_props:
        cell_prop_record = {"type": "cell_properties", "props": canon_cell_props}
        object_lines.append(
            json.dumps(
                cell_prop_record,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    objects_bytes = ("\n".join(object_lines) + ("\n" if object_lines else "")).encode("utf-8")

    sorted_defs = tuple(definitions_by_id[k] for k in sorted(definitions_by_id))
    if sorted_defs:
        def_lines = [
            json.dumps(
                {"def_id": item.def_id, "kind": item.kind, "payload": item.payload},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for item in sorted_defs
        ]
        defs_bytes: bytes | None = ("\n".join(def_lines) + "\n").encode("utf-8")
    else:
        defs_bytes = None

    return objects_bytes, defs_bytes, sorted_defs


def _parse_json_strict(text: str, path: str) -> Any:
    def reject_constant(val: str) -> Any:
        raise SnapshotIntegrityError(f"{path}: non-finite JSON constant {val}")

    def reject_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise SnapshotIntegrityError(f"{path}: duplicate JSON key {k!r}")
            out[k] = v
        return out

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_dupes,
        )
    except SnapshotIntegrityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"{path}: invalid JSON: {exc}") from exc


def decode_definitions_jsonl(raw: bytes) -> dict[str, LayoutDefinition]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotIntegrityError("definitions.jsonl is not valid UTF-8") from exc
    defs: dict[str, LayoutDefinition] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise SnapshotIntegrityError(f"definitions.jsonl:{line_no} is blank")
        data = _parse_json_strict(line, f"definitions.jsonl:{line_no}")
        mapping = _require_mapping(data, f"definitions.jsonl:{line_no}")
        if set(mapping) != {"def_id", "kind", "payload"}:
            raise _err(
                f"definitions.jsonl:{line_no} must have exact keys def_id, kind, payload"
            )
        def_id = _require_text(mapping["def_id"], f"definitions.jsonl:{line_no}.def_id")
        kind = _require_text(mapping["kind"], f"definitions.jsonl:{line_no}.kind")
        if kind not in VALID_DEFINITION_KINDS:
            raise _err(f"definitions.jsonl:{line_no}.kind unsupported: {kind!r}")
        payload = _canonical_json_value(
            _require_mapping(mapping["payload"], f"definitions.jsonl:{line_no}.payload"),
            f"definitions.jsonl:{line_no}.payload",
        )
        if def_id in defs:
            raise _err(f"definitions.jsonl:{line_no}: duplicate def_id {def_id!r}")
        defs[def_id] = LayoutDefinition(
            def_id=def_id,
            kind=kind,  # type: ignore[arg-type]
            payload=payload,
        )
    return defs


def layout_object_from_dict(
    data: Mapping[str, Any],
    *,
    definitions: Mapping[str, LayoutDefinition] | None = None,
    path: str = "object",
) -> LayoutObject:
    """Parse and canonicalize a single layout object dictionary (inline or def-referenced)."""

    mapping = _require_mapping(data, path)
    obj_type = _require_text(mapping.get("type"), f"{path}.type")
    defs = definitions or {}

    def resolve_props(
        *, with_types: bool = False
    ) -> tuple[dict[str, ParamValue], dict[str, str]]:
        if "props_def" in mapping:
            if "props" in mapping or "prop_types" in mapping:
                raise _err(f"{path} cannot specify both props_def and inline props")
            def_id = _require_text(mapping["props_def"], f"{path}.props_def")
            defn = defs.get(def_id)
            if defn is None or defn.kind != "props":
                raise _err(f"{path}.props_def references unknown props definition {def_id!r}")
            raw_props = defn.payload.get("props", {})
            raw_types = defn.payload.get("prop_types", {})
        else:
            raw_props = mapping.get("props", {})
            raw_types = mapping.get("prop_types", {})
        props = _canonical_param_map(raw_props, f"{path}.props", strip_metadata=True)
        if with_types:
            prop_types = _canonical_type_map(
                raw_types,
                VALID_PROP_TYPES,
                props.keys(),
                f"{path}.prop_types",
                infer_from=props,
            )
            return props, prop_types
        return props, {}

    def resolve_pcell_params() -> tuple[dict[str, ParamValue], dict[str, str]]:
        if "pcell_def" in mapping:
            if "pcell_params" in mapping or "pcell_param_types" in mapping:
                raise _err(f"{path} cannot specify both pcell_def and inline pcell_params")
            def_id = _require_text(mapping["pcell_def"], f"{path}.pcell_def")
            defn = defs.get(def_id)
            if defn is None or defn.kind != "pcell_params":
                raise _err(
                    f"{path}.pcell_def references unknown pcell_params definition {def_id!r}"
                )
            raw_params = defn.payload.get("params", {})
            raw_types = defn.payload.get("param_types", {})
        else:
            raw_params = mapping.get("pcell_params", {})
            raw_types = mapping.get("pcell_param_types", {})
        params = _canonical_param_map(
            raw_params, f"{path}.pcell_params", strip_metadata=False
        )
        types = _canonical_type_map(
            raw_types,
            VALID_PCELL_PARAM_TYPES,
            params.keys(),
            f"{path}.pcell_param_types",
            infer_from=params,
        )
        return params, types

    if obj_type == "net":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutNet(
                name=_require_text(mapping.get("name"), f"{path}.name"),
                is_global=_require_bool(mapping.get("is_global", False), f"{path}.is_global"),
                num_bits=_require_int(mapping.get("num_bits", 1), f"{path}.num_bits", minimum=1),
                sig_type=_require_text(mapping.get("sig_type", "signal"), f"{path}.sig_type"),
                props=props,
            )
        )

    if obj_type == "pin":
        props, _ = resolve_props()
        shape_kind = _require_text(mapping.get("shape_kind", "rect"), f"{path}.shape_kind")
        raw_bbox = mapping.get("bbox")
        raw_pts = _require_sequence(mapping.get("points", ()), f"{path}.points")
        raw_access = _require_sequence(mapping.get("access_dir", ()), f"{path}.access_dir")
        raw_begin = mapping.get("begin_pt")
        raw_end = mapping.get("end_pt")
        return canonicalize_layout_object(
            LayoutPin(
                name=_require_text(mapping.get("name"), f"{path}.name"),
                net=_require_text(mapping.get("net"), f"{path}.net"),
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "pin"), f"{path}.purpose"),
                pin_name=_require_text(
                    mapping.get("pin_name", ""), f"{path}.pin_name", nonempty=False
                ),
                direction=_require_text(
                    mapping.get("direction", "inputOutput"), f"{path}.direction"
                ),
                shape_kind=shape_kind,  # type: ignore[arg-type]
                bbox=canonicalize_bbox(raw_bbox, f"{path}.bbox") if raw_bbox is not None else None,
                points=tuple(quantize_layout_xy(p) for p in raw_pts) if raw_pts else (),
                begin_pt=quantize_layout_xy(raw_begin) if raw_begin is not None else None,
                end_pt=quantize_layout_xy(raw_end) if raw_end is not None else None,
                width=quantize_layout_coord(mapping.get("width", 0.0)),
                begin_style=_require_text(
                    mapping.get("begin_style", "truncate"), f"{path}.begin_style"
                ),
                end_style=_require_text(
                    mapping.get("end_style", "truncate"), f"{path}.end_style"
                ),
                begin_ext=quantize_layout_coord(mapping.get("begin_ext", 0.0)),
                end_ext=quantize_layout_coord(mapping.get("end_ext", 0.0)),
                custom_style=_canonical_custom_style(
                    mapping.get("custom_style"), f"{path}.custom_style"
                ),
                path_style=_require_text(
                    mapping.get("path_style", "truncateExtend"), f"{path}.path_style"
                ),
                access_dir=tuple(raw_access),
                props=props,
            )
        )

    if obj_type == "rect":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutRect(
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                bbox=canonicalize_bbox(mapping.get("bbox"), f"{path}.bbox"),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "polygon":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutPolygon(
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                points=canonicalize_ring_points(mapping.get("points"), f"{path}.points"),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "path":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutPath(
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                points=canonicalize_polyline_points(mapping.get("points"), f"{path}.points"),
                width=quantize_layout_coord(mapping.get("width", 0.0)),
                path_style=_require_text(
                    mapping.get("path_style", "truncateExtend"), f"{path}.path_style"
                ),
                begin_ext=quantize_layout_coord(mapping.get("begin_ext", 0.0)),
                end_ext=quantize_layout_coord(mapping.get("end_ext", 0.0)),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "path_seg":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutPathSeg(
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                begin_pt=quantize_layout_xy(mapping.get("begin_pt")),
                end_pt=quantize_layout_xy(mapping.get("end_pt")),
                width=quantize_layout_coord(mapping.get("width", 0.0)),
                begin_style=_require_text(
                    mapping.get("begin_style", "truncate"), f"{path}.begin_style"
                ),
                end_style=_require_text(
                    mapping.get("end_style", "truncate"), f"{path}.end_style"
                ),
                begin_ext=quantize_layout_coord(mapping.get("begin_ext", 0.0)),
                end_ext=quantize_layout_coord(mapping.get("end_ext", 0.0)),
                custom_style=_canonical_custom_style(
                    mapping.get("custom_style"), f"{path}.custom_style"
                ),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "ellipse":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutEllipse(
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                bbox=canonicalize_bbox(mapping.get("bbox"), f"{path}.bbox"),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "line":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutLine(
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                points=canonicalize_polyline_points(mapping.get("points"), f"{path}.points"),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "via":
        props, _ = resolve_props()
        if "params_def" in mapping:
            if "params" in mapping:
                raise _err(f"{path} cannot specify both params_def and inline params")
            def_id = _require_text(mapping["params_def"], f"{path}.params_def")
            defn = defs.get(def_id)
            if defn is None or defn.kind != "via_params":
                raise _err(
                    f"{path}.params_def references unknown via_params definition {def_id!r}"
                )
            via_params = dict(
                _require_mapping(defn.payload.get("params", {}), f"{path}.params_def.params")
            )
        else:
            via_params = dict(
                _require_mapping(mapping.get("params", {}), f"{path}.params")
            )
        return canonicalize_layout_object(
            LayoutVia(
                via_def=_require_text(mapping.get("via_def"), f"{path}.via_def"),
                via_kind=_require_text(
                    mapping.get("via_kind", "stdVia"), f"{path}.via_kind"
                ),  # type: ignore[arg-type]
                origin=quantize_layout_xy(mapping.get("origin")),
                orient=_require_text(mapping.get("orient", "R0"), f"{path}.orient"),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                name=_require_optional_text(mapping.get("name"), f"{path}.name"),
                params=via_params,
                props=props,
            )
        )

    if obj_type == "label":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutLabel(
                text=_require_text(mapping.get("text"), f"{path}.text", nonempty=False),
                xy=quantize_layout_xy(mapping.get("xy")),
                layer=_require_text(mapping.get("layer"), f"{path}.layer"),
                purpose=_require_text(mapping.get("purpose", "drawing"), f"{path}.purpose"),
                justify=_require_text(
                    mapping.get("justify", "centerCenter"), f"{path}.justify"
                ),
                orient=_require_text(mapping.get("orient", "R0"), f"{path}.orient"),
                font=_require_text(mapping.get("font", "stick"), f"{path}.font"),
                height=quantize_layout_coord(mapping.get("height", 0.1)),
                label_type=_require_text(
                    mapping.get("label_type", "normalLabel"), f"{path}.label_type"
                ),
                net=_require_optional_text(mapping.get("net"), f"{path}.net"),
                props=props,
            )
        )

    if obj_type == "instance":
        pcell_params, pcell_types = resolve_pcell_params()
        props, prop_types = resolve_props(with_types=True)
        return canonicalize_layout_object(
            LayoutInstance(
                name=_require_text(mapping.get("name"), f"{path}.name"),
                lib=_require_text(mapping.get("lib"), f"{path}.lib"),
                cell=_require_text(mapping.get("cell"), f"{path}.cell"),
                view=_require_text(mapping.get("view", "layout"), f"{path}.view"),
                xy=quantize_layout_xy(mapping.get("xy", (0.0, 0.0))),
                orient=_require_text(mapping.get("orient", "R0"), f"{path}.orient"),
                is_pcell=_require_bool(mapping.get("is_pcell", False), f"{path}.is_pcell"),
                pcell_params=pcell_params,
                pcell_param_types=pcell_types,
                connections=_canonical_string_map(
                    mapping.get("connections", {}), f"{path}.connections"
                ),
                props=props,
                prop_types=prop_types,
            )
        )

    if obj_type == "mosaic":
        pcell_params, pcell_types = resolve_pcell_params()
        props, prop_types = resolve_props(with_types=True)
        return canonicalize_layout_object(
            LayoutMosaic(
                name=_require_text(mapping.get("name"), f"{path}.name"),
                lib=_require_text(mapping.get("lib"), f"{path}.lib"),
                cell=_require_text(mapping.get("cell"), f"{path}.cell"),
                view=_require_text(mapping.get("view", "layout"), f"{path}.view"),
                xy=quantize_layout_xy(mapping.get("xy", (0.0, 0.0))),
                orient=_require_text(mapping.get("orient", "R0"), f"{path}.orient"),
                rows=_require_int(mapping.get("rows", 1), f"{path}.rows", minimum=1),
                columns=_require_int(mapping.get("columns", 1), f"{path}.columns", minimum=1),
                u_x=quantize_layout_coord(mapping.get("u_x", 0.0)),
                u_y=quantize_layout_coord(mapping.get("u_y", 0.0)),
                is_pcell=_require_bool(mapping.get("is_pcell", False), f"{path}.is_pcell"),
                pcell_params=pcell_params,
                pcell_param_types=pcell_types,
                props=props,
                prop_types=prop_types,
            )
        )

    if obj_type == "blockage":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutBlockage(
                blockage_type=_require_text(
                    mapping.get("blockage_type"), f"{path}.blockage_type"
                ),
                points=canonicalize_ring_points(
                    mapping.get("points"), f"{path}.points", min_points=2
                ),
                layer=_require_optional_text(mapping.get("layer"), f"{path}.layer"),
                props=props,
            )
        )

    if obj_type == "boundary":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutBoundary(
                boundary_kind=_require_text(
                    mapping.get("boundary_kind", "prBoundary"), f"{path}.boundary_kind"
                ),  # type: ignore[arg-type]
                points=canonicalize_ring_points(mapping.get("points"), f"{path}.points"),
                name=_require_optional_text(mapping.get("name"), f"{path}.name"),
                props=props,
            )
        )

    if obj_type == "fig_group":
        props, _ = resolve_props()
        return canonicalize_layout_object(
            LayoutFigGroup(
                name=_require_text(mapping.get("name"), f"{path}.name"),
                origin=quantize_layout_xy(mapping.get("origin", (0.0, 0.0))),
                orient=_require_text(mapping.get("orient", "R0"), f"{path}.orient"),
                group_type=_require_text(
                    mapping.get("group_type", "default"), f"{path}.group_type"
                ),
                props=props,
            )
        )

    raise _err(f"{path}.type has unsupported layout object type {obj_type!r}")


def decode_objects_jsonl(
    raw: bytes,
    *,
    definitions: Mapping[str, LayoutDefinition] | None = None,
) -> tuple[tuple[LayoutObject, ...], dict[str, ParamValue]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotIntegrityError("objects.jsonl is not valid UTF-8") from exc
    objects: list[LayoutObject] = []
    cell_props: dict[str, ParamValue] = {}
    seen_cell_props = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise SnapshotIntegrityError(f"objects.jsonl:{line_no} is blank")
        data = _parse_json_strict(line, f"objects.jsonl:{line_no}")
        mapping = _require_mapping(data, f"objects.jsonl:{line_no}")
        if mapping.get("type") == "cell_properties":
            if seen_cell_props:
                raise _err("objects.jsonl contains duplicate cell_properties records")
            seen_cell_props = True
            cell_props = _canonical_param_map(
                mapping.get("props", {}), f"objects.jsonl:{line_no}.props", strip_metadata=True
            )
        else:
            objects.append(
                layout_object_from_dict(
                    mapping, definitions=definitions, path=f"objects.jsonl:{line_no}"
                )
            )
    return canonicalize_layout_objects(objects), cell_props


# ---------------------------------------------------------------------------
# Snapshot Build, Write, Verify, Read
# ---------------------------------------------------------------------------


def render_layout_omissions_json(omissions: Sequence[OmissionRecord]) -> str:
    canonical = _canonicalize_omissions(omissions)
    payload = [record.to_dict() for record in canonical]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def render_layout_manifest_json(manifest: LayoutManifest) -> str:
    return json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def build_layout_snapshot(
    cellview: CellViewRef,
    objects: Sequence[LayoutObject],
    *,
    cell_properties: Mapping[str, ParamValue] | None = None,
    omissions: Sequence[OmissionRecord] = (),
    metadata: Mapping[str, Any] | None = None,
) -> LayoutSnapshot:
    """Validate, canonicalize, and build a complete Manifest v2 LayoutSnapshot."""

    if not isinstance(cellview, CellViewRef):
        raise _err("cellview must be a CellViewRef")
    canon_objects = canonicalize_layout_objects(objects)
    canon_cell_props = _canonical_param_map(
        cell_properties or {}, "cell_properties", strip_metadata=True
    )
    canon_omissions = _canonicalize_omissions(omissions)
    canon_meta = _canonical_json_value(
        _require_mapping(metadata or {}, "metadata"), "metadata"
    )
    if not isinstance(canon_meta, dict):  # pragma: no cover
        raise _err("metadata must be a mapping")

    objects_bytes, defs_bytes, _ = encode_layout_jsonl(
        canon_objects, cell_properties=canon_cell_props
    )
    file_contents: dict[str, bytes] = {"objects.jsonl": objects_bytes}
    if defs_bytes is not None:
        file_contents["definitions.jsonl"] = defs_bytes
    if canon_omissions:
        file_contents["omissions.json"] = render_layout_omissions_json(
            canon_omissions
        ).encode("utf-8")

    files_digests = {name: sha256_bytes(raw) for name, raw in sorted(file_contents.items())}
    content_sha = compute_content_sha256(file_contents)
    full_sha = compute_layout_full_sha256(
        cellview=cellview,
        files=files_digests,
        omissions_count=len(canon_omissions),
        metadata=canon_meta,
    )
    manifest = LayoutManifest(
        schema=LAYOUT_MANIFEST_SCHEMA,
        view_type=LAYOUT_VIEW_TYPE,
        cellview=cellview,
        content_sha256=content_sha,
        full_sha256=full_sha,
        files=files_digests,
        electrical_sha256=None,
        placement_sha256=None,
        omissions_count=len(canon_omissions),
        metadata=canon_meta,
    )
    return LayoutSnapshot(
        manifest=manifest,
        objects=canon_objects,
        cell_properties=canon_cell_props,
        omissions=canon_omissions,
    )


def layout_snapshot_dir_for(root: Path, cellview: CellViewRef) -> Path:
    """Return canonical directory ``<root>/<library>/<cell>/<view>``."""

    if not isinstance(cellview, CellViewRef):
        raise _err("cellview must be a CellViewRef")
    return Path(root) / cellview.library / cellview.cell / cellview.view


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_layout_snapshot(snapshot: LayoutSnapshot, dest_dir: Path) -> Path:
    """Atomically write canonical Manifest v2 + JSONL layout snapshot files."""

    if not isinstance(snapshot, LayoutSnapshot):
        raise _err("snapshot must be a LayoutSnapshot")
    rebuilt = build_layout_snapshot(
        snapshot.manifest.cellview,
        snapshot.objects,
        cell_properties=snapshot.cell_properties,
        omissions=snapshot.omissions,
        metadata=snapshot.manifest.metadata,
    )
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    objects_bytes, defs_bytes, _ = encode_layout_jsonl(
        rebuilt.objects, cell_properties=rebuilt.cell_properties
    )
    _atomic_write_bytes(dest / "objects.jsonl", objects_bytes)
    defs_path = dest / "definitions.jsonl"
    if defs_bytes is not None:
        _atomic_write_bytes(defs_path, defs_bytes)
    elif defs_path.exists() or defs_path.is_symlink():
        defs_path.unlink()

    omissions_path = dest / "omissions.json"
    if rebuilt.omissions:
        _atomic_write_bytes(
            omissions_path,
            render_layout_omissions_json(rebuilt.omissions).encode("utf-8"),
        )
    elif omissions_path.exists() or omissions_path.is_symlink():
        omissions_path.unlink()

    _atomic_write_bytes(
        dest / "manifest.json",
        render_layout_manifest_json(rebuilt.manifest).encode("utf-8"),
    )
    return dest


def _read_regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SnapshotIntegrityError(f"Missing or symlinked snapshot file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Unable to read snapshot file {path}: {exc}") from exc


def _read_layout_manifest(
    snapshot_dir: Path, *, require_canonical: bool = True
) -> LayoutManifest:
    root = Path(snapshot_dir)
    if not root.is_dir():
        raise SnapshotIntegrityError(f"Snapshot directory does not exist: {root}")
    raw = _read_regular_bytes(root / "manifest.json")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotIntegrityError("manifest.json is not valid UTF-8") from exc
    data = _parse_json_strict(text, "manifest.json")
    mapping = _require_mapping(data, "manifest.json")
    unknown = set(mapping) - _MANIFEST_KEYS
    if unknown:
        raise _err(f"manifest.json contains unsupported keys: {sorted(unknown)!r}")
    manifest = LayoutManifest.from_dict(dict(mapping))
    for name, digest in manifest.files.items():
        if name not in _ALLOWED_SNAPSHOT_FILES:
            raise _err(f"manifest.files contains unsupported file {name!r}")
        _require_hash(digest, f"manifest.files.{name}")
    if "objects.jsonl" not in manifest.files:
        raise _err("manifest.files must include 'objects.jsonl'")
    _require_hash(manifest.content_sha256, "manifest.content_sha256")
    _require_hash(manifest.full_sha256, "manifest.full_sha256")
    if manifest.omissions_count > 0 and "omissions.json" not in manifest.files:
        raise _err("manifest.files must include omissions.json when omissions_count > 0")
    if manifest.omissions_count == 0 and "omissions.json" in manifest.files:
        raise _err("manifest.files must omit omissions.json when omissions_count == 0")
    if require_canonical:
        expected_raw = render_layout_manifest_json(manifest).encode("utf-8")
        if raw != expected_raw:
            raise SnapshotIntegrityError("manifest.json is not in canonical form")
    return manifest


def verify_layout_snapshot_integrity(snapshot_dir: Path) -> LayoutManifest:
    """Verify Manifest v2, file SHA-256s, ``content_sha256``, ``full_sha256``, and omissions."""

    root = Path(snapshot_dir)
    manifest = _read_layout_manifest(root, require_canonical=True)

    file_bytes: dict[str, bytes] = {}
    for name, expected_hash in sorted(manifest.files.items()):
        raw = _read_regular_bytes(root / name)
        actual_hash = sha256_bytes(raw)
        if actual_hash != expected_hash:
            raise SnapshotIntegrityError(
                f"SHA-256 mismatch for {name}: expected {expected_hash}, got {actual_hash}"
            )
        file_bytes[name] = raw

    # Reject stale unreferenced optional files.
    for opt in ("definitions.jsonl", "omissions.json"):
        opt_path = root / opt
        if opt not in manifest.files and (opt_path.exists() or opt_path.is_symlink()):
            raise SnapshotIntegrityError(f"Stale unreferenced {opt} exists in {root}")

    expected_content = compute_content_sha256(file_bytes)
    if manifest.content_sha256 != expected_content:
        raise SnapshotIntegrityError(
            f"content_sha256 mismatch: expected {expected_content}, got {manifest.content_sha256}"
        )

    expected_full = compute_layout_full_sha256(
        cellview=manifest.cellview,
        files=manifest.files,
        omissions_count=manifest.omissions_count,
        metadata=manifest.metadata,
    )
    if manifest.full_sha256 != expected_full:
        raise SnapshotIntegrityError(
            f"full_sha256 mismatch: expected {expected_full}, got {manifest.full_sha256}"
        )
    return manifest


def read_layout_snapshot(snapshot_dir: Path, *, verify: bool = True) -> LayoutSnapshot:
    """Read a layout snapshot directory, validating digests and JSONL schemas."""

    if type(verify) is not bool:
        raise _err("verify must be a bool")
    root = Path(snapshot_dir)
    manifest = (
        verify_layout_snapshot_integrity(root)
        if verify
        else _read_layout_manifest(root, require_canonical=False)
    )
    definitions: dict[str, LayoutDefinition] = {}
    if "definitions.jsonl" in manifest.files:
        definitions = decode_definitions_jsonl(_read_regular_bytes(root / "definitions.jsonl"))

    objects, cell_props = decode_objects_jsonl(
        _read_regular_bytes(root / "objects.jsonl"), definitions=definitions
    )

    omissions: tuple[OmissionRecord, ...] = ()
    if manifest.omissions_count > 0:
        try:
            raw_om = _read_regular_bytes(root / "omissions.json").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotIntegrityError("omissions.json is not valid UTF-8") from exc
        data_om = _parse_json_strict(raw_om, "omissions.json")
        seq = _require_sequence(data_om, "omissions.json")
        parsed = []
        for idx, item in enumerate(seq):
            m = _require_mapping(item, f"omissions[{idx}]")
            parsed.append(
                OmissionRecord(
                    kind=_require_text(m.get("kind"), f"omissions[{idx}].kind"),
                    identifier=_require_text(
                        m.get("identifier"), f"omissions[{idx}].identifier"
                    ),
                    reason=_require_text(m.get("reason"), f"omissions[{idx}].reason"),
                    details=dict(_require_mapping(m.get("details", {}), f"omissions[{idx}].details")),
                )
            )
        omissions = _canonicalize_omissions(parsed)
    if len(omissions) != manifest.omissions_count:
        raise SchemaValidationError(
            f"manifest omissions_count={manifest.omissions_count} does not match "
            f"omissions.json length {len(omissions)}"
        )

    return LayoutSnapshot(
        manifest=manifest,
        objects=objects,
        cell_properties=cell_props,
        omissions=omissions,
    )


__all__ = [
    "build_layout_snapshot",
    "canonical_json_bytes",
    "canonicalize_bbox",
    "canonicalize_layout_object",
    "canonicalize_layout_objects",
    "canonicalize_polyline_points",
    "canonicalize_ring_points",
    "compute_content_sha256",
    "compute_layout_full_sha256",
    "decode_definitions_jsonl",
    "decode_objects_jsonl",
    "encode_layout_jsonl",
    "layout_object_from_dict",
    "layout_snapshot_dir_for",
    "quantize_layout_coord",
    "quantize_layout_xy",
    "read_layout_snapshot",
    "render_layout_manifest_json",
    "render_layout_omissions_json",
    "sha256_bytes",
    "verify_layout_snapshot_integrity",
    "write_layout_snapshot",
]
