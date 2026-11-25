"""Comprehensive tests for the oa-git v0.2 Symbol subsystem and IC26.1 SKILL scripts."""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from oa_git.bridge import to_skill_literal
from oa_git.model import (
    SCHEMA_VERSION_V2,
    SYMBOL_SCHEMA_V1,
    BridgeExecutionError,
    CellViewRef,
    DisallowedLibraryError,
    FailClosedError,
    LiveLockError,
    OmissionRecord,
    RoundtripMismatchError,
    SchemaValidationError,
    SnapshotIntegrityError,
    UnsupportedObjectError,
)
from oa_git.symbol_diff import diff_symbols, format_symbol_diff_text
from oa_git.symbol_merge import format_symbol_merge_conflicts, merge_symbols
from oa_git.symbol_model import (
    SymbolGraphics,
    SymbolLabel,
    SymbolPin,
    SymbolPinFigure,
    SymbolShape,
    SymbolSnapshot,
)
from oa_git.symbol_snapshot import (
    SymbolViewHandler,
    build_symbol_restore_request_dict,
    build_symbol_snapshot,
    parse_symbol_restore_response,
    raw_symbol_capture_to_snapshot,
    read_symbol_snapshot,
    verify_symbol_roundtrip,
    write_symbol_snapshot,
)
from oa_git.view_contract import ViewHandler, get_view_handler
from oa_git.virtuoso_backend import VirtuosoSkillBackend

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "oa_git" / "skill"
EXPORT_SYMBOL_IL = SKILL_DIR / "export_symbol.il"
RESTORE_SYMBOL_IL = SKILL_DIR / "restore_symbol.il"


# ---------------------------------------------------------------------------
# Realistic Dissected Symbol Factories (IC26.1 TSMC28 & TSMC65 Anatomy)
# ---------------------------------------------------------------------------


def _rect_pin(
    name: str,
    direction: str,
    xy: tuple[float, float],
    orient: str = "R0",
    half: float = 0.025,
) -> SymbolPin:
    x, y = xy
    return SymbolPin(
        name=name,
        direction=direction,
        xy=xy,
        orient=orient,
        pin_figure=SymbolPinFigure(
            kind="rect",
            layer="pin",
            purpose="drawing",
            bbox=((x - half, y - half), (x + half, y + half)),
        ),
    )


def _make_sar_adc_symbol() -> SymbolSnapshot:
    """Model adc_lib/SAR_ADC: bus pins DOUTP<9:0> + NLPLabel [@instanceName]/[@partName]."""

    cv = CellViewRef("adc_lib", "SAR_ADC", "symbol")
    pins = (
        _rect_pin("VINP", "input", (0.0, 0.0)),
        _rect_pin("VINN", "input", (0.0, -0.25)),
        _rect_pin("CLK", "input", (0.0, -0.5)),
        _rect_pin("VDD", "input", (1.25, 0.2)),
        _rect_pin("VSS", "input", (1.25, -1.1)),
        _rect_pin("VCM", "input", (0.0, -0.75)),
        _rect_pin("DOUTP<9:0>", "output", (2.75, 0.0)),
        _rect_pin("DOUTN<9:0>", "output", (2.75, -0.25)),
        _rect_pin("EOC", "output", (2.75, -0.5)),
        _rect_pin("RDY", "output", (2.75, -0.75)),
    )
    graphics = SymbolGraphics(
        shapes=(
            SymbolShape(
                kind="rectangle",  # verifies alias normalization -> 'rect'
                layer="device",
                purpose="drawing",
                bbox=((0.25, -0.95), (2.5, 0.1)),
            ),
            SymbolShape(
                kind="line",
                layer="device",
                purpose="drawing",
                points=((0.0, 0.0), (0.25, 0.0)),
            ),
            SymbolShape(
                kind="line",
                layer="device",
                purpose="drawing",
                points=((2.5, 0.0), (2.75, 0.0)),
            ),
        ),
        labels=(
            SymbolLabel(
                text="DOUTP<9:0>",
                xy=(2.45, 0.0),
                layer="pin",
                purpose="label",
                justify="centerRight",
            ),
            SymbolLabel(
                text="VINP",
                xy=(0.3, 0.0),
                layer="pin",
                purpose="label",
                justify="centerLeft",
            ),
        ),
        nlp_labels=(
            SymbolLabel(
                text="[@instanceName]",
                xy=(1.375, 0.15),
                layer="instance",
                purpose="label",
                label_type="NLPLabel",
            ),
            SymbolLabel(
                text="[@partName]",
                xy=(1.375, -0.45),
                layer="device",
                purpose="label",
                label_type="NLPLabel",
            ),
        ),
        selection_box=((-0.025, -1.125), (2.8, 0.225)),
        origin=(0.0, 0.0),
        bbox=((-0.025, -1.125), (2.8, 0.225)),
    )
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("VINP", "VINN", "CLK", "VCM", "VDD", "VSS", "DOUTP<9:0>", "DOUTN<9:0>", "EOC", "RDY"),
        graphics=graphics,
        properties={"partName": "SAR_ADC", "vendorName": "demo_vendor"},
        metadata={"cellview_type": "schematicSymbol", "dbu_per_uu": 160.0},
    )


def _make_top_redun2_adc_symbol() -> SymbolSnapshot:
    """Model redundancy/TOP_redun2_ADC: ILLabel cdsName()/cdsParam(1..3)."""

    cv = CellViewRef("redundancy", "TOP_redun2_ADC", "symbol")
    pins = (
        _rect_pin("VINP", "input", (0.0, 0.25)),
        _rect_pin("VDD", "inputOutput", (1.2, 0.55)),
        _rect_pin("DOUTP<9:0>", "output", (2.4, 0.25)),
    )
    graphics = SymbolGraphics(
        shapes=(
            SymbolShape(kind="rect", layer="device", purpose="drawing", bbox=((0.2, -0.6), (2.2, 0.4))),
        ),
        labels=(
            SymbolLabel(text="DOUTP<9:0>", xy=(2.15, 0.25), layer="pin", purpose="label"),
        ),
        il_labels=(
            SymbolLabel(
                text="cdsName()",
                xy=(1.2, 0.5),
                layer="annotate",
                purpose="drawing7",
                justify="lowerLeft",
                label_type="ILLabel",
            ),
            SymbolLabel(
                text="cdsParam(1)",
                xy=(1.2, -0.65),
                layer="annotate",
                purpose="drawing",
                justify="lowerLeft",
                label_type="ILLabel",
            ),
            SymbolLabel(
                text="cdsParam(2)",
                xy=(1.2, -0.75),
                layer="annotate",
                purpose="drawing",
                justify="lowerLeft",
                label_type="ILLabel",
            ),
            SymbolLabel(
                text="cdsParam(3)",
                xy=(1.2, -0.85),
                layer="annotate",
                purpose="drawing",
                justify="lowerLeft",
                label_type="ILLabel",
            ),
        ),
        selection_box=((-0.025, -0.8125), (2.45, 0.5875)),
        origin=(0.0, 0.0),
    )
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("VINP", "VDD", "DOUTP<9:0>"),
        graphics=graphics,
    )


