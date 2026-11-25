"""Deterministic object-level three-way merge for schematic snapshots.

The merge engine operates entirely on the immutable domain objects from
``oa_git.model``.  Electrical and placement data are merged independently, and
all comparisons use strict runtime types so that values such as ``True`` and
``1`` remain distinct.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal

from .model import (
    VALID_CDF_TYPES,
    AnnotationShape,
    CdfCouplingRule,
    DerivedState,
    Instance,
    InstancePlacement,
    Net,
    NoteLabel,
    OmissionRecord,
    PinPlacement,
    Port,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    SchemaValidationError,
    WireSegment,
)
from .snapshot import (
    build_schematic_snapshot,
    canonicalize_electrical,
    canonicalize_placement,
    compute_cdf_input_sha256,
    resolve_cdf_callback_order,
)

__all__ = [
    "MergeConflict",
    "MergeResult",
    "merge_schematics",
    "format_merge_conflicts",
]


@dataclass(frozen=True)
class MergeConflict:
    """A single deterministic conflict in the electrical or placement domain."""

    domain: Literal["electrical", "placement"]
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
class MergeResult:
    """Result of merging three snapshots."""

    ok: bool
    snapshot: SchematicSnapshot | None
    conflicts: tuple[MergeConflict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot": _to_plain(self.snapshot),
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


_MISSING = object()


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
            _strict_equal(getattr(left, field.name), getattr(right, field.name))
            for field in fields(left)
        )
    return left == right


def _stable_key(value: Any) -> tuple[Any, ...]:
    """Build a total, type-aware key for deterministic ordering."""

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
                (field.name, _stable_key(getattr(value, field.name)))
                for field in fields(value)
            ),
        )
    return ("object", type(value).__module__, type(value).__qualname__, repr(value))


def _to_plain(value: Any) -> Any:
    """Convert domain values into deterministic JSON-compatible values."""

    if value is _MISSING:
        return None
    if is_dataclass(value):
        return {
            field.name: _to_plain(getattr(value, field.name))
            for field in fields(value)
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


def _key_text(value: Any) -> str:
    """Render a stable key for conflict paths and human output."""

    return repr(_to_plain(value))


def _conflict_value(value: Any) -> Any:
    """Preserve domain objects while representing an absent slot as ``None``."""

    return None if value is _MISSING else value


def _sort_conflicts(conflicts: Sequence[MergeConflict]) -> tuple[MergeConflict, ...]:
    return tuple(sorted(conflicts, key=lambda item: (item.domain, item.path, item.reason)))


def _merge_atom(
    base: Any,
    ours: Any,
    theirs: Any,
    *,
    domain: Literal["electrical", "placement"],
    path: str,
    conflicts: list[MergeConflict],
) -> Any:
    """Apply classic three-way semantics to one value or presence slot."""

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
        MergeConflict(
            domain=domain,
            path=path,
            base=_conflict_value(base),
            ours=_conflict_value(ours),
            theirs=_conflict_value(theirs),
            reason=reason,
        )
    )
    # Continue enough to build deterministic intermediate values.  The public
    # result will discard the snapshot whenever a conflict was recorded.
    return ours if ours is not _MISSING else theirs


def _merge_mapping(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    *,
    domain: Literal["electrical", "placement"],
    path_prefix: str,
    conflicts: list[MergeConflict],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    keys = sorted(set(base) | set(ours) | set(theirs))
    for key in keys:
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


def _merge_ports(
    base: Sequence[Port],
    ours: Sequence[Port],
    theirs: Sequence[Port],
    conflicts: list[MergeConflict],
) -> tuple[Port, ...]:
    base_by_name = {item.name: item for item in base}
    ours_by_name = {item.name: item for item in ours}
    theirs_by_name = {item.name: item for item in theirs}
    result: list[Port] = []
    for name in sorted(set(base_by_name) | set(ours_by_name) | set(theirs_by_name)):
        b = base_by_name.get(name, _MISSING)
        o = ours_by_name.get(name, _MISSING)
        t = theirs_by_name.get(name, _MISSING)
        if b is not _MISSING and o is not _MISSING and t is not _MISSING:
            direction = _merge_atom(
                b.direction,
                o.direction,
                t.direction,
                domain="electrical",
                path=f"ports.{name}.direction",
                conflicts=conflicts,
            )
            result.append(Port(name=name, direction=direction))
        else:
            value = _merge_atom(
                b,
                o,
                t,
                domain="electrical",
                path=f"ports.{name}",
                conflicts=conflicts,
            )
            if value is not _MISSING:
                result.append(value)
    return tuple(result)


def _merge_nets(
    base: Sequence[Net],
    ours: Sequence[Net],
    theirs: Sequence[Net],
    conflicts: list[MergeConflict],
) -> tuple[Net, ...]:
    base_by_name = {item.name: item for item in base}
    ours_by_name = {item.name: item for item in ours}
    theirs_by_name = {item.name: item for item in theirs}
    result: list[Net] = []
    for name in sorted(set(base_by_name) | set(ours_by_name) | set(theirs_by_name)):
        b = base_by_name.get(name, _MISSING)
        o = ours_by_name.get(name, _MISSING)
        t = theirs_by_name.get(name, _MISSING)
        if b is not _MISSING and o is not _MISSING and t is not _MISSING:
            is_global = _merge_atom(
                b.is_global,
                o.is_global,
                t.is_global,
                domain="electrical",
                path=f"nets.{name}.is_global",
                conflicts=conflicts,
            )
            num_bits = _merge_atom(
                b.num_bits,
                o.num_bits,
                t.num_bits,
                domain="electrical",
                path=f"nets.{name}.num_bits",
                conflicts=conflicts,
            )
            result.append(Net(name=name, is_global=is_global, num_bits=num_bits))
        else:
            value = _merge_atom(
                b,
                o,
                t,
                domain="electrical",
                path=f"nets.{name}",
                conflicts=conflicts,
            )
            if value is not _MISSING:
                result.append(value)
    return tuple(result)


def _cdf_value_matches_type(value: Any, cdf_type: str) -> bool:
    if cdf_type not in VALID_CDF_TYPES:
        return False
    if cdf_type == "boolean":
        return type(value) is bool
    if cdf_type == "int":
        return type(value) is int
    if cdf_type == "float":
        return type(value) in (int, float)
    return type(value) is str


def _is_pure_legacy_derived(instance: Instance) -> bool:
    derived = instance.derived
    return (
        derived is not None
        and derived.source == "legacy-import"
        and derived.status == "valid"
        and derived.input_sha256 is None
        and derived.callback_order == ()
    )


def _merge_instance_derived(
    base: Instance,
    ours: Instance,
    theirs: Instance,
    *,
    merged_lib: str,
    merged_cell: str,
    merged_view: str,
    merged_params: Mapping[str, Any],
    merged_param_types: Mapping[str, str],
    rules: Sequence[CdfCouplingRule],
) -> DerivedState | None:
    """Resolve instance DerivedState under v2 derived-aware merge semantics.

    Derived values are read-only PDK callback observations and never produce
    merge conflicts.
    """

    b_der = base.effective_derived
    o_der = ours.effective_derived
    t_der = theirs.effective_derived
    any_derived_present = any(
        inst.derived is not None or bool(inst.derived_params)
        for inst in (base, ours, theirs)
    )

    ours_primary_changed = not (
        _strict_equal((base.lib, base.cell, base.view), (ours.lib, ours.cell, ours.view))
        and _strict_equal(base.params, ours.params)
        and _strict_equal(base.param_types, ours.param_types)
    )
    theirs_primary_changed = not (
        _strict_equal((base.lib, base.cell, base.view), (theirs.lib, theirs.cell, theirs.view))
        and _strict_equal(base.params, theirs.params)
        and _strict_equal(base.param_types, theirs.param_types)
    )
    merged_primary_changed = not (
        _strict_equal((base.lib, base.cell, base.view), (merged_lib, merged_cell, merged_view))
        and _strict_equal(base.params, merged_params)
        and _strict_equal(base.param_types, merged_param_types)
    )

    coupled_members: set[str] = set()
    for rule in rules:
        coupled_members.update(rule.members)
        coupled_members.update(rule.primary)
    coupled_member_modified = False
    if coupled_members:
        all_param_keys = set(base.params) | set(ours.params) | set(theirs.params)
        for key in all_param_keys & coupled_members:
            if not _strict_equal(base.params.get(key, _MISSING), ours.params.get(key, _MISSING)) or not _strict_equal(
                base.params.get(key, _MISSING), theirs.params.get(key, _MISSING)
            ):
                coupled_member_modified = True
                break

    stale_state = DerivedState(
        status="stale",
        values={},
        input_sha256=None,
        callback_order=(),
        source="cdf-readback",
    )

    if merged_primary_changed or coupled_member_modified:
        # Preserve legacy v1 single-branch primary+derived-together edits when
        # all three ancestors are legacy imports and no coupling rules apply.
        if (
            not rules
            and _is_pure_legacy_derived(base)
            and _is_pure_legacy_derived(ours)
            and _is_pure_legacy_derived(theirs)
        ):
            ours_derived_changed = not _strict_equal(b_der.values, o_der.values)
            theirs_derived_changed = not _strict_equal(b_der.values, t_der.values)
            if (
                theirs_derived_changed
                and not ours_derived_changed
                and theirs_primary_changed
                and not ours_primary_changed
                and not (set(merged_params) & set(t_der.values))
            ):
                return t_der
            if (
                ours_derived_changed
                and not theirs_derived_changed
                and ours_primary_changed
                and not theirs_primary_changed
                and not (set(merged_params) & set(o_der.values))
            ):
                return o_der
        return stale_state

    if not any_derived_present:
        return None

    if (
        b_der.status == "unavailable"
        and o_der.status == "unavailable"
        and t_der.status == "unavailable"
    ):
        return DerivedState(
            status="unavailable",
            values={},
            input_sha256=None,
            callback_order=(),
            source="cdf-readback",
        )

    merged_input_sha = compute_cdf_input_sha256(
        merged_lib,
        merged_cell,
        merged_view,
        merged_params,
        merged_param_types,
        coupling_rules=rules,
    )

    # Prefer valid base > valid ours > valid theirs; invalidate if input_sha256
    # mismatches merged inputs or overlaps primary params.
    for candidate in (b_der, o_der, t_der):
        if candidate.status != "valid":
            continue
        if set(merged_params) & set(candidate.values):
            continue
        if candidate.input_sha256 is not None and candidate.input_sha256 != merged_input_sha:
            continue
        return candidate

    return stale_state


def _merge_instances(
    base: Sequence[Instance],
    ours: Sequence[Instance],
    theirs: Sequence[Instance],
    conflicts: list[MergeConflict],
    *,
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
) -> tuple[Instance, ...]:
    base_by_name = {item.name: item for item in base}
    ours_by_name = {item.name: item for item in ours}
    theirs_by_name = {item.name: item for item in theirs}
    rules_map = coupling_rules or {}
    result: list[Instance] = []
    for name in sorted(set(base_by_name) | set(ours_by_name) | set(theirs_by_name)):
        b = base_by_name.get(name, _MISSING)
        o = ours_by_name.get(name, _MISSING)
        t = theirs_by_name.get(name, _MISSING)
        if b is _MISSING or o is _MISSING or t is _MISSING:
            value = _merge_atom(
                b,
                o,
                t,
                domain="electrical",
                path=f"instances.{name}",
                conflicts=conflicts,
            )
            if value is not _MISSING:
                result.append(value)
            continue

        lib = _merge_atom(
            b.lib,
            o.lib,
            t.lib,
            domain="electrical",
            path=f"instances.{name}.master.lib",
            conflicts=conflicts,
        )
        cell = _merge_atom(
            b.cell,
            o.cell,
            t.cell,
            domain="electrical",
            path=f"instances.{name}.master.cell",
            conflicts=conflicts,
        )
        view = _merge_atom(
            b.view,
            o.view,
            t.view,
            domain="electrical",
            path=f"instances.{name}.master.view",
            conflicts=conflicts,
        )
        connections = _merge_mapping(
            b.connections,
            o.connections,
            t.connections,
            domain="electrical",
            path_prefix=f"instances.{name}.connections",
            conflicts=conflicts,
        )
        conflicts_before_params = len(conflicts)
        params = _merge_mapping(
            b.params,
            o.params,
            t.params,
            domain="electrical",
            path_prefix=f"instances.{name}.params",
            conflicts=conflicts,
        )
        param_types = _merge_mapping(
            b.param_types,
            o.param_types,
            t.param_types,
            domain="electrical",
            path_prefix=f"instances.{name}.param_types",
            conflicts=conflicts,
        )
        conflicted_param_keys: set[str] = set()
        for conflict in conflicts[conflicts_before_params:]:
            for prefix in (f"instances.{name}.params.", f"instances.{name}.param_types."):
                if conflict.path.startswith(prefix):
                    conflicted_param_keys.add(conflict.path[len(prefix) :])

        has_any_param_types = bool(b.param_types or o.param_types or t.param_types)
        if has_any_param_types:
            for key in sorted(set(params) | set(param_types)):
                if key in conflicted_param_keys:
                    continue
                if key in params and key not in param_types:
                    conflicts.append(
                        MergeConflict(
                            domain="electrical",
                            path=f"instances.{name}.param_types.{key}",
                            base=b.param_types.get(key),
                            ours=o.param_types.get(key),
                            theirs=t.param_types.get(key),
                            reason="schema-conflict",
                        )
                    )
                elif key in param_types and key not in params:
                    conflicts.append(
                        MergeConflict(
                            domain="electrical",
                            path=f"instances.{name}.params.{key}",
                            base=b.params.get(key),
                            ours=o.params.get(key),
                            theirs=t.params.get(key),
                            reason="schema-conflict",
                        )
                    )
                elif not _cdf_value_matches_type(params[key], param_types[key]):
                    conflicts.append(
                        MergeConflict(
                            domain="electrical",
                            path=f"instances.{name}.params.{key}",
                            base=b.params.get(key),
                            ours=o.params.get(key),
                            theirs=t.params.get(key),
                            reason="schema-conflict",
                        )
                    )

        props = _merge_mapping(
            b.props,
            o.props,
            t.props,
            domain="electrical",
            path_prefix=f"instances.{name}.props",
            conflicts=conflicts,
        )

        instance_rules: list[CdfCouplingRule] = list(rules_map.get(f"{lib}/{cell}", ()))
        base_master_key = f"{b.lib}/{b.cell}"
        if base_master_key != f"{lib}/{cell}":
            for rule in rules_map.get(base_master_key, ()):
                if rule not in instance_rules:
                    instance_rules.append(rule)
            if instance_rules:
                resolve_cdf_callback_order((), coupling_rules=tuple(instance_rules))

        merged_derived = _merge_instance_derived(
            b,
            o,
            t,
            merged_lib=lib,
            merged_cell=cell,
            merged_view=view,
            merged_params=params,
            merged_param_types=param_types,
            rules=tuple(instance_rules),
        )
        derived_values = dict(merged_derived.values) if merged_derived is not None else {}
        result.append(
            Instance(
                name=name,
                lib=lib,
                cell=cell,
                view=view,
                connections=connections,
                params=params,
                param_types=param_types,
                derived_params=derived_values,
                props=props,
                derived=merged_derived,
            )
        )
    return tuple(result)


def _merge_electrical(
    base: SchematicElectrical,
    ours: SchematicElectrical,
    theirs: SchematicElectrical,
    conflicts: list[MergeConflict],
    *,
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
) -> SchematicElectrical:
    return SchematicElectrical(
        ports=_merge_ports(base.ports, ours.ports, theirs.ports, conflicts),
        nets=_merge_nets(base.nets, ours.nets, theirs.nets, conflicts),
        instances=_merge_instances(
            base.instances,
            ours.instances,
            theirs.instances,
            conflicts,
            coupling_rules=coupling_rules,
        ),
        properties=_merge_mapping(
            base.properties,
            ours.properties,
            theirs.properties,
            domain="electrical",
            path_prefix="properties",
            conflicts=conflicts,
        ),
        constraints=_merge_mapping(
            base.constraints,
            ours.constraints,
            theirs.constraints,
            domain="electrical",
            path_prefix="constraints",
            conflicts=conflicts,
        ),
    )


def _merge_placement_instances(
    base: Sequence[InstancePlacement],
    ours: Sequence[InstancePlacement],
    theirs: Sequence[InstancePlacement],
    conflicts: list[MergeConflict],
) -> tuple[InstancePlacement, ...]:
    base_by_name = {item.name: item for item in base}
    ours_by_name = {item.name: item for item in ours}
    theirs_by_name = {item.name: item for item in theirs}
    result: list[InstancePlacement] = []
    for name in sorted(set(base_by_name) | set(ours_by_name) | set(theirs_by_name)):
        b = base_by_name.get(name, _MISSING)
        o = ours_by_name.get(name, _MISSING)
        t = theirs_by_name.get(name, _MISSING)
        if b is not _MISSING and o is not _MISSING and t is not _MISSING:
            xy = _merge_atom(
                b.xy,
                o.xy,
                t.xy,
                domain="placement",
                path=f"instances.{name}.xy",
                conflicts=conflicts,
            )
            orient = _merge_atom(
                b.orient,
                o.orient,
                t.orient,
                domain="placement",
                path=f"instances.{name}.orient",
                conflicts=conflicts,
            )
            result.append(InstancePlacement(name=name, xy=xy, orient=orient))
        else:
            value = _merge_atom(
                b,
                o,
                t,
                domain="placement",
                path=f"instances.{name}",
                conflicts=conflicts,
            )
            if value is not _MISSING:
                result.append(value)
    return tuple(result)


def _group_pins(
    pins: Sequence[PinPlacement],
) -> dict[str, list[PinPlacement]]:
    groups: dict[str, list[PinPlacement]] = {}
    for pin in pins:
        groups.setdefault(pin.name, []).append(pin)
    for values in groups.values():
        values.sort(key=_stable_key)
    return groups


def _align_pin_branch(
    base: Sequence[PinPlacement], branch: Sequence[PinPlacement]
) -> list[PinPlacement | object]:
    """Align a branch's pins to base occurrences, retaining exact matches."""

    branch_remaining = list(sorted(branch, key=_stable_key))
    aligned: list[PinPlacement | object] = []
    for base_pin in sorted(base, key=_stable_key):
        exact_index = next(
            (
                index
                for index, branch_pin in enumerate(branch_remaining)
                if _strict_equal(base_pin, branch_pin)
            ),
            None,
        )
        if exact_index is None:
            aligned.append(_MISSING)
        else:
            aligned.append(branch_remaining.pop(exact_index))
    for index, value in enumerate(aligned):
        if value is _MISSING and branch_remaining:
            aligned[index] = branch_remaining.pop(0)
    aligned.extend(branch_remaining)
    return aligned


