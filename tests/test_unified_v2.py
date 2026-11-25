"""Unified 3-view (Schematic + Symbol + Layout) CLI integration tests for oa-git v0.2.

Verifies Plan A (Minimal Unified ``oagit`` CLI):
- Exactly 7 top-level commands (init, status, capture, diff, merge, restore, daemon).
- Seamless automatic dispatch across Schematic, Symbol, and Layout snapshots.
- Dirty-index tracking for sch.oa, symbol.oa, layout.oa, and custom layout view names.
- Noise-free layout status and diff (no duplicate-via alerts; pin bbox moves under Geometry).
- Unified 0-arg / 1-arg / 2-arg / multi-view diff with --stat & --connectivity-only.
- Unified 3-way layout merge (clean, spatial overlap conflict, --allow-spatial-overlap).
- Unified layout restore with shorthand, allowlist, live locks, omissions, and daemon/fallback.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

import oa_git.cli as cli_mod
from oa_git.cli import main
from oa_git.layout_backend import (
    FixtureLayoutBackend,
    raw_layout_capture_to_snapshot,
)
from oa_git.layout_model import (
    LayoutLabel,
    LayoutNet,
    LayoutRect,
    LayoutRestoreReceipt,
    LayoutSnapshot,
    LayoutVia,
)
from oa_git.layout_snapshot import (
    build_layout_snapshot,
    read_layout_snapshot,
    write_layout_snapshot,
)
from oa_git.model import (
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    OmissionRecord,
    Port,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    SnapshotIntegrityError,
)
from oa_git.project_state import load_index, write_project_config
from oa_git.snapshot import (
    build_schematic_snapshot,
    read_snapshot,
    snapshot_dir_for,
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
from oa_git.view_contract import OaSession

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "layout"
INVERTER_FIXTURE = FIXTURES_DIR / "inverter_capture.json"


# ---------------------------------------------------------------------------
# Factories & Helpers
# ---------------------------------------------------------------------------


def _make_sch(
    cv: CellViewRef,
    *,
    w: str = "2u",
    xy: tuple[float, float] = (1.0, 2.0),
) -> SchematicSnapshot:
    return build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"), Port("OUT", "output")),
            nets=(Net("IN"), Net("OUT")),
            instances=(
                Instance(
                    "M1",
                    "tsmcN65",
                    "nch",
                    "symbol",
                    params={"w": w},
                    param_types={"w": "string"},
                    connections={"G": "IN", "D": "OUT"},
                ),
            ),
        ),
        SchematicPlacement(instances=(InstancePlacement("M1", xy, "R0"),)),
    )


def _make_sym(
    cv: CellViewRef,
    *,
    in_xy: tuple[float, float] = (0.0, 0.0),
    out_dir: str = "output",
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
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("IN", "OUT"),
        graphics=SymbolGraphics(
            shapes=(
                SymbolShape(
                    kind="rect", layer="device", purpose="drawing", bbox=((0.2, -0.5), (1.8, 0.5))
                ),
            ),
            labels=(SymbolLabel(text="IN", xy=(0.3, 0.0), layer="pin", purpose="label"),),
            selection_box=((-0.05, -0.6), (2.05, 0.6)),
            origin=(0.0, 0.0),
        ),
        properties={"partName": cv.cell},
    )


def _make_layout(
    cv: CellViewRef,
    *,
    extra_objects: tuple[Any, ...] = (),
    omissions: tuple[OmissionRecord, ...] = (),
) -> LayoutSnapshot:
    payload = json.loads(INVERTER_FIXTURE.read_text(encoding="utf-8"))
    payload["cellview"] = cv.to_dict()
    base = raw_layout_capture_to_snapshot(payload)
    if not extra_objects and not omissions:
        return base
    return build_layout_snapshot(
        cv,
        (*base.objects, *extra_objects),
        cell_properties=base.cell_properties,
        omissions=(*base.omissions, *omissions),
        metadata=base.manifest.metadata,
    )


def _write_oa_view(
    oa_lib_dir: Path, cell: str, view: str, filename: str, content: bytes
) -> Path:
    view_dir = oa_lib_dir / cell / view
    view_dir.mkdir(parents=True, exist_ok=True)
    primary = view_dir / filename
    primary.write_bytes(content)
    (view_dir / "master.tag").write_text(f"-- Master.tag File\n{filename}\n", encoding="utf-8")
    return view_dir


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "unified@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Unified Test"], cwd=repo, check=True)


def _git_commit_all(repo: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)


def _invoke_json(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any], Any]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured


# ---------------------------------------------------------------------------
# 1. Minimal 7 Top-Level Commands & Universal Helpers
# ---------------------------------------------------------------------------


def test_help_keeps_exact_seven_minimal_top_level_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = cli_mod._build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert set(subparsers_action.choices) == {
        "init",
        "status",
        "capture",
        "diff",
        "merge",
        "restore",
        "daemon",
    }
    assert len(subparsers_action.choices) == 7

    assert main(["--help"]) == 0
    help_text = capsys.readouterr().out
    for cmd in ("init", "status", "capture", "diff", "merge", "restore", "daemon"):
        assert cmd in help_text

    # Verify new flags appear in crisp subcommand help
    assert main(["capture", "--help"]) == 0
    assert "--view-type" in capsys.readouterr().out

    assert main(["diff", "--help"]) == 0
    diff_help = capsys.readouterr().out
    assert "--stat" in diff_help
    assert "--connectivity-only" in diff_help

    assert main(["merge", "--help"]) == 0
    assert "--allow-spatial-overlap" in capsys.readouterr().out


def test_universal_three_view_snapshot_helpers_and_view_detection(tmp_path: Path) -> None:
    snaps = tmp_path / "snapshots"
    sch_cv = CellViewRef("work", "inv", "schematic")
    sym_cv = CellViewRef("work", "inv", "symbol")
    lay_cv = CellViewRef("work", "inv", "layout")
    custom_lay_cv = CellViewRef("work", "inv", "my_custom_view")

    sch_snap = _make_sch(sch_cv)
    sym_snap = _make_sym(sym_cv)
    lay_snap = _make_layout(lay_cv)
    custom_lay_snap = _make_layout(custom_lay_cv)

    for snap in (sch_snap, sym_snap, lay_snap, custom_lay_snap):
        dest = snapshot_dir_for(snaps, snap.manifest.cellview)
        cli_mod._write_any_snapshot(snap, dest)
        verified = cli_mod._verify_any_snapshot(dest)
        assert verified.view_type == snap.manifest.view_type
        reloaded = cli_mod._read_any_snapshot(dest, verify=True)
        assert reloaded == snap

    # Tampered layout fails closed in _verify_any_snapshot
    tamper_path = snapshot_dir_for(snaps, lay_cv) / "objects.jsonl"
    tamper_path.write_text(tamper_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError):
        cli_mod._verify_any_snapshot(snapshot_dir_for(snaps, lay_cv))

    # View type auto-detection rules
    assert cli_mod._detect_capture_view_type(lay_cv, snapshots_dir=snaps) == "layout"
    assert (
        cli_mod._detect_capture_view_type(
            CellViewRef("work", "inv", "layout_pulsic"), snapshots_dir=snaps
        )
        == "layout"
    )
    assert cli_mod._detect_capture_view_type(sym_cv, snapshots_dir=snaps) == "symbol"
    assert (
        cli_mod._detect_capture_view_type(
            CellViewRef("work", "inv", "symbol_alt"), snapshots_dir=snaps
        )
        == "symbol"
    )
    # Existing manifest view_type wins for custom view names!
    assert cli_mod._detect_capture_view_type(custom_lay_cv, snapshots_dir=snaps) == "layout"
    # Explicit override wins over auto-detection
    assert (
        cli_mod._detect_capture_view_type(
            CellViewRef("work", "inv", "unknown_view"),
            snapshots_dir=snaps,
            explicit_view_type="layout",
        )
        == "layout"
    )
    assert (
        cli_mod._detect_capture_view_type(
            CellViewRef("work", "inv", "unknown_view"), snapshots_dir=snaps
        )
        == "schematic"
    )


# ---------------------------------------------------------------------------
# 2. Unified `oagit status` (Schematic + Symbol + Layout + dup-via + custom view)
# ---------------------------------------------------------------------------


def test_unified_status_three_views_dup_via_badge_and_custom_layout_dirty_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "proj"
    oa_work = project / "oa" / "work"
    snapshots_dir = project / "snapshots"

    sch_cv = CellViewRef("work", "inv", "schematic")
    sym_cv = CellViewRef("work", "inv", "symbol")
    lay_cv = CellViewRef("work", "inv", "layout")
    custom_cv = CellViewRef("work", "amp2", "layout_pulsic")
    dup_cv = CellViewRef("work", "dup_blk", "layout")

    _write_oa_view(oa_work, "inv", "schematic", "sch.oa", b"SCH_V1")
    _write_oa_view(oa_work, "inv", "symbol", "symbol.oa", b"SYM_V1")
    lay_dir = _write_oa_view(oa_work, "inv", "layout", "layout.oa", b"LAY_V1")
    pulsic_dir = _write_oa_view(oa_work, "amp2", "layout_pulsic", "layout.oa", b"PULSIC_V1")
    _write_oa_view(oa_work, "dup_blk", "layout", "layout.oa", b"DUP_V1")

    write_project_config(
        project,
        {
            "oa_libs": {"work": str(oa_work)},
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
        },
    )

    sch_snap = _make_sch(sch_cv)
    sym_snap = _make_sym(sym_cv)
    lay_snap = _make_layout(lay_cv)
    pulsic_snap = _make_layout(custom_cv)
    dup_via = LayoutVia(
        via_def="M2_M1", origin=(1.0, 2.0), orient="R0", net="VDD", params={"cutRows": 2, "cutColumns": 1}
    )
    dup_snap = build_layout_snapshot(dup_cv, (dup_via, dup_via))

    for snap in (sch_snap, sym_snap, lay_snap, pulsic_snap, dup_snap):
        dest = snapshot_dir_for(snapshots_dir, snap.manifest.cellview)
        cli_mod._write_any_snapshot(snap, dest)
        cli_mod._record_index_after_capture(
            project,
            config={"schema": "oa-git-config/2"},
            oa_libs={"work": oa_work},
            snapshot=snap,
            destination=dest,
        )

    # All 5 clean (including custom layout_pulsic), no dup-via noise in JSON or text status
    code, st_json, _ = _invoke_json(["--json", "status", "-C", str(project)], capsys)
    assert code == 0
    assert st_json["ok"] is True
    by_key = {
        f"{s['cellview']['library']}/{s['cellview']['cell']}/{s['cellview']['view']}": s
        for s in st_json["snapshots"]
    }
    assert by_key[sch_cv.key]["view_type"] == "schematic"
    assert by_key[sym_cv.key]["view_type"] == "symbol"
    assert by_key[lay_cv.key]["view_type"] == "layout"
    assert by_key[custom_cv.key]["view_type"] == "layout"
    for snap_entry in by_key.values():
        assert "duplicate_vias_count" not in snap_entry

    state_map = {item["key"]: item["state"] for item in st_json["index_states"]}
    assert state_map == {
        sch_cv.key: "clean",
        sym_cv.key: "clean",
        lay_cv.key: "clean",
        custom_cv.key: "clean",
        dup_cv.key: "clean",
    }

    # Text status stays clean even when stacked identical vias exist
    assert main(["status", "-C", str(project)]) == 0
    st_text = capsys.readouterr().out
    assert "dup-via" not in st_text
    assert "Clean (5):" in st_text

    # Edit layout.oa on both standard layout AND custom layout_pulsic -> both become dirty
    (lay_dir / "layout.oa").write_bytes(b"LAY_V2")
    (pulsic_dir / "layout.oa").write_bytes(b"PULSIC_V2")

    code, ch_json, _ = _invoke_json(["--json", "status", "--changed", "-C", str(project)], capsys)
    assert code == 0
    changed_states = {item["key"]: (item["state"], item["reason"]) for item in ch_json["index_states"]}
    assert changed_states == {
        lay_cv.key: ("dirty", "source modified"),
        custom_cv.key: ("dirty", "source modified"),
    }

    # Also verify no-oa_libs mode ("Snapshots (...):" branch) stays free of dup-via noise
    plain_proj = tmp_path / "plain_proj"
    write_project_config(plain_proj, {"backend": "fixture"})
    write_layout_snapshot(dup_snap, snapshot_dir_for(plain_proj / "snapshots", dup_cv))
    assert main(["status", "-C", str(plain_proj)]) == 0
    plain_text = capsys.readouterr().out
    assert "work/dup_blk/layout [layout]" in plain_text
    assert "dup-via" not in plain_text


# ---------------------------------------------------------------------------
# 3. Unified `oagit capture` (Layout, Custom View, Mixed 3-View, 0-arg index_skip)
# ---------------------------------------------------------------------------


def test_unified_capture_single_layout_custom_view_mixed_and_zero_arg_index_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "cap_proj"
    oa_work = project / "oa" / "work"
    fixture_dir = tmp_path / "fixtures"

    sch_cv = CellViewRef("work", "inv", "schematic")
    sym_cv = CellViewRef("work", "inv", "symbol")
    lay_cv = CellViewRef("work", "inv", "layout")
    custom_cv = CellViewRef("work", "inv", "my_lay")

    _write_oa_view(oa_work, "inv", "schematic", "sch.oa", b"SCH_1")
    _write_oa_view(oa_work, "inv", "symbol", "symbol.oa", b"SYM_1")
    lay_oa_dir = _write_oa_view(oa_work, "inv", "layout", "layout.oa", b"LAY_1")
    custom_oa_dir = _write_oa_view(oa_work, "inv", "my_lay", "layout.oa", b"MYLAY_1")

    write_project_config(
        project,
        {
            "oa_libs": {"work": str(oa_work)},
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
        },
    )

    # 1. Single layout capture from raw layout JSON fixture file
    raw_lay_file = tmp_path / "inv_layout.json"
    raw_payload = json.loads(INVERTER_FIXTURE.read_text(encoding="utf-8"))
    raw_payload["cellview"] = lay_cv.to_dict()
    raw_lay_file.write_text(json.dumps(raw_payload), encoding="utf-8")

    code, single_json, _ = _invoke_json(
        [
            "--json",
            "capture",
            lay_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(raw_lay_file),
        ],
        capsys,
    )
    assert code == 0
    assert single_json["ok"] is True
    assert single_json["view_type"] == "layout"
    assert single_json["cellview"] == lay_cv.to_dict()
    assert Path(single_json["snapshot_dir"]) == snapshot_dir_for(project / "snapshots", lay_cv).resolve()
    assert single_json["content_sha256"]
    assert single_json["full_sha256"]
    assert single_json["omissions_count"] == 0
    assert len(single_json["captured"]) == 1
    assert single_json["skipped"] == []
    assert single_json["failed"] == []
    assert single_json["transport"] == "fixture"
    assert read_layout_snapshot(Path(single_json["snapshot_dir"]), verify=True).manifest.view_type == "layout"

    # Verify index recorded layout.oa + master.tag
    idx = load_index(project)
    assert lay_cv.key in idx
    assert [fp.path for fp in idx[lay_cv.key].source_files] == [
        "inv/layout/layout.oa",
        "inv/layout/master.tag",
    ]

    # 2. Custom layout view name with --view-type layout (text output check too)
    custom_fixture_dir = tmp_path / "custom_fix"
    write_layout_snapshot(_make_layout(custom_cv), custom_fixture_dir / "work" / "inv" / "my_lay")
    assert (
        main(
            [
                "capture",
                custom_cv.key,
                "--view-type",
                "layout",
                "-C",
                str(project),
                "--fixture",
                str(custom_fixture_dir),
            ]
        )
        == 0
    )
    cap_text = capsys.readouterr().out
    assert f"Captured {custom_cv.key} to" in cap_text
    assert "Content SHA-256:" in cap_text
    assert "Captured 1, skipped 0, failed 0 (transport=fixture)" in cap_text
    idx2 = load_index(project)
    assert [fp.path for fp in idx2[custom_cv.key].source_files] == [
        "inv/my_lay/layout.oa",
        "inv/my_lay/master.tag",
    ]

    # 3. Mixed 3-view capture using REAL un-mocked FixtureBackend on a single directory
    #    containing schematic + symbol + layout snapshots!
    sch_snap = _make_sch(sch_cv)
    sym_snap = _make_sym(sym_cv)
    lay_snap_v2 = _make_layout(
        lay_cv,
        extra_objects=(LayoutLabel(text="V2", xy=(1.0, 1.0), layer="text", purpose="drawing"),),
    )
    write_snapshot(sch_snap, fixture_dir / "work" / "inv" / "schematic")
    write_symbol_snapshot(sym_snap, fixture_dir / "work" / "inv" / "symbol")
    write_layout_snapshot(lay_snap_v2, fixture_dir / "work" / "inv" / "layout")
    write_layout_snapshot(_make_layout(custom_cv), fixture_dir / "work" / "inv" / "my_lay")

    real_backend = cli_mod._backend
    backend_creations: list[Any] = []

    def spy_backend(settings, **kw):
        backend_creations.append(settings)
        return real_backend(settings, **kw)

    monkeypatch.setattr(cli_mod, "_backend", spy_backend)

    code, mixed_json, _ = _invoke_json(
        [
            "--json",
            "capture",
            sch_cv.key,
            sym_cv.key,
            lay_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
        ],
        capsys,
    )
    assert code == 0
    assert mixed_json["ok"] is True
    captured_map = {
        f"{c['cellview']['library']}/{c['cellview']['cell']}/{c['cellview']['view']}": c["view_type"]
        for c in mixed_json["captured"]
    }
    assert captured_map == {
        sch_cv.key: "schematic",
        sym_cv.key: "symbol",
        lay_cv.key: "layout",
    }
    assert len(backend_creations) == 1
    assert read_snapshot(snapshot_dir_for(project / "snapshots", sch_cv), verify=True) == sch_snap
    assert read_symbol_snapshot(snapshot_dir_for(project / "snapshots", sym_cv), verify=True) == sym_snap
    assert read_layout_snapshot(snapshot_dir_for(project / "snapshots", lay_cv), verify=True) == lay_snap_v2

    # 4. 0-arg `oagit capture` when all 4 views (sch + sym + layout + custom my_lay) are clean
    #    -> transport="index_skip" and ZERO backend creations!
    code, skip_json, _ = _invoke_json(
        ["--json", "capture", "-C", str(project), "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert skip_json["ok"] is True
    assert skip_json["transport"] == "index_skip"
    assert skip_json["captured"] == []
    assert {s["key"] for s in skip_json["skipped"]} == {
        sch_cv.key,
        sym_cv.key,
        lay_cv.key,
        custom_cv.key,
    }
    assert len(backend_creations) == 1  # ZERO new backend calls!

    # 5. Modify ONLY layout.oa and my_lay/layout.oa -> 0-arg capture re-captures ONLY those 2!
    (lay_oa_dir / "layout.oa").write_bytes(b"LAY_2")
    (custom_oa_dir / "layout.oa").write_bytes(b"MYLAY_2")
    code, partial_json, _ = _invoke_json(
        ["--json", "capture", "-C", str(project), "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert {
        f"{c['cellview']['library']}/{c['cellview']['cell']}/{c['cellview']['view']}"
        for c in partial_json["captured"]
    } == {lay_cv.key, custom_cv.key}
    assert {s["key"] for s in partial_json["skipped"]} == {sch_cv.key, sym_cv.key}
    assert len(backend_creations) == 2

    # 6. Immediate re-run of `oagit capture --changed` skips all 4 again -> 0 backend calls!
    code, skip2_json, _ = _invoke_json(
        ["--json", "capture", "--changed", "-C", str(project), "--fixture", str(fixture_dir)],
        capsys,
    )
    assert code == 0
    assert skip2_json["transport"] == "index_skip"
    assert len(skip2_json["skipped"]) == 4
    assert len(backend_creations) == 2


# ---------------------------------------------------------------------------
# 4. Unified `oagit diff` (0-arg, HEAD~1..HEAD multi-view, --stat, --connectivity-only)
# ---------------------------------------------------------------------------


def test_unified_diff_three_views_stat_connectivity_only_dup_vias_and_shorthand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "diff_repo"
    _init_git_repo(repo)
    snaps = repo / "snapshots"

    sch_cv = CellViewRef("work", "inv", "schematic")
    sym_cv = CellViewRef("work", "inv", "symbol")
    lay_cv = CellViewRef("work", "inv", "layout")

    # Commit 1: initial schematic + symbol + layout
    lay_v1 = _make_layout(lay_cv)
    write_snapshot(_make_sch(sch_cv, w="2u"), snapshot_dir_for(snaps, sch_cv))
    write_symbol_snapshot(_make_sym(sym_cv, out_dir="output"), snapshot_dir_for(snaps, sym_cv))
    write_layout_snapshot(lay_v1, snapshot_dir_for(snaps, lay_cv))
    _git_commit_all(repo, "v1 all three views")

    # Commit 2: edit schematic (param w), symbol (pin move), layout (M2 rect + duplicate via)
    existing_via = lay_v1.objects_by_type("via")[0]
    lay_v2 = _make_layout(
        lay_cv,
        extra_objects=(
            existing_via,
            LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 10.0), (12.0, 11.0)), net="VDD"),
            LayoutLabel(text="REV2", xy=(2.0, 2.0), layer="text", purpose="drawing"),
        ),
    )
    write_snapshot(_make_sch(sch_cv, w="4u"), snapshot_dir_for(snaps, sch_cv))
    write_symbol_snapshot(_make_sym(sym_cv, in_xy=(0.0, 0.25)), snapshot_dir_for(snaps, sym_cv))
    write_layout_snapshot(lay_v2, snapshot_dir_for(snaps, lay_cv))
    _git_commit_all(repo, "v2 edits across all 3 views")

    monkeypatch.chdir(repo)

    # 1. 0-arg clean diff -> "No changes." exit 0
    assert main(["diff", "--exit-code"]) == 0
    assert capsys.readouterr().out.strip() == "No changes."

    # 2. Multi-view `HEAD~1..HEAD` JSON -> diffs Schematic + Symbol + Layout together!
    code, multi_json, _ = _invoke_json(["diff", "HEAD~1..HEAD", "--exit-code", "--json"], capsys)
    assert code == 1
    assert multi_json["ok"] is True
    assert multi_json["multi"] is True
    assert multi_json["count"] == 3
    assert multi_json["changed_count"] == 3
    by_key = {item["key"]: item for item in multi_json["diffs"]}
    assert by_key[sch_cv.key]["view_type"] == "schematic"
    assert by_key[sym_cv.key]["view_type"] == "symbol"
    assert by_key[lay_cv.key]["view_type"] == "layout"
    assert by_key[lay_cv.key]["has_geometry_changes"] is True
    assert by_key[lay_cv.key]["has_connectivity_changes"] is False
    assert "duplicate_via_changes" not in by_key[lay_cv.key]

    # 3. Multi-view `HEAD~1..HEAD --stat` renders compact stats for ALL 3 views without dup-via noise
    assert main(["diff", "HEAD~1..HEAD", "--stat"]) == 0
    stat_text = capsys.readouterr().out
    assert f"=== {sch_cv.key} [schematic] (modified) ===" in stat_text
    assert f"{sch_cv.key} [schematic]: electrical=1, placement=0" in stat_text
    assert f"=== {sym_cv.key} [symbol] (modified) ===" in stat_text
    assert f"{sym_cv.key} [symbol]: contract=0, geometry=1, graphics=0" in stat_text
    assert f"=== {lay_cv.key} [layout] (modified) ===" in stat_text
    assert "DUPLICATE_VIAS" not in stat_text
    assert "Duplicate / overlapping vias" not in stat_text

    # 4. Layout-only `--connectivity-only` AND `--electrical-only` on geometry-only change -> exit 0!
    for flag in ("--connectivity-only", "--electrical-only"):
        code, conn_json, _ = _invoke_json(
            ["diff", "HEAD~1..HEAD", "--cellview", lay_cv.key, flag, "--exit-code", "--json"],
            capsys,
        )
        assert code == 0
        assert conn_json["ok"] is True
        assert conn_json["view_type"] == "layout"
        assert "geometry" not in conn_json
        assert "annotation" not in conn_json
        assert "duplicate_via_changes" not in conn_json

    # 5. Edit working layout with a connectivity change (add net CLK) -> 0-arg + 1-arg shorthand!
    lay_v3 = _make_layout(
        lay_cv,
        extra_objects=(
            existing_via,
            LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 10.0), (12.0, 11.0)), net="VDD"),
            LayoutNet(name="CLK", sig_type="clock"),
        ),
    )
    write_layout_snapshot(lay_v3, snapshot_dir_for(snaps, lay_cv))

    # 1-arg `<lib>/<cell>/<view>` shorthand diffs HEAD vs working layout!
    code, short_json, _ = _invoke_json(
        ["diff", lay_cv.key, "--connectivity-only", "--exit-code", "--json"], capsys
    )
    assert code == 1
    assert short_json["view_type"] == "layout"
    assert short_json["has_connectivity_changes"] is True
    assert short_json["connectivity"]["nets"][0]["name"] == "CLK"

    # Full 0-arg working diff text shows only modified layout
    assert main(["diff"]) == 0
    work_text = capsys.readouterr().out
    assert f"=== {lay_cv.key} [layout] (modified) ===" in work_text
    assert "CLK (added)" in work_text
    assert sch_cv.key not in work_text

    # 2-arg local snapshot dirs diff with --stat & --json
    old_dir = write_layout_snapshot(lay_v1, tmp_path / "l_old")
    new_dir = write_layout_snapshot(lay_v2, tmp_path / "l_new")
    code, two_json, _ = _invoke_json(
        ["diff", str(old_dir), str(new_dir), "--stat", "--exit-code", "--json"], capsys
    )
    assert code == 1
    assert two_json["view_type"] == "layout"
    assert "duplicate_via_changes" not in two_json
    assert "summary" in two_json
    assert "stat" in two_json

    # 6. Pin-only bbox move -> --connectivity-only --exit-code returns 0, full diff reports pin_moves
    lay_pins = lay_v1.objects_by_type("pin")
    assert lay_pins, "fixture layout should contain at least one pin"
    pin0 = lay_pins[0]
    assert pin0.bbox is not None
    (px0, py0), (px1, py1) = pin0.bbox
    moved_pin = replace(pin0, bbox=((round(px0 + 0.5, 4), py0), (round(px1 + 0.5, 4), py1)))
    moved_objs = tuple(moved_pin if o == pin0 else o for o in lay_v1.objects)
    lay_pin_moved = build_layout_snapshot(
        lay_cv, moved_objs, cell_properties=lay_v1.cell_properties, metadata=lay_v1.manifest.metadata
    )
    pin_moved_dir = write_layout_snapshot(lay_pin_moved, tmp_path / "l_pin_moved")
    code, pm_conn_json, _ = _invoke_json(
        ["diff", str(old_dir), str(pin_moved_dir), "--connectivity-only", "--exit-code", "--json"],
        capsys,
    )
    assert code == 0
    assert pm_conn_json["has_connectivity_changes"] is False
    code, pm_full_json, _ = _invoke_json(
        ["diff", str(old_dir), str(pin_moved_dir), "--stat", "--exit-code", "--json"],
        capsys,
    )
    assert code == 1
    assert pm_full_json["has_geometry_changes"] is True
    assert pm_full_json["summary"]["pin_moves"] == 1
    assert pm_full_json["summary"]["pins"] == 0


# ---------------------------------------------------------------------------
# 5. Unified `oagit merge` for Layout (clean, spatial conflict, --allow-spatial-overlap, shorthand)
# ---------------------------------------------------------------------------


def test_unified_merge_layout_clean_spatial_conflict_override_and_shorthand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "merge_proj"
    write_project_config(project, {"backend": "fixture"})
    cv = CellViewRef("work", "inv", "layout")
    base = _make_layout(cv)

    # Store base at canonical snapshot dir so <lib>/<cell>/<view> shorthand resolves it!
    write_layout_snapshot(base, snapshot_dir_for(project / "snapshots", cv))

    # Clean merge: disjoint rects added on M2
    rect_ours = LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 0.0), (11.0, 1.0)), net="IN")
    rect_theirs = LayoutRect(layer="M2", purpose="drawing", bbox=((20.0, 0.0), (21.0, 1.0)), net="OUT")
    write_layout_snapshot(_make_layout(cv, extra_objects=(rect_ours,)), project / "ours_clean")
    write_layout_snapshot(_make_layout(cv, extra_objects=(rect_theirs,)), project / "theirs_clean")

    code, clean_json, _ = _invoke_json(
        [
            "--json",
            "merge",
            cv.key,
            "ours_clean",
            "theirs_clean",
            "-o",
            "merged_clean",
            "-C",
            str(project),
        ],
        capsys,
    )
    assert code == 0
    assert clean_json["ok"] is True
    assert clean_json["view_type"] == "layout"
    assert clean_json["conflicts"] == []
    assert Path(clean_json["snapshot_dir"]) == (project / "merged_clean").resolve()
    merged = read_layout_snapshot(project / "merged_clean", verify=True)
    assert clean_json["content_sha256"] == merged.manifest.content_sha256
    assert clean_json["full_sha256"] == merged.manifest.full_sha256
    assert len(merged.objects) == len(base.objects) + 2

    # Spatial overlap conflict: ours & theirs add overlapping M2 shapes on different nets
    overlap_ours = LayoutRect(layer="M2", purpose="drawing", bbox=((10.0, 10.0), (12.0, 12.0)), net="IN")
    overlap_theirs = LayoutRect(layer="M2", purpose="drawing", bbox=((11.0, 11.0), (13.0, 13.0)), net="OUT")
    write_layout_snapshot(_make_layout(cv, extra_objects=(overlap_ours,)), project / "ours_ov")
    write_layout_snapshot(_make_layout(cv, extra_objects=(overlap_theirs,)), project / "theirs_ov")

    conflict_out = project / "merged_conflict"
    code, conf_json, _ = _invoke_json(
        [
            "--json",
            "merge",
            cv.key,
            "ours_ov",
            "theirs_ov",
            "-o",
            "merged_conflict",
            "-C",
            str(project),
        ],
        capsys,
    )
    assert code == 1
    assert conf_json["ok"] is False
    assert conf_json["view_type"] == "layout"
    assert len(conf_json["conflicts"]) >= 1
    assert conf_json["conflicts"][0]["reason"] == "spatial_overlap_on_same_layer"
    assert not conflict_out.exists()

    # Text mode formats spatial conflict cleanly
    assert (
        main(
            [
                "merge",
                cv.key,
                "ours_ov",
                "theirs_ov",
                "-o",
                "merged_conflict",
                "-C",
                str(project),
            ]
        )
        == 1
    )
    conf_text = capsys.readouterr().out
    assert "Layout merge conflicts" in conf_text
    assert "spatial_overlap_on_same_layer" in conf_text
    assert not conflict_out.exists()

    # --allow-spatial-overlap succeeds cleanly and writes output
    code, allow_json, _ = _invoke_json(
        [
            "--json",
            "merge",
            cv.key,
            "ours_ov",
            "theirs_ov",
            "-o",
            "merged_allowed",
            "--allow-spatial-overlap",
            "-C",
            str(project),
        ],
        capsys,
    )
    assert code == 0
    assert allow_json["ok"] is True
    assert allow_json["view_type"] == "layout"
    assert (project / "merged_allowed" / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# 6. Unified `oagit restore` for Layout (shorthand, guards, live lock, daemon/fallback)
# ---------------------------------------------------------------------------


def test_unified_restore_layout_shorthand_roundtrip_and_fail_closed_guards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "res_proj"
    oa_sandbox = project / "oa" / "sandbox"
    oa_sandbox.mkdir(parents=True)
    fixture_dir = tmp_path / "res_fix"
    fixture_dir.mkdir()

    src_cv = CellViewRef("work", "inv", "layout")
    dst_cv = CellViewRef("sandbox", "inv", "layout")

    write_project_config(
        project,
        {
            "oa_libs": {"sandbox": str(oa_sandbox)},
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
        },
    )
    snap = _make_layout(src_cv)
    write_layout_snapshot(snap, snapshot_dir_for(project / "snapshots", src_cv))

    # 1. Shorthand restore roundtrip (`work/inv/layout` -> `sandbox/inv/layout`)
    code, res_json, _ = _invoke_json(
        [
            "--json",
            "restore",
            src_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            dst_cv.key,
        ],
        capsys,
    )
    assert code == 0
    assert res_json["ok"] is True
    assert res_json["view_type"] == "layout"
    assert res_json["target"] == dst_cv.to_dict()
    assert res_json["published"] is True
    assert res_json["draft_cell"]
    assert res_json["pcell_verified"] is True
    assert res_json["connectivity_verified"] is True
    assert res_json["readback_verified"] is True
    assert isinstance(res_json["object_counts"], dict)
    assert isinstance(res_json["details"], dict)
    assert res_json["transport"] == "fixture"

    # Text summary includes layout verification lines
    assert (
        main(
            [
                "restore",
                src_cv.key,
                "-C",
                str(project),
                "--fixture",
                str(fixture_dir),
                "--target",
                "sandbox/inv_text/layout",
            ]
        )
        == 0
    )
    res_text = capsys.readouterr().out
    assert "Restored sandbox/inv_text/layout" in res_text
    assert "PCell verified: True" in res_text
    assert "Connectivity verified: True" in res_text
    assert "Readback verified: True" in res_text

    # 2. Disallowed library fails closed with actionable hint
    code, err_lib, cap_lib = _invoke_json(
        [
            "--json",
            "restore",
            src_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            "forbidden/inv/layout",
        ],
        capsys,
    )
    assert code == 2
    assert err_lib["ok"] is False
    assert err_lib["error_type"] == "DisallowedLibraryError"
    assert "--allow-library forbidden" in err_lib["hint"]
    assert cap_lib.err == ""

    # 3. Auto-inferred oa_view_dir live lock rejection (.cdslck under oa_libs["sandbox"])
    locked_view_dir = oa_sandbox / "inv" / "layout"
    locked_view_dir.mkdir(parents=True)
    lock_file = locked_view_dir / "layout.oa.cdslck"
    lock_file.write_text("active lock", encoding="utf-8")
    code, err_lock, _ = _invoke_json(
        [
            "--json",
            "restore",
            src_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            dst_cv.key,
        ],
        capsys,
    )
    assert code == 2
    assert err_lock["error_type"] == "LiveLockError"
    assert "Virtuoso" in err_lock["hint"]
    lock_file.unlink()

    # 4. Omissions guard blocks without --allow-omissions and succeeds with --allow-omissions
    om_cv = CellViewRef("work", "inv_om", "layout")
    om_snap = _make_layout(
        om_cv,
        omissions=(OmissionRecord("rod", "r1", "unsupported rod object"),),
    )
    write_layout_snapshot(om_snap, snapshot_dir_for(project / "snapshots", om_cv))

    code, err_om, _ = _invoke_json(
        [
            "--json",
            "restore",
            om_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            "sandbox/inv_om/layout",
        ],
        capsys,
    )
    assert code == 2
    assert err_om["error_type"] == "UnsupportedObjectError"
    assert "--allow-omissions" in err_om["hint"]

    code, ok_om, _ = _invoke_json(
        [
            "--json",
            "restore",
            om_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            "sandbox/inv_om/layout",
            "--allow-omissions",
        ],
        capsys,
    )
    assert code == 0
    assert ok_om["published"] is True

    # 5. Existing target fails without --overwrite (with hint) and succeeds with --overwrite
    write_layout_snapshot(_make_layout(dst_cv), fixture_dir / "sandbox" / "inv" / "layout")
    code, err_ow, _ = _invoke_json(
        [
            "--json",
            "restore",
            src_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            dst_cv.key,
        ],
        capsys,
    )
    assert code == 2
    assert err_ow["error_type"] == "BridgeExecutionError"
    assert "--overwrite" in err_ow["hint"]

    code, ok_ow, _ = _invoke_json(
        [
            "--json",
            "restore",
            src_cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            dst_cv.key,
            "--overwrite",
        ],
        capsys,
    )
    assert code == 0
    assert ok_ow["published"] is True
    assert ok_ow["readback_verified"] is True


def test_unified_layout_daemon_and_skill_runner_fallback_wiring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify both direct backend.capture_layout/restore_layout AND SkillRequestRunner fallback."""

    project = tmp_path / "virt_proj"
    write_project_config(
        project,
        {
            "cds_lib": "/remote/work/cds.lib",
            "ssh_host": "eda-server",
            "allow_libraries": ["sandbox"],
            "backend": "virtuoso",
            "daemon": {"enabled": True},
        },
    )

    cv = CellViewRef("work", "inv", "layout")
    dst_cv = CellViewRef("sandbox", "inv", "layout")
    raw_cap = json.loads(INVERTER_FIXTURE.read_text(encoding="utf-8"))
    raw_cap["cellview"] = cv.to_dict()

    # Case A: Mock backend WITHOUT capture_layout/restore_layout -> falls back to VirtuosoLayoutBackend(skill_runner=mock)
    recorded_actions: list[tuple[str, Any]] = []

    class SkillRunnerOnlyBackend:
        last_transport = "daemon"

        def run_skill_request(self, action: str, request: dict[str, Any], *, cds_lib=None, session=None):
            recorded_actions.append((action, session))
            if action == "capture_layout":
                out = dict(raw_cap)
                out["cellview"] = {
                    "library": request["library"],
                    "cell": request["cell"],
                    "view": request["view"],
                }
                return out
            if action == "restore_layout":
                return {
                    "ok": True,
                    "error": None,
                    "error_code": None,
                    "target": request["target"],
                    "published": True,
                    "draft_cell": request["draft_cell"],
                    "pcell_verified": True,
                    "connectivity_verified": True,
                    "object_counts": {"instances": 2, "nets": 4},
                    "pcell_mismatches": [],
                    "connectivity_mismatches": [],
                }
            raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(cli_mod, "_backend", lambda settings, **kw: SkillRunnerOnlyBackend())

    # Warm daemon capture (session=None)
    code, cap_out, _ = _invoke_json(["--json", "capture", cv.key, "-C", str(project)], capsys)
    assert code == 0
    assert cap_out["view_type"] == "layout"
    assert cap_out["transport"] == "daemon"
    assert recorded_actions[-1] == ("capture_layout", None)

    # Restore with --one-shot passes OaSession(transport="one_shot")
    code, res_out, _ = _invoke_json(
        [
            "--json",
            "restore",
            cv.key,
            "-C",
            str(project),
            "--target",
            dst_cv.key,
            "--one-shot",
            "--skip-readback",
        ],
        capsys,
    )
    assert code == 0
    assert res_out["view_type"] == "layout"
    assert res_out["published"] is True
    assert recorded_actions[-1][0] == "restore_layout"
    assert isinstance(recorded_actions[-1][1], OaSession)
    assert recorded_actions[-1][1].transport == "one_shot"

    # Case B: Backend exposing Worker A's keyword-only `restore_layout(snapshot, *, target=None, ...)`
    seen_kw_restore: list[dict[str, Any]] = []

    class WorkerAContractBackend:
        last_transport = "daemon"

        def restore_layout(
            self,
            snapshot: LayoutSnapshot,
            *,
            target: CellViewRef | None = None,
            cds_lib: Path | str | None = None,
            allow_libraries: Any = (),
            overwrite: bool = False,
            allow_omissions: bool = False,
            verify_readback: bool = True,
            oa_view_dir: Path | None = None,
            session: OaSession | None = None,
        ) -> LayoutRestoreReceipt:
            seen_kw_restore.append(
                {
                    "target": target,
                    "cds_lib": cds_lib,
                    "overwrite": overwrite,
                    "session": session,
                }
            )
            eff_target = target or snapshot.manifest.cellview
            return LayoutRestoreReceipt(
                target=eff_target,
                published=True,
                draft_cell="inv__draft_01",
                pcell_verified=True,
                connectivity_verified=True,
                readback_verified=verify_readback,
                object_counts={"instances": 2},
                details={"backend": "virtuoso"},
                transport="one_shot",
            )

    monkeypatch.setattr(cli_mod, "_backend", lambda settings, **kw: WorkerAContractBackend())
    code, kw_out, _ = _invoke_json(
        [
            "--json",
            "restore",
            cv.key,
            "-C",
            str(project),
            "--target",
            dst_cv.key,
            "--overwrite",
        ],
        capsys,
    )
    assert code == 0
    assert kw_out["ok"] is True
    assert kw_out["view_type"] == "layout"
    assert kw_out["transport"] == "one_shot"
    assert seen_kw_restore[0]["target"] == dst_cv
    assert seen_kw_restore[0]["overwrite"] is True
    assert seen_kw_restore[0]["session"] is None