def _make_folded_cascode_symbol() -> SymbolSnapshot:
    """Model analog_opamp_lib/folded_cascode_1p8V: path|device:drawing1 + cdsTerm()."""

    cv = CellViewRef("analog_opamp_lib", "folded_cascode_1p8V", "symbol")
    pins = (
        _rect_pin("VINP", "input", (-0.5, 0.25)),
        _rect_pin("VINN", "input", (-0.5, -0.25)),
        _rect_pin("VOUT", "output", (2.4, 0.0)),
        _rect_pin("VDD", "inputOutput", (0.95, 0.4)),
        _rect_pin("VSS", "inputOutput", (0.95, -0.55)),
    )
    graphics = SymbolGraphics(
        shapes=(
            SymbolShape(
                kind="path",
                layer="device",
                purpose="drawing1",
                points=((0.0, -0.5), (0.0, 0.5), (2.0, 0.0), (0.0, -0.5)),
                width=0.025,
            ),
            SymbolShape(
                kind="path",
                layer="device",
                purpose="drawing1",
                points=((-0.5, 0.25), (0.0, 0.25)),
                width=0.0125,
            ),
        ),
        labels=(
            SymbolLabel(text="VINP", xy=(-0.35, 0.25), layer="pin", purpose="label"),
            SymbolLabel(text="VOUT", xy=(2.2, 0.0), layer="pin", purpose="label"),
        ),
        il_labels=(
            SymbolLabel(
                text='cdsTerm("VINP")',
                xy=(-0.45, 0.3),
                layer="annotate",
                purpose="drawing8",
                label_type="ILLabel",
            ),
            SymbolLabel(
                text='cdsTerm("VOUT")',
                xy=(2.3, 0.05),
                layer="annotate",
                purpose="drawing8",
                label_type="ILLabel",
            ),
            SymbolLabel(
                text="cdsName()",
                xy=(0.95, 0.45),
                layer="annotate",
                purpose="drawing7",
                label_type="ILLabel",
            ),
        ),
        selection_box=((-0.59375, -0.59375), (2.43125, 0.40625)),
        origin=(0.0, 0.0),
    )
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("VINP", "VINN", "VOUT", "VDD", "VSS"),
        graphics=graphics,
    )


def _make_lc_vco_symbol() -> SymbolSnapshot:
    """Model demo_pll_lib/LC_vco: arc + ellipse + off-origin selection_box + bus S<2:0> + portOrder."""

    cv = CellViewRef("demo_pll_lib", "LC_vco", "symbol")
    pins = (
        _rect_pin("VDD", "inputOutput", (2.25, 0.4375)),
        _rect_pin("VSS", "inputOutput", (2.25, -1.4375)),
        _rect_pin("OUTP", "inputOutput", (3.3375, -0.25)),
        _rect_pin("OUTN", "inputOutput", (3.3375, -0.75)),
        _rect_pin("VCTRL", "inputOutput", (1.1875, -0.25)),
        _rect_pin("IBIAS", "inputOutput", (1.1875, -0.75)),
        _rect_pin("S<2:0>", "inputOutput", (1.1875, -1.125)),
    )
    graphics = SymbolGraphics(
        shapes=(
            SymbolShape(
                kind="arc",
                layer="device",
                purpose="drawing",
                # Deliberately non-sorted arc_bbox (start > stop in x) to verify arc order preservation!
                arc_bbox=((2.5, -0.2), (2.1, -0.6)),
                ellipse_bbox=((2.0, -0.7), (2.6, -0.1)),
            ),
            SymbolShape(
                kind="ellipse",
                layer="device",
                purpose="drawing",
                bbox=((1.5, -1.0), (3.0, 0.0)),
            ),
            SymbolShape(
                kind="line",
                layer="device",
                purpose="drawing",
                points=((1.1875, -1.125), (1.5, -1.125)),
            ),
        ),
        labels=(
            SymbolLabel(
                text="S<2:0>",
                xy=(1.3, -1.125),
                layer="pin",
                purpose="label",
                orient="R0",
            ),
            SymbolLabel(
                text="OUTP",
                xy=(3.2, -0.25),
                layer="pin",
                purpose="label",
                orient="R90",
            ),
        ),
        nlp_labels=(
            SymbolLabel(
                text="[@instanceName]",
                xy=(2.25, 0.25),
                layer="instance",
                purpose="label",
                label_type="NLPLabel",
            ),
            SymbolLabel(
                text="VCO",
                xy=(2.25, -0.5),
                layer="device",
                purpose="label",
                label_type="NLPLabel",
            ),
        ),
        selection_box=((1.1625, -1.4625), (3.3625, 0.4625)),
        origin=(0.0, 0.0),
        bbox=((1.1625, -1.4625), (3.3625, 0.4625)),
    )
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("VDD", "VSS", "VCTRL", "IBIAS", "S<2:0>", "OUTP", "OUTN"),
        graphics=graphics,
        properties={"vendorName": "TSMC", "partName": "LC_vco"},
        metadata={"cellview_type": "schematicSymbol", "dbu_per_uu": 160.0},
    )


# ---------------------------------------------------------------------------
# 1. Real Dissected Symbols, Canonicalization & Determinism
# ---------------------------------------------------------------------------


def test_real_dissected_symbols_write_read_roundtrip(tmp_path: Path) -> None:
    for snap in (
        _make_sar_adc_symbol(),
        _make_top_redun2_adc_symbol(),
        _make_folded_cascode_symbol(),
        _make_lc_vco_symbol(),
    ):
        dest = tmp_path / snap.cellview.library / snap.cellview.cell / snap.cellview.view
        written = write_symbol_snapshot(snap, dest)
        assert (written / "symbol.yaml").is_file()
        assert (written / "manifest.json").is_file()
        assert not (written / "omissions.json").exists()

        loaded = read_symbol_snapshot(written, verify=True)
        assert loaded == snap
        assert loaded.manifest.schema == SCHEMA_VERSION_V2
        assert loaded.manifest.view_type == "symbol"
        assert loaded.manifest.electrical_sha256 is None
        assert loaded.manifest.placement_sha256 is None
        assert loaded.manifest.content_sha256 is not None

        # Verify arc_bbox start/stop point ordering preserved on LC_vco
        if snap.cellview.cell == "LC_vco":
            arc_shapes = [s for s in loaded.graphics.shapes if s.kind == "arc"]
            assert len(arc_shapes) == 1
            assert arc_shapes[0].arc_bbox == ((2.5, -0.2), (2.1, -0.6))
            assert arc_shapes[0].ellipse_bbox == ((2.0, -0.7), (2.6, -0.1))

        # Verify rectangle normalized canonically to rect on SAR_ADC
        if snap.cellview.cell == "SAR_ADC":
            assert any(s.kind == "rect" for s in loaded.graphics.shapes)
            assert all(s.kind != "rectangle" for s in loaded.graphics.shapes)


