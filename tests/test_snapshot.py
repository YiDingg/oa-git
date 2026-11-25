from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

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
    SnapshotIntegrityError,
    WireLabel,
    WireSegment,
)
from oa_git.snapshot import (
    build_schematic_snapshot,
    canonicalize_electrical,
    canonicalize_placement,
    electrical_from_dict,
    electrical_to_dict,
    placement_from_dict,
    placement_to_dict,
    quantize_coord,
    read_snapshot,
    render_electrical_yaml,
    render_omissions_json,
    render_placement_yaml,
    snapshot_dir_for,
    verify_snapshot_integrity,
    write_snapshot,
)


def _electrical(*, reverse: bool = False) -> SchematicElectrical:
    ports = (
        Port("out", "output"),
        Port("in", "input"),
    )
    nets = (
        Net("signal"),
        Net("vss", is_global=True),
    )
    instances = (
        Instance(
            "m2",
            "pdklib",
            "nmos",
            params={"w": 2.0, "enabled": True},
            param_types={"w": "float", "enabled": "boolean"},
            connections={"s": "vss", "d": "signal"},
        ),
        Instance(
            "m1",
            "pdklib",
            "pmos",
            params={"w": 1, "enabled": False},
            param_types={"w": "int", "enabled": "boolean"},
            connections={"d": "signal", "s": "vss"},
        ),
    )
    if reverse:
        ports = tuple(reversed(ports))
        nets = tuple(reversed(nets))
        instances = tuple(reversed(instances))
    return SchematicElectrical(
        ports=ports,
        nets=nets,
        instances=instances,
        properties={"count": 1, "enabled": True},
        constraints={"order": [2, True], "name": "demo"},
    )


def _placement(*, reverse: bool = False) -> SchematicPlacement:
    wires = (
        WireSegment(
            points=((3.0000004, 1.0), (1.0, 1.0000004)),
            net="signal",
            width=-0.0,
            labels=(WireLabel("z", (2.0, 1.0)), WireLabel("a", (1.0, 1.0))),
        ),
        WireSegment(points=((0.0, 0.0), (0.0, 2.0)), net=None),
    )
    labels = (
        NoteLabel("second", (2.0, 1.0)),
        NoteLabel("first", (1.0, 1.0)),
    )
    instances = (
        InstancePlacement("m2", (2.12345678, -0.0), "R90"),
        InstancePlacement("m1", (1.0, 1.0), "R0"),
    )
    pins = (
        PinPlacement("p2", "vss", "input", "pdklib", "nmos", "symbol", (2.0, 0.0)),
        PinPlacement("p1", "signal", "output", "pdklib", "nmos", "symbol", (1.0, 0.0)),
    )
    if reverse:
        wires = tuple(reversed(wires))
        labels = tuple(reversed(labels))
        instances = tuple(reversed(instances))
        pins = tuple(reversed(pins))
    return SchematicPlacement(
        instances=instances,
        pins=pins,
        wires=wires,
        labels=labels,
        sheet={"height": 100, "width": 200},
    )


def _snapshot(*, reverse: bool = False, omissions: tuple[OmissionRecord, ...] = ()):
    return build_schematic_snapshot(
        CellViewRef("demo_lib", "amp", "schematic"),
        _electrical(reverse=reverse),
        _placement(reverse=reverse),
        omissions=omissions,
        metadata={"z": 2, "a": True},
    )


def test_canonical_output_is_deterministic_under_shuffled_input(tmp_path: Path) -> None:
    first = _snapshot()
    second = _snapshot(reverse=True)
    assert render_electrical_yaml(first.electrical) == render_electrical_yaml(second.electrical)
    assert render_placement_yaml(first.placement) == render_placement_yaml(second.placement)
    assert first.manifest == second.manifest

    left = write_snapshot(first, tmp_path / "left")
    right = write_snapshot(second, tmp_path / "right")
    for name in ("manifest.json", "circuit.yaml", "placement.yaml"):
        assert (left / name).read_bytes() == (right / name).read_bytes()


def test_yaml_and_json_roundtrip_preserves_model() -> None:
    snapshot = _snapshot()
    assert electrical_from_dict(electrical_to_dict(snapshot.electrical)) == snapshot.electrical
    assert placement_from_dict(placement_to_dict(snapshot.placement)) == snapshot.placement

    omissions = (
        OmissionRecord("z-kind", "z-id", "z-reason", {"flag": True}),
        OmissionRecord("a-kind", "a-id", "a-reason", {"count": 1}),
    )
    rendered = render_omissions_json(omissions)
    loaded = json.loads(rendered)
    assert [item["kind"] for item in loaded] == ["a-kind", "z-kind"]
    assert loaded[0]["details"]["count"] == 1
    assert type(loaded[0]["details"]["count"]) is int


