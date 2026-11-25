"""Deterministic 3-tier semantic diff engine for OpenAccess layout snapshots.

Changes are classified into three strictly separated tiers so that physical
routing or annotation edits can never masquerade as connectivity/device edits:

1. **Tier 1 (``connectivity``)**: nets, pin additions/removals and connectivity
   edits (``net``, ``direction``, ``layer``, ``purpose``, ``access_dir``,
   ``props``), instances added/removed, master changes, PCell parameter/flag
   changes, instance terminal connectivity, mosaic device/array changes,
   matched via connectivity changes (``net``, ``via_def``, ``cutRows``,
   ``cutColumns``), instance/mosaic properties, and cellview properties.
2. **Tier 2 (``geometry``)**: pure placement moves (existing instance/mosaic
   ``xy``/``orient`` and pure physical pin geometry/bbox moves), unmatched
   vias, physical mask shapes, blockages, boundaries, figure groups, and
   per-``layer:purpose`` delta counts.
3. **Tier 3 (``annotation``)**: text labels and non-netted shapes on auxiliary
   layers or purposes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, TypeVar, Union

from .layout_model import (
    AUXILIARY_LAYERS,
    AUXILIARY_PURPOSES,
    CellViewRef,
    LayoutBlockage,
    LayoutBoundary,
    LayoutEllipse,
    LayoutFigGroup,
    LayoutInstance,
    LayoutLabel,
    LayoutLine,
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
    ParamValue,
    Point2D,
    is_stripped_metadata_key,
)
from .layout_snapshot import canonicalize_layout_objects, quantize_layout_coord

__all__ = [
    "LayoutShape",
    "LayoutNetChange",
    "LayoutPinChange",
    "LayoutMasterChange",
    "LayoutPcellParamChange",
    "LayoutInstConnectivityChange",
    "LayoutMosaicChange",
    "LayoutViaConnectivityChange",
    "LayoutKeyValueChange",
    "LayoutInstanceMove",
    "LayoutBoundaryChange",
    "LayoutFigGroupChange",
    "LayoutConnectivityDiff",
    "LayoutGeometryDiff",
    "LayoutAnnotationDiff",
    "LayoutDiff",
    "is_layout_shape",
    "is_auxiliary_shape",
    "diff_layouts",
    "format_layout_diff_stat",
    "format_layout_diff_text",
]

LayoutShape = Union[
    LayoutRect,
    LayoutPathSeg,
    LayoutPath,
    LayoutPolygon,
    LayoutEllipse,
    LayoutLine,
]

_SHAPE_TYPES = (
    LayoutRect,
    LayoutPathSeg,
    LayoutPath,
    LayoutPolygon,
    LayoutEllipse,
    LayoutLine,
)

_T = TypeVar("_T")


def is_layout_shape(obj: LayoutObject) -> bool:
    """Return True if *obj* is one of the six OA layout shape primitives."""

    return isinstance(obj, _SHAPE_TYPES)


def is_auxiliary_shape(obj: LayoutObject) -> bool:
    """Return True if *obj* is a non-netted shape on an auxiliary layer/purpose."""

    if not isinstance(obj, _SHAPE_TYPES):
        return False
    return obj.net is None and (
        obj.layer in AUXILIARY_LAYERS or obj.purpose in AUXILIARY_PURPOSES
    )


# ---------------------------------------------------------------------------
# Change Record Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutNetChange:
    name: str
    change_type: Literal["added", "removed", "modified"]
    old_sig_type: str | None = None
    new_sig_type: str | None = None
    old_is_global: bool | None = None
    new_is_global: bool | None = None
    old_num_bits: int | None = None
    new_num_bits: int | None = None
    old_props: dict[str, ParamValue] | None = None
    new_props: dict[str, ParamValue] | None = None


@dataclass(frozen=True)
class LayoutPinChange:
    key: str
    name: str
    pin_name: str
    change_type: Literal["added", "removed", "modified"]
    old: LayoutPin | None = None
    new: LayoutPin | None = None
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutMasterChange:
    instance: str
    old_master: tuple[str, str, str]
    new_master: tuple[str, str, str]


@dataclass(frozen=True)
class LayoutPcellParamChange:
    instance: str
    param: str
    old: Any
    new: Any
    old_type: str | None = None
    new_type: str | None = None
    target_kind: Literal["instance", "mosaic"] = "instance"


@dataclass(frozen=True)
class LayoutInstConnectivityChange:
    instance: str
    terminal: str
    old_net: str | None
    new_net: str | None


@dataclass(frozen=True)
class LayoutMosaicChange:
    name: str
    change_type: Literal["added", "removed", "modified"]
    old_master: tuple[str, str, str] | None = None
    new_master: tuple[str, str, str] | None = None
    old_rows: int | None = None
    new_rows: int | None = None
    old_columns: int | None = None
    new_columns: int | None = None
    old_u_x: float | None = None
    new_u_x: float | None = None
    old_u_y: float | None = None
    new_u_y: float | None = None
    old_is_pcell: bool | None = None
    new_is_pcell: bool | None = None
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutViaConnectivityChange:
    origin: Point2D
    orient: str
    old_via_def: str
    new_via_def: str
    old_net: str | None
    new_net: str | None
    old_cut_rows: Any = None
    new_cut_rows: Any = None
    old_cut_columns: Any = None
    new_cut_columns: Any = None
    old_via: LayoutVia | None = None
    new_via: LayoutVia | None = None
    changed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class LayoutKeyValueChange:
    scope: str
    key: str
    old: Any
    new: Any
    change_type: Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class LayoutInstanceMove:
    name: str
    old_xy: Point2D
    new_xy: Point2D
    old_orient: str
    new_orient: str


@dataclass(frozen=True)
class LayoutBoundaryChange:
    key: str
    boundary_kind: str
    name: str | None
    old: LayoutBoundary
    new: LayoutBoundary


@dataclass(frozen=True)
class LayoutFigGroupChange:
    name: str
    old: LayoutFigGroup
    new: LayoutFigGroup
    changed_fields: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Three-Tier Diff Containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutConnectivityDiff:
    """Tier 1: Connectivity, net, pin, device/PCell, and property changes."""

    nets: tuple[LayoutNetChange, ...] = ()
    pins: tuple[LayoutPinChange, ...] = ()
    instances_added: tuple[str, ...] = ()
    instances_removed: tuple[str, ...] = ()
    master_changes: tuple[LayoutMasterChange, ...] = ()
    pcell_param_changes: tuple[LayoutPcellParamChange, ...] = ()
    pcell_flag_changes: tuple[LayoutKeyValueChange, ...] = ()
    inst_connectivity_changes: tuple[LayoutInstConnectivityChange, ...] = ()
    mosaic_changes: tuple[LayoutMosaicChange, ...] = ()
    via_connectivity_changes: tuple[LayoutViaConnectivityChange, ...] = ()
    instance_prop_changes: tuple[LayoutKeyValueChange, ...] = ()
    cell_property_changes: tuple[LayoutKeyValueChange, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "nets",
            "pins",
            "instances_added",
            "instances_removed",
            "master_changes",
            "pcell_param_changes",
            "pcell_flag_changes",
            "inst_connectivity_changes",
            "mosaic_changes",
            "via_connectivity_changes",
            "instance_prop_changes",
            "cell_property_changes",
        ):
            values = tuple(getattr(self, field_name))
            object.__setattr__(self, field_name, tuple(sorted(values, key=_stable_key)))

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.nets,
                self.pins,
                self.instances_added,
                self.instances_removed,
                self.master_changes,
                self.pcell_param_changes,
                self.pcell_flag_changes,
                self.inst_connectivity_changes,
                self.mosaic_changes,
                self.via_connectivity_changes,
                self.instance_prop_changes,
                self.cell_property_changes,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class LayoutGeometryDiff:
    """Tier 2: Pure placement moves, routing, mask shapes, blockages, boundaries."""

    instance_moves: tuple[LayoutInstanceMove, ...] = ()
    mosaic_moves: tuple[LayoutInstanceMove, ...] = ()
    pin_moves: tuple[LayoutPinChange, ...] = ()
    vias_added: tuple[LayoutVia, ...] = ()
    vias_removed: tuple[LayoutVia, ...] = ()
    shapes_added: tuple[LayoutShape, ...] = ()
    shapes_removed: tuple[LayoutShape, ...] = ()
    blockages_added: tuple[LayoutBlockage, ...] = ()
    blockages_removed: tuple[LayoutBlockage, ...] = ()
    boundaries_added: tuple[LayoutBoundary, ...] = ()
    boundaries_removed: tuple[LayoutBoundary, ...] = ()
    boundaries_modified: tuple[LayoutBoundaryChange, ...] = ()
    fig_groups_added: tuple[LayoutFigGroup, ...] = ()
    fig_groups_removed: tuple[LayoutFigGroup, ...] = ()
    fig_groups_modified: tuple[LayoutFigGroupChange, ...] = ()
    layer_deltas: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "instance_moves",
            "mosaic_moves",
            "pin_moves",
            "vias_added",
            "vias_removed",
            "shapes_added",
            "shapes_removed",
            "blockages_added",
            "blockages_removed",
            "boundaries_added",
            "boundaries_removed",
            "boundaries_modified",
            "fig_groups_added",
            "fig_groups_removed",
            "fig_groups_modified",
        ):
            values = tuple(getattr(self, field_name))
            object.__setattr__(self, field_name, tuple(sorted(values, key=_stable_key)))
        normalized_deltas = {
            layer_key: {
                "added": int(counts.get("added", 0)),
                "removed": int(counts.get("removed", 0)),
            }
            for layer_key, counts in sorted(self.layer_deltas.items())
        }
        object.__setattr__(self, "layer_deltas", normalized_deltas)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.instance_moves,
                self.mosaic_moves,
                self.pin_moves,
                self.vias_added,
                self.vias_removed,
                self.shapes_added,
                self.shapes_removed,
                self.blockages_added,
                self.blockages_removed,
                self.boundaries_added,
                self.boundaries_removed,
                self.boundaries_modified,
                self.fig_groups_added,
                self.fig_groups_removed,
                self.fig_groups_modified,
                self.layer_deltas,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class LayoutAnnotationDiff:
    """Tier 3: Text labels and non-netted auxiliary layer/purpose shapes."""

    labels_added: tuple[LayoutLabel, ...] = ()
    labels_removed: tuple[LayoutLabel, ...] = ()
    aux_shapes_added: tuple[LayoutShape, ...] = ()
    aux_shapes_removed: tuple[LayoutShape, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "labels_added",
            "labels_removed",
            "aux_shapes_added",
            "aux_shapes_removed",
        ):
            values = tuple(getattr(self, field_name))
            object.__setattr__(self, field_name, tuple(sorted(values, key=_stable_key)))

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.labels_added,
                self.labels_removed,
                self.aux_shapes_added,
                self.aux_shapes_removed,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class LayoutDiff:
    """Complete deterministic 3-tier semantic diff between two layout snapshots."""

    old_cellview: CellViewRef
    new_cellview: CellViewRef
    connectivity: LayoutConnectivityDiff
    geometry: LayoutGeometryDiff
    annotation: LayoutAnnotationDiff

    @property
    def cellview_changed(self) -> bool:
        return self.old_cellview != self.new_cellview

    @property
    def has_connectivity_changes(self) -> bool:
        return not self.connectivity.is_empty

    @property
    def has_geometry_changes(self) -> bool:
        return not self.geometry.is_empty

    @property
    def has_annotation_changes(self) -> bool:
        return not self.annotation.is_empty

    @property
    def has_changes(self) -> bool:
        return (
            self.cellview_changed
            or self.has_connectivity_changes
            or self.has_geometry_changes
            or self.has_annotation_changes
        )

    @property
    def is_empty(self) -> bool:
        return not self.has_changes

    def summary(self) -> dict[str, int]:
        """Return deterministic counts for every layout diff category."""

        return {
            "aux_shapes_added": len(self.annotation.aux_shapes_added),
            "aux_shapes_removed": len(self.annotation.aux_shapes_removed),
            "blockages_added": len(self.geometry.blockages_added),
            "blockages_removed": len(self.geometry.blockages_removed),
            "boundaries_added": len(self.geometry.boundaries_added),
            "boundaries_modified": len(self.geometry.boundaries_modified),
            "boundaries_removed": len(self.geometry.boundaries_removed),
            "cell_property_changes": len(self.connectivity.cell_property_changes),
            "fig_groups_added": len(self.geometry.fig_groups_added),
            "fig_groups_modified": len(self.geometry.fig_groups_modified),
            "fig_groups_removed": len(self.geometry.fig_groups_removed),
            "inst_connectivity_changes": len(self.connectivity.inst_connectivity_changes),
            "instance_moves": len(self.geometry.instance_moves),
            "instance_prop_changes": len(self.connectivity.instance_prop_changes),
            "instances_added": len(self.connectivity.instances_added),
            "instances_removed": len(self.connectivity.instances_removed),
            "labels_added": len(self.annotation.labels_added),
            "labels_removed": len(self.annotation.labels_removed),
            "master_changes": len(self.connectivity.master_changes),
            "mosaic_changes": len(self.connectivity.mosaic_changes),
            "mosaic_moves": len(self.geometry.mosaic_moves),
            "nets": len(self.connectivity.nets),
            "pcell_flag_changes": len(self.connectivity.pcell_flag_changes),
            "pcell_param_changes": len(self.connectivity.pcell_param_changes),
            "pin_moves": len(self.geometry.pin_moves),
            "pins": len(self.connectivity.pins),
            "shapes_added": len(self.geometry.shapes_added),
            "shapes_removed": len(self.geometry.shapes_removed),
            "via_connectivity_changes": len(self.connectivity.via_connectivity_changes),
            "vias_added": len(self.geometry.vias_added),
            "vias_removed": len(self.geometry.vias_removed),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotation": self.annotation.to_dict(),
            "cellview_changed": self.cellview_changed,
            "connectivity": self.connectivity.to_dict(),
            "geometry": self.geometry.to_dict(),
            "has_annotation_changes": self.has_annotation_changes,
            "has_changes": self.has_changes,
            "has_connectivity_changes": self.has_connectivity_changes,
            "has_geometry_changes": self.has_geometry_changes,
            "new_cellview": self.new_cellview.to_dict(),
            "old_cellview": self.old_cellview.to_dict(),
        }


# ---------------------------------------------------------------------------
# Strict Type-Aware Comparison & Deterministic Serialization Helpers
# ---------------------------------------------------------------------------


def _strict_equal(left: Any, right: Any) -> bool:
    """Compare values recursively without Python's bool/int/float equivalence."""

    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        if len(left) != len(right):
            return False
        unmatched = list(right.items())
        for left_key, left_value in left.items():
            for index, (right_key, right_value) in enumerate(unmatched):
                if _strict_equal(left_key, right_key):
                    if not _strict_equal(left_value, right_value):
                        return False
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, (set, frozenset)):
        if len(left) != len(right):
            return False
        return {_stable_key(value) for value in left} == {
            _stable_key(value) for value in right
        }
    if is_dataclass(left):
        return all(
            _strict_equal(getattr(left, item.name), getattr(right, item.name))
            for item in fields(left)
        )
    return left == right