# ---------------------------------------------------------------------------
# 9. Regression: CellView shorthand vs same-named OA library dir at repo root
# ---------------------------------------------------------------------------


def test_shorthand_resolves_to_snapshots_when_same_named_oa_dir_exists_at_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `<root>/<lib>/<cell>/<view>` is a real OA directory (no manifest.json)
    while `snapshots/<lib>/<cell>/<view>/manifest.json` exists, `<lib>/<cell>/<view>`
    shorthand in restore/merge/diff must resolve to `snapshots/...` while preserving
    Fail-Closed checks for explicit non-cellview paths or missing manifests.
    """

    repo = tmp_path / "oa_at_root_repo"
    _init_git_repo(repo)
    snaps = repo / "snapshots"

    sch_cv = CellViewRef("oagit_test", "inv", "schematic")
    lay_cv = CellViewRef("oagit_test", "inv", "layout")
    dst_sch_cv = CellViewRef("sandbox", "inv_rest", "schematic")
    dst_lay_cv = CellViewRef("sandbox", "inv_rest", "layout")

    # Real OA library directories directly under repo root (no manifest.json)
    _write_oa_view(repo / "oagit_test", "inv", "schematic", "sch.oa", b"OA_SCH_BIN")
    _write_oa_view(repo / "oagit_test", "inv", "layout", "layout.oa", b"OA_LAY_BIN")

    # Canonical snapshots under snapshots/
    sch_base = _make_sch(sch_cv, w="2u")
    lay_base = _make_layout(lay_cv)
    write_snapshot(sch_base, snapshot_dir_for(snaps, sch_cv))
    write_layout_snapshot(lay_base, snapshot_dir_for(snaps, lay_cv))

    write_project_config(
        repo,
        {
            "oa_libs": {"oagit_test": str(repo / "oagit_test")},
            "allow_libraries": ["sandbox"],
            "backend": "fixture",
        },
    )
    _git_commit_all(repo, "base: snapshots alongside root OA library dir")

    # 1. `oagit restore <lib>/<cell>/<view>` (schematic + layout) works from repo root
    monkeypatch.chdir(repo)
    code, res_sch, _ = _invoke_json(
        ["--json", "restore", sch_cv.key, "--target", dst_sch_cv.key, "--overwrite"],
        capsys,
    )
    assert code == 0
    assert res_sch["ok"] is True
    assert res_sch["published"] is True
    assert res_sch["view_type"] == "schematic"

    code, res_lay, _ = _invoke_json(
        ["--json", "restore", lay_cv.key, "--target", dst_lay_cv.key, "--overwrite"],
        capsys,
    )
    assert code == 0
    assert res_lay["ok"] is True
    assert res_lay["published"] is True
    assert res_lay["view_type"] == "layout"

    # 2. `oagit merge <lib>/<cell>/<view> ours/ theirs/ -o merged/` resolves base to snapshots/
    ours_dir = repo / "merge_ours"
    theirs_dir = repo / "merge_theirs"
    merged_dir = repo / "merge_out"
    write_snapshot(_make_sch(sch_cv, w="4u"), ours_dir)
    write_snapshot(_make_sch(sch_cv, w="2u", xy=(3.0, 2.0)), theirs_dir)
    code, merge_out, _ = _invoke_json(
        ["--json", "merge", sch_cv.key, str(ours_dir), str(theirs_dir), "-o", str(merged_dir)],
        capsys,
    )
    assert code == 0
    assert merge_out["ok"] is True
    merged_snap = read_snapshot(merged_dir, verify=True)
    assert merged_snap.electrical.instances[0].params["w"] == "4u"
    assert merged_snap.placement.instances[0].xy == (3.0, 2.0)

    # 3. `oagit diff` (both 1-arg and 2-arg `HEAD <lib>/<cell>/<view>`) resolves to snapshots/
    write_snapshot(_make_sch(sch_cv, w="6u"), snapshot_dir_for(snaps, sch_cv))
    code, diff_1, _ = _invoke_json(["--json", "diff", sch_cv.key], capsys)
    assert code == 0
    assert diff_1["ok"] is True
    assert diff_1["electrical"]["param_changes"][0]["new"] == "6u"

    code, diff_2, _ = _invoke_json(["--json", "diff", "HEAD", sch_cv.key], capsys)
    assert code == 0
    assert diff_2["ok"] is True
    assert diff_2["electrical"]["param_changes"][0]["new"] == "6u"

    # 4. Fail-Closed: explicit non-cellview path (`./oagit_test/inv/schematic`) STILL fails closed
    code, err_explicit, _ = _invoke_json(
        ["--json", "restore", "./oagit_test/inv/schematic", "--target", dst_sch_cv.key],
        capsys,
    )
    assert code == 2
    assert err_explicit["ok"] is False
    assert err_explicit["error_type"] == "SnapshotIntegrityError"

    code, err_merge_explicit, _ = _invoke_json(
        [
            "--json",
            "merge",
            "./oagit_test/inv/schematic",
            str(ours_dir),
            str(theirs_dir),
            "-o",
            str(repo / "merge_fail_out"),
        ],
        capsys,
    )
    assert code == 2
    assert err_merge_explicit["ok"] is False
    assert err_merge_explicit["error_type"] == "SnapshotIntegrityError"

    code, err_diff_explicit, _ = _invoke_json(
        ["--json", "diff", "HEAD", "./oagit_test/inv/schematic"],
        capsys,
    )
    assert code == 2
    assert err_diff_explicit["ok"] is False
    assert err_diff_explicit["error_type"] == "SchemaValidationError"

    # 5. Fail-Closed: cellview shorthand when NEITHER root OA dir NOR snapshots/ has manifest.json
    _write_oa_view(repo / "oagit_test", "uncaptured", "schematic", "sch.oa", b"OA_UNCAPTURED")
    code, err_uncap, _ = _invoke_json(
        ["--json", "restore", "oagit_test/uncaptured/schematic", "--target", dst_sch_cv.key],
        capsys,
    )
    assert code == 2
    assert err_uncap["ok"] is False
    assert err_uncap["error_type"] == "SnapshotIntegrityError"

    code, err_uncap_diff, _ = _invoke_json(
        ["--json", "diff", "HEAD", "oagit_test/uncaptured/schematic"],
        capsys,
    )
    assert code == 2
    assert err_uncap_diff["ok"] is False
    assert err_uncap_diff["error_type"] == "SchemaValidationError"

    # 6. Explicit 3-segment local snapshot dir WITH manifest.json still takes precedence over snapshots/
    explicit_snap_dir = repo / "oagit_test" / "inv" / "alt_snap"
    write_snapshot(_make_sch(sch_cv, w="9u"), explicit_snap_dir)
    write_snapshot(_make_sch(sch_cv, w="1u"), snaps / "oagit_test" / "inv" / "alt_snap")
    resolved = cli_mod._resolve_snapshot_input(
        "oagit_test/inv/alt_snap", root=repo, snapshots_dir=snaps, prefer_cwd=False
    )
    assert resolved == explicit_snap_dir.resolve()