def test_symbol_determinism_under_reordering_and_coord_noise(tmp_path: Path) -> None:
    cv = CellViewRef("work", "amp", "symbol")
    pin_a = SymbolPin(
        name="IN<1:0>",
        direction="input",
        xy=(-0.0, 0.1234561),
        orient="R0",
        pin_figure=SymbolPinFigure(
            kind="rect",
            layer="pin",
            purpose="drawing",
            bbox=((0.025, 0.1484561), (-0.025, 0.0984561)),
        ),
    )
    pin_b = SymbolPin(
        name="OUT",
        direction="output",
        xy=(1.0, 0.0),
        orient="R0",
        pin_figure=SymbolPinFigure(
            kind="inst", lib="basic", cell="opin", view="symbol"
        ),
    )
    tri_1 = SymbolShape(
        kind="polygon",
        layer="device",
        purpose="drawing",
        points=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
    )
    tri_2 = SymbolShape(
        kind="polygon",
        layer="device",
        purpose="drawing",
        # Rotated + reversed + closed ring equivalent of tri_1
        points=((0.0, 1.0), (1.0, 0.0), (0.0, 0.0), (0.0, 1.0)),
    )
    line_1 = SymbolShape(
        kind="line", layer="device", purpose="drawing", points=((0.0, 0.0), (1.0, 0.0))
    )
    line_2 = SymbolShape(
        kind="line", layer="device", purpose="drawing", points=((1.0, 0.0), (-0.0, -0.0))
    )
    lbl_1 = SymbolLabel(text="A", xy=(0.1, 0.2), layer="pin", purpose="label")
    lbl_2 = SymbolLabel(text="B", xy=(0.3, 0.4), layer="pin", purpose="label")

    snap_a = build_symbol_snapshot(
        cv,
        pins=(pin_b, pin_a),
        port_order=("OUT", "IN<1:0>"),
        graphics=SymbolGraphics(
            shapes=(tri_1, line_1),
            labels=(lbl_2, lbl_1),
            selection_box=((1.0, 1.0), (-0.0, -0.0)),
            origin=(-0.0, -0.0),
        ),
        properties={"z": 1, "a": -0.0},
    )
    snap_b = build_symbol_snapshot(
        cv,
        pins=(pin_a, pin_b),
        port_order=("OUT", "IN<1:0>"),
        graphics=SymbolGraphics(
            shapes=(line_2, tri_2),
            labels=(lbl_1, lbl_2),
            selection_box=((0.0, 0.0), (1.0, 1.0)),
            origin=(0.0, 0.0),
        ),
        properties={"a": 0.0, "z": 1},
    )

    dir_a = write_symbol_snapshot(snap_a, tmp_path / "a")
    dir_b = write_symbol_snapshot(snap_b, tmp_path / "b")
    assert (dir_a / "symbol.yaml").read_bytes() == (dir_b / "symbol.yaml").read_bytes()
    assert (dir_a / "manifest.json").read_bytes() == (dir_b / "manifest.json").read_bytes()
    assert snap_a.manifest.content_sha256 == snap_b.manifest.content_sha256
    assert snap_a.manifest.full_sha256 == snap_b.manifest.full_sha256
    # Declared port_order sequence must NOT be alphabetically sorted!
    assert snap_a.port_order == ("OUT", "IN<1:0>")


def test_strict_validation_rejects_bad_inputs() -> None:
    cv = CellViewRef("work", "bad", "symbol")

    # NaN / Inf coordinate
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(cv, pins=(_rect_pin("A", "input", (math.nan, 0.0)),))
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(
            cv, graphics=SymbolGraphics(origin=(math.inf, 0.0))
        )

    # Duplicate pin names
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(
            cv, pins=(_rect_pin("IN", "input", (0.0, 0.0)), _rect_pin("IN", "output", (1.0, 0.0)))
        )

    # Invalid direction & orient
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(cv, pins=(_rect_pin("IN", "bogus", (0.0, 0.0)),))
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(cv, pins=(_rect_pin("IN", "input", (0.0, 0.0), orient="R45"),))

    # Duplicate port_order entry
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(cv, port_order=("A", "B", "A"))

    # Selection box rect must not appear in graphics.shapes
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(
            cv,
            graphics=SymbolGraphics(
                shapes=(
                    SymbolShape(
                        kind="rect",
                        layer="instance",
                        purpose="drawing",
                        bbox=((0.0, 0.0), (1.0, 1.0)),
                    ),
                )
            ),
        )

    # Label type mismatch (ILLabel in normal labels bucket)
    with pytest.raises(SchemaValidationError):
        build_symbol_snapshot(
            cv,
            graphics=SymbolGraphics(
                labels=(SymbolLabel(text="cdsName()", xy=(0.0, 0.0), label_type="ILLabel"),)
            ),
        )

    # Invalid non-scalar / nested property values (flat scalar ILList is valid, nested/dict/None rejected)
    for bad_val in (
        {"nested": "dict"},
        [["nested", "list"]],
        ["ok", None],
        [math.nan],
        None,
    ):
        with pytest.raises(SchemaValidationError):
            build_symbol_snapshot(cv, properties={"bad": bad_val})


# ---------------------------------------------------------------------------
# 2. Manifest v2 Tamper Detection, Omissions Lifecycle & ViewHandler Registry
# ---------------------------------------------------------------------------