def _stable_key(value: Any) -> tuple[Any, ...]:
    """Build a total, type-aware ordering key for deterministic sorting."""

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
    if isinstance(value, tuple):
        return ("tuple", tuple(_stable_key(item) for item in value))
    if isinstance(value, list):
        return ("list", tuple(_stable_key(item) for item in value))
    if isinstance(value, Mapping):
        items = tuple(
            sorted(
                (
                    (_stable_key(key), _stable_key(item))
                    for key, item in value.items()
                ),
                key=lambda pair: pair[0],
            )
        )
        return ("mapping", items)
    if isinstance(value, (set, frozenset)):
        return ("set", tuple(sorted(_stable_key(item) for item in value)))
    if is_dataclass(value):
        return (
            "dataclass",
            type(value).__module__,
            type(value).__qualname__,
            tuple(
                (item.name, _stable_key(getattr(value, item.name)))
                for item in fields(value)
            ),
        )
    return ("object", type(value).__module__, type(value).__qualname__, repr(value))


def _to_plain(value: Any) -> Any:
    """Convert dataclasses, tuples, and mappings to deterministic JSON values."""

    if is_dataclass(value):
        return {
            item.name: _to_plain(getattr(value, item.name))
            for item in sorted(fields(value), key=lambda f: f.name)
        }
    if isinstance(value, Mapping):
        return {
            key: _to_plain(value[key])
            for key in sorted(value, key=_stable_key)
        }
    if isinstance(value, (tuple, list)):
        return [_to_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_to_plain(item) for item in sorted(value, key=_stable_key)]
    return value


