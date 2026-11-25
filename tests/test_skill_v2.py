"""Offline contract checks for oa-git v0.2 schematic SKILL root-cause fixes."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from oa_git.bridge import (
    _SKILL_SYMBOL_RE,
    render_restore_request_skill,
    to_skill_literal,
)
from oa_git.model import (
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    Port,
    SchematicElectrical,
    SchematicPlacement,
)
from oa_git.snapshot import build_schematic_snapshot


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "oa_git" / "skill"
V2_SKILL_FILES = {
    "export": SKILL_DIR / "export_schematic.il",
    "restore": SKILL_DIR / "restore_schematic.il",
}
ALL_ACTION_SKILL_FILES = (
    SKILL_DIR / "export_schematic.il",
    SKILL_DIR / "restore_schematic.il",
    SKILL_DIR / "export_symbol.il",
    SKILL_DIR / "restore_symbol.il",
    SKILL_DIR / "batch_capture.il",
    SKILL_DIR / "batch_restore.il",
)

IGNORED_PDK_METADATA_KEYS = (
    "lxComponentType",
    "description",
    "pdkVersion",
    "macroArgumentStyle",
    "pasUpdateParamList",
    "lxIgnoredParamsForCAS",
    "modelLabelSet",
    "opPointLabelSet",
    "paramLabelSet",
    "lxStopList",
    "lxUseCell",
)

V2_INTERNAL_CELL_PROPS = (
    "instancesLastChanged",
    "interfaceLastChanged",
    "connectivityLastChanged",
    "schLastExtracted",
    "instancesLastModified",
)


def _lex_and_check_parentheses(source: str) -> str:
    """Validate SKILL parenthesis balance and return code stripped of strings/comments."""

    depth = 0
    state = "normal"
    escaped = False
    index = 0
    stripped: list[str] = []

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
                stripped.append(char)
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
            stripped.append('""')
        elif char == ";":
            state = "line_comment"
        elif char == "/" and next_char == "*":
            state = "block_comment"
            index += 1
        else:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise AssertionError(
                        f"unmatched closing parenthesis at offset {index}"
                    )
            stripped.append(char)
        index += 1

    assert state == "normal", f"unterminated SKILL {state}"
    assert depth == 0, f"unbalanced SKILL parentheses, depth={depth}"
    return "".join(stripped)


@pytest.mark.parametrize("path", V2_SKILL_FILES.values(), ids=lambda p: p.name)
def test_v2_skill_scripts_strict_lexer_parenthesis_balance(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    stripped = _lex_and_check_parentheses(source)
    assert re.search(r"cond\s*\(\s*\(\s*nil\b", stripped) is None, (
        f"{path.name} contains forbidden cond((nil ...)) construct"
    )


@pytest.mark.parametrize("path", V2_SKILL_FILES.values(), ids=lambda p: p.name)
def test_v2_skill_scripts_zero_forbidden_apis(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for forbidden in (
        "schHi",
        "cdfGetParam",
        "cdfGetParameterList",
        "dbReplaceProp",
        "stringToChar",
    ):
        assert forbidden not in source, (
            f"{path.name} contains forbidden API token {forbidden!r}"
        )


def test_export_v2_dynamic_editable_and_static_pdk_filter() -> None:
    source = V2_SKILL_FILES["export"].read_text(encoding="utf-8")

    for required in (
        "procedure(oagitExportIgnoredCdfParamP",
        "procedure(oagitExportInvokeCdfHook",
        "procedure(oagitExportEditableEvalP",
        "deGetEditCellView",
        '!=.*\\"maskLayout\\"',
        '==.*\\"schematic\\"',
        '==.*\\"maskLayout\\"',
        "iPDK_isSchematic()",
        "evalstring(editableExpr)",
        "formInitProc",
        "cdfgData = instCdf",
        "cdfgForm = nil",
        '"param_order"',
        "oagitExportArray(paramOrder)",
    ):
        assert required in source, f"export_schematic.il missing {required!r}"

    for ignored_key in IGNORED_PDK_METADATA_KEYS:
        assert f'"{ignored_key}"' in source, (
            f"export_schematic.il missing ignored PDK key {ignored_key!r}"
        )

    cdf_proc = source[source.index("procedure(oagitExportCdfData") :]
    assert cdf_proc.index("cdfgData = instCdf") < cdf_proc.index(
        "foreach(cdfParam instCdf~>parameters"
    )
    assert cdf_proc.index("formInitProc") < cdf_proc.index(
        "foreach(cdfParam instCdf~>parameters"
    )
    assert cdf_proc.index("oagitExportIgnoredCdfParamP") < cdf_proc.index(
        "oagitExportEditableEvalP"
    )


def test_export_v2_internal_props_and_annotations() -> None:
    source = V2_SKILL_FILES["export"].read_text(encoding="utf-8")

    prop_section = source[
        source.index("procedure(oagitExportInternalCellPropP") : source.index(
            "procedure(oagitExportPropOmission"
        )
    ]
    for prop_name in V2_INTERNAL_CELL_PROPS:
        assert f'"{prop_name}"' in prop_section, (
            f"oagitExportInternalCellPropP missing {prop_name!r}"
        )
    assert "*" not in prop_section, "internal cell prop filter must not use wildcards"

    for required in (
        "procedure(oagitExportIsAnnotationShape",
        "procedure(oagitExportAnnotation",
        'list("rect" "polygon" "line")',
        "shape~>bBox",
        "shape~>points",
        '"annotations"',
        "oagitExportArray(cadddr(geometry))",
    ):
        assert required in source, f"export_schematic.il missing {required!r}"

    geom_proc = source[
        source.index("procedure(oagitExportGeometry") : source.index(
            "procedure(oagitExportInternalCellPropP"
        )
    ]
    assert geom_proc.index("oagitExportIsWireShape(shape)") < geom_proc.index(
        "oagitExportIsAnnotationShape(shape)"
    )
    assert geom_proc.index("oagitExportIsAnnotationShape(shape)") < geom_proc.index(
        "oagitExportIsLineOrPath(shape)"
    )


def test_restore_v2_cross_instance_cdf_reset_and_multipass_convergence() -> None:
    source = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")

    for required in (
        "procedure(oagitRestoreIgnoredCdfParamP",
        "procedure(oagitRestoreEditableEvalP",
        "procedure(oagitRestoreResetInstCdf",
        "baseParam~>defValue",
        'equal(pType "button")',
        "procedure(oagitRestoreOrderParams",
        "record->param_order",
        "procedure(oagitRestoreSnapshotEditable",
        "procedure(oagitRestoreSnapshotsEquivP",
        "procedure(oagitRestoreCollectDerived",
        "req->max_passes",
        "req->recompute_derived",
        '"cdf_contradiction"',
        '"cdf_nonconvergent"',
        '"cdf_passes"',
        '"callback_order"',
        '"derived_refresh"',
        '"derived_status"',
        "cons('jsonArray lastExecutedCallbacks)",
        "cons('jsonObject reverse(oagitRestoreCallbackOrder))",
        "cons('jsonObject reverse(oagitRestoreDerivedRefresh))",
    ):
        assert required in source, f"restore_schematic.il missing {required!r}"

    for ignored_key in IGNORED_PDK_METADATA_KEYS:
        assert f'"{ignored_key}"' in source, (
            f"restore_schematic.il missing ignored PDK key {ignored_key!r}"
        )

    apply_proc = source[
        source.index("procedure(oagitRestoreApplyCdf") : source.index(
            "procedure(oagitRestoreCreateInstances"
        )
    ]
    assert apply_proc.index("cdfgData = instCdf") < apply_proc.index(
        "oagitRestoreResetInstCdf(instCdf baseCdf)"
    )
    assert apply_proc.index("cdfgForm = nil") < apply_proc.index(
        "oagitRestoreResetInstCdf(instCdf baseCdf)"
    )
    assert apply_proc.index("oagitRestoreResetInstCdf(instCdf baseCdf)") < apply_proc.index(
        "oagitRestoreInvokeCdfHook(formInit instCdf)"
    )
    assert apply_proc.index("oagitRestoreResetInstCdf(instCdf baseCdf)") < apply_proc.index(
        "while(pass <= maxPasses"
    )


def test_restore_v2_anonymous_net_bijection_and_annotations() -> None:
    source = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")

    for required in (
        "procedure(oagitRestoreCreateAnnotations",
        "dbCreateRect(cv list(layer purpose) bBox)",
        "dbCreatePolygon(cv list(layer purpose) points)",
        "dbCreateLine(cv list(layer purpose) points)",
        '"annotation_create_failed"',
        "procedure(oagitRestoreBuildExplicitNetTable",
        "procedure(oagitRestoreIsAnonNetP",
        '"^net[0-9]+$"',
        "expToAct",
        "actToExp",
        '"anon_net_split"',
        '"anon_net_short"',
        '"missing_terminal"',
        '"unexpected_terminal"',
        '"connectivity_mismatch"',
    ):
        assert required in source, f"restore_schematic.il missing {required!r}"

    build_proc = source[
        source.index("procedure(oagitRestoreBuild") : source.index(
            "procedure(oagitRunRestore"
        )
    ]
    assert build_proc.index("oagitRestoreCreateAnnotations(cv req->annotations)") < (
        build_proc.index("schCheck(cv)")
    )
    assert "oagitRestoreVerifyConnectivity(req->instances instByName req)" in build_proc


def test_restore_collect_derived_uses_local_ptype_not_dynamic_paramtype() -> None:
    source = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")
    collect_proc = source[
        source.index("procedure(oagitRestoreCollectDerived") : source.index(
            "procedure(oagitRestoreApplyCdf"
        )
    ]
    assert "oagitRestoreExportCdfValue(current pType)" in collect_proc
    assert "paramType" not in collect_proc


def test_restore_validates_dynamic_editability_after_applying_primaries_before_callbacks() -> None:
    source = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")
    apply_proc = source[
        source.index("procedure(oagitRestoreApplyCdf") : source.index(
            "procedure(oagitRestoreCreateInstances"
        )
    ]
    apply_primaries_pos = apply_proc.index("instParam~>value = cadr(spec)")
    dynamic_edit_pos = apply_proc.index(
        "editableOk = oagitRestoreEditableEvalP(instParam~>editable inst)"
    )
    callback_exec_pos = apply_proc.index("evalstring(instParam~>callback)")
    assert apply_primaries_pos < dynamic_edit_pos < callback_exec_pos
    assert "dynamically read-only" in apply_proc


def test_restore_scripts_close_and_purge_published_cellview_handle() -> None:
    for script_name in ("restore_schematic.il", "restore_symbol.il"):
        source = (SKILL_DIR / script_name).read_text(encoding="utf-8")
        _lex_and_check_parentheses(source)
        assert "pubCv = car(copyResult)" in source, f"{script_name} must capture pubCv"
        assert "errset(dbClose(pubCv) t)" in source, f"{script_name} must dbClose(pubCv)"
        assert "errset(dbPurge(pubCv) t)" in source, f"{script_name} must dbPurge(pubCv)"


def test_export_points_uses_while_loop_instead_of_invalid_4_arg_skill_for() -> None:
    for script_name, proc_name in (
        ("export_schematic.il", "procedure(oagitExportPoints"),
        ("export_symbol.il", "procedure(oagitSymExpPoints"),
    ):
        source = (SKILL_DIR / script_name).read_text(encoding="utf-8")
        _lex_and_check_parentheses(source)
        proc = source[source.index(proc_name) : source.index(proc_name) + 600]
        assert "for(" not in proc, f"{script_name} still contains invalid 4-arg SKILL for-loop"
        assert "while(" in proc
        assert "+ 2" in proc


def test_stage4_live_ic261_fixes_in_skill_scripts() -> None:
    """Lock in real IC26.1 fixes verified live on TSMC28 & TSMC65 complex circuits."""

    export_sch = V2_SKILL_FILES["export"].read_text(encoding="utf-8")
    restore_sch = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")
    export_sym = (SKILL_DIR / "export_symbol.il").read_text(encoding="utf-8")

    for src in (export_sch, restore_sch, export_sym):
        _lex_and_check_parentheses(src)

    # Derived parameters must omit unchanged default layout/DFM parameters (keep differs || stored || model)
    assert '(!writable && (differs || stored || equal(pName "model")) &&' in export_sch
    assert "procedure(oagitRestoreFindProp" in restore_sch
    collect_proc = restore_sch[
        restore_sch.index("procedure(oagitRestoreCollectDerived") : restore_sch.index(
            "procedure(oagitRestoreApplyCdf"
        )
    ]
    assert 'when(!editableOk && (differs || stored || equal(pName "model")) &&' in collect_proc
    assert "oagitRestoreFindProp(inst~>prop pName)" in collect_proc

    # master_open_failed must report the exact missing lib/cell/view and instance name
    assert '"unable to open instance master %s/%s/%s for %s"' in restore_sch

    # IC26.1 OA arc shapes expose bBox (not arcBBox) alongside ellipseBBox
    assert 'if(shape~>arcBBox shape~>arcBBox shape~>bBox)' in export_sym
    assert 'shape~>ellipseBBox' in export_sym

    # Cadence PAS headless callbacks require PasCdf* formInitProc before callback evaluation
    assert 'index(oagitRestoreString(formInit) "PasCdf")' in restore_sch


@pytest.mark.parametrize("path", ALL_ACTION_SKILL_FILES, ids=lambda p: p.name)
def test_all_action_scripts_guard_auto_exit_on_output_file_existence(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    _lex_and_check_parentheses(source)
    tail = source[-300:]
    assert 'equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1")' in tail
    assert "isFile(oagitRequest->output_path)" in tail


def test_skill_symbol_regex_quotes_bus_and_operator_names_as_strings() -> None:
    # Valid IC26.1 bare SKILL symbols
    for valid in ("w", "fingers", "sd_param", "calcParam_PPE", "_priv", "flag?"):
        assert _SKILL_SYMBOL_RE.fullmatch(valid), f"{valid!r} should match _SKILL_SYMBOL_RE"

    # Real IC26.1 bus/pin/net/operator names that MUST be emitted as quoted SKILL strings
    bus_and_op_names = (
        "DOUTP<9:0>",
        "IN+",
        "IN-",
        "VDD!",
        "a.b",
        "x*y",
        "p/n",
        "k=1",
        "sig~b",
    )
    for name in bus_and_op_names:
        assert not _SKILL_SYMBOL_RE.fullmatch(name), f"{name!r} must NOT be bare SKILL symbol"
        lit = to_skill_literal({name: "v"})
        assert f'"{name}" "v"' in lit, f"expected quoted key for {name!r}, got {lit!r}"

    cv = CellViewRef("work", "adc", "schematic")
    snap = build_schematic_snapshot(
        cv,
        SchematicElectrical(
            ports=(Port("DOUTP<9:0>", "output"), Port("IN+", "input"), Port("IN-", "input")),
            nets=(Net("DOUTP<9:0>"), Net("IN+"), Net("IN-"), Net("VDD!", is_global=True), Net("a.b")),
            instances=(
                Instance(
                    name="I0",
                    lib="work",
                    cell="sar",
                    connections={
                        "DOUTP<9:0>": "DOUTP<9:0>",
                        "IN+": "IN+",
                        "IN-": "IN-",
                        "VDD!": "VDD!",
                        "a.b": "a.b",
                    },
                    params={"w": "2u"},
                    param_types={"w": "string"},
                ),
            ),
        ),
        SchematicPlacement(
            instances=(InstancePlacement("I0", (0.0, 0.0), "R0"),),
        ),
    )
    rendered = render_restore_request_skill(
        snap,
        CellViewRef("sandbox", "adc", "schematic"),
        draft_cell="adc__draft",
        overwrite=True,
        allow_libraries=("sandbox",),
        output_path=Path("/tmp/adc_restore.json"),
    )
    _lex_and_check_parentheses(rendered)
    for expected in (
        '("DOUTP<9:0>" "DOUTP<9:0>")',
        '("IN+" "IN+")',
        '("IN-" "IN-")',
        '("VDD!" "VDD!")',
        '("a.b" "a.b")',
    ):
        assert expected in rendered, f"missing quoted connection pair {expected!r}"


def test_restore_v2_multipass_reinvokes_form_init_proc() -> None:
    """Bug #1: SMIC55 PAS multi-pass must re-run formInitProc when pass > 1."""

    source = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")
    _lex_and_check_parentheses(source)
    apply_proc = source[
        source.index("procedure(oagitRestoreApplyCdf") : source.index(
            "procedure(oagitRestoreCreateInstances"
        )
    ]
    loop_body = apply_proc[apply_proc.index("while(pass <= maxPasses") :]

    pass_gt1_pos = loop_body.index("when(pass > 1 && instCdf")
    reinit_hook_pos = loop_body.index("oagitRestoreInvokeCdfHook(formInit instCdf)")
    apply_primaries_pos = loop_body.index("instParam~>value = cadr(spec)")
    callback_pos = loop_body.index("evalstring(instParam~>callback)")

    assert pass_gt1_pos < reinit_hook_pos < apply_primaries_pos < callback_pos
    assert "cdfgData = instCdf" in loop_body[:reinit_hook_pos]
    assert "cdfgForm = nil" in loop_body[:reinit_hook_pos]
    assert "CDF formInitProc failed on pass %d" in loop_body
    assert "unless(oagitRestoreErrorCode" in loop_body[reinit_hook_pos:apply_primaries_pos]


