"""Deterministic Fail-Closed object-level three-way merge for OpenAccess layouts.

Named/keyed entities (nets, pins, instances, mosaics, singleton/named boundaries,
figure groups, cell properties, and manifest metadata) undergo field-level
three-way merges so disjoint edits on the same object combine cleanly.

Geometric/multiset entities (shapes, vias, labels, blockages, unnamed
boundaries, omissions) undergo canonical multiset three-way merges, augmented
by spatial overlap conflict detection across branches on the same
``(layer, purpose)`` (and overlapping newly added vias on the same ``via_def``).

Fail-Closed invariant: if any conflict exists in any domain, ``merge_layouts``
returns ``LayoutMergeResult(ok=False, snapshot=None, conflicts=...)`` and never
emits a partial or invalid snapshot.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal

from .layout_diff import LayoutShape, is_auxiliary_shape, is_layout_shape
from .layout_model import (
    BBox,
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
    OmissionRecord,
    ParamValue,
    SchemaValidationError,
    is_stripped_metadata_key,
)
from .layout_snapshot import (
    build_layout_snapshot,
    canonicalize_layout_objects,
    quantize_layout_coord,
)

__all__ = [
    "LayoutMergeConflict",
    "LayoutMergeResult",
    "merge_layouts",
    "format_layout_merge_conflicts",
]

ConflictDomain = Literal["connectivity", "geometry", "annotation"]

_MISSING = object()
_GEOM_EPS = 1e-9


@dataclass(frozen=True)
class LayoutMergeConflict:
    """A single deterministic conflict in connectivity, geometry, or annotation."""

    domain: ConflictDomain
    path: str
    base: Any
    ours: Any
    theirs: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": _to_plain(self.base),
            "domain": self.domain,
            "ours": _to_plain(self.ours),
            "path": self.path,
            "reason": self.reason,
            "theirs": _to_plain(self.theirs),
        }


@dataclass(frozen=True)
class LayoutMergeResult:
    """Result of three-way merging three layout snapshots."""

    ok: bool
    snapshot: LayoutSnapshot | None
    conflicts: tuple[LayoutMergeConflict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "ok": self.ok,
            "snapshot": _to_plain(self.snapshot),
        }


# ---------------------------------------------------------------------------
# Strict Type-Aware Comparison & Deterministic Key Helpers
# ---------------------------------------------------------------------------


def _strict_equal(left: Any, right: Any) -> bool:
    """Compare values recursively while preserving exact runtime types."""

    if left is _MISSING or right is _MISSING:
        return left is right
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        if len(left) != len(right):
            return False
        remaining = list(right.items())
        for left_key, left_value in left.items():
            for index, (right_key, right_value) in enumerate(remaining):
                if _strict_equal(left_key, right_key):
                    if not _strict_equal(left_value, right_value):
                        return False
                    remaining.pop(index)
                    break
            else:
                return False
        return not remaining
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, (set, frozenset)):
        return len(left) == len(right) and sorted(
            _stable_key(item) for item in left
        ) == sorted(_stable_key(item) for item in right)
    if is_dataclass(left):
        return all(
            _strict_equal(getattr(left, item.name), getattr(right, item.name))
            for item in fields(left)
        )
    return left == right


def _stable_key(value: Any) -> tuple[Any, ...]:
    """Build a total, type-aware ordering key for deterministic sorting."""

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
    if isinstance(value, tuple):
        return ("tuple", tuple(_stable_key(item) for item in value))
    if isinstance(value, list):
        return ("list", tuple(_stable_key(item) for item in value))
    if isinstance(value, Mapping):
        return (
            "mapping",
            tuple(
                sorted(
                    (
                        (_stable_key(key), _stable_key(item))
                        for key, item in value.items()
                    ),
                    key=lambda pair: pair[0],
                )
            ),
        )
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
    """Convert domain values into deterministic JSON-compatible values."""

    if value is _MISSING:
        return None
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


def _key_text(value: Any) -> str:
    return repr(_to_plain(value))


def _conflict_value(value: Any) -> Any:
    return None if value is _MISSING else value


def _sort_conflicts(
    conflicts: Sequence[LayoutMergeConflict],
) -> tuple[LayoutMergeConflict, ...]:
    return tuple(
        sorted(
            conflicts,
            key=lambda item: (
                item.domain,
                item.path,
                item.reason,
                _stable_key(item.base),
                _stable_key(item.ours),
                _stable_key(item.theirs),
            ),
        )
    )


def _normalize_cell_properties(props: Mapping[str, ParamValue]) -> dict[str, ParamValue]:
    out: dict[str, ParamValue] = {}
    for key in sorted(props):
        if is_stripped_metadata_key(key):
            continue
        val = props[key]
        if type(val) is float:
            val = quantize_layout_coord(val)
        out[key] = val
    return out


# ---------------------------------------------------------------------------
# Generic 3-Way Merge Primitives
# ---------------------------------------------------------------------------


def _merge_atom(
    base: Any,
    ours: Any,
    theirs: Any,
    *,
    domain: ConflictDomain,
    path: str,
    conflicts: list[LayoutMergeConflict],
) -> Any:
    """Apply classic three-way merge semantics to a single value or presence slot."""

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
        LayoutMergeConflict(
            domain=domain,
            path=path,
            base=_conflict_value(base),
            ours=_conflict_value(ours),
            theirs=_conflict_value(theirs),
            reason=reason,
        )
    )
    # Placeholder value only; Fail-Closed discards the snapshot when conflicts exist.
    return ours if ours is not _MISSING else theirs


def _merge_mapping(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    *,
    domain: ConflictDomain,
    path_prefix: str,
    conflicts: list[LayoutMergeConflict],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        value = _merge_atom(
            base.get(key, _MISSING),
            ours.get(key, _MISSING),
            theirs.get(key, _MISSING),
            domain=domain,
            path=f"{path_prefix}.{key}",
            conflicts=conflicts,
        )
        if value is not _MISSING:
            result[key] = value
    return result


def _merge_multiset(
    base: Sequence[Any],
    ours: Sequence[Any],
    theirs: Sequence[Any],
    *,
    domain: ConflictDomain,
    path_prefix: str,
    conflicts: list[LayoutMergeConflict],
) -> tuple[Any, ...]:
    """Three-way merge canonicalized multisets by exact stable item key."""

    groups: list[dict[tuple[Any, ...], list[Any]]] = []
    for values in (base, ours, theirs):
        grouped: dict[tuple[Any, ...], list[Any]] = {}
        for value in values:
            grouped.setdefault(_stable_key(value), []).append(value)
        groups.append(grouped)

    base_groups, ours_groups, theirs_groups = groups
    result: list[Any] = []
    for index, key in enumerate(
        sorted(set(base_groups) | set(ours_groups) | set(theirs_groups))
    ):
        base_count = len(base_groups.get(key, []))
        ours_count = len(ours_groups.get(key, []))
        theirs_count = len(theirs_groups.get(key, []))
        count = _merge_atom(
            base_count,
            ours_count,
            theirs_count,
            domain=domain,
            path=f"{path_prefix}[{index}:{_key_text(key)}]",
            conflicts=conflicts,
        )
        if count is _MISSING or count <= 0:
            continue
        representative = next(
            (
                values[key][0]
                for values in (ours_groups, theirs_groups, base_groups)
                if values.get(key)
            ),
            None,
        )
        if representative is not None:
            result.extend([representative] * count)
    return tuple(result)


def _align_branch_occurrences(
    base: Sequence[Any], branch: Sequence[Any]
) -> list[Any]:
    """Align a branch's occurrences to base occurrences, retaining exact matches."""

    branch_remaining = list(sorted(branch, key=_stable_key))
    aligned: list[Any] = []
    for base_item in sorted(base, key=_stable_key):
        exact_index = next(
            (
                idx
                for idx, cand in enumerate(branch_remaining)
                if _strict_equal(base_item, cand)
            ),
            None,
        )
        if exact_index is None:
            aligned.append(_MISSING)
        else:
            aligned.append(branch_remaining.pop(exact_index))
    for idx, value in enumerate(aligned):
        if value is _MISSING and branch_remaining:
            aligned[idx] = branch_remaining.pop(0)
    aligned.extend(branch_remaining)
    return aligned