def _sorted_union(*mappings: Mapping[str, Any]) -> tuple[str, ...]:
    keys: set[str] = set()
    for mapping in mappings:
        keys.update(mapping)
    return tuple(sorted(keys))


def _normalize_cell_properties(props: Mapping[str, ParamValue]) -> dict[str, ParamValue]:
    out: dict[str, ParamValue] = {}
    for key in sorted(props):
        if is_stripped_metadata_key(key):
            continue
        val = props[key]
        if type(val) is float:
            val = 0.0 if val == 0.0 else float(f"{val:.12g}")
        out[key] = val
    return out


def _multiset_diff(
    old: Sequence[_T], new: Sequence[_T]
) -> tuple[tuple[_T, ...], tuple[_T, ...]]:
    """Return ``(added, removed)`` for canonicalized multisets."""

    old_groups: dict[tuple[Any, ...], list[_T]] = defaultdict(list)
    new_groups: dict[tuple[Any, ...], list[_T]] = defaultdict(list)
    for item in old:
        old_groups[_stable_key(item)].append(item)
    for item in new:
        new_groups[_stable_key(item)].append(item)

    added: list[_T] = []
    removed: list[_T] = []
    for key in sorted(set(old_groups) | set(new_groups)):
        old_items = sorted(old_groups.get(key, []), key=_stable_key)
        new_items = sorted(new_groups.get(key, []), key=_stable_key)
        common = min(len(old_items), len(new_items))
        removed.extend(old_items[common:])
        added.extend(new_items[common:])
    return tuple(added), tuple(removed)


def _diff_mapping(
    scope: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
) -> tuple[LayoutKeyValueChange, ...]:
    changes: list[LayoutKeyValueChange] = []
    for key in _sorted_union(old, new):
        old_present = key in old
        new_present = key in new
        old_val = old[key] if old_present else None
        new_val = new[key] if new_present else None
        if old_present and not new_present:
            change_type: Literal["added", "removed", "modified"] = "removed"
        elif new_present and not old_present:
            change_type = "added"
        elif _strict_equal(old_val, new_val):
            continue
        else:
            change_type = "modified"
        changes.append(LayoutKeyValueChange(scope, key, old_val, new_val, change_type))
    return tuple(changes)


def _diff_pcell_params(
    instance: str,
    old_values: Mapping[str, Any],
    new_values: Mapping[str, Any],
    old_types: Mapping[str, str],
    new_types: Mapping[str, str],
    *,
    target_kind: Literal["instance", "mosaic"],
) -> tuple[LayoutPcellParamChange, ...]:
    changes: list[LayoutPcellParamChange] = []
    for name in _sorted_union(old_values, new_values, old_types, new_types):
        old_present = name in old_values
        new_present = name in new_values
        old_val = old_values[name] if old_present else None
        new_val = new_values[name] if new_present else None
        old_type = old_types.get(name)
        new_type = new_types.get(name)
        if (
            old_present == new_present
            and _strict_equal(old_val, new_val)
            and _strict_equal(old_type, new_type)
        ):
            continue
        changes.append(
            LayoutPcellParamChange(
                instance=instance,
                param=name,
                old=old_val,
                new=new_val,
                old_type=old_type,
                new_type=new_type,
                target_kind=target_kind,
            )
        )
    return tuple(changes)


def _diff_connections(
    instance: str,
    old: Mapping[str, str],
    new: Mapping[str, str],
) -> tuple[LayoutInstConnectivityChange, ...]:
    changes: list[LayoutInstConnectivityChange] = []
    for terminal in _sorted_union(old, new):
        old_present = terminal in old
        new_present = terminal in new
        old_net = old[terminal] if old_present else None
        new_net = new[terminal] if new_present else None
        if old_present == new_present and _strict_equal(old_net, new_net):
            continue
        changes.append(LayoutInstConnectivityChange(instance, terminal, old_net, new_net))
    return tuple(changes)


# ---------------------------------------------------------------------------
# Entity-Specific Differs
# ---------------------------------------------------------------------------


def _diff_nets(
    old_nets: Sequence[LayoutNet], new_nets: Sequence[LayoutNet]
) -> tuple[LayoutNetChange, ...]:
    old_by_name = {n.name: n for n in old_nets}
    new_by_name = {n.name: n for n in new_nets}
    changes: list[LayoutNetChange] = []
    for name in sorted(set(old_by_name) | set(new_by_name)):
        o = old_by_name.get(name)
        n = new_by_name.get(name)
        if o is None and n is not None:
            changes.append(
                LayoutNetChange(
                    name=name,
                    change_type="added",
                    new_sig_type=n.sig_type,
                    new_is_global=n.is_global,
                    new_num_bits=n.num_bits,
                    new_props=dict(n.props),
                )
            )
        elif n is None and o is not None:
            changes.append(
                LayoutNetChange(
                    name=name,
                    change_type="removed",
                    old_sig_type=o.sig_type,
                    old_is_global=o.is_global,
                    old_num_bits=o.num_bits,
                    old_props=dict(o.props),
                )
            )
        elif o is not None and n is not None and (
            not _strict_equal(o.sig_type, n.sig_type)
            or not _strict_equal(o.is_global, n.is_global)
            or not _strict_equal(o.num_bits, n.num_bits)
            or not _strict_equal(o.props, n.props)
        ):
            changes.append(
                LayoutNetChange(
                    name=name,
                    change_type="modified",
                    old_sig_type=o.sig_type,
                    new_sig_type=n.sig_type,
                    old_is_global=o.is_global,
                    new_is_global=n.is_global,
                    old_num_bits=o.num_bits,
                    new_num_bits=n.num_bits,
                    old_props=dict(o.props),
                    new_props=dict(n.props),
                )
            )
    return tuple(changes)


def _pair_occurrences(
    old_items: Sequence[_T],
    new_items: Sequence[_T],
    *,
    soft_key: Any = None,
) -> tuple[tuple[_T | None, _T | None], ...]:
    """Pair duplicate-keyed items: exact matches first, optional soft-key next, then order."""

    old_sorted = sorted(old_items, key=_stable_key)
    new_remaining = sorted(new_items, key=_stable_key)
    pairs: list[tuple[_T | None, _T | None]] = []

    # Pass 1: exact identity match.
    for old_item in old_sorted:
        exact_idx = next(
            (idx for idx, cand in enumerate(new_remaining) if _strict_equal(old_item, cand)),
            None,
        )
        if exact_idx is None:
            pairs.append((old_item, None))
        else:
            pairs.append((old_item, new_remaining.pop(exact_idx)))

    # Pass 2: optional soft-key match (e.g., same net/layer/purpose).
    if soft_key is not None and new_remaining:
        for idx, (old_item, new_item) in enumerate(pairs):
            if new_item is None and old_item is not None:
                target = soft_key(old_item)
                match_idx = next(
                    (
                        n_idx
                        for n_idx, cand in enumerate(new_remaining)
                        if soft_key(cand) == target
                    ),
                    None,
                )
                if match_idx is not None:
                    pairs[idx] = (old_item, new_remaining.pop(match_idx))

    # Pass 3: deterministic positional pairing for remaining items.
    for idx, (old_item, new_item) in enumerate(pairs):
        if new_item is None and new_remaining:
            pairs[idx] = (old_item, new_remaining.pop(0))

    pairs.extend((None, cand) for cand in new_remaining)
    return tuple(pairs)


_PIN_CONNECTIVITY_FIELDS = frozenset(
    {"access_dir", "direction", "layer", "net", "props", "purpose"}
)


def _pin_changed_fields(old: LayoutPin, new: LayoutPin) -> tuple[str, ...]:
    changed: list[str] = []
    for attr in (
        "access_dir",
        "bbox",
        "begin_ext",
        "begin_pt",
        "begin_style",
        "custom_style",
        "direction",
        "end_ext",
        "end_pt",
        "end_style",
        "layer",
        "net",
        "path_style",
        "points",
        "props",
        "purpose",
        "shape_kind",
        "width",
    ):
        if not _strict_equal(getattr(old, attr), getattr(new, attr)):
            changed.append(attr)
    return tuple(changed)


