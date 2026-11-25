from __future__ import annotations

from dataclasses import replace

import pytest

from oa_git.merge import (
    MergeConflict,
    MergeResult,
    format_merge_conflicts,
    merge_schematics,
)
from oa_git.model import (
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    NoteLabel,
    OmissionRecord,
    PinPlacement,
    Port,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    WireLabel,
    WireSegment,
)
from oa_git.snapshot import build_schematic_snapshot


CELLVIEW = CellViewRef("lib", "cell", "schematic")


def _snapshot(
    electrical: SchematicElectrical | None = None,
    placement: SchematicPlacement | None = None,
    *,
    metadata: dict | None = None,
    omissions: tuple[OmissionRecord, ...] = (),
):
    return build_schematic_snapshot(
        CELLVIEW,
        electrical or SchematicElectrical(),
        placement or SchematicPlacement(),
        metadata=metadata,
        omissions=omissions,
    )


def _base() -> SchematicSnapshot:
    return _snapshot(
        SchematicElectrical(
            ports=(Port("in", "input"),),
            nets=(Net("sig"),),
            instances=(
                Instance(
                    "M1",
                    "lib",
                    "nmos",
                    connections={"g": "sig"},
                    params={"w": 1, "l": 2},
                    param_types={"w": "int", "l": "int"},
                    derived_params={"area": 3},
                    props={"role": "amp"},
                ),
            ),
            properties={"owner": "base"},
            constraints={"match": True},
        ),
        SchematicPlacement(
            instances=(InstancePlacement("M1", (0.0, 0.0), "R0"),),
            pins=(
                PinPlacement("p1", "sig", "input", "basic", "ipin", "symbol", (0, 0)),
                PinPlacement("p2", "sig", "input", "basic", "ipin", "symbol", (1, 0)),
            ),
            wires=(WireSegment(((0, 0), (1, 0)), net="sig"),),
            labels=(NoteLabel("base", (0, 1)),),
            sheet={"width": 100, "height": 80},
        ),
        metadata={"base": "yes"},
    )


def test_disjoint_field_and_domain_edits_merge_cleanly() -> None:
    base = _base()
    ours_electrical = replace(
        base.electrical,
        instances=(
            replace(
                base.electrical.instances[0],
                lib="ours_lib",
                connections={"g": "sig", "s": "vss"},
                params={"w": 3, "l": 2},
                param_types={"w": "int", "l": "int"},
                props={"role": "amp", "side": "ours"},
            ),
            Instance("M3", "lib", "res"),
        ),
        properties={"owner": "base", "ours": True},
    )
    ours = _snapshot(ours_electrical, base.placement, metadata={"base": "yes", "ours": 1})

    theirs_electrical = replace(
        base.electrical,
        instances=(
            replace(
                base.electrical.instances[0],
                cell="pmos",
                params={"w": 1, "l": 4},
                param_types={"w": "int", "l": "int"},
                derived_params={"area": 9},
            ),
            Instance("M4", "lib", "cap"),
        ),
        constraints={"match": True, "theirs": "constraint"},
    )
    theirs_placement = replace(
        base.placement,
        instances=(InstancePlacement("M1", (4, 5), "R90"),),
        wires=base.placement.wires
        + (WireSegment(((1, 0), (2, 0)), net="sig"),),
        labels=base.placement.labels + (NoteLabel("theirs", (2, 2)),),
        sheet={"width": 120, "height": 80},
    )
    theirs = _snapshot(theirs_electrical, theirs_placement, metadata={"base": "yes", "theirs": 2})

    result = merge_schematics(base, ours, theirs)

    assert result.ok
    assert result.conflicts == ()
    assert result.snapshot is not None
    merged = result.snapshot
    instance = merged.electrical.instances[0]
    assert (instance.lib, instance.cell) == ("ours_lib", "pmos")
    assert instance.connections == {"g": "sig", "s": "vss"}
    assert instance.params == {"l": 4, "w": 3}
    assert instance.derived_params == {}
    assert instance.derived is not None
    assert instance.derived.status == "stale"
    assert instance.derived.input_sha256 is None
    assert instance.props == {"role": "amp", "side": "ours"}
    assert {item.name for item in merged.electrical.instances} == {"M1", "M3", "M4"}
    assert merged.electrical.properties == {"owner": "base", "ours": True}
    assert merged.electrical.constraints == {"match": True, "theirs": "constraint"}
    assert merged.placement.instances == (InstancePlacement("M1", (4.0, 5.0), "R90"),)
    assert len(merged.placement.wires) == 2
    assert len(merged.placement.labels) == 2
    assert merged.placement.sheet == {"height": 80, "width": 120}
    assert merged.manifest.cellview == ours.manifest.cellview
    assert merged.manifest.metadata == {"base": "yes", "ours": 1, "theirs": 2}


