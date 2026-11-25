"""Tests for oa-git v0.3 zero-config workspace onboarding, cds.lib discovery, and Git hygiene."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

import pytest

from oa_git.cli import main
from oa_git.model import (
    CONFIG_SCHEMA_V1,
    CONFIG_SCHEMA_V2,
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    Port,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
)
from oa_git.project_state import load_index, load_project_config, write_project_config
from oa_git.snapshot import build_schematic_snapshot, snapshot_dir_for, write_snapshot
from oa_git.symbol_model import SymbolGraphics, SymbolPin, SymbolPinFigure
from oa_git.symbol_snapshot import build_symbol_snapshot, write_symbol_snapshot
from oa_git.workspace import (
    BUILTIN_CADENCE_LIBS,
    MANAGED_GITIGNORE_END,
    MANAGED_GITIGNORE_START,
    check_gitignore_health,
    detect_virtuoso_cmd,
    discover_workspace_libraries,
    ensure_git_repo,
    is_inside_git_repo,
    parse_cds_lib_statements,
    render_managed_gitignore_block,
    update_managed_gitignore,
)


def _make_sch(cv: CellViewRef, *, w: str = "2u"):
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
                    params={"w": w},
                    param_types={"w": "string"},
                    connections={"p": "IN", "n": "OUT"},
                ),
            ),
        ),
        SchematicPlacement(instances=(InstancePlacement("R1", (1.0, 2.0), "R0"),)),
    )


def _invoke_json(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any], Any]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# 1. cds.lib parsing & portable workspace library classification
# ---------------------------------------------------------------------------


def test_parse_cds_lib_statements_comments_quotes_includes_and_assigns(
    tmp_path: Path,
) -> None:
    included = tmp_path / "included.lib"
    included.write_text("DEFINE should_not_be_followed ./included_lib\n", encoding="utf-8")

    cds_lib = tmp_path / "cds.lib"
    cds_lib.write_text(
        "\n".join(
            [
                "# hash full-line comment",
                "-- dash full-line comment",
                "",
                f"INCLUDE {included}",
                f"SOFTINCLUDE {included}",
                "DEFINE work ./work -- trailing dash comment",
                'DEFINE spaced_lib "./my lib#1--v2" # trailing hash comment',
                "DEFINE single_quoted './single # -- lib' -- comment",
                "define lower_case_lib ./lower",
                "ASSIGN hidden_lib DISPLAY Invisible -- hidden in library manager",
                "ASSIGN visible_lib DISPLAY Visible",
                "ASSIGN DesignGroup COMBINE work spaced_lib",
                "DEFINE DesignGroup ./fake_container_dir",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    defines, invisible_libs, combined_containers = parse_cds_lib_statements(cds_lib)
    assert defines == [
        ("work", "./work"),
        ("spaced_lib", "./my lib#1--v2"),
        ("single_quoted", "./single # -- lib"),
        ("lower_case_lib", "./lower"),
        ("DesignGroup", "./fake_container_dir"),
    ]
    assert "should_not_be_followed" not in {name for name, _ in defines}
    assert invisible_libs == {"hidden_lib"}
    assert combined_containers == {"DesignGroup"}


def test_discover_workspace_libraries_classification_and_last_define_wins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    # Empty discovery when cds.lib missing
    empty = discover_workspace_libraries(root, env={})
    assert empty.cds_lib is None
    assert empty.entries == ()
    assert empty.oa_libs == {}
    assert empty.allow_libraries == ()
    assert empty.repo_local_ignore_paths == ()
    assert empty.excluded == ()

    # Directories inside workspace
    (root / "analog_top").mkdir()
    (root / "oa" / "sub_blk").mkdir(parents=True)
    (root / "old_dup").mkdir()
    (root / "new_dup").mkdir()
    (root / "local_with_tech").mkdir()
    (root / "local_with_tech" / "tech.db").write_bytes(b"LOCAL_TECH")
    (root / "hidden_local").mkdir()
    (root / "combined_dir").mkdir()
    (root / "ro_local").mkdir()
    (root / "ro_local").chmod(0o555)
    (root / "snapshots" / "snap_lib").mkdir(parents=True)
    (root / ".oagit" / "internal_lib").mkdir(parents=True)

    # External directories
    ext_root = tmp_path / "external"
    ext_design = ext_root / "shared_ip"
    ext_design.mkdir(parents=True)
    ext_ro = ext_root / "readonly_lib"
    ext_ro.mkdir(parents=True)
    ext_ro.chmod(0o555)
    ext_pdk_tech = ext_root / "pdk_tech"
    ext_pdk_tech.mkdir(parents=True)
    (ext_pdk_tech / "tech.db").write_bytes(b"TECH")
    ext_pdk_init = ext_root / "pdk_init"
    ext_pdk_init.mkdir(parents=True)
    (ext_pdk_init / "libInit.il").write_text("; pdk init\n", encoding="utf-8")
    ext_pdk_cxt = ext_root / "pdk_cxt"
    ext_pdk_cxt.mkdir(parents=True)
    (ext_pdk_cxt / "libInit.cxt").write_bytes(b"CXT")

    cdshome = tmp_path / "eda_install"
    cds_builtin = cdshome / "tools" / "dfII" / "etc" / "cdslib" / "analogLib"
    cds_builtin.mkdir(parents=True)
    cds_other = cdshome / "share" / "vendor_lib"
    cds_other.mkdir(parents=True)

    cds_lib = root / "cds.lib"
    cds_lib.write_text(
        "\n".join(
            [
                "DEFINE analogLib $CDSHOME/tools/dfII/etc/cdslib/analogLib",
                "DEFINE basic ./analog_top",
                "DEFINE vendor_eda ${CDSHOME}/share/vendor_lib",
                "DEFINE unresolved_pdk $UNSET_PDK/lib1",
                "DEFINE sys_opt /opt/vendor/pdk_lib",
                "DEFINE missing_lib ./does_not_exist",
                "DEFINE hidden_local ./hidden_local",
                "ASSIGN hidden_local DISPLAY Invisible",
                "DEFINE combined_dir ./combined_dir",
                "ASSIGN combined_dir COMBINE analog_top",
                "DEFINE inside_snapshots ./snapshots/snap_lib",
                "DEFINE inside_oagit ./.oagit/internal_lib",
                "DEFINE ro_local ./ro_local",
                "DEFINE analog_top ./analog_top",
                "DEFINE sub_blk ./oa/sub_blk",
                "DEFINE local_with_tech ./local_with_tech",
                "DEFINE dup_lib ./old_dup",
                "DEFINE dup_lib ./new_dup",
                f"DEFINE shared_ip {ext_design}",
                f"DEFINE readonly_ext {ext_ro}",
                f"DEFINE pdk_tech {ext_pdk_tech}",
                f"DEFINE pdk_init {ext_pdk_init}",
                f"DEFINE pdk_cxt {ext_pdk_cxt}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    try:
        disc = discover_workspace_libraries(
            root,
            env={"CDSHOME": str(cdshome)},
        )
    finally:
        # Restore write permissions so pytest cleanup succeeds
        (root / "ro_local").chmod(0o755)
        ext_ro.chmod(0o755)

    assert disc.cds_lib == cds_lib.resolve()
    by_name = {entry.name: entry for entry in disc.entries}
    assert len(disc.entries) == len(by_name)  # duplicate DEFINE deduplicated

    # Last DEFINE wins for dup_lib
    assert by_name["dup_lib"].raw_path == "./new_dup"
    assert by_name["dup_lib"].classification == "design_local"

    # Design local libraries (writable + inside workspace)
    for local_name in ("analog_top", "sub_blk", "dup_lib", "local_with_tech"):
        assert by_name[local_name].classification == "design_local"
        assert by_name[local_name].in_workspace is True
        assert by_name[local_name].is_writable is True

    # Design external library: in oa_libs (read-only dirty tracking), NOT in allow_libraries or ignore paths
    assert by_name["shared_ip"].classification == "design_external"
    assert by_name["shared_ip"].in_workspace is False
    assert disc.oa_libs["shared_ip"] == str(ext_design.resolve())
    assert "shared_ip" not in disc.allow_libraries
    assert all("shared_ip" not in p for p in disc.repo_local_ignore_paths)

    # Exact oa_libs / allow_libraries / repo_local_ignore_paths sets
    assert disc.oa_libs == {
        "analog_top": "analog_top",
        "dup_lib": "new_dup",
        "local_with_tech": "local_with_tech",
        "shared_ip": str(ext_design.resolve()),
        "sub_blk": "oa/sub_blk",
    }
    assert disc.allow_libraries == (
        "analog_top",
        "dup_lib",
        "local_with_tech",
        "sub_blk",
    )
    assert disc.repo_local_ignore_paths == (
        "analog_top",
        "local_with_tech",
        "new_dup",
        "oa/sub_blk",
    )

    # Excluded classifications & reasons
    assert by_name["analogLib"].classification == "system_or_pdk"
    assert by_name["basic"].classification == "system_or_pdk"
    assert by_name["vendor_eda"].classification == "system_or_pdk"
    assert by_name["unresolved_pdk"].classification == "system_or_pdk"
    assert "unresolved" in by_name["unresolved_pdk"].reason
    assert by_name["sys_opt"].classification == "system_or_pdk"
    assert by_name["ro_local"].classification == "system_or_pdk"
    assert by_name["readonly_ext"].classification == "system_or_pdk"
    assert by_name["pdk_tech"].classification == "system_or_pdk"
    assert by_name["pdk_init"].classification == "system_or_pdk"
    assert by_name["pdk_cxt"].classification == "system_or_pdk"
    assert by_name["hidden_local"].classification == "invisible"
    assert by_name["combined_dir"].classification == "invisible"
    assert by_name["inside_snapshots"].classification == "invisible"
    assert by_name["inside_oagit"].classification == "invisible"
    assert by_name["missing_lib"].classification == "missing"

    excluded_names = {e.name for e in disc.excluded}
    assert "analog_top" not in excluded_names
    assert "shared_ip" not in excluded_names
    assert {"analogLib", "pdk_tech", "hidden_local", "missing_lib"} <= excluded_names
    assert "analogLib" in BUILTIN_CADENCE_LIBS


def test_discover_workspace_libraries_resolves_relative_to_cds_lib_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    sub = root / "sim"
    (sub / "my_lib").mkdir(parents=True)
    cds_lib = sub / "cds.lib"
    cds_lib.write_text("DEFINE my_lib ./my_lib\n", encoding="utf-8")

    disc = discover_workspace_libraries(root, cds_lib=cds_lib, env={})
    assert disc.oa_libs == {"my_lib": "sim/my_lib"}
    assert disc.allow_libraries == ("my_lib",)
    assert disc.repo_local_ignore_paths == ("sim/my_lib",)


# ---------------------------------------------------------------------------
# 2. Virtuoso command detection priority
# ---------------------------------------------------------------------------


def test_detect_virtuoso_cmd_priority_order(tmp_path: Path) -> None:
    cdshome = tmp_path / "cdshome"
    cds_bin = _make_executable(cdshome / "tools" / "dfII" / "bin" / "virtuoso")

    bin_v = tmp_path / "bin_v"
    _make_executable(bin_v / "virtuoso")
    _make_executable(bin_v / "virtuoso261")

    bin_v261 = tmp_path / "bin_v261"
    _make_executable(bin_v261 / "virtuoso261")

    # 1. OAGIT_VIRTUOSO_CMD takes top priority
    assert (
        detect_virtuoso_cmd(
            env={
                "OAGIT_VIRTUOSO_CMD": "  /custom/virtuoso_wrapper  ",
                "PATH": str(bin_v),
                "CDSHOME": str(cdshome),
            }
        )
        == "/custom/virtuoso_wrapper"
    )

    # 2. virtuoso on PATH -> portable command name "virtuoso"
    assert (
        detect_virtuoso_cmd(env={"PATH": str(bin_v), "CDSHOME": str(cdshome)})
        == "virtuoso"
    )

    # 3. virtuoso261 on PATH -> portable command name "virtuoso261"
    assert (
        detect_virtuoso_cmd(env={"PATH": str(bin_v261), "CDSHOME": str(cdshome)})
        == "virtuoso261"
    )

    # 4. Fallback to $CDSHOME/tools/dfII/bin/virtuoso
    assert detect_virtuoso_cmd(env={"PATH": "", "CDSHOME": str(cdshome)}) == str(cds_bin)

    # Non-executable $CDSHOME binary is ignored
    cds_bin.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert detect_virtuoso_cmd(env={"PATH": "", "CDSHOME": str(cdshome)}) is None

    # 5. Custom which_fn support
    assert (
        detect_virtuoso_cmd(
            env={},
            which_fn=lambda cmd: "/eda/bin/virtuoso261" if cmd == "virtuoso261" else None,
        )
        == "virtuoso261"
    )
    assert detect_virtuoso_cmd(env={}, which_fn=lambda _cmd: None) is None


# ---------------------------------------------------------------------------
# 3. Git repo & managed .gitignore hygiene + health checks
# ---------------------------------------------------------------------------


def test_managed_gitignore_preserves_user_lines_and_anchors_oa_libs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "my_lib").mkdir()
    (repo / "my_lib" / "cell.oa").write_bytes(b"OA_BIN")

    assert is_inside_git_repo(repo) is None
    inited, top = ensure_git_repo(repo)
    assert inited is True
    assert top == repo.resolve()
    inited_again, top_again = ensure_git_repo(repo)
    assert inited_again is False
    assert top_again == repo.resolve()

    # Pre-existing user .gitignore
    gi = repo / ".gitignore"
    gi.write_text("# user rules\nsim_results/\n*.psf\n", encoding="utf-8")

    path1, changed1 = update_managed_gitignore(repo, ["my_lib", "oa/sub_lib"])
    assert path1 == gi
    assert changed1 is True
    content = gi.read_text(encoding="utf-8")
    assert content.startswith("# user rules\nsim_results/\n*.psf\n")
    assert MANAGED_GITIGNORE_START in content
    assert MANAGED_GITIGNORE_END in content
    assert "/my_lib/" in content.splitlines()
    assert "/oa/sub_lib/" in content.splitlines()
    assert ".oagit/index.json" in content.splitlines()
    assert "*.cdslck*" in content.splitlines()

    # Add trailing user lines after managed block, then update block in-place
    gi.write_text(content + "\n# trailing user rule\nmy_notes.txt\n", encoding="utf-8")
    _, changed2 = update_managed_gitignore(repo, ["my_lib"])
    assert changed2 is True
    updated = gi.read_text(encoding="utf-8")
    assert "# user rules" in updated
    assert "# trailing user rule\nmy_notes.txt\n" in updated
    assert "/my_lib/" in updated.splitlines()
    assert "/oa/sub_lib/" not in updated.splitlines()

    # Idempotent re-run does not rewrite file
    _, changed3 = update_managed_gitignore(repo, ["my_lib"])
    assert changed3 is False

    # Root-anchored /my_lib/ ignores binary ./my_lib/ while snapshots/my_lib/ stays tracked!
    cv = CellViewRef("my_lib", "amp", "schematic")
    snap_dir = snapshot_dir_for(repo / "snapshots", cv)
    write_snapshot(_make_sch(cv), snap_dir)

    subprocess.run(["git", "add", "snapshots/"], cwd=repo, check=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "snapshots/my_lib/amp/schematic/manifest.json" in staged

    cds_lib = repo / "cds.lib"
    cds_lib.write_text("DEFINE my_lib ./my_lib\n", encoding="utf-8")
    disc = discover_workspace_libraries(repo, env={})
    health = check_gitignore_health(repo, snapshots_dir=repo / "snapshots", discovery=disc)
    assert health == {"unignored_oa_libs": [], "ignored_snapshot_paths": []}


def test_render_managed_gitignore_block_normalizes_and_sorts() -> None:
    block = render_managed_gitignore_block(["./z_lib/", "a_lib", "oa/b_lib/", "../escape"])
    lines = block.splitlines()
    assert lines[0] == MANAGED_GITIGNORE_START
    assert lines[-1] == MANAGED_GITIGNORE_END
    lib_lines = [line for line in lines if line.startswith("/")]
    assert lib_lines == ["/a_lib/", "/oa/b_lib/", "/z_lib/"]


# ---------------------------------------------------------------------------
# 4. End-to-end CLI: 0-arg init, dynamic cds.lib, --sync, warnings/hints, flags
# ---------------------------------------------------------------------------


def test_zero_arg_init_discovers_workspace_inits_git_and_prints_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "cadence_ws"
    ws.mkdir()
    (ws / "work_lib").mkdir()
    ext_lib = tmp_path / "ext_design"
    ext_lib.mkdir()
    ext_pdk = tmp_path / "ext_pdk"
    ext_pdk.mkdir()
    (ext_pdk / "tech.db").write_bytes(b"TECH")

    (ws / "cds.lib").write_text(
        "\n".join(
            [
                "DEFINE analogLib $CDSHOME/tools/dfII/etc/cdslib/analogLib",
                "DEFINE work_lib ./work_lib",
                f"DEFINE ext_design {ext_lib}",
                f"DEFINE ext_pdk {ext_pdk}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    _make_executable(fake_bin / "virtuoso261")
    monkeypatch.delenv("OAGIT_VIRTUOSO_CMD", raising=False)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        "oa_git.cli.detect_virtuoso_cmd",
        lambda: detect_virtuoso_cmd(env={"PATH": str(fake_bin)}),
    )
    monkeypatch.chdir(ws)

    # Zero-arg `oagit init` (human text output)
    code = main(["init"])
    text = capsys.readouterr().out
    assert code == 0
    assert (ws / ".oagit" / "config.json").is_file()
    assert (ws / ".git").is_dir()
    assert (ws / ".gitignore").is_file()
    assert "cds.lib: cds.lib" in text
    assert "Virtuoso cmd: virtuoso261" in text
    assert "work_lib=work_lib" in text
    assert "Allow libraries: work_lib" in text
    assert ".gitignore: updated" in text

    cfg = load_project_config(ws)
    assert cfg["schema"] == CONFIG_SCHEMA_V2
    assert cfg["cds_lib"] == "cds.lib"
    assert cfg["virtuoso_cmd"] == "virtuoso261"
    assert cfg["oa_libs"] == {
        "ext_design": str(ext_lib.resolve()),
        "work_lib": "work_lib",
    }
    assert cfg["allow_libraries"] == ["work_lib"]
    assert "/work_lib/" in (ws / ".gitignore").read_text(encoding="utf-8").splitlines()

    # Re-running `oagit init` without --sync / --force fails closed with actionable hint
    code, err, captured = _invoke_json(["init"], capsys)
    assert code == 2
    assert err["error_type"] == "SchemaValidationError"
    assert "--sync" in err["error"]
    assert "--sync" in err["hint"]
    assert "--force" in err["hint"]
    assert captured.err == ""

    # Fresh JSON init in a second directory verifies JSON discovery keys
    ws2 = tmp_path / "cadence_ws2"
    ws2.mkdir()
    (ws2 / "lib_a").mkdir()
    (ws2 / "cds.lib").write_text("DEFINE lib_a ./lib_a\nDEFINE analogLib /opt/cds/a\n", encoding="utf-8")
    code, payload, _ = _invoke_json(["init", "-C", str(ws2)], capsys)
    assert code == 0
    assert payload["git_initialized"] is True
    assert payload["gitignore_updated"] is True
    assert payload["gitignore_path"] == str((ws2 / ".gitignore").resolve())
    assert payload["discovered"] == {
        "cds_lib": str((ws2 / "cds.lib").resolve()),
        "oa_libs": {"lib_a": "lib_a"},
        "allow_libraries": ["lib_a"],
        "excluded_count": 1,
    }


def test_gui_appended_define_dynamic_status_capture_fail_closed_restore_and_sync(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = tmp_path / "live_ws"
    ws.mkdir()
    (ws / "work").mkdir()
    (ws / "cds.lib").write_text("DEFINE work ./work\n", encoding="utf-8")

    # Initialize with custom daemon/cdf/timeout/virtuoso_cmd settings & fixture backend
    fixture_dir = tmp_path / "fixture"
    code, _, _ = _invoke_json(
        [
            "init",
            "-C",
            str(ws),
            "--backend",
            "fixture",
            "--virtuoso-cmd",
            "custom_virtuoso",
            "--timeout-s",
            "321",
            "--daemon-idle-timeout",
            "95",
            "--max-passes",
            "5",
            "--remote-workdir",
            "/var/tmp/custom_oagit",
            "--allow-library",
            "custom_sandbox",
        ],
        capsys,
    )
    assert code == 0

    # Add custom coupling rule & custom oa_lib override directly into config
    cfg = load_project_config(ws)
    custom_override_dir = tmp_path / "custom_override"
    custom_override_dir.mkdir()
    cfg["oa_libs"]["custom_lib"] = str(custom_override_dir)
    cfg["cdf"]["coupling_rules"] = {
        "pdk/nch": [
            {
                "name": "size",
                "members": ["w", "l"],
                "primary": ["w", "l"],
                "derived": [],
                "order": ["w", "l"],
                "dependencies": {},
            }
        ]
    }
    write_project_config(ws, cfg, force=True)

    # Simulate Virtuoso GUI appending a brand-new library to cds.lib!
    new_lib_dir = ws / "new_lib"
    oa_file = new_lib_dir / "inv" / "schematic" / "sch.oa"
    oa_file.parent.mkdir(parents=True)
    oa_file.write_bytes(b"OA_NEW_V1")
    with (ws / "cds.lib").open("a", encoding="utf-8") as fh:
        fh.write("DEFINE new_lib ./new_lib\n")

    cv_new = CellViewRef("new_lib", "inv", "schematic")
    write_snapshot(_make_sch(cv_new), fixture_dir / "new_lib" / "inv" / "schematic")

    gitignore_before = (ws / ".gitignore").read_text(encoding="utf-8")
    config_before = (ws / ".oagit" / "config.json").read_text(encoding="utf-8")

    # 1) `oagit capture` dynamically sees new_lib via cds.lib without re-init & records clean index!
    code, cap_out, _ = _invoke_json(
        ["capture", cv_new.key, "-C", str(ws), "--fixture", str(fixture_dir)],
        capsys,
    )
    assert code == 0
    idx = load_index(ws)
    assert cv_new.key in idx
    assert idx[cv_new.key].state == "clean"
    assert idx[cv_new.key].source_files and idx[cv_new.key].source_files[0].exists is True

    # Modify new_lib OA file -> `oagit status` dynamically detects dirty state + prints unsynced hint
    oa_file.write_bytes(b"OA_NEW_V2")
    code, st_json, _ = _invoke_json(["status", "-C", str(ws)], capsys)
    assert code == 0
    assert st_json["ok"] is True
    assert any(
        item["key"] == cv_new.key and item["state"] == "dirty"
        for item in st_json["index_states"]
    )
    assert "new_lib" in st_json["gitignore_health"]["unignored_oa_libs"]
    assert "new_lib" in st_json["gitignore_health"]["unsynced_oa_libs"]
    assert any("new_lib" in hint and "oagit init --sync" in hint for hint in st_json["hints"])

    # Human text status prints the hint and NEVER modifies .gitignore or config.json!
    code = main(["status", "-C", str(ws)])
    st_text = capsys.readouterr().out
    assert code == 0
    assert "Hint: new OA library 'new_lib' in cds.lib is not synced; run 'oagit init --sync'" in st_text
    assert (ws / ".gitignore").read_text(encoding="utf-8") == gitignore_before
    assert (ws / ".oagit" / "config.json").read_text(encoding="utf-8") == config_before

    # 2) `oagit restore` stays strictly FAIL-CLOSED for unsynced new_lib!
    code, res_err, _ = _invoke_json(
        [
            "restore",
            cv_new.key,
            "-C",
            str(ws),
            "--fixture",
            str(fixture_dir),
            "--target",
            "new_lib/inv_copy/schematic",
        ],
        capsys,
    )
    assert code == 2
    assert res_err["error_type"] == "DisallowedLibraryError"

    # 3) `oagit init --sync` updates oa_libs, allow_libraries, and .gitignore while preserving ALL customizations!
    code, sync_out, _ = _invoke_json(["init", "--sync", "-C", str(ws)], capsys)
    assert code == 0
    assert sync_out["synced"] is True
    assert sync_out["gitignore_updated"] is True
    synced_cfg = load_project_config(ws)
    assert synced_cfg["backend"] == "fixture"
    assert synced_cfg["virtuoso_cmd"] == "custom_virtuoso"
    assert synced_cfg["timeout_s"] == 321
    assert synced_cfg["remote_workdir"] == "/var/tmp/custom_oagit"
    assert synced_cfg["daemon"]["idle_timeout_s"] == 95
    assert synced_cfg["cdf"]["max_passes"] == 5
    assert synced_cfg["cdf"]["coupling_rules"] == {
        "pdk/nch": [
            {
                "name": "size",
                "members": ["w", "l"],
                "primary": ["w", "l"],
                "derived": [],
                "order": ["w", "l"],
                "dependencies": {},
            }
        ]
    }
    assert synced_cfg["oa_libs"]["custom_lib"] == str(custom_override_dir.resolve())
    assert synced_cfg["oa_libs"]["new_lib"] == "new_lib"
    assert synced_cfg["oa_libs"]["work"] == "work"
    assert set(synced_cfg["allow_libraries"]) == {"work", "custom_sandbox", "new_lib"}
    assert "/new_lib/" in (ws / ".gitignore").read_text(encoding="utf-8").splitlines()

    # Repeated `--sync` is idempotent (gitignore_updated == False)
    code, sync_out2, _ = _invoke_json(["init", "--sync", "-C", str(ws)], capsys)
    assert code == 0
    assert sync_out2["gitignore_updated"] is False

    # Status now has NO unsynced hints
    code, st_after, _ = _invoke_json(["status", "-C", str(ws)], capsys)
    assert code == 0
    assert st_after["gitignore_health"]["unignored_oa_libs"] == []
    assert st_after["gitignore_health"]["unsynced_oa_libs"] == []
    assert st_after["hints"] == []

    # Restore to new_lib is now allowed!
    code, res_ok, _ = _invoke_json(
        [
            "restore",
            cv_new.key,
            "-C",
            str(ws),
            "--fixture",
            str(fixture_dir),
            "--target",
            "new_lib/inv_copy/schematic",
        ],
        capsys,
    )
    assert code == 0
    assert res_ok["published"] is True


def test_status_warns_on_unanchored_gitignore_footgun_without_mutating(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "footgun_ws"
    ws.mkdir()
    (ws / "my_lib").mkdir()
    (ws / "cds.lib").write_text("DEFINE my_lib ./my_lib\n", encoding="utf-8")

    code, _, _ = _invoke_json(["init", "-C", str(ws), "--backend", "fixture"], capsys)
    assert code == 0

    cv = CellViewRef("my_lib", "amp", "schematic")
    write_snapshot(_make_sch(cv), snapshot_dir_for(ws / "snapshots", cv))

    # User accidentally adds unanchored `my_lib/` which also ignores `snapshots/my_lib/`!
    gi = ws / ".gitignore"
    gi.write_text(gi.read_text(encoding="utf-8") + "\nmy_lib/\n", encoding="utf-8")
    gi_before = gi.read_text(encoding="utf-8")

    code, st_json, _ = _invoke_json(["status", "-C", str(ws)], capsys)
    assert code == 0
    ignored_snaps = st_json["gitignore_health"]["ignored_snapshot_paths"]
    assert "snapshots/my_lib/" in ignored_snaps
    assert "snapshots/my_lib/amp/schematic/manifest.json" in ignored_snaps
    assert any(
        "snapshots/my_lib/" in w and "use leading '/<lib>/'" in w
        for w in st_json["warnings"]
    )

    code = main(["status", "-C", str(ws)])
    st_text = capsys.readouterr().out
    assert code == 0
    assert (
        "Warning: snapshot path 'snapshots/my_lib/' is ignored by .gitignore "
        "(use leading '/<lib>/' for OA libraries)"
    ) in st_text
    assert gi.read_text(encoding="utf-8") == gi_before


def test_init_flags_v1_no_git_no_auto_allow_no_auto_discover_sync_fresh_and_ssh(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. --v1 creates legacy .oagit.json without .git or .gitignore
    p_v1 = tmp_path / "p_v1"
    p_v1.mkdir()
    (p_v1 / "work").mkdir()
    (p_v1 / "cds.lib").write_text("DEFINE work ./work\n", encoding="utf-8")
    code, out_v1, _ = _invoke_json(["init", "-C", str(p_v1), "--v1", "--backend", "fixture"], capsys)
    assert code == 0
    assert out_v1["config"]["schema"] == CONFIG_SCHEMA_V1
    assert (p_v1 / ".oagit.json").is_file()
    assert not (p_v1 / ".oagit").exists()
    assert not (p_v1 / ".git").exists()
    assert not (p_v1 / ".gitignore").exists()

    # --sync on existing v1 fails cleanly with migration guidance
    code, err_v1_sync, _ = _invoke_json(["init", "-C", str(p_v1), "--sync"], capsys)
    assert code == 2
    assert err_v1_sync["error_type"] == "SchemaValidationError"
    assert "--force" in err_v1_sync["error"]

    # --v1 combined with --v2 or --sync is rejected
    code, err_conflict, _ = _invoke_json(["init", "-C", str(tmp_path / "bad"), "--v1", "--v2"], capsys)
    assert code == 2
    assert err_conflict["error_type"] == "SchemaValidationError"

    # 2. --no-git skips git init and .gitignore
    p_nogit = tmp_path / "p_nogit"
    p_nogit.mkdir()
    (p_nogit / "work").mkdir()
    (p_nogit / "cds.lib").write_text("DEFINE work ./work\n", encoding="utf-8")
    code, out_nogit, _ = _invoke_json(["init", "-C", str(p_nogit), "--no-git"], capsys)
    assert code == 0
    assert out_nogit["git_initialized"] is False
    assert out_nogit["gitignore_path"] is None
    assert not (p_nogit / ".git").exists()
    assert not (p_nogit / ".gitignore").exists()
    assert out_nogit["config"]["oa_libs"] == {"work": "work"}
    assert out_nogit["config"]["allow_libraries"] == ["work"]

    # 3. --no-auto-allow discovers oa_libs but leaves allow_libraries empty (or CLI-only)
    p_noallow = tmp_path / "p_noallow"
    p_noallow.mkdir()
    (p_noallow / "work").mkdir()
    (p_noallow / "cds.lib").write_text("DEFINE work ./work\n", encoding="utf-8")
    code, out_noallow, _ = _invoke_json(
        ["init", "-C", str(p_noallow), "--no-auto-allow", "--no-git"], capsys
    )
    assert code == 0
    assert out_noallow["config"]["oa_libs"] == {"work": "work"}
    assert out_noallow["config"]["allow_libraries"] == []

    # 4. --no-auto-discover skips cds.lib & PATH Virtuoso probe, still adds explicit repo-local --oa-lib to .gitignore
    p_nodisc = tmp_path / "p_nodisc"
    p_nodisc.mkdir()
    (p_nodisc / "work").mkdir()
    (p_nodisc / "explicit_oa").mkdir()
    (p_nodisc / "cds.lib").write_text("DEFINE work ./work\n", encoding="utf-8")
    monkeypatch.setattr("oa_git.cli.detect_virtuoso_cmd", lambda: "virtuoso261")
    code, out_nodisc, _ = _invoke_json(
        [
            "init",
            "-C",
            str(p_nodisc),
            "--no-auto-discover",
            "--oa-lib",
            f"exp={p_nodisc / 'explicit_oa'}",
        ],
        capsys,
    )
    assert code == 0
    assert out_nodisc["config"]["cds_lib"] is None
    assert out_nodisc["config"]["oa_libs"] == {"exp": "explicit_oa"}
    assert out_nodisc["config"]["allow_libraries"] == []
    assert out_nodisc["config"]["virtuoso_cmd"] == "virtuoso"
    assert "/explicit_oa/" in (p_nodisc / ".gitignore").read_text(encoding="utf-8").splitlines()

    # 5. --sync on an uninitialized directory initializes cleanly
    p_sync_fresh = tmp_path / "p_sync_fresh"
    p_sync_fresh.mkdir()
    (p_sync_fresh / "blk").mkdir()
    (p_sync_fresh / "cds.lib").write_text("DEFINE blk ./blk\n", encoding="utf-8")
    code, out_sync_fresh, _ = _invoke_json(["init", "--sync", "-C", str(p_sync_fresh)], capsys)
    assert code == 0
    assert (p_sync_fresh / ".oagit" / "config.json").is_file()
    assert out_sync_fresh["config"]["oa_libs"] == {"blk": "blk"}
    assert out_sync_fresh["config"]["allow_libraries"] == ["blk"]

    # 6. SSH mode skips local cds.lib AND local PATH Virtuoso probing!
    p_ssh = tmp_path / "p_ssh"
    p_ssh.mkdir()
    (p_ssh / "local_lib").mkdir()
    (p_ssh / "cds.lib").write_text("DEFINE local_lib ./local_lib\n", encoding="utf-8")
    code, out_ssh, _ = _invoke_json(
        [
            "init",
            "-C",
            str(p_ssh),
            "--ssh-host",
            "remote_eda_box",
            "--cds-lib",
            "/remote/work/cds.lib",
            "--no-git",
        ],
        capsys,
    )
    assert code == 0
    assert out_ssh["config"]["ssh_host"] == "remote_eda_box"
    assert out_ssh["config"]["cds_lib"] == "/remote/work/cds.lib"
    assert out_ssh["config"]["oa_libs"] == {}
    assert out_ssh["config"]["allow_libraries"] == []
    assert out_ssh["config"]["virtuoso_cmd"] == "virtuoso"
    assert out_ssh["discovered"]["cds_lib"] is None

    # `--sync` on an SSH-configured project preserves ssh_host & skips local cds.lib
    code, out_ssh_sync, _ = _invoke_json(["init", "--sync", "-C", str(p_ssh), "--no-git"], capsys)
    assert code == 0
    assert out_ssh_sync["synced"] is True
    assert out_ssh_sync["config"]["ssh_host"] == "remote_eda_box"
    assert out_ssh_sync["config"]["cds_lib"] == "/remote/work/cds.lib"
    assert out_ssh_sync["config"]["oa_libs"] == {}

    # 7. --v2 remains an accepted no-op
    p_v2 = tmp_path / "p_v2"
    code, out_v2, _ = _invoke_json(["init", "-C", str(p_v2), "--v2", "--no-git"], capsys)
    assert code == 0
    assert out_v2["config"]["schema"] == CONFIG_SCHEMA_V2


def test_python_module_entrypoints_and_release_hygiene(tmp_path: Path) -> None:
    import oa_git

    assert oa_git.__version__ == "0.1.0"
    for mod in ("oa_git", "oa_git.cli"):
        res = subprocess.run(
            [sys.executable, "-m", mod, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr
        assert "init,status,capture,restore,diff,merge,daemon" in res.stdout

    # UNDEFINE removes earlier DEFINE; ASSIGN ... DISPLAY Visible overrides Invisible
    cds = tmp_path / "cds.lib"
    cds.write_text(
        "\n".join(
            [
                "DEFINE removed_lib ./removed_lib",
                "UNDEFINE removed_lib",
                "DEFINE toggled_lib ./toggled_lib",
                "ASSIGN toggled_lib DISPLAY Invisible",
                "ASSIGN toggled_lib DISPLAY Visible",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    defines, invisible, _combined = parse_cds_lib_statements(cds)
    assert defines == [("toggled_lib", "./toggled_lib")]
    assert "toggled_lib" not in invisible

    # Managed .gitignore defaults to world-readable 0644 (not 0600)
    gi_path, _ = update_managed_gitignore(tmp_path, ["toggled_lib"])
    assert stat.S_IMODE(gi_path.stat().st_mode) == 0o644


def test_managed_gitignore_includes_ssh_and_fails_closed_on_malformed_markers(
    tmp_path: Path,
) -> None:
    """Managed .gitignore ignores .oagit/ssh/ and fails closed on mismatched markers."""

    block = render_managed_gitignore_block(["work_lib"])
    assert ".oagit/ssh/" in block.splitlines()

    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    gi_ok, changed = update_managed_gitignore(ok_dir, ["work_lib"])
    assert changed is True
    assert ".oagit/ssh/" in gi_ok.read_text(encoding="utf-8").splitlines()

    malformed_cases = {
        "unclosed_start": f"# user\n{MANAGED_GITIGNORE_START}\n/work_lib/\n# keep me\n",
        "stray_end": f"# user\n{MANAGED_GITIGNORE_END}\n# keep me\n",
        "end_before_start": (
            f"# user\n{MANAGED_GITIGNORE_END}\nmiddle_rule/\n{MANAGED_GITIGNORE_START}\n"
        ),
        "duplicate_blocks": (
            f"{MANAGED_GITIGNORE_START}\n/a/\n{MANAGED_GITIGNORE_END}\n"
            f"# user line in between\n"
            f"{MANAGED_GITIGNORE_START}\n/b/\n{MANAGED_GITIGNORE_END}\n"
        ),
        "duplicate_start": (
            f"{MANAGED_GITIGNORE_START}\n{MANAGED_GITIGNORE_START}\n{MANAGED_GITIGNORE_END}\n"
        ),
    }

    for name, raw_content in malformed_cases.items():
        case_dir = tmp_path / name
        case_dir.mkdir()
        gi = case_dir / ".gitignore"
        gi.write_text(raw_content, encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="malformed oa-git managed ignore markers"):
            update_managed_gitignore(case_dir, ["work_lib"])
        # File must remain completely untouched on failure
        assert gi.read_text(encoding="utf-8") == raw_content


def test_restore_auto_infers_oa_view_dir_live_lock_for_schematic_and_symbol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """oagit restore infers oa_view_dir from oa_libs for schematic and symbol views too."""

    ws = tmp_path / "lock_ws"
    ws.mkdir()
    sandbox_dir = ws / "sandbox"
    sandbox_dir.mkdir()
    (ws / "cds.lib").write_text("DEFINE sandbox ./sandbox\n", encoding="utf-8")

    code, _, _ = _invoke_json(["init", "-C", str(ws), "--backend", "fixture", "--no-git"], capsys)
    assert code == 0

    fixture_dir = tmp_path / "fixture"
    sch_cv = CellViewRef("work", "amp", "schematic")
    sch_target = CellViewRef("sandbox", "amp", "schematic")
    sch_snap = _make_sch(sch_cv)
    write_snapshot(sch_snap, snapshot_dir_for(ws / "snapshots", sch_cv))
    write_snapshot(_make_sch(sch_target), fixture_dir / "sandbox" / "amp" / "schematic")

    sym_cv = CellViewRef("work", "amp", "symbol")
    sym_target = CellViewRef("sandbox", "amp", "symbol")

    def _make_sym(cv: CellViewRef):
        return build_symbol_snapshot(
            cv,
            pins=(
                SymbolPin(
                    name="IN",
                    direction="input",
                    xy=(0.0, 0.0),
                    pin_figure=SymbolPinFigure(
                        kind="rect",
                        layer="pin",
                        purpose="drawing",
                        bbox=((-0.025, -0.025), (0.025, 0.025)),
                    ),
                ),
            ),
            port_order=("IN",),
            graphics=SymbolGraphics(origin=(0.0, 0.0)),
        )

    write_symbol_snapshot(_make_sym(sym_cv), snapshot_dir_for(ws / "snapshots", sym_cv))
    write_symbol_snapshot(_make_sym(sym_target), fixture_dir / "sandbox" / "amp" / "symbol")

    # Plant active .cdslck lock files in inferred OA view directories
    sch_lock = sandbox_dir / "amp" / "schematic" / "sch.oa.cdslck"
    sch_lock.parent.mkdir(parents=True)
    sch_lock.write_text("lock", encoding="utf-8")

    sym_lock = sandbox_dir / "amp" / "symbol" / "symbol.oa.cdslck"
    sym_lock.parent.mkdir(parents=True)
    sym_lock.write_text("lock", encoding="utf-8")

    # Schematic restore fails closed with LiveLockError WITHOUT --oa-view-dir
    code, err_sch, _ = _invoke_json(
        [
            "restore",
            sch_cv.key,
            "-C",
            str(ws),
            "--fixture",
            str(fixture_dir),
            "--target",
            sch_target.key,
            "--overwrite",
        ],
        capsys,
    )
    assert code == 2
    assert err_sch["error_type"] == "LiveLockError"

    # Symbol restore fails closed with LiveLockError WITHOUT --oa-view-dir
    code, err_sym, _ = _invoke_json(
        [
            "restore",
            sym_cv.key,
            "-C",
            str(ws),
            "--fixture",
            str(fixture_dir),
            "--target",
            sym_target.key,
            "--overwrite",
        ],
        capsys,
    )
    assert code == 2
    assert err_sym["error_type"] == "LiveLockError"

    # Removing locks allows both restores to succeed cleanly
    sch_lock.unlink()
    sym_lock.unlink()

    code, ok_sch, _ = _invoke_json(
        [
            "restore",
            sch_cv.key,
            "-C",
            str(ws),
            "--fixture",
            str(fixture_dir),
            "--target",
            sch_target.key,
            "--overwrite",
        ],
        capsys,
    )
    assert code == 0
    assert ok_sch["published"] is True

    code, ok_sym, _ = _invoke_json(
        [
            "restore",
            sym_cv.key,
            "-C",
            str(ws),
            "--fixture",
            str(fixture_dir),
            "--target",
            sym_target.key,
            "--overwrite",
        ],
        capsys,
    )
    assert code == 0
    assert ok_sym["published"] is True