def _diff_pins(
    old_pins: Sequence[LayoutPin], new_pins: Sequence[LayoutPin]
) -> tuple[tuple[LayoutPinChange, ...], tuple[LayoutPinChange, ...]]:
    """Diff pins into Tier 1 connectivity changes vs Tier 2 pure physical geometry moves."""

    old_groups: dict[tuple[str, str], list[LayoutPin]] = defaultdict(list)
    new_groups: dict[tuple[str, str], list[LayoutPin]] = defaultdict(list)
    for pin in old_pins:
        old_groups[(pin.name, pin.pin_name)].append(pin)
    for pin in new_pins:
        new_groups[(pin.name, pin.pin_name)].append(pin)

    conn_changes: list[LayoutPinChange] = []
    geom_moves: list[LayoutPinChange] = []
    for name, pin_name in sorted(set(old_groups) | set(new_groups)):
        pairs = _pair_occurrences(
            old_groups.get((name, pin_name), ()),
            new_groups.get((name, pin_name), ()),
            soft_key=lambda p: (p.net, p.layer, p.purpose),
        )
        base_key = f"{name}:{pin_name}" if pin_name else name
        multiple = len(pairs) > 1
        for idx, (old_pin, new_pin) in enumerate(pairs):
            key = f"{base_key}[{idx}]" if multiple else base_key
            if old_pin is None and new_pin is not None:
                conn_changes.append(
                    LayoutPinChange(
                        key=key,
                        name=name,
                        pin_name=pin_name,
                        change_type="added",
                        new=new_pin,
                    )
                )
            elif new_pin is None and old_pin is not None:
                conn_changes.append(
                    LayoutPinChange(
                        key=key,
                        name=name,
                        pin_name=pin_name,
                        change_type="removed",
                        old=old_pin,
                    )
                )
            elif old_pin is not None and new_pin is not None and not _strict_equal(old_pin, new_pin):
                changed = _pin_changed_fields(old_pin, new_pin)
                pin_change = LayoutPinChange(
                    key=key,
                    name=name,
                    pin_name=pin_name,
                    change_type="modified",
                    old=old_pin,
                    new=new_pin,
                    changed_fields=changed,
                )
                if not changed or any(f in _PIN_CONNECTIVITY_FIELDS for f in changed):
                    conn_changes.append(pin_change)
                else:
                    geom_moves.append(pin_change)
    return tuple(conn_changes), tuple(geom_moves)


def _diff_vias(
    old_vias: Sequence[LayoutVia], new_vias: Sequence[LayoutVia]
) -> tuple[tuple[LayoutViaConnectivityChange, ...], tuple[LayoutVia, ...], tuple[LayoutVia, ...]]:
    """Diff vias, promoting matched ``(origin, orient)`` connectivity edits to Tier 1."""

    # Cancel exact identical vias first.
    rem_new, rem_old = _multiset_diff(old_vias, new_vias)

    old_by_loc: dict[tuple[Point2D, str], list[LayoutVia]] = defaultdict(list)
    new_by_loc: dict[tuple[Point2D, str], list[LayoutVia]] = defaultdict(list)
    for via in rem_old:
        old_by_loc[(via.origin, via.orient)].append(via)
    for via in rem_new:
        new_by_loc[(via.origin, via.orient)].append(via)

    conn_changes: list[LayoutViaConnectivityChange] = []
    vias_added: list[LayoutVia] = []
    vias_removed: list[LayoutVia] = []

    for loc in sorted(set(old_by_loc) | set(new_by_loc)):
        origin, orient = loc
        old_list = sorted(old_by_loc.get(loc, []), key=_stable_key)
        new_list = sorted(new_by_loc.get(loc, []), key=_stable_key)

        if not old_list:
            vias_added.extend(new_list)
            continue
        if not new_list:
            vias_removed.extend(old_list)
            continue

        # Multi-pass deterministic pairing at the same (origin, orient):
        # 1) matching non-None name, 2) matching (via_def, net), 3) matching via_def,
        # 4) matching net, 5) positional order.
        paired: list[tuple[LayoutVia, LayoutVia]] = []
        o_rem = list(old_list)
        n_rem = list(new_list)

        for matcher in (
            lambda a, b: a.name is not None and a.name == b.name,
            lambda a, b: a.via_def == b.via_def and _strict_equal(a.net, b.net),
            lambda a, b: a.via_def == b.via_def,
            lambda a, b: a.net is not None and _strict_equal(a.net, b.net),
            lambda _a, _b: True,
        ):
            next_o: list[LayoutVia] = []
            for o_via in o_rem:
                match_idx = next(
                    (idx for idx, n_via in enumerate(n_rem) if matcher(o_via, n_via)),
                    None,
                )
                if match_idx is not None:
                    paired.append((o_via, n_rem.pop(match_idx)))
                else:
                    next_o.append(o_via)
            o_rem = next_o

        vias_removed.extend(o_rem)
        vias_added.extend(n_rem)

        for o_via, n_via in paired:
            o_rows = o_via.params.get("cutRows")
            n_rows = n_via.params.get("cutRows")
            o_cols = o_via.params.get("cutColumns")
            n_cols = n_via.params.get("cutColumns")

            changed: list[str] = []
            if not _strict_equal(o_via.via_def, n_via.via_def):
                changed.append("via_def")
            if not _strict_equal(o_via.net, n_via.net):
                changed.append("net")
            if not _strict_equal(o_rows, n_rows):
                changed.append("cutRows")
            if not _strict_equal(o_cols, n_cols):
                changed.append("cutColumns")

            if changed:
                conn_changes.append(
                    LayoutViaConnectivityChange(
                        origin=origin,
                        orient=orient,
                        old_via_def=o_via.via_def,
                        new_via_def=n_via.via_def,
                        old_net=o_via.net,
                        new_net=n_via.net,
                        old_cut_rows=o_rows,
                        new_cut_rows=n_rows,
                        old_cut_columns=o_cols,
                        new_cut_columns=n_cols,
                        old_via=o_via,
                        new_via=n_via,
                        changed_fields=tuple(sorted(changed)),
                    )
                )
            else:
                # Non-connectivity via geometry edit (e.g., enclosure/spacing/props).
                vias_removed.append(o_via)
                vias_added.append(n_via)

    return tuple(conn_changes), tuple(vias_added), tuple(vias_removed)


def _via_int_param(via: LayoutVia, key: str) -> int | None:
    val = via.params.get(key)
    return val if type(val) is int else None


def _diff_boundaries(
    old_bounds: Sequence[LayoutBoundary], new_bounds: Sequence[LayoutBoundary]
) -> tuple[tuple[LayoutBoundary, ...], tuple[LayoutBoundary, ...], tuple[LayoutBoundaryChange, ...]]:
    added: list[LayoutBoundary] = []
    removed: list[LayoutBoundary] = []
    modified: list[LayoutBoundaryChange] = []

    # 1) Singleton prBoundary
    old_pr = next((b for b in old_bounds if b.boundary_kind == "prBoundary"), None)
    new_pr = next((b for b in new_bounds if b.boundary_kind == "prBoundary"), None)
    if old_pr is None and new_pr is not None:
        added.append(new_pr)
    elif new_pr is None and old_pr is not None:
        removed.append(old_pr)
    elif old_pr is not None and new_pr is not None and not _strict_equal(old_pr, new_pr):
        modified.append(
            LayoutBoundaryChange(
                key="prBoundary",
                boundary_kind="prBoundary",
                name=new_pr.name if new_pr.name is not None else old_pr.name,
                old=old_pr,
                new=new_pr,
            )
        )

    # 2) Named non-prBoundary boundaries grouped by (boundary_kind, name)
    old_named: dict[tuple[str, str], list[LayoutBoundary]] = defaultdict(list)
    new_named: dict[tuple[str, str], list[LayoutBoundary]] = defaultdict(list)
    for b in old_bounds:
        if b.boundary_kind != "prBoundary" and b.name is not None:
            old_named[(b.boundary_kind, b.name)].append(b)
    for b in new_bounds:
        if b.boundary_kind != "prBoundary" and b.name is not None:
            new_named[(b.boundary_kind, b.name)].append(b)

    for bkind, bname in sorted(set(old_named) | set(new_named)):
        pairs = _pair_occurrences(
            old_named.get((bkind, bname), ()),
            new_named.get((bkind, bname), ()),
        )
        base_key = f"{bkind}:{bname}"
        multiple = len(pairs) > 1
        for idx, (o_b, n_b) in enumerate(pairs):
            key = f"{base_key}[{idx}]" if multiple else base_key
            if o_b is None and n_b is not None:
                added.append(n_b)
            elif n_b is None and o_b is not None:
                removed.append(o_b)
            elif o_b is not None and n_b is not None and not _strict_equal(o_b, n_b):
                modified.append(
                    LayoutBoundaryChange(
                        key=key,
                        boundary_kind=bkind,
                        name=bname,
                        old=o_b,
                        new=n_b,
                    )
                )

    # 3) Unnamed non-prBoundary boundaries as multiset
    old_unnamed = [b for b in old_bounds if b.boundary_kind != "prBoundary" and b.name is None]
    new_unnamed = [b for b in new_bounds if b.boundary_kind != "prBoundary" and b.name is None]
    u_added, u_removed = _multiset_diff(old_unnamed, new_unnamed)
    added.extend(u_added)
    removed.extend(u_removed)

    return tuple(added), tuple(removed), tuple(modified)