def test_bool_int_float_and_negative_zero_types_are_preserved() -> None:
    electrical = SchematicElectrical(
        instances=(
            Instance(
                "x",
                "lib",
                "cell",
                params={"bool": True, "int": 1, "float": 1.0, "zero": -0.0},
                param_types={"bool": "boolean", "int": "int", "float": "float", "zero": "float"},
            ),
        ),
        properties={"bool": False, "int": 2},
    )
    rendered = render_electrical_yaml(electrical)
    parsed = yaml.safe_load(rendered)
    values = parsed["instances"][0]["params"]
    assert type(values["bool"]) is bool
    assert type(values["int"]) is int
    assert type(values["float"]) is float
    assert values["zero"] == 0.0
    assert not math.copysign(1.0, values["zero"]) < 0
    roundtrip = electrical_from_dict(parsed)
    values = roundtrip.instances[0].params
    assert type(values["bool"]) is bool
    assert type(values["int"]) is int
    assert type(values["float"]) is float
    assert values["zero"] == 0.0


def test_write_read_and_omission_create_remove(tmp_path: Path) -> None:
    omission = OmissionRecord("unsupported", "obj-1", "not captured", {"active": True})
    directory = write_snapshot(_snapshot(omissions=(omission,)), tmp_path / "snapshot")
    assert (directory / "omissions.json").is_file()
    loaded = read_snapshot(directory)
    assert loaded.omissions == (omission,)
    assert verify_snapshot_integrity(directory).omissions_count == 1

    write_snapshot(_snapshot(), directory)
    assert not (directory / "omissions.json").exists()
    assert read_snapshot(directory).omissions == ()
    assert verify_snapshot_integrity(directory).omissions_count == 0


def test_integrity_tampering_and_missing_file_fail_closed(tmp_path: Path) -> None:
    directory = write_snapshot(_snapshot(), tmp_path / "snapshot")
    circuit = directory / "circuit.yaml"
    circuit.write_bytes(circuit.read_bytes().replace(b"signal", b"tampered", 1))
    with pytest.raises(SnapshotIntegrityError):
        verify_snapshot_integrity(directory)

    circuit.unlink()
    with pytest.raises(SnapshotIntegrityError):
        read_snapshot(directory)


@pytest.mark.parametrize("field, value", [("schema", "oa-git/99"), ("view_type", "layout")])
def test_manifest_schema_and_view_type_are_rejected(tmp_path: Path, field: str, value: str) -> None:
    directory = write_snapshot(_snapshot(), tmp_path / "snapshot")
    manifest_path = directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SchemaValidationError):
        verify_snapshot_integrity(directory)


def test_snapshot_dir_for_uses_cellview_components(tmp_path: Path) -> None:
    cellview = CellViewRef("lib", "cell", "schematic")
    assert snapshot_dir_for(tmp_path, cellview) == tmp_path / "lib" / "cell" / "schematic"


@pytest.mark.parametrize(
    "component",
    [".", "..", " lib", "cell ", "a/b", "a\\b", "a\x00b", "a\n"],
)
def test_cellview_ref_rejects_path_and_control_components(component: str) -> None:
    with pytest.raises(SchemaValidationError):
        CellViewRef(component, "cell", "schematic")
    with pytest.raises(SchemaValidationError):
        CellViewRef.parse(f"{component}/cell/schematic")


def test_snapshot_dir_for_rejects_resolved_symlink_escape(tmp_path: Path) -> None:
    snapshots_root = tmp_path / "snapshots"
    snapshots_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (snapshots_root / "lib").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SchemaValidationError):
        snapshot_dir_for(snapshots_root, CellViewRef("lib", "cell", "schematic"))


