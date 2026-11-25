import json
from dataclasses import replace

from oa_git.diff import (
    ConnectivityChange,
    diff_schematics,
    format_diff_text,
)
from oa_git.model import (
    CellViewRef,
    Instance,
    InstancePlacement,
    Manifest,
    Net,
    NoteLabel,
    PinPlacement,
    Port,
    SCHEMA_VERSION,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    WireSegment,
)


def snapshot(
    electrical: SchematicElectrical | None = None,
    placement: SchematicPlacement | None = None,
    *,
    cell: str = "amp",
) -> SchematicSnapshot:
    cellview = CellViewRef("work", cell, "schematic")
    manifest = Manifest(
        schema=SCHEMA_VERSION,
        view_type="schematic",
        cellview=cellview,
        electrical_sha256="electrical",
        placement_sha256="placement",
        full_sha256="full",
        files={},
    )
    return SchematicSnapshot(
        manifest=manifest,
        electrical=electrical or SchematicElectrical(),
        placement=placement or SchematicPlacement(),
    )


def test_empty_diff_is_deterministic_and_json_serializable() -> None:
    result = diff_schematics(snapshot(), snapshot())

    assert result.is_empty
    assert not result.has_electrical_changes
    assert not result.has_placement_changes
    assert result.summary() == {
        "ports": 0,
        "nets": 0,
        "instances_added": 0,
        "instances_removed": 0,
        "master_changes": 0,
        "param_changes": 0,
        "derived_param_changes": 0,
        "connectivity_changes": 0,
        "instance_prop_changes": 0,
        "property_changes": 0,
        "constraint_changes": 0,
        "instance_moves": 0,
        "pin_changes": 0,
        "wires_added": 0,
        "wires_removed": 0,
        "labels_added": 0,
        "labels_removed": 0,
        "sheet_changes": 0,
    }
    json.dumps(result.to_dict())
    assert format_diff_text(result) == (
        "work/amp/schematic -> work/amp/schematic\n"
        "Electrical changes\n"
        "  none\n"
        "Placement changes\n"
        "  none"
    )


def test_moving_and_rerouting_are_placement_only() -> None:
    old_wire = WireSegment(points=((0.0, 0.0), (1.0, 0.0)), net="out")
    new_wire = WireSegment(points=((0.0, 0.0), (2.0, 0.0)), net="out")
    electrical = SchematicElectrical(
        instances=(Instance("u1", "lib", "cell", connections={"p": "out"}),),
        nets=(Net("out"),),
    )
    old = snapshot(
        electrical,
        SchematicPlacement(
            instances=(InstancePlacement("u1", (1.0, 2.0), "R0"),),
            wires=(old_wire,),
        ),
    )
    new = snapshot(
        electrical,
        SchematicPlacement(
            instances=(InstancePlacement("u1", (3.0, 4.0), "R90"),),
            wires=(new_wire,),
        ),
    )

    result = diff_schematics(old, new)

    assert result.electrical.is_empty
    assert result.is_placement_only
    assert result.has_placement_changes
    assert result.placement.instance_moves[0].change_type == "moved"
    assert result.placement.wires_added == (new_wire,)
    assert result.placement.wires_removed == (old_wire,)


def test_electrical_changes_cover_instances_params_types_and_connectivity() -> None:
    old_electrical = SchematicElectrical(
        ports=(Port("z", "output"), Port("a", "input")),
        nets=(Net("znet", False, 1), Net("old", True, 1)),
        instances=(
            Instance(
                "u2",
                "lib",
                "removed",
            ),
            Instance(
                "u1",
                "lib",
                "mos",
                params={"w": 1, "mode": "A", "flag": True},
                param_types={"w": "int", "mode": "cyclic", "flag": "boolean"},
                derived_params={"calc": 1},
                connections={"g": "old", "d": "znet"},
                props={"enabled": True},
            ),
        ),
        properties={"sim": True, "unchanged": "x"},
        constraints={"max": {"enabled": True}},
    )
    new_electrical = SchematicElectrical(
        ports=(Port("a", "output"), Port("b", "input")),
        nets=(Net("znet", True, 8), Net("new", False, 1)),
        instances=(
            Instance(
                "u3",
                "lib",
                "added",
            ),
            Instance(
                "u1",
                "lib2",
                "mos2",
                view="spectre",
                params={"w": 2, "new": 0.5, "flag": 1},
                param_types={"w": "float", "new": "float", "flag": "boolean"},
                derived_params={"calc": 2, "derived_new": "x"},
                connections={"g": "new", "d": "znet", "s": "new"},
                props={"enabled": 1, "kind": "analog"},
            ),
        ),
        properties={"sim": 0, "unchanged": "x", "new_prop": "yes"},
        constraints={"max": {"enabled": 1}, "new_constraint": False},
    )

    result = diff_schematics(snapshot(old_electrical), snapshot(new_electrical))
    electrical = result.electrical

    assert [change.name for change in electrical.ports] == ["a", "b", "z"]
    assert [change.change_type for change in electrical.ports] == [
        "modified",
        "added",
        "removed",
    ]
    assert [change.name for change in electrical.nets] == ["new", "old", "znet"]
    assert electrical.instances_added == ("u3",)
    assert electrical.instances_removed == ("u2",)
    assert electrical.master_changes[0].old_master == ("lib", "mos", "symbol")
    assert electrical.master_changes[0].new_master == ("lib2", "mos2", "spectre")

    params = {(change.param, change.old, change.new) for change in electrical.param_changes}
    assert ("mode", "A", None) in params
    assert ("new", None, 0.5) in params
    assert ("w", 1, 2) in params
    # bool and int compare differently even though Python normally equates them.
    assert ("flag", True, 1) in params
    width = next(change for change in electrical.param_changes if change.param == "w")
    assert width.old_type == "int"
    assert width.new_type == "float"

    assert [(change.param, change.old, change.new) for change in electrical.derived_param_changes] == [
        ("calc", 1, 2),
        ("derived_new", None, "x"),
    ]
    assert electrical.connectivity_changes == (
        ConnectivityChange("u1", "g", "old", "new"),
        ConnectivityChange("u1", "s", None, "new"),
    )
    assert {change.key for change in electrical.instance_prop_changes} == {"enabled", "kind"}
    assert {change.key for change in electrical.property_changes} == {
        "new_prop",
        "sim",
    }
    assert {change.key for change in electrical.constraint_changes} == {
        "max",
        "new_constraint",
    }
    assert not result.is_placement_only