def test_manifest_v2_tamper_detection_and_omissions_cleanup(tmp_path: Path) -> None:
    snap_with_omission = build_symbol_snapshot(
        CellViewRef("work", "sym", "symbol"),
        pins=(_rect_pin("IN", "input", (0.0, 0.0)),),
        omissions=(
            OmissionRecord(
                kind="shape",
                identifier="donut",
                reason="unsupported_shape",
                details={"layer": "device"},
            ),
        ),
    )
    dest = write_symbol_snapshot(snap_with_omission, tmp_path / "sym")
    assert (dest / "omissions.json").is_file()
    loaded = read_symbol_snapshot(dest, verify=True)
    assert len(loaded.omissions) == 1

    # Tamper symbol.yaml -> SnapshotIntegrityError
    orig_yaml = (dest / "symbol.yaml").read_bytes()
    (dest / "symbol.yaml").write_bytes(orig_yaml + b"# tampered\n")
    with pytest.raises(SnapshotIntegrityError):
        read_symbol_snapshot(dest, verify=True)
    (dest / "symbol.yaml").write_bytes(orig_yaml)

    # Tamper omissions.json -> SnapshotIntegrityError
    orig_om = (dest / "omissions.json").read_bytes()
    (dest / "omissions.json").write_bytes(b"[]\n")
    with pytest.raises(SnapshotIntegrityError):
        read_symbol_snapshot(dest, verify=True)
    (dest / "omissions.json").write_bytes(orig_om)

    # Overwriting with clean snapshot removes stale omissions.json atomically
    clean_snap = build_symbol_snapshot(
        CellViewRef("work", "sym", "symbol"),
        pins=(_rect_pin("IN", "input", (0.0, 0.0)),),
    )
    write_symbol_snapshot(clean_snap, dest)
    assert not (dest / "omissions.json").exists()
    assert read_symbol_snapshot(dest, verify=True) == clean_snap

    # Stale omissions.json fails closed
    (dest / "omissions.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError):
        read_symbol_snapshot(dest, verify=True)
    (dest / "omissions.json").unlink()

    # Symlinked destination fails closed
    link_dest = tmp_path / "link_sym"
    link_dest.symlink_to(dest)
    with pytest.raises(SchemaValidationError):
        write_symbol_snapshot(clean_snap, link_dest)
    with pytest.raises(SnapshotIntegrityError):
        read_symbol_snapshot(link_dest, verify=True)


def test_symbol_view_handler_registration_and_contract(tmp_path: Path) -> None:
    handler = get_view_handler("symbol")
    assert isinstance(handler, SymbolViewHandler)
    assert isinstance(handler, ViewHandler)
    assert handler.view_type == "symbol"
    assert handler.capture_action == "capture_symbol"
    assert handler.restore_action == "restore_symbol"
    assert handler.source_file_candidates("LC_vco", "symbol") == (
        "LC_vco/symbol/symbol.oa",
        "LC_vco/symbol/master.tag",
    )

    s1 = _make_lc_vco_symbol()
    out_dir = handler.write_snapshot(s1, tmp_path / "lc")
    loaded = handler.read_snapshot(out_dir, verify=True)
    assert loaded == s1
    assert handler.diff(s1, loaded).is_empty
    merged = handler.merge(s1, s1, loaded)
    assert merged.ok and merged.snapshot == s1


# ---------------------------------------------------------------------------
# 3. Wire Helpers, SkillRequestRunner Seam & Roundtrip Verification
# ---------------------------------------------------------------------------


def test_wire_capture_restore_and_virtuoso_seam(tmp_path: Path) -> None:
    raw_capture = {
        "ok": True,
        "error": None,
        "cellview": {"library": "demo_pll_lib", "cell": "LC_vco", "view": "symbol"},
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematicSymbol", "dbu_per_uu": 160.0},
        "pins": [
            {
                "name": "S<2:0>",
                "direction": "inputOutput",
                "xy": [1.1875, -1.125],
                "orient": "R0",
                "pin_figure": {
                    "kind": "rect",
                    "layer": "pin",
                    "purpose": "drawing",
                    "bbox": [[1.1625, -1.15], [1.2125, -1.1]],
                },
            }
        ],
        "port_order": ["S<2:0>"],
        "graphics": {
            "shapes": [
                {
                    "kind": "arc",
                    "layer": "device",
                    "purpose": "drawing",
                    "arc_bbox": [[2.5, -0.2], [2.1, -0.6]],
                    "ellipse_bbox": [[2.0, -0.7], [2.6, -0.1]],
                    "fill": False,
                }
            ],
            "labels": [
                {
                    "text": "S<2:0>",
                    "xy": [1.3, -1.125],
                    "layer": "pin",
                    "purpose": "label",
                    "justify": "centerLeft",
                    "orient": "R0",
                    "font": "stick",
                    "height": 0.0625,
                    "label_type": "normalLabel",
                }
            ],
            "nlp_labels": [
                {
                    "text": "[@instanceName]",
                    "xy": [2.25, 0.25],
                    "layer": "instance",
                    "purpose": "label",
                    "justify": "centerCenter",
                    "orient": "R0",
                    "font": "stick",
                    "height": 0.0625,
                    "label_type": "NLPLabel",
                }
            ],
            "il_labels": [],
            "selection_box": [[1.1625, -1.4625], [3.3625, 0.4625]],
            "origin": [0.0, 0.0],
            "bbox": [[1.1625, -1.4625], [3.3625, 0.4625]],
        },
        "properties": {"vendorName": "TSMC", "flag": True},
        "omissions": [],
    }
    snap = raw_symbol_capture_to_snapshot(raw_capture)
    assert snap.pins[0].name == "S<2:0>"
    assert snap.properties == {"flag": True, "vendorName": "TSMC"}

    # Capture error paths
    with pytest.raises(BridgeExecutionError):
        raw_symbol_capture_to_snapshot({"ok": False, "error": "open_failed"})
    with pytest.raises(FailClosedError):
        raw_symbol_capture_to_snapshot({**raw_capture, "modified_but_not_saved": True})

    # Restore request dict must serialize cleanly via to_skill_literal & run_skill_request
    target = CellViewRef("SANDBOX", "LC_vco_copy", "symbol")
    req_dict = build_symbol_restore_request_dict(
        snap,
        target,
        draft_cell="LC_vco_copy__oagit_draft_1",
        overwrite=True,
        allow_libraries=("SANDBOX",),
    )
    rendered_skill = "setq(oagitRequest '" + to_skill_literal(req_dict) + ")\n"
    _balanced_skill_parentheses(rendered_skill)
    assert req_dict["action"] == "restore_symbol"
    assert req_dict["properties"] == [
        ["flag", True, "boolean"],
        ["vendorName", "TSMC", "string"],
    ]

    # Exercise VirtuosoSkillBackend.run_skill_request seam with fake runner
    captured_req_text: list[str] = []

    def fake_runner(
        cmd: list[str], *, env: dict[str, str], timeout: int, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        run_dir = Path(env["IC261_WORKDIR"])
        captured_req_text.append((run_dir / "request.il").read_text(encoding="utf-8"))
        (run_dir / "response.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "error": None,
                    "error_code": None,
                    "target": target.to_dict(),
                    "published": True,
                    "draft_cell": "LC_vco_copy__oagit_draft_1",
                    "pin_contract_verified": True,
                    "readback_verified": False,
                    "pin_mismatches": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = VirtuosoSkillBackend(runner=fake_runner, skill_dir=SKILL_DIR)
    resp = backend.run_skill_request(
        "restore_symbol", req_dict, cds_lib=tmp_path / "cds.lib"
    )
    _balanced_skill_parentheses(captured_req_text[0])
    receipt = parse_symbol_restore_response(resp, target)
    assert receipt.published is True
    assert receipt.details["pin_contract_verified"] is True

    # Error code mappings in parse_symbol_restore_response
    for code, exc_cls in (
        ("disallowed_library", DisallowedLibraryError),
        ("live_lock", LiveLockError),
        ("unsupported_object", UnsupportedObjectError),
        ("pin_contract_mismatch", RoundtripMismatchError),
        ("symbol_check_failed", RoundtripMismatchError),
        ("unknown_code", BridgeExecutionError),
    ):
        with pytest.raises(exc_cls):
            parse_symbol_restore_response(
                {"ok": False, "error_code": code, "error": "boom"}, target
            )

    with pytest.raises(RoundtripMismatchError):
        parse_symbol_restore_response(
            {
                "ok": True,
                "target": {"library": "OTHER", "cell": "X", "view": "symbol"},
                "published": True,
            },
            target,
        )


def test_verify_symbol_roundtrip_tolerance_and_mismatch_detection() -> None:
    expected = _make_folded_cascode_symbol()
    # Small float jitter within coord_tol=1e-4 passes
    jittered_pins = tuple(
        replace(
            p,
            xy=(p.xy[0] + 5e-5, p.xy[1] - 5e-5),
            pin_figure=replace(
                p.pin_figure,
                bbox=(
                    (p.pin_figure.bbox[0][0] + 5e-5, p.pin_figure.bbox[0][1] - 5e-5),
                    (p.pin_figure.bbox[1][0] + 5e-5, p.pin_figure.bbox[1][1] - 5e-5),
                )
                if p.pin_figure.bbox
                else None,
            ),
        )
        for p in expected.pins
    )
    actual_ok = build_symbol_snapshot(
        expected.cellview,
        pins=jittered_pins,
        port_order=expected.port_order,
        graphics=expected.graphics,
        properties=expected.properties,
    )
    report_ok = verify_symbol_roundtrip(expected, actual_ok, coord_tol=1e-4)
    assert report_ok.ok is True

    # Break direction + pin xy + missing ILLabel
    broken_pins = list(expected.pins)
    broken_pins[0] = replace(broken_pins[0], direction="output", xy=(9.9, 9.9))
    actual_bad = build_symbol_snapshot(
        expected.cellview,
        pins=tuple(broken_pins),
        port_order=expected.port_order,
        graphics=replace(expected.graphics, il_labels=()),
        properties=expected.properties,
    )
    report_bad = verify_symbol_roundtrip(
        expected, actual_bad, coord_tol=1e-4, raise_on_mismatch=False
    )
    assert report_bad.ok is False
    assert report_bad.pin_contract_ok is False
    assert report_bad.pin_geometry_ok is False
    assert report_bad.graphics_ok is False
    with pytest.raises(RoundtripMismatchError):
        verify_symbol_roundtrip(expected, actual_bad, coord_tol=1e-4, raise_on_mismatch=True)


# ---------------------------------------------------------------------------
# 4. Three-Domain Semantic Diff
# ---------------------------------------------------------------------------


def test_symbol_diff_three_domains_and_text_formatting() -> None:
    base = _make_sar_adc_symbol()

    # 1) Graphics-only change (add a body line & change selection_box)
    gfx_only = build_symbol_snapshot(
        base.cellview,
        pins=base.pins,
        port_order=base.port_order,
        graphics=replace(
            base.graphics,
            shapes=base.graphics.shapes
            + (
                SymbolShape(
                    kind="line",
                    layer="device",
                    purpose="drawing",
                    points=((0.5, 0.0), (1.0, 0.0)),
                ),
            ),
            selection_box=((-0.1, -1.2), (2.9, 0.3)),
        ),
        properties=base.properties,
    )
    d_gfx = diff_symbols(base, gfx_only)
    assert d_gfx.is_graphics_only is True
    assert d_gfx.has_pin_contract_changes is False
    assert d_gfx.has_pin_geometry_changes is False
    assert d_gfx.has_graphics_changes is True
    assert "No pin contract changes." in format_symbol_diff_text(d_gfx, contract_only=True)
    assert "Graphics Changes:" in format_symbol_diff_text(d_gfx, contract_only=False)

    # 2) Pin geometry-only move (move VINP xy & orient)
    moved_pins = tuple(
        replace(p, xy=(0.0, 0.125), orient="R90") if p.name == "VINP" else p
        for p in base.pins
    )
    geom_only = build_symbol_snapshot(
        base.cellview,
        pins=moved_pins,
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    d_geom = diff_symbols(base, geom_only)
    assert d_geom.has_pin_contract_changes is False
    assert d_geom.has_pin_geometry_changes is True
    assert d_geom.has_graphics_changes is False
    assert len(d_geom.pin_geometry_changes.moves) == 1
    assert d_geom.pin_geometry_changes.moves[0].name == "VINP"

    # 3) Pin contract change: rename RDY -> DONE (strictly remove + add), change EOC dir, reorder port_order, edit property
    contract_pins = tuple(
        _rect_pin("DONE", "output", (2.75, -0.75))
        if p.name == "RDY"
        else (replace(p, direction="inputOutput") if p.name == "EOC" else p)
        for p in base.pins
    )
    contract_mod = build_symbol_snapshot(
        base.cellview,
        pins=contract_pins,
        port_order=tuple(reversed(base.port_order)),
        graphics=base.graphics,
        properties={**base.properties, "vendorName": "TSMC28"},
    )
    d_con = diff_symbols(base, contract_mod)
    assert d_con.has_pin_contract_changes is True
    changes_by_name = {c.name: c for c in d_con.pin_contract_changes.pins}
    assert changes_by_name["RDY"].change_type == "removed"
    assert changes_by_name["DONE"].change_type == "added"
    assert changes_by_name["EOC"].change_type == "modified"
    assert d_con.pin_contract_changes.port_order_change is not None
    assert len(d_con.pin_contract_changes.property_changes) == 1
    text = format_symbol_diff_text(d_con, contract_only=True)
    assert "+ pin DONE" in text
    assert "- pin RDY" in text
    assert "~ pin EOC" in text
    assert "~ port_order:" in text


# ---------------------------------------------------------------------------
# 5. Three-Way Merge (Clean + Structured Conflicts)
# ---------------------------------------------------------------------------


def test_symbol_merge_clean_independent_edits() -> None:
    base = _make_top_redun2_adc_symbol()

    # Ours: change VINP direction + add vendor property + add a line shape (expanding bbox right)
    ours_pins = tuple(
        replace(p, direction="inputOutput") if p.name == "VINP" else p
        for p in base.pins
    )
    ours = build_symbol_snapshot(
        base.cellview,
        pins=ours_pins,
        port_order=base.port_order,
        graphics=replace(
            base.graphics,
            shapes=base.graphics.shapes
            + (
                SymbolShape(
                    kind="line",
                    layer="device",
                    purpose="drawing",
                    points=((2.2, 0.0), (2.6, 0.0)),
                ),
            ),
            bbox=((-0.025, -0.8125), (2.6, 0.5875)),
        ),
        properties={"vendorName": "TSMC"},
    )

    # Theirs: move VINP xy + add new pin RST + add ILLabel + add partName property (expanding bbox left)
    theirs_pins = tuple(
        replace(p, xy=(0.0, 0.375)) if p.name == "VINP" else p for p in base.pins
    ) + (_rect_pin("RST", "input", (0.0, -0.25)),)
    theirs = build_symbol_snapshot(
        base.cellview,
        pins=theirs_pins,
        port_order=base.port_order + ("RST",),
        graphics=replace(
            base.graphics,
            il_labels=base.graphics.il_labels
            + (
                SymbolLabel(
                    text='cdsTerm("RST")',
                    xy=(0.1, -0.25),
                    layer="annotate",
                    purpose="drawing8",
                    label_type="ILLabel",
                ),
            ),
            bbox=((-0.2, -0.8125), (2.45, 0.5875)),
        ),
        properties={"partName": "TOP_redun2_ADC"},
    )

    res = merge_symbols(base, ours, theirs)
    assert res.ok is True
    assert res.conflicts == ()
    assert res.snapshot is not None
    pins_map = {p.name: p for p in res.snapshot.pins}
    assert set(pins_map) == {"VINP", "VDD", "DOUTP<9:0>", "RST"}
    assert pins_map["VINP"].direction == "inputOutput"
    assert pins_map["VINP"].xy == (0.0, 0.375)
    assert res.snapshot.port_order == ("VINP", "VDD", "DOUTP<9:0>", "RST")
    assert res.snapshot.properties == {"partName": "TOP_redun2_ADC", "vendorName": "TSMC"}
    assert len(res.snapshot.graphics.shapes) == 2
    assert len(res.snapshot.graphics.il_labels) == 5
    # Derived bbox merged to union without spurious conflict
    assert res.snapshot.graphics.bbox == ((-0.2, -0.8125), (2.6, 0.5875))


def test_symbol_merge_conflicts_produce_no_partial_snapshot() -> None:
    base = _make_lc_vco_symbol()

    # Conflict 1: concurrent direction edit + concurrent xy move + concurrent port_order + concurrent selection_box
    ours = build_symbol_snapshot(
        base.cellview,
        pins=tuple(
            replace(p, direction="input", xy=(1.0, -1.125)) if p.name == "S<2:0>" else p
            for p in base.pins
        ),
        port_order=("S<2:0>", "VDD", "VSS", "VCTRL", "IBIAS", "OUTP", "OUTN"),
        graphics=replace(base.graphics, selection_box=((1.0, -1.5), (3.4, 0.5))),
        properties=base.properties,
    )
    theirs = build_symbol_snapshot(
        base.cellview,
        pins=tuple(
            replace(p, direction="output", xy=(1.3, -1.125)) if p.name == "S<2:0>" else p
            for p in base.pins
        ),
        port_order=("OUTP", "OUTN", "VDD", "VSS", "VCTRL", "IBIAS", "S<2:0>"),
        graphics=replace(base.graphics, selection_box=((0.9, -1.6), (3.5, 0.6))),
        properties=base.properties,
    )

    res = merge_symbols(base, ours, theirs)
    assert res.ok is False
    assert res.snapshot is None
    paths = {(c.domain, c.path, c.reason) for c in res.conflicts}
    assert ("pin_contract", "pins.S<2:0>.direction", "modify/modify") in paths
    assert ("pin_geometry", "pins.S<2:0>.xy", "modify/modify") in paths
    assert ("pin_contract", "port_order", "modify/modify") in paths
    assert ("graphics", "graphics.selection_box", "modify/modify") in paths
    formatted = format_symbol_merge_conflicts(res)
    assert "pins.S<2:0>.direction" in formatted

    # Delete/modify conflict on a pin
    ours_del = build_symbol_snapshot(
        base.cellview,
        pins=tuple(p for p in base.pins if p.name != "IBIAS"),
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    theirs_mod = build_symbol_snapshot(
        base.cellview,
        pins=tuple(
            replace(p, direction="input") if p.name == "IBIAS" else p for p in base.pins
        ),
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    res_dm = merge_symbols(base, ours_del, theirs_mod)
    assert res_dm.ok is False
    assert res_dm.snapshot is None
    assert any(
        c.path == "pins.IBIAS" and c.reason == "delete/modify"
        for c in res_dm.conflicts
    )

    # Mismatched cellviews fail closed
    other = _make_sar_adc_symbol()
    with pytest.raises(SchemaValidationError):
        merge_symbols(base, ours, other)


# ---------------------------------------------------------------------------
# 6. Strict SKILL Lexer Parenthesis-Balance, Prefix, Forbidden-API & Protocol Checks
# ---------------------------------------------------------------------------


def _balanced_skill_parentheses(source: str) -> None:
    """Strict SKILL lexer checking parenthesis balance across strings and comments."""

    depth = 0
    state = "normal"
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if state == "string":
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                state = "normal"
            index += 1
            continue

        if state == "line_comment":
            if char in "\r\n":
                state = "normal"
            index += 1
            continue

        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "normal"
                index += 2
            else:
                index += 1
            continue

        if char == '"':
            state = "string"
        elif char == ";":
            state = "line_comment"
        elif char == "/" and next_char == "*":
            state = "block_comment"
            index += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise AssertionError(f"unmatched closing parenthesis at offset {index}")
        index += 1

    assert state == "normal", f"unterminated SKILL {state}"
    assert depth == 0, f"unbalanced SKILL parentheses, depth={depth}"


@pytest.mark.parametrize(
    "path", [EXPORT_SYMBOL_IL, RESTORE_SYMBOL_IL], ids=lambda p: p.name
)
def test_symbol_skill_scripts_are_balanced_and_clean(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    _balanced_skill_parentheses(source)

    for forbidden in (
        "schHi",
        "cdfGetParam",
        "cdfGetParameterList",
        "dbReplaceProp",
        "stringToChar",
        "cond((nil",
    ):
        assert forbidden not in source, f"{path.name} contains forbidden pattern {forbidden!r}"

    # Every procedure MUST have unique oagitSym* prefix
    procs = re.findall(r"procedure\(\s*([A-Za-z0-9_]+)\s*\(", source)
    assert procs, f"{path.name} defines no procedures"
    for name in procs:
        assert name.startswith("oagitSym"), (
            f"{path.name} procedure {name!r} violates oagitSym* prefix requirement"
        )
    assert len(procs) == len(set(procs)), f"duplicate procedures in {path.name}"


def test_symbol_skill_scripts_have_disjoint_procedures() -> None:
    exp_procs = set(
        re.findall(
            r"procedure\(\s*([A-Za-z0-9_]+)\s*\(",
            EXPORT_SYMBOL_IL.read_text(encoding="utf-8"),
        )
    )
    res_procs = set(
        re.findall(
            r"procedure\(\s*([A-Za-z0-9_]+)\s*\(",
            RESTORE_SYMBOL_IL.read_text(encoding="utf-8"),
        )
    )
    assert exp_procs.isdisjoint(res_procs), (
        f"overlapping procedures across export/restore: {exp_procs & res_procs}"
    )


def test_export_symbol_skill_protocol_and_safety() -> None:
    source = EXPORT_SYMBOL_IL.read_text(encoding="utf-8")
    for required in (
        "oagitSymBuildResponse",
        "oagitSymRunExport",
        'oagitRequest->action == "capture_symbol"',
        "dbFindOpenCellViewByName",
        "dbOpenCellViewByType",
        '"r"',
        "modifiedButNotSaved",
        "cv~>terminals",
        "term~>pins",
        "pinFigIds",
        'equal(layer "instance") && equal(purpose "drawing")',
        "arcBBox",
        "ellipseBBox",
        "labelType",
        '"normalLabel"',
        '"NLPLabel"',
        '"ILLabel"',
        '"textDisplay"',
        '"portOrder"',
        '"ILList"',
        '"interfaceLastChanged"',
        '"pin#"',
        '"viewSubType"',
        "LastChanged$",
        "cons('jsonArray",
        "isCallable('oagitJsonEncode)",
        "oagitWriteJsonFile",
        "oagitFail",
        "OAGIT_REQUEST_IL",
        "OAGIT_AUTO_EXIT",
    ):
        assert required in source, f"export_symbol.il missing {required!r}"

    # Never opens view for write
    assert '"w"' not in source

    # Pin figures MUST be collected before scanning graphics shapes
    build = source[source.index("procedure(oagitSymBuildResponse") :]
    assert build.index("oagitSymExpPins(cv pinFigIds)") < build.index(
        "oagitSymExpGraphics(cv pinFigIds)"
    )


def test_restore_symbol_skill_atomic_draft_publish_and_safety() -> None:
    source = RESTORE_SYMBOL_IL.read_text(encoding="utf-8")
    for required in (
        "oagitSymRunRestore",
        'oagitRequest->action == "restore_symbol"',
        "allow_libraries",
        "disallowed_library",
        "oagitDirHasLiveLocks",
        "live_lock",
        "target_exists",
        "draft_exists",
        '"schematicSymbol" "w"',
        "dbMakeNet",
        "dbCreateTerm",
        "dbCreatePin",
        "dbCreateRect",
        "dbCreateLine",
        "dbCreatePolygon",
        "dbCreatePath",
        "dbCreateArc(cv lpp rec->ellipse_bbox rec->arc_bbox)",
        "dbCreateEllipse",
        "dbCreateLabel",
        "lbl~>labelType = lType",
        "dbDeletePropByName",
        'dbCreateProp(cv "portOrder" "ILList"',
        "pin_contract_mismatch",
        "dbSave(cv)",
        "dbCopyCellView(cv lib cell view nil nil t)",
        "dbPurge(cv)",
        "ddDeleteObj(draftDd)",
        "purgeResult && car(purgeResult)",
        "deleteResult && car(deleteResult)",
        "isCallable('oagitJsonEncode)",
        "oagitWriteJsonFile",
        "OAGIT_REQUEST_IL",
        "OAGIT_AUTO_EXIT",
    ):
        assert required in source, f"restore_symbol.il missing {required!r}"

    # Must NEVER open target cell directly for write
    assert 'dbOpenCellViewByType(lib cell view "schematicSymbol" "w")' not in source
    assert 'dbOpenCellViewByType(lib cell view nil "w")' not in source


# ---------------------------------------------------------------------------
# 7. Reviewer 2 Regression Tests: Pin Move Translation, Dangling port_order,
#    Strict Label Type, Circle/Ellipse Roundtrip, JSON-Serializable Report
# ---------------------------------------------------------------------------


def test_real_symbol_pin_move_does_not_trigger_pin_contract_diff_or_conflict() -> None:
    base = _make_sar_adc_symbol()

    # Realistic Virtuoso pin move: BOTH pin.xy AND pin_figure.bbox translate by (0.0, +0.25)
    moved_pins = tuple(
        _rect_pin("VINP", p.direction, (0.0, 0.25), orient="R90")
        if p.name == "VINP"
        else p
        for p in base.pins
    )
    moved = build_symbol_snapshot(
        base.cellview,
        pins=moved_pins,
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )

    d = diff_symbols(base, moved)
    assert d.has_pin_contract_changes is False
    assert d.has_pin_geometry_changes is True
    assert d.has_graphics_changes is False
    assert len(d.pin_geometry_changes.moves) == 1
    assert d.pin_geometry_changes.moves[0].name == "VINP"
    assert d.pin_geometry_changes.moves[0].new_pin_figure_bbox == ((-0.025, 0.225), (0.025, 0.275))

    # Resizing pin_figure bbox (contract change!) MUST still trigger pin_contract_changes
    resized_pins = tuple(
        _rect_pin("VINP", p.direction, p.xy, half=0.05) if p.name == "VINP" else p
        for p in base.pins
    )
    resized = build_symbol_snapshot(
        base.cellview,
        pins=resized_pins,
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    d_resize = diff_symbols(base, resized)
    assert d_resize.has_pin_contract_changes is True
    assert d_resize.has_pin_geometry_changes is False

    # Merge: ours performs a real pin move (xy + bbox translated), theirs changes direction & layer & size
    theirs_pins = tuple(
        SymbolPin(
            name="VINP",
            direction="inputOutput",
            xy=p.xy,
            orient=p.orient,
            pin_figure=SymbolPinFigure(
                kind="rect",
                layer="metal1",
                purpose="drawing",
                bbox=((-0.05, -0.05), (0.05, 0.05)),
            ),
        )
        if p.name == "VINP"
        else p
        for p in base.pins
    )
    theirs = build_symbol_snapshot(
        base.cellview,
        pins=theirs_pins,
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    merged = merge_symbols(base, moved, theirs)
    assert merged.ok is True
    assert merged.conflicts == ()
    assert merged.snapshot is not None
    vinp = {p.name: p for p in merged.snapshot.pins}["VINP"]
    assert vinp.direction == "inputOutput"
    assert vinp.xy == (0.0, 0.25)
    assert vinp.orient == "R90"
    assert vinp.pin_figure.layer == "metal1"
    assert vinp.pin_figure.bbox == ((-0.05, 0.2), (0.05, 0.3))

    # Conflicting real pin moves on both branches conflict ONLY in pin_geometry (never pin_contract!)
    theirs_moved_pins = tuple(
        _rect_pin("VINP", p.direction, (0.0, 0.50)) if p.name == "VINP" else p
        for p in base.pins
    )
    theirs_moved = build_symbol_snapshot(
        base.cellview,
        pins=theirs_moved_pins,
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    res_conf = merge_symbols(base, moved, theirs_moved)
    assert res_conf.ok is False
    assert res_conf.snapshot is None
    assert all(c.domain == "pin_geometry" for c in res_conf.conflicts)


def test_symbol_merge_dangling_port_order_emits_explicit_conflict() -> None:
    base = _make_top_redun2_adc_symbol()  # pins: VINP, VDD, DOUTP<9:0>
    # Ours deletes VDD without removing it from port_order; theirs adds RST and updates port_order keeping VDD
    ours = build_symbol_snapshot(
        base.cellview,
        pins=tuple(p for p in base.pins if p.name != "VDD"),
        port_order=base.port_order,
        graphics=base.graphics,
        properties=base.properties,
    )
    theirs = build_symbol_snapshot(
        base.cellview,
        pins=base.pins + (_rect_pin("RST", "input", (0.0, -0.25)),),
        port_order=base.port_order + ("RST",),
        graphics=base.graphics,
        properties=base.properties,
    )
    res = merge_symbols(base, ours, theirs)
    assert res.ok is False
    assert res.snapshot is None
    assert any(
        c.domain == "pin_contract"
        and c.path == "port_order"
        and "dangling-port-order" in c.reason
        and "VDD" in c.reason
        for c in res.conflicts
    )


def test_symbol_label_type_rejection_circle_ellipse_equiv_and_json_report() -> None:
    cv = CellViewRef("work", "edge", "symbol")

    # Explicit mismatched label_type inside nlp_labels / il_labels MUST fail with SchemaValidationError
    with pytest.raises(SchemaValidationError, match="label_type"):
        build_symbol_snapshot(
            cv,
            graphics=SymbolGraphics(
                nlp_labels=(
                    SymbolLabel(
                        text="[@partName]",
                        xy=(0.0, 0.0),
                        label_type="normalLabel",
                    ),
                )
            ),
        )
    with pytest.raises(SchemaValidationError, match="label_type"):
        build_symbol_snapshot(
            cv,
            graphics=SymbolGraphics(
                il_labels=(
                    SymbolLabel(
                        text="cdsName()",
                        xy=(0.0, 0.0),
                        label_type="normalLabel",
                    ),
                )
            ),
        )
    with pytest.raises(SchemaValidationError, match="label_type"):
        raw_symbol_capture_to_snapshot(
            {
                "ok": True,
                "cellview": cv.to_dict(),
                "modified_but_not_saved": False,
                "pins": [],
                "graphics": {
                    "shapes": [],
                    "labels": [],
                    "nlp_labels": [
                        {
                            "text": "[@partName]",
                            "xy": [0.0, 0.0],
                            "label_type": "normalLabel",
                        }
                    ],
                    "il_labels": [],
                },
            }
        )

    # verify_symbol_roundtrip treats circle and equivalent square-bbox ellipse on same layer/purpose as equivalent
    circle_snap = build_symbol_snapshot(
        cv,
        graphics=SymbolGraphics(
            shapes=(
                SymbolShape(
                    kind="circle",
                    layer="device",
                    purpose="drawing",
                    bbox=((0.0, 0.0), (1.0, 1.0)),
                ),
            )
        ),
    )
    sq_ellipse_snap = build_symbol_snapshot(
        cv,
        graphics=SymbolGraphics(
            shapes=(
                SymbolShape(
                    kind="ellipse",
                    layer="device",
                    purpose="drawing",
                    bbox=((0.0, 0.0), (1.0, 1.0)),
                ),
            )
        ),
    )
    assert verify_symbol_roundtrip(circle_snap, sq_ellipse_snap, raise_on_mismatch=False).ok is True
    assert verify_symbol_roundtrip(sq_ellipse_snap, circle_snap, raise_on_mismatch=False).ok is True

    # Non-square ellipse or different layer MUST NOT match circle
    rect_ellipse_snap = build_symbol_snapshot(
        cv,
        graphics=SymbolGraphics(
            shapes=(
                SymbolShape(
                    kind="ellipse",
                    layer="device",
                    purpose="drawing",
                    bbox=((0.0, 0.0), (2.0, 1.0)),
                ),
            )
        ),
    )
    report_mismatch = verify_symbol_roundtrip(
        circle_snap, rect_ellipse_snap, raise_on_mismatch=False
    )
    assert report_mismatch.ok is False

    # SymbolRoundtripReport.to_dict() MUST be recursively JSON-serializable (no raw dataclasses!)
    serialized = json.dumps(report_mismatch.to_dict())
    decoded = json.loads(serialized)
    assert decoded["ok"] is False
    assert isinstance(decoded["issues"][0]["expected"], dict)
    assert decoded["issues"][0]["expected"]["kind"] == "circle"


def test_symbol_illist_properties_roundtrip_wire_and_diff_merge(tmp_path: Path) -> None:
    """Bug #4: Symbol OA ILList properties (e.g. termOrder) roundtrip cleanly without omissions."""

    cv = CellViewRef("demo_rf_lib", "DCO8", "symbol")
    pins = (
        _rect_pin("VDD", "inputOutput", (0.0, 0.5)),
        _rect_pin("VSS", "inputOutput", (0.0, -0.5)),
        _rect_pin("CTRL<7:0>", "input", (-0.5, 0.0)),
        _rect_pin("OUTP", "output", (0.5, 0.125)),
        _rect_pin("OUTN", "output", (0.5, -0.125)),
    )
    term_order = ["VDD", "VSS", "CTRL<7:0>", "OUTP", "OUTN"]

    # Build from raw capture payload including termOrder, mixed scalar ILList, and empty ILList
    raw_capture = {
        "ok": True,
        "error": None,
        "cellview": cv.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematicSymbol", "dbu_per_uu": 160.0},
        "pins": [
            {
                "name": p.name,
                "direction": p.direction,
                "xy": list(p.xy),
                "orient": p.orient,
                "pin_figure": {
                    "kind": p.pin_figure.kind,
                    "layer": p.pin_figure.layer,
                    "purpose": p.pin_figure.purpose,
                    "bbox": [list(p.pin_figure.bbox[0]), list(p.pin_figure.bbox[1])]
                    if p.pin_figure.bbox
                    else None,
                },
            }
            for p in pins
        ],
        "port_order": term_order,
        "graphics": {
            "shapes": [],
            "labels": [],
            "nlp_labels": [],
            "il_labels": [],
            "selection_box": [[-0.525, -0.525], [0.525, 0.525]],
            "origin": [0.0, 0.0],
        },
        "properties": {
            "termOrder": term_order,
            "mixedList": ["A", 1, -0.0, True, False],
            "emptyList": [],
            "vendorName": "TSMC28",
        },
        "omissions": [],
    }
    snap = raw_symbol_capture_to_snapshot(raw_capture)
    assert snap.omissions == ()
    assert snap.properties["termOrder"] == term_order
    assert snap.properties["mixedList"] == ["A", 1, 0.0, True, False]
    assert snap.properties["emptyList"] == []

    # Tuple input canonicalizes identically to list input (byte-for-byte deterministic YAML)
    snap_tuple = build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=term_order,
        graphics=snap.graphics,
        properties={
            "termOrder": tuple(term_order),
            "mixedList": ("A", 1, 0.0, True, False),
            "emptyList": (),
            "vendorName": "TSMC28",
        },
        metadata=snap.manifest.metadata,
    )
    dir_a = write_symbol_snapshot(snap, tmp_path / "a")
    dir_b = write_symbol_snapshot(snap_tuple, tmp_path / "b")
    assert (dir_a / "symbol.yaml").read_bytes() == (dir_b / "symbol.yaml").read_bytes()
    assert read_symbol_snapshot(dir_a, verify=True) == snap

    # build_symbol_restore_request_dict infers "ILList" and renders valid SKILL
    target = CellViewRef("SANDBOX", "DCO8_rt", "symbol")
    req_dict = build_symbol_restore_request_dict(
        snap,
        target,
        draft_cell="DCO8_rt__draft_1",
        overwrite=True,
        allow_libraries=("SANDBOX",),
    )
    assert req_dict["properties"] == [
        ["emptyList", [], "ILList"],
        ["mixedList", ["A", 1, 0.0, True, False], "ILList"],
        ["termOrder", term_order, "ILList"],
        ["vendorName", "TSMC28", "string"],
    ]
    rendered = "setq(oagitRequest '" + to_skill_literal(req_dict) + ")\n"
    _balanced_skill_parentheses(rendered)
    assert '("termOrder" ("VDD" "VSS" "CTRL<7:0>" "OUTP" "OUTN") "ILList")' in rendered
    assert '("emptyList" nil "ILList")' in rendered

    # verify_symbol_roundtrip accepts exact match, rejects reordered ILList & bool/int coercion
    assert verify_symbol_roundtrip(snap, snap_tuple, raise_on_mismatch=False).ok is True

    reordered = build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=term_order,
        graphics=snap.graphics,
        properties={**snap.properties, "termOrder": list(reversed(term_order))},
    )
    rep_order = verify_symbol_roundtrip(snap, reordered, raise_on_mismatch=False)
    assert rep_order.ok is False and rep_order.pin_contract_ok is False
    assert any(i.path == "properties['termOrder']" for i in rep_order.issues)

    bool_int_swap = build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=term_order,
        graphics=snap.graphics,
        properties={**snap.properties, "mixedList": ["A", True, 0.0, 1, False]},
    )
    rep_type = verify_symbol_roundtrip(snap, bool_int_swap, raise_on_mismatch=False)
    assert rep_type.ok is False and rep_type.pin_contract_ok is False
    assert any(i.path == "properties['mixedList']" for i in rep_type.issues)

    # diff_symbols and merge_symbols support ILList properties cleanly
    d = diff_symbols(snap, reordered)
    assert d.has_pin_contract_changes is True
    assert len(d.pin_contract_changes.property_changes) == 1
    assert d.pin_contract_changes.property_changes[0].key == "termOrder"

    ours = build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=term_order,
        graphics=snap.graphics,
        properties={**snap.properties, "termOrder": ["VDD", "VSS", "OUTP", "OUTN", "CTRL<7:0>"]},
    )
    theirs = build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=term_order,
        graphics=snap.graphics,
        properties={**snap.properties, "vendorName": "TSMC65"},
    )
    merged = merge_symbols(snap, ours, theirs)
    assert merged.ok is True and merged.snapshot is not None
    assert merged.snapshot.properties["termOrder"] == [
        "VDD",
        "VSS",
        "OUTP",
        "OUTN",
        "CTRL<7:0>",
    ]
    assert merged.snapshot.properties["vendorName"] == "TSMC65"