# ---------------------------------------------------------------------------
# Entity-Specific Field-Level 3-Way Mergers
# ---------------------------------------------------------------------------


def _merge_nets(
    base: Sequence[LayoutNet],
    ours: Sequence[LayoutNet],
    theirs: Sequence[LayoutNet],
    conflicts: list[LayoutMergeConflict],
) -> list[LayoutNet]:
    b_map = {n.name: n for n in base}
    o_map = {n.name: n for n in ours}
    t_map = {n.name: n for n in theirs}
    result: list[LayoutNet] = []
    for name in sorted(set(b_map) | set(o_map) | set(t_map)):
        b = b_map.get(name, _MISSING)
        o = o_map.get(name, _MISSING)
        t = t_map.get(name, _MISSING)
        if b is _MISSING or o is _MISSING or t is _MISSING:
            val = _merge_atom(
                b, o, t, domain="connectivity", path=f"nets.{name}", conflicts=conflicts
            )
            if val is not _MISSING:
                result.append(val)
            continue
        assert isinstance(b, LayoutNet) and isinstance(o, LayoutNet) and isinstance(t, LayoutNet)
        result.append(
            LayoutNet(
                name=name,
                is_global=_merge_atom(
                    b.is_global,
                    o.is_global,
                    t.is_global,
                    domain="connectivity",
                    path=f"nets.{name}.is_global",
                    conflicts=conflicts,
                ),
                num_bits=_merge_atom(
                    b.num_bits,
                    o.num_bits,
                    t.num_bits,
                    domain="connectivity",
                    path=f"nets.{name}.num_bits",
                    conflicts=conflicts,
                ),
                sig_type=_merge_atom(
                    b.sig_type,
                    o.sig_type,
                    t.sig_type,
                    domain="connectivity",
                    path=f"nets.{name}.sig_type",
                    conflicts=conflicts,
                ),
                props=_merge_mapping(
                    b.props,
                    o.props,
                    t.props,
                    domain="connectivity",
                    path_prefix=f"nets.{name}.props",
                    conflicts=conflicts,
                ),
            )
        )
    return result


def _merge_pin_fields(
    b: LayoutPin,
    o: LayoutPin,
    t: LayoutPin,
    path: str,
    conflicts: list[LayoutMergeConflict],
) -> LayoutPin:
    net = _merge_atom(
        b.net, o.net, t.net, domain="connectivity", path=f"{path}.net", conflicts=conflicts
    )
    direction = _merge_atom(
        b.direction,
        o.direction,
        t.direction,
        domain="connectivity",
        path=f"{path}.direction",
        conflicts=conflicts,
    )
    layer = _merge_atom(
        b.layer, o.layer, t.layer, domain="connectivity", path=f"{path}.layer", conflicts=conflicts
    )
    purpose = _merge_atom(
        b.purpose,
        o.purpose,
        t.purpose,
        domain="connectivity",
        path=f"{path}.purpose",
        conflicts=conflicts,
    )
    # Keep shape_kind and its geometry fields invariant-consistent when shape_kind changes.
    def _pin_geom(pin: LayoutPin) -> tuple[Any, ...]:
        return (
            pin.shape_kind,
            pin.bbox,
            pin.points,
            pin.begin_pt,
            pin.end_pt,
            pin.width,
            pin.begin_style,
            pin.end_style,
            pin.begin_ext,
            pin.end_ext,
            pin.path_style,
        )

    if not _strict_equal(o.shape_kind, b.shape_kind) or not _strict_equal(
        t.shape_kind, b.shape_kind
    ):
        (
            shape_kind,
            bbox,
            points,
            begin_pt,
            end_pt,
            width,
            begin_style,
            end_style,
            begin_ext,
            end_ext,
            path_style,
        ) = _merge_atom(
            _pin_geom(b),
            _pin_geom(o),
            _pin_geom(t),
            domain="connectivity",
            path=f"{path}.shape",
            conflicts=conflicts,
        )
    else:
        shape_kind = b.shape_kind
        bbox = _merge_atom(
            b.bbox, o.bbox, t.bbox, domain="geometry", path=f"{path}.bbox", conflicts=conflicts
        )
        points = _merge_atom(
            b.points,
            o.points,
            t.points,
            domain="geometry",
            path=f"{path}.points",
            conflicts=conflicts,
        )
        begin_pt = _merge_atom(
            b.begin_pt,
            o.begin_pt,
            t.begin_pt,
            domain="geometry",
            path=f"{path}.begin_pt",
            conflicts=conflicts,
        )
        end_pt = _merge_atom(
            b.end_pt,
            o.end_pt,
            t.end_pt,
            domain="geometry",
            path=f"{path}.end_pt",
            conflicts=conflicts,
        )
        width = _merge_atom(
            b.width, o.width, t.width, domain="geometry", path=f"{path}.width", conflicts=conflicts
        )
        begin_style = _merge_atom(
            b.begin_style,
            o.begin_style,
            t.begin_style,
            domain="geometry",
            path=f"{path}.begin_style",
            conflicts=conflicts,
        )
        end_style = _merge_atom(
            b.end_style,
            o.end_style,
            t.end_style,
            domain="geometry",
            path=f"{path}.end_style",
            conflicts=conflicts,
        )
        begin_ext = _merge_atom(
            b.begin_ext,
            o.begin_ext,
            t.begin_ext,
            domain="geometry",
            path=f"{path}.begin_ext",
            conflicts=conflicts,
        )
        end_ext = _merge_atom(
            b.end_ext,
            o.end_ext,
            t.end_ext,
            domain="geometry",
            path=f"{path}.end_ext",
            conflicts=conflicts,
        )
        path_style = _merge_atom(
            b.path_style,
            o.path_style,
            t.path_style,
            domain="geometry",
            path=f"{path}.path_style",
            conflicts=conflicts,
        )
    access_dir = _merge_atom(
        b.access_dir,
        o.access_dir,
        t.access_dir,
        domain="geometry",
        path=f"{path}.access_dir",
        conflicts=conflicts,
    )
    props = _merge_mapping(
        b.props,
        o.props,
        t.props,
        domain="connectivity",
        path_prefix=f"{path}.props",
        conflicts=conflicts,
    )
    return LayoutPin(
        name=b.name,
        pin_name=b.pin_name,
        net=net,
        direction=direction,
        layer=layer,
        purpose=purpose,
        shape_kind=shape_kind,
        bbox=bbox,
        points=points,
        begin_pt=begin_pt,
        end_pt=end_pt,
        width=width,
        begin_style=begin_style,
        end_style=end_style,
        begin_ext=begin_ext,
        end_ext=end_ext,
        path_style=path_style,
        access_dir=access_dir,
        props=props,
    )