def test_placement_pins_labels_sheet_and_duplicate_wires_are_deterministic() -> None:
    pin = PinPlacement(
        name="p1",
        net="n1",
        direction="input",
        lib="lib",
        cell="symbol",
        view="symbol",
        xy=(1.0, 1.0),
    )
    changed_pin = PinPlacement(
        name="p1",
        net="n2",
        direction="output",
        lib="lib",
        cell="symbol",
        view="symbol",
        xy=(2.0, 1.0),
    )
    removed_pin = PinPlacement(
        name="p2",
        net="n2",
        direction="input",
        lib="lib",
        cell="symbol",
        view="symbol",
        xy=(4.0, 1.0),
    )
    added_pin = PinPlacement(
        name="p3",
        net="n3",
        direction="input",
        lib="lib",
        cell="symbol",
        view="symbol",
        xy=(5.0, 1.0),
    )
    old_label = NoteLabel("old", (0.0, 0.0))
    new_label = NoteLabel("new", (0.0, 0.0))
    old_wire = WireSegment(((0.0, 0.0), (1.0, 0.0)), net="n1")
    new_wire = WireSegment(((0.0, 0.0), (2.0, 0.0)), net="n1")
    old = snapshot(
        placement=SchematicPlacement(
            pins=(removed_pin, pin),
            wires=(old_wire, old_wire),
            labels=(old_label,),
            sheet={"size": "A4", "grid": 0.1},
        )
    )
    new = snapshot(
        placement=SchematicPlacement(
            pins=(added_pin, changed_pin),
            wires=(old_wire, new_wire),
            labels=(new_label,),
            sheet={"grid": 0.2, "orientation": "landscape"},
        )
    )

    result = diff_schematics(old, new)
    placement = result.placement

    assert [(change.key, change.change_type) for change in placement.pin_changes] == [
        ("p1", "modified"),
        ("p2", "removed"),
        ("p3", "added"),
    ]
    assert placement.wires_added == (new_wire,)
    assert placement.wires_removed == (old_wire,)
    assert placement.labels_added == (new_label,)
    assert placement.labels_removed == (old_label,)
    assert [(change.key, change.change_type) for change in placement.sheet_changes] == [
        ("grid", "modified"),
        ("orientation", "added"),
        ("size", "removed"),
    ]


def test_format_diff_text_can_exclude_all_placement_content() -> None:
    old = snapshot(placement=SchematicPlacement(instances=(InstancePlacement("u", (0, 0)),)))
    new = snapshot(placement=SchematicPlacement(instances=(InstancePlacement("u", (1, 0)),)))
    result = diff_schematics(old, new)

    electrical_text = format_diff_text(result, electrical_only=True)
    full_text = format_diff_text(result)
    assert "Electrical changes" in electrical_text
    assert "Placement changes" not in electrical_text
    assert "Instance moves" in full_text
    assert "(moved)" in full_text


def test_duplicate_pin_names_are_compared_by_occurrence() -> None:
    first = PinPlacement("port", "n", "input", "lib", "pin", "symbol", (0, 0))
    second = PinPlacement("port", "n", "input", "lib", "pin", "symbol", (1, 0))
    old = snapshot(placement=SchematicPlacement(pins=(first, second)))
    new = snapshot(
        placement=SchematicPlacement(
            pins=(replace(first, xy=(2, 0)), second),
        )
    )

    result = diff_schematics(old, new)

    assert [(change.key, change.change_type) for change in result.placement.pin_changes] == [
        ("port[0]", "modified")
    ]


def test_cellview_change_is_visible_without_content_changes() -> None:
    old = snapshot(cell="original")
    new = snapshot(cell="restored-copy")

    result = diff_schematics(old, new)

    assert result.cellview_changed is True
    assert result.has_electrical_changes is False
    assert result.has_placement_changes is False
    assert result.is_empty is False
    assert result.to_dict()["cellview_changed"] is True
