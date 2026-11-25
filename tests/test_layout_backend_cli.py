"""Offline tests for layout_backend.py and layout_cli.py (Track 2 Layout v0.2)."""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import shlex
from subprocess import CompletedProcess, TimeoutExpired
from typing import Any

import pytest

from oa_git.layout_backend import (
    FixtureLayoutBackend,
    LayoutBackend,
    LayoutHandler,
    LayoutRoundtripReport,
    VirtuosoLayoutBackend,
    build_capture_layout_request_dict,
    build_restore_layout_request_dict,
    ensure_no_layout_omissions,
    load_layout_backend,
    parse_layout_restore_response,
    raw_layout_capture_to_snapshot,
    render_capture_layout_request_skill,
    render_restore_layout_request_skill,
    verify_layout_roundtrip,
)
from oa_git.layout_cli import main as layout_cli_main
from oa_git.layout_model import (
    BridgeExecutionError,
    CdfParameterError,
    CellViewRef,
    DisallowedLibraryError,
    FailClosedError,
    LayoutBlockage,
    LayoutBoundary,
    LayoutEllipse,
    LayoutFigGroup,
    LayoutInstance,
    LayoutLabel,
    LayoutLine,
    LayoutMosaic,
    LayoutNet,
    LayoutPath,
    LayoutPathSeg,
    LayoutPin,
    LayoutPolygon,
    LayoutRect,
    LayoutSnapshot,
    LayoutVia,
    LiveLockError,
    OmissionRecord,
    RoundtripMismatchError,
    SchemaValidationError,
    UnsupportedObjectError,
)
from oa_git.layout_snapshot import (
    build_layout_snapshot,
    read_layout_snapshot,
    write_layout_snapshot,
)


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "layout"
INVERTER_FIXTURE = FIXTURES_DIR / "inverter_capture.json"
SOURCE = CellViewRef("work", "inv_dut", "layout")
TARGET = CellViewRef("sandbox", "inv_dut", "layout")


def _load_inverter_payload(cellview: CellViewRef = SOURCE) -> dict[str, Any]:
    payload = json.loads(INVERTER_FIXTURE.read_text(encoding="utf-8"))
    payload["cellview"] = cellview.to_dict()
    return payload


def _make_inverter_snapshot(cellview: CellViewRef = SOURCE) -> LayoutSnapshot:
    return raw_layout_capture_to_snapshot(_load_inverter_payload(cellview))


def _make_rich_snapshot(cellview: CellViewRef = SOURCE) -> LayoutSnapshot:
    base = _make_inverter_snapshot(cellview)
    extra = (
        LayoutPin(
            name="POLY_PIN",
            pin_name="P__POLY_0",
            net="IN",
            direction="input",
            layer="PO",
            purpose="pin",
            shape_kind="polygon",
            points=((0.0, 0.0), (0.5, 0.0), (0.5, 0.4), (0.0, 0.4)),
            access_dir=("top", "bottom"),
            props={"pinTag": "poly"},
        ),
        LayoutEllipse(
            layer="M3",
            purpose="drawing",
            bbox=((1.0, 1.0), (1.4, 1.4)),
            net="OUT",
        ),
        LayoutLine(
            layer="text",
            purpose="drawing",
            points=((0.0, 0.0), (1.0, 1.0)),
        ),
        LayoutMosaic(
            name="CAP_ARR",
            lib="work",
            cell="unit_cap",
            view="layout",
            xy=(4.5, 0.5),
            orient="R0",
            rows=2,
            columns=3,
            u_x=1.2,
            u_y=1.5,
            is_pcell=True,
            pcell_params={"c": "50f", "rows": 2},
            pcell_param_types={"c": "string", "rows": "int"},
            props={"matchGroup": "A"},
            prop_types={"matchGroup": "string"},
        ),
    )
    return build_layout_snapshot(
        cellview,
        (*base.objects, *extra),
        cell_properties=base.cell_properties,
        omissions=base.omissions,
        metadata=base.manifest.metadata,
    )


def _restore_ok_payload(
    target: CellViewRef = TARGET,
    *,
    draft_cell: str = "inv_dut__oagit_draft_0123456789",
    pcell_mismatches: list[Any] | None = None,
    connectivity_mismatches: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "error": None,
        "error_code": None,
        "target": target.to_dict(),
        "published": True,
        "draft_cell": draft_cell,
        "pcell_verified": True,
        "connectivity_verified": True,
        "object_counts": {"instances": 2, "nets": 4, "pins": 4, "shapes": 6, "vias": 2},
        "pcell_mismatches": pcell_mismatches or [],
        "connectivity_mismatches": connectivity_mismatches or [],
    }


# ---------------------------------------------------------------------------
# 1. LayoutHandler Protocol Conformance
# ---------------------------------------------------------------------------


def test_layout_handler_conforms_to_view_handler_contract(tmp_path: Path) -> None:
    handler = LayoutHandler()
    assert handler.view_type == "layout"
    assert handler.capture_action == "capture_layout"
    assert handler.restore_action == "restore_layout"
    assert handler.source_file_candidates("inv_dut", "layout") == (
        "inv_dut/layout/layout.oa",
        "inv_dut/layout/master.tag",
    )

    snap = _make_inverter_snapshot()
    dest = handler.write_snapshot(snap, tmp_path / "snap")
    loaded = handler.read_snapshot(dest, verify=True)
    assert loaded == snap

    # Verify structural compatibility against Track 1's ViewHandler Protocol definition
    # and end-to-end diff/merge delegation.
    for required_attr in (
        "view_type",
        "capture_action",
        "restore_action",
        "source_file_candidates",
        "read_snapshot",
        "write_snapshot",
        "diff",
        "merge",
    ):
        assert hasattr(handler, required_attr)

    ours = build_layout_snapshot(
        SOURCE,
        (*snap.objects, LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 10.0), (11.0, 11.0)))),
        cell_properties=snap.cell_properties,
        metadata=snap.manifest.metadata,
    )
    theirs = build_layout_snapshot(
        SOURCE,
        (*snap.objects, LayoutLabel(text="NOTE", xy=(1.0, 1.0), layer="text", purpose="drawing")),
        cell_properties=snap.cell_properties,
        metadata=snap.manifest.metadata,
    )
    diff_res = handler.diff(snap, ours)
    assert diff_res.has_changes is True
    assert diff_res.has_geometry_changes is True
    assert diff_res.has_connectivity_changes is False

    merge_res = handler.merge(snap, ours, theirs)
    assert merge_res.ok is True
    assert merge_res.snapshot is not None
    assert len(merge_res.snapshot.objects) == len(snap.objects) + 2


# ---------------------------------------------------------------------------
# 2. DPL-JSON Wire Codec Tests
# ---------------------------------------------------------------------------


def test_capture_request_skill_and_dict(tmp_path: Path) -> None:
    out_path = tmp_path / "response.json"
    rendered = render_capture_layout_request_skill(SOURCE, out_path)
    assert rendered.startswith("setq(oagitRequest '(nil ")
    assert 'action "capture_layout"' in rendered
    assert 'library "work"' in rendered
    assert 'cell "inv_dut"' in rendered
    assert 'view "layout"' in rendered
    assert f'output_path "{out_path}"' in rendered
    assert rendered.endswith(")\n")

    req_dict = build_capture_layout_request_dict(SOURCE)
    assert req_dict == {
        "action": "capture_layout",
        "library": "work",
        "cell": "inv_dut",
        "view": "layout",
    }