def _merge_pins(
    base: Sequence[PinPlacement],
    ours: Sequence[PinPlacement],
    theirs: Sequence[PinPlacement],
    conflicts: list[MergeConflict],
) -> tuple[PinPlacement, ...]:
    base_groups = _group_pins(base)
    ours_groups = _group_pins(ours)
    theirs_groups = _group_pins(theirs)
    result: list[PinPlacement] = []
    keys = sorted(set(base_groups) | set(ours_groups) | set(theirs_groups))
    for key in keys:
        base_values = sorted(base_groups.get(key, []), key=_stable_key)
        ours_values = _align_pin_branch(base_values, ours_groups.get(key, []))
        theirs_values = _align_pin_branch(base_values, theirs_groups.get(key, []))
        for index in range(max(len(base_values), len(ours_values), len(theirs_values))):
            b = base_values[index] if index < len(base_values) else _MISSING
            o = ours_values[index] if index < len(ours_values) else _MISSING
            t = theirs_values[index] if index < len(theirs_values) else _MISSING
            path = f"pins[{key!r}]"
            if index:
                path += f"[{index}]"
            if b is not _MISSING and o is not _MISSING and t is not _MISSING:
                merged = _merge_pin_fields(b, o, t, path, conflicts)
                result.append(merged)
            else:
                value = _merge_atom(
                    b,
                    o,
                    t,
                    domain="placement",
                    path=path,
                    conflicts=conflicts,
                )
                if value is not _MISSING:
                    result.append(value)
    return tuple(result)