def _merge_pins(
    base: Sequence[LayoutPin],
    ours: Sequence[LayoutPin],
    theirs: Sequence[LayoutPin],
    conflicts: list[LayoutMergeConflict],
) -> list[LayoutPin]:
    def group(items: Sequence[LayoutPin]) -> dict[tuple[str, str], list[LayoutPin]]:
        g: dict[tuple[str, str], list[LayoutPin]] = defaultdict(list)
        for item in items:
            g[(item.name, item.pin_name)].append(item)
        for vals in g.values():
            vals.sort(key=_stable_key)
        return g

    b_groups = group(base)
    o_groups = group(ours)
    t_groups = group(theirs)
    result: list[LayoutPin] = []
    for name, pin_name in sorted(set(b_groups) | set(o_groups) | set(t_groups)):
        b_vals = b_groups.get((name, pin_name), [])
        o_vals = _align_branch_occurrences(b_vals, o_groups.get((name, pin_name), []))
        t_vals = _align_branch_occurrences(b_vals, t_groups.get((name, pin_name), []))
        pin_key = f"{name}:{pin_name}" if pin_name else name
        slots = max(len(b_vals), len(o_vals), len(t_vals))
        for idx in range(slots):
            b = b_vals[idx] if idx < len(b_vals) else _MISSING
            o = o_vals[idx] if idx < len(o_vals) else _MISSING
            t = t_vals[idx] if idx < len(t_vals) else _MISSING
            path = f"pins[{pin_key!r}]"
            if idx:
                path += f"[{idx}]"
            if b is not _MISSING and o is not _MISSING and t is not _MISSING:
                assert (
                    isinstance(b, LayoutPin)
                    and isinstance(o, LayoutPin)
                    and isinstance(t, LayoutPin)
                )
                result.append(_merge_pin_fields(b, o, t, path, conflicts))
            else:
                val = _merge_atom(
                    b, o, t, domain="connectivity", path=path, conflicts=conflicts
                )
                if val is not _MISSING:
                    result.append(val)
    return result


def _merge_instances(
    base: Sequence[LayoutInstance],
    ours: Sequence[LayoutInstance],
    theirs: Sequence[LayoutInstance],
    conflicts: list[LayoutMergeConflict],
) -> list[LayoutInstance]:
    b_map = {inst.name: inst for inst in base}
    o_map = {inst.name: inst for inst in ours}
    t_map = {inst.name: inst for inst in theirs}
    result: list[LayoutInstance] = []
    for name in sorted(set(b_map) | set(o_map) | set(t_map)):
        b = b_map.get(name, _MISSING)
        o = o_map.get(name, _MISSING)
        t = t_map.get(name, _MISSING)
        if b is _MISSING or o is _MISSING or t is _MISSING:
            val = _merge_atom(
                b, o, t, domain="connectivity", path=f"instances.{name}", conflicts=conflicts
            )
            if val is not _MISSING:
                result.append(val)
            continue
        assert (
            isinstance(b, LayoutInstance)
            and isinstance(o, LayoutInstance)
            and isinstance(t, LayoutInstance)
        )
        lib, cell, view = _merge_atom(
            (b.lib, b.cell, b.view),
            (o.lib, o.cell, o.view),
            (t.lib, t.cell, t.view),
            domain="connectivity",
            path=f"instances.{name}.master",
            conflicts=conflicts,
        )
        xy = _merge_atom(
            b.xy, o.xy, t.xy, domain="geometry", path=f"instances.{name}.xy", conflicts=conflicts
        )
        orient = _merge_atom(
            b.orient,
            o.orient,
            t.orient,
            domain="geometry",
            path=f"instances.{name}.orient",
            conflicts=conflicts,
        )
        is_pcell = _merge_atom(
            b.is_pcell,
            o.is_pcell,
            t.is_pcell,
            domain="connectivity",
            path=f"instances.{name}.is_pcell",
            conflicts=conflicts,
        )
        pcell_params = _merge_mapping(
            b.pcell_params,
            o.pcell_params,
            t.pcell_params,
            domain="connectivity",
            path_prefix=f"instances.{name}.pcell_params",
            conflicts=conflicts,
        )
        pcell_param_types = _merge_mapping(
            b.pcell_param_types,
            o.pcell_param_types,
            t.pcell_param_types,
            domain="connectivity",
            path_prefix=f"instances.{name}.pcell_param_types",
            conflicts=conflicts,
        )
        if not is_pcell and pcell_params:
            conflicts.append(
                LayoutMergeConflict(
                    domain="connectivity",
                    path=f"instances.{name}.is_pcell",
                    base={"is_pcell": b.is_pcell, "pcell_params": b.pcell_params},
                    ours={"is_pcell": o.is_pcell, "pcell_params": o.pcell_params},
                    theirs={"is_pcell": t.is_pcell, "pcell_params": t.pcell_params},
                    reason="pcell_flag_param_inconsistency",
                )
            )
        connections = _merge_mapping(
            b.connections,
            o.connections,
            t.connections,
            domain="connectivity",
            path_prefix=f"instances.{name}.connections",
            conflicts=conflicts,
        )
        props = _merge_mapping(
            b.props,
            o.props,
            t.props,
            domain="connectivity",
            path_prefix=f"instances.{name}.props",
            conflicts=conflicts,
        )
        prop_types = _merge_mapping(
            b.prop_types,
            o.prop_types,
            t.prop_types,
            domain="connectivity",
            path_prefix=f"instances.{name}.prop_types",
            conflicts=conflicts,
        )
        result.append(
            LayoutInstance(
                name=name,
                lib=lib,
                cell=cell,
                view=view,
                xy=xy,
                orient=orient,
                is_pcell=is_pcell,
                pcell_params=pcell_params,
                pcell_param_types=pcell_param_types,
                connections=connections,
                props=props,
                prop_types=prop_types,
            )
        )
    return result