def test_restore_request_skill_renders_all_primitives_and_escapes(tmp_path: Path) -> None:
    rich = _make_rich_snapshot()
    special_label = LayoutLabel(
        text='line1\n"quoted"\\tab\t',
        xy=(0.5, 0.5),
        layer="text",
        purpose="drawing",
    )
    snap = build_layout_snapshot(
        SOURCE,
        (*rich.objects, special_label),
        cell_properties={"designIntent": "cmos_inverter", "rev": 2, "verified": True},
        metadata=rich.manifest.metadata,
    )
    out_path = tmp_path / "response.json"
    rendered = render_restore_layout_request_skill(
        snap,
        TARGET,
        draft_cell="inv_dut__draft_01",
        overwrite=True,
        allow_libraries=("sandbox", "work"),
        output_path=out_path,
    )

    assert rendered.startswith("setq(oagitRequest '(nil action \"restore_layout\" ")
    assert 'target (nil library "sandbox" cell "inv_dut" view "layout")' in rendered
    assert 'draft_cell "inv_dut__draft_01"' in rendered
    assert "overwrite t" in rendered
    assert 'allow_libraries ("sandbox" "work")' in rendered

    # Via overrideParams include both scalars and 2-element float lists.
    assert '("cutRows" 2)' in rendered
    assert '("layer1Enc" (0.03 0.04))' in rendered

    # PCell params as (key value type) triples & connections as (term net) pairs.
    assert '("w" "1.2u" "string")' in rendered
    assert '("l" "60n" "string")' in rendered
    assert '("D" "OUT")' in rendered
    assert '("userRole" "pullup" "string")' in rendered

    # All 6 physical shape types + pins + boundaries + mosaics + blockages + fig_groups.
    for token in (
        'type "rect"',
        'type "path_seg"',
        'type "path"',
        'type "polygon"',
        'type "ellipse"',
        'type "line"',
        'shape_kind "rect"',
        'shape_kind "polygon"',
        'boundary_kind "prBoundary"',
        'blockage_type "routing"',
        'name "CAP_ARR"',
        'name "INV_CORE"',
    ):
        assert token in rendered

    # Escaped label text and typed cell properties.
    assert r'"line1\n\"quoted\"\\tab\t"' in rendered
    assert '("rev" 2 "int")' in rendered
    assert '("verified" t "boolean")' in rendered

    # Request dict matches exact SkillRequestRunner structure.
    req_dict = build_restore_layout_request_dict(
        snap,
        TARGET,
        draft_cell="inv_dut__draft_01",
        overwrite=False,
        allow_libraries=("sandbox",),
    )
    assert req_dict["action"] == "restore_layout"
    assert req_dict["target"] == TARGET.to_dict()
    assert len(req_dict["instances"]) == 2
    assert len(req_dict["mosaics"]) == 1
    assert len(req_dict["vias"]) == 2
    assert len(req_dict["shapes"]) == 8
    assert len(req_dict["pins"]) == 5

    # Rejects draft_cell == target.cell and unsafe control characters.
    with pytest.raises(SchemaValidationError, match="distinct"):
        render_restore_layout_request_skill(
            snap,
            TARGET,
            draft_cell=TARGET.cell,
            overwrite=False,
            allow_libraries=("sandbox",),
            output_path=out_path,
        )

    bad_label = LayoutLabel(text="bad\x01ctrl", xy=(0.0, 0.0), layer="text")
    bad_snap = build_layout_snapshot(SOURCE, (bad_label,))
    with pytest.raises(SchemaValidationError, match="control character"):
        render_restore_layout_request_skill(
            bad_snap,
            TARGET,
            draft_cell="draft_bad",
            overwrite=False,
            allow_libraries=("sandbox",),
            output_path=out_path,
        )


# ---------------------------------------------------------------------------
# 3. Capture & Restore Response Parsers
# ---------------------------------------------------------------------------


def test_raw_layout_capture_to_snapshot_valid_and_fail_closed() -> None:
    snap = _make_inverter_snapshot()
    assert snap.manifest.cellview == SOURCE
    assert snap.manifest.view_type == "layout"
    assert len(snap.objects_by_type("instance")) == 2
    assert len(snap.objects_by_type("via")) == 2
    assert len(snap.objects_by_type("pin")) == 4

    with pytest.raises(BridgeExecutionError, match="open_failed"):
        raw_layout_capture_to_snapshot(
            {"ok": False, "error": "cannot open", "error_code": "open_failed"}
        )

    dirty = _load_inverter_payload()
    dirty["modified_but_not_saved"] = True
    with pytest.raises(FailClosedError, match="modified_but_not_saved"):
        raw_layout_capture_to_snapshot(dirty)

    bad_extra = _load_inverter_payload()
    bad_extra["unexpected_field"] = 123
    with pytest.raises(SchemaValidationError, match="unsupported keys"):
        raw_layout_capture_to_snapshot(bad_extra)

    missing_objects = _load_inverter_payload()
    del missing_objects["objects"]
    with pytest.raises(SchemaValidationError, match="missing 'objects'"):
        raw_layout_capture_to_snapshot(missing_objects)


@pytest.mark.parametrize(
    ("code", "expected_exc"),
    [
        ("disallowed_library", DisallowedLibraryError),
        ("live_lock", LiveLockError),
        ("unsupported_object", UnsupportedObjectError),
        ("pcell_parameter_error", CdfParameterError),
        ("pcell_param_error", CdfParameterError),
        ("pcell_param_mismatch", CdfParameterError),
        ("connectivity_mismatch", RoundtripMismatchError),
        ("object_count_mismatch", RoundtripMismatchError),
        ("unknown_error_code", BridgeExecutionError),
    ],
)
def test_parse_layout_restore_response_error_mappings(
    code: str, expected_exc: type[Exception]
) -> None:
    with pytest.raises(expected_exc):
        parse_layout_restore_response(
            {"ok": False, "error": "failed", "error_code": code},
            TARGET,
        )

    # Also handles structured error dict form.
    with pytest.raises(expected_exc):
        parse_layout_restore_response(
            {"ok": False, "error": {"code": code, "message": "structured failure"}},
            TARGET,
        )


def test_parse_layout_restore_response_valid_and_mismatch_rejections() -> None:
    receipt = parse_layout_restore_response(_restore_ok_payload(), TARGET)
    assert receipt.target == TARGET
    assert receipt.published is True
    assert receipt.pcell_verified is True
    assert receipt.connectivity_verified is True
    assert receipt.object_counts["instances"] == 2

    with pytest.raises(RoundtripMismatchError, match="pcell_mismatches"):
        parse_layout_restore_response(
            _restore_ok_payload(pcell_mismatches=[{"instance": "MP0", "param": "w"}]),
            TARGET,
        )

    with pytest.raises(RoundtripMismatchError, match="connectivity_mismatches"):
        parse_layout_restore_response(
            _restore_ok_payload(connectivity_mismatches=[{"instance": "MP0", "terminal": "G"}]),
            TARGET,
        )

    with pytest.raises(RoundtripMismatchError, match="does not match"):
        parse_layout_restore_response(
            _restore_ok_payload(target=CellViewRef("sandbox", "other", "layout")),
            TARGET,
        )


# ---------------------------------------------------------------------------
# 4. Layout Roundtrip Verification & Omission Guard
# ---------------------------------------------------------------------------


def test_verify_layout_roundtrip_exact_and_engineering_number_equivalence() -> None:
    expected = _make_rich_snapshot()
    report = verify_layout_roundtrip(expected, expected)
    assert isinstance(report, LayoutRoundtripReport)
    assert report.ok is True
    assert report.mismatches == ()
    assert report.to_dict() == {"ok": True, "mismatches": []}

    # Engineering string equivalence ("1.2u" == "1.20u", "600n" == "0.6u", "60n" == "6e-8")
    # + sub-tolerance coordinate jitter (2e-6 < 1e-5) + auto-generated via.name difference.
    equiv_objs = []
    for obj in expected.objects:
        if isinstance(obj, LayoutInstance) and obj.name == "MP0":
            equiv_objs.append(
                LayoutInstance(
                    name=obj.name,
                    lib=obj.lib,
                    cell=obj.cell,
                    view=obj.view,
                    xy=(obj.xy[0] + 2e-6, obj.xy[1]),
                    orient=obj.orient,
                    is_pcell=obj.is_pcell,
                    pcell_params={"w": "1.20u", "l": "6e-8", "fingers": "2"},
                    pcell_param_types={"w": "string", "l": "string", "fingers": "string"},
                    connections=obj.connections,
                    props=obj.props,
                    prop_types=obj.prop_types,
                )
            )
        elif isinstance(obj, LayoutInstance) and obj.name == "MN0":
            equiv_objs.append(
                LayoutInstance(
                    name=obj.name,
                    lib=obj.lib,
                    cell=obj.cell,
                    view=obj.view,
                    xy=obj.xy,
                    orient=obj.orient,
                    is_pcell=obj.is_pcell,
                    pcell_params={"w": "0.6u", "l": "0.06u", "fingers": "2"},
                    pcell_param_types={"w": "string", "l": "string", "fingers": "string"},
                    connections=obj.connections,
                    props=obj.props,
                    prop_types=obj.prop_types,
                )
            )
        elif isinstance(obj, LayoutVia):
            equiv_objs.append(
                LayoutVia(
                    via_def=obj.via_def,
                    origin=obj.origin,
                    orient=obj.orient,
                    via_kind=obj.via_kind,
                    net=obj.net,
                    name="autogenerated_via_999",
                    params=obj.params,
                    props=obj.props,
                )
            )
        else:
            equiv_objs.append(obj)

    actual = build_layout_snapshot(
        TARGET,
        equiv_objs,
        cell_properties=expected.cell_properties,
        metadata=expected.manifest.metadata,
    )
    equiv_report = verify_layout_roundtrip(expected, actual)
    assert equiv_report.ok is True