def test_restore_v2_wide_bus_wires_pass_width_and_fail_closed() -> None:
    """Bug #2: Real wide bus wires pass wireWidth to schCreateWire and verify width Fail-Closed."""

    source = V2_SKILL_FILES["restore"].read_text(encoding="utf-8")
    _lex_and_check_parentheses(source)
    wires_proc = source[
        source.index("procedure(oagitRestoreCreateWires") : source.index(
            "procedure(oagitRestoreCreateLabels"
        )
    ]

    assert "wireWidth" in wires_proc
    assert "schCreateWire(cv \"draw\" routeMethod record->points 0 0 wireWidth)" in wires_proc
    assert "schCreateWire(cv \"draw\" routeMethod record->points 0 0 0)" not in wires_proc
    assert 'equal(wire~>objType "path")' in wires_proc
    assert "wire~>width = wireWidth" in wires_proc
    assert "numberp(wire~>width) && abs(wire~>width - wireWidth) <= 1.0e-6" in wires_proc
    assert '"non-zero wire width cannot be applied to non-path wire"' in wires_proc


def test_symbol_skill_illist_property_export_and_restore() -> None:
    """Bug #4: Symbol SKILL scripts support scalar ILList properties (e.g. termOrder) Fail-Closed."""

    export_sym = (SKILL_DIR / "export_symbol.il").read_text(encoding="utf-8")
    restore_sym = (SKILL_DIR / "restore_symbol.il").read_text(encoding="utf-8")
    _lex_and_check_parentheses(export_sym)
    _lex_and_check_parentheses(restore_sym)

    # export_symbol.il: checks scalar list & exports JSON array, keeps portOrder separate & omissions Fail-Closed
    assert "procedure(oagitSymExpScalarListP" in export_sym
    assert "procedure(oagitSymExpScalarListValue" in export_sym
    exp_props = export_sym[
        export_sym.index("procedure(oagitSymExpProperties") : export_sym.index(
            "procedure(oagitSymExpClose"
        )
    ]
    assert 'equal(name "portOrder") && equal(pType "ILList")' in exp_props
    assert 'equal(pType "ILList") && oagitSymExpScalarListP(prop~>value)' in exp_props
    assert "oagitSymExpScalarListValue(prop~>value)" in exp_props
    assert '"unsupported_property_type"' in exp_props
    assert exp_props.index('equal(name "portOrder")') < exp_props.index(
        "oagitSymExpScalarListP(prop~>value)"
    )

    # restore_symbol.il: validates scalar ILList (including empty nil list) and writes via dbCreateProp
    assert "procedure(oagitSymResScalarListP" in restore_sym
    res_props = restore_sym[
        restore_sym.index("procedure(oagitSymResCreateProperties") : restore_sym.index(
            "procedure(oagitSymResMismatch"
        )
    ]
    assert 'list("string" "int" "float" "boolean" "ILList")' in res_props
    assert "oagitSymResScalarListP(pVal)" in res_props
    assert "dbCreateProp(cv pName pType converted)" in res_props