def _merge_mosaics(
    base: Sequence[LayoutMosaic],
    ours: Sequence[LayoutMosaic],
    theirs: Sequence[LayoutMosaic],
    conflicts: list[LayoutMergeConflict],
) -> list[LayoutMosaic]:
    b_map = {m.name: m for m in base}
    o_map = {m.name: m for m in ours}
    t_map = {m.name: m for m in theirs}
    result: list[LayoutMosaic] = []
    for name in sorted(set(b_map) | set(o_map) | set(t_map)):
        b = b_map.get(name, _MISSING)
        o = o_map.get(name, _MISSING)
        t = t_map.get(name, _MISSING)
        if b is _MISSING or o is _MISSING or t is _MISSING:
            val = _merge_atom(
                b, o, t, domain="connectivity", path=f"mosaics.{name}", conflicts=conflicts
            )
            if val is not _MISSING:
                result.append(val)
            continue
        assert (
            isinstance(b, LayoutMosaic)
            and isinstance(o, LayoutMosaic)
            and isinstance(t, LayoutMosaic)
        )
        lib, cell, view = _merge_atom(
            (b.lib, b.cell, b.view),
            (o.lib, o.cell, o.view),
            (t.lib, t.cell, t.view),
            domain="connectivity",
            path=f"mosaics.{name}.master",
            conflicts=conflicts,
        )
        xy = _merge_atom(
            b.xy, o.xy, t.xy, domain="geometry", path=f"mosaics.{name}.xy", conflicts=conflicts
        )
        orient = _merge_atom(
            b.orient,
            o.orient,
            t.orient,
            domain="geometry",
            path=f"mosaics.{name}.orient",
            conflicts=conflicts,
        )
        rows = _merge_atom(
            b.rows,
            o.rows,
            t.rows,
            domain="connectivity",
            path=f"mosaics.{name}.rows",
            conflicts=conflicts,
        )
        columns = _merge_atom(
            b.columns,
            o.columns,
            t.columns,
            domain="connectivity",
            path=f"mosaics.{name}.columns",
            conflicts=conflicts,
        )
        u_x = _merge_atom(
            b.u_x,
            o.u_x,
            t.u_x,
            domain="connectivity",
            path=f"mosaics.{name}.u_x",
            conflicts=conflicts,
        )
        u_y = _merge_atom(
            b.u_y,
            o.u_y,
            t.u_y,
            domain="connectivity",
            path=f"mosaics.{name}.u_y",
            conflicts=conflicts,
        )
        is_pcell = _merge_atom(
            b.is_pcell,
            o.is_pcell,
            t.is_pcell,
            domain="connectivity",
            path=f"mosaics.{name}.is_pcell",
            conflicts=conflicts,
        )
        pcell_params = _merge_mapping(
            b.pcell_params,
            o.pcell_params,
            t.pcell_params,
            domain="connectivity",
            path_prefix=f"mosaics.{name}.pcell_params",
            conflicts=conflicts,
        )
        pcell_param_types = _merge_mapping(
            b.pcell_param_types,
            o.pcell_param_types,
            t.pcell_param_types,
            domain="connectivity",
            path_prefix=f"mosaics.{name}.pcell_param_types",
            conflicts=conflicts,
        )
        if not is_pcell and pcell_params:
            conflicts.append(
                LayoutMergeConflict(
                    domain="connectivity",
                    path=f"mosaics.{name}.is_pcell",
                    base={"is_pcell": b.is_pcell, "pcell_params": b.pcell_params},
                    ours={"is_pcell": o.is_pcell, "pcell_params": o.pcell_params},
                    theirs={"is_pcell": t.is_pcell, "pcell_params": t.pcell_params},
                    reason="pcell_flag_param_inconsistency",
                )
            )
        props = _merge_mapping(
            b.props,
            o.props,
            t.props,
            domain="connectivity",
            path_prefix=f"mosaics.{name}.props",
            conflicts=conflicts,
        )
        prop_types = _merge_mapping(
            b.prop_types,
            o.prop_types,
            t.prop_types,
            domain="connectivity",
            path_prefix=f"mosaics.{name}.prop_types",
            conflicts=conflicts,
        )
        result.append(
            LayoutMosaic(
                name=name,
                lib=lib,
                cell=cell,
                view=view,
                xy=xy,
                orient=orient,
                rows=rows,
                columns=columns,
                u_x=u_x,
                u_y=u_y,
                is_pcell=is_pcell,
                pcell_params=pcell_params,
                pcell_param_types=pcell_param_types,
                props=props,
                prop_types=prop_types,
            )
        )
    return result


def _merge_boundaries(
    base: Sequence[LayoutBoundary],
    ours: Sequence[LayoutBoundary],
    theirs: Sequence[LayoutBoundary],
    conflicts: list[LayoutMergeConflict],
) -> list[LayoutBoundary]:
    result: list[LayoutBoundary] = []

    # 1) Singleton prBoundary
    b_pr = next((x for x in base if x.boundary_kind == "prBoundary"), _MISSING)
    o_pr = next((x for x in ours if x.boundary_kind == "prBoundary"), _MISSING)
    t_pr = next((x for x in theirs if x.boundary_kind == "prBoundary"), _MISSING)
    if b_pr is not _MISSING and o_pr is not _MISSING and t_pr is not _MISSING:
        assert (
            isinstance(b_pr, LayoutBoundary)
            and isinstance(o_pr, LayoutBoundary)
            and isinstance(t_pr, LayoutBoundary)
        )
        result.append(
            LayoutBoundary(
                boundary_kind="prBoundary",
                points=_merge_atom(
                    b_pr.points,
                    o_pr.points,
                    t_pr.points,
                    domain="geometry",
                    path="boundaries.prBoundary.points",
                    conflicts=conflicts,
                ),
                name=_merge_atom(
                    b_pr.name,
                    o_pr.name,
                    t_pr.name,
                    domain="geometry",
                    path="boundaries.prBoundary.name",
                    conflicts=conflicts,
                ),
                props=_merge_mapping(
                    b_pr.props,
                    o_pr.props,
                    t_pr.props,
                    domain="geometry",
                    path_prefix="boundaries.prBoundary.props",
                    conflicts=conflicts,
                ),
            )
        )
    elif not (b_pr is _MISSING and o_pr is _MISSING and t_pr is _MISSING):
        val = _merge_atom(
            b_pr,
            o_pr,
            t_pr,
            domain="geometry",
            path="boundaries.prBoundary",
            conflicts=conflicts,
        )
        if val is not _MISSING:
            result.append(val)

    # 2) Named non-prBoundary boundaries keyed by (boundary_kind, name)
    def group_named(
        items: Sequence[LayoutBoundary],
    ) -> dict[tuple[str, str], list[LayoutBoundary]]:
        g: dict[tuple[str, str], list[LayoutBoundary]] = defaultdict(list)
        for item in items:
            if item.boundary_kind != "prBoundary" and item.name is not None:
                g[(item.boundary_kind, item.name)].append(item)
        for vals in g.values():
            vals.sort(key=_stable_key)
        return g

    b_named = group_named(base)
    o_named = group_named(ours)
    t_named = group_named(theirs)
    for bkind, bname in sorted(set(b_named) | set(o_named) | set(t_named)):
        b_vals = b_named.get((bkind, bname), [])
        o_vals = _align_branch_occurrences(b_vals, o_named.get((bkind, bname), []))
        t_vals = _align_branch_occurrences(b_vals, t_named.get((bkind, bname), []))
        slots = max(len(b_vals), len(o_vals), len(t_vals))
        for idx in range(slots):
            b = b_vals[idx] if idx < len(b_vals) else _MISSING
            o = o_vals[idx] if idx < len(o_vals) else _MISSING
            t = t_vals[idx] if idx < len(t_vals) else _MISSING
            path = f"boundaries[{bkind}:{bname}]"
            if idx:
                path += f"[{idx}]"
            if b is not _MISSING and o is not _MISSING and t is not _MISSING:
                assert (
                    isinstance(b, LayoutBoundary)
                    and isinstance(o, LayoutBoundary)
                    and isinstance(t, LayoutBoundary)
                )
                result.append(
                    LayoutBoundary(
                        boundary_kind=b.boundary_kind,
                        name=b.name,
                        points=_merge_atom(
                            b.points,
                            o.points,
                            t.points,
                            domain="geometry",
                            path=f"{path}.points",
                            conflicts=conflicts,
                        ),
                        props=_merge_mapping(
                            b.props,
                            o.props,
                            t.props,
                            domain="geometry",
                            path_prefix=f"{path}.props",
                            conflicts=conflicts,
                        ),
                    )
                )
            else:
                val = _merge_atom(b, o, t, domain="geometry", path=path, conflicts=conflicts)
                if val is not _MISSING:
                    result.append(val)

    # 3) Unnamed non-prBoundary boundaries as multiset
    b_unnamed = [x for x in base if x.boundary_kind != "prBoundary" and x.name is None]
    o_unnamed = [x for x in ours if x.boundary_kind != "prBoundary" and x.name is None]
    t_unnamed = [x for x in theirs if x.boundary_kind != "prBoundary" and x.name is None]
    result.extend(
        _merge_multiset(
            b_unnamed,
            o_unnamed,
            t_unnamed,
            domain="geometry",
            path_prefix="boundaries.unnamed",
            conflicts=conflicts,
        )
    )
    return result