def test_verify_layout_roundtrip_detects_mismatches() -> None:
    expected = _make_inverter_snapshot()
    drifted_objs = []
    for obj in expected.objects:
        if isinstance(obj, LayoutInstance) and obj.name == "MP0":
            drifted_objs.append(
                LayoutInstance(
                    name=obj.name,
                    lib=obj.lib,
                    cell=obj.cell,
                    view=obj.view,
                    xy=(obj.xy[0] + 0.2, obj.xy[1]),
                    orient=obj.orient,
                    is_pcell=obj.is_pcell,
                    pcell_params={"w": "2.4u", "l": "60n", "fingers": "2"},
                    pcell_param_types=obj.pcell_param_types,
                    connections={**obj.connections, "D": "WRONG_NET"},
                    props=obj.props,
                    prop_types=obj.prop_types,
                )
            )
        elif isinstance(obj, LayoutVia) and obj.via_def == "M2_M1":
            drifted_objs.append(
                LayoutVia(
                    via_def=obj.via_def,
                    origin=obj.origin,
                    orient=obj.orient,
                    via_kind=obj.via_kind,
                    net=obj.net,
                    params={**obj.params, "cutRows": 4},
                )
            )
        else:
            drifted_objs.append(obj)

    actual = build_layout_snapshot(
        TARGET,
        drifted_objs,
        cell_properties={"designIntent": "modified"},
    )
    report = verify_layout_roundtrip(expected, actual, raise_on_mismatch=False)
    assert report.ok is False
    categories = {m["category"] for m in report.mismatches}
    assert {"instance", "via", "cell_properties"}.issubset(categories)

    with pytest.raises(RoundtripMismatchError, match="Layout roundtrip mismatch"):
        verify_layout_roundtrip(expected, actual, raise_on_mismatch=True)


def test_ensure_no_layout_omissions_fail_closed() -> None:
    clean = _make_inverter_snapshot()
    ensure_no_layout_omissions(clean)

    omitted = build_layout_snapshot(
        SOURCE,
        clean.objects,
        omissions=(OmissionRecord(kind="shape", identifier="arc", reason="unsupported_shape"),),
    )
    with pytest.raises(UnsupportedObjectError, match="omissions"):
        ensure_no_layout_omissions(omitted)

    ensure_no_layout_omissions(omitted, allow_omissions=True)


def test_duplicate_overlapping_vias_and_shapes_multiset_fidelity(tmp_path: Path) -> None:
    dup_via_payload = {
        "type": "via",
        "via_def": "M2_M1",
        "via_kind": "stdVia",
        "origin": [1.0, 2.0],
        "orient": "R0",
        "net": "VDD",
        "params": {"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.03]},
    }
    dup_rect_payload = {
        "type": "rect",
        "layer": "M1",
        "purpose": "drawing",
        "bbox": [[0.0, 0.0], [1.0, 0.5]],
        "net": "VDD",
    }
    capture_payload = {
        "ok": True,
        "error": None,
        "cellview": SOURCE.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "maskLayout", "dbu_per_uu": 1000.0},
        "objects": [
            {"type": "net", "name": "VDD", "is_global": False, "num_bits": 1, "sig_type": "power"},
            dup_via_payload,
            dict(dup_via_payload),
            dup_rect_payload,
            dict(dup_rect_payload),
        ],
        "properties": {},
        "omissions": [],
    }

    # 1. Capture -> snapshot -> disk roundtrip preserves both identical overlapping vias & rects.
    snap = raw_layout_capture_to_snapshot(capture_payload)
    assert len(snap.objects_by_type("via")) == 2
    assert len(snap.objects_by_type("rect")) == 2

    snap_dir = write_layout_snapshot(snap, tmp_path / "dup_snap")
    loaded = read_layout_snapshot(snap_dir, verify=True)
    assert len(loaded.objects_by_type("via")) == 2
    assert len(loaded.objects_by_type("rect")) == 2

    # 2. Restore request dict & rendered SKILL emit BOTH duplicate vias and BOTH duplicate rects.
    req_dict = build_restore_layout_request_dict(
        loaded,
        TARGET,
        draft_cell="inv_dut__draft_dup",
        overwrite=False,
        allow_libraries=("sandbox",),
    )
    assert len(req_dict["vias"]) == 2
    assert len(req_dict["shapes"]) == 2

    rendered = render_restore_layout_request_skill(
        loaded,
        TARGET,
        draft_cell="inv_dut__draft_dup",
        overwrite=False,
        allow_libraries=("sandbox",),
        output_path=tmp_path / "response.json",
    )
    assert rendered.count('via_def "M2_M1"') == 2
    assert rendered.count('type "rect"') == 2

    # 3. Roundtrip verification passes when both snapshots have 2 identical overlapping vias.
    assert verify_layout_roundtrip(snap, loaded).ok is True

    # 4. Dropping 1 of the 2 overlapping vias MUST fail closed with RoundtripMismatchError.
    dropped_via_objs = [
        o for o in snap.objects if not isinstance(o, LayoutVia)
    ] + [snap.objects_by_type("via")[0]]
    dropped_via_snap = build_layout_snapshot(TARGET, dropped_via_objs)
    with pytest.raises(RoundtripMismatchError, match="via"):
        verify_layout_roundtrip(snap, dropped_via_snap, raise_on_mismatch=True)

    # 5. Adding an extra overlapping via (3 vs 2) MUST also fail closed.
    extra_via_snap = build_layout_snapshot(
        TARGET, (*snap.objects, snap.objects_by_type("via")[0])
    )
    with pytest.raises(RoundtripMismatchError, match="via"):
        verify_layout_roundtrip(snap, extra_via_snap, raise_on_mismatch=True)

    # 6. Dropping 1 of the 2 identical overlapping rects MUST fail closed too.
    dropped_rect_objs = [
        o for o in snap.objects if not isinstance(o, LayoutRect)
    ] + [snap.objects_by_type("rect")[0]]
    dropped_rect_snap = build_layout_snapshot(TARGET, dropped_rect_objs)
    with pytest.raises(RoundtripMismatchError, match="rect"):
        verify_layout_roundtrip(snap, dropped_rect_snap, raise_on_mismatch=True)


# ---------------------------------------------------------------------------
# 5. FixtureLayoutBackend End-to-End & Guards
# ---------------------------------------------------------------------------


def test_fixture_layout_backend_capture_restore_and_guards(tmp_path: Path) -> None:
    backend = FixtureLayoutBackend(fixture_path=FIXTURES_DIR)
    assert isinstance(backend, LayoutBackend)

    captured = backend.capture_layout(SOURCE)
    assert captured.manifest.cellview == SOURCE

    # Guard 1: Disallowed library
    with pytest.raises(DisallowedLibraryError):
        backend.restore_layout(captured, TARGET, allow_libraries=())

    # Guard 2: Omissions blocked unless allow_omissions=True
    omitted = build_layout_snapshot(
        SOURCE,
        captured.objects,
        omissions=(OmissionRecord(kind="via", identifier="v0", reason="unsupported_via_type"),),
    )
    with pytest.raises(UnsupportedObjectError):
        backend.restore_layout(omitted, TARGET, allow_libraries=("sandbox",))

    # Guard 3: Live *.cdslck blocks restore
    lock_dir = tmp_path / "oa_view"
    lock_dir.mkdir()
    (lock_dir / "layout.oa.cdslck").write_text("locked", encoding="utf-8")
    with pytest.raises(LiveLockError):
        backend.restore_layout(
            captured,
            TARGET,
            allow_libraries=("sandbox",),
            oa_view_dir=lock_dir,
        )
    (lock_dir / "layout.oa.cdslck").unlink()

    # Happy path restore + readback
    receipt = backend.restore_layout(
        captured,
        TARGET,
        allow_libraries=("sandbox",),
        oa_view_dir=lock_dir,
    )
    assert receipt.published is True
    assert receipt.readback_verified is True
    assert receipt.transport == "fixture"
    assert receipt.target == TARGET

    # Guard 4: Existing target blocked unless overwrite=True
    with pytest.raises(BridgeExecutionError, match="already exists"):
        backend.restore_layout(captured, TARGET, allow_libraries=("sandbox",), overwrite=False)

    # Overwrite succeeds
    receipt2 = backend.restore_layout(
        captured, TARGET, allow_libraries=("sandbox",), overwrite=True
    )
    assert receipt2.published is True

    # Readback drift rolls back fixture database
    drifted = build_layout_snapshot(
        TARGET, (LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0))),)
    )
    drift_backend = FixtureLayoutBackend(
        fixtures={SOURCE: captured},
        readback_hook={TARGET: drifted},
    )
    with pytest.raises(RoundtripMismatchError):
        drift_backend.restore_layout(captured, TARGET, allow_libraries=("sandbox",))
    assert TARGET not in drift_backend.snapshots

    # Also loads from a snapshot directory containing manifest.json
    snap_dir = write_layout_snapshot(captured, tmp_path / "snap_dir")
    snap_backend = FixtureLayoutBackend(fixture_path=snap_dir)
    assert snap_backend.capture_layout(SOURCE) == captured