def test_convergent_edits_and_identical_multiset_additions_merge_once() -> None:
    base = _base()
    changed_instance = replace(
        base.electrical.instances[0],
        params={"w": 5, "l": 2},
    )
    changed_placement = replace(
        base.placement,
        wires=base.placement.wires
        + (WireSegment(((2, 0), (3, 0)), net="sig"),),
        labels=base.placement.labels + (NoteLabel("same", (3, 3)),),
    )
    ours = _snapshot(
        replace(base.electrical, instances=(changed_instance,)),
        changed_placement,
        metadata={"base": "yes", "same": True},
        omissions=(OmissionRecord("shape", "x", "unsupported"),),
    )
    theirs = _snapshot(
        replace(base.electrical, instances=(changed_instance,)),
        changed_placement,
        metadata={"base": "yes", "same": True},
        omissions=(OmissionRecord("shape", "x", "unsupported"),),
    )

    result = merge_schematics(base, ours, theirs)

    assert result.ok
    assert result.snapshot is not None
    assert result.snapshot.electrical.instances[0].params["w"] == 5
    assert len(result.snapshot.placement.wires) == 2
    assert len(result.snapshot.placement.labels) == 2
    assert result.snapshot.omissions == (OmissionRecord("shape", "x", "unsupported"),)


def test_modify_modify_conflicts_are_structured_and_sorted() -> None:
    base = _base()
    ours_electrical = replace(
        base.electrical,
        ports=(Port("in", "output"),),
        nets=(Net("sig", is_global=False, num_bits=2),),
        instances=(replace(base.electrical.instances[0], params={"w": 3, "l": 2}),),
    )
    theirs_electrical = replace(
        base.electrical,
        ports=(Port("in", "switch"),),
        nets=(Net("sig", is_global=False, num_bits=4),),
        instances=(replace(base.electrical.instances[0], params={"w": 4, "l": 2}),),
    )
    ours = _snapshot(ours_electrical, base.placement)
    theirs = _snapshot(theirs_electrical, base.placement)

    result = merge_schematics(base, ours, theirs)

    assert not result.ok
    assert result.snapshot is None
    paths = [conflict.path for conflict in result.conflicts]
    assert paths == sorted(paths)
    assert {
        "ports.in.direction",
        "nets.sig.num_bits",
        "instances.M1.params.w",
    } <= set(paths)
    assert all(conflict.reason == "modify/modify" for conflict in result.conflicts)


def test_delete_modify_and_conflicting_concurrent_additions() -> None:
    base = _base()
    ours_electrical = replace(
        base.electrical,
        ports=(),
        instances=base.electrical.instances + (Instance("M2", "lib", "a"),),
    )
    theirs_electrical = replace(
        base.electrical,
        ports=(Port("in", "output"),),
        instances=base.electrical.instances + (Instance("M2", "lib", "b"),),
    )
    result = merge_schematics(
        base,
        _snapshot(ours_electrical, base.placement),
        _snapshot(theirs_electrical, base.placement),
    )

    assert not result.ok
    assert result.snapshot is None
    assert result.conflicts[0].path == "instances.M2"
    assert result.conflicts[0].reason == "concurrent-addition"
    assert any(
        conflict.path == "ports.in" and conflict.reason == "delete/modify"
        for conflict in result.conflicts
    )


def test_bool_and_int_are_not_equal_during_merge() -> None:
    base = _base()
    ours_instance = replace(base.electrical.instances[0], params={"w": True, "l": 2})
    theirs_instance = replace(base.electrical.instances[0], params={"w": 2, "l": 4})
    result = merge_schematics(
        base,
        _snapshot(replace(base.electrical, instances=(ours_instance,)), base.placement),
        _snapshot(replace(base.electrical, instances=(theirs_instance,)), base.placement),
    )

    assert not result.ok
    conflict = next(item for item in result.conflicts if item.path.endswith("params.w"))
    assert conflict.base == 1 and type(conflict.base) is int
    assert conflict.ours is True and type(conflict.ours) is bool
    assert conflict.theirs == 2 and type(conflict.theirs) is int