def _merge_fig_groups(
    base: Sequence[LayoutFigGroup],
    ours: Sequence[LayoutFigGroup],
    theirs: Sequence[LayoutFigGroup],
    conflicts: list[LayoutMergeConflict],
) -> list[LayoutFigGroup]:
    b_map = {g.name: g for g in base}
    o_map = {g.name: g for g in ours}
    t_map = {g.name: g for g in theirs}
    result: list[LayoutFigGroup] = []
    for name in sorted(set(b_map) | set(o_map) | set(t_map)):
        b = b_map.get(name, _MISSING)
        o = o_map.get(name, _MISSING)
        t = t_map.get(name, _MISSING)
        if b is _MISSING or o is _MISSING or t is _MISSING:
            val = _merge_atom(
                b, o, t, domain="geometry", path=f"fig_groups.{name}", conflicts=conflicts
            )
            if val is not _MISSING:
                result.append(val)
            continue
        assert (
            isinstance(b, LayoutFigGroup)
            and isinstance(o, LayoutFigGroup)
            and isinstance(t, LayoutFigGroup)
        )
        result.append(
            LayoutFigGroup(
                name=name,
                origin=_merge_atom(
                    b.origin,
                    o.origin,
                    t.origin,
                    domain="geometry",
                    path=f"fig_groups.{name}.origin",
                    conflicts=conflicts,
                ),
                orient=_merge_atom(
                    b.orient,
                    o.orient,
                    t.orient,
                    domain="geometry",
                    path=f"fig_groups.{name}.orient",
                    conflicts=conflicts,
                ),
                group_type=_merge_atom(
                    b.group_type,
                    o.group_type,
                    t.group_type,
                    domain="geometry",
                    path=f"fig_groups.{name}.group_type",
                    conflicts=conflicts,
                ),
                props=_merge_mapping(
                    b.props,
                    o.props,
                    t.props,
                    domain="geometry",
                    path_prefix=f"fig_groups.{name}.props",
                    conflicts=conflicts,
                ),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Spatial Overlap Conflict Detection (Shapes & Vias)
# ---------------------------------------------------------------------------


def _pathseg_ext(style: str, ext: float, half_w: float) -> float:
    if style == "truncate":
        return 0.0
    if style in ("extend", "octagon"):
        return half_w if ext == 0.0 else max(0.0, ext)
    return max(0.0, ext)


def _segment_bbox(
    p0: tuple[float, float],
    p1: tuple[float, float],
    half_w: float,
    b_ext: float,
    e_ext: float,
) -> BBox:
    x0, y0 = p0
    x1, y1 = p1
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    if length <= _GEOM_EPS:
        pad = half_w + max(b_ext, e_ext)
        return ((x0 - pad, y0 - pad), (x0 + pad, y0 + pad))
    ux = dx / length
    uy = dy / length
    px = -uy * half_w
    py = ux * half_w
    sx = x0 - ux * b_ext
    sy = y0 - uy * b_ext
    ex = x1 + ux * e_ext
    ey = y1 + uy * e_ext
    xs = (sx + px, sx - px, ex + px, ex - px)
    ys = (sy + py, sy - py, ey + py, ey - py)
    return ((min(xs), min(ys)), (max(xs), max(ys)))


def _shape_bbox(shape: LayoutShape) -> BBox:
    if isinstance(shape, (LayoutRect, LayoutEllipse)):
        return shape.bbox
    if isinstance(shape, (LayoutPolygon, LayoutLine)):
        xs = [pt[0] for pt in shape.points]
        ys = [pt[1] for pt in shape.points]
        return ((min(xs), min(ys)), (max(xs), max(ys)))
    if isinstance(shape, LayoutPathSeg):
        hw = shape.width / 2.0
        b_ext = _pathseg_ext(shape.begin_style, shape.begin_ext, hw)
        e_ext = _pathseg_ext(shape.end_style, shape.end_ext, hw)
        return _segment_bbox(shape.begin_pt, shape.end_pt, hw, b_ext, e_ext)
    if isinstance(shape, LayoutPath):
        hw = shape.width / 2.0
        if shape.path_style == "truncateExtend":
            b_ext = 0.0
            e_ext = hw if shape.end_ext == 0.0 else max(0.0, shape.end_ext)
        elif shape.path_style in ("extendExtend", "roundRound"):
            b_ext = hw if shape.begin_ext == 0.0 else max(0.0, shape.begin_ext)
            e_ext = hw if shape.end_ext == 0.0 else max(0.0, shape.end_ext)
        else:  # varExtendExtend
            b_ext = max(0.0, shape.begin_ext)
            e_ext = max(0.0, shape.end_ext)
        pts = shape.points
        min_x = math.inf
        min_y = math.inf
        max_x = -math.inf
        max_y = -math.inf
        seg_count = len(pts) - 1
        for idx in range(seg_count):
            sb = b_ext if idx == 0 else hw
            se = e_ext if idx == seg_count - 1 else hw
            (x0, y0), (x1, y1) = _segment_bbox(pts[idx], pts[idx + 1], hw, sb, se)
            min_x = min(min_x, x0)
            min_y = min(min_y, y0)
            max_x = max(max_x, x1)
            max_y = max(max_y, y1)
        return ((min_x, min_y), (max_x, max_y))
    raise SchemaValidationError(f"Unsupported shape type: {type(shape).__name__}")


def _bboxes_conflict(
    a: BBox,
    b: BBox,
    net_a: str | None,
    net_b: str | None,
) -> bool:
    """Return True if bboxes strictly overlap, overlap as segments, or touch on different nets."""

    (ax0, ay0), (ax1, ay1) = a
    (bx0, by0), (bx1, by1) = b

    ix0 = max(ax0, bx0)
    ix1 = min(ax1, bx1)
    iy0 = max(ay0, by0)
    iy1 = min(ay1, by1)
    dx = ix1 - ix0
    dy = iy1 - iy0

    # Completely disjoint.
    if dx < -_GEOM_EPS or dy < -_GEOM_EPS:
        return False

    # Positive-area interior overlap always conflicts.
    if dx > _GEOM_EPS and dy > _GEOM_EPS:
        return True

    # Different non-None nets touching along an edge or vertex is an electrical short.
    diff_nets = net_a is not None and net_b is not None and net_a != net_b
    if diff_nets:
        return True

    # Handle 1D / degenerate segments (e.g., LayoutLine or width==0 PathSeg/Path).
    a_is_1d = (ax1 - ax0 <= _GEOM_EPS) or (ay1 - ay0 <= _GEOM_EPS)
    b_is_1d = (bx1 - bx0 <= _GEOM_EPS) or (by1 - by0 <= _GEOM_EPS)
    if a_is_1d or b_is_1d:
        # Overlapping along a positive-length line interval.
        if dx > _GEOM_EPS or dy > _GEOM_EPS:
            return True
        # Perpendicular 1D segments crossing strictly in their interiors.
        a_horiz = (ay1 - ay0 <= _GEOM_EPS) and (ax1 - ax0 > _GEOM_EPS)
        b_vert = (bx1 - bx0 <= _GEOM_EPS) and (by1 - by0 > _GEOM_EPS)
        if a_horiz and b_vert:
            return (ax0 + _GEOM_EPS < bx0 < ax1 - _GEOM_EPS) and (
                by0 + _GEOM_EPS < ay0 < by1 - _GEOM_EPS
            )
        a_vert = (ax1 - ax0 <= _GEOM_EPS) and (ay1 - ay0 > _GEOM_EPS)
        b_horiz = (by1 - by0 <= _GEOM_EPS) and (bx1 - bx0 > _GEOM_EPS)
        if a_vert and b_horiz:
            return (bx0 + _GEOM_EPS < ax0 < bx1 - _GEOM_EPS) and (
                ay0 + _GEOM_EPS < by0 < ay1 - _GEOM_EPS
            )

    return False


def _branch_exclusive_additions(
    base: Sequence[Any],
    ours: Sequence[Any],
    theirs: Sequence[Any],
) -> tuple[list[Any], list[Any]]:
    """Return deduplicated sorted items newly added exclusively by ``ours`` vs ``theirs``."""

    b_counts: Counter[tuple[Any, ...]] = Counter(_stable_key(x) for x in base)
    o_counts: Counter[tuple[Any, ...]] = Counter(_stable_key(x) for x in ours)
    t_counts: Counter[tuple[Any, ...]] = Counter(_stable_key(x) for x in theirs)

    o_reps = {_stable_key(x): x for x in ours}
    t_reps = {_stable_key(x): x for x in theirs}

    ours_only: list[Any] = []
    theirs_only: list[Any] = []

    for key in sorted(set(o_counts) | set(t_counts)):
        o_added = max(0, o_counts[key] - b_counts[key])
        t_added = max(0, t_counts[key] - b_counts[key])
        if o_added > t_added and key in o_reps:
            ours_only.append(o_reps[key])
        elif t_added > o_added and key in t_reps:
            theirs_only.append(t_reps[key])

    return ours_only, theirs_only


def _detect_shape_spatial_overlaps(
    base_shapes: Sequence[LayoutShape],
    ours_shapes: Sequence[LayoutShape],
    theirs_shapes: Sequence[LayoutShape],
    conflicts: list[LayoutMergeConflict],
) -> None:
    ours_only, theirs_only = _branch_exclusive_additions(
        base_shapes, ours_shapes, theirs_shapes
    )
    if not ours_only or not theirs_only:
        return

    o_by_lpp: dict[tuple[str, str], list[tuple[int, LayoutShape, BBox]]] = defaultdict(list)
    t_by_lpp: dict[tuple[str, str], list[tuple[int, LayoutShape, BBox]]] = defaultdict(list)

    for shape in ours_only:
        lpp = (shape.layer, shape.purpose)
        o_by_lpp[lpp].append((len(o_by_lpp[lpp]), shape, _shape_bbox(shape)))
    for shape in theirs_only:
        lpp = (shape.layer, shape.purpose)
        t_by_lpp[lpp].append((len(t_by_lpp[lpp]), shape, _shape_bbox(shape)))

    for lpp in sorted(set(o_by_lpp) & set(t_by_lpp)):
        layer, purpose = lpp
        o_entries = o_by_lpp[lpp]
        t_entries = sorted(t_by_lpp[lpp], key=lambda item: (item[2][0][0], item[0]))

        for o_idx, o_shape, o_bbox in o_entries:
            o_min_x, o_max_x = o_bbox[0][0], o_bbox[1][0]
            for t_idx, t_shape, t_bbox in t_entries:
                if t_bbox[0][0] > o_max_x + _GEOM_EPS:
                    break
                if t_bbox[1][0] < o_min_x - _GEOM_EPS:
                    continue
                if _strict_equal(o_shape, t_shape):
                    continue
                if _bboxes_conflict(o_bbox, t_bbox, o_shape.net, t_shape.net):
                    conflicts.append(
                        LayoutMergeConflict(
                            domain="geometry",
                            path=f"shapes.{layer}:{purpose}[ours={o_idx},theirs={t_idx}]",
                            base=None,
                            ours=o_shape,
                            theirs=t_shape,
                            reason="spatial_overlap_on_same_layer",
                        )
                    )


def _via_footprint_bbox(via: LayoutVia) -> BBox | None:
    """Compute a via's physical footprint bbox if explicit cut dimensions are present."""

    cw = via.params.get("cutWidth")
    ch = via.params.get("cutHeight")
    if not isinstance(cw, (int, float)) or not isinstance(ch, (int, float)):
        return None
    if isinstance(cw, bool) or isinstance(ch, bool) or cw <= 0 or ch <= 0:
        return None
    rows = via.params.get("cutRows", 1)
    cols = via.params.get("cutColumns", 1)
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 1:
        rows = 1
    if not isinstance(cols, int) or isinstance(cols, bool) or cols < 1:
        cols = 1
    spacing = via.params.get("cutSpacing", [0.0, 0.0])
    sx = float(spacing[0]) if isinstance(spacing, (list, tuple)) and len(spacing) >= 1 else 0.0
    sy = float(spacing[1]) if isinstance(spacing, (list, tuple)) and len(spacing) >= 2 else 0.0

    enc_x = 0.0
    enc_y = 0.0
    for key in ("layer1Enc", "layer2Enc"):
        enc = via.params.get(key)
        if isinstance(enc, (list, tuple)) and len(enc) >= 2:
            enc_x = max(enc_x, float(enc[0]))
            enc_y = max(enc_y, float(enc[1]))

    width = cols * float(cw) + max(0, cols - 1) * sx + 2.0 * enc_x
    height = rows * float(ch) + max(0, rows - 1) * sy + 2.0 * enc_y
    if via.orient in ("R90", "R270", "MXR90", "MYR90"):
        width, height = height, width
    ox, oy = via.origin
    return ((ox - width / 2.0, oy - height / 2.0), (ox + width / 2.0, oy + height / 2.0))


def _vias_spatially_overlap(o_via: LayoutVia, t_via: LayoutVia) -> bool:
    same_origin = (
        abs(o_via.origin[0] - t_via.origin[0]) <= _GEOM_EPS
        and abs(o_via.origin[1] - t_via.origin[1]) <= _GEOM_EPS
    )
    if same_origin:
        return True
    o_box = _via_footprint_bbox(o_via)
    t_box = _via_footprint_bbox(t_via)
    if o_box is not None and t_box is not None:
        return _bboxes_conflict(o_box, t_box, o_via.net, t_via.net)
    return False


def _detect_via_spatial_overlaps(
    base_vias: Sequence[LayoutVia],
    ours_vias: Sequence[LayoutVia],
    theirs_vias: Sequence[LayoutVia],
    conflicts: list[LayoutMergeConflict],
) -> None:
    ours_only, theirs_only = _branch_exclusive_additions(base_vias, ours_vias, theirs_vias)
    if not ours_only or not theirs_only:
        return

    o_by_def: dict[str, list[tuple[int, LayoutVia]]] = defaultdict(list)
    t_by_def: dict[str, list[tuple[int, LayoutVia]]] = defaultdict(list)
    for via in ours_only:
        o_by_def[via.via_def].append((len(o_by_def[via.via_def]), via))
    for via in theirs_only:
        t_by_def[via.via_def].append((len(t_by_def[via.via_def]), via))

    for via_def in sorted(set(o_by_def) & set(t_by_def)):
        for o_idx, o_via in o_by_def[via_def]:
            for t_idx, t_via in t_by_def[via_def]:
                if _strict_equal(o_via, t_via):
                    continue
                if not _vias_spatially_overlap(o_via, t_via):
                    continue
                conflicts.append(
                    LayoutMergeConflict(
                        domain="geometry",
                        path=(
                            f"vias.{via_def}@({o_via.origin[0]},{o_via.origin[1]})"
                            f"[ours={o_idx},theirs={t_idx}]"
                        ),
                        base=None,
                        ours=o_via,
                        theirs=t_via,
                        reason="spatial_overlap_via",
                    )
                )


# ---------------------------------------------------------------------------
# Public Merge Entry Point & Conflict Formatter
# ---------------------------------------------------------------------------


def _partition_objects(
    objects: Sequence[LayoutObject],
) -> dict[str, list[Any]]:
    parts: dict[str, list[Any]] = {
        "net": [],
        "pin": [],
        "instance": [],
        "mosaic": [],
        "via": [],
        "phys_shape": [],
        "aux_shape": [],
        "label": [],
        "blockage": [],
        "boundary": [],
        "fig_group": [],
    }
    for obj in objects:
        if isinstance(obj, LayoutNet):
            parts["net"].append(obj)
        elif isinstance(obj, LayoutPin):
            parts["pin"].append(obj)
        elif isinstance(obj, LayoutInstance):
            parts["instance"].append(obj)
        elif isinstance(obj, LayoutMosaic):
            parts["mosaic"].append(obj)
        elif isinstance(obj, LayoutVia):
            parts["via"].append(obj)
        elif is_layout_shape(obj):
            if is_auxiliary_shape(obj):
                parts["aux_shape"].append(obj)
            else:
                parts["phys_shape"].append(obj)
        elif isinstance(obj, LayoutLabel):
            parts["label"].append(obj)
        elif isinstance(obj, LayoutBlockage):
            parts["blockage"].append(obj)
        elif isinstance(obj, LayoutBoundary):
            parts["boundary"].append(obj)
        elif isinstance(obj, LayoutFigGroup):
            parts["fig_group"].append(obj)
        else:  # pragma: no cover
            raise SchemaValidationError(f"Unsupported layout object type: {type(obj).__name__}")
    return parts


def merge_layouts(
    base: LayoutSnapshot,
    ours: LayoutSnapshot,
    theirs: LayoutSnapshot,
    *,
    check_spatial_overlap: bool = True,
) -> LayoutMergeResult:
    """Three-way merge two layout descendants against a common base snapshot.

    Fail-Closed: returns ``LayoutMergeResult(ok=False, snapshot=None, conflicts=...)``
    whenever any conflict is detected.
    """

    if not (
        isinstance(base, LayoutSnapshot)
        and isinstance(ours, LayoutSnapshot)
        and isinstance(theirs, LayoutSnapshot)
    ):
        raise SchemaValidationError("base, ours, and theirs must be LayoutSnapshot instances")
    if type(check_spatial_overlap) is not bool:
        raise SchemaValidationError("check_spatial_overlap must be a bool")

    cellviews = (base.manifest.cellview, ours.manifest.cellview, theirs.manifest.cellview)
    if not (cellviews[0] == cellviews[1] == cellviews[2]):
        raise SchemaValidationError(
            "cannot merge layout snapshots with mismatched cellview identities: "
            + ", ".join(cv.key for cv in cellviews)
        )

    base_objects = canonicalize_layout_objects(base.objects)
    ours_objects = canonicalize_layout_objects(ours.objects)
    theirs_objects = canonicalize_layout_objects(theirs.objects)

    b_parts = _partition_objects(base_objects)
    o_parts = _partition_objects(ours_objects)
    t_parts = _partition_objects(theirs_objects)

    conflicts: list[LayoutMergeConflict] = []
    merged_objects: list[LayoutObject] = []

    # Keyed/named entities (field-level 3-way merge)
    merged_objects.extend(
        _merge_nets(b_parts["net"], o_parts["net"], t_parts["net"], conflicts)
    )
    merged_objects.extend(
        _merge_pins(b_parts["pin"], o_parts["pin"], t_parts["pin"], conflicts)
    )
    merged_objects.extend(
        _merge_instances(b_parts["instance"], o_parts["instance"], t_parts["instance"], conflicts)
    )
    merged_objects.extend(
        _merge_mosaics(b_parts["mosaic"], o_parts["mosaic"], t_parts["mosaic"], conflicts)
    )
    merged_objects.extend(
        _merge_boundaries(b_parts["boundary"], o_parts["boundary"], t_parts["boundary"], conflicts)
    )
    merged_objects.extend(
        _merge_fig_groups(
            b_parts["fig_group"], o_parts["fig_group"], t_parts["fig_group"], conflicts
        )
    )

    # Geometric/multiset entities
    merged_objects.extend(
        _merge_multiset(
            b_parts["via"],
            o_parts["via"],
            t_parts["via"],
            domain="geometry",
            path_prefix="vias",
            conflicts=conflicts,
        )
    )
    merged_objects.extend(
        _merge_multiset(
            b_parts["phys_shape"],
            o_parts["phys_shape"],
            t_parts["phys_shape"],
            domain="geometry",
            path_prefix="shapes",
            conflicts=conflicts,
        )
    )
    merged_objects.extend(
        _merge_multiset(
            b_parts["blockage"],
            o_parts["blockage"],
            t_parts["blockage"],
            domain="geometry",
            path_prefix="blockages",
            conflicts=conflicts,
        )
    )
    merged_objects.extend(
        _merge_multiset(
            b_parts["label"],
            o_parts["label"],
            t_parts["label"],
            domain="annotation",
            path_prefix="labels",
            conflicts=conflicts,
        )
    )
    merged_objects.extend(
        _merge_multiset(
            b_parts["aux_shape"],
            o_parts["aux_shape"],
            t_parts["aux_shape"],
            domain="annotation",
            path_prefix="aux_shapes",
            conflicts=conflicts,
        )
    )

    # Cell properties, metadata, omissions
    merged_cell_props = _merge_mapping(
        _normalize_cell_properties(base.cell_properties),
        _normalize_cell_properties(ours.cell_properties),
        _normalize_cell_properties(theirs.cell_properties),
        domain="connectivity",
        path_prefix="cell_properties",
        conflicts=conflicts,
    )
    merged_metadata = _merge_mapping(
        base.manifest.metadata,
        ours.manifest.metadata,
        theirs.manifest.metadata,
        domain="annotation",
        path_prefix="manifest.metadata",
        conflicts=conflicts,
    )
    merged_omissions: tuple[OmissionRecord, ...] = _merge_multiset(
        base.omissions,
        ours.omissions,
        theirs.omissions,
        domain="annotation",
        path_prefix="omissions",
        conflicts=conflicts,
    )

    # Spatial overlap conflict detection across branches
    if check_spatial_overlap:
        _detect_shape_spatial_overlaps(
            b_parts["phys_shape"],
            o_parts["phys_shape"],
            t_parts["phys_shape"],
            conflicts,
        )
        _detect_via_spatial_overlaps(
            b_parts["via"],
            o_parts["via"],
            t_parts["via"],
            conflicts,
        )

    sorted_conflicts = _sort_conflicts(conflicts)
    if sorted_conflicts:
        return LayoutMergeResult(ok=False, snapshot=None, conflicts=sorted_conflicts)

    snapshot = build_layout_snapshot(
        ours.manifest.cellview,
        tuple(merged_objects),
        cell_properties=merged_cell_props,
        omissions=merged_omissions,
        metadata=merged_metadata,
    )
    return LayoutMergeResult(ok=True, snapshot=snapshot, conflicts=())


def _format_bbox(bbox: BBox) -> str:
    (x0, y0), (x1, y1) = bbox
    return (
        f"(({quantize_layout_coord(x0)}, {quantize_layout_coord(y0)}), "
        f"({quantize_layout_coord(x1)}, {quantize_layout_coord(y1)}))"
    )


def _overlap_bbox(a: BBox, b: BBox) -> BBox | None:
    ix0 = max(a[0][0], b[0][0])
    iy0 = max(a[0][1], b[0][1])
    ix1 = min(a[1][0], b[1][0])
    iy1 = min(a[1][1], b[1][1])
    if ix1 < ix0 - _GEOM_EPS or iy1 < iy0 - _GEOM_EPS:
        return None
    return ((ix0, iy0), (max(ix0, ix1), max(iy0, iy1)))


def _format_net_tag(net: str | None) -> str:
    return repr(net) if net is not None else "<unnetted>"


def _format_spatial_shape(shape: LayoutShape) -> str:
    bbox = _shape_bbox(shape)
    return (
        f"{shape.obj_type} layer={shape.layer}:{shape.purpose} "
        f"net={_format_net_tag(shape.net)} bbox={_format_bbox(bbox)}"
    )


def _format_spatial_via(via: LayoutVia) -> str:
    rows = via.params.get("cutRows")
    cols = via.params.get("cutColumns")
    if rows is not None or cols is not None:
        r_str = "?" if rows is None else str(rows)
        c_str = "?" if cols is None else str(cols)
        cuts_part = f" cuts={r_str}x{c_str}"
    else:
        cuts_part = ""
    box = _via_footprint_bbox(via)
    bbox_part = f" bbox={_format_bbox(box)}" if box is not None else ""
    return (
        f"via via_def={via.via_def} origin={via.origin!r} orient={via.orient} "
        f"net={_format_net_tag(via.net)}{cuts_part}{bbox_part}"
    )


def format_layout_merge_conflicts(result: LayoutMergeResult) -> str:
    """Render layout merge conflicts in deterministic human-readable text."""

    if not result.conflicts:
        return "No merge conflicts."
    ordered = _sort_conflicts(result.conflicts)
    domain_counts = Counter(conflict.domain for conflict in ordered)
    breakdown = ", ".join(
        f"{domain_counts[domain]} {domain}" for domain in sorted(domain_counts)
    )
    lines = [f"Layout merge conflicts ({len(ordered)} total: {breakdown})"]
    for conflict in ordered:
        if (
            conflict.reason == "spatial_overlap_on_same_layer"
            and is_layout_shape(conflict.ours)
            and is_layout_shape(conflict.theirs)
        ):
            o_shape = conflict.ours
            t_shape = conflict.theirs
            o_box = _shape_bbox(o_shape)
            t_box = _shape_bbox(t_shape)
            inter = _overlap_bbox(o_box, t_box)
            meta_parts = [f"layer={o_shape.layer}:{o_shape.purpose}"]
            if inter is not None:
                meta_parts.append(f"overlap_bbox={_format_bbox(inter)}")
            if o_shape.net is not None or t_shape.net is not None:
                net_label = f"{_format_net_tag(o_shape.net)} vs {_format_net_tag(t_shape.net)}"
                if o_shape.net is not None and t_shape.net is not None and o_shape.net != t_shape.net:
                    net_label += " (DIFFERENT NETS)"
                meta_parts.append(f"nets={net_label}")
            lines.append(
                f"- [{conflict.domain}] {conflict.path}: {conflict.reason} "
                f"({', '.join(meta_parts)})"
            )
            lines.append(f"  ours:   {_format_spatial_shape(o_shape)}")
            lines.append(f"  theirs: {_format_spatial_shape(t_shape)}")
        elif (
            conflict.reason == "spatial_overlap_via"
            and isinstance(conflict.ours, LayoutVia)
            and isinstance(conflict.theirs, LayoutVia)
        ):
            o_via = conflict.ours
            t_via = conflict.theirs
            meta_parts = [f"via_def={o_via.via_def}"]
            o_box = _via_footprint_bbox(o_via)
            t_box = _via_footprint_bbox(t_via)
            if o_box is not None and t_box is not None:
                inter = _overlap_bbox(o_box, t_box)
                if inter is not None:
                    meta_parts.append(f"overlap_bbox={_format_bbox(inter)}")
            if o_via.net is not None or t_via.net is not None:
                net_label = f"{_format_net_tag(o_via.net)} vs {_format_net_tag(t_via.net)}"
                if o_via.net is not None and t_via.net is not None and o_via.net != t_via.net:
                    net_label += " (DIFFERENT NETS)"
                meta_parts.append(f"nets={net_label}")
            lines.append(
                f"- [{conflict.domain}] {conflict.path}: {conflict.reason} "
                f"({', '.join(meta_parts)})"
            )
            lines.append(f"  ours:   {_format_spatial_via(o_via)}")
            lines.append(f"  theirs: {_format_spatial_via(t_via)}")
        else:
            lines.append(
                f"- [{conflict.domain}] {conflict.path}: {conflict.reason}; "
                f"base={_to_plain(conflict.base)!r}, "
                f"ours={_to_plain(conflict.ours)!r}, "
                f"theirs={_to_plain(conflict.theirs)!r}"
            )
    return "\n".join(lines)
