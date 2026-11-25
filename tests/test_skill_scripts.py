"""Offline contract checks for the official-API SKILL bridge scripts."""

from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "oa_git" / "skill"
SKILL_FILES = {
    "common": SKILL_DIR / "oagit_common.il",
    "export": SKILL_DIR / "export_schematic.il",
    "restore": SKILL_DIR / "restore_schematic.il",
}


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


@pytest.mark.parametrize("path", SKILL_FILES.values(), ids=lambda path: path.name)
def test_skill_scripts_are_parenthesis_balanced(path: Path) -> None:
    _balanced_skill_parentheses(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SKILL_FILES.values(), ids=lambda path: path.name)
def test_skill_scripts_use_only_allowed_public_surface(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for forbidden in ("schHi", "cdfGetParam", "cdfGetParameterList", "dbReplaceProp"):
        assert forbidden not in source, f"{path.name} contains forbidden API {forbidden}"


def test_common_json_and_lock_contract() -> None:
    source = SKILL_FILES["common"].read_text(encoding="utf-8")
    for required in (
        "jsonBool",
        "jsonNull",
        "jsonArray",
        "jsonObject",
        "oagitJsonEncode",
        "oagitWriteJsonFile",
        "oagitFail",
        "oagitDirHasLiveLocks",
        "getDirFiles",
        "*.cdslck*",
        "\\.lck$",
    ):
        assert required in source


def test_export_protocol_and_safety_primitives() -> None:
    source = SKILL_FILES["export"].read_text(encoding="utf-8")
    for required in (
        "oagitRunExport",
        "dbFindOpenCellViewByName",
        "dbOpenCellViewByType",
        '"r"',
        "modifiedButNotSaved",
        "cdfGetInstCDF",
        "cdfGetBaseCellCDF",
        "cdfFindParamByName",
        "cv~>terminals",
        "cv~>nets",
        "cv~>instances",
        "cv~>shapes",
        "pinFigIds",
        "oagitExportIsSolderDot",
        "oagitExportIsAttachedTextDisplay",
        "oagitExportInternalCellPropP",
        "oagitExportArray",
        "omissions",
        "route_method",
        "OAGIT_REQUEST_IL",
        "OAGIT_AUTO_EXIT",
    ):
        assert required in source
    assert 'dbOpenCellViewByType(lib cell view nil "w")' not in source
    assert "oagitExportOptionalNativeField" in source
    assert 'list("constraints" list(\'jsonObject))' not in source
    assert 'list("sheet" list(\'jsonObject))' not in source


def test_export_collects_pin_figures_before_filtering_instances() -> None:
    source = SKILL_FILES["export"].read_text(encoding="utf-8")
    build = source[source.index("procedure(oagitExportBuildResponse"):]
    assert build.index("pins = oagitExportPins(cv pinFigIds)") < build.index(
        "instances = oagitExportInstances(cv pinFigIds)"
    )


def test_restore_atomic_draft_publish_and_cdf_primitives() -> None:
    source = SKILL_FILES["restore"].read_text(encoding="utf-8")
    for required in (
        "oagitRunRestore",
        "allow_libraries",
        "disallowed_library",
        "live_lock",
        "target_exists",
        "draft_exists",
        "cdfGetInstCDF",
        "cdfGetBaseCellCDF",
        "cdfFindParamByName",
        "cdfUpdateInstParam",
        "cdfgData",
        "cdfgForm",
        "cdfgForm = nil",
        "formInitProc",
        "oagitRestoreInvokeCdfHook",
        "1.0e-9",
        "cdfParseFloatString",
        "evalstring",
        "schCreateInst",
        "schCreatePin",
        "schCreateWire",
        "schCreateWireLabel",
        "schCheck",
        "dbSave",
        "dbCopyCellView",
        "dbPurge",
        "ddDeleteObj",
        "cdf_parameter_error",
        "sch_check_failed",
        "connectivity_mismatch",
        '"schematic" "w"',
        "oagitRestoreCleanupDraft",
    ):
        assert required in source
    assert 'dbOpenCellViewByType(lib cell view "schematic" "w")' not in source
    assert "dbPurge(cv)" in source
    assert "ddDeleteObj(draftDd)" in source
    assert "purgeResult && car(purgeResult)" in source
    assert "deleteResult && car(deleteResult)" in source


def test_restore_fail_closed_and_preserves_net_and_property_types() -> None:
    source = SKILL_FILES["restore"].read_text(encoding="utf-8")
    assert "callbackResult && car(callbackResult)" in source
    assert "oagitRestoreSaveResult && car(oagitRestoreSaveResult)" in source
    assert "copyResult && car(copyResult)" in source
    assert "unless(numberp(converted)" in source
    assert "refreshedParam~>value" in source
    assert "record->net" in source
    assert "req->ports" in source
    assert "dbMakeNet(cv netName)" in source
    assert "propType" in source and '"boolean"' in source
