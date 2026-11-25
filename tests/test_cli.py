from __future__ import annotations

from contextlib import ExitStack
import json
from dataclasses import replace
from pathlib import Path
import subprocess
import pytest

from oa_git.cli import _git_snapshot, main
from oa_git.model import (
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    Port,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
)
from oa_git.snapshot import build_schematic_snapshot, read_snapshot, write_snapshot


CELLVIEW = CellViewRef("work", "amp", "schematic")
TARGET = CellViewRef("sandbox", "amp", "schematic")


def make_snapshot(
    cellview: CellViewRef = CELLVIEW,
    *,
    width: int = 1,
    resistance: int = 1,
):
    return build_schematic_snapshot(
        cellview,
        SchematicElectrical(
            ports=(Port("in", "input"), Port("out", "output")),
            nets=(Net("in"), Net("out")),
            instances=(
                Instance(
                    "R1",
                    "basic",
                    "res",
                    params={"w": width, "r": resistance},
                    param_types={"w": "int", "r": "int"},
                    connections={"p": "in", "n": "out"},
                ),
            ),
        ),
        SchematicPlacement(
            instances=(InstancePlacement("R1", (1.0 + width, 2.0)),),
        ),
    )


def invoke_json(args: list[str], capsys):
    code = main([*args, "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    return code, payload, captured


def test_console_entrypoint_help_and_status_integrity_and_locks(tmp_path: Path, capsys) -> None:
    repo = Path(__file__).resolve().parents[1]
    help_result = subprocess.run(
        [str(repo / ".venv/bin/oagit"), "--help"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert "usage:" in help_result.stdout.lower()

    root = tmp_path / "project"
    code, payload, _ = invoke_json(
        [
            "init",
            "--root",
            str(root),
            "--v1",
            "--backend",
            "fixture",
            "--allow-library",
            "sandbox",
        ],
        capsys,
    )
    assert code == 0
    assert payload["config"]["schema"] == "oa-git-config/1"
    snapshot_dir = root / "snapshots" / "work" / "amp" / "schematic"
    write_snapshot(make_snapshot(), snapshot_dir)

    code, payload, _ = invoke_json(["status", "--root", str(root)], capsys)
    assert code == 0
    assert payload["ok"] is True
    assert payload["snapshots"][0]["omissions_count"] == 0

    circuit = snapshot_dir / "circuit.yaml"
    circuit.write_text(circuit.read_text(encoding="utf-8") + "# tamper\n", encoding="utf-8")
    code, payload, _ = invoke_json(["status", "--root", str(root)], capsys)
    assert code == 1
    assert payload["ok"] is False
    assert payload["snapshots"][0]["error_type"] == "SnapshotIntegrityError"

    write_snapshot(make_snapshot(), snapshot_dir)
    oa_library = tmp_path / "oa-lib"
    oa_library.mkdir()
    (oa_library / "amp.cdslck").write_text("lock", encoding="utf-8")
    code, payload, _ = invoke_json(
        ["status", "--root", str(root), "--oa-lib-map", f"sandbox={oa_library}"],
        capsys,
    )
    assert code == 1
    assert payload["locks"][0]["blocking"] is True

    code, error, captured = invoke_json(["init", "--root", str(root)], capsys)
    assert code == 2
    assert error["error_type"] == "SchemaValidationError"
    assert captured.err == ""
    assert main(["init", "--root", str(root), "--force"]) == 0
    capsys.readouterr()


def test_capture_restore_fixture_and_fail_closed_errors(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    fixture_root = tmp_path / "fixture"
    write_snapshot(make_snapshot(), fixture_root / "work" / "amp" / "schematic")
    assert main(
        [
            "init",
            "--root",
            str(root),
            "--backend",
            "fixture",
            "--allow-library",
            "sandbox",
        ]
    ) == 0
    capsys.readouterr()

    code, capture, _ = invoke_json(
        [
            "capture",
            CELLVIEW.key,
            "--root",
            str(root),
            "--fixture",
            str(fixture_root),
        ],
        capsys,
    )
    assert code == 0
    assert capture["cellview"] == CELLVIEW.to_dict()
    captured_snapshot = Path(capture["snapshot_dir"])
    assert read_snapshot(captured_snapshot).manifest.full_sha256 == capture["full_sha256"]

    code, restore, _ = invoke_json(
        [
            "restore",
            str(captured_snapshot),
            "--root",
            str(root),
            "--fixture",
            str(fixture_root),
            "--target",
            TARGET.key,
            "--allow-library",
            TARGET.library,
        ],
        capsys,
    )
    assert code == 0
    assert restore["ok"] is True
    assert restore["target"] == TARGET.to_dict()
    assert restore["readback_verified"] is True

    code, error, captured = invoke_json(
        [
            "restore",
            str(captured_snapshot),
            "--root",
            str(root),
            "--fixture",
            str(fixture_root),
            "--target",
            "other/amp/schematic",
        ],
        capsys,
    )
    assert code == 2
    assert error["ok"] is False
    assert error["error_type"] == "DisallowedLibraryError"
    assert captured.err == ""

    target_fixture = fixture_root / "sandbox" / "amp" / "schematic"
    write_snapshot(make_snapshot(TARGET), target_fixture)
    code, error, _ = invoke_json(
        [
            "restore",
            str(captured_snapshot),
            "--root",
            str(root),
            "--fixture",
            str(fixture_root),
            "--target",
            TARGET.key,
            "--allow-library",
            TARGET.library,
        ],
        capsys,
    )
    assert code == 2
    assert error["error_type"] == "BridgeExecutionError"


def test_diff_local_electrical_placement_exit_code_and_json(tmp_path: Path, capsys) -> None:
    old_dir = write_snapshot(make_snapshot(), tmp_path / "old")
    placement_new = replace(make_snapshot().placement, instances=(InstancePlacement("R1", (99.0, 2.0)),))
    placement_dir = write_snapshot(
        build_schematic_snapshot(CELLVIEW, make_snapshot().electrical, placement_new),
        tmp_path / "placement",
    )

    assert main(["diff", str(old_dir), str(placement_dir), "--exit-code"]) == 1
    assert main(["diff", str(old_dir), str(placement_dir), "--electrical-only", "--exit-code"]) == 0
    capsys.readouterr()
    code, payload, _ = invoke_json(
        ["diff", str(old_dir), str(placement_dir), "--electrical-only"], capsys
    )
    assert code == 0
    assert payload["ok"] is True
    assert "placement" not in payload

    electrical_dir = write_snapshot(make_snapshot(width=2), tmp_path / "electrical")
    assert main(["diff", str(old_dir), str(electrical_dir), "--electrical-only", "--exit-code"]) == 1
    assert main(["diff", str(old_dir), str(electrical_dir)]) == 0
    capsys.readouterr()


def test_diff_git_revisions_and_inferred_cellview(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=repo, check=True)
    tracked_dir = repo / "snapshots" / "work" / "amp" / "schematic"
    write_snapshot(make_snapshot(), tracked_dir)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=repo, check=True)
    write_snapshot(make_snapshot(width=2), tracked_dir)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v2"], cwd=repo, check=True)

    code = main(
        [
            "diff",
            "HEAD~1",
            "HEAD",
            "--root",
            str(repo),
            "--cellview",
            CELLVIEW.key,
            "--exit-code",
        ]
    )
    assert code == 1
    capsys.readouterr()

    local_v1 = write_snapshot(make_snapshot(), tmp_path / "local-v1")
    code = main(
        ["diff", "HEAD", str(local_v1), "--root", str(repo), "--exit-code"]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "work/amp/schematic" in captured.out


def test_merge_clean_output_and_conflict_json(tmp_path: Path, capsys) -> None:
    base = make_snapshot()
    ours = build_schematic_snapshot(
        CELLVIEW,
        base.electrical,
        replace(base.placement, instances=(InstancePlacement("R1", (10.0, 2.0)),)),
    )
    theirs = make_snapshot(width=1, resistance=2)
    base_dir = write_snapshot(base, tmp_path / "base")
    ours_dir = write_snapshot(ours, tmp_path / "ours")
    theirs_dir = write_snapshot(theirs, tmp_path / "theirs")
    out_dir = tmp_path / "merged"

    code, payload, _ = invoke_json(
        ["merge", str(base_dir), str(ours_dir), str(theirs_dir), "-o", str(out_dir)], capsys
    )
    assert code == 0
    assert payload["ok"] is True
    assert out_dir.is_dir()
    assert read_snapshot(out_dir).placement.instances[0].xy == (10.0, 2.0)
    assert read_snapshot(out_dir).electrical.instances[0].params["r"] == 2

    conflict_ours = write_snapshot(make_snapshot(width=2), tmp_path / "conflict-ours")
    conflict_theirs = write_snapshot(make_snapshot(width=3), tmp_path / "conflict-theirs")
    conflict_out = tmp_path / "conflict-out"
    code, payload, _ = invoke_json(
        [
            "merge",
            str(base_dir),
            str(conflict_ours),
            str(conflict_theirs),
            "--out",
            str(conflict_out),
        ],
        capsys,
    )
    assert code == 1
    assert payload["ok"] is False
    assert payload["conflicts"]
    assert not conflict_out.exists()


def test_merge_prints_success_only_after_write_succeeds(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_dir = write_snapshot(make_snapshot(), tmp_path / "base")
    ours_dir = write_snapshot(make_snapshot(), tmp_path / "ours")
    theirs_dir = write_snapshot(make_snapshot(), tmp_path / "theirs")

    def fail_write(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr("oa_git.cli.write_snapshot", fail_write)
    code, payload, captured = invoke_json(
        ["merge", str(base_dir), str(ours_dir), str(theirs_dir), "-o", str(tmp_path / "out")],
        capsys,
    )

    assert code == 2
    assert payload["ok"] is False
    assert "injected write failure" in payload["error"]
    assert captured.err == ""


def test_git_revision_option_injection_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with ExitStack() as stack:
        with pytest.raises(SchemaValidationError):
            _git_snapshot(
                repo,
                repo / "snapshots",
                "--output=/tmp/evil",
                CELLVIEW,
                stack,
            )


def test_diff_discovers_git_root_from_subdirectory(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "CLI Test"], cwd=repo, check=True)
    tracked_dir = repo / "snapshots" / "work" / "amp" / "schematic"
    write_snapshot(make_snapshot(), tracked_dir)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=repo, check=True)
    write_snapshot(make_snapshot(width=2), tracked_dir)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v2"], cwd=repo, check=True)
    subdirectory = repo / "subdirectory"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)

    code, payload, _ = invoke_json(
        ["diff", "HEAD~1", "HEAD", "--cellview", CELLVIEW.key, "--exit-code"],
        capsys,
    )

    assert code == 1
    assert payload["ok"] is True
    assert payload["electrical"]["param_changes"]
