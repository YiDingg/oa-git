"""Deterministic object-level three-way merge for Symbol snapshots.

Performs domain-aware three-way merges across:
- Per-pin fields (``direction``, ``pin_figure``, ``xy``, ``orient`` independently).
- Positional ``port_order`` (conflicts whenever both branches diverge differently).
- Cellview scalar ``properties`` per key.
- Graphics shapes & typed labels as canonical multisets, and ``selection_box`` / ``origin``
  as scalar fields.
- Fail-closed conflict behavior: any conflict yields ``ok=False`` and ``snapshot=None``
  (never a partially merged symbol snapshot).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal

from .model import OmissionRecord, SchemaValidationError
from .symbol_model import (
    BBox,
    Point2D,
    SymbolGraphics,
    SymbolLabel,
    SymbolPin,
    SymbolPinFigure,
    SymbolShape,
    SymbolSnapshot,
)
from .symbol_snapshot import (
    build_symbol_snapshot,
    canonicalize_port_order,
    canonicalize_symbol_graphics,
    canonicalize_symbol_pins,
    canonicalize_symbol_properties,
    quantize_symbol_coord,
)

SymbolConflictDomain = Literal["pin_contract", "pin_geometry", "graphics"]
_MISSING = object()


@dataclass(frozen=True)
class SymbolMergeConflict:
    """One deterministic conflict recorded during three-way symbol merge."""

    domain: SymbolConflictDomain
    path: str
    base: Any
    ours: Any
    theirs: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "path": self.path,
            "base": _to_plain(self.base),
            "ours": _to_plain(self.ours),
            "theirs": _to_plain(self.theirs),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SymbolMergeResult:
    """Outcome of merging three SymbolSnapshots."""

    ok: bool
    snapshot: SymbolSnapshot | None
    conflicts: tuple[SymbolMergeConflict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot": _to_plain(self.snapshot),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


# ---------------------------------------------------------------------------
# Strict Comparison & Deterministic Helpers
# ---------------------------------------------------------------------------


def _strict_equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        if len(left) != len(right):
            return False
        return all(k in right and _strict_equal(v, right[k]) for k, v in left.items())
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, (set, frozenset)):
        return len(left) == len(right) and sorted(
            _stable_key(i) for i in left
        ) == sorted(_stable_key(i) for i in right)
    if is_dataclass(left):
        return all(
            _strict_equal(getattr(left, f.name), getattr(right, f.name))
            for f in fields(left)
        )
    return left == right


def _stable_key(value: Any) -> tuple[Any, ...]:
    if value is _MISSING:
        return ("missing",)
    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", int(value))
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", repr(value))
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (tuple, list)):
        return (type(value).__name__, tuple(_stable_key(i) for i in value))
    if isinstance(value, Mapping):
        return (
            "mapping",
            tuple(
                sorted(
                    ((_stable_key(k), _stable_key(v)) for k, v in value.items()),
                    key=lambda p: p[0],
                )
            ),
        )
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted(_stable_key(i) for i in value)))
    if is_dataclass(value):
        return (
            "dataclass",
            type(value).__qualname__,
            tuple((f.name, _stable_key(getattr(value, f.name))) for f in fields(value)),
        )
    return ("object", type(value).__qualname__, repr(value))


def _to_plain(value: Any) -> Any:
    if value is _MISSING:
        return None
    if is_dataclass(value):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _to_plain(value[k]) for k in sorted(value, key=_stable_key)}
    if isinstance(value, (tuple, list)):
        return [_to_plain(i) for i in value]
    if isinstance(value, (set, frozenset)):
        return [_to_plain(i) for i in sorted(value, key=_stable_key)]
    return value


def _clone_value(value: Any) -> Any:
    """Copy mutable containers (list/dict) so merged properties never alias inputs."""

    if isinstance(value, list):
        return [_clone_value(i) for i in value]
    if isinstance(value, dict):
        return {k: _clone_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(_clone_value(i) for i in value)
    return value


def _conflict_value(value: Any) -> Any:
    return None if value is _MISSING else value


def _sort_conflicts(
    conflicts: Sequence[SymbolMergeConflict],
) -> tuple[SymbolMergeConflict, ...]:
    return tuple(sorted(conflicts, key=lambda c: (c.domain, c.path, c.reason)))


def _merge_atom(
    base: Any,
    ours: Any,
    theirs: Any,
    *,
    domain: SymbolConflictDomain,
    path: str,
    conflicts: list[SymbolMergeConflict],
) -> Any:
    if _strict_equal(ours, base):
        return theirs
    if _strict_equal(theirs, base):
        return ours
    if _strict_equal(ours, theirs):
        return ours

    if base is _MISSING:
        reason = "concurrent-addition"
    elif ours is _MISSING or theirs is _MISSING:
        reason = "delete/modify"
    else:
        reason = "modify/modify"
    conflicts.append(
        SymbolMergeConflict(
            domain=domain,
            path=path,
            base=_conflict_value(base),
            ours=_conflict_value(ours),
            theirs=_conflict_value(theirs),
            reason=reason,
        )
    )
    return ours if ours is not _MISSING else theirs


def _merge_mapping(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    *,
    domain: SymbolConflictDomain,
    path_prefix: str,
    conflicts: list[SymbolMergeConflict],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        merged = _merge_atom(
            base.get(key, _MISSING),
            ours.get(key, _MISSING),
            theirs.get(key, _MISSING),
            domain=domain,
            path=f"{path_prefix}.{key}",
            conflicts=conflicts,
        )
        if merged is not _MISSING:
            out[key] = _clone_value(merged)
    return out


def _fmt_coord(value: float) -> str:
    return f"{0.0 if value == 0.0 else value:g}"


def _fmt_xy(xy: Point2D) -> str:
    return f"({_fmt_coord(xy[0])}, {_fmt_coord(xy[1])})"


def _fmt_bbox(bbox: BBox) -> str:
    return f"[{_fmt_xy(bbox[0])}..{_fmt_xy(bbox[1])}]"


def _describe_multiset_item(item: Any) -> str:
    if isinstance(item, SymbolShape):
        geom = ""
        if item.bbox is not None:
            geom = f", {_fmt_bbox(item.bbox)}"
        elif item.points:
            sep = "->" if item.kind in {"line", "path"} else ", "
            geom = ", " + sep.join(_fmt_xy(p) for p in item.points)
        elif item.arc_bbox is not None:
            geom = f", arc={_fmt_bbox(item.arc_bbox)}"
        return f"{item.kind}({item.layer}:{item.purpose}{geom})"
    if isinstance(item, SymbolLabel):
        return f"label({item.text!r}, {_fmt_xy(item.xy)})"
    if isinstance(item, OmissionRecord):
        return f"omission({item.kind}:{item.identifier}:{item.reason})"
    return repr(_to_plain(item))


def _merge_multiset(
    base: Sequence[Any],
    ours: Sequence[Any],
    theirs: Sequence[Any],
    *,
    domain: SymbolConflictDomain,
    path_prefix: str,
    conflicts: list[SymbolMergeConflict],
) -> tuple[Any, ...]:
    groups: list[dict[tuple[Any, ...], list[Any]]] = []
    for values in (base, ours, theirs):
        grouped: dict[tuple[Any, ...], list[Any]] = {}
        for item in values:
            grouped.setdefault(_stable_key(item), []).append(item)
        groups.append(grouped)

    b_grp, o_grp, t_grp = groups
    result: list[Any] = []
    for idx, key in enumerate(sorted(set(b_grp) | set(o_grp) | set(t_grp))):
        b_cnt = len(b_grp.get(key, []))
        o_cnt = len(o_grp.get(key, []))
        t_cnt = len(t_grp.get(key, []))
        rep = next(
            (g[key][0] for g in (o_grp, t_grp, b_grp) if g.get(key)),
            None,
        )
        item_path = (
            f"{path_prefix}[{idx}:{_describe_multiset_item(rep)}]"
            if rep is not None
            else f"{path_prefix}[{idx}]"
        )
        count = _merge_atom(
            b_cnt,
            o_cnt,
            t_cnt,
            domain=domain,
            path=item_path,
            conflicts=conflicts,
        )
        if count is _MISSING or count <= 0:
            continue
        if rep is not None:
            result.extend([rep] * count)
    return tuple(result)


# ---------------------------------------------------------------------------
# Domain-Specific Mergers
# ---------------------------------------------------------------------------


def _norm_zero(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _bbox_size(bbox: BBox | None) -> tuple[float, float] | None:
    if bbox is None:
        return None
    return (
        _norm_zero(round(bbox[1][0] - bbox[0][0], 6)),
        _norm_zero(round(bbox[1][1] - bbox[0][1], 6)),
    )


def _bbox_center2(bbox: BBox | None) -> tuple[float, float] | None:
    """Return ``(x0 + x1, y0 + y1)`` (2 * center) to avoid half-bit rounding drift."""

    if bbox is None:
        return None
    return (
        _norm_zero(round(bbox[0][0] + bbox[1][0], 6)),
        _norm_zero(round(bbox[0][1] + bbox[1][1], 6)),
    )


def _poly_anchor_and_rel(
    points: tuple[Point2D, ...],
) -> tuple[Point2D | None, tuple[Point2D, ...]]:
    if not points:
        return None, ()
    min_x = _norm_zero(round(min(p[0] for p in points), 6))
    min_y = _norm_zero(round(min(p[1] for p in points), 6))
    rel = tuple(
        (_norm_zero(round(p[0] - min_x, 6)), _norm_zero(round(p[1] - min_y, 6)))
        for p in points
    )
    return (min_x, min_y), rel


def _merge_pin_figure(
    name: str,
    b_fig: SymbolPinFigure,
    o_fig: SymbolPinFigure,
    t_fig: SymbolPinFigure,
    conflicts: list[SymbolMergeConflict],
) -> SymbolPinFigure:
    """Merge pin_figure contract fields separately from absolute bbox/points translation."""

    kind = _merge_atom(
        b_fig.kind,
        o_fig.kind,
        t_fig.kind,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.kind",
        conflicts=conflicts,
    )
    layer = _merge_atom(
        b_fig.layer,
        o_fig.layer,
        t_fig.layer,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.layer",
        conflicts=conflicts,
    )
    purpose = _merge_atom(
        b_fig.purpose,
        o_fig.purpose,
        t_fig.purpose,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.purpose",
        conflicts=conflicts,
    )
    lib = _merge_atom(
        b_fig.lib,
        o_fig.lib,
        t_fig.lib,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.lib",
        conflicts=conflicts,
    )
    cell = _merge_atom(
        b_fig.cell,
        o_fig.cell,
        t_fig.cell,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.cell",
        conflicts=conflicts,
    )
    view = _merge_atom(
        b_fig.view,
        o_fig.view,
        t_fig.view,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.view",
        conflicts=conflicts,
    )
    if kind != "inst":
        lib = None
        cell = None
        view = None

    b_size = _bbox_size(b_fig.bbox)
    o_size = _bbox_size(o_fig.bbox)
    t_size = _bbox_size(t_fig.bbox)
    merged_size = _merge_atom(
        b_size,
        o_size,
        t_size,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.size",
        conflicts=conflicts,
    )

    merged_bbox: BBox | None = None
    if merged_size is not None:
        b_c2 = _bbox_center2(b_fig.bbox)
        o_c2 = _bbox_center2(o_fig.bbox)
        t_c2 = _bbox_center2(t_fig.bbox)
        merged_c2 = _merge_atom(
            _MISSING if b_c2 is None else b_c2,
            _MISSING if o_c2 is None else o_c2,
            _MISSING if t_c2 is None else t_c2,
            domain="pin_geometry",
            path=f"pins.{name}.pin_figure.bbox",
            conflicts=conflicts,
        )
        if merged_c2 is not _MISSING and merged_c2 is not None:
            if o_fig.bbox is not None and o_size == merged_size and o_c2 == merged_c2:
                merged_bbox = o_fig.bbox
            elif t_fig.bbox is not None and t_size == merged_size and t_c2 == merged_c2:
                merged_bbox = t_fig.bbox
            elif b_fig.bbox is not None and b_size == merged_size and b_c2 == merged_c2:
                merged_bbox = b_fig.bbox
            else:
                cx = merged_c2[0] / 2.0
                cy = merged_c2[1] / 2.0
                dx, dy = merged_size
                x0 = quantize_symbol_coord(cx - dx / 2.0)
                x1 = quantize_symbol_coord(cx + dx / 2.0)
                y0 = quantize_symbol_coord(cy - dy / 2.0)
                y1 = quantize_symbol_coord(cy + dy / 2.0)
                merged_bbox = ((min(x0, x1), min(y0, y1)), (max(x0, x1), max(y0, y1)))

    b_anchor, b_rel = _poly_anchor_and_rel(b_fig.points)
    o_anchor, o_rel = _poly_anchor_and_rel(o_fig.points)
    t_anchor, t_rel = _poly_anchor_and_rel(t_fig.points)
    merged_rel = _merge_atom(
        b_rel,
        o_rel,
        t_rel,
        domain="pin_contract",
        path=f"pins.{name}.pin_figure.shape",
        conflicts=conflicts,
    )
    merged_points: tuple[Point2D, ...] = ()
    if kind == "polygon" and merged_rel:
        merged_anchor = _merge_atom(
            _MISSING if b_anchor is None else b_anchor,
            _MISSING if o_anchor is None else o_anchor,
            _MISSING if t_anchor is None else t_anchor,
            domain="pin_geometry",
            path=f"pins.{name}.pin_figure.points",
            conflicts=conflicts,
        )
        if merged_anchor is not _MISSING and merged_anchor is not None:
            if o_fig.points and o_rel == merged_rel and o_anchor == merged_anchor:
                merged_points = o_fig.points
            elif t_fig.points and t_rel == merged_rel and t_anchor == merged_anchor:
                merged_points = t_fig.points
            elif b_fig.points and b_rel == merged_rel and b_anchor == merged_anchor:
                merged_points = b_fig.points
            else:
                ax, ay = merged_anchor
                merged_points = tuple(
                    (quantize_symbol_coord(ax + rx), quantize_symbol_coord(ay + ry))
                    for rx, ry in merged_rel
                )

    return SymbolPinFigure(
        kind=kind,
        layer=layer,
        purpose=purpose,
        bbox=merged_bbox,
        lib=lib,
        cell=cell,
        view=view,
        points=merged_points,
    )


def _merge_pins(
    base: Sequence[SymbolPin],
    ours: Sequence[SymbolPin],
    theirs: Sequence[SymbolPin],
    conflicts: list[SymbolMergeConflict],
) -> tuple[SymbolPin, ...]:
    b_map = {p.name: p for p in base}
    o_map = {p.name: p for p in ours}
    t_map = {p.name: p for p in theirs}
    out: list[SymbolPin] = []

    for name in sorted(set(b_map) | set(o_map) | set(t_map)):
        bp = b_map.get(name, _MISSING)
        op = o_map.get(name, _MISSING)
        tp = t_map.get(name, _MISSING)
        if bp is not _MISSING and op is not _MISSING and tp is not _MISSING:
            assert isinstance(bp, SymbolPin)
            assert isinstance(op, SymbolPin)
            assert isinstance(tp, SymbolPin)
            direction = _merge_atom(
                bp.direction,
                op.direction,
                tp.direction,
                domain="pin_contract",
                path=f"pins.{name}.direction",
                conflicts=conflicts,
            )
            pin_figure = _merge_pin_figure(
                name,
                bp.pin_figure,
                op.pin_figure,
                tp.pin_figure,
                conflicts,
            )
            xy = _merge_atom(
                bp.xy,
                op.xy,
                tp.xy,
                domain="pin_geometry",
                path=f"pins.{name}.xy",
                conflicts=conflicts,
            )
            orient = _merge_atom(
                bp.orient,
                op.orient,
                tp.orient,
                domain="pin_geometry",
                path=f"pins.{name}.orient",
                conflicts=conflicts,
            )
            out.append(
                SymbolPin(
                    name=name,
                    direction=direction,
                    xy=xy,
                    orient=orient,
                    pin_figure=pin_figure,
                )
            )
        else:
            merged = _merge_atom(
                bp,
                op,
                tp,
                domain="pin_contract",
                path=f"pins.{name}",
                conflicts=conflicts,
            )
            if merged is not _MISSING:
                out.append(merged)
    return tuple(out)


def _merge_derived_bbox(
    base: BBox | None, ours: BBox | None, theirs: BBox | None
) -> BBox | None:
    """Merge derived cellview ``bbox`` without false conflicts when graphics expand independently."""

    if _strict_equal(ours, base):
        return theirs
    if _strict_equal(theirs, base):
        return ours
    if _strict_equal(ours, theirs):
        return ours
    if ours is not None and theirs is not None:
        return (
            (min(ours[0][0], theirs[0][0]), min(ours[0][1], theirs[0][1])),
            (max(ours[1][0], theirs[1][0]), max(ours[1][1], theirs[1][1])),
        )
    return None


def _merge_graphics(
    base: SymbolGraphics,
    ours: SymbolGraphics,
    theirs: SymbolGraphics,
    conflicts: list[SymbolMergeConflict],
) -> SymbolGraphics:
    shapes = _merge_multiset(
        base.shapes,
        ours.shapes,
        theirs.shapes,
        domain="graphics",
        path_prefix="graphics.shapes",
        conflicts=conflicts,
    )
    labels = _merge_multiset(
        base.labels,
        ours.labels,
        theirs.labels,
        domain="graphics",
        path_prefix="graphics.labels",
        conflicts=conflicts,
    )
    nlp_labels = _merge_multiset(
        base.nlp_labels,
        ours.nlp_labels,
        theirs.nlp_labels,
        domain="graphics",
        path_prefix="graphics.nlp_labels",
        conflicts=conflicts,
    )
    il_labels = _merge_multiset(
        base.il_labels,
        ours.il_labels,
        theirs.il_labels,
        domain="graphics",
        path_prefix="graphics.il_labels",
        conflicts=conflicts,
    )
    selection_box = _merge_atom(
        base.selection_box,
        ours.selection_box,
        theirs.selection_box,
        domain="graphics",
        path="graphics.selection_box",
        conflicts=conflicts,
    )
    origin = _merge_atom(
        base.origin,
        ours.origin,
        theirs.origin,
        domain="graphics",
        path="graphics.origin",
        conflicts=conflicts,
    )
    bbox = _merge_derived_bbox(base.bbox, ours.bbox, theirs.bbox)
    return SymbolGraphics(
        shapes=shapes,
        labels=labels,
        nlp_labels=nlp_labels,
        il_labels=il_labels,
        selection_box=selection_box,
        origin=origin,
        bbox=bbox,
    )


def merge_symbols(
    base: SymbolSnapshot,
    ours: SymbolSnapshot,
    theirs: SymbolSnapshot,
) -> SymbolMergeResult:
    """Three-way merge two symbol snapshot descendants against a common base."""

    cellviews = (base.cellview, ours.cellview, theirs.cellview)
    if not (cellviews[0] == cellviews[1] == cellviews[2]):
        raise SchemaValidationError(
            "cannot merge symbol snapshots with mismatched cellview identities: "
            + ", ".join(cv.key for cv in cellviews)
        )

    b_pins = canonicalize_symbol_pins(base.pins)
    o_pins = canonicalize_symbol_pins(ours.pins)
    t_pins = canonicalize_symbol_pins(theirs.pins)

    b_po = canonicalize_port_order(base.port_order)
    o_po = canonicalize_port_order(ours.port_order)
    t_po = canonicalize_port_order(theirs.port_order)

    b_gfx = canonicalize_symbol_graphics(base.graphics)
    o_gfx = canonicalize_symbol_graphics(ours.graphics)
    t_gfx = canonicalize_symbol_graphics(theirs.graphics)

    b_props = canonicalize_symbol_properties(base.properties)
    o_props = canonicalize_symbol_properties(ours.properties)
    t_props = canonicalize_symbol_properties(theirs.properties)

    conflicts: list[SymbolMergeConflict] = []
    merged_pins = _merge_pins(b_pins, o_pins, t_pins, conflicts)
    merged_port_order = _merge_atom(
        b_po,
        o_po,
        t_po,
        domain="pin_contract",
        path="port_order",
        conflicts=conflicts,
    )
    merged_pin_names = {p.name for p in merged_pins}
    dangling_ports = tuple(
        name for name in merged_port_order if name not in merged_pin_names
    )
    if dangling_ports:
        conflicts.append(
            SymbolMergeConflict(
                domain="pin_contract",
                path="port_order",
                base=b_po,
                ours=o_po,
                theirs=t_po,
                reason=f"dangling-port-order: {', '.join(dangling_ports)}",
            )
        )
    merged_props = _merge_mapping(
        b_props,
        o_props,
        t_props,
        domain="pin_contract",
        path_prefix="properties",
        conflicts=conflicts,
    )
    merged_graphics = _merge_graphics(b_gfx, o_gfx, t_gfx, conflicts)
    merged_metadata = _merge_mapping(
        base.manifest.metadata,
        ours.manifest.metadata,
        theirs.manifest.metadata,
        domain="graphics",
        path_prefix="manifest.metadata",
        conflicts=conflicts,
    )
    merged_omissions = _merge_multiset(
        base.omissions,
        ours.omissions,
        theirs.omissions,
        domain="graphics",
        path_prefix="omissions",
        conflicts=conflicts,
    )

    sorted_conflicts = _sort_conflicts(conflicts)
    if sorted_conflicts:
        return SymbolMergeResult(ok=False, snapshot=None, conflicts=sorted_conflicts)

    merged_snapshot = build_symbol_snapshot(
        ours.cellview,
        pins=merged_pins,
        port_order=merged_port_order,
        graphics=merged_graphics,
        properties=merged_props,
        omissions=tuple(merged_omissions),
        metadata=merged_metadata,
    )
    return SymbolMergeResult(ok=True, snapshot=merged_snapshot, conflicts=())


_DOMAIN_HEADERS: dict[str, str] = {
    "pin_contract": "Pin contract conflicts",
    "pin_geometry": "Pin geometry conflicts",
    "graphics": "Graphics conflicts",
}


def format_symbol_merge_conflicts(result: SymbolMergeResult) -> str:
    """Render symbol merge conflicts in deterministic human-readable text."""

    if not result.conflicts:
        return "No symbol merge conflicts."
    sorted_conflicts = _sort_conflicts(result.conflicts)
    lines = [f"Symbol merge conflicts ({len(sorted_conflicts)}):"]
    grouped: dict[str, list[SymbolMergeConflict]] = {}
    for c in sorted_conflicts:
        grouped.setdefault(c.domain, []).append(c)
    for domain, items in grouped.items():
        header = _DOMAIN_HEADERS.get(domain, f"{domain} conflicts")
        lines.append(f"{header} ({len(items)}):")
        for c in items:
            lines.append(
                f"  - [{c.domain}] {c.path}: {c.reason}; "
                f"base={_to_plain(c.base)!r}, "
                f"ours={_to_plain(c.ours)!r}, "
                f"theirs={_to_plain(c.theirs)!r}"
            )
    return "\n".join(lines)


__all__ = [
    "SymbolConflictDomain",
    "SymbolMergeConflict",
    "SymbolMergeResult",
    "merge_symbols",
    "format_symbol_merge_conflicts",
]