def _merge_pin_fields(
    base: PinPlacement,
    ours: PinPlacement,
    theirs: PinPlacement,
    path: str,
    conflicts: list[MergeConflict],
) -> PinPlacement:
    name = base.name
    net = _merge_atom(
        base.net,
        ours.net,
        theirs.net,
        domain="placement",
        path=f"{path}.net",
        conflicts=conflicts,
    )
    direction = _merge_atom(
        base.direction,
        ours.direction,
        theirs.direction,
        domain="placement",
        path=f"{path}.direction",
        conflicts=conflicts,
    )
    lib = _merge_atom(
        base.lib,
        ours.lib,
        theirs.lib,
        domain="placement",
        path=f"{path}.lib",
        conflicts=conflicts,
    )
    cell = _merge_atom(
        base.cell,
        ours.cell,
        theirs.cell,
        domain="placement",
        path=f"{path}.cell",
        conflicts=conflicts,
    )
    view = _merge_atom(
        base.view,
        ours.view,
        theirs.view,
        domain="placement",
        path=f"{path}.view",
        conflicts=conflicts,
    )
    xy = _merge_atom(
        base.xy,
        ours.xy,
        theirs.xy,
        domain="placement",
        path=f"{path}.xy",
        conflicts=conflicts,
    )
    orient = _merge_atom(
        base.orient,
        ours.orient,
        theirs.orient,
        domain="placement",
        path=f"{path}.orient",
        conflicts=conflicts,
    )
    return PinPlacement(
        name=name,
        net=net,
        direction=direction,
        lib=lib,
        cell=cell,
        view=view,
        xy=xy,
        orient=orient,
    )