def _diff_fig_groups(
    old_groups: Sequence[LayoutFigGroup], new_groups: Sequence[LayoutFigGroup]
) -> tuple[tuple[LayoutFigGroup, ...], tuple[LayoutFigGroup, ...], tuple[LayoutFigGroupChange, ...]]:
    old_by_name = {g.name: g for g in old_groups}
    new_by_name = {g.name: g for g in new_groups}
    added: list[LayoutFigGroup] = []
    removed: list[LayoutFigGroup] = []
    modified: list[LayoutFigGroupChange] = []

    for name in sorted(set(old_by_name) | set(new_by_name)):
        o = old_by_name.get(name)
        n = new_by_name.get(name)
        if o is None and n is not None:
            added.append(n)
        elif n is None and o is not None:
            removed.append(o)
        elif o is not None and n is not None and not _strict_equal(o, n):
            changed = tuple(
                attr
                for attr in ("group_type", "orient", "origin", "props")
                if not _strict_equal(getattr(o, attr), getattr(n, attr))
            )
            modified.append(
                LayoutFigGroupChange(name=name, old=o, new=n, changed_fields=changed)
            )
    return tuple(added), tuple(removed), tuple(modified)


def _compute_layer_deltas(
    shapes_added: Sequence[LayoutShape], shapes_removed: Sequence[LayoutShape]
) -> dict[str, dict[str, int]]:
    deltas: dict[str, dict[str, int]] = {}
    for shape in shapes_added:
        key = f"{shape.layer}:{shape.purpose}"
        entry = deltas.setdefault(key, {"added": 0, "removed": 0})
        entry["added"] += 1
    for shape in shapes_removed:
        key = f"{shape.layer}:{shape.purpose}"
        entry = deltas.setdefault(key, {"added": 0, "removed": 0})
        entry["removed"] += 1
    return {k: deltas[k] for k in sorted(deltas)}


# ---------------------------------------------------------------------------
# Public Diff Engine Entry Point
# ---------------------------------------------------------------------------