def test_pins_use_stable_multi_pin_keys_and_fields_merge() -> None:
    base = _base()
    ours_pins = tuple(
        replace(pin, xy=(pin.xy[0] + 1, pin.xy[1])) for pin in base.placement.pins
    )
    theirs_pins = tuple(
        replace(pin, orient="R90" if pin.name == "p1" else pin.orient)
        for pin in base.placement.pins
    )
    result = merge_schematics(
        base,
        _snapshot(base.electrical, replace(base.placement, pins=ours_pins)),
        _snapshot(base.electrical, replace(base.placement, pins=theirs_pins)),
    )

    assert result.ok
    assert result.snapshot is not None
    merged = {pin.name: pin for pin in result.snapshot.placement.pins}
    assert merged["p1"].xy == (1.0, 0.0)
    assert merged["p1"].orient == "R90"
    assert merged["p2"].xy == (2.0, 0.0)


def test_placement_only_and_electrical_only_changes_do_not_conflict() -> None:
    base = _base()
    electrical_only = _snapshot(
        replace(
            base.electrical,
            properties={"owner": "base", "edited": "electrical"},
        ),
        base.placement,
    )
    placement_only = _snapshot(
        base.electrical,
        replace(
            base.placement,
            instances=(InstancePlacement("M1", (10, 20), "MX"),),
            sheet={"width": 200, "height": 80},
        ),
    )
    result = merge_schematics(base, electrical_only, placement_only)

    assert result.ok
    assert result.snapshot is not None
    assert result.snapshot.electrical.properties["edited"] == "electrical"
    assert result.snapshot.placement.instances[0] == InstancePlacement("M1", (10.0, 20.0), "MX")
    assert result.snapshot.placement.sheet == {"height": 80, "width": 200}


def test_result_and_conflict_formatting_are_deterministic() -> None:
    conflict = MergeConflict("electrical", "instances.M1.params.w", 1, True, 2, "modify/modify")
    result = MergeResult(False, None, (conflict,))

    assert conflict.to_dict() == {
        "domain": "electrical",
        "path": "instances.M1.params.w",
        "base": 1,
        "ours": True,
        "theirs": 2,
        "reason": "modify/modify",
    }
    assert result.to_dict()["ok"] is False
    assert result.to_dict()["snapshot"] is None
    text = format_merge_conflicts(result)
    assert "[electrical] instances.M1.params.w" in text
    assert "base=1" in text and "ours=True" in text and "theirs=2" in text
    assert format_merge_conflicts(MergeResult(True, None, ())) == "No merge conflicts."


def test_merge_rejects_mismatched_cellview_identity() -> None:
    base = _base()
    unrelated = build_schematic_snapshot(
        CellViewRef("lib", "other-cell", "schematic"),
        base.electrical,
        base.placement,
    )

    with pytest.raises(SchemaValidationError):
        merge_schematics(base, unrelated, base)


def test_duplicate_pin_names_merge_by_occurrence_without_losing_edits() -> None:
    first = PinPlacement("port", "sig", "input", "basic", "ipin", "symbol", (0, 0))
    second = PinPlacement("port", "sig", "input", "basic", "ipin", "symbol", (1, 0))
    base = _snapshot(placement=SchematicPlacement(pins=(first, second)))
    ours = _snapshot(
        placement=SchematicPlacement(pins=(replace(first, xy=(2, 0)), second))
    )
    theirs = _snapshot(
        placement=SchematicPlacement(pins=(first, replace(second, orient="R90")))
    )

    result = merge_schematics(base, ours, theirs)

    assert result.ok
    assert result.snapshot is not None
    merged = result.snapshot.placement.pins
    assert any(pin.xy == (2.0, 0.0) and pin.orient == "R0" for pin in merged)
    assert any(pin.xy == (1.0, 0.0) and pin.orient == "R90" for pin in merged)


def test_single_branch_legacy_primary_and_derived_edit_preserves_derived() -> None:
    base = _base()
    # Ours does NOT edit primary params/master (only edits props)
    ours = _snapshot(
        replace(
            base.electrical,
            instances=(
                replace(
                    base.electrical.instances[0],
                    props={"role": "amp", "note": "ours"},
                ),
            ),
        ),
        base.placement,
    )
    # Theirs edits primary + legacy derived together
    theirs = _snapshot(
        replace(
            base.electrical,
            instances=(
                replace(
                    base.electrical.instances[0],
                    params={"w": 1, "l": 4},
                    param_types={"w": "int", "l": "int"},
                    derived_params={"area": 9},
                ),
            ),
        ),
        base.placement,
    )
    result = merge_schematics(base, ours, theirs)
    assert result.ok
    assert result.snapshot is not None
    merged_inst = result.snapshot.electrical.instances[0]
    assert merged_inst.params == {"l": 4, "w": 1}
    assert merged_inst.props == {"note": "ours", "role": "amp"}
    assert merged_inst.derived_params == {"area": 9}
    assert merged_inst.derived is not None
    assert merged_inst.derived.status == "valid"
    assert merged_inst.derived.source == "legacy-import"