# ---------------------------------------------------------------------------
# 6. VirtuosoLayoutBackend: Local, SSH, SkillRequestRunner, SIGSEGV Retry
# ---------------------------------------------------------------------------


class LocalLayoutRunner:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        item = self.responses.popleft() if self.responses else None
        cwd = Path(str(kwargs["cwd"]))
        if item == "timeout":
            raise TimeoutExpired(command, kwargs.get("timeout", 0), stderr="timeout stderr")
        if item == "sigsegv":
            (cwd / "virtuoso.log").write_text("crash log", encoding="utf-8")
            return CompletedProcess(
                command, 139, "", "corrupted double-linked list\nSegmentation fault"
            )
        if item == "missing":
            (cwd / "virtuoso.log").write_text("no response written", encoding="utf-8")
            return CompletedProcess(command, 1, "", "normal error")
        if isinstance(item, dict):
            (cwd / "response.json").write_text(json.dumps(item), encoding="utf-8")
        return CompletedProcess(command, 0, "", "")


class RemoteLayoutRunner:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.remote_files: dict[str, bytes] = {}

    @staticmethod
    def _remote_dir(shell: str) -> str:
        tokens = shlex.split(shell)
        val = next(tok for tok in tokens if tok.startswith("IC261_WORKDIR="))
        return val.split("=", 1)[1]

    def __call__(self, command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if command[0] == "/usr/bin/ssh":
            shell = command[2]
            if "env ENABLE_SOS705=0" in shell:
                remote_dir = self._remote_dir(shell)
                item = self.responses.popleft()
                if item == "sigsegv":
                    self.remote_files[f"{remote_dir}/virtuoso.log"] = b"corrupted double-linked list"
                    return CompletedProcess(command, 139, "", "Segmentation fault")
                if isinstance(item, dict):
                    self.remote_files[f"{remote_dir}/response.json"] = json.dumps(item).encode()
                self.remote_files[f"{remote_dir}/virtuoso.log"] = b"remote ok"
            return CompletedProcess(command, 0, "", "")
        if command[0] == "/usr/bin/scp":
            src, dst = command[1:3]
            if ":" in src:
                remote_src = shlex.split(src.split(":", 1)[1])[0]
                if remote_src not in self.remote_files:
                    return CompletedProcess(command, 1, "", "No such file")
                Path(dst).write_bytes(self.remote_files[remote_src])
            return CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unexpected non-ssh/scp command: {command}")


def _ensure_skill_dir(tmp_path: Path) -> Path:
    real_skill_dir = Path(__file__).resolve().parents[1] / "src" / "oa_git" / "skill"
    if (real_skill_dir / "export_layout.il").is_file() and (
        real_skill_dir / "restore_layout.il"
    ).is_file():
        return real_skill_dir
    fake_dir = tmp_path / "skill"
    fake_dir.mkdir()
    for name in ("oagit_common.il", "export_layout.il", "restore_layout.il"):
        (fake_dir / name).write_text("/* stub */\n", encoding="utf-8")
    return fake_dir


def test_virtuoso_layout_backend_local_happy_path(tmp_path: Path) -> None:
    snap = _make_inverter_snapshot()
    runner = LocalLayoutRunner(
        [
            _load_inverter_payload(SOURCE),
            _restore_ok_payload(TARGET),
            _load_inverter_payload(TARGET),
        ]
    )
    backend = VirtuosoLayoutBackend(
        runner=runner,
        skill_dir=_ensure_skill_dir(tmp_path),
    )
    cds_lib = tmp_path / "cds.lib"

    captured = backend.capture_layout(SOURCE, cds_lib=cds_lib)
    receipt = backend.restore_layout(
        snap,
        TARGET,
        cds_lib=cds_lib,
        allow_libraries=("sandbox",),
    )

    assert captured.manifest.cellview == SOURCE
    assert receipt.published is True
    assert receipt.readback_verified is True
    assert len(runner.calls) == 3

    cmd, kwargs = runner.calls[0]
    assert cmd[1:3] == ["-nograph", "-nocdsinit"]
    assert cmd[cmd.index("-cdslib") + 1] == str(cds_lib)
    assert kwargs["env"]["ENABLE_SOS705"] == "0"
    assert kwargs["env"]["OAGIT_AUTO_EXIT"] == "1"
    assert kwargs["env"]["TMPDIR"].endswith("/tmp")
    # Temp run directory cleaned up after run.
    assert backend.last_run_dir is not None and not backend.last_run_dir.exists()


def test_virtuoso_layout_backend_ssh_transport_and_cleanup(tmp_path: Path) -> None:
    runner = RemoteLayoutRunner([_load_inverter_payload(SOURCE)])
    backend = VirtuosoLayoutBackend(
        ssh_host="eda-server",
        remote_workdir="/tmp/oagit_layout_runs",
        virtuoso_cmd="/opt/eda/bin/virtuoso",
        runner=runner,
        skill_dir=_ensure_skill_dir(tmp_path),
    )
    captured = backend.capture_layout(SOURCE, cds_lib="/remote/work/cds.lib")
    assert captured.manifest.cellview == SOURCE

    assert all(call[0][0] in {"/usr/bin/ssh", "/usr/bin/scp"} for call in runner.calls)
    ssh_calls = [call for call in runner.calls if call[0][0] == "/usr/bin/ssh"]
    scp_calls = [call for call in runner.calls if call[0][0] == "/usr/bin/scp"]
    assert ssh_calls and scp_calls

    exec_call = next(call for call in ssh_calls if "env ENABLE_SOS705=0" in call[0][2])
    shell = exec_call[0][2]
    assert "ENABLE_SOS705=0" in shell
    assert "OAGIT_AUTO_EXIT=1" in shell
    assert "-cdslib /remote/work/cds.lib" in shell
    assert "/opt/eda/bin/virtuoso" in shell
    assert any("rm -rf --" in call[0][2] for call in ssh_calls)


def test_virtuoso_layout_backend_skill_runner_delegation_and_guards(tmp_path: Path) -> None:
    class MockSkillRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any], Any, Any]] = []

        def run_skill_request(
            self,
            action: str,
            request: dict[str, Any],
            *,
            cds_lib: Path | str | None = None,
            session: Any | None = None,
        ) -> dict[str, Any]:
            self.calls.append((action, dict(request), cds_lib, session))
            if action == "capture_layout":
                cv = CellViewRef(request["library"], request["cell"], request["view"])
                return _load_inverter_payload(cv)
            if action == "restore_layout":
                tgt = CellViewRef.from_dict(request["target"])
                return _restore_ok_payload(tgt, draft_cell=request["draft_cell"])
            raise AssertionError(f"unexpected action {action}")

    mock_runner = MockSkillRunner()
    backend = VirtuosoLayoutBackend(skill_runner=mock_runner)

    # Guard must fire BEFORE delegating to skill_runner
    snap = _make_inverter_snapshot()
    with pytest.raises(DisallowedLibraryError):
        backend.restore_layout(snap, TARGET, allow_libraries=())
    assert mock_runner.calls == []

    # Happy path delegates capture + restore + readback through run_skill_request
    captured = backend.capture_layout(SOURCE, cds_lib="/remote/cds.lib")
    assert captured.manifest.cellview == SOURCE

    receipt = backend.restore_layout(
        snap,
        TARGET,
        cds_lib="/remote/cds.lib",
        allow_libraries=("sandbox",),
    )
    assert receipt.published is True
    assert receipt.readback_verified is True
    assert receipt.transport == "skill_runner"
    assert [c[0] for c in mock_runner.calls] == [
        "capture_layout",
        "restore_layout",
        "capture_layout",
    ]
    restore_req = mock_runner.calls[1][1]
    assert restore_req["target"] == TARGET.to_dict()
    assert len(restore_req["instances"]) == 2
    assert len(restore_req["vias"]) == 2