def diff_layouts(old: LayoutSnapshot, new: LayoutSnapshot) -> LayoutDiff:
    """Compare two layout snapshots across connectivity, geometry, and annotation tiers."""

    old_objects = canonicalize_layout_objects(old.objects)
    new_objects = canonicalize_layout_objects(new.objects)

    old_nets = [o for o in old_objects if isinstance(o, LayoutNet)]
    new_nets = [o for o in new_objects if isinstance(o, LayoutNet)]

    old_pins = [o for o in old_objects if isinstance(o, LayoutPin)]
    new_pins = [o for o in new_objects if isinstance(o, LayoutPin)]

    old_insts = {o.name: o for o in old_objects if isinstance(o, LayoutInstance)}
    new_insts = {o.name: o for o in new_objects if isinstance(o, LayoutInstance)}

    old_mosaics = {o.name: o for o in old_objects if isinstance(o, LayoutMosaic)}
    new_mosaics = {o.name: o for o in new_objects if isinstance(o, LayoutMosaic)}

    old_vias = [o for o in old_objects if isinstance(o, LayoutVia)]
    new_vias = [o for o in new_objects if isinstance(o, LayoutVia)]

    old_phys_shapes: list[LayoutShape] = []
    old_aux_shapes: list[LayoutShape] = []
    for o in old_objects:
        if isinstance(o, _SHAPE_TYPES):
            if is_auxiliary_shape(o):
                old_aux_shapes.append(o)
            else:
                old_phys_shapes.append(o)

    new_phys_shapes: list[LayoutShape] = []
    new_aux_shapes: list[LayoutShape] = []
    for o in new_objects:
        if isinstance(o, _SHAPE_TYPES):
            if is_auxiliary_shape(o):
                new_aux_shapes.append(o)
            else:
                new_phys_shapes.append(o)

    old_labels = [o for o in old_objects if isinstance(o, LayoutLabel)]
    new_labels = [o for o in new_objects if isinstance(o, LayoutLabel)]

    old_blockages = [o for o in old_objects if isinstance(o, LayoutBlockage)]
    new_blockages = [o for o in new_objects if isinstance(o, LayoutBlockage)]

    old_boundaries = [o for o in old_objects if isinstance(o, LayoutBoundary)]
    new_boundaries = [o for o in new_objects if isinstance(o, LayoutBoundary)]

    old_fig_groups = [o for o in old_objects if isinstance(o, LayoutFigGroup)]
    new_fig_groups = [o for o in new_objects if isinstance(o, LayoutFigGroup)]

    # Instances: Tier 1 (connectivity/master/pcell/props) vs Tier 2 (xy/orient moves)
    instances_added = tuple(sorted(set(new_insts) - set(old_insts)))
    instances_removed = tuple(sorted(set(old_insts) - set(new_insts)))

    master_changes: list[LayoutMasterChange] = []
    pcell_param_changes: list[LayoutPcellParamChange] = []
    pcell_flag_changes: list[LayoutKeyValueChange] = []
    inst_connectivity_changes: list[LayoutInstConnectivityChange] = []
    instance_prop_changes: list[LayoutKeyValueChange] = []
    instance_moves: list[LayoutInstanceMove] = []

    for name in sorted(set(old_insts) & set(new_insts)):
        o_inst = old_insts[name]
        n_inst = new_insts[name]
        o_master = (o_inst.lib, o_inst.cell, o_inst.view)
        n_master = (n_inst.lib, n_inst.cell, n_inst.view)
        if not _strict_equal(o_master, n_master):
            master_changes.append(LayoutMasterChange(name, o_master, n_master))
        if not _strict_equal(o_inst.is_pcell, n_inst.is_pcell):
            pcell_flag_changes.append(
                LayoutKeyValueChange(
                    scope=name,
                    key="is_pcell",
                    old=o_inst.is_pcell,
                    new=n_inst.is_pcell,
                    change_type="modified",
                )
            )
        pcell_param_changes.extend(
            _diff_pcell_params(
                name,
                o_inst.pcell_params,
                n_inst.pcell_params,
                o_inst.pcell_param_types,
                n_inst.pcell_param_types,
                target_kind="instance",
            )
        )
        inst_connectivity_changes.extend(
            _diff_connections(name, o_inst.connections, n_inst.connections)
        )
        instance_prop_changes.extend(_diff_mapping(name, o_inst.props, n_inst.props))
        if not _strict_equal(o_inst.xy, n_inst.xy) or not _strict_equal(
            o_inst.orient, n_inst.orient
        ):
            instance_moves.append(
                LayoutInstanceMove(
                    name=name,
                    old_xy=o_inst.xy,
                    new_xy=n_inst.xy,
                    old_orient=o_inst.orient,
                    new_orient=n_inst.orient,
                )
            )

    # Mosaics: Tier 1 (added/removed/master/array/pcell/props) vs Tier 2 (xy/orient moves)
    mosaic_changes: list[LayoutMosaicChange] = []
    mosaic_moves: list[LayoutInstanceMove] = []
    for name in sorted(set(old_mosaics) | set(new_mosaics)):
        o_m = old_mosaics.get(name)
        n_m = new_mosaics.get(name)
        if o_m is None and n_m is not None:
            mosaic_changes.append(
                LayoutMosaicChange(
                    name=name,
                    change_type="added",
                    new_master=(n_m.lib, n_m.cell, n_m.view),
                    new_rows=n_m.rows,
                    new_columns=n_m.columns,
                    new_u_x=n_m.u_x,
                    new_u_y=n_m.u_y,
                    new_is_pcell=n_m.is_pcell,
                )
            )
        elif n_m is None and o_m is not None:
            mosaic_changes.append(
                LayoutMosaicChange(
                    name=name,
                    change_type="removed",
                    old_master=(o_m.lib, o_m.cell, o_m.view),
                    old_rows=o_m.rows,
                    old_columns=o_m.columns,
                    old_u_x=o_m.u_x,
                    old_u_y=o_m.u_y,
                    old_is_pcell=o_m.is_pcell,
                )
            )
        elif o_m is not None and n_m is not None:
            o_master = (o_m.lib, o_m.cell, o_m.view)
            n_master = (n_m.lib, n_m.cell, n_m.view)
            m_pcell_diffs = _diff_pcell_params(
                name,
                o_m.pcell_params,
                n_m.pcell_params,
                o_m.pcell_param_types,
                n_m.pcell_param_types,
                target_kind="mosaic",
            )
            pcell_param_changes.extend(m_pcell_diffs)
            instance_prop_changes.extend(_diff_mapping(name, o_m.props, n_m.props))

            changed_m_fields: list[str] = []
            if not _strict_equal(o_master, n_master):
                changed_m_fields.append("master")
            if not _strict_equal(o_m.rows, n_m.rows):
                changed_m_fields.append("rows")
            if not _strict_equal(o_m.columns, n_m.columns):
                changed_m_fields.append("columns")
            if not _strict_equal(o_m.u_x, n_m.u_x):
                changed_m_fields.append("u_x")
            if not _strict_equal(o_m.u_y, n_m.u_y):
                changed_m_fields.append("u_y")
            if not _strict_equal(o_m.is_pcell, n_m.is_pcell):
                changed_m_fields.append("is_pcell")
            if m_pcell_diffs:
                changed_m_fields.append("pcell_params")

            if changed_m_fields:
                mosaic_changes.append(
                    LayoutMosaicChange(
                        name=name,
                        change_type="modified",
                        old_master=o_master,
                        new_master=n_master,
                        old_rows=o_m.rows,
                        new_rows=n_m.rows,
                        old_columns=o_m.columns,
                        new_columns=n_m.columns,
                        old_u_x=o_m.u_x,
                        new_u_x=n_m.u_x,
                        old_u_y=o_m.u_y,
                        new_u_y=n_m.u_y,
                        old_is_pcell=o_m.is_pcell,
                        new_is_pcell=n_m.is_pcell,
                        changed_fields=tuple(sorted(changed_m_fields)),
                    )
                )

            if not _strict_equal(o_m.xy, n_m.xy) or not _strict_equal(o_m.orient, n_m.orient):
                mosaic_moves.append(
                    LayoutInstanceMove(
                        name=name,
                        old_xy=o_m.xy,
                        new_xy=n_m.xy,
                        old_orient=o_m.orient,
                        new_orient=n_m.orient,
                    )
                )

    # Vias (Tier 1 connectivity changes vs Tier 2 unmatched vias)
    via_conn_changes, vias_added, vias_removed = _diff_vias(old_vias, new_vias)

    # Pins (Tier 1 connectivity/contract changes vs Tier 2 pure geometry moves)
    pin_conn_changes, pin_moves = _diff_pins(old_pins, new_pins)

    # Physical shapes & layer deltas (Tier 2)
    shapes_added, shapes_removed = _multiset_diff(old_phys_shapes, new_phys_shapes)
    layer_deltas = _compute_layer_deltas(shapes_added, shapes_removed)

    # Blockages, boundaries, fig_groups (Tier 2)
    blockages_added, blockages_removed = _multiset_diff(old_blockages, new_blockages)
    boundaries_added, boundaries_removed, boundaries_modified = _diff_boundaries(
        old_boundaries, new_boundaries
    )
    fig_groups_added, fig_groups_removed, fig_groups_modified = _diff_fig_groups(
        old_fig_groups, new_fig_groups
    )

    # Labels & auxiliary shapes (Tier 3)
    labels_added, labels_removed = _multiset_diff(old_labels, new_labels)
    aux_shapes_added, aux_shapes_removed = _multiset_diff(old_aux_shapes, new_aux_shapes)

    # Cell properties (Tier 1)
    cell_prop_changes = _diff_mapping(
        "cellview",
        _normalize_cell_properties(old.cell_properties),
        _normalize_cell_properties(new.cell_properties),
    )

    connectivity = LayoutConnectivityDiff(
        nets=_diff_nets(old_nets, new_nets),
        pins=pin_conn_changes,
        instances_added=instances_added,
        instances_removed=instances_removed,
        master_changes=tuple(master_changes),
        pcell_param_changes=tuple(pcell_param_changes),
        pcell_flag_changes=tuple(pcell_flag_changes),
        inst_connectivity_changes=tuple(inst_connectivity_changes),
        mosaic_changes=tuple(mosaic_changes),
        via_connectivity_changes=via_conn_changes,
        instance_prop_changes=tuple(instance_prop_changes),
        cell_property_changes=cell_prop_changes,
    )
    geometry = LayoutGeometryDiff(
        instance_moves=tuple(instance_moves),
        mosaic_moves=tuple(mosaic_moves),
        pin_moves=pin_moves,
        vias_added=vias_added,
        vias_removed=vias_removed,
        shapes_added=shapes_added,
        shapes_removed=shapes_removed,
        blockages_added=blockages_added,
        blockages_removed=blockages_removed,
        boundaries_added=boundaries_added,
        boundaries_removed=boundaries_removed,
        boundaries_modified=boundaries_modified,
        fig_groups_added=fig_groups_added,
        fig_groups_removed=fig_groups_removed,
        fig_groups_modified=fig_groups_modified,
        layer_deltas=layer_deltas,
    )
    annotation = LayoutAnnotationDiff(
        labels_added=labels_added,
        labels_removed=labels_removed,
        aux_shapes_added=aux_shapes_added,
        aux_shapes_removed=aux_shapes_removed,
    )

    return LayoutDiff(
        old_cellview=old.manifest.cellview,
        new_cellview=new.manifest.cellview,
        connectivity=connectivity,
        geometry=geometry,
        annotation=annotation,
    )


# ---------------------------------------------------------------------------
# Human-Readable Deterministic Text Formatter
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    if isinstance(value, LayoutPin):
        parts = [
            f"net={value.net}",
            f"dir={value.direction}",
            f"{value.layer}:{value.purpose}",
            value.shape_kind,
        ]
        if value.shape_kind in ("rect", "ellipse") and value.bbox is not None:
            parts.append(f"bbox=[{list(value.bbox[0])}, {list(value.bbox[1])}]")
        elif value.shape_kind == "path_seg" and value.begin_pt is not None and value.end_pt is not None:
            parts.append(f"{list(value.begin_pt)}->{list(value.end_pt)} w={value.width}")
            if value.custom_style:
                parts.append(f"custom_style={list(value.custom_style)}")
        elif value.points:
            parts.append(f"pts={len(value.points)}")
        if value.access_dir:
            parts.append(f"access={list(value.access_dir)}")
        if value.props:
            parts.append(f"props={dict(sorted(value.props.items()))!r}")
        return "(" + ", ".join(parts) + ")"
    if isinstance(value, LayoutRect):
        net_s = f" net={value.net}" if value.net else ""
        props_s = f" props={dict(sorted(value.props.items()))!r}" if value.props else ""
        return (
            f"{value.layer}:{value.purpose} rect "
            f"[{list(value.bbox[0])}, {list(value.bbox[1])}]{net_s}{props_s}"
        )
    if isinstance(value, LayoutPathSeg):
        net_s = f" net={value.net}" if value.net else ""
        style_s = (
            f" style={value.begin_style}/{value.end_style}"
            if value.begin_style != "truncate" or value.end_style != "truncate"
            else ""
        )
        cs_s = f" custom_style={list(value.custom_style)}" if value.custom_style else ""
        return (
            f"{value.layer}:{value.purpose} path_seg "
            f"{list(value.begin_pt)}->{list(value.end_pt)} w={value.width}"
            f"{style_s}{cs_s}{net_s}"
        )
    if isinstance(value, LayoutPath):
        net_s = f" net={value.net}" if value.net else ""
        return (
            f"{value.layer}:{value.purpose} path "
            f"pts={len(value.points)} w={value.width} style={value.path_style}{net_s}"
        )
    if isinstance(value, LayoutPolygon):
        net_s = f" net={value.net}" if value.net else ""
        return f"{value.layer}:{value.purpose} polygon pts={len(value.points)}{net_s}"
    if isinstance(value, LayoutEllipse):
        net_s = f" net={value.net}" if value.net else ""
        return (
            f"{value.layer}:{value.purpose} ellipse "
            f"[{list(value.bbox[0])}, {list(value.bbox[1])}]{net_s}"
        )
    if isinstance(value, LayoutLine):
        net_s = f" net={value.net}" if value.net else ""
        return f"{value.layer}:{value.purpose} line pts={len(value.points)}{net_s}"
    if isinstance(value, LayoutVia):
        net_s = f" net={value.net}" if value.net else ""
        params_s = f" params={dict(sorted(value.params.items()))!r}" if value.params else ""
        return f"{value.via_def} ({value.via_kind}) @{value.origin!r} {value.orient}{net_s}{params_s}"
    if isinstance(value, LayoutLabel):
        net_s = f" net={value.net}" if value.net else ""
        ltype_s = f" [{value.label_type}]" if value.label_type != "normalLabel" else ""
        return (
            f"{value.layer}:{value.purpose} label {value.text!r} "
            f"@{value.xy!r} h={value.height}{ltype_s}{net_s}"
        )
    if isinstance(value, LayoutBlockage):
        layer_s = f" layer={value.layer}" if value.layer else ""
        return f"blockage({value.blockage_type}{layer_s}, pts={len(value.points)})"
    if isinstance(value, LayoutBoundary):
        name_s = f" name={value.name!r}" if value.name else ""
        return f"boundary({value.boundary_kind}{name_s}, pts={len(value.points)})"
    if isinstance(value, LayoutFigGroup):
        return (
            f"fig_group({value.name!r}, origin={value.origin!r}, "
            f"orient={value.orient}, type={value.group_type})"
        )
    return repr(_to_plain(value))


