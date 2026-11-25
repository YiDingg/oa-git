"""Deterministic semantic diffs for schematic snapshots.

The diff engine deliberately works on the domain objects from :mod:`oa_git.model`
only.  It does not read snapshot files or know anything about an OA backend.  The
electrical and placement portions are compared independently so that a visual
cleanup cannot be mistaken for an electrical change.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal, TypeVar

from .guards import explicit_net_names, is_anonymous_net
from .model import (
    AnnotationShape,
    CellViewRef,
    Instance,
    Net,
    NoteLabel,
    PinPlacement,
    Port,
    SchematicSnapshot,
    WireSegment,
)

__all__ = [
    "ParamChange",
    "ConnectivityChange",
    "MasterChange",
    "PortChange",
    "NetChange",
    "KeyValueChange",
    "InstanceMove",
    "PinChange",
    "ElectricalDiff",
    "PlacementDiff",
    "SchematicDiff",
    "diff_schematics",
    "format_diff_text",
]


@dataclass(frozen=True)
class ParamChange:
    instance: str
    param: str
    old: Any
    new: Any
    old_type: str | None = None
    new_type: str | None = None


@dataclass(frozen=True)
class ConnectivityChange:
    instance: str
    terminal: str
    old_net: str | None
    new_net: str | None


@dataclass(frozen=True)
class MasterChange:
    instance: str
    old_master: tuple[str, str, str]
    new_master: tuple[str, str, str]


@dataclass(frozen=True)
class PortChange:
    name: str
    change_type: Literal["added", "removed", "modified"]
    old_direction: str | None = None
    new_direction: str | None = None


@dataclass(frozen=True)
class NetChange:
    name: str
    change_type: Literal["added", "removed", "modified"]
    old_is_global: bool | None = None
    new_is_global: bool | None = None
    old_num_bits: int | None = None
    new_num_bits: int | None = None


@dataclass(frozen=True)
class KeyValueChange:
    scope: str
    key: str
    old: Any
    new: Any
    change_type: Literal["added", "removed", "modified"]


@dataclass(frozen=True)
class InstanceMove:
    name: str
    change_type: Literal["added", "removed", "moved"]
    old_xy: tuple[float, float] | None = None
    new_xy: tuple[float, float] | None = None
    old_orient: str | None = None
    new_orient: str | None = None


@dataclass(frozen=True)
class PinChange:
    key: str
    change_type: Literal["added", "removed", "modified"]
    old: PinPlacement | None = None
    new: PinPlacement | None = None


@dataclass(frozen=True)
class ElectricalDiff:
    """All changes that can affect schematic electrical truth."""

    ports: tuple[PortChange, ...] = ()
    nets: tuple[NetChange, ...] = ()
    instances_added: tuple[str, ...] = ()
    instances_removed: tuple[str, ...] = ()
    master_changes: tuple[MasterChange, ...] = ()
    param_changes: tuple[ParamChange, ...] = ()
    derived_param_changes: tuple[ParamChange, ...] = ()
    connectivity_changes: tuple[ConnectivityChange, ...] = ()
    instance_prop_changes: tuple[KeyValueChange, ...] = ()
    property_changes: tuple[KeyValueChange, ...] = ()
    constraint_changes: tuple[KeyValueChange, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "ports",
            "nets",
            "instances_added",
            "instances_removed",
            "master_changes",
            "param_changes",
            "derived_param_changes",
            "connectivity_changes",
            "instance_prop_changes",
            "property_changes",
            "constraint_changes",
        ):
            values = tuple(getattr(self, field_name))
            object.__setattr__(self, field_name, tuple(sorted(values, key=_stable_key)))

    @property
    def derived_observation_changes(self) -> tuple[ParamChange, ...]:
        """Alias exposing derived callback observation diffs separately from electrical truth."""

        return self.derived_param_changes

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.ports,
                self.nets,
                self.instances_added,
                self.instances_removed,
                self.master_changes,
                self.param_changes,
                self.connectivity_changes,
                self.instance_prop_changes,
                self.property_changes,
                self.constraint_changes,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass(frozen=True)
class PlacementDiff:
    """All changes that affect schematic placement or visual geometry."""

    instance_moves: tuple[InstanceMove, ...] = ()
    pin_changes: tuple[PinChange, ...] = ()
    wires_added: tuple[WireSegment, ...] = ()
    wires_removed: tuple[WireSegment, ...] = ()
    labels_added: tuple[NoteLabel, ...] = ()
    labels_removed: tuple[NoteLabel, ...] = ()
    sheet_changes: tuple[KeyValueChange, ...] = ()
    annotations_added: tuple[AnnotationShape, ...] = ()
    annotations_removed: tuple[AnnotationShape, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "instance_moves",
            "pin_changes",
            "wires_added",
            "wires_removed",
            "labels_added",
            "labels_removed",
            "sheet_changes",
            "annotations_added",
            "annotations_removed",
        ):
            values = tuple(getattr(self, field_name))
            object.__setattr__(self, field_name, tuple(sorted(values, key=_stable_key)))

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.instance_moves,
                self.pin_changes,
                self.wires_added,
                self.wires_removed,
                self.labels_added,
                self.labels_removed,
                self.sheet_changes,
                self.annotations_added,
                self.annotations_removed,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return _to_plain(self)


@dataclass
class SchematicDiff:
    old_cellview: CellViewRef
    new_cellview: CellViewRef
    electrical: ElectricalDiff
    placement: PlacementDiff

    @property
    def derived_observation_changes(self) -> tuple[ParamChange, ...]:
        return self.electrical.derived_param_changes

    @property
    def has_derived_changes(self) -> bool:
        return bool(self.electrical.derived_param_changes)

    @property
    def has_electrical_changes(self) -> bool:
        return not self.electrical.is_empty

    @property
    def has_placement_changes(self) -> bool:
        return not self.placement.is_empty

    @property
    def cellview_changed(self) -> bool:
        return self.old_cellview != self.new_cellview

    @property
    def is_placement_only(self) -> bool:
        return self.has_placement_changes and not self.has_electrical_changes

    @property
    def is_empty(self) -> bool:
        return (
            not self.cellview_changed
            and self.electrical.is_empty
            and self.placement.is_empty
        )

    def summary(self) -> dict[str, int]:
        """Return deterministic counts for every diff category."""

        counts = {
            "ports": len(self.electrical.ports),
            "nets": len(self.electrical.nets),
            "instances_added": len(self.electrical.instances_added),
            "instances_removed": len(self.electrical.instances_removed),
            "master_changes": len(self.electrical.master_changes),
            "param_changes": len(self.electrical.param_changes),
            "derived_param_changes": len(self.electrical.derived_param_changes),
            "connectivity_changes": len(self.electrical.connectivity_changes),
            "instance_prop_changes": len(self.electrical.instance_prop_changes),
            "property_changes": len(self.electrical.property_changes),
            "constraint_changes": len(self.electrical.constraint_changes),
            "instance_moves": len(self.placement.instance_moves),
            "pin_changes": len(self.placement.pin_changes),
            "wires_added": len(self.placement.wires_added),
            "wires_removed": len(self.placement.wires_removed),
            "labels_added": len(self.placement.labels_added),
            "labels_removed": len(self.placement.labels_removed),
            "sheet_changes": len(self.placement.sheet_changes),
        }
        if self.placement.annotations_added or self.placement.annotations_removed:
            counts["annotations_added"] = len(self.placement.annotations_added)
            counts["annotations_removed"] = len(self.placement.annotations_removed)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_cellview": self.old_cellview.to_dict(),
            "new_cellview": self.new_cellview.to_dict(),
            "cellview_changed": self.cellview_changed,
            "electrical": self.electrical.to_dict(),
            "derived_observation_changes": _to_plain(self.electrical.derived_param_changes),
            "placement": self.placement.to_dict(),
        }


_T = TypeVar("_T")


def _strict_equal(left: Any, right: Any) -> bool:
    """Compare values without Python's bool/int equivalence.

    Snapshot maps can contain nested values in constraints and properties, so the
    check is recursive.  Exact runtime types make ``True`` different from ``1``
    and also preserve distinctions such as integer versus floating-point CDF data.
    """

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
    """Build a total, type-aware ordering key for snapshot values."""

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
        return ("set", tuple(sorted((_stable_key(item) for item in value))))
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
    """Convert dataclasses and tuples to deterministic JSON-compatible values."""

    if is_dataclass(value):
        return {
            item.name: _to_plain(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            key: _to_plain(value[key])
            for key in sorted(value, key=_stable_key)
        }
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_to_plain(item) for item in sorted(value, key=_stable_key)]
    return value


def _sorted_union(*mappings: Mapping[str, Any]) -> tuple[str, ...]:
    keys: set[str] = set()
    for mapping in mappings:
        keys.update(mapping)
    return tuple(sorted(keys))


def _by_name(items: Sequence[_T]) -> dict[str, _T]:
    """Index canonical named objects while keeping comparison deterministic."""

    return {getattr(item, "name"): item for item in items}


def _pin_groups(items: Sequence[PinPlacement]) -> dict[str, list[PinPlacement]]:
    grouped: dict[str, list[PinPlacement]] = defaultdict(list)
    for item in items:
        grouped[item.name].append(item)
    for values in grouped.values():
        values.sort(key=_stable_key)
    return grouped


def _pin_occurrence_pairs(
    old: Sequence[PinPlacement], new: Sequence[PinPlacement]
) -> tuple[tuple[PinPlacement | None, PinPlacement | None], ...]:
    """Pair duplicate pins by exact identity before pairing changed occurrences."""

    old_values = sorted(old, key=_stable_key)
    new_remaining = sorted(new, key=_stable_key)
    pairs: list[tuple[PinPlacement | None, PinPlacement | None]] = []
    for old_item in old_values:
        exact_index = next(
            (
                index
                for index, new_item in enumerate(new_remaining)
                if _strict_equal(old_item, new_item)
            ),
            None,
        )
        if exact_index is None:
            pairs.append((old_item, None))
        else:
            pairs.append((old_item, new_remaining.pop(exact_index)))
    for index, (old_item, new_item) in enumerate(pairs):
        if new_item is None and new_remaining:
            pairs[index] = (old_item, new_remaining.pop(0))
    pairs.extend((None, new_item) for new_item in new_remaining)
    return tuple(pairs)


def _master(instance: Instance) -> tuple[str, str, str]:
    return (instance.lib, instance.cell, instance.view)


def _diff_ports(old: Sequence[Port], new: Sequence[Port]) -> tuple[PortChange, ...]:
    old_by_name = _by_name(old)
    new_by_name = _by_name(new)
    changes: list[PortChange] = []
    for name in sorted(set(old_by_name) | set(new_by_name)):
        old_port = old_by_name.get(name)
        new_port = new_by_name.get(name)
        if old_port is None:
            changes.append(PortChange(name, "added", None, new_port.direction))
        elif new_port is None:
            changes.append(PortChange(name, "removed", old_port.direction, None))
        elif not _strict_equal(old_port.direction, new_port.direction):
            changes.append(
                PortChange(name, "modified", old_port.direction, new_port.direction)
            )
    return tuple(changes)


def _compute_anon_bijection(
    old: SchematicSnapshot, new: SchematicSnapshot
) -> dict[str, str]:
    """Match anonymous auto-nets that have identical connected terminal sets."""

    old_explicit = explicit_net_names(old)
    new_explicit = explicit_net_names(new)
    old_classes: dict[str, set[tuple[str, str]]] = defaultdict(set)
    new_classes: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for instance in old.electrical.instances:
        for terminal, net_name in instance.connections.items():
            if is_anonymous_net(net_name, old_explicit):
                old_classes[net_name].add((instance.name, terminal))
    for instance in new.electrical.instances:
        for terminal, net_name in instance.connections.items():
            if is_anonymous_net(net_name, new_explicit):
                new_classes[net_name].add((instance.name, terminal))

    new_by_terms = {
        frozenset(terms): net_name
        for net_name, terms in new_classes.items()
        if terms
    }
    bijection: dict[str, str] = {}
    for old_net, terms in sorted(old_classes.items()):
        key = frozenset(terms)
        if key and key in new_by_terms:
            bijection[old_net] = new_by_terms[key]
    return bijection


def _effective_derived_values(instance: Instance) -> Mapping[str, Any]:
    derived = instance.derived
    if derived is not None:
        if (
            derived.source == "legacy-import"
            and derived.status == "valid"
            and derived.input_sha256 is None
            and derived.callback_order == ()
            and instance.derived_params != derived.values
        ):
            return instance.derived_params
        return derived.values
    return instance.derived_params


def _diff_nets(
    old: Sequence[Net],
    new: Sequence[Net],
    anon_bijection: Mapping[str, str] = {},
) -> tuple[NetChange, ...]:
    old_by_name = _by_name(old)
    new_by_name = _by_name(new)
    mapped_old = set(anon_bijection.keys())
    mapped_new = set(anon_bijection.values())
    changes: list[NetChange] = []

    for old_name, new_name in sorted(anon_bijection.items()):
        old_net = old_by_name.get(old_name)
        new_net = new_by_name.get(new_name)
        if old_net is None and new_net is None:
            continue
        if old_net is None:
            changes.append(
                NetChange(
                    new_name,
                    "added",
                    None,
                    new_net.is_global,
                    None,
                    new_net.num_bits,
                )
            )
        elif new_net is None:
            changes.append(
                NetChange(
                    old_name,
                    "removed",
                    old_net.is_global,
                    None,
                    old_net.num_bits,
                    None,
                )
            )
        elif not _strict_equal(old_net.is_global, new_net.is_global) or not _strict_equal(
            old_net.num_bits, new_net.num_bits
        ):
            changes.append(
                NetChange(
                    old_name,
                    "modified",
                    old_net.is_global,
                    new_net.is_global,
                    old_net.num_bits,
                    new_net.num_bits,
                )
            )

    unmapped_old = {k: v for k, v in old_by_name.items() if k not in mapped_old}
    unmapped_new = {k: v for k, v in new_by_name.items() if k not in mapped_new}
    for name in sorted(set(unmapped_old) | set(unmapped_new)):
        old_net = unmapped_old.get(name)
        new_net = unmapped_new.get(name)
        if old_net is None:
            changes.append(
                NetChange(
                    name,
                    "added",
                    None,
                    new_net.is_global,
                    None,
                    new_net.num_bits,
                )
            )
        elif new_net is None:
            changes.append(
                NetChange(
                    name,
                    "removed",
                    old_net.is_global,
                    None,
                    old_net.num_bits,
                    None,
                )
            )
        elif not _strict_equal(old_net.is_global, new_net.is_global) or not _strict_equal(
            old_net.num_bits, new_net.num_bits
        ):
            changes.append(
                NetChange(
                    name,
                    "modified",
                    old_net.is_global,
                    new_net.is_global,
                    old_net.num_bits,
                    new_net.num_bits,
                )
            )
    return tuple(changes)


def _diff_params(
    instance: str,
    old_values: Mapping[str, Any],
    new_values: Mapping[str, Any],
    old_types: Mapping[str, str] | None = None,
    new_types: Mapping[str, str] | None = None,
) -> tuple[ParamChange, ...]:
    old_types = old_types or {}
    new_types = new_types or {}
    changes: list[ParamChange] = []
    for name in _sorted_union(old_values, new_values, old_types, new_types):
        old_present = name in old_values
        new_present = name in new_values
        old_value = old_values[name] if old_present else None
        new_value = new_values[name] if new_present else None
        old_type = old_types.get(name)
        new_type = new_types.get(name)
        if (
            old_present == new_present
            and _strict_equal(old_value, new_value)
            and _strict_equal(old_type, new_type)
        ):
            continue
        changes.append(
            ParamChange(
                instance=instance,
                param=name,
                old=old_value,
                new=new_value,
                old_type=old_type,
                new_type=new_type,
            )
        )
    return tuple(changes)


def _diff_connections(
    instance: str,
    old: Mapping[str, str],
    new: Mapping[str, str],
    anon_bijection: Mapping[str, str] = {},
) -> tuple[ConnectivityChange, ...]:
    changes: list[ConnectivityChange] = []
    for terminal in _sorted_union(old, new):
        old_present = terminal in old
        new_present = terminal in new
        old_net = old[terminal] if old_present else None
        new_net = new[terminal] if new_present else None
        if old_present and new_present:
            if _strict_equal(old_net, new_net):
                continue
            if old_net is not None and anon_bijection.get(old_net) == new_net:
                continue
        changes.append(ConnectivityChange(instance, terminal, old_net, new_net))
    return tuple(changes)


def _diff_mapping(
    scope: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
) -> tuple[KeyValueChange, ...]:
    changes: list[KeyValueChange] = []
    for key in _sorted_union(old, new):
        old_present = key in old
        new_present = key in new
        old_value = old[key] if old_present else None
        new_value = new[key] if new_present else None
        if old_present and not new_present:
            change_type: Literal["added", "removed", "modified"] = "removed"
        elif new_present and not old_present:
            change_type = "added"
        elif _strict_equal(old_value, new_value):
            continue
        else:
            change_type = "modified"
        changes.append(KeyValueChange(scope, key, old_value, new_value, change_type))
    return tuple(changes)


def _diff_electrical(
    old: SchematicSnapshot,
    new: SchematicSnapshot,
    anon_bijection: Mapping[str, str] = {},
) -> ElectricalDiff:
    old_electrical = old.electrical
    new_electrical = new.electrical
    old_instances = _by_name(old_electrical.instances)
    new_instances = _by_name(new_electrical.instances)

    instance_names = set(old_instances) | set(new_instances)
    instances_added = tuple(sorted(set(new_instances) - set(old_instances)))
    instances_removed = tuple(sorted(set(old_instances) - set(new_instances)))

    master_changes: list[MasterChange] = []
    param_changes: list[ParamChange] = []
    derived_param_changes: list[ParamChange] = []
    connectivity_changes: list[ConnectivityChange] = []
    instance_prop_changes: list[KeyValueChange] = []
    for name in sorted(instance_names & set(old_instances) & set(new_instances)):
        old_instance = old_instances[name]
        new_instance = new_instances[name]
        old_master = _master(old_instance)
        new_master = _master(new_instance)
        if not _strict_equal(old_master, new_master):
            master_changes.append(MasterChange(name, old_master, new_master))
        param_changes.extend(
            _diff_params(
                name,
                old_instance.params,
                new_instance.params,
                old_instance.param_types,
                new_instance.param_types,
            )
        )
        derived_param_changes.extend(
            _diff_params(
                name,
                _effective_derived_values(old_instance),
                _effective_derived_values(new_instance),
            )
        )
        connectivity_changes.extend(
            _diff_connections(
                name, old_instance.connections, new_instance.connections, anon_bijection
            )
        )
        instance_prop_changes.extend(
            _diff_mapping(name, old_instance.props, new_instance.props)
        )

    return ElectricalDiff(
        ports=_diff_ports(old_electrical.ports, new_electrical.ports),
        nets=_diff_nets(old_electrical.nets, new_electrical.nets, anon_bijection),
        instances_added=instances_added,
        instances_removed=instances_removed,
        master_changes=tuple(master_changes),
        param_changes=tuple(param_changes),
        derived_param_changes=tuple(derived_param_changes),
        connectivity_changes=tuple(connectivity_changes),
        instance_prop_changes=tuple(instance_prop_changes),
        property_changes=_diff_mapping(
            "cellview", old_electrical.properties, new_electrical.properties
        ),
        constraint_changes=_diff_mapping(
            "constraints", old_electrical.constraints, new_electrical.constraints
        ),
    )


def _multiset_diff(
    old: Sequence[_T], new: Sequence[_T]
) -> tuple[tuple[_T, ...], tuple[_T, ...]]:
    """Return ``(added, removed)`` for possibly repeated geometry objects."""

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


def _wire_anon_key(
    wire: WireSegment,
    *,
    side: Literal["old", "new"],
    anon_bijection: Mapping[str, str],
    reverse_bijection: Mapping[str, str],
) -> tuple[Any, ...]:
    if side == "old" and wire.net is not None and wire.net in anon_bijection:
        net_key: Any = ("__anon_pair__", wire.net, anon_bijection[wire.net])
    elif side == "new" and wire.net is not None and wire.net in reverse_bijection:
        net_key = ("__anon_pair__", reverse_bijection[wire.net], wire.net)
    else:
        net_key = ("__literal__", wire.net)
    return (
        _stable_key(wire.points),
        _stable_key(net_key),
        _stable_key(wire.width),
        _stable_key(wire.route_method),
        _stable_key(wire.labels),
    )


def _multiset_diff_wires(
    old: Sequence[WireSegment],
    new: Sequence[WireSegment],
    anon_bijection: Mapping[str, str],
) -> tuple[tuple[WireSegment, ...], tuple[WireSegment, ...]]:
    if not anon_bijection:
        return _multiset_diff(old, new)
    reverse_bijection = {v: k for k, v in anon_bijection.items()}
    old_groups: dict[tuple[Any, ...], list[WireSegment]] = defaultdict(list)
    new_groups: dict[tuple[Any, ...], list[WireSegment]] = defaultdict(list)
    for wire in old:
        old_groups[
            _wire_anon_key(
                wire,
                side="old",
                anon_bijection=anon_bijection,
                reverse_bijection=reverse_bijection,
            )
        ].append(wire)
    for wire in new:
        new_groups[
            _wire_anon_key(
                wire,
                side="new",
                anon_bijection=anon_bijection,
                reverse_bijection=reverse_bijection,
            )
        ].append(wire)

    added: list[WireSegment] = []
    removed: list[WireSegment] = []
    for key in sorted(set(old_groups) | set(new_groups)):
        old_items = sorted(old_groups.get(key, []), key=_stable_key)
        new_items = sorted(new_groups.get(key, []), key=_stable_key)
        common = min(len(old_items), len(new_items))
        removed.extend(old_items[common:])
        added.extend(new_items[common:])
    return tuple(added), tuple(removed)


def _diff_placement(
    old: SchematicSnapshot,
    new: SchematicSnapshot,
    anon_bijection: Mapping[str, str] = {},
) -> PlacementDiff:
    old_placements = _by_name(old.placement.instances)
    new_placements = _by_name(new.placement.instances)
    instance_moves: list[InstanceMove] = []
    for name in sorted(set(old_placements) | set(new_placements)):
        old_placement = old_placements.get(name)
        new_placement = new_placements.get(name)
        if old_placement is None:
            instance_moves.append(
                InstanceMove(
                    name=name,
                    change_type="added",
                    new_xy=new_placement.xy,
                    new_orient=new_placement.orient,
                )
            )
        elif new_placement is None:
            instance_moves.append(
                InstanceMove(
                    name=name,
                    change_type="removed",
                    old_xy=old_placement.xy,
                    old_orient=old_placement.orient,
                )
            )
        elif not _strict_equal(old_placement.xy, new_placement.xy) or not _strict_equal(
            old_placement.orient, new_placement.orient
        ):
            instance_moves.append(
                InstanceMove(
                    name=name,
                    change_type="moved",
                    old_xy=old_placement.xy,
                    new_xy=new_placement.xy,
                    old_orient=old_placement.orient,
                    new_orient=new_placement.orient,
                )
            )

    old_groups = _pin_groups(old.placement.pins)
    new_groups = _pin_groups(new.placement.pins)
    pin_changes: list[PinChange] = []
    for name in sorted(set(old_groups) | set(new_groups)):
        pairs = _pin_occurrence_pairs(old_groups.get(name, ()), new_groups.get(name, ()))
        multiple = len(pairs) > 1
        for index, (old_pin, new_pin) in enumerate(pairs):
            key = name if not multiple else f"{name}[{index}]"
            if old_pin is None:
                pin_changes.append(PinChange(key, "added", None, new_pin))
            elif new_pin is None:
                pin_changes.append(PinChange(key, "removed", old_pin, None))
            elif not _strict_equal(old_pin, new_pin):
                pin_changes.append(PinChange(key, "modified", old_pin, new_pin))

    wires_added, wires_removed = _multiset_diff_wires(
        old.placement.wires, new.placement.wires, anon_bijection
    )
    labels_added, labels_removed = _multiset_diff(
        old.placement.labels, new.placement.labels
    )
    annotations_added, annotations_removed = _multiset_diff(
        old.placement.annotations, new.placement.annotations
    )
    return PlacementDiff(
        instance_moves=tuple(instance_moves),
        pin_changes=tuple(pin_changes),
        wires_added=wires_added,
        wires_removed=wires_removed,
        labels_added=labels_added,
        labels_removed=labels_removed,
        sheet_changes=_diff_mapping("sheet", old.placement.sheet, new.placement.sheet),
        annotations_added=annotations_added,
        annotations_removed=annotations_removed,
    )


def diff_schematics(old: SchematicSnapshot, new: SchematicSnapshot) -> SchematicDiff:
    """Compare two schematic snapshots with electrical/placement separation."""

    anon_bijection = _compute_anon_bijection(old, new)
    return SchematicDiff(
        old_cellview=old.manifest.cellview,
        new_cellview=new.manifest.cellview,
        electrical=_diff_electrical(old, new, anon_bijection),
        placement=_diff_placement(old, new, anon_bijection),
    )


def _format_value(value: Any) -> str:
    return repr(_to_plain(value))


def _fmt_coord(value: float) -> str:
    normalized = 0.0 if value == 0.0 else value
    return f"{normalized:g}"


def _fmt_xy(xy: tuple[float, float] | None) -> str:
    if xy is None:
        return "none"
    return f"({_fmt_coord(xy[0])}, {_fmt_coord(xy[1])})"


def _fmt_master(master: tuple[str, str, str]) -> str:
    return f"{master[0]}/{master[1]}/{master[2]}"


def _fmt_pin(pin: PinPlacement) -> str:
    return (
        f"net={pin.net} dir={pin.direction} "
        f"master={pin.lib}/{pin.cell}/{pin.view} "
        f"at {_fmt_xy(pin.xy)} {pin.orient}"
    )


def _fmt_wire(wire: WireSegment) -> str:
    net_part = f"net={wire.net} " if wire.net is not None else ""
    pts_part = " -> ".join(_fmt_xy(p) for p in wire.points)
    extras: list[str] = []
    if wire.width:
        extras.append(f"width={_fmt_coord(wire.width)}")
    if wire.route_method != "full":
        extras.append(f"route={wire.route_method}")
    if wire.labels:
        extras.append("labels=" + ", ".join(repr(lbl.text) for lbl in wire.labels))
    extra_text = f" ({', '.join(extras)})" if extras else ""
    return f"wire {net_part}[{pts_part}]{extra_text}"


def _fmt_note_label(label: NoteLabel) -> str:
    return f"label {label.text!r} at {_fmt_xy(label.xy)} ({label.layer}:{label.purpose}, {label.orient})"


def _fmt_annotation(shape: AnnotationShape) -> str:
    pts_part = ", ".join(_fmt_xy(p) for p in shape.points)
    width_part = f", width={_fmt_coord(shape.width)}" if shape.width else ""
    return f"{shape.kind} {shape.layer}:{shape.purpose} [{pts_part}]{width_part}"


def _append_change_lines(lines: list[str], heading: str, entries: Sequence[Any]) -> None:
    if not entries:
        return
    lines.append(f"  {heading}")
    for entry in entries:
        if isinstance(entry, PortChange):
            details = f"{entry.name} ({entry.change_type})"
            if entry.change_type == "modified":
                details += f" {entry.old_direction!r} -> {entry.new_direction!r}"
            elif entry.change_type == "added":
                details += f" {entry.new_direction!r}"
            else:
                details += f" {entry.old_direction!r}"
        elif isinstance(entry, NetChange):
            details = f"{entry.name} ({entry.change_type})"
            if entry.change_type == "added":
                attrs: list[str] = []
                if entry.new_is_global:
                    attrs.append("global")
                if entry.new_num_bits is not None:
                    attrs.append(f"bits={entry.new_num_bits}")
                if attrs:
                    details += " " + ", ".join(attrs)
            elif entry.change_type == "removed":
                attrs = []
                if entry.old_is_global:
                    attrs.append("global")
                if entry.old_num_bits is not None:
                    attrs.append(f"bits={entry.old_num_bits}")
                if attrs:
                    details += " " + ", ".join(attrs)
            else:
                parts: list[str] = []
                if entry.old_is_global != entry.new_is_global:
                    parts.append(f"global {entry.old_is_global!r} -> {entry.new_is_global!r}")
                if entry.old_num_bits != entry.new_num_bits:
                    parts.append(f"bits {entry.old_num_bits!r} -> {entry.new_num_bits!r}")
                if parts:
                    details += " " + ", ".join(parts)
        elif isinstance(entry, MasterChange):
            details = f"{entry.instance} {_fmt_master(entry.old_master)} -> {_fmt_master(entry.new_master)}"
        elif isinstance(entry, ParamChange):
            if entry.old is None and entry.old_type is None and entry.new is not None:
                type_suffix = f" ({entry.new_type})" if entry.new_type is not None else ""
                details = f"{entry.instance}.{entry.param} (added) = {_format_value(entry.new)}{type_suffix}"
            elif entry.new is None and entry.new_type is None and entry.old is not None:
                type_suffix = f" ({entry.old_type})" if entry.old_type is not None else ""
                details = f"{entry.instance}.{entry.param} (removed) = {_format_value(entry.old)}{type_suffix}"
            else:
                details = f"{entry.instance}.{entry.param} {_format_value(entry.old)} -> {_format_value(entry.new)}"
                if entry.old_type != entry.new_type:
                    details += f" ({entry.old_type!r} -> {entry.new_type!r})"
        elif isinstance(entry, ConnectivityChange):
            if entry.old_net is None:
                details = f"{entry.instance}.{entry.terminal} (connected) -> {entry.new_net}"
            elif entry.new_net is None:
                details = f"{entry.instance}.{entry.terminal} (disconnected) {entry.old_net}"
            else:
                details = f"{entry.instance}.{entry.terminal} {entry.old_net} -> {entry.new_net}"
        elif isinstance(entry, KeyValueChange):
            if entry.change_type == "added":
                details = f"{entry.scope}.{entry.key} (added) = {_format_value(entry.new)}"
            elif entry.change_type == "removed":
                details = f"{entry.scope}.{entry.key} (removed) = {_format_value(entry.old)}"
            else:
                details = f"{entry.scope}.{entry.key} (modified) {_format_value(entry.old)} -> {_format_value(entry.new)}"
        elif isinstance(entry, InstanceMove):
            details = f"{entry.name} ({entry.change_type})"
            if entry.change_type == "added":
                orient = f" {entry.new_orient}" if entry.new_orient is not None else ""
                details += f" at {_fmt_xy(entry.new_xy)}{orient}"
            elif entry.change_type == "removed":
                orient = f" {entry.old_orient}" if entry.old_orient is not None else ""
                details += f" from {_fmt_xy(entry.old_xy)}{orient}"
            else:
                parts = []
                if entry.old_xy != entry.new_xy:
                    parts.append(f"{_fmt_xy(entry.old_xy)} -> {_fmt_xy(entry.new_xy)}")
                if entry.old_orient != entry.new_orient:
                    parts.append(f"orient {entry.old_orient} -> {entry.new_orient}")
                if parts:
                    details += " " + ", ".join(parts)
        elif isinstance(entry, PinChange):
            details = f"{entry.key} ({entry.change_type})"
            if entry.change_type == "modified" and entry.old is not None and entry.new is not None:
                parts = []
                if entry.old.net != entry.new.net:
                    parts.append(f"net {entry.old.net} -> {entry.new.net}")
                if entry.old.direction != entry.new.direction:
                    parts.append(f"dir {entry.old.direction} -> {entry.new.direction}")
                if (entry.old.lib, entry.old.cell, entry.old.view) != (
                    entry.new.lib,
                    entry.new.cell,
                    entry.new.view,
                ):
                    parts.append(
                        f"master {entry.old.lib}/{entry.old.cell}/{entry.old.view} -> "
                        f"{entry.new.lib}/{entry.new.cell}/{entry.new.view}"
                    )
                if entry.old.xy != entry.new.xy:
                    parts.append(f"xy {_fmt_xy(entry.old.xy)} -> {_fmt_xy(entry.new.xy)}")
                if entry.old.orient != entry.new.orient:
                    parts.append(f"orient {entry.old.orient} -> {entry.new.orient}")
                if parts:
                    details += " " + ", ".join(parts)
                else:
                    details += f" {_fmt_pin(entry.old)} -> {_fmt_pin(entry.new)}"
            elif entry.change_type == "added" and entry.new is not None:
                details += f" {_fmt_pin(entry.new)}"
            elif entry.old is not None:
                details += f" {_fmt_pin(entry.old)}"
        elif isinstance(entry, WireSegment):
            details = _fmt_wire(entry)
        elif isinstance(entry, NoteLabel):
            details = _fmt_note_label(entry)
        elif isinstance(entry, AnnotationShape):
            details = _fmt_annotation(entry)
        elif isinstance(entry, str):
            details = entry
        else:
            details = _format_value(entry)
        lines.append(f"    {details}")


def format_diff_text(diff: SchematicDiff, *, electrical_only: bool = False) -> str:
    """Render a deterministic, scannable text representation of a diff."""

    lines = [f"{diff.old_cellview.key} -> {diff.new_cellview.key}"]
    lines.append("Electrical changes")
    electrical = diff.electrical
    _append_change_lines(lines, "Ports", electrical.ports)
    _append_change_lines(lines, "Nets", electrical.nets)
    _append_change_lines(lines, "Instances added", electrical.instances_added)
    _append_change_lines(lines, "Instances removed", electrical.instances_removed)
    _append_change_lines(lines, "Master changes", electrical.master_changes)
    _append_change_lines(lines, "Parameter changes", electrical.param_changes)
    _append_change_lines(lines, "Connectivity changes", electrical.connectivity_changes)
    _append_change_lines(lines, "Instance property changes", electrical.instance_prop_changes)
    _append_change_lines(lines, "Cellview property changes", electrical.property_changes)
    _append_change_lines(lines, "Constraint changes", electrical.constraint_changes)
    if electrical.is_empty:
        lines.append("  none")

    if electrical.derived_param_changes:
        lines.append("Derived observation changes")
        _append_change_lines(lines, "Derived parameters", electrical.derived_param_changes)

    if not electrical_only:
        lines.append("Placement changes")
        placement = diff.placement
        _append_change_lines(lines, "Instance moves", placement.instance_moves)
        _append_change_lines(lines, "Pin changes", placement.pin_changes)
        _append_change_lines(lines, "Wires added", placement.wires_added)
        _append_change_lines(lines, "Wires removed", placement.wires_removed)
        _append_change_lines(lines, "Labels added", placement.labels_added)
        _append_change_lines(lines, "Labels removed", placement.labels_removed)
        _append_change_lines(lines, "Annotations added", placement.annotations_added)
        _append_change_lines(lines, "Annotations removed", placement.annotations_removed)
        _append_change_lines(lines, "Sheet changes", placement.sheet_changes)
        if placement.is_empty:
            lines.append("  none")
    return "\n".join(lines)
