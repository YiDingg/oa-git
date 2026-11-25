"""End-to-end v0.2 integration tests for bridge, VirtuosoSkillBackend, and CLI."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

import pytest

from oa_git.bridge import (
    FixtureBackend,
    OaBackend,
    load_backend,
    parse_restore_response,
    raw_capture_to_snapshot,
    render_restore_request_skill,
)
from oa_git.cli import main
from oa_git.model import (
    BATCH_PROTOCOL_V1,
    CONFIG_SCHEMA_V2,
    SPOOL_PROTOCOL_V1,
    AnnotationShape,
    BridgeExecutionError,
    CdfCouplingRule,
    CdfParameterError,
    CellViewRef,
    DerivedState,
    DisallowedLibraryError,
    Instance,
    InstancePlacement,
    Net,
    Port,
    RoundtripMismatchError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
)
from oa_git.project_state import (
    load_index,
    load_project_config,
    write_project_config,
)
from oa_git.snapshot import (
    build_schematic_snapshot,
    compute_cdf_input_sha256,
    read_snapshot,
    write_snapshot,
)
from oa_git.symbol_model import (
    SymbolGraphics,
    SymbolLabel,
    SymbolPin,
    SymbolPinFigure,
    SymbolShape,
    SymbolSnapshot,
)
from oa_git.symbol_snapshot import (
    build_symbol_snapshot,
    read_symbol_snapshot,
    write_symbol_snapshot,
)
from oa_git.virtuoso_backend import VirtuosoSkillBackend
import oa_git.cli as cli_mod


# ---------------------------------------------------------------------------
# Helpers & Realistic Factories
# ---------------------------------------------------------------------------


def invoke_json(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], Any]:
    code = main([*args, "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    return code, payload, captured


def make_mos_schematic(
    cv: CellViewRef,
    *,
    w: str = "2u",
    fingers: int = 2,
    derived: DerivedState | None = None,
    annotations: tuple[AnnotationShape, ...] = (),
    child_master: tuple[str, str] | None = None,
) -> SchematicSnapshot:
    instances: list[Instance] = [
        Instance(
            name="M1",
            lib="tsmcN65",
            cell="nch",
            view="symbol",
            connections={"D": "OUT", "G": "IN", "S": "VSS", "B": "VSS"},
            params={"w": w, "fingers": fingers},
            param_types={"w": "string", "fingers": "int"},
            props={},
            derived=derived,
        )
    ]
    placements: list[InstancePlacement] = [InstancePlacement("M1", (1.0, 2.0), "R0")]
    if child_master is not None:
        c_lib, c_cell = child_master
        instances.append(
            Instance(
                name="I_SUB",
                lib=c_lib,
                cell=c_cell,
                view="symbol",
                connections={"IN": "IN", "OUT": "OUT"},
                params={},
                param_types={},
                props={},
            )
        )
        placements.append(InstancePlacement("I_SUB", (4.0, 2.0), "R0"))

    return build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"), Port("OUT", "output")),
            nets=(Net("IN"), Net("OUT"), Net("VSS", is_global=True)),
            instances=tuple(instances),
        ),
        SchematicPlacement(
            instances=tuple(placements),
            annotations=annotations,
        ),
    )


def make_symbol(
    cv: CellViewRef,
    *,
    in_xy: tuple[float, float] = (0.0, 0.0),
    out_dir: str = "output",
    extra_shape: SymbolShape | None = None,
) -> SymbolSnapshot:
    pins = (
        SymbolPin(
            name="IN",
            direction="input",
            xy=in_xy,
            orient="R0",
            pin_figure=SymbolPinFigure(
                kind="rect",
                layer="pin",
                purpose="drawing",
                bbox=((in_xy[0] - 0.025, in_xy[1] - 0.025), (in_xy[0] + 0.025, in_xy[1] + 0.025)),
            ),
        ),
        SymbolPin(
            name="OUT",
            direction=out_dir,
            xy=(2.0, 0.0),
            orient="R0",
            pin_figure=SymbolPinFigure(
                kind="rect",
                layer="pin",
                purpose="drawing",
                bbox=((1.975, -0.025), (2.025, 0.025)),
            ),
        ),
    )
    shapes: list[SymbolShape] = [
        SymbolShape(kind="rect", layer="device", purpose="drawing", bbox=((0.2, -0.5), (1.8, 0.5))),
    ]
    if extra_shape is not None:
        shapes.append(extra_shape)
    graphics = SymbolGraphics(
        shapes=tuple(shapes),
        labels=(SymbolLabel(text="IN", xy=(0.3, 0.0), layer="pin", purpose="label"),),
        nlp_labels=(
            SymbolLabel(
                text="[@instanceName]",
                xy=(1.0, 0.6),
                layer="instance",
                purpose="label",
                label_type="NLPLabel",
            ),
        ),
        il_labels=(
            SymbolLabel(
                text="cdsName()",
                xy=(1.0, -0.6),
                layer="annotate",
                purpose="drawing7",
                label_type="ILLabel",
            ),
        ),
        selection_box=((-0.05, -0.6), (2.05, 0.6)),
        origin=(0.0, 0.0),
    )
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("IN", "OUT"),
        graphics=graphics,
        properties={"partName": cv.cell},
    )


def raw_schematic_payload(
    cv: CellViewRef,
    *,
    with_annotations: bool = True,
    param_order: list[str] | None = None,
    derived_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "error": None,
        "cellview": cv.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematic", "dbu_per_uu": 160.0},
        "ports": [{"name": "IN", "direction": "input"}, {"name": "OUT", "direction": "output"}],
        "nets": [
            {"name": "IN", "is_global": False, "num_bits": 1},
            {"name": "OUT", "is_global": False, "num_bits": 1},
            {"name": "VSS", "is_global": True, "num_bits": 1},
        ],
        "instances": [
            {
                "name": "M1",
                "lib": "tsmcN65",
                "cell": "nch",
                "view": "symbol",
                "xy": [1.0, 2.0],
                "orient": "R0",
                "connections": {"D": "OUT", "G": "IN", "S": "VSS", "B": "VSS"},
                "params": {"w": "2u", "fingers": 2},
                "param_types": {"w": "string", "fingers": "int"},
                "param_order": ["w", "fingers"] if param_order is None else param_order,
                "derived_params": {"wf": "1u", "totalW": "4u"} if derived_params is None else derived_params,
                "props": {},
            }
        ],
        "pins": [],
        "wires": [],
        "labels": [],
        "properties": {},
        "constraints": {},
        "sheet": {},
        "omissions": [],
    }
    if with_annotations:
        payload["annotations"] = [
            {
                "kind": "rect",
                "layer": "text",
                "purpose": "drawing",
                "points": [[0.0, 0.0], [3.0, 2.0]],
                "width": 0.0,
            }
        ]
    return payload


def raw_symbol_payload(cv: CellViewRef) -> dict[str, Any]:
    return {
        "ok": True,
        "error": None,
        "cellview": cv.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematicSymbol", "dbu_per_uu": 160.0},
        "pins": [
            {
                "name": "IN",
                "direction": "input",
                "xy": [0.0, 0.0],
                "orient": "R0",
                "pin_figure": {
                    "kind": "rect",
                    "layer": "pin",
                    "purpose": "drawing",
                    "bbox": [[-0.025, -0.025], [0.025, 0.025]],
                },
            },
            {
                "name": "OUT",
                "direction": "output",
                "xy": [2.0, 0.0],
                "orient": "R0",
                "pin_figure": {
                    "kind": "rect",
                    "layer": "pin",
                    "purpose": "drawing",
                    "bbox": [[1.975, -0.025], [2.025, 0.025]],
                },
            },
        ],
        "port_order": ["IN", "OUT"],
        "graphics": {
            "shapes": [
                {
                    "kind": "rect",
                    "layer": "device",
                    "purpose": "drawing",
                    "bbox": [[0.2, -0.5], [1.8, 0.5]],
                    "fill": False,
                }
            ],
            "labels": [
                {
                    "text": "IN",
                    "xy": [0.3, 0.0],
                    "layer": "pin",
                    "purpose": "label",
                    "justify": "centerCenter",
                    "orient": "R0",
                    "font": "stick",
                    "height": 0.0625,
                    "label_type": "normalLabel",
                }
            ],
            "nlp_labels": [
                {
                    "text": "[@instanceName]",
                    "xy": [1.0, 0.6],
                    "layer": "instance",
                    "purpose": "label",
                    "justify": "centerCenter",
                    "orient": "R0",
                    "font": "stick",
                    "height": 0.0625,
                    "label_type": "NLPLabel",
                }
            ],
            "il_labels": [
                {
                    "text": "cdsName()",
                    "xy": [1.0, -0.6],
                    "layer": "annotate",
                    "purpose": "drawing7",
                    "justify": "centerCenter",
                    "orient": "R0",
                    "font": "stick",
                    "height": 0.0625,
                    "label_type": "ILLabel",
                }
            ],
            "selection_box": [[-0.05, -0.6], [2.05, 0.6]],
            "origin": [0.0, 0.0],
        },
        "properties": {"partName": cv.cell},
        "omissions": [],
    }


# ---------------------------------------------------------------------------
# 1. Bridge v0.2 Wire Enhancements (annotations, DerivedState, param_order, errors)
# ---------------------------------------------------------------------------


def test_bridge_raw_capture_annotations_param_order_and_restore_wire() -> None:
    cv = CellViewRef("work", "opamp", "schematic")
    snap = raw_capture_to_snapshot(raw_schematic_payload(cv, with_annotations=True))
    assert len(snap.placement.annotations) == 1
    assert snap.placement.annotations[0].kind == "rect"
    inst = snap.electrical.instances[0]
    assert inst.derived is not None
    assert inst.derived.status == "valid"
    assert inst.derived.source == "cdf-readback"
    assert inst.derived.callback_order == ("w", "fingers")
    assert inst.derived.values == {"totalW": "4u", "wf": "1u"}
    assert inst.derived.input_sha256 == compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "2u", "fingers": 2}, {"w": "string", "fingers": "int"}
    )

    # Empty derived & empty param_order -> status='unavailable'
    snap_unavail = raw_capture_to_snapshot(
        raw_schematic_payload(cv, with_annotations=False, param_order=[], derived_params={})
    )
    assert snap_unavail.electrical.instances[0].effective_derived.status == "unavailable"

    # render_restore_request_skill respects coupling rules topological order & annotations
    rule = CdfCouplingRule(
        name="mos_w",
        members=("w", "fingers", "wf"),
        primary=("w", "fingers"),
        derived=("wf",),
        order=("fingers", "w"),
        dependencies={"w": ("fingers",), "fingers": ()},
    )
    target = CellViewRef("sandbox", "opamp", "schematic")
    rendered = render_restore_request_skill(
        snap,
        target,
        draft_cell="opamp__draft_1",
        overwrite=True,
        allow_libraries=("sandbox",),
        output_path=Path("/tmp/restore.json"),
        max_passes=5,
        recompute_derived=True,
        coupling_rules={"tsmcN65/nch": (rule,)},
    )
    assert "max_passes 5" in rendered
    assert "recompute_derived t" in rendered
    assert 'param_order ("fingers" "w")' in rendered
    assert 'annotations ((nil kind "rect" layer "text" purpose "drawing"' in rendered

    # Cyclic coupling rule fails closed with CdfParameterError('cdf_order_cycle')
    cyclic_rule = CdfCouplingRule(
        name="cycle",
        members=("w", "fingers"),
        primary=("w", "fingers"),
        dependencies={"w": ("fingers",), "fingers": ("w",)},
    )
    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        render_restore_request_skill(
            snap,
            target,
            draft_cell="opamp__draft_2",
            overwrite=True,
            allow_libraries=("sandbox",),
            output_path=Path("/tmp/restore.json"),
            coupling_rules={"tsmcN65/nch": (cyclic_rule,)},
        )

    # parse_restore_response maps all v0.2 CDF error codes -> CdfParameterError
    for code in (
        "cdf_order_cycle",
        "cdf_contradiction",
        "cdf_nonconvergent",
        "cdf_readback_mismatch",
    ):
        with pytest.raises(CdfParameterError, match=code):
            parse_restore_response(
                {"ok": False, "error_code": code, "error": "failed"},
                target,
            )

    # parse_restore_response populates v0.2 receipt fields
    receipt = parse_restore_response(
        {
            "ok": True,
            "target": target.to_dict(),
            "published": True,
            "draft_cell": "opamp__draft_1",
            "sch_check": {"errors": 0, "warnings": 0},
            "cdf_verified": True,
            "readback_verified": True,
            "connectivity_mismatches": [],
            "derived_status": "valid",
            "derived_refresh": {"M1": {"wf": "1u"}},
            "cdf_passes": 2,
            "callback_order": {"M1": ["w", "fingers"]},
            "transport": "daemon",
        },
        target,
    )
    assert receipt.derived_status == "valid"
    assert receipt.derived_refresh == {"M1": {"wf": "1u"}}
    assert receipt.cdf_passes == 2
    assert receipt.callback_order == {"M1": ["w", "fingers"]}
    assert receipt.transport == "daemon"

    assert isinstance(FixtureBackend(), OaBackend)
    assert isinstance(VirtuosoSkillBackend(), OaBackend)


# ---------------------------------------------------------------------------
# 2. Upward .oagit/config.json Discovery from Nested Subdirectory
# ---------------------------------------------------------------------------


def test_upward_config_discovery_from_nested_subdirectory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "chip"
    nested = project / "blocks" / "analog" / "opamp"
    nested.mkdir(parents=True)
    oa_work = project / "oa" / "work"
    oa_work.mkdir(parents=True)

    cv = CellViewRef("work", "opamp", "schematic")
    fixture_dir = tmp_path / "fixture"
    snap = make_mos_schematic(cv)
    write_snapshot(snap, fixture_dir / "work" / "opamp" / "schematic")

    # Initialize v2 config via CLI (--oa-lib triggers .oagit/config.json)
    code, init_out, _ = invoke_json(
        [
            "init",
            "--root",
            str(project),
            "--oa-lib",
            f"work={oa_work}",
            "--allow-library",
            "sandbox",
            "--backend",
            "fixture",
            "--daemon-idle-timeout",
            "45",
        ],
        capsys,
    )
    assert code == 0
    assert init_out["config"]["schema"] == CONFIG_SCHEMA_V2
    assert (project / ".oagit" / "config.json").is_file()
    assert load_project_config(project)["daemon"]["idle_timeout_s"] == 45

    # Run status, capture, restore, diff from deeply nested subdir WITHOUT --root!
    monkeypatch.chdir(nested)

    code, cap_out, _ = invoke_json(
        ["capture", cv.key, "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert Path(cap_out["snapshot_dir"]) == (project / "snapshots" / "work" / "opamp" / "schematic").resolve()

    code, st_out, _ = invoke_json(["status"], capsys)
    assert code == 0
    assert st_out["ok"] is True
    assert Path(st_out["root"]) == project.resolve()
    assert len(st_out["snapshots"]) == 1

    code, res_out, _ = invoke_json(
        [
            "restore",
            "snapshots/work/opamp/schematic",
            "--fixture",
            str(fixture_dir),
            "--target",
            "sandbox/opamp/schematic",
        ],
        capsys,
    )
    assert code == 0
    assert res_out["published"] is True
    assert res_out["readback_verified"] is True

    edited_dir = write_snapshot(make_mos_schematic(cv, w="4u"), tmp_path / "edited")
    code, diff_out, _ = invoke_json(
        ["diff", "snapshots/work/opamp/schematic", str(edited_dir), "--exit-code"],
        capsys,
    )
    assert code == 1
    assert diff_out["ok"] is True
    assert diff_out["electrical"]["param_changes"]

    # Missing config outside any project returns exit code 2 prompting 'oagit init'
    outside = tmp_path / "uninitialized"
    outside.mkdir()
    monkeypatch.chdir(outside)
    code, err_out, _ = invoke_json(["status"], capsys)
    assert code == 2
    assert "oagit init" in err_out["error"]


# ---------------------------------------------------------------------------
# 3. Dirty Index Skip (--changed -> 0 backend calls on clean & mtime-only drift)
# ---------------------------------------------------------------------------


def test_dirty_index_skip_changed_zero_backend_calls_and_mtime_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    oa_work = project / "oa" / "work"
    fixture_dir = tmp_path / "fixtures"

    cv_clean = CellViewRef("work", "clean_blk", "schematic")
    cv_dirty = CellViewRef("work", "dirty_blk", "schematic")

    for cv in (cv_clean, cv_dirty):
        oa_file = oa_work / cv.cell / "schematic" / "sch.oa"
        oa_file.parent.mkdir(parents=True, exist_ok=True)
        oa_file.write_bytes(f"OA_{cv.cell}_V1".encode())
        write_snapshot(make_mos_schematic(cv), fixture_dir / cv.library / cv.cell / cv.view)

    write_project_config(
        project,
        {
            "oa_libs": {"work": str(oa_work)},
            "allow_libraries": ["work"],
            "backend": "fixture",
        },
    )
    monkeypatch.chdir(project)

    # First capture populates snapshots + .oagit/index.json
    code, cap1, _ = invoke_json(
        ["capture", cv_clean.key, cv_dirty.key, "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert len(cap1["captured"]) == 2
    idx = load_index(project)
    assert set(idx) == {cv_clean.key, cv_dirty.key}

    # Verify status reports both clean
    code, st1, _ = invoke_json(["status"], capsys)
    assert code == 0
    assert {item["key"]: item["state"] for item in st1["index_states"]} == {
        cv_clean.key: "clean",
        cv_dirty.key: "clean",
    }

    # Spy on load_backend to guarantee ZERO backend calls when clean!
    backend_calls: list[Any] = []
    real_backend = cli_mod._backend

    def spy_backend(settings, **kw):
        backend_calls.append(settings)
        return real_backend(settings, **kw)

    monkeypatch.setattr(cli_mod, "_backend", spy_backend)

    # --changed with all clean -> 0 backend calls, both skipped
    code, skip_all, _ = invoke_json(["capture", "--changed", "--fixture", str(fixture_dir)], capsys)
    assert code == 0
    assert skip_all["transport"] == "index_skip"
    assert skip_all["captured"] == []
    assert len(skip_all["skipped"]) == 2
    assert len(backend_calls) == 0

    # mtime-only change with identical SHA-256 -> STILL clean, STILL 0 backend calls!
    clean_oa = oa_work / cv_clean.cell / "schematic" / "sch.oa"
    st = clean_oa.stat()
    os.utime(clean_oa, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    code, skip_mtime, _ = invoke_json(
        ["capture", cv_clean.key, "--changed", "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert skip_mtime["transport"] == "index_skip"
    assert len(skip_mtime["skipped"]) == 1
    assert len(backend_calls) == 0

    # Modify dirty_blk content -> captures ONLY dirty_blk, skips clean_blk!
    dirty_oa = oa_work / cv_dirty.cell / "schematic" / "sch.oa"
    dirty_oa.write_bytes(b"OA_dirty_blk_V2_CHANGED")
    write_snapshot(
        make_mos_schematic(cv_dirty, w="4u"),
        fixture_dir / cv_dirty.library / cv_dirty.cell / cv_dirty.view,
    )
    code, cap_partial, _ = invoke_json(
        ["capture", "--changed", "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert len(backend_calls) == 1
    assert [c["cellview"]["cell"] for c in cap_partial["captured"]] == ["dirty_blk"]
    assert [s["cellview"]["cell"] for s in cap_partial["skipped"]] == ["clean_blk"]
    assert read_snapshot(project / "snapshots" / "work" / "dirty_blk" / "schematic").electrical.instances[0].params["w"] == "4u"


# ---------------------------------------------------------------------------
# 4. Single-Session --hier + --include-symbols Batch Capture
# ---------------------------------------------------------------------------


def test_single_session_hier_batch_capture_schematic_and_symbol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "hier_proj"
    fixture_dir = tmp_path / "hier_fixtures"
    oa_work = project / "oa" / "work"
    oa_work.mkdir(parents=True)

    top_sch = CellViewRef("work", "top", "schematic")
    top_sym = CellViewRef("work", "top", "symbol")
    sub_sch = CellViewRef("work", "sub", "schematic")
    sub_sym = CellViewRef("work", "sub", "symbol")

    write_snapshot(
        make_mos_schematic(top_sch, child_master=("work", "sub")),
        fixture_dir / "work" / "top" / "schematic",
    )
    write_symbol_snapshot(make_symbol(top_sym), fixture_dir / "work" / "top" / "symbol")
    write_snapshot(make_mos_schematic(sub_sch), fixture_dir / "work" / "sub" / "schematic")
    write_symbol_snapshot(make_symbol(sub_sym), fixture_dir / "work" / "sub" / "symbol")

    write_project_config(
        project,
        {
            "oa_libs": {"work": str(oa_work)},
            "allow_libraries": ["work"],
            "backend": "fixture",
        },
    )
    monkeypatch.chdir(project)

    code, out, _ = invoke_json(
        ["capture", top_sch.key, "--hier", "--include-symbols", "--fixture", str(fixture_dir)],
        capsys,
    )
    assert code == 0
    assert out["ok"] is True
    captured_keys = {
        f"{c['cellview']['library']}/{c['cellview']['cell']}/{c['cellview']['view']}"
        for c in out["captured"]
    }
    assert captured_keys == {top_sch.key, top_sym.key, sub_sch.key, sub_sym.key}
    assert (project / "snapshots" / "work" / "sub" / "symbol" / "symbol.yaml").is_file()
    assert (project / "snapshots" / "work" / "top" / "schematic" / "circuit.yaml").is_file()

    # Also verify VirtuosoSkillBackend.capture_batch runs in a SINGLE Virtuoso invocation!
    runner_calls: list[list[str]] = []

    def fake_batch_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        runner_calls.append(cmd)
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        # Extract request_id from request.il
        marker = 'request_id "'
        req_id = req_text.split(marker, 1)[1].split('"', 1)[0]
        driver_text = (run_dir / "driver.il").read_text(encoding="utf-8")
        assert "export_schematic.il" in driver_text
        assert "export_symbol.il" in driver_text
        assert "batch_capture.il" in driver_text
        resp = {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": req_id,
            "ok": True,
            "results": [
                {"cellview": top_sch.to_dict(), "ok": True, "capture": raw_schematic_payload(top_sch)},
                {"cellview": sub_sym.to_dict(), "ok": True, "capture": raw_symbol_payload(sub_sym)},
            ],
            "discovered": [sub_sym.to_dict()],
            "cycles": [],
            "errors": [],
            "session": {"daemon_id": None, "reused": False},
        }
        (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    v_backend = VirtuosoSkillBackend(runner=fake_batch_runner)
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    batch_res = v_backend.capture_batch(
        [top_sch], cds_lib=cds, hier=True, include_symbols=True
    )
    assert len(runner_calls) == 1
    assert batch_res.ok is True
    assert len(batch_res.items) == 2
    assert isinstance(batch_res.items[0].snapshot, SchematicSnapshot)
    assert isinstance(batch_res.items[1].snapshot, SymbolSnapshot)
    assert batch_res.discovered == (sub_sym,)


# ---------------------------------------------------------------------------
# 5. Symbol CLI End-to-End (capture, status, diff contract-only vs full, merge, restore)
# ---------------------------------------------------------------------------


def test_symbol_cli_end_to_end_lifecycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "sym_proj"
    fixture_dir = tmp_path / "sym_fixtures"
    sym_cv = CellViewRef("work", "amp", "symbol")
    target_sym = CellViewRef("sandbox", "amp", "symbol")

    base_sym = make_symbol(sym_cv)
    write_symbol_snapshot(base_sym, fixture_dir / "work" / "amp" / "symbol")

    write_project_config(
        project,
        {
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
        },
    )
    monkeypatch.chdir(project)

    # Capture symbol
    code, cap_out, _ = invoke_json(
        ["capture", sym_cv.key, "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert cap_out["view_type"] == "symbol"
    assert cap_out["content_sha256"] == base_sym.manifest.content_sha256
    snap_dir = Path(cap_out["snapshot_dir"])
    assert read_symbol_snapshot(snap_dir, verify=True) == base_sym

    # Status verifies symbol snapshot cleanly
    code, st_out, _ = invoke_json(["status"], capsys)
    assert code == 0
    assert st_out["ok"] is True
    assert st_out["snapshots"][0]["view_type"] == "symbol"

    # Diff: geometry-only move (same pin_figure contract, moved xy/orient) -> contract-only exit 0, full diff exit 1
    moved_sym = build_symbol_snapshot(
        sym_cv,
        pins=(replace(base_sym.pins[0], xy=(0.0, 0.25), orient="R90"), base_sym.pins[1]),
        port_order=base_sym.port_order,
        graphics=base_sym.graphics,
        properties=base_sym.properties,
    )
    moved_dir = write_symbol_snapshot(moved_sym, tmp_path / "moved_sym")
    code, d_contract, _ = invoke_json(
        ["diff", str(snap_dir), str(moved_dir), "--contract-only", "--exit-code"], capsys
    )
    assert code == 0
    assert d_contract["view_type"] == "symbol"
    assert "pin_geometry_changes" not in d_contract

    code, d_full, _ = invoke_json(
        ["diff", str(snap_dir), str(moved_dir), "--exit-code"], capsys
    )
    assert code == 1
    assert d_full["pin_geometry_changes"]["moves"]

    # Diff: contract change -> --contract-only exit 1
    contract_sym = make_symbol(sym_cv, out_dir="inputOutput")
    contract_dir = write_symbol_snapshot(contract_sym, tmp_path / "contract_sym")
    code, d_breaking, _ = invoke_json(
        ["diff", str(snap_dir), str(contract_dir), "--contract-only", "--exit-code"], capsys
    )
    assert code == 1
    assert d_breaking["pin_contract_changes"]["pins"]

    # Merge symbol cleanly (ours moves pin, theirs adds body shape)
    theirs_sym = make_symbol(
        sym_cv,
        extra_shape=SymbolShape(kind="line", layer="device", purpose="drawing", points=((0.0, 0.0), (0.2, 0.0))),
    )
    theirs_dir = write_symbol_snapshot(theirs_sym, tmp_path / "theirs_sym")
    merged_dir = tmp_path / "merged_sym"
    code, merge_out, _ = invoke_json(
        ["merge", str(snap_dir), str(moved_dir), str(theirs_dir), "-o", str(merged_dir)], capsys
    )
    assert code == 0
    assert merge_out["ok"] is True
    merged_loaded = read_symbol_snapshot(merged_dir, verify=True)
    assert merged_loaded.pins[0].xy == (0.0, 0.25)
    assert len(merged_loaded.graphics.shapes) == 2

    # Restore symbol via CLI (allowlist enforced + happy path)
    code, res_ok, _ = invoke_json(
        [
            "restore",
            str(merged_dir),
            "--fixture",
            str(fixture_dir),
            "--target",
            target_sym.key,
        ],
        capsys,
    )
    assert code == 0
    assert res_ok["ok"] is True
    assert res_ok["view_type"] == "symbol"
    assert res_ok["published"] is True
    assert res_ok["readback_verified"] is True

    code, res_blocked, _ = invoke_json(
        [
            "restore",
            str(merged_dir),
            "--fixture",
            str(fixture_dir),
            "--target",
            "forbidden/amp/symbol",
        ],
        capsys,
    )
    assert code == 2
    assert res_blocked["error_type"] == "DisallowedLibraryError"

    # Git revision diff for symbol snapshots works seamlessly
    repo = tmp_path / "sym_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    tracked = repo / "snapshots" / "work" / "amp" / "symbol"
    write_symbol_snapshot(base_sym, tracked)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sym1"], cwd=repo, check=True)
    write_symbol_snapshot(contract_sym, tracked)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sym2"], cwd=repo, check=True)

    code, git_sym_diff, _ = invoke_json(
        ["diff", "HEAD~1", "HEAD", "--root", str(repo), "--cellview", sym_cv.key, "--exit-code"],
        capsys,
    )
    assert code == 1
    assert git_sym_diff["view_type"] == "symbol"
    assert git_sym_diff["pin_contract_changes"]["pins"]


# ---------------------------------------------------------------------------
# 6. Coupled CDF Merge + Restore --recompute-derived + cdf_order_cycle Fail-Closed
# ---------------------------------------------------------------------------


def test_coupled_cdf_merge_restore_recompute_and_cycle_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "cdf_proj"
    fixture_dir = tmp_path / "cdf_fixtures"
    cv = CellViewRef("work", "nmos_stage", "schematic")
    target = CellViewRef("sandbox", "nmos_stage", "schematic")

    mos_rule_dict = {
        "name": "mos_width",
        "members": ["w", "fingers", "wf", "totalW"],
        "primary": ["w", "fingers"],
        "derived": ["wf", "totalW"],
        "order": ["w", "fingers"],
        "dependencies": {"w": [], "fingers": ["w"]},
    }
    mos_rule = CdfCouplingRule.from_dict(mos_rule_dict)

    write_project_config(
        project,
        {
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
            "cdf": {
                "max_passes": 3,
                "coupling_rules": {"tsmcN65/nch": [mos_rule_dict]},
            },
        },
    )
    monkeypatch.chdir(project)

    sha_base = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "2u", "fingers": 2}, {"w": "string", "fingers": "int"}, (mos_rule,)
    )
    sha_ours = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "4u", "fingers": 2}, {"w": "string", "fingers": "int"}, (mos_rule,)
    )
    sha_theirs = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "2u", "fingers": 4}, {"w": "string", "fingers": "int"}, (mos_rule,)
    )

    base = make_mos_schematic(
        cv,
        w="2u",
        fingers=2,
        derived=DerivedState("valid", {"wf": "1u", "totalW": "4u"}, sha_base, ("w", "fingers")),
    )
    ours = make_mos_schematic(
        cv,
        w="4u",
        fingers=2,
        derived=DerivedState("valid", {"wf": "2u", "totalW": "8u"}, sha_ours, ("w", "fingers")),
    )
    theirs = make_mos_schematic(
        cv,
        w="2u",
        fingers=4,
        derived=DerivedState("valid", {"wf": "500n", "totalW": "8u"}, sha_theirs, ("w", "fingers")),
    )
    base_dir = write_snapshot(base, tmp_path / "base")
    ours_dir = write_snapshot(ours, tmp_path / "ours")
    theirs_dir = write_snapshot(theirs, tmp_path / "theirs")
    merged_dir = tmp_path / "merged"

    # Merge concurrent w & fingers -> zero conflicts, derived invalidated to stale!
    code, merge_out, _ = invoke_json(
        ["merge", str(base_dir), str(ours_dir), str(theirs_dir), "-o", str(merged_dir)], capsys
    )
    assert code == 0
    assert merge_out["ok"] is True
    merged_snap = read_snapshot(merged_dir, verify=True)
    merged_inst = merged_snap.electrical.instances[0]
    assert merged_inst.params == {"w": "4u", "fingers": 4}
    assert merged_inst.derived is not None
    assert merged_inst.derived.status == "stale"
    assert merged_inst.derived.values == {}

    # Restore merged snapshot with --recompute-derived
    fixture_dir.mkdir(parents=True)
    code, restore_out, _ = invoke_json(
        [
            "restore",
            str(merged_dir),
            "--fixture",
            str(fixture_dir),
            "--target",
            target.key,
            "--recompute-derived",
        ],
        capsys,
    )
    assert code == 0
    assert restore_out["published"] is True
    assert restore_out["derived_status"] == "valid"
    assert restore_out["callback_order"] == {"M1": ["w", "fingers"]}
    assert "M1" in restore_out["derived_refresh"]

    # Cyclic coupling rule in config -> write_project_config fails closed AND hand-edited config fails CLI restore!
    cyclic_config_payload = {
        "schema": CONFIG_SCHEMA_V2,
        "allow_libraries": ["sandbox"],
        "backend": "fixture",
        "cdf": {
            "max_passes": 3,
            "coupling_rules": {
                "tsmcN65/nch": [
                    {
                        "name": "bad_cycle",
                        "members": ["w", "fingers"],
                        "primary": ["w", "fingers"],
                        "dependencies": {"w": ["fingers"], "fingers": ["w"]},
                    }
                ]
            },
        },
    }
    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        write_project_config(project, cyclic_config_payload, force=True)

    # Simulate a hand-edited cyclic config on disk -> CLI restore STILL fails closed!
    (project / ".oagit" / "config.json").write_text(
        json.dumps(cyclic_config_payload, indent=2) + "\n", encoding="utf-8"
    )
    code, err_out, _ = invoke_json(
        ["restore", str(merged_dir), "--fixture", str(fixture_dir), "--target", target.key],
        capsys,
    )
    assert code == 2
    assert err_out["error_type"] == "CdfParameterError"
    assert "cdf_order_cycle" in err_out["error"]


# ---------------------------------------------------------------------------
# 7. TSMC65 Exit 139 / Corrupted Double-Linked List Crash Retry (NO retry on business error)
# ---------------------------------------------------------------------------


def test_virtuoso_crash_retry_succeeds_in_fresh_run_and_never_retries_business_errors(
    tmp_path: Path,
) -> None:
    cv = CellViewRef("work", "bgr", "schematic")
    target = CellViewRef("sandbox", "bgr", "schematic")
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")

    # 1) Exit 139 + corrupted double-linked list with no response.json -> retries once in fresh run_id!
    run_dirs: list[str] = []
    outcomes = deque(["crash_139", raw_schematic_payload(cv)])

    def crash_then_ok(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(str(kwargs["cwd"]))
        run_dirs.append(run_dir.name)
        item = outcomes.popleft()
        if item == "crash_139":
            return subprocess.CompletedProcess(
                cmd, 139, "", "corrupted double-linked list\nAborted\n"
            )
        (run_dir / "response.json").write_text(json.dumps(item), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = VirtuosoSkillBackend(runner=crash_then_ok, crash_retries=1)
    snap = backend.capture_schematic(cv, cds_lib=cds)
    assert snap.manifest.cellview == cv
    assert len(run_dirs) == 2
    assert run_dirs[0] != run_dirs[1]  # Fresh isolated run_id!

    # 2) Persistent crash exhausts crash_retries and raises BridgeExecutionError
    persistent_calls = 0

    def always_crash(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal persistent_calls
        persistent_calls += 1
        return subprocess.CompletedProcess(cmd, -11, "", "Segmentation fault")

    backend_fail = VirtuosoSkillBackend(runner=always_crash, crash_retries=1)
    with pytest.raises(BridgeExecutionError, match="2 attempt"):
        backend_fail.capture_schematic(cv, cds_lib=cds)
    assert persistent_calls == 2

    # 3) Business error (valid JSON with CdfParameterError) is NEVER retried!
    cdf_calls = 0

    def cdf_error_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal cdf_calls
        cdf_calls += 1
        run_dir = Path(str(kwargs["cwd"]))
        (run_dir / "response.json").write_text(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "cdf_contradiction",
                    "error": "w and wf conflict",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend_cdf = VirtuosoSkillBackend(runner=cdf_error_runner, crash_retries=2)
    with pytest.raises(CdfParameterError, match="cdf_contradiction"):
        backend_cdf.restore_schematic(
            snap,
            target,
            cds_lib=cds,
            allow_libraries=("sandbox",),
            verify_readback=False,
        )
    assert cdf_calls == 1  # Zero retries on valid JSON business errors!


# ---------------------------------------------------------------------------
# 8. Single-Session Inline Readback Restore (1 Virtuoso launch instead of 2)
# ---------------------------------------------------------------------------


def test_single_session_inline_readback_restore_uses_one_runner_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cv = CellViewRef("work", "opamp", "schematic")
    target = CellViewRef("sandbox", "opamp", "schematic")
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    snap = raw_capture_to_snapshot(raw_schematic_payload(cv))

    calls: list[tuple[list[str], str]] = []

    def single_session_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        driver_text = (run_dir / "driver.il").read_text(encoding="utf-8")
        calls.append((cmd, driver_text))
        assert "export_schematic.il" in driver_text
        assert "restore_schematic.il" in driver_text
        assert "batch_restore.il" in driver_text
        req_id = req_text.split('request_id "', 1)[1].split('"', 1)[0]
        resp = {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": req_id,
            "ok": True,
            "results": [
                {
                    "target": target.to_dict(),
                    "ok": True,
                    "published": True,
                    "draft_cell": "opamp__oagit_draft_1",
                    "sch_check": {"errors": 0, "warnings": 0},
                    "cdf_verified": True,
                    "connectivity_mismatches": [],
                    "readback": raw_schematic_payload(target),
                    "error": None,
                    "error_code": None,
                }
            ],
            "errors": [],
            "session": {"daemon_id": None, "reused": False},
        }
        (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = VirtuosoSkillBackend(runner=single_session_runner, inline_readback=True)
    receipt = backend.restore_schematic(
        snap,
        target,
        cds_lib=cds,
        allow_libraries=("sandbox",),
        verify_readback=True,
        recompute_derived=True,
    )
    assert len(calls) == 1  # Single Virtuoso cold start for restore + readback!
    assert receipt.published is True
    assert receipt.readback_verified is True
    assert receipt.derived_status == "valid"

    # Also verify CLI restore defaults to inline_readback=True for one-shot Virtuoso!
    project = tmp_path / "cli_inline"
    write_project_config(
        project,
        {
            "cds_lib": str(cds),
            "allow_libraries": ["sandbox"],
            "backend": "virtuoso",
            "daemon": {"enabled": False},
        },
    )
    snap_dir = write_snapshot(snap, project / "snapshots" / "work" / "opamp" / "schematic")
    monkeypatch.chdir(project)
    cli_calls: list[int] = []

    def cli_backend_factory(settings: Any, *, inline_readback: bool = False):
        assert inline_readback is True

        def _runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            cli_calls.append(1)
            return single_session_runner(cmd, **kwargs)

        return VirtuosoSkillBackend(runner=_runner, inline_readback=inline_readback)

    monkeypatch.setattr(cli_mod, "_backend", cli_backend_factory)
    code, res_json, _ = invoke_json(
        ["restore", str(snap_dir), "--target", target.key, "--one-shot"], capsys
    )
    assert code == 0
    assert res_json["readback_verified"] is True
    assert len(cli_calls) == 1


# ---------------------------------------------------------------------------
# 9. oagit daemon status/start/stop & Crash Fallback transport='one_shot_fallback'
# ---------------------------------------------------------------------------


def test_daemon_cli_lifecycle_and_backend_crash_fallback_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "daemon_proj"
    cds = project / "cds.lib"
    project.mkdir(parents=True)
    cds.write_text("", encoding="utf-8")
    write_project_config(
        project,
        {
            "cds_lib": "cds.lib",
            "allow_libraries": ["sandbox"],
            "backend": "virtuoso",
            "daemon": {"enabled": True, "idle_timeout_s": 60},
        },
    )
    monkeypatch.chdir(project)

    # 1) oagit daemon status (stopped initially)
    code, st0, _ = invoke_json(["daemon", "status"], capsys)
    assert code == 0
    assert st0["ok"] is True
    assert st0["running"] is False
    assert st0["state"] == "stopped"

    # 2) oagit daemon start & stop using a fake VirtuosoDaemonClient
    fake_state = {"running": False, "pid": None, "idle": None}

    class _FakeDaemonClient:
        daemon_id = "deadbeef01234567"

        def status(self) -> dict[str, Any]:
            return {
                "daemon_id": self.daemon_id,
                "protocol": SPOOL_PROTOCOL_V1,
                "running": fake_state["running"],
                "state": "ready" if fake_state["running"] else "stopped",
                "pid": fake_state["pid"],
            }

        def start(self, *, idle_timeout_s: int | None = None) -> dict[str, Any]:
            reused = fake_state["running"]
            fake_state["running"] = True
            fake_state["pid"] = 51515
            fake_state["idle"] = idle_timeout_s
            return {**self.status(), "reused": reused}

        def stop(self) -> dict[str, Any]:
            was = fake_state["running"]
            fake_state["running"] = False
            fake_state["pid"] = None
            return {"daemon_id": self.daemon_id, "stopped": True, "was_running": was}

    monkeypatch.setattr(cli_mod, "_daemon_client", lambda *a, **k: _FakeDaemonClient())

    code, started, _ = invoke_json(["daemon", "start", "--idle-timeout-s", "30"], capsys)
    assert code == 0
    assert started["running"] is True
    assert started["pid"] == 51515
    assert fake_state["idle"] == 30

    code, st1, _ = invoke_json(["daemon", "status"], capsys)
    assert code == 0
    assert st1["running"] is True

    code, stopped, _ = invoke_json(["daemon", "stop"], capsys)
    assert code == 0
    assert stopped["stopped"] is True
    assert stopped["was_running"] is True

    # 3) VirtuosoSkillBackend daemon crash -> falls back ONCE to one-shot with transport='one_shot_fallback'!
    cv = CellViewRef("work", "amp", "schematic")
    target = CellViewRef("sandbox", "amp", "schematic")
    snap = raw_capture_to_snapshot(raw_schematic_payload(cv))

    class _DyingProc:
        pid = 62626

        def poll(self) -> int:
            return 139  # Dies immediately before ready!

    fallback_runs = 0

    def one_shot_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal fallback_runs
        fallback_runs += 1
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        if 'action "restore_schematic"' in req_text:
            (run_dir / "response.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "target": target.to_dict(),
                        "published": True,
                        "draft_cell": "amp__draft_fb",
                        "sch_check": {"errors": 0, "warnings": 0},
                        "cdf_verified": True,
                        "connectivity_mismatches": [],
                    }
                ),
                encoding="utf-8",
            )
        else:
            (run_dir / "response.json").write_text(
                json.dumps(raw_schematic_payload(target)),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = VirtuosoSkillBackend(
        runner=one_shot_runner,
        popen_factory=lambda *a, **k: _DyingProc(),
        daemon_enabled=True,
        project_root=project,
        daemon_startup_timeout_s=1,
    )
    receipt = backend.restore_schematic(
        snap,
        target,
        cds_lib=cds,
        allow_libraries=("sandbox",),
        verify_readback=True,
    )
    assert receipt.published is True
    assert receipt.readback_verified is True
    assert receipt.transport == "one_shot_fallback"
    assert backend.last_daemon_error is not None
    assert fallback_runs == 2  # restore fallback + readback one-shot (no repeated daemon launch)

    # Also test load_backend forwarding v0.2 kwargs cleanly
    loaded = load_backend(
        "virtuoso",
        daemon_enabled=True,
        daemon_idle_timeout_s=90,
        project_root=project,
        crash_retries=2,
        inline_readback=True,
    )
    assert isinstance(loaded, VirtuosoSkillBackend)
    assert loaded.daemon_enabled is True
    assert loaded.daemon_idle_timeout_s == 90
    assert loaded.project_root == project
    assert loaded.crash_retries == 2
    assert loaded.inline_readback is True


def test_virtuoso_remote_crash_retry_and_symbol_single_session_restore(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sym_cv = CellViewRef("work", "amp", "symbol")
    target_sym = CellViewRef("sandbox", "amp", "symbol")
    sym_snap = make_symbol(sym_cv)
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")

    # 1) Remote SSH crash retry (exit 139 with no response.json -> retries once in fresh remote run_id)
    remote_exec_dirs: list[str] = []
    remote_files: dict[str, bytes] = {}
    first_crash = [True]

    def remote_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "/usr/bin/ssh":
            shell = cmd[2]
            if "env ENABLE_SOS705=0" in shell:
                tokens = shlex.split(shell)
                rdir = next(t for t in tokens if t.startswith("IC261_WORKDIR=")).split("=", 1)[1]
                remote_exec_dirs.append(rdir)
                if first_crash[0]:
                    first_crash[0] = False
                    return subprocess.CompletedProcess(
                        cmd, 139, "", "corrupted double-linked list\n"
                    )
                remote_files[f"{rdir}/response.json"] = json.dumps(
                    raw_symbol_payload(sym_cv)
                ).encode("utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "/usr/bin/scp":
            src, dst = cmd[1:3]
            if ":" in src:
                rpath = shlex.split(src.split(":", 1)[1])[0]
                if rpath not in remote_files:
                    return subprocess.CompletedProcess(cmd, 1, "", "No such file")
                Path(dst).write_bytes(remote_files[rpath])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    remote_backend = VirtuosoSkillBackend(
        ssh_host="eda-server",
        remote_workdir="/tmp/oagit_runs",
        runner=remote_runner,
        crash_retries=1,
    )
    captured_sym = remote_backend.capture_symbol(sym_cv, cds_lib="/remote/cds.lib")
    assert captured_sym.cellview == sym_snap.cellview
    assert captured_sym.pins == sym_snap.pins
    assert captured_sym.port_order == sym_snap.port_order
    assert captured_sym.graphics == sym_snap.graphics
    assert captured_sym.properties == sym_snap.properties
    assert len(remote_exec_dirs) == 2
    assert remote_exec_dirs[0] != remote_exec_dirs[1]

    # 2) Single-session inline readback for VirtuosoSkillBackend.restore_symbol (1 runner call!)
    sym_restore_calls = 0

    def sym_inline_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal sym_restore_calls
        sym_restore_calls += 1
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        req_id = req_text.split('request_id "', 1)[1].split('"', 1)[0]
        driver = (run_dir / "driver.il").read_text(encoding="utf-8")
        assert "export_symbol.il" in driver and "restore_symbol.il" in driver and "batch_restore.il" in driver
        resp = {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": req_id,
            "ok": True,
            "results": [
                {
                    "target": target_sym.to_dict(),
                    "ok": True,
                    "published": True,
                    "draft_cell": "amp__sym_draft",
                    "sch_check": {"errors": 0, "warnings": 0},
                    "cdf_verified": True,
                    "connectivity_mismatches": [],
                    "readback": raw_symbol_payload(target_sym),
                    "error": None,
                    "error_code": None,
                }
            ],
            "errors": [],
            "session": {"daemon_id": None, "reused": False},
        }
        (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    sym_backend = VirtuosoSkillBackend(runner=sym_inline_runner, inline_readback=True)
    with pytest.raises(DisallowedLibraryError):
        sym_backend.restore_symbol(sym_snap, target_sym, cds_lib=cds, allow_libraries=())
    assert sym_restore_calls == 0

    sym_receipt = sym_backend.restore_symbol(
        sym_snap, target_sym, cds_lib=cds, allow_libraries=("sandbox",), verify_readback=True
    )
    assert sym_restore_calls == 1
    assert sym_receipt.published is True
    assert sym_receipt.readback_verified is True

    # 3) Plain text CLI smoke check for init --v2, status, daemon status
    text_proj = tmp_path / "text_proj"
    assert main(["init", "--root", str(text_proj), "--v2", "--cds-lib", str(cds), "--backend", "virtuoso"]) == 0
    monkeypatch.chdir(text_proj)
    assert main(["status"]) == 0
    assert main(["daemon", "status"]) == 0
    out_text = capsys.readouterr().out
    assert "Daemon " in out_text


# ---------------------------------------------------------------------------
# 10. Reviewer 2 Regression Tests: Malformed Manifest CLI Status, --hier/--include-symbols
#     --changed False-Skip Prevention, and Single-Cell --recompute-derived
# ---------------------------------------------------------------------------


def test_cli_status_malformed_manifest_never_raises_keyerror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "malformed_proj"
    oa_work = project / "oa" / "work"
    (oa_work / "bad" / "symbol").mkdir(parents=True)
    (oa_work / "bad" / "symbol" / "symbol.oa").write_bytes(b"SYM")

    write_project_config(
        project,
        {"oa_libs": {"work": str(oa_work)}, "backend": "fixture"},
    )
    bad_snap_dir = project / "snapshots" / "work" / "bad" / "symbol"
    bad_snap_dir.mkdir(parents=True)
    (bad_snap_dir / "manifest.json").write_text(
        json.dumps({"schema": "oa-git/2", "view_type": "symbol"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    code, st_out, _ = invoke_json(["status"], capsys)
    assert code == 1
    assert st_out["ok"] is False
    assert st_out["snapshots"][0]["ok"] is False
    assert st_out["snapshots"][0]["error_type"] == "SchemaValidationError"


def test_hier_and_include_symbols_changed_prevent_silent_false_clean_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "hier_skip_proj"
    fixture_dir = tmp_path / "hier_skip_fixtures"
    oa_work = project / "oa" / "work"

    top_sch = CellViewRef("work", "top", "schematic")
    top_sym = CellViewRef("work", "top", "symbol")
    sub_sch = CellViewRef("work", "sub", "schematic")
    sub_sym = CellViewRef("work", "sub", "symbol")

    for cv in (top_sch, top_sym, sub_sch, sub_sym):
        oa_file = oa_work / cv.cell / cv.view / ("sch.oa" if cv.view == "schematic" else "symbol.oa")
        oa_file.parent.mkdir(parents=True, exist_ok=True)
        oa_file.write_bytes(f"OA_{cv.cell}_{cv.view}".encode())

    write_snapshot(
        make_mos_schematic(top_sch, child_master=("work", "sub")),
        fixture_dir / "work" / "top" / "schematic",
    )
    write_symbol_snapshot(make_symbol(top_sym), fixture_dir / "work" / "top" / "symbol")
    write_snapshot(make_mos_schematic(sub_sch), fixture_dir / "work" / "sub" / "schematic")
    write_symbol_snapshot(make_symbol(sub_sym), fixture_dir / "work" / "sub" / "symbol")

    write_project_config(
        project,
        {
            "oa_libs": {"work": str(oa_work)},
            "allow_libraries": ["work"],
            "backend": "fixture",
        },
    )
    monkeypatch.chdir(project)

    # 1) Single-cell capture of top_sch -> hierarchy_complete MUST be False
    code, cap_single, _ = invoke_json(
        ["capture", top_sch.key, "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    idx1 = load_index(project)
    assert idx1[top_sch.key].hierarchy_complete is False

    # 2) Subsequent --hier --changed MUST NOT skip top_sch (it must capture sub_sch!)
    code, cap_hier, _ = invoke_json(
        ["capture", top_sch.key, "--hier", "--changed", "--fixture", str(fixture_dir)],
        capsys,
    )
    assert code == 0
    captured_hier_keys = {
        f"{c['cellview']['library']}/{c['cellview']['cell']}/{c['cellview']['view']}"
        for c in cap_hier["captured"]
    }
    assert {top_sch.key, sub_sch.key} <= captured_hier_keys
    idx2 = load_index(project)
    assert idx2[top_sch.key].hierarchy_complete is True
    assert sub_sch.key in idx2[top_sch.key].dependencies

    # 3) Single-cell capture of unchanged top_sch MUST preserve prior dependencies (not overwrite with ())
    code, _, _ = invoke_json(
        ["capture", top_sch.key, "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    idx3 = load_index(project)
    assert sub_sch.key in idx3[top_sch.key].dependencies

    # 4) --hier --changed with all schematics clean -> skips cleanly with 0 backend calls
    backend_calls: list[Any] = []
    real_backend = cli_mod._backend

    def spy_backend(settings, **kw):
        backend_calls.append(settings)
        return real_backend(settings, **kw)

    monkeypatch.setattr(cli_mod, "_backend", spy_backend)
    code, skip_hier, _ = invoke_json(
        ["capture", top_sch.key, "--hier", "--changed", "--fixture", str(fixture_dir)],
        capsys,
    )
    assert code == 0
    assert skip_hier["transport"] == "index_skip"
    assert len(backend_calls) == 0

    # 5) --include-symbols --changed MUST NOT skip when symbols have not been captured yet!
    code, cap_syms, _ = invoke_json(
        [
            "capture",
            top_sch.key,
            "--hier",
            "--include-symbols",
            "--changed",
            "--fixture",
            str(fixture_dir),
        ],
        capsys,
    )
    assert code == 0
    assert len(backend_calls) == 1
    captured_sym_keys = {
        f"{c['cellview']['library']}/{c['cellview']['cell']}/{c['cellview']['view']}"
        for c in cap_syms["captured"]
    }
    assert {top_sch.key, top_sym.key, sub_sch.key, sub_sym.key} <= captured_sym_keys

    # Once symbols ARE captured & clean, --hier --include-symbols --changed skips with 0 backend calls!
    code, skip_all_syms, _ = invoke_json(
        [
            "capture",
            top_sch.key,
            "--hier",
            "--include-symbols",
            "--changed",
            "--fixture",
            str(fixture_dir),
        ],
        capsys,
    )
    assert code == 0
    assert skip_all_syms["transport"] == "index_skip"
    assert len(backend_calls) == 1

    # 6) Partial child failure in --hier MUST set parent hierarchy_complete=False so next --hier --changed retries!
    def failing_sub_backend(settings, **kw):
        b = real_backend(settings, **kw)
        b.failure_hook = lambda op, ctx: (
            (_ for _ in ()).throw(BridgeExecutionError("sub_boom"))
            if op == "capture" and ctx.get("cellview") == sub_sch
            else None
        )
        return b

    monkeypatch.setattr(cli_mod, "_backend", failing_sub_backend)
    code, partial_out, _ = invoke_json(
        ["capture", top_sch.key, "--hier", "--fixture", str(fixture_dir)], capsys
    )
    assert code == 1
    assert partial_out["ok"] is False
    assert any(f["key"] == sub_sch.key for f in partial_out["failed"])
    idx_after_fail = load_index(project)
    assert idx_after_fail[top_sch.key].hierarchy_complete is False

    # Restore healthy backend -> --hier --changed MUST retry (not false-skip!)
    monkeypatch.setattr(cli_mod, "_backend", spy_backend)
    calls_before = len(backend_calls)
    code, retry_out, _ = invoke_json(
        ["capture", top_sch.key, "--hier", "--changed", "--fixture", str(fixture_dir)],
        capsys,
    )
    assert code == 0
    assert len(backend_calls) == calls_before + 1
    assert load_index(project)[top_sch.key].hierarchy_complete is True


def test_single_cell_capture_honors_recompute_derived_and_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "recomp_proj"
    cv = CellViewRef("work", "amp", "schematic")
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")

    write_project_config(
        project,
        {
            "cds_lib": str(cds),
            "backend": "virtuoso",
            "daemon": {"enabled": False},
        },
    )
    monkeypatch.chdir(project)

    recorded_requests: list[str] = []

    def fake_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(str(kwargs["cwd"]))
        req_text = (run_dir / "request.il").read_text(encoding="utf-8")
        recorded_requests.append(req_text)
        req_id = req_text.split('request_id "', 1)[1].split('"', 1)[0]
        resp = {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": req_id,
            "ok": True,
            "results": [
                {"cellview": cv.to_dict(), "ok": True, "capture": raw_schematic_payload(cv)}
            ],
            "discovered": [],
            "cycles": [],
            "errors": [],
            "session": {"daemon_id": None, "reused": False},
        }
        (run_dir / "response.json").write_text(json.dumps(resp), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        cli_mod,
        "_backend",
        lambda settings, **kw: VirtuosoSkillBackend(runner=fake_runner),
    )

    custom_out = tmp_path / "custom_amp_out"
    code, out, _ = invoke_json(
        ["capture", cv.key, "--recompute-derived", "--out", str(custom_out)],
        capsys,
    )
    assert code == 0
    assert out["ok"] is True
    assert Path(out["snapshot_dir"]) == custom_out.resolve()
    assert (custom_out / "circuit.yaml").is_file()
    assert len(recorded_requests) == 1
    assert 'action "capture_batch"' in recorded_requests[0]
    assert "recompute_derived t" in recorded_requests[0]

