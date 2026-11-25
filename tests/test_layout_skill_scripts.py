"""Offline static syntax and contract audit tests for layout SKILL scripts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "oa_git" / "skill"
LAYOUT_SKILL_FILES = {
    "export_layout": SKILL_DIR / "export_layout.il",
    "restore_layout": SKILL_DIR / "restore_layout.il",
}
PROCEDURE_RE = re.compile(r"\bprocedure\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _balanced_skill_parentheses(source: str) -> None:
    """Check parentheses while ignoring SKILL strings and both comment forms."""

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
    assert depth == 0, f"unbalanced parentheses, depth={depth}"


@pytest.mark.parametrize(
    "path", LAYOUT_SKILL_FILES.values(), ids=lambda path: path.name
)
def test_layout_skill_scripts_are_parenthesis_balanced(path: Path) -> None:
    _balanced_skill_parentheses(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "path", LAYOUT_SKILL_FILES.values(), ids=lambda path: path.name
)
def test_layout_skill_scripts_forbid_gui_and_private_apis(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    forbidden_apis = (
        "leHi",
        "schHi",
        "geHi",
        "cdfGetParam",
        "cdfGetParameterList",
        "dbReplaceProp",
    )
    for forbidden in forbidden_apis:
        assert forbidden not in source, (
            f"{path.name} contains forbidden API substring {forbidden!r}"
        )


def test_layout_skill_procedure_prefix_isolation() -> None:
    export_src = LAYOUT_SKILL_FILES["export_layout"].read_text(encoding="utf-8")
    restore_src = LAYOUT_SKILL_FILES["restore_layout"].read_text(encoding="utf-8")

    export_procs = PROCEDURE_RE.findall(export_src)
    restore_procs = PROCEDURE_RE.findall(restore_src)

    assert export_procs, "export_layout.il must define procedures"
    assert restore_procs, "restore_layout.il must define procedures"

    for name in export_procs:
        assert name.startswith("oagitLayoutExport") or name == "oagitLayoutRunExport", (
            f"export_layout.il procedure {name!r} violates oagitLayoutExport* prefix"
        )
    for name in restore_procs:
        assert name.startswith("oagitLayoutRestore") or name == "oagitLayoutRunRestore", (
            f"restore_layout.il procedure {name!r} violates oagitLayoutRestore* prefix"
        )
    # Verify Track 1 daemon.il entrypoint aliases exist.
    assert "oagitLayoutRunExport" in export_procs
    assert "oagitLayoutRunRestore" in restore_procs
    assert "dbAddFigToPin" in restore_src
    assert "ddGetObj(lib draft_cell)" in restore_src

    assert set(export_procs).isdisjoint(set(restore_procs))

    # Also verify zero procedure collisions with shared/schematic SKILL scripts
    # when loaded together inside a persistent Virtuoso daemon.
    layout_procs = set(export_procs) | set(restore_procs)
    for other_name in ("oagit_common.il", "export_schematic.il", "restore_schematic.il"):
        other_path = SKILL_DIR / other_name
        if other_path.is_file():
            other_procs = set(PROCEDURE_RE.findall(other_path.read_text(encoding="utf-8")))
            collisions = layout_procs & other_procs
            assert not collisions, (
                f"procedure collision between layout SKILL and {other_name}: {sorted(collisions)}"
            )


def test_export_layout_strips_all_model_metadata_and_display_props() -> None:
    from oa_git.layout_model import (
        STRIPPED_CELLVIEW_DISPLAY_PROPS,
        STRIPPED_PDK_METADATA_KEYS,
    )

    source = LAYOUT_SKILL_FILES["export_layout"].read_text(encoding="utf-8")
    for key in sorted(STRIPPED_PDK_METADATA_KEYS):
        assert f'"{key}"' in source, f"export_layout.il missing stripped metadata key {key!r}"
    for key in sorted(STRIPPED_CELLVIEW_DISPLAY_PROPS):
        assert f'"{key}"' in source, f"export_layout.il missing stripped display prop {key!r}"


def test_export_layout_protocol_and_safety_primitives() -> None:
    source = LAYOUT_SKILL_FILES["export_layout"].read_text(encoding="utf-8")
    for required in (
        "oagitLayoutExportRun",
        '"capture_layout"',
        "dbFindOpenCellViewByName",
        'dbOpenCellViewByType(lib cell view nil "r")',
        "modifiedButNotSaved",
        "modified_but_not_saved",
        "open_failed",
        "pinFigIds",
        "cv~>terminals",
        "cv~>nets",
        "cv~>instances",
        "cv~>mosaics",
        "cv~>vias",
        "cv~>shapes",
        "cv~>blockages",
        "cv~>prBoundary",
        "cv~>areaBoundary",
        "cv~>areaBoundaries",
        "cv~>snapBoundary",
        "cv~>snapBoundaries",
        "cv~>clusterBoundaries",
        "cv~>figGroups",
        "superMaster~>parameters~>value",
        "overrideParams",
        "lxComponentType",
        "hierProp",
        '"TRUE"',
        '"pin_shape"',
        "oagitLayoutExportIsAttachedTextDisplay",
        "oagitLayoutExportDisplayPropP",
        "techGetTechLibName",
        "OAGIT_COMMON_IL",
        "OAGIT_REQUEST_IL",
        "OAGIT_AUTO_EXIT",
        "omissions",
    ):
        assert required in source, f"export_layout.il missing required token {required!r}"

    # Export must strictly open read-only, never write or append.
    assert 'dbOpenCellViewByType(lib cell view nil "w")' not in source
    assert 'dbOpenCellViewByType(lib cell view "maskLayout" "w")' not in source
    assert 'dbOpenCellViewByType(lib cell view nil "a")' not in source


def test_export_layout_collects_pin_figures_before_instances_and_shapes() -> None:
    source = LAYOUT_SKILL_FILES["export_layout"].read_text(encoding="utf-8")
    build = source[source.index("procedure(oagitLayoutExportBuildResponse") :]
    pins_pos = build.index("pinRes = oagitLayoutExportPins(cv pinFigIds)")
    mosaics_pos = build.index("mosaics = oagitLayoutExportMosaics(cv mosaicInstIds)")
    insts_pos = build.index("instances = oagitLayoutExportInstances(cv pinFigIds mosaicInstIds)")
    shapes_pos = build.index("shapeRes = oagitLayoutExportShapes(cv pinFigIds)")
    assert pins_pos < mosaics_pos < insts_pos
    assert pins_pos < shapes_pos
    assert "oagitLayoutExportIsMosaicInst" in source
    assert "oagitLayoutExportWriteStreamValue" in source
    assert "%.12g" in source
    assert "oagitLayoutExportWithEnvelope" in source

    restore_src = LAYOUT_SKILL_FILES["restore_layout"].read_text(encoding="utf-8")
    assert "oagitLayoutRestoreCountNonMosaicInstances" in restore_src
    assert "oagitLayoutRestoreWriteAtomicJson" in restore_src
    assert '"oagit-spool/1"' in restore_src


def test_restore_layout_atomic_draft_publish_and_verification_primitives() -> None:
    source = LAYOUT_SKILL_FILES["restore_layout"].read_text(encoding="utf-8")
    for required in (
        "oagitLayoutRestoreRun",
        '"restore_layout"',
        "allow_libraries",
        "disallowed_library",
        "oagitDirHasLiveLocks",
        "live_lock",
        "target_exists",
        "draft_exists",
        'dbOpenCellViewByType(lib draft_cell view "maskLayout" "w")',
        "techGetTechFile",
        "techFindViaDefByName",
        "via_def_missing",
        "via_create_failed",
        "dbMakeNet",
        "dbCreatePRBoundary",
        "dbCreateAreaBoundary",
        "dbCreateSnapBoundary",
        "dbCreateClusterBoundary",
        "dbCreateParamInst",
        "dbCreateInst",
        "dbCreateConnByName",
        "dbCreateParamSimpleMosaic",
        "dbCreateSimpleMosaic",
        "dbCreateVia",
        "dbAddFigToNet",
        "dbCreateRect",
        "dbCreatePolygon",
        "dbCreatePath",
        "dbCreatePathSeg",
        "dbCreateEllipse",
        "dbCreateLine",
        "dbCreateTerm",
        "dbCreatePin",
        "dbCreateLabel",
        "dbCreateLayerBlockage",
        "dbCreateAreaBlockage",
        "dbCreateFigGroup",
        "dbCreateProp",
        "cdfParseFloatString",
        "1.0e-9",
        "pcell_parameter_error",
        "pcell_param_mismatch",
        "connectivity_mismatch",
        "pcell_verified",
        "connectivity_verified",
        "object_counts",
        "pcell_mismatches",
        "connectivity_mismatches",
        "dbSave",
        "dbCopyCellView",
        "dbPurge",
        "ddDeleteObj",
        "oagitLayoutRestoreCleanupDraft",
        "OAGIT_COMMON_IL",
        "OAGIT_REQUEST_IL",
        "OAGIT_AUTO_EXIT",
    ):
        assert required in source, f"restore_layout.il missing required token {required!r}"

    # Must NEVER open target cell for write directly.
    assert 'dbOpenCellViewByType(lib cell view "maskLayout" "w")' not in source
    assert 'dbOpenCellViewByType(lib cell view nil "w")' not in source

    # Cleanup must check both dbPurge and ddDeleteObj results.
    assert "dbPurge(cv)" in source
    assert "ddDeleteObj(draftDd)" in source
    assert "purgeResult && car(purgeResult)" in source
    assert "deleteResult && car(deleteResult)" in source
    assert "saveResult && car(saveResult)" in source
    assert "copyResult && car(copyResult)" in source


def test_restore_layout_verifies_before_save_and_publish_and_cleans_up_on_error() -> None:
    source = LAYOUT_SKILL_FILES["restore_layout"].read_text(encoding="utf-8")

    build = source[source.index("procedure(oagitLayoutRestoreBuild") :]
    pcell_pos = build.index("oagitLayoutRestoreVerifyPCells(")
    conn_pos = build.index("oagitLayoutRestoreVerifyConnectivity(")
    counts_pos = build.index("oagitLayoutRestoreVerifyCounts(")
    save_pos = build.index("saveResult = errset(dbSave(cv) t)")
    assert pcell_pos < save_pos
    assert conn_pos < save_pos
    assert counts_pos < save_pos
    assert "object_count_mismatch" in source
    assert "length(req->vias)" in source
    assert "length(req->shapes)" in source

    run_body = source[source.index("procedure(oagitLayoutRestoreRun") :]
    build_pos = run_body.index("oagitLayoutRestoreBuild(")
    err_branch_cleanup = run_body.index("oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)")
    publish_pos = run_body.index("dbCopyCellView(cv lib cell view nil nil t)")
    assert build_pos < err_branch_cleanup < publish_pos
