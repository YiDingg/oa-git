"""Offline unit tests for layout_model.py and layout_snapshot.py (Manifest v2 + JSONL)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from oa_git.layout_model import (
    CellViewRef,
    LAYOUT_MANIFEST_SCHEMA,
    LAYOUT_VIEW_TYPE,
    LayoutBlockage,
    LayoutBoundary,
    LayoutEllipse,
    LayoutFigGroup,
    LayoutInstance,
    LayoutLabel,
    LayoutLine,
    LayoutManifest,
    LayoutMosaic,
    LayoutNet,
    LayoutPath,
    LayoutPathSeg,
    LayoutPin,
    LayoutPolygon,
    LayoutRect,
    LayoutVia,
    OmissionRecord,
    SchemaValidationError,
    SnapshotIntegrityError,
)
from oa_git.layout_snapshot import (
    build_layout_snapshot,
    canonical_json_bytes,
    canonicalize_layout_object,
    compute_content_sha256,
    compute_layout_full_sha256,
    encode_layout_jsonl,
    layout_snapshot_dir_for,
    quantize_layout_coord,
    read_layout_snapshot,
    sha256_bytes,
    verify_layout_snapshot_integrity,
    write_layout_snapshot,
)


CV = CellViewRef("work", "inv2t", "layout")


def _sample_objects():
    return (
        LayoutNet(name="VDD", is_global=False, num_bits=1, sig_type="power"),
        LayoutNet(name="IN", is_global=False, num_bits=1, sig_type="signal"),
        LayoutPin(
            name="IN",
            pin_name="P__IN_0",
            net="IN",
            direction="input",
            layer="M1",
            purpose="pin",
            shape_kind="rect",
            bbox=((0.2, 0.3), (0.0, 0.1)),  # reversed corners -> normalized
            access_dir=("right", "left", "top", "left"),  # deduped + sorted
        ),
        LayoutInstance(
            name="MP0",
            lib="tsmcN65",
            cell="pch",
            view="layout",
            xy=(0.0, 1.5000004),
            orient="R0",
            is_pcell=True,
            pcell_params={"w": "900n", "l": "60n", "fingers": "2"},
            pcell_param_types={"w": "string", "l": "string", "fingers": "string"},
            connections={"D": "OUT", "G": "IN", "S": "VDD", "B": "VDD"},
            props={
                "lxComponentType": "HUGE_STATIC_PDK_JUNK_TEXT",
                "description": "STATIC_DESC",
                "userTag": "matched_pair",
            },
        ),
        LayoutInstance(
            name="MP1",
            lib="tsmcN65",
            cell="pch",
            view="layout",
            xy=(2.0, 1.5),
            orient="MY",
            is_pcell=True,
            pcell_params={"l": "60n", "w": "900n", "fingers": "2"},  # same PCell def
            pcell_param_types={"l": "string", "w": "string", "fingers": "string"},
            connections={"D": "OUT", "G": "IN", "S": "VDD", "B": "VDD"},
            props={"userTag": "matched_pair"},  # repeated props def
        ),
        LayoutVia(
            via_def="M2_M1",
            origin=(1.0, 0.5),
            orient="R0",
            net="VDD",
            params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.04]},
        ),
        LayoutVia(
            via_def="M2_M1",
            origin=(2.0, 0.5),
            orient="R0",
            net="VDD",
            params={"layer1Enc": (0.03, 0.04), "cutColumns": 1, "cutRows": 2},  # repeated via def
        ),
        LayoutRect(layer="M1", purpose="drawing", bbox=((1.0, 0.2), (0.0, 0.0)), net="IN"),
        LayoutPathSeg(
            layer="M1",
            purpose="drawing",
            begin_pt=(2.0, 0.0),
            end_pt=(0.0, 0.0),  # reversed endpoints -> normalized
            width=0.1,
            begin_style="extend",
            end_style="truncate",
            begin_ext=0.05,
            end_ext=0.0,
            net="IN",
        ),
        LayoutPath(
            layer="M2",
            purpose="drawing",
            points=((0.0, 0.0), (1.0, 0.0), (1.0, 2.0)),
            width=0.12,
            net="VDD",
        ),
        LayoutPolygon(
            layer="PO",
            purpose="drawing",
            points=((1.0, 1.0), (0.0, 1.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),  # rotated + closed
            net="IN",
        ),
        LayoutEllipse(layer="M3", purpose="drawing", bbox=((0.0, 0.0), (0.4, 0.4))),
        LayoutLine(layer="text", purpose="drawing", points=((0.0, 0.0), (1.0, 1.0))),
        LayoutLabel(text="IN", xy=(0.1, 0.2), layer="M1", purpose="pin", net="IN"),
        LayoutMosaic(
            name="CAP_ARR",
            lib="work",
            cell="unit_cap",
            view="layout",
            xy=(5.0, 0.0),
            rows=2,
            columns=4,
            u_x=1.2,
            u_y=1.2,
        ),
        LayoutBlockage(
            blockage_type="routing",
            layer="M2",
            points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        ),
        LayoutBoundary(
            boundary_kind="prBoundary",
            points=((5.0, 5.0), (0.0, 5.0), (0.0, 0.0), (5.0, 0.0)),
        ),
        LayoutFigGroup(name="DIFF_PAIR", origin=(0.0, 0.0), orient="R0"),
    )


def test_determinism_permutation_and_negative_zero(tmp_path: Path) -> None:
    objs = _sample_objects()
    snap1 = build_layout_snapshot(
        CV,
        objs,
        cell_properties={"customRule": "matched", "gridSpacing": 0.005, "instancesLastChanged": "now"},
        metadata={"dbu_per_uu": 1000.0, "cellview_type": "maskLayout"},
    )
    snap2 = build_layout_snapshot(
        CV,
        tuple(reversed(objs)),
        cell_properties={"customRule": "matched"},
        metadata={"cellview_type": "maskLayout", "dbu_per_uu": 1000.0},
    )

    dir1 = write_layout_snapshot(snap1, tmp_path / "run1")
    dir2 = write_layout_snapshot(snap2, tmp_path / "run2")

    assert snap1.manifest == snap2.manifest
    assert snap1.manifest.schema == LAYOUT_MANIFEST_SCHEMA
    assert snap1.manifest.view_type == LAYOUT_VIEW_TYPE
    assert snap1.manifest.electrical_sha256 is None
    assert snap1.manifest.placement_sha256 is None
    assert snap1.manifest.content_sha256 is not None

    for fname in ("manifest.json", "objects.jsonl", "definitions.jsonl"):
        assert (dir1 / fname).read_bytes() == (dir2 / fname).read_bytes()

    # Quantization normalizes -0.0 -> 0.0
    assert quantize_layout_coord(-0.0) == 0.0
    assert repr(quantize_layout_coord(-0.0)) == "0.0"


def test_pdk_junk_stripped_and_definitions_deduplicated(tmp_path: Path) -> None:
    snap = build_layout_snapshot(CV, _sample_objects())
    out = write_layout_snapshot(snap, tmp_path / "snap")

    objs_text = (out / "objects.jsonl").read_text(encoding="utf-8")
    defs_text = (out / "definitions.jsonl").read_text(encoding="utf-8")

    # Static PDK junk metadata must be completely absent from serialized snapshot.
    assert "lxComponentType" not in objs_text
    assert "lxComponentType" not in defs_text
    assert "STATIC_DESC" not in objs_text
    assert "gridSpacing" not in objs_text

    # Repeated PCell params, via params, and props are deduplicated into definitions.jsonl.
    def_records = [json.loads(line) for line in defs_text.splitlines()]
    kinds = {rec["kind"] for rec in def_records}
    assert kinds == {"pcell_params", "via_params", "props"}
    assert '"pcell_def":' in objs_text
    assert '"params_def":' in objs_text
    assert '"props_def":' in objs_text

    # Roundtrip read resolves definitions seamlessly back to full domain objects.
    loaded = read_layout_snapshot(out, verify=True)
    assert loaded == snap
    mp0 = [o for o in loaded.objects if isinstance(o, LayoutInstance) and o.name == "MP0"][0]
    assert mp0.pcell_params == {"fingers": "2", "l": "60n", "w": "900n"}
    assert mp0.props == {"userTag": "matched_pair"}


def test_geometry_canonicalization_rules() -> None:
    # Polygon ring rotation & closing vertex removal preserves winding order
    p1 = canonicalize_layout_object(
        LayoutPolygon("M1", "drawing", ((1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0), (1.0, 0.0)))
    )
    p2 = canonicalize_layout_object(
        LayoutPolygon("M1", "drawing", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)))
    )
    assert p1 == p2
    assert isinstance(p1, LayoutPolygon)
    assert p1.points == ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))

    # PathSeg endpoint reversal swaps begin/end styles & extensions
    seg = canonicalize_layout_object(
        LayoutPathSeg(
            layer="M1",
            purpose="drawing",
            begin_pt=(5.0, 1.0),
            end_pt=(1.0, 1.0),
            width=0.2,
            begin_style="extend",
            end_style="truncate",
            begin_ext=0.04,
            end_ext=0.0,
        )
    )
    assert isinstance(seg, LayoutPathSeg)
    assert seg.begin_pt == (1.0, 1.0)
    assert seg.end_pt == (5.0, 1.0)
    assert seg.begin_style == "truncate"
    assert seg.end_style == "extend"
    assert seg.begin_ext == 0.0
    assert seg.end_ext == 0.04


def test_optional_definitions_and_omissions_cleanup(tmp_path: Path) -> None:
    simple = build_layout_snapshot(
        CV,
        (LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0))),),
    )
    dest = write_layout_snapshot(simple, tmp_path / "snap")
    assert not (dest / "definitions.jsonl").exists()
    assert not (dest / "omissions.json").exists()
    assert set(simple.manifest.files) == {"objects.jsonl"}

    # Overwrite with a snapshot that has definitions & omissions
    rich = build_layout_snapshot(
        CV,
        _sample_objects(),
        omissions=(OmissionRecord(kind="shape", identifier="arc_0", reason="unsupported_shape"),),
    )
    write_layout_snapshot(rich, dest)
    assert (dest / "definitions.jsonl").is_file()
    assert (dest / "omissions.json").is_file()
    assert verify_layout_snapshot_integrity(dest).omissions_count == 1

    # Overwrite back to simple -> stale optional files must be removed
    write_layout_snapshot(simple, dest)
    assert not (dest / "definitions.jsonl").exists()
    assert not (dest / "omissions.json").exists()
    assert verify_layout_snapshot_integrity(dest).omissions_count == 0


def test_manifest_v2_digests_match_view_contract_formula(tmp_path: Path) -> None:
    snap = build_layout_snapshot(
        CV,
        _sample_objects(),
        metadata={"cellview_type": "maskLayout", "dbu_per_uu": 1000.0},
    )
    dest = write_layout_snapshot(snap, tmp_path / "snap")

    # Verify exact view_contract.py content_sha256 & full_sha256 algorithm independently
    file_bytes = {
        name: (dest / name).read_bytes()
        for name in sorted(snap.manifest.files)
    }
    h = hashlib.sha256()
    for name in sorted(file_bytes):
        if name in ("manifest.json", "omissions.json"):
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(file_bytes[name])
        h.update(b"\x00")
    assert snap.manifest.content_sha256 == h.hexdigest()

    binding = {
        "schema": "oa-git/2",
        "view_type": "layout",
        "cellview": CV.to_dict(),
        "files": dict(sorted(snap.manifest.files.items())),
        "omissions_count": 0,
        "metadata": dict(sorted(snap.manifest.metadata.items())),
    }
    expected_full = sha256_bytes(canonical_json_bytes(binding))
    assert snap.manifest.full_sha256 == expected_full
    assert compute_content_sha256(file_bytes) == snap.manifest.content_sha256
    assert (
        compute_layout_full_sha256(
            cellview=CV,
            files=snap.manifest.files,
            omissions_count=0,
            metadata=snap.manifest.metadata,
        )
        == expected_full
    )


def test_integrity_verification_rejects_tampering_and_stale_files(tmp_path: Path) -> None:
    snap = build_layout_snapshot(
        CV,
        (LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0))),),
    )
    dest = write_layout_snapshot(snap, tmp_path / "snap")

    # Stale unreferenced omissions.json rejected
    (dest / "omissions.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError, match="Stale unreferenced"):
        verify_layout_snapshot_integrity(dest)
    (dest / "omissions.json").unlink()

    # Tampered objects.jsonl rejected
    (dest / "objects.jsonl").write_text(
        '{"bbox":[[0.0,0.0],[2.0,2.0]],"layer":"M1","purpose":"drawing","type":"rect"}\n',
        encoding="utf-8",
    )
    with pytest.raises(SnapshotIntegrityError, match="SHA-256 mismatch"):
        verify_layout_snapshot_integrity(dest)


def test_validation_rejects_invalid_inputs() -> None:
    with pytest.raises(SchemaValidationError):
        quantize_layout_coord(float("nan"))

    with pytest.raises(SchemaValidationError):
        canonicalize_layout_object(LayoutNet(name="N1", sig_type="invalid_sig"))

    with pytest.raises(SchemaValidationError):
        build_layout_snapshot(
            CV,
            (
                LayoutInstance(name="M1", lib="pdk", cell="nch"),
                LayoutInstance(name="M1", lib="pdk", cell="nch"),
            ),
        )

    with pytest.raises(SchemaValidationError):
        build_layout_snapshot(
            CV,
            (
                LayoutBoundary("prBoundary", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0))),
                LayoutBoundary("prBoundary", ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0))),
            ),
        )

    with pytest.raises(SchemaValidationError):
        LayoutManifest.from_dict(
            {
                "schema": "oa-git/2",
                "view_type": "layout",
                "cellview": CV.to_dict(),
                "electrical_sha256": "a" * 64,  # must be null for layout
                "placement_sha256": None,
                "content_sha256": "b" * 64,
                "full_sha256": "c" * 64,
                "files": {"objects.jsonl": "d" * 64},
            }
        )

    assert layout_snapshot_dir_for(Path("/snaps"), CV) == Path("/snaps/work/inv2t/layout")


def test_multiset_fidelity_preserves_duplicate_overlapping_vias_and_shapes(tmp_path: Path) -> None:
    """Identical overlapping vias and shapes must NEVER be silently deduplicated."""

    via = LayoutVia(
        via_def="M2_M1",
        origin=(1.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.03]},
    )
    rect = LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 0.2)), net="VDD")
    seg = LayoutPathSeg(
        layer="M2",
        purpose="drawing",
        begin_pt=(0.0, 0.0),
        end_pt=(1.0, 0.0),
        width=0.1,
        net="VDD",
    )
    poly = LayoutPolygon(
        layer="PO",
        purpose="drawing",
        points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
    )

    snap_single = build_layout_snapshot(CV, (via, rect, seg, poly))
    snap_dup = build_layout_snapshot(CV, (via, via, rect, rect, seg, seg, poly, poly))

    dir_single = write_layout_snapshot(snap_single, tmp_path / "single")
    dir_dup = write_layout_snapshot(snap_dup, tmp_path / "dup")

    single_lines = (dir_single / "objects.jsonl").read_text(encoding="utf-8").splitlines()
    dup_lines = (dir_dup / "objects.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(single_lines) == 4
    assert len(dup_lines) == 8
    assert snap_single.manifest.content_sha256 != snap_dup.manifest.content_sha256

    # Roundtrip read preserves exact multiplicity
    reloaded = read_layout_snapshot(dir_dup, verify=True)
    assert len(reloaded.objects_by_type("via")) == 2
    assert len(reloaded.objects_by_type("rect")) == 2
    assert len(reloaded.objects_by_type("path_seg")) == 2
    assert len(reloaded.objects_by_type("polygon")) == 2


def test_path_seg_and_path_pin_figures_roundtrip(tmp_path: Path) -> None:
    """Real layouts (e.g. Pulsic/TSMC65 OTA5T) can use pathSeg/path figures as pins."""

    p_seg = LayoutPin(
        name="VOUT",
        pin_name="P__VOUT_0",
        net="VOUT",
        direction="output",
        layer="M1",
        purpose="drawing",
        shape_kind="path_seg",
        begin_pt=(3.0, 1.0),
        end_pt=(1.0, 1.0),
        width=0.14,
        begin_style="extend",
        end_style="truncate",
        begin_ext=0.07,
        end_ext=0.0,
    )
    p_path = LayoutPin(
        name="VBIAS",
        pin_name="P__VBIAS_0",
        net="VBIAS",
        direction="input",
        layer="M2",
        purpose="pin",
        shape_kind="path",
        points=((0.0, 0.0), (1.0, 0.0), (1.0, 0.5)),
        width=0.2,
    )
    snap = build_layout_snapshot(CV, (p_seg, p_path))
    dest = write_layout_snapshot(snap, tmp_path / "pin_shapes")
    loaded = read_layout_snapshot(dest, verify=True)
    assert loaded == snap
    vout = [o for o in loaded.objects if isinstance(o, LayoutPin) and o.name == "VOUT"][0]
    assert vout.begin_pt == (1.0, 1.0)
    assert vout.end_pt == (3.0, 1.0)
    assert vout.begin_style == "truncate"
    assert vout.end_style == "extend"
    assert vout.end_ext == 0.07


def test_malformed_manifest_jsonl_and_omissions_raise_domain_errors(tmp_path: Path) -> None:
    """Malformed inputs must raise SchemaValidationError/SnapshotIntegrityError, never raw KeyError/TypeError."""

    with pytest.raises(SchemaValidationError):
        LayoutManifest.from_dict(
            {
                "schema": "oa-git/2",
                "view_type": "layout",
                "content_sha256": "a" * 64,
                "full_sha256": "b" * 64,
                "files": {"objects.jsonl": "c" * 64},
            }
        )

    with pytest.raises(SchemaValidationError):
        LayoutManifest.from_dict(
            {
                "schema": "oa-git/2",
                "view_type": "layout",
                "cellview": CV.to_dict(),
                "content_sha256": "a" * 64,
                "full_sha256": "b" * 64,
                "files": [],  # non-object files must be rejected
            }
        )

    from oa_git.layout_snapshot import layout_object_from_dict

    with pytest.raises(SchemaValidationError):
        layout_object_from_dict(
            {"type": "via", "via_def": "M2_M1", "origin": [0.0, 0.0], "params": ["bad"]}
        )

    with pytest.raises(SchemaValidationError):
        layout_object_from_dict(
            {
                "type": "pin",
                "name": "IN",
                "net": "IN",
                "layer": "M1",
                "shape_kind": "rect",
                "bbox": [[0.0, 0.0], [1.0, 1.0]],
                "access_dir": 123,
            }
        )

    # Invalid UTF-8 omissions.json must raise SnapshotIntegrityError
    snap_om = build_layout_snapshot(
        CV,
        (LayoutRect("M1", "drawing", ((0.0, 0.0), (1.0, 1.0))),),
        omissions=(OmissionRecord(kind="shape", identifier="x", reason="unsupported"),),
    )
    out = write_layout_snapshot(snap_om, tmp_path / "bad_om")
    (out / "omissions.json").write_bytes(b"\xff\xfe\xfd")
    with pytest.raises(SnapshotIntegrityError):
        read_layout_snapshot(out, verify=False)


def test_si_float_pcell_params_and_props_preserve_submicron_precision(tmp_path: Path) -> None:
    """Sub-micron SI float PCell params (e.g. SMIC55 fw=2.4e-7, l=6e-8, cap=1.5e-15) must never round to 0.0."""

    from oa_git.layout_backend import verify_layout_roundtrip
    from oa_git.layout_model import LayoutInstance, LayoutMosaic, RoundtripMismatchError

    inst = LayoutInstance(
        name="MN0",
        lib="smic55llrf_121825",
        cell="n12ll",
        view="layout",
        xy=(12345.678912, 2.0),
        orient="R0",
        is_pcell=True,
        pcell_params={"fw": 2.4e-7, "l": 6.0e-8, "cap": 1.5e-15},
        pcell_param_types={"fw": "float", "l": "float", "cap": "float"},
        props={"bias_i": 3.25e-8},
        prop_types={"bias_i": "float"},
    )
    mos = LayoutMosaic(
        name="MN_ARR",
        lib="smic55llrf_121825",
        cell="n12ll",
        view="layout",
        xy=(10.0, 2.0),
        orient="R0",
        rows=2,
        columns=3,
        u_x=2.5,
        u_y=4.0,
        is_pcell=True,
        pcell_params={"fw": 2.4e-7, "l": 6.0e-8},
        pcell_param_types={"fw": "float", "l": "float"},
    )
    snap = build_layout_snapshot(CV, (inst, mos))
    out = write_layout_snapshot(snap, tmp_path / "si_floats")
    loaded = read_layout_snapshot(out, verify=True)

    loaded_inst = [o for o in loaded.objects if isinstance(o, LayoutInstance)][0]
    loaded_mos = [o for o in loaded.objects if isinstance(o, LayoutMosaic)][0]
    assert loaded_inst.pcell_params["fw"] == pytest.approx(2.4e-7, rel=1e-12)
    assert loaded_inst.pcell_params["l"] == pytest.approx(6.0e-8, rel=1e-12)
    assert loaded_inst.pcell_params["cap"] == pytest.approx(1.5e-15, rel=1e-12)
    assert loaded_inst.props["bias_i"] == pytest.approx(3.25e-8, rel=1e-12)
    assert loaded_inst.xy == (12345.678912, 2.0)
    assert loaded_mos.pcell_params["fw"] == pytest.approx(2.4e-7, rel=1e-12)

    # Roundtrip verifier must detect sub-micron SI float property drift (6e-8 vs 8e-8)!
    drifted = LayoutInstance(
        name="MN0",
        lib="smic55llrf_121825",
        cell="n12ll",
        view="layout",
        xy=(12345.678912, 2.0),
        orient="R0",
        is_pcell=True,
        pcell_params={"fw": 2.4e-7, "l": 6.0e-8, "cap": 1.5e-15},
        pcell_param_types={"fw": "float", "l": "float", "cap": "float"},
        props={"bias_i": 8.0e-8},
        prop_types={"bias_i": "float"},
    )
    snap_drifted = build_layout_snapshot(CV, (drifted, mos))
    with pytest.raises(RoundtripMismatchError):
        verify_layout_roundtrip(snap, snap_drifted)


def test_custom_path_seg_6tuple_canonical_reversal_and_modgen_fig_group_roundtrip(
    tmp_path: Path,
) -> None:
    """Custom 45-deg RF/analog pathSeg 6-tuples must survive canonical endpoint reversal and JSONL roundtrip."""

    from oa_git.layout_backend import verify_layout_roundtrip
    from oa_git.layout_model import LayoutFigGroup, LayoutPathSeg, RoundtripMismatchError

    seg = LayoutPathSeg(
        layer="M9",
        purpose="drawing",
        begin_pt=(40.0, 34.8),
        end_pt=(19.9, 54.9),
        width=8.49,
        begin_style="custom",
        end_style="custom",
        begin_ext=2.0,
        end_ext=3.0,
        custom_style=(1.0, 2.0, 4.245, 4.0, 5.0, 4.245),
        net="INP",
    )
    fg_orig = LayoutFigGroup(name="Modgen_1", origin=(23.07, 9.295), orient="R0", group_type="modgen")
    snap = build_layout_snapshot(CV, (seg, fg_orig))
    out = write_layout_snapshot(snap, tmp_path / "custom_ps")
    loaded = read_layout_snapshot(out, verify=True)

    loaded_seg = [o for o in loaded.objects if isinstance(o, LayoutPathSeg)][0]
    assert loaded_seg.begin_pt == (19.9, 54.9)
    assert loaded_seg.end_pt == (40.0, 34.8)
    assert loaded_seg.begin_ext == 3.0
    assert loaded_seg.end_ext == 2.0
    assert loaded_seg.custom_style == (4.0, 5.0, 4.245, 1.0, 2.0, 4.245)

    # Read-only OA figGroup~>type ("modgen") restores as "none" via dbCreateFigGroup.
    fg_rest = LayoutFigGroup(name="Modgen_1", origin=(23.07, 9.295), orient="R0", group_type="none")
    snap_rest = build_layout_snapshot(CV, (loaded_seg, fg_rest))
    verify_layout_roundtrip(snap, snap_rest)

    # Changing custom_style chamfer offsets must still be caught by roundtrip verifier.
    seg_bad = LayoutPathSeg(
        layer="M9",
        purpose="drawing",
        begin_pt=(19.9, 54.9),
        end_pt=(40.0, 34.8),
        width=8.49,
        begin_style="custom",
        end_style="custom",
        begin_ext=3.0,
        end_ext=2.0,
        custom_style=(9.0, 5.0, 4.245, 1.0, 2.0, 4.245),
        net="INP",
    )
    with pytest.raises(RoundtripMismatchError):
        verify_layout_roundtrip(snap, build_layout_snapshot(CV, (seg_bad, fg_rest)))