def _append_entries(lines: list[str], heading: str, entries: Sequence[Any]) -> None:
    if not entries:
        return
    lines.append(f"  {heading}")
    for entry in entries:
        if isinstance(entry, LayoutNetChange):
            details = f"{entry.name} ({entry.change_type})"
            details += (
                f" sig_type {entry.old_sig_type!r} -> {entry.new_sig_type!r},"
                f" global {entry.old_is_global!r} -> {entry.new_is_global!r},"
                f" bits {entry.old_num_bits!r} -> {entry.new_num_bits!r}"
            )
            if not _strict_equal(entry.old_props, entry.new_props):
                details += (
                    f", props {_format_value(entry.old_props)} -> {_format_value(entry.new_props)}"
                )
        elif isinstance(entry, LayoutPinChange):
            details = f"{entry.key} ({entry.change_type})"
            if entry.change_type == "modified":
                details += (
                    f" [{', '.join(entry.changed_fields)}]"
                    f" {_format_value(entry.old)} -> {_format_value(entry.new)}"
                )
            elif entry.change_type == "added":
                details += f" {_format_value(entry.new)}"
            else:
                details += f" {_format_value(entry.old)}"
        elif isinstance(entry, LayoutMasterChange):
            details = f"{entry.instance} {entry.old_master!r} -> {entry.new_master!r}"
        elif isinstance(entry, LayoutPcellParamChange):
            prefix = f"{entry.target_kind}:{entry.instance}" if entry.target_kind == "mosaic" else entry.instance
            details = f"{prefix}.{entry.param} {_format_value(entry.old)} -> {_format_value(entry.new)}"
            if entry.old_type != entry.new_type:
                details += f" ({entry.old_type!r} -> {entry.new_type!r})"
        elif isinstance(entry, LayoutInstConnectivityChange):
            details = f"{entry.instance}.{entry.terminal} {entry.old_net!r} -> {entry.new_net!r}"
        elif isinstance(entry, LayoutMosaicChange):
            details = f"{entry.name} ({entry.change_type})"
            if entry.change_type == "modified":
                details += (
                    f" [{', '.join(entry.changed_fields)}]"
                    f" master {entry.old_master!r} -> {entry.new_master!r},"
                    f" array ({entry.old_rows}x{entry.old_columns}, {entry.old_u_x}, {entry.old_u_y})"
                    f" -> ({entry.new_rows}x{entry.new_columns}, {entry.new_u_x}, {entry.new_u_y})"
                )
            elif entry.change_type == "added":
                details += (
                    f" master {entry.new_master!r},"
                    f" array ({entry.new_rows}x{entry.new_columns}, {entry.new_u_x}, {entry.new_u_y})"
                )
            else:
                details += (
                    f" master {entry.old_master!r},"
                    f" array ({entry.old_rows}x{entry.old_columns}, {entry.old_u_x}, {entry.old_u_y})"
                )
        elif isinstance(entry, LayoutViaConnectivityChange):
            details = (
                f"via@{entry.origin!r}/{entry.orient} [{', '.join(entry.changed_fields)}]"
                f" def {entry.old_via_def!r} -> {entry.new_via_def!r},"
                f" net {entry.old_net!r} -> {entry.new_net!r},"
                f" cuts ({entry.old_cut_rows!r}x{entry.old_cut_columns!r})"
                f" -> ({entry.new_cut_rows!r}x{entry.new_cut_columns!r})"
            )
        elif isinstance(entry, LayoutKeyValueChange):
            details = (
                f"{entry.scope}.{entry.key} ({entry.change_type})"
                f" {_format_value(entry.old)} -> {_format_value(entry.new)}"
            )
        elif isinstance(entry, LayoutInstanceMove):
            details = (
                f"{entry.name} (moved)"
                f" {entry.old_xy!r}/{entry.old_orient!r} -> {entry.new_xy!r}/{entry.new_orient!r}"
            )
        elif isinstance(entry, LayoutBoundaryChange):
            details = (
                f"{entry.key} (modified)"
                f" {_format_value(entry.old)} -> {_format_value(entry.new)}"
            )
        elif isinstance(entry, LayoutFigGroupChange):
            details = (
                f"{entry.name} (modified) [{', '.join(entry.changed_fields)}]"
                f" {_format_value(entry.old)} -> {_format_value(entry.new)}"
            )
        elif isinstance(entry, str):
            details = entry
        else:
            details = _format_value(entry)
        lines.append(f"    {details}")


def _tier_change_counts(diff: LayoutDiff) -> tuple[int, int, int]:
    conn = diff.connectivity
    geom = diff.geometry
    ann = diff.annotation
    conn_count = (
        len(conn.nets)
        + len(conn.pins)
        + len(conn.instances_added)
        + len(conn.instances_removed)
        + len(conn.master_changes)
        + len(conn.pcell_param_changes)
        + len(conn.pcell_flag_changes)
        + len(conn.inst_connectivity_changes)
        + len(conn.mosaic_changes)
        + len(conn.via_connectivity_changes)
        + len(conn.instance_prop_changes)
        + len(conn.cell_property_changes)
    )
    geom_count = (
        len(geom.instance_moves)
        + len(geom.mosaic_moves)
        + len(geom.pin_moves)
        + len(geom.vias_added)
        + len(geom.vias_removed)
        + len(geom.shapes_added)
        + len(geom.shapes_removed)
        + len(geom.blockages_added)
        + len(geom.blockages_removed)
        + len(geom.boundaries_added)
        + len(geom.boundaries_removed)
        + len(geom.boundaries_modified)
        + len(geom.fig_groups_added)
        + len(geom.fig_groups_removed)
        + len(geom.fig_groups_modified)
    )
    ann_count = (
        len(ann.labels_added)
        + len(ann.labels_removed)
        + len(ann.aux_shapes_added)
        + len(ann.aux_shapes_removed)
    )
    return conn_count, geom_count, ann_count


def _format_summary_line(diff: LayoutDiff, *, connectivity_only: bool = False) -> str:
    conn_count, geom_count, ann_count = _tier_change_counts(diff)
    parts = [f"connectivity={conn_count}"]
    if not connectivity_only:
        parts.append(f"geometry={geom_count}")
        parts.append(f"annotation={ann_count}")
    if diff.cellview_changed:
        parts.append("cellview_changed=yes")
    return "Summary: " + ", ".join(parts)