def test_snapshot_integrity_binds_identity_metadata_and_omissions(tmp_path: Path) -> None:
    omission = OmissionRecord("unsupported", "obj", "not captured")
    directory = write_snapshot(
        _snapshot(omissions=(omission,)), tmp_path / "snapshot"
    )
    original = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    def write_manifest(payload: dict) -> None:
        (directory / "manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )

    tampered = json.loads(json.dumps(original))
    tampered["cellview"]["cell"] = "other"
    write_manifest(tampered)
    with pytest.raises(SnapshotIntegrityError):
        verify_snapshot_integrity(directory)

    tampered = json.loads(json.dumps(original))
    tampered["metadata"]["a"] = False
    write_manifest(tampered)
    with pytest.raises(SnapshotIntegrityError):
        verify_snapshot_integrity(directory)

    tampered = json.loads(json.dumps(original))
    tampered["omissions_count"] = 0
    tampered["files"].pop("omissions.json")
    (directory / "omissions.json").unlink()
    write_manifest(tampered)
    with pytest.raises(SnapshotIntegrityError):
        verify_snapshot_integrity(directory)


def test_write_snapshot_stages_before_commit_and_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import oa_git.snapshot as snapshot_module

    destination = write_snapshot(_snapshot(), tmp_path / "snapshot")
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    original_write = snapshot_module._write_utf8
    calls = 0

    def fail_during_staging(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        original_write(path, text)
        if calls == 2:
            raise OSError("injected staging failure")

    monkeypatch.setattr(snapshot_module, "_write_utf8", fail_during_staging)
    with pytest.raises(OSError):
        write_snapshot(_snapshot(), destination)
    assert before == {path.name: path.read_bytes() for path in destination.iterdir()}

    monkeypatch.setattr(snapshot_module, "_write_utf8", original_write)
    target = tmp_path / "target"
    target.mkdir()
    symlink_destination = tmp_path / "symlink-destination"
    symlink_destination.symlink_to(target, target_is_directory=True)
    with pytest.raises(SchemaValidationError):
        write_snapshot(_snapshot(), symlink_destination)

    (destination / "circuit.yaml").unlink()
    (destination / "circuit.yaml").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SchemaValidationError):
        write_snapshot(_snapshot(), destination)


def test_closed_wire_reversal_has_one_canonical_representation() -> None:
    forward = WireSegment(((0, 0), (2, 0), (2, 2), (0, 0)), net="n")
    reverse = WireSegment(tuple(reversed(forward.points)), net="n")
    first = canonicalize_placement(SchematicPlacement(wires=(forward,)))
    second = canonicalize_placement(SchematicPlacement(wires=(reverse,)))
    assert first == second
    assert (
        build_schematic_snapshot(
            CellViewRef("lib", "cell", "schematic"),
            SchematicElectrical(),
            SchematicPlacement(wires=(forward,)),
        ).manifest.placement_sha256
        == build_schematic_snapshot(
            CellViewRef("lib", "cell", "schematic"),
            SchematicElectrical(),
            SchematicPlacement(wires=(reverse,)),
        ).manifest.placement_sha256
    )


def test_canonical_value_rejects_recursive_containers() -> None:
    constraints: dict[str, object] = {}
    constraints["self"] = constraints
    with pytest.raises(SchemaValidationError):
        build_schematic_snapshot(
            CellViewRef("lib", "cell", "schematic"),
            SchematicElectrical(constraints=constraints),
            SchematicPlacement(),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SchematicElectrical(ports=(Port("p", "bad"),)),
        lambda: SchematicElectrical(ports=(Port("p"), Port("p"))),
        lambda: SchematicElectrical(nets=(Net("n"), Net("n"))),
        lambda: SchematicElectrical(instances=(Instance("x", "l", "c"), Instance("x", "l", "c"))),
        lambda: SchematicElectrical(instances=(Instance("x", "l", "c", param_types={"p": "bad"}),)),
        lambda: SchematicElectrical(instances=(Instance("x", "l", "c", params={"p": float("nan")}),)),
    ],
)
def test_electrical_validation_is_fail_closed(factory) -> None:
    with pytest.raises(SchemaValidationError):
        canonicalize_electrical(factory())


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SchematicPlacement(instances=(InstancePlacement("x", (0, 0), "bad"),)),
        lambda: SchematicPlacement(wires=(WireSegment(((0, 0),)),)),
        lambda: SchematicPlacement(wires=(WireSegment(((0, 0), (float("inf"), 1))),)),
        lambda: SchematicPlacement(pins=(PinPlacement("p", "n", "bad", "l", "c", "v", (0, 0)),)),
    ],
)
def test_placement_validation_is_fail_closed(factory) -> None:
    with pytest.raises(SchemaValidationError):
        canonicalize_placement(factory())


def test_unsupported_schematic_view_is_rejected() -> None:
    with pytest.raises(SchemaValidationError):
        build_schematic_snapshot(
            CellViewRef("lib", "cell", "layout"),
            SchematicElectrical(),
            SchematicPlacement(),
        )


def test_quantization_rejects_non_finite_values() -> None:
    assert quantize_coord(-0.0) == 0.0
    assert quantize_coord(1.23456789) == 1.234568
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SchemaValidationError):
            quantize_coord(value)