def test_virtuoso_layout_backend_transient_sigsegv_retry(tmp_path: Path) -> None:
    snap = _make_inverter_snapshot()
    skill_dir = _ensure_skill_dir(tmp_path)
    cds_lib = tmp_path / "cds.lib"

    # Attempt 1 crashes with exit 139 / corrupted double-linked list; attempt 2 succeeds.
    runner = LocalLayoutRunner(["sigsegv", _restore_ok_payload(TARGET)])
    backend = VirtuosoLayoutBackend(runner=runner, skill_dir=skill_dir)
    receipt = backend.restore_layout(
        snap,
        TARGET,
        cds_lib=cds_lib,
        allow_libraries=("sandbox",),
        verify_readback=False,
    )
    assert receipt.published is True
    assert len(runner.calls) == 2
    assert "retry" in receipt.details
    assert receipt.details["retry"]["retried"] is True
    assert receipt.details["retry"]["retry_count"] == 1
    assert receipt.details["retry"]["crash_events"][0]["returncode"] == 139

    # Two consecutive SIGSEGVs exhaust the 1-time retry and fail closed.
    runner_fail = LocalLayoutRunner(["sigsegv", "sigsegv"])
    backend_fail = VirtuosoLayoutBackend(runner=runner_fail, skill_dir=skill_dir)
    with pytest.raises(BridgeExecutionError, match="exhausted"):
        backend_fail.capture_layout(SOURCE, cds_lib=cds_lib)
    assert len(runner_fail.calls) == 2

    # Non-crash failure (exit 1 without crash signature) does NOT retry.
    runner_normal = LocalLayoutRunner(["missing", _load_inverter_payload(SOURCE)])
    backend_normal = VirtuosoLayoutBackend(runner=runner_normal, skill_dir=skill_dir)
    with pytest.raises(BridgeExecutionError):
        backend_normal.capture_layout(SOURCE, cds_lib=cds_lib)
    assert len(runner_normal.calls) == 1

    # SSH transient crash retry also works cleanly.
    ssh_runner = RemoteLayoutRunner(["sigsegv", _load_inverter_payload(SOURCE)])
    ssh_backend = VirtuosoLayoutBackend(
        ssh_host="eda-server",
        runner=ssh_runner,
        skill_dir=skill_dir,
    )
    captured = ssh_backend.capture_layout(SOURCE, cds_lib="/remote/cds.lib")
    assert captured.manifest.cellview == SOURCE
    assert ssh_backend.last_retry_info is not None
    assert ssh_backend.last_retry_info["retry_count"] == 1


def test_load_layout_backend_factory() -> None:
    fx = load_layout_backend("fixture", fixture_path=INVERTER_FIXTURE)
    assert isinstance(fx, FixtureLayoutBackend)
    vb = load_layout_backend("virtuoso", ssh_host="eda-server")
    assert isinstance(vb, VirtuosoLayoutBackend)
    assert vb.ssh_host == "eda-server"
    with pytest.raises(BridgeExecutionError):
        load_layout_backend("unknown")


# ---------------------------------------------------------------------------
# 7. Standalone CLI Tests (oa_git.layout_cli)
# ---------------------------------------------------------------------------