def _diffstat_bar(added: int, removed: int, *, max_width: int = 24) -> str:
    total = added + removed
    if total <= 0:
        return ""
    if total <= max_width:
        return "+" * added + "-" * removed
    plus_n = max(1, round(max_width * added / total)) if added > 0 else 0
    minus_n = max(1, max_width - plus_n) if removed > 0 else 0
    if plus_n + minus_n > max_width:
        if plus_n >= minus_n:
            plus_n -= 1
        else:
            minus_n -= 1
    return "+" * plus_n + "-" * minus_n


def format_layout_diff_stat(diff: LayoutDiff, *, connectivity_only: bool = False) -> str:
    """Render a compact Git-diffstat-style summary of a 3-tier layout diff."""

    lines = [f"{diff.old_cellview.key} -> {diff.new_cellview.key}"]
    has_relevant = (
        (diff.has_connectivity_changes or diff.cellview_changed)
        if connectivity_only
        else diff.has_changes
    )
    if not has_relevant:
        lines.append("No connectivity changes." if connectivity_only else "No layout changes.")
        return "\n".join(lines)

    lines.append(_format_summary_line(diff, connectivity_only=connectivity_only))

    conn = diff.connectivity
    conn_parts: list[str] = []
    if conn.nets:
        conn_parts.append(f"nets={len(conn.nets)}")
    if conn.pins:
        conn_parts.append(f"pins={len(conn.pins)}")
    if conn.instances_added or conn.instances_removed:
        conn_parts.append(f"insts=+{len(conn.instances_added)}/-{len(conn.instances_removed)}")
    if conn.master_changes:
        conn_parts.append(f"masters={len(conn.master_changes)}")
    if conn.pcell_param_changes:
        conn_parts.append(f"pcell_params={len(conn.pcell_param_changes)}")
    if conn.pcell_flag_changes:
        conn_parts.append(f"pcell_flags={len(conn.pcell_flag_changes)}")
    if conn.inst_connectivity_changes:
        conn_parts.append(f"inst_conn={len(conn.inst_connectivity_changes)}")
    if conn.mosaic_changes:
        conn_parts.append(f"mosaics={len(conn.mosaic_changes)}")
    if conn.via_connectivity_changes:
        conn_parts.append(f"via_conn={len(conn.via_connectivity_changes)}")
    if conn.instance_prop_changes:
        conn_parts.append(f"inst_props={len(conn.instance_prop_changes)}")
    if conn.cell_property_changes:
        conn_parts.append(f"cell_props={len(conn.cell_property_changes)}")
    if conn_parts:
        lines.append("  Connectivity: " + ", ".join(conn_parts))

    if not connectivity_only:
        geom = diff.geometry
        geom_parts: list[str] = []
        if geom.instance_moves:
            geom_parts.append(f"inst_moves={len(geom.instance_moves)}")
        if geom.mosaic_moves:
            geom_parts.append(f"mosaic_moves={len(geom.mosaic_moves)}")
        if geom.pin_moves:
            geom_parts.append(f"pin_moves={len(geom.pin_moves)}")
        if geom.vias_added or geom.vias_removed:
            geom_parts.append(f"vias=+{len(geom.vias_added)}/-{len(geom.vias_removed)}")
        if geom.shapes_added or geom.shapes_removed:
            geom_parts.append(f"shapes=+{len(geom.shapes_added)}/-{len(geom.shapes_removed)}")
        if geom.blockages_added or geom.blockages_removed:
            geom_parts.append(
                f"blockages=+{len(geom.blockages_added)}/-{len(geom.blockages_removed)}"
            )
        if geom.boundaries_added or geom.boundaries_removed or geom.boundaries_modified:
            geom_parts.append(
                f"boundaries=+{len(geom.boundaries_added)}/-{len(geom.boundaries_removed)}"
                f"/~{len(geom.boundaries_modified)}"
            )
        if geom.fig_groups_added or geom.fig_groups_removed or geom.fig_groups_modified:
            geom_parts.append(
                f"fig_groups=+{len(geom.fig_groups_added)}/-{len(geom.fig_groups_removed)}"
                f"/~{len(geom.fig_groups_modified)}"
            )
        if geom_parts:
            lines.append("  Geometry:     " + ", ".join(geom_parts))

        ann = diff.annotation
        ann_parts: list[str] = []
        if ann.labels_added or ann.labels_removed:
            ann_parts.append(f"labels=+{len(ann.labels_added)}/-{len(ann.labels_removed)}")
        if ann.aux_shapes_added or ann.aux_shapes_removed:
            ann_parts.append(
                f"aux_shapes=+{len(ann.aux_shapes_added)}/-{len(ann.aux_shapes_removed)}"
            )
        if ann_parts:
            lines.append("  Annotation:   " + ", ".join(ann_parts))

        if geom.layer_deltas:
            lines.append("  Layers:")
            rows = [
                (f"{layer_key} +{counts['added']} / -{counts['removed']}", counts["added"], counts["removed"])
                for layer_key, counts in sorted(geom.layer_deltas.items())
            ]
            label_width = max(len(label) for label, _, _ in rows)
            for label, added, removed in rows:
                bar = _diffstat_bar(added, removed)
                suffix = f"  {bar}" if bar else ""
                lines.append(f"    {label:<{label_width}}{suffix}")

    return "\n".join(lines)


def format_layout_diff_text(diff: LayoutDiff, *, connectivity_only: bool = False) -> str:
    """Render a deterministic, scannable text summary of a 3-tier layout diff."""

    lines = [f"{diff.old_cellview.key} -> {diff.new_cellview.key}"]
    if diff.has_changes:
        lines.append(_format_summary_line(diff, connectivity_only=connectivity_only))

    lines.append("Connectivity changes")
    conn = diff.connectivity
    _append_entries(lines, "Nets", conn.nets)
    _append_entries(lines, "Pins", conn.pins)
    _append_entries(lines, "Instances added", conn.instances_added)
    _append_entries(lines, "Instances removed", conn.instances_removed)
    _append_entries(lines, "Master changes", conn.master_changes)
    _append_entries(lines, "PCell parameter changes", conn.pcell_param_changes)
    _append_entries(lines, "PCell flag changes", conn.pcell_flag_changes)
    _append_entries(lines, "Instance connectivity changes", conn.inst_connectivity_changes)
    _append_entries(lines, "Mosaic changes", conn.mosaic_changes)
    _append_entries(lines, "Via connectivity changes", conn.via_connectivity_changes)
    _append_entries(lines, "Instance property changes", conn.instance_prop_changes)
    _append_entries(lines, "Cellview property changes", conn.cell_property_changes)
    if conn.is_empty:
        lines.append("  none")

    if not connectivity_only:
        lines.append("Geometry changes")
        geom = diff.geometry
        if geom.layer_deltas:
            lines.append("  Layer deltas")
            for layer_key, counts in sorted(geom.layer_deltas.items()):
                lines.append(
                    f"    {layer_key}: +{counts['added']} / -{counts['removed']}"
                )
        _append_entries(lines, "Instance moves", geom.instance_moves)
        _append_entries(lines, "Mosaic moves", geom.mosaic_moves)
        _append_entries(lines, "Pin moves", geom.pin_moves)
        _append_entries(lines, "Vias added", geom.vias_added)
        _append_entries(lines, "Vias removed", geom.vias_removed)
        _append_entries(lines, "Shapes added", geom.shapes_added)
        _append_entries(lines, "Shapes removed", geom.shapes_removed)
        _append_entries(lines, "Blockages added", geom.blockages_added)
        _append_entries(lines, "Blockages removed", geom.blockages_removed)
        _append_entries(lines, "Boundaries added", geom.boundaries_added)
        _append_entries(lines, "Boundaries removed", geom.boundaries_removed)
        _append_entries(lines, "Boundaries modified", geom.boundaries_modified)
        _append_entries(lines, "FigGroups added", geom.fig_groups_added)
        _append_entries(lines, "FigGroups removed", geom.fig_groups_removed)
        _append_entries(lines, "FigGroups modified", geom.fig_groups_modified)
        if geom.is_empty:
            lines.append("  none")

        lines.append("Annotation changes")
        ann = diff.annotation
        _append_entries(lines, "Labels added", ann.labels_added)
        _append_entries(lines, "Labels removed", ann.labels_removed)
        _append_entries(lines, "Auxiliary shapes added", ann.aux_shapes_added)
        _append_entries(lines, "Auxiliary shapes removed", ann.aux_shapes_removed)
        if ann.is_empty:
            lines.append("  none")

    return "\n".join(lines)
