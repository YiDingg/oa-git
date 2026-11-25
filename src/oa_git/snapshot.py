"""Deterministic canonical codec for schematic snapshots.

The public functions in this module deliberately keep the serialization boundary
small.  Dataclasses from :mod:`oa_git.model` are validated and canonicalized
before they cross that boundary, so equivalent input objects produce identical
text and identical manifest digests.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .model import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    SUPPORTED_SCHEMA_VERSIONS,
    VALID_ANNOTATION_KINDS,
    VALID_CDF_TYPES,
    VALID_DERIVED_SOURCES,
    VALID_DERIVED_STATUSES,
    VALID_ORIENTS,
    VALID_PORT_DIRECTIONS,
    AnnotationShape,
    CdfCouplingRule,
    CdfParameterError,
    CellViewRef,
    DerivedState,
    Instance,
    InstancePlacement,
    Manifest,
    Net,
    NoteLabel,
    OmissionRecord,
    PinPlacement,
    Port,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    SnapshotIntegrityError,
    ViewType,
    WireLabel,
    WireSegment,
)
from .view_contract import (
    compute_content_sha256,
    compute_manifest_v2_full_sha256,
    has_view_handler,
    register_view_handler,
)


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = {
    "schema",
    "view_type",
    "cellview",
    "content_sha256",
    "electrical_sha256",
    "placement_sha256",
    "full_sha256",
    "files",
    "omissions_count",
    "metadata",
}


def _error(message: str) -> SchemaValidationError:
    return SchemaValidationError(message)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(f"{path} must be a mapping")
    for key in value:
        if not isinstance(key, str):
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


def _require_text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise _error(f"{path} must be a string")
    if nonempty and not value.strip():
        raise _error(f"{path} must not be empty")
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


def _normalize_float(value: float) -> float:
    return 0.0 if value == 0.0 else value


def _canonical_value(
    value: Any, path: str, _active_ids: set[int] | None = None
) -> Any:
    """Return a JSON/YAML-safe value while preserving scalar types."""

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
        active_ids = _active_ids if _active_ids is not None else set()
        object_id = id(value)
        if object_id in active_ids:
            raise _error(f"{path} contains a cyclic container reference")
        active_ids.add(object_id)
        try:
            if isinstance(value, Mapping):
                out: dict[str, Any] = {}
                keys = list(value)
                if any(not isinstance(key, str) for key in keys):
                    raise _error(f"{path} keys must be strings")
                for key in sorted(keys):
                    out[key] = _canonical_value(
                        value[key], f"{path}.{key}", active_ids
                    )
                return out
            return [
                _canonical_value(item, f"{path}[{index}]", active_ids)
                for index, item in enumerate(value)
            ]
        finally:
            active_ids.remove(object_id)
    raise _error(f"{path} contains unsupported value type {type(value).__name__}")


def _canonical_param_map(value: Any, path: str) -> dict[str, str | int | float | bool]:
    mapping = _require_mapping(value, path)
    out: dict[str, str | int | float | bool] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        item = mapping[key]
        if type(item) not in (str, bool, int, float):
            raise _error(f"{path}.{key} must be a scalar ParamValue")
        if isinstance(item, float) and not math.isfinite(item):
            raise _error(f"{path}.{key} must be finite")
        out[key] = _normalize_float(item) if type(item) is float else item
    return out


def _canonical_string_map(value: Any, path: str) -> dict[str, str]:
    mapping = _require_mapping(value, path)
    out: dict[str, str] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        out[key] = _require_text(mapping[key], f"{path}.{key}")
    return out


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


def quantize_coord(value: float, precision: int = 6) -> float:
    """Quantize one finite coordinate and normalize negative zero."""

    if type(precision) is not int or precision < 0:
        raise _error("precision must be a non-negative int")
    number = _require_finite_number(value, "coordinate")
    try:
        as_float = float(number)
    except (OverflowError, ValueError) as exc:
        raise _error("coordinate must be representable as a finite float") from exc
    if not math.isfinite(as_float):
        raise _error("coordinate must be finite")
    result = float(round(as_float, precision))
    return _normalize_float(result)


def quantize_xy(
    xy: tuple[float, float], precision: int = 6
) -> tuple[float, float]:
    """Quantize a two-dimensional coordinate."""

    values = _require_sequence(xy, "xy")
    if len(values) != 2:
        raise _error("xy must contain exactly two coordinates")
    return (
        quantize_coord(values[0], precision),
        quantize_coord(values[1], precision),
    )


def resolve_cdf_callback_order(
    params: Iterable[str],
    *,
    declaration_order: Sequence[str] = (),
    coupling_rules: Sequence[CdfCouplingRule] = (),
) -> tuple[str, ...]:
    """Deterministically order CDF parameters by coupling dependencies.

    Dependencies are topologically sorted with ties broken by
    ``declaration_order``, then coupling rule ``order``, then parameter name.
    Any dependency cycle fails closed with :class:`CdfParameterError` containing
    ``cdf_order_cycle``.
    """

    param_list: list[str] = []
    param_set: set[str] = set()
    for index, raw_name in enumerate(params):
        name = _require_text(raw_name, f"params[{index}]")
        if name not in param_set:
            param_set.add(name)
            param_list.append(name)

    decl_index: dict[str, int] = {}
    for idx, raw_name in enumerate(_require_sequence(declaration_order, "declaration_order")):
        name = _require_text(raw_name, f"declaration_order[{idx}]")
        decl_index.setdefault(name, idx)

    rule_order_index: dict[str, int] = {}
    edges: dict[str, set[str]] = {}
    all_nodes: set[str] = set(param_set)
    for rule_idx, rule in enumerate(_require_sequence(coupling_rules, "coupling_rules")):
        if not isinstance(rule, CdfCouplingRule):
            raise _error(f"coupling_rules[{rule_idx}] must be a CdfCouplingRule")
        for order_name in rule.order:
            text = _require_text(order_name, f"coupling_rules[{rule_idx}].order")
            rule_order_index.setdefault(text, len(rule_order_index))
        for target_raw, deps_raw in rule.dependencies.items():
            target = _require_text(target_raw, f"coupling_rules[{rule_idx}].dependencies key")
            all_nodes.add(target)
            edges.setdefault(target, set())
            for dep_raw in _require_sequence(
                deps_raw, f"coupling_rules[{rule_idx}].dependencies[{target!r}]"
            ):
                dep = _require_text(
                    dep_raw, f"coupling_rules[{rule_idx}].dependencies[{target!r}]"
                )
                all_nodes.add(dep)
                edges.setdefault(dep, set()).add(target)

    if not all_nodes:
        return ()

    indegree: dict[str, int] = {node: 0 for node in all_nodes}
    for src, targets in edges.items():
        for dst in targets:
            indegree[dst] += 1

    decl_default = len(decl_index)
    rule_default = len(rule_order_index)

    def _priority(node: str) -> tuple[int, int, str]:
        return (
            decl_index.get(node, decl_default),
            rule_order_index.get(node, rule_default),
            node,
        )

    heap: list[tuple[int, int, str]] = [
        _priority(node) for node, deg in indegree.items() if deg == 0
    ]
    heapq.heapify(heap)

    ordered_all: list[str] = []
    while heap:
        _, _, current = heapq.heappop(heap)
        ordered_all.append(current)
        for neighbor in sorted(edges.get(current, ())):
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                heapq.heappush(heap, _priority(neighbor))

    if len(ordered_all) != len(all_nodes):
        cycle_nodes = sorted(node for node, deg in indegree.items() if deg > 0)
        raise CdfParameterError(
            f"cdf_order_cycle: cyclic CDF parameter dependencies involving {', '.join(cycle_nodes)}"
        )

    return tuple(node for node in ordered_all if node in param_set)


def compute_cdf_input_sha256(
    lib: str,
    cell: str,
    view: str,
    params: Mapping[str, Any],
    param_types: Mapping[str, str],
    coupling_rules: Sequence[CdfCouplingRule] = (),
) -> str:
    """Compute canonical SHA-256 fingerprint of CDF primary inputs and coupling contract."""

    canonical_lib = _require_text(lib, "lib")
    canonical_cell = _require_text(cell, "cell")
    canonical_view = _require_text(view, "view")
    canonical_params = _canonical_param_map(params, "params")
    canonical_types = _canonical_param_map_types(param_types, "param_types")

    serialized_rules: list[dict[str, Any]] = []
    for idx, rule in enumerate(_require_sequence(coupling_rules, "coupling_rules")):
        if not isinstance(rule, CdfCouplingRule):
            raise _error(f"coupling_rules[{idx}] must be a CdfCouplingRule")
        serialized_rules.append(rule.to_dict())
    serialized_rules.sort(key=lambda item: _json_text(item, compact=True))

    payload = {
        "master": {
            "lib": canonical_lib,
            "cell": canonical_cell,
            "view": canonical_view,
        },
        "params": canonical_params,
        "param_types": canonical_types,
        "coupling_rules": serialized_rules,
    }
    return _sha256_bytes(_json_text(payload, compact=True).encode("utf-8"))


def _canonical_derived_state(derived: DerivedState, path: str) -> DerivedState:
    if not isinstance(derived, DerivedState):
        raise _error(f"{path} must be a DerivedState")
    status = _require_text(derived.status, f"{path}.status")
    if status not in VALID_DERIVED_STATUSES:
        raise _error(f"{path}.status has unsupported value {status!r}")
    source = _require_text(derived.source, f"{path}.source")
    if source not in VALID_DERIVED_SOURCES:
        raise _error(f"{path}.source has unsupported value {source!r}")
    values = _canonical_param_map(derived.values, f"{path}.values")
    input_sha256 = derived.input_sha256
    if input_sha256 is not None:
        input_sha256 = _require_hash(input_sha256, f"{path}.input_sha256")
    raw_order = _require_sequence(derived.callback_order, f"{path}.callback_order")
    callback_order = tuple(
        _require_text(item, f"{path}.callback_order[{idx}]")
        for idx, item in enumerate(raw_order)
    )
    return DerivedState(
        status=status,  # type: ignore[arg-type]
        values=values,
        input_sha256=input_sha256,
        callback_order=callback_order,
        source=source,  # type: ignore[arg-type]
    )


def _derived_state_from_dict(data: Any, path: str) -> DerivedState:
    mapping = _require_mapping(data, path)
    _check_keys(
        mapping,
        {"status", "values", "input_sha256", "callback_order", "source"},
        {"status"},
        path,
    )
    status = _require_text(mapping["status"], f"{path}.status")
    if status not in VALID_DERIVED_STATUSES:
        raise _error(f"{path}.status has unsupported value {status!r}")
    source = _require_text(mapping.get("source", "cdf-readback"), f"{path}.source")
    if source not in VALID_DERIVED_SOURCES:
        raise _error(f"{path}.source has unsupported value {source!r}")
    values = _canonical_param_map(mapping.get("values", {}), f"{path}.values")
    raw_sha = mapping.get("input_sha256")
    input_sha256 = None if raw_sha is None else _require_hash(raw_sha, f"{path}.input_sha256")
    raw_order = _require_sequence(mapping.get("callback_order", ()), f"{path}.callback_order")
    callback_order = tuple(
        _require_text(item, f"{path}.callback_order[{idx}]")
        for idx, item in enumerate(raw_order)
    )
    return DerivedState(
        status=status,  # type: ignore[arg-type]
        values=values,
        input_sha256=input_sha256,
        callback_order=callback_order,
        source=source,  # type: ignore[arg-type]
    )


def _canonical_annotation_shape(shape: AnnotationShape, path: str) -> AnnotationShape:
    if not isinstance(shape, AnnotationShape):
        raise _error(f"{path} must be an AnnotationShape")
    kind = _require_text(shape.kind, f"{path}.kind")
    if kind not in VALID_ANNOTATION_KINDS:
        raise _error(f"{path}.kind has unsupported annotation kind {kind!r}")
    layer = _require_text(shape.layer, f"{path}.layer")
    purpose = _require_text(shape.purpose, f"{path}.purpose")
    width = quantize_coord(shape.width)
    if width < 0.0:
        raise _error(f"{path}.width must be >= 0")
    raw_points = _require_sequence(shape.points, f"{path}.points")
    points = tuple(quantize_xy(pt) for pt in raw_points)
    if kind == "rect":
        if len(points) != 2:
            raise _error(f"{path}.points for rect must contain exactly 2 points")
        (x0, y0), (x1, y1) = points
        points = ((min(x0, x1), min(y0, y1)), (max(x0, x1), max(y0, y1)))
    elif kind == "line":
        if len(points) < 2:
            raise _error(f"{path}.points for line must contain at least 2 points")
        points = min(points, tuple(reversed(points)))
    elif kind == "polygon":
        if len(points) < 3:
            raise _error(f"{path}.points for polygon must contain at least 3 points")
    return AnnotationShape(
        kind=kind,
        layer=layer,
        purpose=purpose,
        points=points,
        width=width,
    )


def _annotation_shape_key(shape: AnnotationShape) -> tuple[Any, ...]:
    return (shape.kind, shape.layer, shape.purpose, shape.points, shape.width)


def canonicalize_electrical(electrical: SchematicElectrical) -> SchematicElectrical:
    """Validate and deterministically sort electrical schematic data."""

    if not isinstance(electrical, SchematicElectrical):
        raise _error("electrical must be a SchematicElectrical")

    ports: list[Port] = []
    port_names: set[str] = set()
    for index, port in enumerate(_require_sequence(electrical.ports, "ports")):
        if not isinstance(port, Port):
            raise _error(f"ports[{index}] must be a Port")
        name = _require_text(port.name, f"ports[{index}].name")
        if name in port_names:
            raise _error(f"duplicate port name {name!r}")
        port_names.add(name)
        ports.append(Port(name=name, direction=_require_direction(port.direction, f"ports[{index}].direction")))

    nets: list[Net] = []
    net_names: set[str] = set()
    for index, net in enumerate(_require_sequence(electrical.nets, "nets")):
        if not isinstance(net, Net):
            raise _error(f"nets[{index}] must be a Net")
        name = _require_text(net.name, f"nets[{index}].name")
        if name in net_names:
            raise _error(f"duplicate net name {name!r}")
        net_names.add(name)
        nets.append(
            Net(
                name=name,
                is_global=_require_bool(net.is_global, f"nets[{index}].is_global"),
                num_bits=_require_int(net.num_bits, f"nets[{index}].num_bits", minimum=1),
            )
        )

    instances: list[Instance] = []
    instance_names: set[str] = set()
    for index, instance in enumerate(_require_sequence(electrical.instances, "instances")):
        if not isinstance(instance, Instance):
            raise _error(f"instances[{index}] must be an Instance")
        name = _require_text(instance.name, f"instances[{index}].name")
        if name in instance_names:
            raise _error(f"duplicate instance name {name!r}")
        instance_names.add(name)
        canonical_params = _canonical_param_map(instance.params, f"instances[{index}].params")
        canonical_param_types = _canonical_param_map_types(
            instance.param_types, f"instances[{index}].param_types"
        )
        raw_derived_params = _canonical_param_map(
            instance.derived_params, f"instances[{index}].derived_params"
        )
        raw_derived = instance.derived
        if raw_derived is None:
            if raw_derived_params:
                canonical_derived: DerivedState | None = DerivedState(
                    status="valid",
                    values=raw_derived_params,
                    input_sha256=None,
                    callback_order=(),
                    source="legacy-import",
                )
            else:
                canonical_derived = None
        else:
            if not isinstance(raw_derived, DerivedState):
                raise _error(f"instances[{index}].derived must be a DerivedState or None")
            if (
                raw_derived.source == "legacy-import"
                and raw_derived.status == "valid"
                and raw_derived.input_sha256 is None
                and raw_derived.callback_order == ()
                and raw_derived_params != raw_derived.values
            ):
                canonical_derived = (
                    DerivedState(
                        status="valid",
                        values=raw_derived_params,
                        input_sha256=None,
                        callback_order=(),
                        source="legacy-import",
                    )
                    if raw_derived_params
                    else None
                )
            else:
                canonical_derived = _canonical_derived_state(
                    raw_derived, f"instances[{index}].derived"
                )

        derived_values = dict(canonical_derived.values) if canonical_derived is not None else {}
        overlap = set(canonical_params) & set(derived_values)
        if overlap:
            raise _error(
                f"instances[{index}] params and derived.values must be disjoint; "
                f"shared keys: {sorted(overlap)!r}"
            )
        instances.append(
            Instance(
                name=name,
                lib=_require_text(instance.lib, f"instances[{index}].lib"),
                cell=_require_text(instance.cell, f"instances[{index}].cell"),
                view=_require_text(instance.view, f"instances[{index}].view"),
                connections=_canonical_string_map(
                    instance.connections, f"instances[{index}].connections"
                ),
                params=canonical_params,
                param_types=canonical_param_types,
                derived_params=derived_values,
                props=_canonical_param_map(instance.props, f"instances[{index}].props"),
                derived=canonical_derived,
            )
        )

    properties = _canonical_param_map(electrical.properties, "properties")
    constraints_raw = _require_mapping(electrical.constraints, "constraints")
    constraints = _canonical_value(constraints_raw, "constraints")
    if not isinstance(constraints, dict):  # pragma: no cover - guarded above
        raise _error("constraints must be a mapping")

    return SchematicElectrical(
        ports=tuple(sorted(ports, key=lambda item: item.name)),
        nets=tuple(sorted(nets, key=lambda item: item.name)),
        instances=tuple(sorted(instances, key=lambda item: item.name)),
        properties=properties,
        constraints=constraints,
    )


def _canonical_param_map_types(value: Any, path: str) -> dict[str, str]:
    mapping = _require_mapping(value, path)
    out: dict[str, str] = {}
    for key in sorted(mapping):
        _require_text(key, f"{path} key")
        item = _require_text(mapping[key], f"{path}.{key}")
        if item not in VALID_CDF_TYPES:
            raise _error(f"{path}.{key} has unsupported CDF type {item!r}")
        out[key] = item
    return out


def _canonical_wire_label(label: WireLabel, path: str) -> WireLabel:
    if not isinstance(label, WireLabel):
        raise _error(f"{path} must be a WireLabel")
    return WireLabel(
        text=_require_text(label.text, f"{path}.text", nonempty=False),
        xy=quantize_xy(label.xy),
        justify=_require_text(label.justify, f"{path}.justify"),
        orient=_require_orientation(label.orient, f"{path}.orient"),
        font=_require_text(label.font, f"{path}.font"),
        height=quantize_coord(label.height),
    )


def _wire_label_key(label: WireLabel) -> tuple[Any, ...]:
    return (label.text, label.xy, label.justify, label.orient, label.font, label.height)


def _canonical_note_label(label: NoteLabel, path: str) -> NoteLabel:
    if not isinstance(label, NoteLabel):
        raise _error(f"{path} must be a NoteLabel")
    return NoteLabel(
        text=_require_text(label.text, f"{path}.text", nonempty=False),
        xy=quantize_xy(label.xy),
        layer=_require_text(label.layer, f"{path}.layer"),
        purpose=_require_text(label.purpose, f"{path}.purpose"),
        justify=_require_text(label.justify, f"{path}.justify"),
        orient=_require_orientation(label.orient, f"{path}.orient"),
        font=_require_text(label.font, f"{path}.font"),
        height=quantize_coord(label.height),
    )


def _note_label_key(label: NoteLabel) -> tuple[Any, ...]:
    return (
        label.text,
        label.xy,
        label.layer,
        label.purpose,
        label.justify,
        label.orient,
        label.font,
        label.height,
    )


def canonicalize_placement(placement: SchematicPlacement) -> SchematicPlacement:
    """Validate geometry, quantize coordinates, and apply canonical ordering."""

    if not isinstance(placement, SchematicPlacement):
        raise _error("placement must be a SchematicPlacement")

    instance_placements: list[InstancePlacement] = []
    placement_names: set[str] = set()
    for index, item in enumerate(_require_sequence(placement.instances, "placement.instances")):
        if not isinstance(item, InstancePlacement):
            raise _error(f"instances[{index}] must be an InstancePlacement")
        name = _require_text(item.name, f"instances[{index}].name")
        if name in placement_names:
            raise _error(f"duplicate instance placement name {name!r}")
        placement_names.add(name)
        instance_placements.append(
            InstancePlacement(
                name=name,
                xy=quantize_xy(item.xy),
                orient=_require_orientation(item.orient, f"instances[{index}].orient"),
            )
        )

    pins: list[PinPlacement] = []
    for index, pin in enumerate(_require_sequence(placement.pins, "placement.pins")):
        if not isinstance(pin, PinPlacement):
            raise _error(f"pins[{index}] must be a PinPlacement")
        pins.append(
            PinPlacement(
                name=_require_text(pin.name, f"pins[{index}].name"),
                net=_require_text(pin.net, f"pins[{index}].net", nonempty=False),
                direction=_require_direction(pin.direction, f"pins[{index}].direction"),
                lib=_require_text(pin.lib, f"pins[{index}].lib"),
                cell=_require_text(pin.cell, f"pins[{index}].cell"),
                view=_require_text(pin.view, f"pins[{index}].view"),
                xy=quantize_xy(pin.xy),
                orient=_require_orientation(pin.orient, f"pins[{index}].orient"),
            )
        )

    wires: list[WireSegment] = []
    for index, wire in enumerate(_require_sequence(placement.wires, "placement.wires")):
        if not isinstance(wire, WireSegment):
            raise _error(f"wires[{index}] must be a WireSegment")
        raw_points = _require_sequence(wire.points, f"wires[{index}].points")
        if len(raw_points) < 2:
            raise _error(f"wires[{index}].points must contain at least two points")
        points = tuple(quantize_xy(point) for point in raw_points)
        points = min(points, tuple(reversed(points)))
        if wire.net is not None and not isinstance(wire.net, str):
            raise _error(f"wires[{index}].net must be a string or null")
        labels = tuple(
            sorted(
                (
                    _canonical_wire_label(label, f"wires[{index}].labels[{label_index}]")
                    for label_index, label in enumerate(
                        _require_sequence(wire.labels, f"wires[{index}].labels")
                    )
                ),
                key=_wire_label_key,
            )
        )
        wires.append(
            WireSegment(
                points=points,
                net=wire.net,
                width=quantize_coord(wire.width),
                route_method=_require_text(wire.route_method, f"wires[{index}].route_method"),
                labels=labels,
            )
        )

    labels = tuple(
        sorted(
            (
                _canonical_note_label(label, f"labels[{index}]")
                for index, label in enumerate(
                    _require_sequence(placement.labels, "placement.labels")
                )
            ),
            key=_note_label_key,
        )
    )
    sheet = _canonical_value(_require_mapping(placement.sheet, "sheet"), "sheet")
    if not isinstance(sheet, dict):  # pragma: no cover - guarded above
        raise _error("sheet must be a mapping")

    annotations = tuple(
        sorted(
            (
                _canonical_annotation_shape(shape, f"annotations[{index}]")
                for index, shape in enumerate(
                    _require_sequence(placement.annotations, "placement.annotations")
                )
            ),
            key=_annotation_shape_key,
        )
    )

    return SchematicPlacement(
        instances=tuple(sorted(instance_placements, key=lambda item: item.name)),
        pins=tuple(
            sorted(
                pins,
                key=lambda item: (
                    item.net,
                    item.name,
                    item.xy,
                    item.orient,
                    item.lib,
                    item.cell,
                    item.view,
                ),
            )
        ),
        wires=tuple(
            sorted(
                wires,
                key=lambda item: (
                    item.points,
                    item.net or "",
                    item.width,
                    item.route_method,
                    tuple(_wire_label_key(label) for label in item.labels),
                    item.net is not None,
                ),
            )
        ),
        labels=labels,
        sheet=sheet,
        annotations=annotations,
    )


def _instance_to_dict(item: Instance) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": item.name,
        "lib": item.lib,
        "cell": item.cell,
        "view": item.view,
        "connections": dict(item.connections),
        "params": dict(item.params),
        "param_types": dict(item.param_types),
    }
    if item.derived is not None:
        payload["derived"] = item.derived.to_dict()
    payload["props"] = dict(item.props)
    return payload


def electrical_to_dict(electrical: SchematicElectrical) -> dict[str, Any]:
    canonical = canonicalize_electrical(electrical)
    return {
        "ports": [
            {"name": item.name, "direction": item.direction}
            for item in canonical.ports
        ],
        "nets": [
            {
                "name": item.name,
                "is_global": item.is_global,
                "num_bits": item.num_bits,
            }
            for item in canonical.nets
        ],
        "instances": [_instance_to_dict(item) for item in canonical.instances],
        "properties": dict(canonical.properties),
        "constraints": canonical.constraints,
    }


def placement_to_dict(placement: SchematicPlacement) -> dict[str, Any]:
    canonical = canonicalize_placement(placement)
    payload: dict[str, Any] = {
        "instances": [
            {"name": item.name, "xy": list(item.xy), "orient": item.orient}
            for item in canonical.instances
        ],
        "pins": [
            {
                "name": item.name,
                "net": item.net,
                "direction": item.direction,
                "lib": item.lib,
                "cell": item.cell,
                "view": item.view,
                "xy": list(item.xy),
                "orient": item.orient,
            }
            for item in canonical.pins
        ],
        "wires": [
            {
                "points": [list(point) for point in item.points],
                "net": item.net,
                "width": item.width,
                "route_method": item.route_method,
                "labels": [
                    {
                        "text": label.text,
                        "xy": list(label.xy),
                        "justify": label.justify,
                        "orient": label.orient,
                        "font": label.font,
                        "height": label.height,
                    }
                    for label in item.labels
                ],
            }
            for item in canonical.wires
        ],
        "labels": [
            {
                "text": item.text,
                "xy": list(item.xy),
                "layer": item.layer,
                "purpose": item.purpose,
                "justify": item.justify,
                "orient": item.orient,
                "font": item.font,
                "height": item.height,
            }
            for item in canonical.labels
        ],
        "sheet": canonical.sheet,
    }
    if canonical.annotations:
        payload["annotations"] = [
            {
                "kind": shape.kind,
                "layer": shape.layer,
                "purpose": shape.purpose,
                "points": [list(pt) for pt in shape.points],
                "width": shape.width,
            }
            for shape in canonical.annotations
        ]
    return payload


def electrical_from_dict(data: dict[str, Any]) -> SchematicElectrical:
    """Parse a strict electrical mapping and return canonical dataclasses."""

    mapping = _require_mapping(data, "electrical")
    _check_keys(
        mapping,
        {"ports", "nets", "instances", "properties", "constraints"},
        set(),
        "electrical",
    )

    ports: list[Port] = []
    for index, item in enumerate(_require_sequence(mapping.get("ports", []), "electrical.ports")):
        port = _require_mapping(item, f"ports[{index}]")
        _check_keys(port, {"name", "direction"}, {"name"}, f"ports[{index}]")
        ports.append(
            Port(
                name=_require_text(port["name"], f"ports[{index}].name"),
                direction=_require_direction(port.get("direction", "inputOutput"), f"ports[{index}].direction"),
            )
        )

    nets: list[Net] = []
    for index, item in enumerate(_require_sequence(mapping.get("nets", []), "electrical.nets")):
        net = _require_mapping(item, f"nets[{index}]")
        _check_keys(net, {"name", "is_global", "num_bits"}, {"name"}, f"nets[{index}]")
        nets.append(
            Net(
                name=_require_text(net["name"], f"nets[{index}].name"),
                is_global=_require_bool(net.get("is_global", False), f"nets[{index}].is_global"),
                num_bits=_require_int(net.get("num_bits", 1), f"nets[{index}].num_bits", minimum=1),
            )
        )

    instances: list[Instance] = []
    instance_keys = {
        "name",
        "lib",
        "cell",
        "view",
        "connections",
        "params",
        "param_types",
        "derived",
        "derived_params",
        "props",
    }
    for index, item in enumerate(_require_sequence(mapping.get("instances", []), "electrical.instances")):
        instance = _require_mapping(item, f"instances[{index}]")
        _check_keys(instance, instance_keys, {"name", "lib", "cell"}, f"instances[{index}]")
        has_derived = "derived" in instance
        has_legacy_derived = "derived_params" in instance
        if has_derived and has_legacy_derived:
            raise _error(
                f"instances[{index}] must not declare both 'derived' and legacy 'derived_params'"
            )
        parsed_params = _canonical_param_map(instance.get("params", {}), f"instances[{index}].params")
        parsed_param_types = _canonical_param_map_types(
            instance.get("param_types", {}), f"instances[{index}].param_types"
        )
        parsed_derived: DerivedState | None = None
        parsed_derived_params: dict[str, str | int | float | bool] = {}
        if has_derived:
            if instance["derived"] is not None:
                parsed_derived = _derived_state_from_dict(
                    instance["derived"], f"instances[{index}].derived"
                )
                parsed_derived_params = dict(parsed_derived.values)
        elif has_legacy_derived:
            legacy_values = _canonical_param_map(
                instance["derived_params"], f"instances[{index}].derived_params"
            )
            if legacy_values:
                parsed_derived = DerivedState(
                    status="valid",
                    values=legacy_values,
                    input_sha256=None,
                    callback_order=(),
                    source="legacy-import",
                )
                parsed_derived_params = dict(legacy_values)
        overlap = set(parsed_params) & set(parsed_derived_params)
        if overlap:
            raise _error(
                f"instances[{index}] params and derived.values must be disjoint; "
                f"shared keys: {sorted(overlap)!r}"
            )
        instances.append(
            Instance(
                name=_require_text(instance["name"], f"instances[{index}].name"),
                lib=_require_text(instance["lib"], f"instances[{index}].lib"),
                cell=_require_text(instance["cell"], f"instances[{index}].cell"),
                view=_require_text(instance.get("view", "symbol"), f"instances[{index}].view"),
                connections=_canonical_string_map(instance.get("connections", {}), f"instances[{index}].connections"),
                params=parsed_params,
                param_types=parsed_param_types,
                derived_params=parsed_derived_params,
                props=_canonical_param_map(instance.get("props", {}), f"instances[{index}].props"),
                derived=parsed_derived,
            )
        )

    properties = _canonical_param_map(mapping.get("properties", {}), "electrical.properties")
    constraints = _canonical_value(
        _require_mapping(mapping.get("constraints", {}), "electrical.constraints"),
        "electrical.constraints",
    )
    if not isinstance(constraints, dict):  # pragma: no cover - guarded above
        raise _error("electrical.constraints must be a mapping")
    return canonicalize_electrical(
        SchematicElectrical(
            ports=tuple(ports),
            nets=tuple(nets),
            instances=tuple(instances),
            properties=properties,
            constraints=constraints,
        )
    )


def placement_from_dict(data: dict[str, Any]) -> SchematicPlacement:
    """Parse a strict placement mapping and return canonical dataclasses."""

    mapping = _require_mapping(data, "placement")
    _check_keys(
        mapping,
        {"instances", "pins", "wires", "labels", "sheet", "annotations"},
        set(),
        "placement",
    )

    instances: list[InstancePlacement] = []
    for index, item in enumerate(_require_sequence(mapping.get("instances", []), "placement.instances")):
        instance = _require_mapping(item, f"placement.instances[{index}]")
        _check_keys(instance, {"name", "xy", "orient"}, {"name", "xy"}, f"placement.instances[{index}]")
        instances.append(
            InstancePlacement(
                name=_require_text(instance["name"], f"placement.instances[{index}].name"),
                xy=quantize_xy(instance["xy"]),
                orient=_require_orientation(instance.get("orient", "R0"), f"placement.instances[{index}].orient"),
            )
        )

    pins: list[PinPlacement] = []
    pin_keys = {"name", "net", "direction", "lib", "cell", "view", "xy", "orient"}
    for index, item in enumerate(_require_sequence(mapping.get("pins", []), "placement.pins")):
        pin = _require_mapping(item, f"placement.pins[{index}]")
        _check_keys(pin, pin_keys, {"name", "net", "direction", "lib", "cell", "view", "xy"}, f"placement.pins[{index}]")
        pins.append(
            PinPlacement(
                name=_require_text(pin["name"], f"placement.pins[{index}].name"),
                net=_require_text(pin["net"], f"placement.pins[{index}].net", nonempty=False),
                direction=_require_direction(pin["direction"], f"placement.pins[{index}].direction"),
                lib=_require_text(pin["lib"], f"placement.pins[{index}].lib"),
                cell=_require_text(pin["cell"], f"placement.pins[{index}].cell"),
                view=_require_text(pin["view"], f"placement.pins[{index}].view"),
                xy=quantize_xy(pin["xy"]),
                orient=_require_orientation(pin.get("orient", "R0"), f"placement.pins[{index}].orient"),
            )
        )

    wires: list[WireSegment] = []
    wire_keys = {"points", "net", "width", "route_method", "labels"}
    for index, item in enumerate(_require_sequence(mapping.get("wires", []), "placement.wires")):
        wire = _require_mapping(item, f"placement.wires[{index}]")
        _check_keys(wire, wire_keys, {"points"}, f"placement.wires[{index}]")
        wire_labels: list[WireLabel] = []
        for label_index, label_data in enumerate(
            _require_sequence(wire.get("labels", []), f"placement.wires[{index}].labels")
        ):
            label = _require_mapping(label_data, f"placement.wires[{index}].labels[{label_index}]")
            _check_keys(
                label,
                {"text", "xy", "justify", "orient", "font", "height"},
                {"text", "xy"},
                f"placement.wires[{index}].labels[{label_index}]",
            )
            wire_labels.append(
                WireLabel(
                    text=_require_text(label["text"], "wire label text", nonempty=False),
                    xy=quantize_xy(label["xy"]),
                    justify=_require_text(label.get("justify", "centerCenter"), "wire label justify"),
                    orient=_require_orientation(label.get("orient", "R0"), "wire label orient"),
                    font=_require_text(label.get("font", "stick"), "wire label font"),
                    height=quantize_coord(label.get("height", 0.0625)),
                )
            )
        net = wire.get("net")
        if net is not None:
            _require_text(net, f"placement.wires[{index}].net", nonempty=False)
        wires.append(
            WireSegment(
                points=tuple(quantize_xy(point) for point in _require_sequence(wire["points"], f"placement.wires[{index}].points")),
                net=net,
                width=quantize_coord(wire.get("width", 0.0)),
                route_method=_require_text(wire.get("route_method", "full"), f"placement.wires[{index}].route_method"),
                labels=tuple(wire_labels),
            )
        )

    labels: list[NoteLabel] = []
    note_keys = {"text", "xy", "layer", "purpose", "justify", "orient", "font", "height"}
    for index, item in enumerate(_require_sequence(mapping.get("labels", []), "placement.labels")):
        label = _require_mapping(item, f"placement.labels[{index}]")
        _check_keys(label, note_keys, {"text", "xy"}, f"placement.labels[{index}]")
        labels.append(
            NoteLabel(
                text=_require_text(label["text"], f"placement.labels[{index}].text", nonempty=False),
                xy=quantize_xy(label["xy"]),
                layer=_require_text(label.get("layer", "text"), f"placement.labels[{index}].layer"),
                purpose=_require_text(label.get("purpose", "drawing"), f"placement.labels[{index}].purpose"),
                justify=_require_text(label.get("justify", "lowerLeft"), f"placement.labels[{index}].justify"),
                orient=_require_orientation(label.get("orient", "R0"), f"placement.labels[{index}].orient"),
                font=_require_text(label.get("font", "stick"), f"placement.labels[{index}].font"),
                height=quantize_coord(label.get("height", 0.0625)),
            )
        )

    annotations: list[AnnotationShape] = []
    annotation_keys = {"kind", "layer", "purpose", "points", "width"}
    for index, item in enumerate(_require_sequence(mapping.get("annotations", []), "placement.annotations")):
        shape = _require_mapping(item, f"placement.annotations[{index}]")
        _check_keys(
            shape,
            annotation_keys,
            {"kind", "layer", "points"},
            f"placement.annotations[{index}]",
        )
        raw_pts = _require_sequence(shape["points"], f"placement.annotations[{index}].points")
        annotations.append(
            AnnotationShape(
                kind=_require_text(shape["kind"], f"placement.annotations[{index}].kind"),
                layer=_require_text(shape["layer"], f"placement.annotations[{index}].layer"),
                purpose=_require_text(shape.get("purpose", "drawing"), f"placement.annotations[{index}].purpose"),
                points=tuple(quantize_xy(pt) for pt in raw_pts),
                width=quantize_coord(shape.get("width", 0.0)),
            )
        )

    sheet = _canonical_value(
        _require_mapping(mapping.get("sheet", {}), "placement.sheet"), "placement.sheet"
    )
    if not isinstance(sheet, dict):  # pragma: no cover - guarded above
        raise _error("placement.sheet must be a mapping")
    return canonicalize_placement(
        SchematicPlacement(
            instances=tuple(instances),
            pins=tuple(pins),
            wires=tuple(wires),
            labels=tuple(labels),
            sheet=sheet,
            annotations=tuple(annotations),
        )
    )


def _yaml_dump(data: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )
    return rendered.rstrip("\n") + "\n"


def render_electrical_yaml(electrical: SchematicElectrical) -> str:
    return _yaml_dump(electrical_to_dict(electrical))


def render_placement_yaml(placement: SchematicPlacement) -> str:
    return _yaml_dump(placement_to_dict(placement))


def _canonicalize_omissions(
    omissions: tuple[OmissionRecord, ...] | Sequence[OmissionRecord],
) -> tuple[OmissionRecord, ...]:
    if isinstance(omissions, (str, bytes, bytearray)) or not isinstance(omissions, Sequence):
        raise _error("omissions must be a sequence")
    records: list[OmissionRecord] = []
    for index, item in enumerate(omissions):
        if not isinstance(item, OmissionRecord):
            raise _error(f"omissions[{index}] must be an OmissionRecord")
        records.append(
            OmissionRecord(
                kind=_require_text(item.kind, f"omissions[{index}].kind"),
                identifier=_require_text(item.identifier, f"omissions[{index}].identifier"),
                reason=_require_text(item.reason, f"omissions[{index}].reason"),
                details=_canonical_value(
                    _require_mapping(item.details, f"omissions[{index}].details"),
                    f"omissions[{index}].details",
                ),
            )
        )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item.kind,
                item.identifier,
                item.reason,
                _json_text(item.details, compact=True),
            ),
        )
    )


def render_omissions_json(omissions: tuple[OmissionRecord, ...]) -> str:
    records = _canonicalize_omissions(omissions)
    data = [record.to_dict() for record in records]
    rendered = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return rendered.rstrip("\n") + "\n"


def _json_text(data: Any, *, compact: bool = False) -> str:
    kwargs: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if compact:
        kwargs["separators"] = (",", ":")
    return json.dumps(data, **kwargs)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _full_sha256(
    electrical_sha256: str,
    placement_sha256: str,
    *,
    cellview: CellViewRef,
    omissions_count: int,
    omissions_sha256: str,
    metadata: Mapping[str, Any],
) -> str:
    return _sha256_bytes(
        _json_text(
            {
                "electrical": electrical_sha256,
                "placement": placement_sha256,
                "cellview": cellview.to_dict(),
                "omissions_count": omissions_count,
                "omissions_sha256": omissions_sha256,
                "metadata": _canonical_value(metadata, "metadata"),
            },
            compact=True,
        ).encode("utf-8")
    )


def _manifest_dict(manifest: Manifest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": manifest.schema,
        "view_type": manifest.view_type,
        "cellview": manifest.cellview.to_dict(),
        "electrical_sha256": manifest.electrical_sha256,
        "placement_sha256": manifest.placement_sha256,
        "full_sha256": manifest.full_sha256,
        "files": dict(sorted(manifest.files.items())),
        "omissions_count": manifest.omissions_count,
        "metadata": _canonical_value(manifest.metadata, "manifest.metadata"),
    }
    if manifest.schema == SCHEMA_VERSION_V2 or manifest.content_sha256 is not None:
        payload["content_sha256"] = manifest.content_sha256
    return payload


def _render_manifest_json(manifest: Manifest) -> str:
    return _json_text(_manifest_dict(manifest), compact=False).rstrip("\n") + "\n"


def build_schematic_snapshot(
    cellview: CellViewRef,
    electrical: SchematicElectrical,
    placement: SchematicPlacement,
    *,
    omissions: tuple[OmissionRecord, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> SchematicSnapshot:
    """Build a canonical schematic snapshot and its content-addressed manifest."""

    if not isinstance(cellview, CellViewRef):
        raise _error("cellview must be a CellViewRef")
    if cellview.view != "schematic":
        raise _error(f"unsupported schematic view {cellview.view!r}")
    if metadata is None:
        metadata = {}
    metadata_value = _canonical_value(_require_mapping(metadata, "metadata"), "metadata")
    if not isinstance(metadata_value, dict):  # pragma: no cover - guarded above
        raise _error("metadata must be a mapping")
    canonical_electrical = canonicalize_electrical(electrical)
    canonical_placement = canonicalize_placement(placement)
    canonical_omissions = _canonicalize_omissions(omissions)
    electrical_bytes = render_electrical_yaml(canonical_electrical).encode("utf-8")
    placement_bytes = render_placement_yaml(canonical_placement).encode("utf-8")
    electrical_sha256 = _sha256_bytes(electrical_bytes)
    placement_sha256 = _sha256_bytes(placement_bytes)
    file_bytes: dict[str, bytes] = {
        "circuit.yaml": electrical_bytes,
        "placement.yaml": placement_bytes,
    }
    files = {
        "circuit.yaml": electrical_sha256,
        "placement.yaml": placement_sha256,
    }
    if canonical_omissions:
        omissions_bytes = render_omissions_json(canonical_omissions).encode("utf-8")
        file_bytes["omissions.json"] = omissions_bytes
        files["omissions.json"] = _sha256_bytes(omissions_bytes)
    content_sha256 = compute_content_sha256(file_bytes)
    manifest = Manifest(
        schema=SCHEMA_VERSION_V2,
        view_type="schematic",
        cellview=cellview,
        electrical_sha256=electrical_sha256,
        placement_sha256=placement_sha256,
        full_sha256=compute_manifest_v2_full_sha256(
            view_type="schematic",
            cellview=cellview,
            files=files,
            omissions_count=len(canonical_omissions),
            metadata=metadata_value,
        ),
        files=files,
        omissions_count=len(canonical_omissions),
        metadata=metadata_value,
        content_sha256=content_sha256,
    )
    return SchematicSnapshot(
        manifest=manifest,
        electrical=canonical_electrical,
        placement=canonical_placement,
        omissions=canonical_omissions,
    )


def snapshot_dir_for(snapshots_root: Path, cellview: CellViewRef) -> Path:
    if not isinstance(snapshots_root, Path):
        raise _error("snapshots_root must be a Path")
    if not isinstance(cellview, CellViewRef):
        raise _error("cellview must be a CellViewRef")
    try:
        resolved_root = snapshots_root.resolve(strict=False)
        if resolved_root.exists() and not resolved_root.is_dir():
            raise _error("snapshots_root must resolve to a directory")
        candidate = (
            resolved_root / cellview.library / cellview.cell / cellview.view
        ).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _error("unable to resolve snapshots_root safely") from exc
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise _error("snapshot cellview path escapes snapshots_root") from exc
    if not relative.parts:
        raise _error("snapshot cellview path must be strictly inside snapshots_root")
    return candidate


def _canonical_snapshot(snapshot: SchematicSnapshot) -> SchematicSnapshot:
    if not isinstance(snapshot, SchematicSnapshot):
        raise _error("snapshot must be a SchematicSnapshot")
    manifest = snapshot.manifest
    if not isinstance(manifest, Manifest):
        raise _error("snapshot.manifest must be a Manifest")
    if manifest.schema not in SUPPORTED_SCHEMA_VERSIONS or manifest.view_type != "schematic":
        raise _error("snapshot manifest has unsupported schema or view_type")
    return build_schematic_snapshot(
        manifest.cellview,
        snapshot.electrical,
        snapshot.placement,
        omissions=snapshot.omissions,
        metadata=manifest.metadata,
    )


def _write_utf8(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def write_snapshot(snapshot: SchematicSnapshot, dest_dir: Path) -> Path:
    """Write all canonical snapshot files and remove stale omissions data."""

    if not isinstance(dest_dir, Path):
        raise _error("dest_dir must be a Path")
    canonical = _canonical_snapshot(snapshot)
    rendered_files = {
        "circuit.yaml": render_electrical_yaml(canonical.electrical),
        "placement.yaml": render_placement_yaml(canonical.placement),
        "manifest.json": _render_manifest_json(canonical.manifest),
    }
    if canonical.omissions:
        rendered_files["omissions.json"] = render_omissions_json(canonical.omissions)

    if dest_dir.is_symlink():
        raise _error(f"snapshot destination must not be a symlink: {dest_dir}")
    if dest_dir.exists() and not dest_dir.is_dir():
        raise _error(f"snapshot destination is not a directory: {dest_dir}")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    if dest_dir.exists():
        for child in dest_dir.iterdir():
            if child.is_symlink():
                raise _error(f"snapshot destination contains a symlink: {child.name}")
            if child.name in {"circuit.yaml", "placement.yaml", "omissions.json", "manifest.json"}:
                if not child.is_file():
                    raise _error(f"snapshot destination entry is not a file: {child.name}")

    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.staging-", dir=str(dest_dir.parent))
    )
    backup: Path | None = None
    try:
        for name, text in rendered_files.items():
            _write_utf8(staging / name, text)

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
            name
            for name in ("circuit.yaml", "placement.yaml", "omissions.json", "manifest.json")
            if (dest_dir / name).exists()
        }
        for name in existing_names:
            shutil.copy2(dest_dir / name, backup / name)

        replaced: list[str] = []
        commit_names = ["circuit.yaml", "placement.yaml"]
        if canonical.omissions:
            commit_names.append("omissions.json")
        commit_names.append("manifest.json")
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


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise SchemaValidationError("YAML mapping keys must be hashable") from exc
        if duplicate:
            raise SchemaValidationError(f"duplicate YAML mapping key {key!r}")
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _read_utf8(path: Path) -> bytes:
    try:
        if not path.is_file():
            raise SnapshotIntegrityError(f"missing snapshot file: {path.name}")
        return path.read_bytes()
    except OSError as exc:
        raise SnapshotIntegrityError(f"unable to read snapshot file {path}") from exc


def _load_yaml(path: Path) -> Any:
    raw = _read_utf8(path)
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


def _load_json(path: Path) -> Any:
    raw = _read_utf8(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaValidationError(f"{path.name} is not valid UTF-8") from exc

    def reject_constant(value: str) -> Any:
        raise SchemaValidationError(f"non-finite JSON constant {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SchemaValidationError(f"duplicate JSON mapping key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except SchemaValidationError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"invalid JSON in {path.name}") from exc


def _require_hash(value: Any, path: str) -> str:
    value = _require_text(value, path)
    if not _HASH_RE.fullmatch(value):
        raise _error(f"{path} must be a lowercase SHA-256 hex digest")
    return value


def _manifest_from_dict_strict(data: Any) -> Manifest:
    mapping = _require_mapping(data, "manifest")
    _check_keys(mapping, _MANIFEST_KEYS, {"schema", "view_type", "cellview", "electrical_sha256", "placement_sha256", "full_sha256", "files"}, "manifest")
    schema = _require_text(mapping["schema"], "manifest.schema")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise _error(f"unsupported manifest schema {schema!r}")
    view_type = _require_text(mapping["view_type"], "manifest.view_type")
    if view_type != "schematic":
        raise _error(f"unsupported snapshot view_type {view_type!r}")
    cellview_data = _require_mapping(mapping["cellview"], "manifest.cellview")
    _check_keys(cellview_data, {"library", "cell", "view"}, {"library", "cell", "view"}, "manifest.cellview")
    cellview = CellViewRef(
        library=_require_text(cellview_data["library"], "manifest.cellview.library"),
        cell=_require_text(cellview_data["cell"], "manifest.cellview.cell"),
        view=_require_text(cellview_data["view"], "manifest.cellview.view"),
    )
    if cellview.view != "schematic":
        raise _error(f"unsupported schematic cellview view {cellview.view!r}")
    files_data = _require_mapping(mapping["files"], "manifest.files")
    files: dict[str, str] = {}
    for key in sorted(files_data):
        if key not in {"circuit.yaml", "placement.yaml", "omissions.json"}:
            raise _error(f"manifest.files contains unsupported file {key!r}")
        files[key] = _require_hash(files_data[key], f"manifest.files.{key}")
    for required in ("circuit.yaml", "placement.yaml"):
        if required not in files:
            raise _error(f"manifest.files is missing {required!r}")
    omissions_count = _require_int(mapping.get("omissions_count", 0), "manifest.omissions_count", minimum=0)
    if omissions_count > 0 and "omissions.json" not in files:
        raise _error("manifest.files must include omissions.json when omissions_count > 0")
    if omissions_count == 0 and "omissions.json" in files:
        raise _error("manifest.files must omit omissions.json when omissions_count is zero")
    metadata = _canonical_value(
        _require_mapping(mapping.get("metadata", {}), "manifest.metadata"),
        "manifest.metadata",
    )
    if not isinstance(metadata, dict):  # pragma: no cover - guarded above
        raise _error("manifest.metadata must be a mapping")
    content_sha256: str | None = None
    if schema == SCHEMA_VERSION_V2:
        content_sha256 = _require_hash(mapping.get("content_sha256"), "manifest.content_sha256")
    elif "content_sha256" in mapping and mapping["content_sha256"] is not None:
        raise _error("oa-git/1 manifest must not declare content_sha256")
    return Manifest(
        schema=schema,
        view_type="schematic",
        cellview=cellview,
        electrical_sha256=_require_hash(mapping["electrical_sha256"], "manifest.electrical_sha256"),
        placement_sha256=_require_hash(mapping["placement_sha256"], "manifest.placement_sha256"),
        full_sha256=_require_hash(mapping["full_sha256"], "manifest.full_sha256"),
        files=files,
        omissions_count=omissions_count,
        metadata=metadata,
        content_sha256=content_sha256,
    )


def _read_manifest(
    snapshot_dir: Path, *, require_canonical: bool = True
) -> tuple[Manifest, bytes]:
    if not isinstance(snapshot_dir, Path) or not snapshot_dir.is_dir():
        raise SnapshotIntegrityError(f"snapshot directory does not exist: {snapshot_dir}")
    raw = _read_utf8(snapshot_dir / "manifest.json")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotIntegrityError("manifest.json is not valid UTF-8") from exc

    def reject_constant(value: str) -> Any:
        raise SnapshotIntegrityError(f"non-finite manifest JSON constant {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SnapshotIntegrityError(f"duplicate manifest JSON key {key!r}")
            result[key] = value
        return result

    try:
        data = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except SnapshotIntegrityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError("manifest.json is not valid JSON") from exc
    try:
        manifest = _manifest_from_dict_strict(data)
    except SchemaValidationError:
        raise
    if require_canonical and raw != _render_manifest_json(manifest).encode("utf-8"):
        raise SnapshotIntegrityError("manifest.json is not in canonical form")
    return manifest, raw


def _check_file_presence(snapshot_dir: Path, manifest: Manifest) -> None:
    listed = set(manifest.files)
    for name in ("circuit.yaml", "placement.yaml"):
        if name not in listed or not (snapshot_dir / name).is_file():
            raise SnapshotIntegrityError(f"missing snapshot file: {name}")
    omissions_path = snapshot_dir / "omissions.json"
    if manifest.omissions_count > 0:
        if not omissions_path.is_file():
            raise SnapshotIntegrityError("missing snapshot file: omissions.json")
    elif omissions_path.exists():
        raise SnapshotIntegrityError("stale omissions.json exists for an empty omission set")


def verify_snapshot_integrity(snapshot_dir: Path) -> Manifest:
    """Verify manifest, required files, and all declared SHA-256 digests."""

    manifest, _ = _read_manifest(snapshot_dir)
    _check_file_presence(snapshot_dir, manifest)
    circuit_bytes = _read_utf8(snapshot_dir / "circuit.yaml")
    placement_bytes = _read_utf8(snapshot_dir / "placement.yaml")
    circuit_sha = _sha256_bytes(circuit_bytes)
    placement_sha = _sha256_bytes(placement_bytes)
    if circuit_sha != manifest.electrical_sha256:
        raise SnapshotIntegrityError("circuit.yaml SHA-256 mismatch")
    if placement_sha != manifest.placement_sha256:
        raise SnapshotIntegrityError("placement.yaml SHA-256 mismatch")
    if manifest.files.get("circuit.yaml") != circuit_sha:
        raise SnapshotIntegrityError("manifest circuit.yaml digest mismatch")
    if manifest.files.get("placement.yaml") != placement_sha:
        raise SnapshotIntegrityError("manifest placement.yaml digest mismatch")
    file_bytes: dict[str, bytes] = {
        "circuit.yaml": circuit_bytes,
        "placement.yaml": placement_bytes,
    }
    omissions_sha256 = ""
    if manifest.omissions_count > 0:
        omissions_bytes = _read_utf8(snapshot_dir / "omissions.json")
        omissions_sha256 = _sha256_bytes(omissions_bytes)
        if omissions_sha256 != manifest.files.get("omissions.json"):
            raise SnapshotIntegrityError("omissions.json SHA-256 mismatch")
        file_bytes["omissions.json"] = omissions_bytes

    if manifest.schema == SCHEMA_VERSION_V2:
        expected_content = compute_content_sha256(file_bytes)
        if manifest.content_sha256 != expected_content:
            raise SnapshotIntegrityError("manifest content_sha256 mismatch")
        expected_full = compute_manifest_v2_full_sha256(
            view_type="schematic",
            cellview=manifest.cellview,
            files=manifest.files,
            omissions_count=manifest.omissions_count,
            metadata=manifest.metadata,
        )
    else:
        expected_full = _full_sha256(
            circuit_sha,
            placement_sha,
            cellview=manifest.cellview,
            omissions_count=manifest.omissions_count,
            omissions_sha256=omissions_sha256,
            metadata=manifest.metadata,
        )
    if manifest.full_sha256 != expected_full:
        raise SnapshotIntegrityError("manifest full_sha256 mismatch")
    return manifest


def _omissions_from_json(data: Any) -> tuple[OmissionRecord, ...]:
    values = _require_sequence(data, "omissions")
    records: list[OmissionRecord] = []
    for index, item in enumerate(values):
        mapping = _require_mapping(item, f"omissions[{index}]")
        _check_keys(mapping, {"kind", "identifier", "reason", "details"}, {"kind", "identifier", "reason"}, f"omissions[{index}]")
        details = _canonical_value(
            _require_mapping(mapping.get("details", {}), f"omissions[{index}].details"),
            f"omissions[{index}].details",
        )
        if not isinstance(details, dict):  # pragma: no cover - guarded above
            raise _error("omission details must be a mapping")
        records.append(
            OmissionRecord(
                kind=_require_text(mapping["kind"], f"omissions[{index}].kind"),
                identifier=_require_text(mapping["identifier"], f"omissions[{index}].identifier"),
                reason=_require_text(mapping["reason"], f"omissions[{index}].reason"),
                details=details,
            )
        )
    return _canonicalize_omissions(tuple(records))


def read_snapshot(snapshot_dir: Path, *, verify: bool = True) -> SchematicSnapshot:
    """Read a snapshot, validating schema and optionally content digests."""

    if type(verify) is not bool:
        raise _error("verify must be a bool")
    manifest = (
        verify_snapshot_integrity(snapshot_dir)
        if verify
        else _read_manifest(snapshot_dir, require_canonical=False)[0]
    )
    _check_file_presence(snapshot_dir, manifest)
    electrical_data = _load_yaml(snapshot_dir / "circuit.yaml")
    placement_data = _load_yaml(snapshot_dir / "placement.yaml")
    electrical = electrical_from_dict(_require_mapping(electrical_data, "circuit.yaml"))
    placement = placement_from_dict(_require_mapping(placement_data, "placement.yaml"))
    if manifest.omissions_count:
        omissions = _omissions_from_json(_load_json(snapshot_dir / "omissions.json"))
    else:
        omissions = ()
    if len(omissions) != manifest.omissions_count:
        raise SchemaValidationError(
            f"manifest omissions_count={manifest.omissions_count} does not match omissions.json"
        )
    return SchematicSnapshot(
        manifest=manifest,
        electrical=electrical,
        placement=placement,
        omissions=omissions,
    )


class SchematicViewHandler:
    """ViewHandler implementation for Cadence schematic snapshots."""

    view_type: ViewType = "schematic"
    capture_action: str = "capture_schematic"
    restore_action: str = "restore_schematic"

    def source_file_candidates(self, cell: str, view: str) -> tuple[str, ...]:
        cell_name = _require_text(cell, "cell")
        _require_text(view, "view")
        return (
            f"{cell_name}/schematic/sch.oa",
            f"{cell_name}/symbol/symbol.oa",
            f"{cell_name}/symbol/master.tag",
        )

    def read_snapshot(self, snapshot_dir: Path, *, verify: bool = True) -> SchematicSnapshot:
        return read_snapshot(snapshot_dir, verify=verify)

    def write_snapshot(self, snapshot: SchematicSnapshot, dest_dir: Path) -> Path:
        return write_snapshot(snapshot, dest_dir)

    def diff(self, old: SchematicSnapshot, new: SchematicSnapshot) -> Any:
        from .diff import diff_schematics

        return diff_schematics(old, new)

    def merge(
        self,
        base: SchematicSnapshot,
        ours: SchematicSnapshot,
        theirs: SchematicSnapshot,
    ) -> Any:
        from .merge import merge_schematics

        return merge_schematics(base, ours, theirs)


if not has_view_handler("schematic"):
    register_view_handler("schematic", SchematicViewHandler())


__all__ = [
    "quantize_coord",
    "quantize_xy",
    "compute_cdf_input_sha256",
    "resolve_cdf_callback_order",
    "canonicalize_electrical",
    "canonicalize_placement",
    "electrical_to_dict",
    "electrical_from_dict",
    "placement_to_dict",
    "placement_from_dict",
    "render_electrical_yaml",
    "render_placement_yaml",
    "render_omissions_json",
    "build_schematic_snapshot",
    "snapshot_dir_for",
    "write_snapshot",
    "read_snapshot",
    "verify_snapshot_integrity",
    "SchematicViewHandler",
]