def test_cli_capture_verify_and_restore(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "captured_inv"
    rc = layout_cli_main(
        [
            "capture",
            "work/inv_dut/layout",
            "--out",
            str(out_dir),
            "--backend",
            "fixture",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    cap_data = json.loads(capsys.readouterr().out)
    assert cap_data["ok"] is True
    assert cap_data["view_type"] == "layout"
    assert cap_data["schema"] == "oa-git/2"
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "objects.jsonl").is_file()

    # Verify valid snapshot -> exit 0
    rc = layout_cli_main(["verify", str(out_dir), "--json"])
    assert rc == 0
    ver_data = json.loads(capsys.readouterr().out)
    assert ver_data["ok"] is True
    assert ver_data["cellview"] == SOURCE.to_dict()

    # Restore happy path -> exit 0
    rc = layout_cli_main(
        [
            "restore",
            str(out_dir),
            "--target",
            "sandbox/inv_dut/layout",
            "--allow-library",
            "sandbox",
            "--backend",
            "fixture",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    res_data = json.loads(capsys.readouterr().out)
    assert res_data["ok"] is True
    assert res_data["published"] is True
    assert res_data["readback_verified"] is True

    # Restore disallowed library -> exit 2
    rc = layout_cli_main(
        [
            "restore",
            str(out_dir),
            "--target",
            "sandbox/inv_dut/layout",
            "--backend",
            "fixture",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 2
    err_data = json.loads(capsys.readouterr().out)
    assert err_data["ok"] is False
    assert err_data["error_type"] == "DisallowedLibraryError"

    # Restore blocked by live lock -> exit 1
    lock_dir = tmp_path / "locked_oa"
    lock_dir.mkdir()
    (lock_dir / "layout.oa.cdslck").write_text("lock", encoding="utf-8")
    rc = layout_cli_main(
        [
            "restore",
            str(out_dir),
            "--target",
            "sandbox/inv_dut/layout",
            "--allow-library",
            "sandbox",
            "--oa-view-dir",
            str(lock_dir),
            "--backend",
            "fixture",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 1
    lock_data = json.loads(capsys.readouterr().out)
    assert lock_data["ok"] is False
    assert lock_data["error_type"] == "LiveLockError"

    # Tampered snapshot -> verify returns exit 1
    (out_dir / "objects.jsonl").write_text(
        '{"bbox":[[0.0,0.0],[1.0,1.0]],"layer":"M1","purpose":"drawing","type":"rect"}\n',
        encoding="utf-8",
    )
    rc = layout_cli_main(["verify", str(out_dir), "--json"])
    assert rc == 1
    tamper_data = json.loads(capsys.readouterr().out)
    assert tamper_data["ok"] is False
    assert tamper_data["error_type"] == "SnapshotIntegrityError"


def test_cli_diff_and_merge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_snap = _make_inverter_snapshot()
    ours_snap = build_layout_snapshot(
        SOURCE,
        (
            *base_snap.objects,
            LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 10.0), (11.0, 11.0)), net="VDD"),
        ),
        cell_properties=base_snap.cell_properties,
        metadata=base_snap.manifest.metadata,
    )
    theirs_snap = build_layout_snapshot(
        SOURCE,
        (
            *base_snap.objects,
            LayoutLabel(text="NOTE", xy=(1.0, 1.0), layer="text", purpose="drawing"),
        ),
        cell_properties=base_snap.cell_properties,
        metadata=base_snap.manifest.metadata,
    )
    overlap_theirs_snap = build_layout_snapshot(
        SOURCE,
        (
            *base_snap.objects,
            LayoutRect(layer="M2", purpose="drawing", bbox=((10.5, 10.5), (11.5, 11.5)), net="VSS"),
        ),
        cell_properties=base_snap.cell_properties,
        metadata=base_snap.manifest.metadata,
    )

    base_dir = write_layout_snapshot(base_snap, tmp_path / "base")
    ours_dir = write_layout_snapshot(ours_snap, tmp_path / "ours")
    theirs_dir = write_layout_snapshot(theirs_snap, tmp_path / "theirs")
    overlap_dir = write_layout_snapshot(overlap_theirs_snap, tmp_path / "overlap_theirs")

    # diff identical with --exit-code -> 0
    rc = layout_cli_main(["diff", str(base_dir), str(base_dir), "--exit-code", "--json"])
    assert rc == 0
    d0 = json.loads(capsys.readouterr().out)
    assert d0["ok"] is True
    assert d0["has_changes"] is False

    # diff changed with --exit-code -> 1
    rc = layout_cli_main(["diff", str(base_dir), str(ours_dir), "--exit-code", "--json"])
    assert rc == 1
    d1 = json.loads(capsys.readouterr().out)
    assert d1["ok"] is True
    assert d1["has_changes"] is True
    assert d1["has_geometry_changes"] is True

    # diff --connectivity-only on pure-annotation change -> exit 0
    rc = layout_cli_main(
        ["diff", str(base_dir), str(theirs_dir), "--connectivity-only", "--exit-code", "--json"]
    )
    assert rc == 0
    d2 = json.loads(capsys.readouterr().out)
    assert d2["has_connectivity_changes"] is False
    assert d2["has_annotation_changes"] is True

    # merge non-conflicting -> exit 0 and writes valid snapshot
    merged_dir = tmp_path / "merged"
    rc = layout_cli_main(
        ["merge", str(base_dir), str(ours_dir), str(theirs_dir), "-o", str(merged_dir), "--json"]
    )
    assert rc == 0
    m_data = json.loads(capsys.readouterr().out)
    assert m_data["ok"] is True
    merged_snap = read_layout_snapshot(merged_dir, verify=True)
    assert len(merged_snap.objects) == len(base_snap.objects) + 2

    # merge with same-layer spatial overlap -> exit 1 Fail-Closed
    conflict_out = tmp_path / "conflict_out"
    rc = layout_cli_main(
        [
            "merge",
            str(base_dir),
            str(ours_dir),
            str(overlap_dir),
            "-o",
            str(conflict_out),
            "--json",
        ]
    )
    assert rc == 1
    c_data = json.loads(capsys.readouterr().out)
    assert c_data["ok"] is False
    assert len(c_data["conflicts"]) >= 1
    assert not conflict_out.exists()

    # merge with --allow-spatial-overlap -> exit 0
    allowed_out = tmp_path / "allowed_out"
    rc = layout_cli_main(
        [
            "merge",
            str(base_dir),
            str(ours_dir),
            str(overlap_dir),
            "-o",
            str(allowed_out),
            "--allow-spatial-overlap",
            "--json",
        ]
    )
    assert rc == 0
    a_data = json.loads(capsys.readouterr().out)
    assert a_data["ok"] is True
    assert (allowed_out / "manifest.json").is_file()

    # Argument parse error in --json mode -> exit 2 + JSON payload
    rc = layout_cli_main(["verify", "--json"])
    assert rc == 2
    arg_err = json.loads(capsys.readouterr().out)
    assert arg_err["ok"] is False
    assert arg_err["error_type"] == "ArgumentError"


def test_post_exit_cxt_teardown_sigsegv_allowed_only_after_clean_exit(tmp_path: Path) -> None:
    """RHEL9/old-PDK .cxt teardown SIGSEGV after `Memory report: on exit` + valid response succeeds,
    while generic non-zero exit codes or crashes without clean exit still fail closed."""

    payload = _load_inverter_payload(SOURCE)
    cds_lib = tmp_path / "cds.lib"

    def teardown_runner(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / "response.json").write_text(json.dumps(payload), encoding="utf-8")
        (cwd / "virtuoso.log").write_text(
            "# Memory report: on exit 1,001 MB, process size 2,701 MB\n",
            encoding="utf-8",
        )
        return CompletedProcess(command, 139, "", "corrupted double-linked list\n")

    backend = VirtuosoLayoutBackend(
        runner=teardown_runner, skill_dir=_ensure_skill_dir(tmp_path)
    )
    snap = backend.capture_layout(SOURCE, cds_lib=cds_lib)
    assert snap.manifest.cellview == SOURCE
    assert backend.last_teardown_warning is not None
    assert backend.last_teardown_warning["returncode"] == 139

    # Without "Memory report: on exit", non-zero exit 139 MUST still fail closed!
    def midrun_crash_runner(command: list[str], **kwargs: Any) -> CompletedProcess[str]:
        cwd = Path(str(kwargs["cwd"]))
        (cwd / "response.json").write_text(json.dumps(payload), encoding="utf-8")
        (cwd / "virtuoso.log").write_text("still running...\n", encoding="utf-8")
        return CompletedProcess(command, 139, "", "corrupted double-linked list\n")

    strict_backend = VirtuosoLayoutBackend(
        runner=midrun_crash_runner, skill_dir=_ensure_skill_dir(tmp_path)
    )
    with pytest.raises(BridgeExecutionError, match="exited with status 139"):
        strict_backend.capture_layout(SOURCE, cds_lib=cds_lib)


def test_parse_layout_restore_response_handles_daemon_spool_error_envelopes() -> None:
    """oagit-spool/1 daemon error payloads with protocol/request_id/session must map error_code."""

    daemon_err = {
        "protocol": "oagit-spool/1",
        "request_id": "req-123",
        "ok": False,
        "error_code": "live_lock",
        "error": "target view has a live edit lock",
        "session": {"daemon_id": "abc123", "reused": True},
    }
    with pytest.raises(LiveLockError, match="live_lock"):
        parse_layout_restore_response(daemon_err, TARGET)

    daemon_ok = {
        "protocol": "oagit-spool/1",
        "request_id": "req-124",
        "session": {"daemon_id": "abc123", "reused": True},
        **_restore_ok_payload(TARGET),
    }
    receipt = parse_layout_restore_response(daemon_ok, TARGET)
    assert receipt.published is True


# ---------------------------------------------------------------------------
# 8. Git-Like CLI Upgrades: Config Discovery, Daemon Wiring, Status,
#    Dirty-Index --changed, Git Revision Diff, --stat, and Upgraded Verify
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *git_args: str) -> None:
    import subprocess

    res = subprocess.run(
        ["git", "-C", str(repo), *git_args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"git {git_args} failed: {res.stderr}"


def _write_oa_layout_files(oa_lib_root: Path, cell: str, view: str, content: bytes = b"oa-v1") -> Path:
    view_dir = oa_lib_root / cell / view
    view_dir.mkdir(parents=True, exist_ok=True)
    (view_dir / "layout.oa").write_bytes(content)
    (view_dir / "master.tag").write_text("-- Master.tag File, Rev:1.0\nlayout.oa\n", encoding="utf-8")
    return view_dir


def test_cli_upward_config_discovery_and_restore_auto_oa_view_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from oa_git.project_state import load_index, write_project_config

    project = tmp_path / "proj"
    oa_work = project / "oa" / "work"
    oa_sandbox = project / "oa" / "sandbox"
    _write_oa_layout_files(oa_work, "inv_dut", "layout", b"oa-inv-v1")
    sandbox_view_dir = _write_oa_layout_files(oa_sandbox, "inv_dut", "layout", b"oa-sb-v1")

    write_project_config(
        project,
        {
            "snapshots_dir": "snapshots",
            "cds_lib": "cds.lib",
            "oa_libs": {"work": str(oa_work), "sandbox": str(oa_sandbox)},
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
        },
    )
    nested = project / "designs" / "analog" / "sub"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    # Capture from nested subdir WITHOUT --root, --snapshots-dir, or --backend
    rc = layout_cli_main(
        [
            "capture",
            "work/inv_dut/layout",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    cap_data = json.loads(capsys.readouterr().out)
    assert cap_data["ok"] is True
    expected_snap = project / "snapshots" / "work" / "inv_dut" / "layout"
    assert Path(cap_data["snapshot_dir"]) == expected_snap
    assert (expected_snap / "manifest.json").is_file()

    # Dirty index updated automatically under project/.oagit/index.json
    idx = load_index(project)
    assert "work/inv_dut/layout" in idx
    assert idx["work/inv_dut/layout"].state == "clean"
    assert idx["work/inv_dut/layout"].snapshot == "snapshots/work/inv_dut/layout"

    # Restore from nested subdir inherits allow_libraries=["sandbox"] and auto-infers oa_view_dir
    rc = layout_cli_main(
        [
            "restore",
            "snapshots/work/inv_dut/layout",
            "--target",
            "sandbox/inv_dut/layout",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    res_data = json.loads(capsys.readouterr().out)
    assert res_data["ok"] is True
    assert res_data["published"] is True

    # Live lock in auto-inferred oa_libs["sandbox"]/inv_dut/layout blocks restore WITHOUT --oa-view-dir!
    lock_file = sandbox_view_dir / "layout.oa.cdslck"
    lock_file.write_text("locked", encoding="utf-8")
    rc = layout_cli_main(
        [
            "restore",
            "snapshots/work/inv_dut/layout",
            "--target",
            "sandbox/inv_dut/layout",
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 1
    lock_err = json.loads(capsys.readouterr().out)
    assert lock_err["ok"] is False
    assert lock_err["error_type"] == "LiveLockError"
    lock_file.unlink()

    # Legacy v1 .oagit.json fallback discovery also works cleanly
    legacy_root = tmp_path / "legacy"
    legacy_sub = legacy_root / "a" / "b"
    legacy_sub.mkdir(parents=True)
    (legacy_root / ".oagit.json").write_text(
        json.dumps(
            {
                "schema": "oa-git-config/1",
                "snapshots_dir": "snaps_v1",
                "allow_libraries": ["sandbox"],
                "backend": "fixture",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(legacy_sub)
    rc = layout_cli_main(
        ["capture", "work/inv_dut/layout", "--fixture", str(INVERTER_FIXTURE), "--json"]
    )
    assert rc == 0
    v1_cap = json.loads(capsys.readouterr().out)
    assert Path(v1_cap["snapshot_dir"]) == legacy_root / "snaps_v1" / "work" / "inv_dut" / "layout"


def test_cli_hot_daemon_wiring_and_one_shot_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import oa_git.layout_cli as layout_cli_mod
    from oa_git.project_state import write_project_config
    from oa_git.view_contract import OaSession
    from oa_git.virtuoso_backend import VirtuosoSkillBackend

    project = tmp_path / "daemon_proj"
    write_project_config(
        project,
        {
            "snapshots_dir": "snapshots",
            "cds_lib": "/remote/work/cds.lib",
            "ssh_host": "eda-server",
            "remote_workdir": "/tmp/oagit_remote",
            "virtuoso_cmd": "/remote/bin/virtuoso261",
            "allow_libraries": ["sandbox"],
            "backend": "virtuoso",
            "timeout_s": 240,
            "daemon": {"enabled": True, "idle_timeout_s": 90, "startup_timeout_s": 45, "poll_ms": 50},
        },
    )

    parser = layout_cli_mod._build_parser()

    # Default v2 config with daemon.enabled=True & cds_lib builds VirtuosoSkillBackend(daemon_enabled=True)
    args_daemon = parser.parse_args(["capture", "work/inv_dut/layout", "--root", str(project)])
    root, cfg = layout_cli_mod._discover_project_and_config(args_daemon.root)
    settings_daemon = layout_cli_mod._resolve_settings(args_daemon, root, cfg)
    assert settings_daemon["daemon_enabled"] is True
    assert settings_daemon["cds_lib"] == "/remote/work/cds.lib"
    assert settings_daemon["ssh_host"] == "eda-server"
    assert settings_daemon["timeout_s"] == 240

    backend_daemon = layout_cli_mod._build_backend(settings_daemon)
    assert isinstance(backend_daemon, VirtuosoLayoutBackend)
    assert isinstance(backend_daemon.skill_runner, VirtuosoSkillBackend)
    assert backend_daemon.skill_runner.daemon_enabled is True
    assert backend_daemon.skill_runner.project_root == project.resolve()
    assert backend_daemon.skill_runner.ssh_host == "eda-server"
    assert backend_daemon.skill_runner.daemon_idle_timeout_s == 90

    # --one-shot and --no-daemon disable daemon and pass OaSession(transport="one_shot")
    seen_sessions: list[Any] = []
    seen_skill_runners: list[Any] = []

    class RecordingBackend:
        last_transport = "one_shot"

        def capture_layout(self, cellview: CellViewRef, *, cds_lib=None, session=None):
            seen_sessions.append(session)
            return _make_inverter_snapshot(cellview)

    def fake_load_layout_backend(name: str, **kwargs: Any):
        seen_skill_runners.append(kwargs.get("skill_runner"))
        return RecordingBackend()

    monkeypatch.setattr(layout_cli_mod, "load_layout_backend", fake_load_layout_backend)

    for flag in ("--one-shot", "--no-daemon"):
        rc = layout_cli_main(
            ["capture", "work/inv_dut/layout", "--root", str(project), flag, "--json"]
        )
        assert rc == 0
        capsys.readouterr()

    assert seen_skill_runners == [None, None]
    assert len(seen_sessions) == 2
    for sess in seen_sessions:
        assert isinstance(sess, OaSession)
        assert sess.session_id == "layout-cli-oneshot"
        assert sess.transport == "one_shot"


def test_cli_capture_changed_skips_clean_and_recaptures_after_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import oa_git.layout_cli as layout_cli_mod
    from oa_git.project_state import IndexEntry, update_index_entry, write_project_config

    project = tmp_path / "dirty_proj"
    oa_work = project / "oa" / "work"
    view_dir = _write_oa_layout_files(oa_work, "inv_dut", "layout", b"oa-layout-v1")
    write_project_config(
        project,
        {
            "snapshots_dir": "snapshots",
            "oa_libs": {"work": str(oa_work)},
            "backend": "fixture",
        },
    )

    # Seed an unrelated schematic entry in .oagit/index.json to verify layout_cli ignores it
    update_index_entry(
        project,
        IndexEntry(
            cellview=CellViewRef("work", "inv_dut", "schematic"),
            source_files=(),
            snapshot="snapshots/work/inv_dut/schematic",
            state="dirty",
        ),
    )

    # Initial capture writes snapshot + clean index entry
    rc = layout_cli_main(
        [
            "capture",
            "work/inv_dut/layout",
            "--root",
            str(project),
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    # Second capture with --changed (no positional cellview!) must make 0 backend calls
    calls: list[str] = []
    real_load = layout_cli_mod.load_layout_backend

    def spy_load(name: str, **kwargs: Any):
        calls.append(name)
        return real_load(name, **kwargs)

    monkeypatch.setattr(layout_cli_mod, "load_layout_backend", spy_load)

    rc = layout_cli_main(
        [
            "capture",
            "--changed",
            "--root",
            str(project),
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    skip_data = json.loads(capsys.readouterr().out)
    assert skip_data["ok"] is True
    assert skip_data["transport"] == "index_skip"
    assert skip_data["captured"] == []
    assert [s["key"] for s in skip_data["skipped"]] == ["work/inv_dut/layout"]
    assert calls == []

    # Edit layout.oa -> now --changed MUST capture once
    (view_dir / "layout.oa").write_bytes(b"oa-layout-v2-modified")
    rc = layout_cli_main(
        [
            "capture",
            "--changed",
            "--root",
            str(project),
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    recap_data = json.loads(capsys.readouterr().out)
    assert recap_data["ok"] is True
    assert len(recap_data["captured"]) == 1
    assert recap_data["captured"][0]["cellview"] == SOURCE.to_dict()
    assert recap_data["skipped"] == []
    assert len(calls) == 1

    # Immediate re-run with --changed skips again with 0 new backend calls
    rc = layout_cli_main(
        [
            "capture",
            "--changed",
            "--root",
            str(project),
            "--fixture",
            str(INVERTER_FIXTURE),
        ]
    )
    assert rc == 0
    human_out = capsys.readouterr().out
    assert "Skipped (clean): work/inv_dut/layout" in human_out
    assert len(calls) == 1


def test_cli_status_clean_dirty_locked_dup_vias_and_changed_filter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from oa_git.project_state import write_project_config

    project = tmp_path / "status_proj"
    oa_work = project / "oa" / "work"
    inv_view_dir = _write_oa_layout_files(oa_work, "inv_dut", "layout", b"inv-v1")
    opamp_view_dir = _write_oa_layout_files(oa_work, "opamp", "layout", b"opamp-v1")
    amp2_view_dir = _write_oa_layout_files(oa_work, "amp2", "layout_pulsic", b"amp2-v1")

    write_project_config(
        project,
        {
            "snapshots_dir": "snapshots",
            "oa_libs": {"work": str(oa_work)},
            "backend": "fixture",
        },
    )

    # Create a non-layout schematic manifest to ensure status ignores it
    sch_dir = project / "snapshots" / "work" / "inv_dut" / "schematic"
    sch_dir.mkdir(parents=True)
    (sch_dir / "manifest.json").write_text(
        json.dumps({"schema": "oa-git/2", "view_type": "schematic", "cellview": {"library": "work", "cell": "inv_dut", "view": "schematic"}}),
        encoding="utf-8",
    )

    # Capture inv_dut cleanly via CLI so it's indexed as clean
    rc = layout_cli_main(
        [
            "capture",
            "work/inv_dut/layout",
            "--root",
            str(project),
            "--fixture",
            str(INVERTER_FIXTURE),
            "--json",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    # Write opamp snapshot WITH a duplicate via (and leave unindexed -> dirty)
    dup_via = LayoutVia(via_def="M2_M1", origin=(1.0, 2.0), orient="R0", net="VDD", params={"cutRows": 2, "cutColumns": 1})
    opamp_cv = CellViewRef("work", "opamp", "layout")
    opamp_snap = build_layout_snapshot(opamp_cv, (dup_via, dup_via))
    write_layout_snapshot(opamp_snap, project / "snapshots" / "work" / "opamp" / "layout")

    # Write amp2 custom-view snapshot (cleanly unlocked initially)
    amp2_cv = CellViewRef("work", "amp2", "layout_pulsic")
    amp2_snap = build_layout_snapshot(amp2_cv, (LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0))),))
    write_layout_snapshot(amp2_snap, project / "snapshots" / "work" / "amp2" / "layout_pulsic")

    # Status without locks -> exit 0, shows clean + dirty without dup-via noise
    rc = layout_cli_main(["status", "--root", str(project)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "clean" in out_text
    assert "work/inv_dut/layout" in out_text
    assert "dirty" in out_text
    assert "work/opamp/layout" in out_text
    assert "dup-via" not in out_text
    assert "schematic" not in out_text

    # --changed hides clean inv_dut
    rc = layout_cli_main(["status", "--root", str(project), "--changed", "--json"])
    assert rc == 0
    changed_data = json.loads(capsys.readouterr().out)
    assert changed_data["ok"] is True
    changed_keys = [e["key"] for e in changed_data["layouts"]]
    assert "work/inv_dut/layout" not in changed_keys
    assert "work/opamp/layout" in changed_keys
    assert "work/amp2/layout_pulsic" in changed_keys

    # Edit inv_dut layout.oa -> becomes dirty
    (inv_view_dir / "layout.oa").write_bytes(b"inv-v2-edited")
    rc = layout_cli_main(["status", "work/inv_dut/layout", "--root", str(project), "--json"])
    assert rc == 0
    inv_status = json.loads(capsys.readouterr().out)
    assert inv_status["layouts"][0]["status"] == "dirty"

    # Add live *.cdslck on amp2 -> LOCKED and exit code 1!
    (amp2_view_dir / "layout.oa.cdslck").write_text("live lock", encoding="utf-8")
    rc = layout_cli_main(["status", "--root", str(project)])
    assert rc == 1
    locked_text = capsys.readouterr().out
    assert "LOCKED" in locked_text
    assert "work/amp2/layout_pulsic" in locked_text
    assert "1 live lock" in locked_text

    # Also check --oa-lib-map works standalone without config
    standalone_snaps = tmp_path / "standalone_snaps"
    write_layout_snapshot(opamp_snap, standalone_snaps / "work" / "opamp" / "layout")
    (opamp_view_dir / "layout.oa.cdslck").write_text("lock", encoding="utf-8")
    rc = layout_cli_main(
        [
            "status",
            "--snapshots-dir",
            str(standalone_snaps),
            "--oa-lib-map",
            f"work={oa_work}",
            "--json",
        ]
    )
    assert rc == 1
    st_json = json.loads(capsys.readouterr().out)
    assert st_json["ok"] is False
    assert st_json["layouts"][0]["status"] == "LOCKED"
    assert "duplicate_vias_count" not in st_json["layouts"][0]

    # Tampered snapshot -> FAILED and exit code 1 (even after removing live lock)
    (amp2_view_dir / "layout.oa.cdslck").unlink()
    (project / "snapshots" / "work" / "opamp" / "layout" / "objects.jsonl").write_text(
        '{"type":"rect","layer":"M1","purpose":"drawing","bbox":[[0,0],[9,9]]}\n',
        encoding="utf-8",
    )
    rc = layout_cli_main(["status", "work/opamp/layout", "--root", str(project)])
    assert rc == 1
    failed_text = capsys.readouterr().out
    assert "FAILED" in failed_text
    assert "work/opamp/layout" in failed_text


def test_cli_diff_git_revisions_auto_infer_stat_and_connectivity_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "git_repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test Engineer")

    snap_v1 = _make_inverter_snapshot()
    snap_dir = write_layout_snapshot(snap_v1, repo / "snapshots" / "work" / "inv_dut" / "layout")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "v1 layout")

    # v2 adds an M2 rect, a duplicate via, and a label (geometry + annotation only)
    existing_via = snap_v1.objects_by_type("via")[0]
    snap_v2 = build_layout_snapshot(
        SOURCE,
        (
            *snap_v1.objects,
            existing_via,
            LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 10.0), (12.0, 11.0)), net="VDD"),
            LayoutLabel(text="REV2", xy=(1.0, 1.0), layer="text", purpose="drawing"),
        ),
        cell_properties=snap_v1.cell_properties,
        metadata=snap_v1.manifest.metadata,
    )
    write_layout_snapshot(snap_v2, snap_dir)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "v2 routing and dup via")

    sub = repo / "sub" / "work"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    # 1. Auto-inferred single layout cellview with HEAD~1 HEAD + --stat + --exit-code
    #    (even when an unrelated schematic snapshot exists alongside it!)
    sch_dir = repo / "snapshots" / "work" / "inv_dut" / "schematic"
    sch_dir.mkdir(parents=True)
    (sch_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "oa-git/2",
                "view_type": "schematic",
                "cellview": {"library": "work", "cell": "inv_dut", "view": "schematic"},
            }
        ),
        encoding="utf-8",
    )
    rc = layout_cli_main(["diff", "HEAD~1", "HEAD", "--stat", "--exit-code"])
    assert rc == 1
    stat_out = capsys.readouterr().out
    assert "work/inv_dut/layout -> work/inv_dut/layout" in stat_out
    assert "Summary:" in stat_out
    assert "DUPLICATE_VIAS" not in stat_out
    assert "M2:drawing +1 / -0" in stat_out
    assert "Duplicate / overlapping vias" not in stat_out

    # 2. Explicit --cellview with --json
    rc = layout_cli_main(
        ["diff", "HEAD~1", "HEAD", "--cellview", "work/inv_dut/layout", "--stat", "--json"]
    )
    assert rc == 0
    diff_json = json.loads(capsys.readouterr().out)
    assert diff_json["ok"] is True
    assert diff_json["has_geometry_changes"] is True
    assert diff_json["has_connectivity_changes"] is False
    assert "has_duplicate_via_changes" not in diff_json
    assert "summary" in diff_json
    assert "stat" in diff_json

    # 3. --connectivity-only --exit-code -> exit 0 (no connectivity changes!)
    rc = layout_cli_main(
        ["diff", "HEAD~1", "HEAD", "--connectivity-only", "--exit-code"]
    )
    assert rc == 0
    conn_out = capsys.readouterr().out
    assert "Connectivity changes" in conn_out
    assert "Geometry changes" not in conn_out

    # 4. Mixed Git revision + local snapshot dir infers cellview from local dir
    local_v3 = build_layout_snapshot(
        SOURCE,
        (*snap_v2.objects, LayoutNet(name="CLK", sig_type="clock")),
        cell_properties=snap_v2.cell_properties,
        metadata=snap_v2.manifest.metadata,
    )
    local_v3_dir = write_layout_snapshot(local_v3, tmp_path / "local_v3")
    rc = layout_cli_main(["diff", "HEAD", str(local_v3_dir), "--connectivity-only", "--exit-code", "--json"])
    assert rc == 1
    mixed_json = json.loads(capsys.readouterr().out)
    assert mixed_json["has_connectivity_changes"] is True

    # 5. Ambiguous snapshots_dir (2 layout snapshots) without --cellview fails closed (exit 2)
    second_cv = CellViewRef("work", "other_block", "layout")
    write_layout_snapshot(
        build_layout_snapshot(second_cv, ()),
        repo / "snapshots" / "work" / "other_block" / "layout",
    )
    rc = layout_cli_main(["diff", "HEAD~1", "HEAD", "--json"])
    assert rc == 2
    ambig = json.loads(capsys.readouterr().out)
    assert ambig["ok"] is False
    assert ambig["error_type"] == "SchemaValidationError"
    assert "--cellview" in ambig["error"]


def test_cli_verify_surfaces_definitions_count_without_dup_via_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shared_via_1 = LayoutVia(
        via_def="M2_M1",
        origin=(1.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.03]},
    )
    shared_via_2 = LayoutVia(
        via_def="M2_M1",
        origin=(1.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.03]},
    )
    snap = build_layout_snapshot(SOURCE, (shared_via_1, shared_via_2))
    snap_dir = write_layout_snapshot(snap, tmp_path / "dup_verify")

    # JSON output includes object_count & definitions_count, no dup-via noise
    rc = layout_cli_main(["verify", str(snap_dir), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["object_count"] == 2
    assert data["definitions_count"] >= 1
    assert "duplicate_vias" not in data
    assert "duplicate_vias_count" not in data

    # Human summary stays clean without duplicate-via warnings
    rc = layout_cli_main(["verify", str(snap_dir)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "OK " in text
    assert f"definitions={data['definitions_count']}" in text
    assert "duplicate_vias" not in text
    assert "WARN:" not in text



