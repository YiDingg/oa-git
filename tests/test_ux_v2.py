"""Tests for Git-like CLI UX, diff/merge formatting, status explanations, and list-valued symbol properties."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

import oa_git.cli as cli_mod
from oa_git.cli import main
from oa_git.diff import diff_schematics, format_diff_text
from oa_git.merge import MergeConflict, MergeResult, format_merge_conflicts, merge_schematics
from oa_git.model import (
    CONFIG_SCHEMA_V1,
    CONFIG_SCHEMA_V2,
    AnnotationShape,
    CdfParameterError,
    CellViewRef,
    Instance,
    InstancePlacement,
    LiveLockError,
    Net,
    NoteLabel,
    OmissionRecord,
    PinPlacement,
    Port,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    UnsupportedObjectError,
    WireSegment,
)
from oa_git.project_state import (
    IndexEntry,
    classify_cellview_state,
    default_source_file_candidates,
    explain_cellview_state,
    fingerprint_source_files,
    write_project_config,
)
from oa_git.snapshot import build_schematic_snapshot, read_snapshot, snapshot_dir_for, write_snapshot
from oa_git.symbol_diff import diff_symbols, format_symbol_diff_text
from oa_git.symbol_merge import (
    SymbolMergeConflict,
    SymbolMergeResult,
    _merge_mapping as symbol_merge_mapping,
    format_symbol_merge_conflicts,
    merge_symbols,
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


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_sch(
    cv: CellViewRef,
    *,
    w: str = "2u",
    r: int = 1,
    xy: tuple[float, float] = (1.0, 2.0),
    omissions: tuple[OmissionRecord, ...] = (),
) -> SchematicSnapshot:
    return build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"), Port("OUT", "output")),
            nets=(Net("IN"), Net("OUT")),
            instances=(
                Instance(
                    "R1",
                    "basic",
                    "res",
                    params={"w": w, "r": r},
                    param_types={"w": "string", "r": "int"},
                    connections={"p": "IN", "n": "OUT"},
                ),
            ),
        ),
        SchematicPlacement(
            instances=(InstancePlacement("R1", xy, "R0"),),
        ),
        omissions=omissions,
    )


def _make_sym(
    cv: CellViewRef,
    *,
    out_dir: str = "output",
    in_xy: tuple[float, float] = (0.0, 0.0),
    extra_shape: SymbolShape | None = None,
    properties: dict[str, Any] | None = None,
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
    return build_symbol_snapshot(
        cv,
        pins=pins,
        port_order=("IN", "OUT"),
        graphics=SymbolGraphics(
            shapes=tuple(shapes),
            labels=(SymbolLabel(text="IN", xy=(0.3, 0.0), layer="pin", purpose="label"),),
            selection_box=((-0.05, -0.6), (2.05, 0.6)),
            origin=(0.0, 0.0),
        ),
        properties=properties or {"partName": cv.cell},
    )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "ux@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "UX Test"], cwd=repo, check=True)


def _git_commit_all(repo: Path, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)


def _invoke_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], Any]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured


# ---------------------------------------------------------------------------
# 1. Native-Git `oagit diff` (0-arg, 1-arg rev / A..B / A.., 2-arg shorthand, multi-cv)
# ---------------------------------------------------------------------------


def test_native_git_diff_zero_one_two_args_and_multi_cellview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    cv_amp = CellViewRef("work", "amp", "schematic")
    cv_sym = CellViewRef("work", "amp", "symbol")
    cv_buf = CellViewRef("work", "buf", "schematic")

    # Commit 1: single schematic
    write_snapshot(_make_sch(cv_amp, w="2u"), snapshot_dir_for(repo / "snapshots", cv_amp))
    _git_commit_all(repo, "v1")

    monkeypatch.chdir(repo)

    # 0-arg diff with 1 clean cellview -> exact single-diff schema, exit 0
    code, payload, _ = _invoke_json(["diff", "--exit-code", "--json"], capsys)
    assert code == 0
    assert payload["ok"] is True
    assert "multi" not in payload
    assert payload["electrical"]["param_changes"] == []

    # Edit working snapshot -> 0-arg diff detects change with single-diff schema
    write_snapshot(_make_sch(cv_amp, w="4u"), snapshot_dir_for(repo / "snapshots", cv_amp))
    code, payload, _ = _invoke_json(["diff", "--exit-code", "--json"], capsys)
    assert code == 1
    assert "multi" not in payload
    assert payload["electrical"]["param_changes"][0]["new"] == "4u"

    # Commit 2: add symbol + second schematic (now 3 cellviews)
    write_symbol_snapshot(_make_sym(cv_sym), snapshot_dir_for(repo / "snapshots", cv_sym))
    write_snapshot(_make_sch(cv_buf, r=10), snapshot_dir_for(repo / "snapshots", cv_buf))
    _git_commit_all(repo, "v2")

    # 1-arg range HEAD~1..HEAD (multi-cellview: 1 modified, 2 added)
    code, multi_json, _ = _invoke_json(["diff", "HEAD~1..HEAD", "--exit-code", "--json"], capsys)
    assert code == 1
    assert multi_json["ok"] is True
    assert multi_json["multi"] is True
    assert multi_json["count"] == 3
    assert multi_json["changed_count"] == 3
    statuses = {item["key"]: item["status"] for item in multi_json["diffs"]}
    assert statuses == {
        cv_amp.key: "modified",
        cv_sym.key: "added",
        cv_buf.key: "added",
    }

    # Text mode for 0-arg diff when all 3 clean -> "No changes."
    assert main(["diff", "--exit-code"]) == 0
    assert capsys.readouterr().out.strip() == "No changes."

    # Modify amp, remove buf, keep sym clean -> 0-arg diff handles modified + removed + unchanged
    write_snapshot(_make_sch(cv_amp, w="8u"), snapshot_dir_for(repo / "snapshots", cv_amp))
    shutil.rmtree(snapshot_dir_for(repo / "snapshots", cv_buf))

    code, work_multi, _ = _invoke_json(["diff", "--exit-code", "--json"], capsys)
    assert code == 1
    assert work_multi["multi"] is True
    assert work_multi["count"] == 3
    assert work_multi["changed_count"] == 2
    work_statuses = {item["key"]: item["status"] for item in work_multi["diffs"]}
    assert work_statuses == {
        cv_amp.key: "modified",
        cv_sym.key: "unchanged",
        cv_buf.key: "removed",
    }

    # Text mode shows concise sections for modified and removed, omits unchanged
    assert main(["diff"]) == 0
    text = capsys.readouterr().out
    assert f"=== {cv_amp.key} [schematic] (modified) ===" in text
    assert f"=== {cv_buf.key} [schematic] (removed) ===" in text
    assert cv_sym.key not in text

    # 1-arg `HEAD~1..` (empty right side -> working) and 1-arg `HEAD~1` (rev vs working)
    code1, r_open, _ = _invoke_json(["diff", "HEAD~1..", "--json"], capsys)
    code2, r_rev, _ = _invoke_json(["diff", "HEAD~1", "--json"], capsys)
    assert code1 == 0 and code2 == 0
    assert r_open == r_rev
    assert r_open["multi"] is True
    assert {item["key"]: item["status"] for item in r_open["diffs"]} == {
        cv_amp.key: "modified",
        cv_sym.key: "added",
    }

    # 2-arg Git revs without --cellview auto-discovers all cellviews
    code, two_rev, _ = _invoke_json(["diff", "HEAD~1", "HEAD", "--json"], capsys)
    assert code == 0
    assert two_rev == multi_json

    # 2-arg cellview shorthand (`work/amp/schematic`) against Git rev and against local path
    code, shorthand_diff, _ = _invoke_json(["diff", "HEAD", cv_amp.key, "--exit-code", "--json"], capsys)
    assert code == 1
    assert "multi" not in shorthand_diff
    assert shorthand_diff["electrical"]["param_changes"][0]["new"] == "8u"

    other_dir = write_snapshot(_make_sch(cv_amp, w="16u"), tmp_path / "other_amp")
    code, shorthand_local, _ = _invoke_json(["diff", cv_amp.key, str(other_dir), "--json"], capsys)
    assert code == 0
    assert shorthand_local["electrical"]["param_changes"][0]["new"] == "16u"

    # 1-arg local path containing '..' (relative to subdir) is NOT split as Git range
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    local_copy = write_snapshot(_make_sch(cv_amp, w="32u"), repo / "local_amp_copy")
    assert local_copy.is_dir()
    code, rel_dotdot, _ = _invoke_json(["diff", "../local_amp_copy", "--exit-code", "--json"], capsys)
    assert code == 1
    assert rel_dotdot["electrical"]["param_changes"][0]["new"] == "32u"

    # >2 targets -> clean SchemaValidationError
    code, too_many, captured = _invoke_json(["diff", "HEAD~1", "HEAD", "extra", "--json"], capsys)
    assert code == 2
    assert too_many["error_type"] == "SchemaValidationError"
    assert captured.err == ""

    # Leading-dash injection in 1-arg or range is rejected with SchemaValidationError
    for bad_argv in (
        ["diff", "--json", "--", "-evil"],
        ["diff", "--json", "--", "-evil..HEAD"],
        ["diff", "HEAD..-evil", "--json"],
        ["diff", "HEAD~1...HEAD", "--json"],
    ):
        code, err_out, _ = _invoke_json(bad_argv, capsys)
        assert code == 2
        assert err_out["error_type"] == "SchemaValidationError"


# ---------------------------------------------------------------------------
# 2. Shorthand restore/merge, Smart 0-Arg capture, daemon default, flags & hints
# ---------------------------------------------------------------------------


def test_shorthand_restore_merge_zero_arg_capture_daemon_and_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    oa_work = project / "oa" / "work"
    fixture_dir = tmp_path / "fixtures"
    cv = CellViewRef("work", "amp", "schematic")
    target = CellViewRef("sandbox", "amp", "schematic")

    oa_file = oa_work / "amp" / "schematic" / "sch.oa"
    oa_file.parent.mkdir(parents=True)
    oa_file.write_bytes(b"OA_V1")
    write_snapshot(_make_sch(cv, w="2u"), fixture_dir / "work" / "amp" / "schematic")

    # Root-level --json + -C + --idle-timeout-s alias on init
    code, init_out, _ = _invoke_json(
        [
            "--json",
            "init",
            "-C",
            str(project),
            "--oa-lib",
            f"work={oa_work}",
            "--allow-library",
            "sandbox",
            "--backend",
            "fixture",
            "--idle-timeout-s",
            "77",
        ],
        capsys,
    )
    assert code == 0
    assert init_out["config"]["schema"] == CONFIG_SCHEMA_V2
    assert init_out["config"]["daemon"]["idle_timeout_s"] == 77

    # 0-arg capture before any tracked entries -> actionable SchemaValidationError
    code, empty_cap, _ = _invoke_json(["--json", "capture", "-C", str(project)], capsys)
    assert code == 2
    assert empty_cap["error_type"] == "SchemaValidationError"
    assert "<lib>/<cell>/<view>" in empty_cap["error"]

    # Capture with -o alias works
    custom_out = tmp_path / "custom_cap"
    code, cap_o, _ = _invoke_json(
        ["capture", cv.key, "-C", str(project), "--fixture", str(fixture_dir), "-o", str(custom_out), "--json"],
        capsys,
    )
    assert code == 0
    assert Path(cap_o["snapshot_dir"]) == custom_out.resolve()

    # Normal capture populates snapshots + index; check text summary line
    assert main(["capture", cv.key, "-C", str(project), "--fixture", str(fixture_dir)]) == 0
    cap_text = capsys.readouterr().out
    assert "Captured 1, skipped 0, failed 0 (transport=fixture)" in cap_text

    # Smart 0-arg capture when clean -> defaults to --changed, 0 backend calls!
    backend_calls: list[Any] = []
    real_backend = cli_mod._backend

    def spy_backend(settings, **kw):
        backend_calls.append(settings)
        return real_backend(settings, **kw)

    monkeypatch.setattr(cli_mod, "_backend", spy_backend)
    assert main(["capture", "-C", str(project), "--fixture", str(fixture_dir)]) == 0
    skip_text = capsys.readouterr().out
    assert "Captured 0, skipped 1, failed 0 (transport=index_skip)" in skip_text
    assert len(backend_calls) == 0

    # Modify OA file -> smart 0-arg capture re-captures dirty entry automatically!
    oa_file.write_bytes(b"OA_V2")
    write_snapshot(_make_sch(cv, w="6u"), fixture_dir / "work" / "amp" / "schematic")
    code, recapped, _ = _invoke_json(
        ["--json", "capture", "-C", str(project), "--fixture", str(fixture_dir)], capsys
    )
    assert code == 0
    assert len(backend_calls) == 1
    assert len(recapped["captured"]) == 1
    assert read_snapshot(snapshot_dir_for(project / "snapshots", cv)).electrical.instances[0].params["w"] == "6u"

    # Shorthand restore (`work/amp/schematic`) resolves via snapshots_dir
    code, res_out, _ = _invoke_json(
        [
            "--json",
            "restore",
            cv.key,
            "-C",
            str(project),
            "--fixture",
            str(fixture_dir),
            "--target",
            target.key,
        ],
        capsys,
    )
    assert code == 0
    assert res_out["published"] is True

    # Merge respects -C/--root AND accepts <lib>/<cell>/<view> shorthand!
    ours_rel = project / "ours_snap"
    theirs_rel = project / "theirs_snap"
    write_snapshot(_make_sch(cv, w="6u", xy=(5.0, 6.0)), ours_rel)
    write_snapshot(_make_sch(cv, w="6u", r=9), theirs_rel)
    code, m_out, _ = _invoke_json(
        [
            "--json",
            "merge",
            cv.key,
            "ours_snap",
            "theirs_snap",
            "-o",
            "merged_out",
            "-C",
            str(project),
        ],
        capsys,
    )
    assert code == 0
    assert m_out["ok"] is True
    merged = read_snapshot(project / "merged_out")
    assert merged.electrical.instances[0].params["r"] == 9
    assert merged.placement.instances[0].xy == (5.0, 6.0)

    # `status --oa-lib` alias works
    code, st_alias, _ = _invoke_json(
        ["--json", "status", "-C", str(project), "--oa-lib", f"work={oa_work}"], capsys
    )
    assert code == 0
    assert st_alias["ok"] is True

    # `oagit daemon` with omitted action defaults to status
    daemon_proj = tmp_path / "daemon_proj"
    cds = daemon_proj / "cds.lib"
    daemon_proj.mkdir()
    cds.write_text("", encoding="utf-8")
    write_project_config(
        daemon_proj,
        {"cds_lib": "cds.lib", "backend": "virtuoso", "daemon": {"enabled": True}},
    )
    code, d_out, _ = _invoke_json(["--json", "-C", str(daemon_proj), "daemon"], capsys)
    assert code == 0
    assert d_out["ok"] is True
    assert d_out["state"] == "stopped"


def test_help_for_all_subcommands_is_crisp(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        ["--help"],
        ["init", "--help"],
        ["status", "--help"],
        ["capture", "--help"],
        ["restore", "--help"],
        ["diff", "--help"],
        ["merge", "--help"],
        ["daemon", "--help"],
    ):
        code = main(argv)
        out = capsys.readouterr().out
        assert code == 0
        assert "usage:" in out.lower()


def test_actionable_error_hints_in_text_and_json_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) DisallowedLibraryError + existing target without --overwrite via real CLI restore
    project = tmp_path / "err_proj"
    fixture_dir = tmp_path / "err_fix"
    cv = CellViewRef("work", "amp", "schematic")
    snap_dir = write_snapshot(_make_sch(cv), project / "snapshots" / "work" / "amp" / "schematic")
    write_snapshot(_make_sch(CellViewRef("sandbox", "amp", "schematic")), fixture_dir / "sandbox" / "amp" / "schematic")
    write_project_config(project, {"allow_libraries": ["sandbox"], "backend": "fixture"})

    # Text mode DisallowedLibraryError -> Hint on stderr
    code = main(["restore", str(snap_dir), "-C", str(project), "--fixture", str(fixture_dir), "--target", "forbidden/amp/schematic"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Hint:" in captured.err
    assert "--allow-library forbidden" in captured.err
    assert ".oagit/config.json" in captured.err

    # JSON mode DisallowedLibraryError -> hint field, empty stderr
    code, err_json, captured = _invoke_json(
        ["--json", "restore", str(snap_dir), "-C", str(project), "--fixture", str(fixture_dir), "--target", "forbidden/amp/schematic"],
        capsys,
    )
    assert code == 2
    assert err_json["ok"] is False
    assert err_json["error_type"] == "DisallowedLibraryError"
    assert "--allow-library forbidden" in err_json["hint"]
    assert captured.err == ""

    # Existing target without --overwrite -> hint suggests --overwrite
    code, err_ow, captured = _invoke_json(
        ["--json", "restore", str(snap_dir), "-C", str(project), "--fixture", str(fixture_dir), "--target", "sandbox/amp/schematic"],
        capsys,
    )
    assert code == 2
    assert "--overwrite" in err_ow["hint"]
    assert captured.err == ""

    # Missing config -> hint suggests oagit init
    empty_dir = tmp_path / "no_cfg"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    code, err_cfg, captured = _invoke_json(["--json", "status"], capsys)
    assert code == 2
    assert "oagit init" in err_cfg["hint"]
    assert captured.err == ""

    # Direct hint checks for LiveLockError, UnsupportedObjectError, CdfParameterError
    for exc, expected_token in (
        (LiveLockError("active .cdslck found"), "Virtuoso"),
        (UnsupportedObjectError("omissions present"), "--allow-omissions"),
        (CdfParameterError("cdf_readback_mismatch"), "--recompute-derived"),
        (CdfParameterError("cdf_order_cycle"), "coupling_rules"),
    ):
        cli_mod._emit_error(exc, json_mode=True)
        out_json = json.loads(capsys.readouterr().out)
        assert expected_token in out_json["hint"]
        cli_mod._emit_error(exc, json_mode=False)
        err_text = capsys.readouterr().err
        assert "Hint:" in err_text and expected_token in err_text


# ---------------------------------------------------------------------------
# 3. explain_cellview_state, Scannable Status & v1 init tip
# ---------------------------------------------------------------------------


def test_explain_cellview_state_and_scannable_status_and_init_tip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "st_proj"
    oa_work = project / "oa" / "work"
    snapshots_dir = project / "snapshots"

    cv_clean = CellViewRef("work", "clean_blk", "schematic")
    cv_dirty = CellViewRef("work", "dirty_blk", "schematic")
    cv_missing_snap = CellViewRef("work", "no_snap", "schematic")
    cv_corrupt = CellViewRef("work", "corrupt_blk", "schematic")
    cv_missing_oa = CellViewRef("work", "missing_oa", "schematic")

    entries: dict[str, IndexEntry] = {}
    for cv in (cv_clean, cv_dirty, cv_corrupt):
        oa_file = oa_work / cv.cell / "schematic" / "sch.oa"
        oa_file.parent.mkdir(parents=True, exist_ok=True)
        oa_file.write_bytes(b"OA_INIT")
        om = (OmissionRecord("shape", "s1", "unsupported"),) if cv == cv_clean else ()
        snap = _make_sch(cv, omissions=om)
        write_snapshot(snap, snapshot_dir_for(snapshots_dir, cv))
        fps = fingerprint_source_files(oa_work, default_source_file_candidates(cv))
        entries[cv.key] = IndexEntry(
            cellview=cv,
            source_files=fps,
            snapshot=f"snapshots/{cv.key}",
            snapshot_full_sha256=snap.manifest.full_sha256,
            state="clean",
        )

    # Create OA file for no_snap without snapshot
    no_snap_oa = oa_work / cv_missing_snap.cell / "schematic" / "sch.oa"
    no_snap_oa.parent.mkdir(parents=True)
    no_snap_oa.write_bytes(b"OA_NEW")

    # Modify dirty_blk OA source
    (oa_work / cv_dirty.cell / "schematic" / "sch.oa").write_bytes(b"OA_EDITED")

    # Corrupt corrupt_blk snapshot
    corrupt_circuit = snapshot_dir_for(snapshots_dir, cv_corrupt) / "circuit.yaml"
    corrupt_circuit.write_text(corrupt_circuit.read_text(encoding="utf-8") + "# tamper\n", encoding="utf-8")

    # Verify explain_cellview_state matches classify_cellview_state and reports crisp reasons
    oa_libs = {"work": oa_work}
    cases = [
        (cv_clean, oa_libs, "clean", "clean"),
        (cv_dirty, oa_libs, "dirty", "source modified"),
        (cv_missing_snap, oa_libs, "dirty", "snapshot missing"),
        (cv_corrupt, oa_libs, "dirty", "snapshot corrupted"),
        (cv_missing_oa, oa_libs, "missing", "primary OA file missing"),
        (cv_clean, {}, "unknown", "oa_libs not configured"),
    ]
    for cv, libs, exp_state, exp_reason in cases:
        c_state, _ = classify_cellview_state(
            cv, oa_libs=libs, snapshots_dir=snapshots_dir, index_entries=entries
        )
        e_state, reason = explain_cellview_state(
            cv, oa_libs=libs, snapshots_dir=snapshots_dir, index_entries=entries
        )
        assert c_state == exp_state
        assert e_state == exp_state
        assert reason == exp_reason

    # Verify scannable CLI status text output
    write_project_config(project, {"oa_libs": {"work": str(oa_work)}, "backend": "fixture"})
    from oa_git.project_state import save_index

    save_index(project, entries)
    code = main(["status", "-C", str(project)])
    st_text = capsys.readouterr().out
    assert code == 1  # corrupt_blk fails integrity
    assert "Broken snapshots (1):" in st_text
    assert "Modified / Dirty" in st_text
    assert "source modified" in st_text
    assert "Clean (1):" in st_text
    assert "omissions=1" in st_text  # clean_blk has 1 omission -> shown
    assert "omissions=0" not in st_text  # healthy views hide omissions=0
    assert str(project) not in st_text  # compact keys, not full absolute paths
    assert "Hint: run 'oagit capture --changed'" in st_text

    # v1 init text tip mentions --v2 without altering v1 JSON schema
    v1_proj = tmp_path / "v1_proj"
    assert main(["init", "-C", str(v1_proj), "--v1", "--backend", "fixture"]) == 0
    v1_text = capsys.readouterr().out
    assert "--v2" in v1_text
    v1_json_proj = tmp_path / "v1_json_proj"
    code, v1_payload, _ = _invoke_json(["init", "-C", str(v1_json_proj), "--v1", "--backend", "fixture", "--json"], capsys)
    assert code == 0
    assert v1_payload["config"]["schema"] == CONFIG_SCHEMA_V1


# ---------------------------------------------------------------------------
# 4. Clean Domain Formatting & List-Valued Symbol Properties
# ---------------------------------------------------------------------------


def test_schematic_diff_and_merge_clean_formatting() -> None:
    cv = CellViewRef("work", "amp", "schematic")
    old = build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"),),
            nets=(Net("IN"), Net("OLD_NET")),
            instances=(
                Instance("M1", "pdk", "nch", "symbol", connections={"G": "IN"}, params={"w": "1u"}, param_types={"w": "string"}),
                Instance("M_OLD", "pdk", "pch", "symbol"),
            ),
        ),
        SchematicPlacement(
            instances=(
                InstancePlacement("M1", (0.0, 0.0), "R0"),
                InstancePlacement("M_OLD", (2.0, 2.0), "R0"),
            ),
            pins=(PinPlacement("IN", "IN", "input", "basic", "ipin", "symbol", (0.0, 0.0), "R0"),),
            wires=(WireSegment(((0.0, 0.0), (1.0, 0.0)), net="IN"),),
            labels=(NoteLabel("old_note", (0.0, 1.0)),),
            annotations=(AnnotationShape("rect", "text", "drawing", ((0.0, 0.0), (1.0, 1.0))),),
        ),
    )
    new = build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("IN", "input"),),
            nets=(Net("IN"), Net("NEW_NET", is_global=True, num_bits=4)),
            instances=(
                Instance("M1", "pdk2", "nch_lvt", "symbol", connections={"G": "IN"}, params={"w": "2u"}, param_types={"w": "string"}),
                Instance("M_NEW", "pdk", "pch", "symbol"),
            ),
        ),
        SchematicPlacement(
            instances=(
                InstancePlacement("M1", (3.0, 4.0), "R90"),
                InstancePlacement("M_NEW", (5.0, 6.0), "R0"),
            ),
            pins=(PinPlacement("IN", "IN", "input", "basic", "ipin", "symbol", (0.5, 0.0), "R0"),),
            wires=(WireSegment(((0.0, 0.0), (2.0, 0.0)), net="IN"),),
            labels=(NoteLabel("new_note", (1.0, 1.0)),),
            annotations=(AnnotationShape("line", "annotate", "drawing", ((0.0, 0.0), (2.0, 2.0))),),
        ),
    )
    diff = diff_schematics(old, new)
    text = format_diff_text(diff)
    assert "M1 pdk/nch/symbol -> pdk2/nch_lvt/symbol" in text
    assert "None ->" not in text
    assert "-> None" not in text
    assert "{'points':" not in text
    assert "{'text':" not in text
    assert "wire net=IN [(0, 0) -> (2, 0)]" in text
    assert "label 'new_note' at (1, 1)" in text
    assert "line annotate:drawing [(0, 0), (2, 2)]" in text
    assert "(moved)" in text

    # Multiset conflict path uses readable object identifier (no raw _stable_key tuple)
    w_dup = WireSegment(((0.0, 0.0), (1.0, 0.0)), net="IN")
    base_m = build_schematic_snapshot(cv, SchematicElectrical(), SchematicPlacement(wires=(w_dup,)))
    ours_m = build_schematic_snapshot(cv, SchematicElectrical(), SchematicPlacement(wires=(w_dup, w_dup)))
    theirs_m = build_schematic_snapshot(cv, SchematicElectrical(), SchematicPlacement(wires=()))
    res = merge_schematics(base_m, ours_m, theirs_m)
    assert res.ok is False
    assert len(res.conflicts) == 1
    assert "dataclass" not in res.conflicts[0].path
    assert "wire(net=IN, (0, 0)->(1, 0))" in res.conflicts[0].path
    formatted = format_merge_conflicts(res)
    assert "Placement conflicts (1):" in formatted
    assert "[placement]" in formatted
    assert "base=1, ours=2, theirs=0" in formatted
    assert format_merge_conflicts(MergeResult(True, None, ())) == "No merge conflicts."


def test_symbol_diff_and_merge_clean_formatting_and_list_valued_properties() -> None:
    cv = CellViewRef("work", "opamp", "symbol")
    base = _make_sym(cv)
    # Construct snapshots directly with ILList-like list-valued properties (e.g. termOrder)
    s_old = replace(base, properties={"partName": "opamp", "termOrder": ["IN", "OUT"]})
    s_new = replace(
        _make_sym(
            cv,
            extra_shape=SymbolShape(kind="line", layer="device", purpose="drawing", points=((0.0, 0.0), (0.5, 0.0))),
        ),
        properties={"partName": "opamp", "termOrder": ["OUT", "IN"], "extraList": [1, 2]},
    )

    sym_diff = diff_symbols(s_old, s_new)
    assert sym_diff.has_pin_contract_changes is True
    assert sym_diff.has_graphics_changes is True
    prop_map = {p.key: p for p in sym_diff.pin_contract_changes.property_changes}
    assert prop_map["termOrder"].old == ["IN", "OUT"]
    assert prop_map["termOrder"].new == ["OUT", "IN"]
    assert prop_map["extraList"].change_type == "added"
    # JSON serializable & clean formatted text (no raw dict repr for shapes)
    json.dumps(sym_diff.to_dict())
    text = format_symbol_diff_text(sym_diff)
    assert "Graphics Changes:" in text
    assert "+ shape line device:drawing [(0, 0) -> (0.5, 0)]" in text
    assert "{'kind':" not in text
    assert "~ property termOrder: ['IN', 'OUT'] -> ['OUT', 'IN']" in text
    assert "+ property extraList = [1, 2]" in text

    # Full end-to-end build_symbol_snapshot + merge_symbols with ILList list-valued properties
    b_list = _make_sym(cv, properties={"partName": "opamp", "termOrder": ["IN", "OUT"]})
    o_list = _make_sym(cv, properties={"partName": "opamp", "termOrder": ["IN", "OUT", "CLK"]})
    t_list = _make_sym(cv, properties={"partName": "opamp", "termOrder": ["IN", "OUT"], "vendor": "TSMC"})
    m_clean = merge_symbols(b_list, o_list, t_list)
    assert m_clean.ok is True
    assert m_clean.snapshot is not None
    assert m_clean.snapshot.properties == {
        "partName": "opamp",
        "termOrder": ["IN", "OUT", "CLK"],
        "vendor": "TSMC",
    }

    t_conflict = _make_sym(cv, properties={"partName": "opamp", "termOrder": ["OUT", "IN"]})
    m_div = merge_symbols(b_list, o_list, t_conflict)
    assert m_div.ok is False
    assert m_div.snapshot is None
    assert len(m_div.conflicts) == 1
    assert m_div.conflicts[0].path == "properties.termOrder"
    json.dumps(m_div.to_dict())
    div_text = format_symbol_merge_conflicts(m_div)
    assert "Pin contract conflicts (1):" in div_text
    assert "[pin_contract] properties.termOrder" in div_text
    assert "base=['IN', 'OUT']" in div_text

    # List-valued properties in symbol_merge._merge_mapping never alias input lists
    clean_conflicts: list[SymbolMergeConflict] = []
    ours_props = {"termOrder": ["IN", "OUT", "CLK"], "partName": "opamp"}
    theirs_props = {"termOrder": ["IN", "OUT"], "partName": "opamp", "vendor": "TSMC"}
    merged_props = symbol_merge_mapping(
        {"termOrder": ["IN", "OUT"], "partName": "opamp"},
        ours_props,
        theirs_props,
        domain="pin_contract",
        path_prefix="properties",
        conflicts=clean_conflicts,
    )
    assert clean_conflicts == []
    assert merged_props["termOrder"] is not ours_props["termOrder"]

    # Symbol multiset conflict path uses readable shape identifier
    dup_line = SymbolShape(kind="line", layer="device", purpose="drawing", points=((0.0, 0.0), (1.0, 0.0)))
    b_sym = _make_sym(cv, extra_shape=dup_line)
    o_sym = build_symbol_snapshot(
        cv,
        pins=b_sym.pins,
        port_order=b_sym.port_order,
        graphics=replace(b_sym.graphics, shapes=b_sym.graphics.shapes + (dup_line,)),
        properties=b_sym.properties,
    )
    t_sym = build_symbol_snapshot(
        cv,
        pins=b_sym.pins,
        port_order=b_sym.port_order,
        graphics=replace(b_sym.graphics, shapes=tuple(s for s in b_sym.graphics.shapes if s != dup_line)),
        properties=b_sym.properties,
    )
    m_sym = merge_symbols(b_sym, o_sym, t_sym)
    assert m_sym.ok is False
    assert any("line(device:drawing, (0, 0)->(1, 0))" in c.path for c in m_sym.conflicts)
    fmt_sym = format_symbol_merge_conflicts(m_sym)
    assert "Graphics conflicts (1):" in fmt_sym
    assert "[graphics]" in fmt_sym


def test_fmt_wire_omits_default_zero_width_and_formats_wide_bus_wire() -> None:
    from oa_git.diff import _fmt_wire

    narrow = WireSegment(points=((0.0, 0.0), (2.0, 0.0)), net="IN", width=0.0)
    wide = WireSegment(points=((0.0, 0.0), (2.0, 0.0)), net="DATA<15:0>", width=0.0625)
    assert _fmt_wire(narrow) == "wire net=IN [(0, 0) -> (2, 0)]"
    assert "(width=0.0625)" in _fmt_wire(wide)


def test_one_arg_diff_accepts_cellview_shorthand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "shorthand_diff_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@oagit.local"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)

    code, _, _ = _invoke_json(["init", "--v2", "--backend", "fixture", "--json"], capsys)
    assert code == 0

    cv = CellViewRef("work", "amp", "schematic")
    snap_dir = snapshot_dir_for(repo / "snapshots", cv)
    write_snapshot(_make_sch(cv, w="2u"), snap_dir)
    subprocess.run(["git", "add", "snapshots/"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    write_snapshot(_make_sch(cv, w="4u"), snap_dir)
    code, data, _ = _invoke_json(["diff", "work/amp/schematic", "--json"], capsys)
    assert code == 0
    assert data["ok"] is True
    assert any(c["param"] == "w" and c["new"] == "4u" for c in data["electrical"]["param_changes"])