def _fmt_coord(value: float) -> str:
    return f"{0.0 if value == 0.0 else value:g}"


def _fmt_xy(xy: tuple[float, float]) -> str:
    return f"({_fmt_coord(xy[0])}, {_fmt_coord(xy[1])})"


def _describe_multiset_item(item: Any) -> str:
    """Render a compact, readable identifier for multiset conflict paths."""

    if isinstance(item, WireSegment):
        pts = "->".join(_fmt_xy(p) for p in item.points)
        if item.net is not None:
            return f"wire(net={item.net}, {pts})"
        return f"wire({pts})"
    if isinstance(item, NoteLabel):
        return f"label({item.text!r}, {_fmt_xy(item.xy)})"
    if isinstance(item, AnnotationShape):
        pts = ", ".join(_fmt_xy(p) for p in item.points)
        return f"{item.kind}({item.layer}:{item.purpose}, {pts})"
    if isinstance(item, OmissionRecord):
        return f"omission({item.kind}:{item.identifier}:{item.reason})"
    return _key_text(item)


def _merge_multiset(
    base: Sequence[Any],
    ours: Sequence[Any],
    theirs: Sequence[Any],
    *,
    domain: Literal["electrical", "placement"],
    path_prefix: str,
    conflicts: list[MergeConflict],
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
    keys = sorted(
        set(base_groups) | set(ours_groups) | set(theirs_groups), key=lambda item: item
    )
    for index, key in enumerate(keys):
        base_count = len(base_groups.get(key, []))
        ours_count = len(ours_groups.get(key, []))
        theirs_count = len(theirs_groups.get(key, []))
        representative = next(
            (
                values[key][0]
                for values in (ours_groups, theirs_groups, base_groups)
                if values.get(key)
            ),
            None,
        )
        item_label = (
            _describe_multiset_item(representative)
            if representative is not None
            else _key_text(key)
        )
        count = _merge_atom(
            base_count,
            ours_count,
            theirs_count,
            domain=domain,
            path=f"{path_prefix}[{index}:{item_label}]",
            conflicts=conflicts,
        )
        if count is _MISSING:
            continue
        if representative is not None:
            result.extend([representative] * count)
    return tuple(result)


def _merge_placement(
    base: SchematicPlacement,
    ours: SchematicPlacement,
    theirs: SchematicPlacement,
    conflicts: list[MergeConflict],
) -> SchematicPlacement:
    return SchematicPlacement(
        instances=_merge_placement_instances(
            base.instances, ours.instances, theirs.instances, conflicts
        ),
        pins=_merge_pins(base.pins, ours.pins, theirs.pins, conflicts),
        wires=_merge_multiset(
            base.wires,
            ours.wires,
            theirs.wires,
            domain="placement",
            path_prefix="wires",
            conflicts=conflicts,
        ),
        labels=_merge_multiset(
            base.labels,
            ours.labels,
            theirs.labels,
            domain="placement",
            path_prefix="labels",
            conflicts=conflicts,
        ),
        sheet=_merge_mapping(
            base.sheet,
            ours.sheet,
            theirs.sheet,
            domain="placement",
            path_prefix="sheet",
            conflicts=conflicts,
        ),
        annotations=_merge_multiset(
            base.annotations,
            ours.annotations,
            theirs.annotations,
            domain="placement",
            path_prefix="annotations",
            conflicts=conflicts,
        ),
    )


def _validate_coupling_rules_map(
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None,
) -> dict[str, tuple[CdfCouplingRule, ...]]:
    if coupling_rules is None:
        return {}
    if not isinstance(coupling_rules, Mapping):
        raise SchemaValidationError("coupling_rules must be a mapping")
    validated: dict[str, tuple[CdfCouplingRule, ...]] = {}
    for master_key, raw_rules in sorted(coupling_rules.items()):
        if type(master_key) is not str or not master_key.strip():
            raise SchemaValidationError(f"invalid coupling_rules key: {master_key!r}")
        if isinstance(raw_rules, (str, bytes, bytearray)) or not isinstance(raw_rules, Sequence):
            raise SchemaValidationError(f"coupling_rules[{master_key!r}] must be a sequence")
        parsed: list[CdfCouplingRule] = []
        for idx, item in enumerate(raw_rules):
            if isinstance(item, CdfCouplingRule):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(CdfCouplingRule.from_dict(item))
            else:
                raise SchemaValidationError(
                    f"coupling_rules[{master_key!r}][{idx}] must be a CdfCouplingRule or mapping"
                )
        rules_tuple = tuple(parsed)
        resolve_cdf_callback_order((), coupling_rules=rules_tuple)
        validated[master_key] = rules_tuple
    return validated


def merge_schematics(
    base: SchematicSnapshot,
    ours: SchematicSnapshot,
    theirs: SchematicSnapshot,
    *,
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
) -> MergeResult:
    """Merge two schematic descendants against a common base snapshot."""

    validated_rules = _validate_coupling_rules_map(coupling_rules)

    cellviews = (base.manifest.cellview, ours.manifest.cellview, theirs.manifest.cellview)
    if not (cellviews[0] == cellviews[1] == cellviews[2]):
        raise SchemaValidationError(
            "cannot merge snapshots with mismatched cellview identities: "
            + ", ".join(cellview.key for cellview in cellviews)
        )

    base_electrical = canonicalize_electrical(base.electrical)
    ours_electrical = canonicalize_electrical(ours.electrical)
    theirs_electrical = canonicalize_electrical(theirs.electrical)
    base_placement = canonicalize_placement(base.placement)
    ours_placement = canonicalize_placement(ours.placement)
    theirs_placement = canonicalize_placement(theirs.placement)

    conflicts: list[MergeConflict] = []
    merged_electrical = _merge_electrical(
        base_electrical,
        ours_electrical,
        theirs_electrical,
        conflicts,
        coupling_rules=validated_rules,
    )
    merged_placement = _merge_placement(
        base_placement, ours_placement, theirs_placement, conflicts
    )

    merged_metadata = _merge_mapping(
        base.manifest.metadata,
        ours.manifest.metadata,
        theirs.manifest.metadata,
        domain="placement",
        path_prefix="manifest.metadata",
        conflicts=conflicts,
    )
    merged_omissions = _merge_multiset(
        base.omissions,
        ours.omissions,
        theirs.omissions,
        domain="placement",
        path_prefix="omissions",
        conflicts=conflicts,
    )

    sorted_conflicts = _sort_conflicts(conflicts)
    if sorted_conflicts:
        return MergeResult(ok=False, snapshot=None, conflicts=sorted_conflicts)

    snapshot = build_schematic_snapshot(
        ours.manifest.cellview,
        merged_electrical,
        merged_placement,
        omissions=tuple(merged_omissions),
        metadata=merged_metadata,
    )
    return MergeResult(ok=True, snapshot=snapshot, conflicts=())


def format_merge_conflicts(result: MergeResult) -> str:
    """Render merge conflicts in deterministic human-readable text."""

    if not result.conflicts:
        return "No merge conflicts."
    sorted_conflicts = _sort_conflicts(result.conflicts)
    lines = [f"Merge conflicts ({len(sorted_conflicts)}):"]
    grouped: dict[str, list[MergeConflict]] = {}
    for conflict in sorted_conflicts:
        grouped.setdefault(conflict.domain, []).append(conflict)
    for domain, items in grouped.items():
        lines.append(f"{domain.capitalize()} conflicts ({len(items)}):")
        for conflict in items:
            lines.append(
                f"  - [{conflict.domain}] {conflict.path}: {conflict.reason}; "
                f"base={_to_plain(conflict.base)!r}, "
                f"ours={_to_plain(conflict.ours)!r}, "
                f"theirs={_to_plain(conflict.theirs)!r}"
            )
    return "\n".join(lines)
