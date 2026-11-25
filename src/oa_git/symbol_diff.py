"""Deterministic three-domain semantic diff engine for Symbol snapshots.

Separates symbol changes into three strict domains:
1. ``pin_contract_changes``: electrical/interface changes (pin added/removed,
   pin direction or pin_figure changed, ``port_order`` changed, cellview properties changed).
   Pin renames are never guessed and appear strictly as remove + add.
2. ``pin_geometry_changes``: existing pin ``xy`` or ``orient`` moved without altering
   its electrical contract.
3. ``graphics_changes``: cosmetic visual changes (shapes, normal labels, NLP labels,
   IL labels, ``selection_box``, ``origin``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal, TypeVar

from .model import CellViewRef
from .symbol_model import (
    BBox,
    Point2D,
    SymbolLabel,
    SymbolPin,
    SymbolPinFigure,
    SymbolShape,
    SymbolSnapshot,
)

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Strict Equality & Deterministic Ordering Helpers
# ---------------------------------------------------------------------------


def _strict_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        if len(left) != len(right):
            return False
        for k, v in left.items():
            if k not in right or not _strict_equal(v, right[k]):
                return False
        return True
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, (set, frozenset)):
        if len(left) != len(right):
            return False
        return {_stable_key(item) for item in left} == {
            _stable_key(item) for item in right
        }
    if is_dataclass(left):
        return all(
            _strict_equal(getattr(left, f.name), getattr(right, f.name))
            for f in fields(left)
        )
    return left == right


def _stable_key(value: Any) -> tuple[Any, ...]:
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
                    key=lambda pair: pair[0],
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
    if is_dataclass(value):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _to_plain(value[k]) for k in sorted(value, key=_stable_key)}
    if isinstance(value, (tuple, list)):
        return [_to_plain(i) for i in value]
    if isinstance(value, (set, frozenset)):
        return [_to_plain(i) for i in sorted(value, key=_stable_key)]
    return value


def _multiset_diff(
    old: Sequence[_T], new: Sequence[_T]
) -> tuple[tuple[_T, ...], tuple[_T, ...]]:
    old_groups: dict[tuple[Any, ...], list[_T]] = defaultdict(list)
    new_groups: dict[tuple[Any, ...], list[_T]] = defaultdict(list)
    for item in old:
        old_groups[_stable_key(item)].append(item)
    for item in new:
        new_groups[_stable_key(item)].append(item)

    added: list[_T] = []
    removed: list[_T] = []
    for key in sorted(set(old_groups) | set(new_groups)):
        old_items = old_groups.get(key, [])
        new_items = new_groups.get(key, [])
        common = min(len(old_items), len(new_items))
        removed.extend(old_items[common:])
        added.extend(new_items[common:])
    return tuple(added), tuple(removed)


# ---------------------------------------------------------------------------
# Diff Domain Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolPinContractChange:
    name: str
    change_type: Literal["added", "removed", "modified"]
    old_direction: str | None = None
    new_direction: str | None = None
    old_pin_figure: SymbolPinFigure | None = None
    new_pin_figure: SymbolPinFigure | None = None


@dataclass(frozen=True)
class PortOrderChange:
    old_order: tuple[str, ...]
    new_order: tuple[str, ...]


@dataclass(frozen=True)
class SymbolPropertyChange:
    key: str
    old: Any
    new: Any
    change_type: Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class SymbolPinGeometryChange:
    name: str
    old_xy: Point2D
    new_xy: Point2D
    old_orient: str
    new_orient: str
    old_pin_figure_bbox: BBox | None = None
    new_pin_figure_bbox: BBox | None = None


@dataclass(frozen=True)
class SymbolFieldChange:
    field: str
    old: Any
    new: Any


@dataclass(frozen=True)
class PinContractDiff:
    """Electrical / interface contract changes on a symbol."""

    pins: tuple[SymbolPinContractChange, ...] = ()
    port_order_change: PortOrderChange | None = None
    property_changes: tuple[SymbolPropertyChange, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "pins", tuple(sorted(self.pins, key=_stable_key)))
        object.__setattr__(
            self,
            "property_changes",
            tuple(sorted(self.property_changes, key=_stable_key)),
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.pins or self.port_order_change is not None or self.property_changes
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class PinGeometryDiff:
    """Existing pin placement changes (xy or orient) without contract changes."""

    moves: tuple[SymbolPinGeometryChange, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "moves", tuple(sorted(self.moves, key=_stable_key)))

    @property
    def is_empty(self) -> bool:
        return not self.moves

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class GraphicsDiff:
    """Cosmetic visual changes to shapes, labels, selection box, and origin."""

    shapes_added: tuple[SymbolShape, ...] = ()
    shapes_removed: tuple[SymbolShape, ...] = ()
    labels_added: tuple[SymbolLabel, ...] = ()
    labels_removed: tuple[SymbolLabel, ...] = ()
    nlp_labels_added: tuple[SymbolLabel, ...] = ()
    nlp_labels_removed: tuple[SymbolLabel, ...] = ()
    il_labels_added: tuple[SymbolLabel, ...] = ()
    il_labels_removed: tuple[SymbolLabel, ...] = ()
    selection_box_change: SymbolFieldChange | None = None
    origin_change: SymbolFieldChange | None = None

    def __post_init__(self) -> None:
        for attr in (
            "shapes_added",
            "shapes_removed",
            "labels_added",
            "labels_removed",
            "nlp_labels_added",
            "nlp_labels_removed",
            "il_labels_added",
            "il_labels_removed",
        ):
            vals = tuple(getattr(self, attr))
            object.__setattr__(self, attr, tuple(sorted(vals, key=_stable_key)))

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.shapes_added,
                self.shapes_removed,
                self.labels_added,
                self.labels_removed,
                self.nlp_labels_added,
                self.nlp_labels_removed,
                self.il_labels_added,
                self.il_labels_removed,
                self.selection_box_change is not None,
                self.origin_change is not None,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class SymbolDiff:
    """Three-domain deterministic semantic diff between two SymbolSnapshots."""

    old_cellview: CellViewRef
    new_cellview: CellViewRef
    pin_contract_changes: PinContractDiff
    pin_geometry_changes: PinGeometryDiff
    graphics_changes: GraphicsDiff

    @property
    def cellview_changed(self) -> bool:
        return self.old_cellview != self.new_cellview

    @property
    def has_pin_contract_changes(self) -> bool:
        return not self.pin_contract_changes.is_empty

    @property
    def has_pin_geometry_changes(self) -> bool:
        return not self.pin_geometry_changes.is_empty

    @property
    def has_graphics_changes(self) -> bool:
        return not self.graphics_changes.is_empty

    @property
    def is_graphics_only(self) -> bool:
        return (
            self.has_graphics_changes
            and not self.has_pin_contract_changes
            and not self.has_pin_geometry_changes
        )

    @property
    def is_empty(self) -> bool:
        return (
            not self.cellview_changed
            and self.pin_contract_changes.is_empty
            and self.pin_geometry_changes.is_empty
            and self.graphics_changes.is_empty
        )

    def summary(self) -> dict[str, int]:
        return {
            "pin_contract_pins": len(self.pin_contract_changes.pins),
            "port_order_changed": int(
                self.pin_contract_changes.port_order_change is not None
            ),
            "property_changes": len(self.pin_contract_changes.property_changes),
            "pin_geometry_moves": len(self.pin_geometry_changes.moves),
            "shapes_added": len(self.graphics_changes.shapes_added),
            "shapes_removed": len(self.graphics_changes.shapes_removed),
            "labels_added": len(self.graphics_changes.labels_added),
            "labels_removed": len(self.graphics_changes.labels_removed),
            "nlp_labels_added": len(self.graphics_changes.nlp_labels_added),
            "nlp_labels_removed": len(self.graphics_changes.nlp_labels_removed),
            "il_labels_added": len(self.graphics_changes.il_labels_added),
            "il_labels_removed": len(self.graphics_changes.il_labels_removed),
            "selection_box_changed": int(
                self.graphics_changes.selection_box_change is not None
            ),
            "origin_changed": int(self.graphics_changes.origin_change is not None),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_cellview": self.old_cellview.to_dict(),
            "new_cellview": self.new_cellview.to_dict(),
            "cellview_changed": self.cellview_changed,
            "pin_contract_changes": self.pin_contract_changes.to_dict(),
            "pin_geometry_changes": self.pin_geometry_changes.to_dict(),
            "graphics_changes": self.graphics_changes.to_dict(),
        }


# ---------------------------------------------------------------------------
# Diff Computation & Text Formatter
# ---------------------------------------------------------------------------


def _norm_zero(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _pin_figure_contract_key(fig: SymbolPinFigure) -> tuple[Any, ...]:
    """Extract translation-invariant pin figure contract properties."""

    rel_dims: tuple[float, float] | None = None
    if fig.bbox is not None:
        dx = _norm_zero(round(fig.bbox[1][0] - fig.bbox[0][0], 6))
        dy = _norm_zero(round(fig.bbox[1][1] - fig.bbox[0][1], 6))
        rel_dims = (dx, dy)
    rel_points: tuple[Point2D, ...] = ()
    if fig.points:
        min_x = min(p[0] for p in fig.points)
        min_y = min(p[1] for p in fig.points)
        rel_points = tuple(
            (_norm_zero(round(p[0] - min_x, 6)), _norm_zero(round(p[1] - min_y, 6)))
            for p in fig.points
        )
    return (
        fig.kind,
        fig.layer,
        fig.purpose,
        fig.lib,
        fig.cell,
        fig.view,
        rel_dims,
        rel_points,
    )


def _pin_figure_geom_key(fig: SymbolPinFigure) -> tuple[Any, ...]:
    """Extract translation/position geometry of a pin figure (bbox center & polygon anchor)."""

    center2: tuple[float, float] | None = None
    if fig.bbox is not None:
        center2 = (
            _norm_zero(round(fig.bbox[0][0] + fig.bbox[1][0], 6)),
            _norm_zero(round(fig.bbox[0][1] + fig.bbox[1][1], 6)),
        )
    poly_anchor: Point2D | None = None
    if fig.points:
        poly_anchor = (
            _norm_zero(round(min(p[0] for p in fig.points), 6)),
            _norm_zero(round(min(p[1] for p in fig.points), 6)),
        )
    return (center2, poly_anchor)


def _diff_properties(
    old_props: Mapping[str, Any], new_props: Mapping[str, Any]
) -> tuple[SymbolPropertyChange, ...]:
    changes: list[SymbolPropertyChange] = []
    for key in sorted(set(old_props) | set(new_props)):
        in_old = key in old_props
        in_new = key in new_props
        if in_old and not in_new:
            changes.append(SymbolPropertyChange(key, old_props[key], None, "removed"))
        elif in_new and not in_old:
            changes.append(SymbolPropertyChange(key, None, new_props[key], "added"))
        elif not _strict_equal(old_props[key], new_props[key]):
            changes.append(
                SymbolPropertyChange(key, old_props[key], new_props[key], "modified")
            )
    return tuple(changes)


def diff_symbols(old: SymbolSnapshot, new: SymbolSnapshot) -> SymbolDiff:
    """Compare two SymbolSnapshots across pin contract, pin geometry, and graphics domains."""

    old_pins: dict[str, SymbolPin] = {p.name: p for p in old.pins}
    new_pins: dict[str, SymbolPin] = {p.name: p for p in new.pins}

    contract_pins: list[SymbolPinContractChange] = []
    geometry_moves: list[SymbolPinGeometryChange] = []

    for name in sorted(set(old_pins) | set(new_pins)):
        op = old_pins.get(name)
        np = new_pins.get(name)
        if op is None:
            assert np is not None
            contract_pins.append(
                SymbolPinContractChange(
                    name=name,
                    change_type="added",
                    new_direction=np.direction,
                    new_pin_figure=np.pin_figure,
                )
            )
        elif np is None:
            contract_pins.append(
                SymbolPinContractChange(
                    name=name,
                    change_type="removed",
                    old_direction=op.direction,
                    old_pin_figure=op.pin_figure,
                )
            )
        else:
            if not _strict_equal(op.direction, np.direction) or (
                _pin_figure_contract_key(op.pin_figure)
                != _pin_figure_contract_key(np.pin_figure)
            ):
                contract_pins.append(
                    SymbolPinContractChange(
                        name=name,
                        change_type="modified",
                        old_direction=op.direction,
                        new_direction=np.direction,
                        old_pin_figure=op.pin_figure,
                        new_pin_figure=np.pin_figure,
                    )
                )
            if (
                not _strict_equal(op.xy, np.xy)
                or not _strict_equal(op.orient, np.orient)
                or _pin_figure_geom_key(op.pin_figure)
                != _pin_figure_geom_key(np.pin_figure)
            ):
                geometry_moves.append(
                    SymbolPinGeometryChange(
                        name=name,
                        old_xy=op.xy,
                        new_xy=np.xy,
                        old_orient=op.orient,
                        new_orient=np.orient,
                        old_pin_figure_bbox=op.pin_figure.bbox,
                        new_pin_figure_bbox=np.pin_figure.bbox,
                    )
                )

    port_order_change = (
        None
        if _strict_equal(old.port_order, new.port_order)
        else PortOrderChange(old_order=old.port_order, new_order=new.port_order)
    )
    prop_changes = _diff_properties(old.properties, new.properties)

    shapes_added, shapes_removed = _multiset_diff(
        old.graphics.shapes, new.graphics.shapes
    )
    labels_added, labels_removed = _multiset_diff(
        old.graphics.labels, new.graphics.labels
    )
    nlp_added, nlp_removed = _multiset_diff(
        old.graphics.nlp_labels, new.graphics.nlp_labels
    )
    il_added, il_removed = _multiset_diff(
        old.graphics.il_labels, new.graphics.il_labels
    )
    sb_change = (
        None
        if _strict_equal(old.graphics.selection_box, new.graphics.selection_box)
        else SymbolFieldChange(
            "selection_box", old.graphics.selection_box, new.graphics.selection_box
        )
    )
    origin_change = (
        None
        if _strict_equal(old.graphics.origin, new.graphics.origin)
        else SymbolFieldChange("origin", old.graphics.origin, new.graphics.origin)
    )

    return SymbolDiff(
        old_cellview=old.cellview,
        new_cellview=new.cellview,
        pin_contract_changes=PinContractDiff(
            pins=tuple(contract_pins),
            port_order_change=port_order_change,
            property_changes=prop_changes,
        ),
        pin_geometry_changes=PinGeometryDiff(moves=tuple(geometry_moves)),
        graphics_changes=GraphicsDiff(
            shapes_added=shapes_added,
            shapes_removed=shapes_removed,
            labels_added=labels_added,
            labels_removed=labels_removed,
            nlp_labels_added=nlp_added,
            nlp_labels_removed=nlp_removed,
            il_labels_added=il_added,
            il_labels_removed=il_removed,
            selection_box_change=sb_change,
            origin_change=origin_change,
        ),
    )


def _fmt_coord(value: float) -> str:
    return f"{_norm_zero(value):g}"


def _fmt_xy(xy: Point2D | None) -> str:
    if xy is None:
        return "none"
    return f"({_fmt_coord(xy[0])}, {_fmt_coord(xy[1])})"


def _fmt_bbox(bbox: BBox | None) -> str:
    if bbox is None:
        return "none"
    return f"[{_fmt_xy(bbox[0])} .. {_fmt_xy(bbox[1])}]"


def _fmt_pin_figure(fig: SymbolPinFigure | None) -> str:
    if fig is None:
        return "none"
    if fig.kind == "inst":
        return f"inst({fig.lib}/{fig.cell}/{fig.view})"
    parts = [f"{fig.kind} {fig.layer}:{fig.purpose}"]
    if fig.bbox is not None:
        parts.append(_fmt_bbox(fig.bbox))
    if fig.points:
        parts.append("[" + ", ".join(_fmt_xy(p) for p in fig.points) + "]")
    return " ".join(parts)


def _fmt_shape(shape: SymbolShape) -> str:
    parts = [f"{shape.kind} {shape.layer}:{shape.purpose}"]
    if shape.bbox is not None:
        parts.append(_fmt_bbox(shape.bbox))
    if shape.points:
        sep = " -> " if shape.kind in {"line", "path"} else ", "
        parts.append("[" + sep.join(_fmt_xy(p) for p in shape.points) + "]")
    if shape.arc_bbox is not None:
        parts.append(f"arc={_fmt_bbox(shape.arc_bbox)}")
    if shape.ellipse_bbox is not None:
        parts.append(f"ellipse={_fmt_bbox(shape.ellipse_bbox)}")
    if shape.width:
        parts.append(f"w={_fmt_coord(shape.width)}")
    if shape.fill:
        parts.append("filled")
    return " ".join(parts)


def _fmt_label(label: SymbolLabel) -> str:
    return f"{label.text!r} at {_fmt_xy(label.xy)} ({label.layer}:{label.purpose}, {label.orient})"


def _fmt_prop_val(value: Any) -> str:
    return repr(_to_plain(value))


def format_symbol_diff_text(
    diff: SymbolDiff, *, contract_only: bool = False
) -> str:
    """Render a deterministic human-scannable text summary of a SymbolDiff."""

    lines: list[str] = [
        f"Symbol Diff: {diff.old_cellview.key} -> {diff.new_cellview.key}"
    ]
    if contract_only and not diff.has_pin_contract_changes and not diff.cellview_changed:
        lines.append("No pin contract changes.")
        return "\n".join(lines)
    if not contract_only and diff.is_empty:
        lines.append("No differences.")
        return "\n".join(lines)

    if diff.has_pin_contract_changes:
        lines.append("Pin Contract Changes:")
        for pin in diff.pin_contract_changes.pins:
            if pin.change_type == "added":
                lines.append(
                    f"  + pin {pin.name} ({pin.new_direction}) [{_fmt_pin_figure(pin.new_pin_figure)}]"
                )
            elif pin.change_type == "removed":
                lines.append(
                    f"  - pin {pin.name} ({pin.old_direction}) [{_fmt_pin_figure(pin.old_pin_figure)}]"
                )
            else:
                parts: list[str] = []
                if pin.old_direction != pin.new_direction:
                    parts.append(f"direction {pin.old_direction!r} -> {pin.new_direction!r}")
                if (
                    pin.old_pin_figure is not None
                    and pin.new_pin_figure is not None
                    and _pin_figure_contract_key(pin.old_pin_figure)
                    != _pin_figure_contract_key(pin.new_pin_figure)
                ) or (pin.old_pin_figure is None) != (pin.new_pin_figure is None):
                    parts.append(
                        f"pin_figure {_fmt_pin_figure(pin.old_pin_figure)} -> {_fmt_pin_figure(pin.new_pin_figure)}"
                    )
                lines.append(f"  ~ pin {pin.name}: " + ", ".join(parts))
        if diff.pin_contract_changes.port_order_change is not None:
            poc = diff.pin_contract_changes.port_order_change
            lines.append(
                f"  ~ port_order: {list(poc.old_order)!r} -> {list(poc.new_order)!r}"
            )
        for prop in diff.pin_contract_changes.property_changes:
            if prop.change_type == "added":
                lines.append(f"  + property {prop.key} = {_fmt_prop_val(prop.new)}")
            elif prop.change_type == "removed":
                lines.append(f"  - property {prop.key} = {_fmt_prop_val(prop.old)}")
            else:
                lines.append(
                    f"  ~ property {prop.key}: {_fmt_prop_val(prop.old)} -> {_fmt_prop_val(prop.new)}"
                )
    else:
        lines.append("Pin Contract Changes: none")

    if contract_only:
        return "\n".join(lines)

    if diff.has_pin_geometry_changes:
        lines.append("Pin Geometry Changes:")
        for mv in diff.pin_geometry_changes.moves:
            parts = []
            if mv.old_xy != mv.new_xy:
                parts.append(f"xy {_fmt_xy(mv.old_xy)} -> {_fmt_xy(mv.new_xy)}")
            if mv.old_orient != mv.new_orient:
                parts.append(f"orient {mv.old_orient!r} -> {mv.new_orient!r}")
            if mv.old_pin_figure_bbox != mv.new_pin_figure_bbox:
                parts.append(
                    f"bbox {_fmt_bbox(mv.old_pin_figure_bbox)} -> {_fmt_bbox(mv.new_pin_figure_bbox)}"
                )
            lines.append(f"  ~ pin {mv.name}: " + ", ".join(parts))

    if diff.has_graphics_changes:
        gc = diff.graphics_changes
        lines.append("Graphics Changes:")
        if gc.selection_box_change is not None:
            lines.append(
                f"  ~ selection_box: {_fmt_bbox(gc.selection_box_change.old)} -> "
                f"{_fmt_bbox(gc.selection_box_change.new)}"
            )
        if gc.origin_change is not None:
            lines.append(
                f"  ~ origin: {_fmt_xy(gc.origin_change.old)} -> "
                f"{_fmt_xy(gc.origin_change.new)}"
            )
        for shape in gc.shapes_added:
            lines.append(f"  + shape {_fmt_shape(shape)}")
        for shape in gc.shapes_removed:
            lines.append(f"  - shape {_fmt_shape(shape)}")
        for lbl in gc.labels_added:
            lines.append(f"  + label {_fmt_label(lbl)}")
        for lbl in gc.labels_removed:
            lines.append(f"  - label {_fmt_label(lbl)}")
        for lbl in gc.nlp_labels_added:
            lines.append(f"  + nlp_label {_fmt_label(lbl)}")
        for lbl in gc.nlp_labels_removed:
            lines.append(f"  - nlp_label {_fmt_label(lbl)}")
        for lbl in gc.il_labels_added:
            lines.append(f"  + il_label {_fmt_label(lbl)}")
        for lbl in gc.il_labels_removed:
            lines.append(f"  - il_label {_fmt_label(lbl)}")

    return "\n".join(lines)


__all__ = [
    "SymbolPinContractChange",
    "PortOrderChange",
    "SymbolPropertyChange",
    "SymbolPinGeometryChange",
    "SymbolFieldChange",
    "PinContractDiff",
    "PinGeometryDiff",
    "GraphicsDiff",
    "SymbolDiff",
    "diff_symbols",
    "format_symbol_diff_text",
]
