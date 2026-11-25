"""Tests for oa-git v0.2 CDF DerivedState, coupling rules, merge/diff separation, and annotations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from oa_git.diff import diff_schematics, format_diff_text
from oa_git.guards import verify_derived_freshness, verify_roundtrip
from oa_git.merge import merge_schematics
from oa_git.model import (
    AnnotationShape,
    CdfCouplingRule,
    CdfParameterError,
    CellViewRef,
    DerivedState,
    Instance,
    InstancePlacement,
    Net,
    Port,
    RoundtripMismatchError,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
)
from oa_git.snapshot import (
    SchematicViewHandler,
    build_schematic_snapshot,
    canonicalize_electrical,
    canonicalize_placement,
    compute_cdf_input_sha256,
    electrical_from_dict,
    electrical_to_dict,
    placement_from_dict,
    placement_to_dict,
    read_snapshot,
    render_electrical_yaml,
    render_placement_yaml,
    resolve_cdf_callback_order,
    write_snapshot,
)
from oa_git.view_contract import ViewHandler, get_view_handler, has_view_handler


CELLVIEW = CellViewRef("work", "amp", "schematic")

MOS_WIDTH_RULE = CdfCouplingRule(
    name="mos_width",
    members=("w", "fingers", "wf", "totalW"),
    primary=("w", "fingers"),
    derived=("wf", "totalW"),
    order=("w", "fingers"),
    dependencies={"w": (), "fingers": ("w",)},
)


def _mos_snapshot(
    *,
    w: str = "2u",
    fingers: int = 2,
    derived: DerivedState | None = None,
    derived_params: dict | None = None,
    annotations: tuple[AnnotationShape, ...] = (),
    param_types: dict[str, str] | None = None,
    extra_params: dict | None = None,
) -> SchematicSnapshot:
    params: dict[str, str | int | float | bool] = {"w": w, "fingers": fingers}
    types: dict[str, str] = (
        dict(param_types)
        if param_types is not None
        else {"w": "string", "fingers": "int"}
    )
    if extra_params:
        params.update(extra_params)
    inst = Instance(
        name="M1",
        lib="tsmcN65",
        cell="nch",
        view="symbol",
        connections={"D": "OUT", "G": "IN", "S": "VSS", "B": "VSS"},
        params=params,
        param_types=types,
        derived_params=derived_params or {},
        props={},
        derived=derived,
    )
    return build_schematic_snapshot(
        CELLVIEW,
        SchematicElectrical(
            ports=(Port("IN", "input"), Port("OUT", "output")),
            nets=(Net("IN"), Net("OUT"), Net("VSS", is_global=True)),
            instances=(inst,),
        ),
        SchematicPlacement(
            instances=(InstancePlacement("M1", (1.0, 2.0), "R0"),),
            annotations=annotations,
        ),
    )


# ---------------------------------------------------------------------------
# 1. CDF Input SHA-256 & Callback Topological Ordering
# ---------------------------------------------------------------------------


def test_compute_cdf_input_sha256_is_deterministic_and_sensitive() -> None:
    sha1 = compute_cdf_input_sha256(
        "tsmcN65",
        "nch",
        "symbol",
        {"w": "2u", "fingers": 2},
        {"w": "string", "fingers": "int"},
        coupling_rules=(MOS_WIDTH_RULE,),
    )
    sha2 = compute_cdf_input_sha256(
        "tsmcN65",
        "nch",
        "symbol",
        {"fingers": 2, "w": "2u"},
        {"fingers": "int", "w": "string"},
        coupling_rules=[MOS_WIDTH_RULE],
    )
    assert sha1 == sha2
    assert len(sha1) == 64

    # Sensitive to primary value, type, master, and coupling rules
    sha_diff_val = compute_cdf_input_sha256(
        "tsmcN65",
        "nch",
        "symbol",
        {"w": "4u", "fingers": 2},
        {"w": "string", "fingers": "int"},
        coupling_rules=(MOS_WIDTH_RULE,),
    )
    sha_diff_master = compute_cdf_input_sha256(
        "tsmcN65",
        "pch",
        "symbol",
        {"w": "2u", "fingers": 2},
        {"w": "string", "fingers": "int"},
        coupling_rules=(MOS_WIDTH_RULE,),
    )
    sha_no_rules = compute_cdf_input_sha256(
        "tsmcN65",
        "nch",
        "symbol",
        {"w": "2u", "fingers": 2},
        {"w": "string", "fingers": "int"},
    )
    assert len({sha1, sha_diff_val, sha_diff_master, sha_no_rules}) == 4


def test_resolve_cdf_callback_order_topological_and_tie_breakers() -> None:
    # Dependency fingers -> w overrides reverse declaration_order
    order = resolve_cdf_callback_order(
        ["fingers", "w", "l"],
        declaration_order=["fingers", "l", "w"],
        coupling_rules=(MOS_WIDTH_RULE,),
    )
    assert order.index("w") < order.index("fingers")
    # l has no dependency and precedes w in declaration_order
    assert order == ("l", "w", "fingers")

    # Tie-break by declaration_order, then rule.order, then param name
    no_dep_rule = CdfCouplingRule(
        name="res",
        members=("r", "l", "w"),
        primary=("r", "l", "w"),
        order=("r", "l", "w"),
        dependencies={},
    )
    assert resolve_cdf_callback_order(["w", "l", "r"], coupling_rules=(no_dep_rule,)) == (
        "r",
        "l",
        "w",
    )
    assert resolve_cdf_callback_order(
        ["w", "l", "r"], declaration_order=("w", "r"), coupling_rules=(no_dep_rule,)
    ) == ("w", "r", "l")
    assert resolve_cdf_callback_order(["z", "a", "m"]) == ("a", "m", "z")

    # Transitive dependency through an unrequested intermediate node
    chain_rule = CdfCouplingRule(
        name="chain",
        members=("a", "mid", "c"),
        primary=("a", "c"),
        dependencies={"mid": ("a",), "c": ("mid",)},
    )
    assert resolve_cdf_callback_order(
        ["c", "a"], declaration_order=("c", "a"), coupling_rules=(chain_rule,)
    ) == ("a", "c")


def test_resolve_cdf_callback_order_detects_cycles() -> None:
    cyclic_rule = CdfCouplingRule(
        name="bad_cycle",
        members=("a", "b", "c"),
        primary=("a", "b", "c"),
        dependencies={"a": ("c",), "b": ("a",), "c": ("b",)},
    )
    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        resolve_cdf_callback_order(["a", "b", "c"], coupling_rules=(cyclic_rule,))

    self_loop = CdfCouplingRule(
        name="self_loop",
        members=("w",),
        primary=("w",),
        dependencies={"w": ("w",)},
    )
    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        resolve_cdf_callback_order(["w"], coupling_rules=(self_loop,))


# ---------------------------------------------------------------------------
# 2. DerivedState v2 YAML Roundtrip, Legacy v1 Compatibility & Invariants
# ---------------------------------------------------------------------------


def test_derived_state_v2_yaml_roundtrip_and_no_legacy_key(tmp_path: Path) -> None:
    input_sha = compute_cdf_input_sha256(
        "tsmcN65",
        "nch",
        "symbol",
        {"w": "2u", "fingers": 2},
        {"w": "string", "fingers": "int"},
        coupling_rules=(MOS_WIDTH_RULE,),
    )
    valid_derived = DerivedState(
        status="valid",
        values={"wf": "1u", "totalW": "4u"},
        input_sha256=input_sha,
        callback_order=("w", "fingers"),
        source="cdf-readback",
    )
    snap = _mos_snapshot(derived=valid_derived)

    raw_yaml = render_electrical_yaml(snap.electrical)
    parsed = yaml.safe_load(raw_yaml)
    inst_dict = parsed["instances"][0]
    assert "derived" in inst_dict
    assert "derived_params" not in inst_dict
    assert inst_dict["derived"] == {
        "status": "valid",
        "values": {"totalW": "4u", "wf": "1u"},
        "input_sha256": input_sha,
        "callback_order": ["w", "fingers"],
        "source": "cdf-readback",
    }

    dest = write_snapshot(snap, tmp_path / "snap_v2")
    loaded = read_snapshot(dest, verify=True)
    assert loaded.electrical == snap.electrical
    assert loaded.electrical.instances[0].derived == valid_derived
    assert loaded.electrical.instances[0].derived_params == {"totalW": "4u", "wf": "1u"}

    # Stale & unavailable states roundtrip cleanly
    for state in (
        DerivedState(status="stale", values={}, input_sha256=None, callback_order=(), source="cdf-readback"),
        DerivedState(status="unavailable", values={}, input_sha256=None, callback_order=(), source="cdf-readback"),
    ):
        s = _mos_snapshot(derived=state)
        rt = electrical_from_dict(electrical_to_dict(s.electrical))
        assert rt.instances[0].derived == state
        assert rt.instances[0].derived_params == {}


def test_legacy_v1_derived_params_read_compat_and_fail_closed_invariants() -> None:
    legacy_payload = {
        "ports": [],
        "nets": [],
        "instances": [
            {
                "name": "M1",
                "lib": "tsmcN65",
                "cell": "nch",
                "view": "symbol",
                "connections": {},
                "params": {"w": "2u"},
                "param_types": {"w": "string"},
                "derived_params": {"wf": "1u"},
                "props": {},
            }
        ],
        "properties": {},
        "constraints": {},
    }
    parsed = electrical_from_dict(legacy_payload)
    inst = parsed.instances[0]
    assert inst.derived == DerivedState(
        status="valid",
        values={"wf": "1u"},
        input_sha256=None,
        callback_order=(),
        source="legacy-import",
    )
    assert inst.derived_params == {"wf": "1u"}

    # Empty legacy derived_params -> derived=None (clean v2 omission)
    legacy_empty = {
        **legacy_payload,
        "instances": [{**legacy_payload["instances"][0], "derived_params": {}}],
    }
    parsed_empty = electrical_from_dict(legacy_empty)
    assert parsed_empty.instances[0].derived is None
    assert "derived" not in electrical_to_dict(parsed_empty)["instances"][0]
    assert "derived_params" not in electrical_to_dict(parsed_empty)["instances"][0]

    # Reject BOTH derived and derived_params on the same instance
    both_payload = {
        **legacy_payload,
        "instances": [
            {
                **legacy_payload["instances"][0],
                "derived": {"status": "stale", "values": {}, "input_sha256": None},
            }
        ],
    }
    with pytest.raises(SchemaValidationError, match="derived"):
        electrical_from_dict(both_payload)

    # Disjointness invariant: params and derived.values MUST NOT share keys
    overlap_payload = {
        **legacy_payload,
        "instances": [
            {
                "name": "M1",
                "lib": "tsmcN65",
                "cell": "nch",
                "params": {"w": "2u"},
                "param_types": {"w": "string"},
                "derived": {"status": "valid", "values": {"w": "2u"}},
            }
        ],
    }
    with pytest.raises(SchemaValidationError, match="disjoint"):
        electrical_from_dict(overlap_payload)

    with pytest.raises(SchemaValidationError, match="disjoint"):
        canonicalize_electrical(
            SchematicElectrical(
                instances=(
                    Instance(
                        "M1",
                        "tsmcN65",
                        "nch",
                        params={"w": "2u"},
                        param_types={"w": "string"},
                        derived=DerivedState(status="valid", values={"w": "2u"}),
                    ),
                )
            )
        )

    # Stale / unavailable invariants enforced on YAML read
    bad_stale = {
        **legacy_payload,
        "instances": [
            {
                "name": "M1",
                "lib": "tsmcN65",
                "cell": "nch",
                "derived": {"status": "stale", "values": {"wf": "1u"}},
            }
        ],
    }
    with pytest.raises(SchemaValidationError):
        electrical_from_dict(bad_stale)


def test_verify_derived_freshness_reports_fresh_mismatched_stale_and_unavailable() -> None:
    rules_map = {"tsmcN65/nch": (MOS_WIDTH_RULE,)}
    sha = compute_cdf_input_sha256(
        "tsmcN65",
        "nch",
        "symbol",
        {"w": "2u", "fingers": 2},
        {"w": "string", "fingers": "int"},
        coupling_rules=(MOS_WIDTH_RULE,),
    )
    fresh_snap = _mos_snapshot(
        derived=DerivedState(
            status="valid",
            values={"wf": "1u"},
            input_sha256=sha,
            callback_order=("w", "fingers"),
            source="cdf-readback",
        )
    )
    report_fresh = verify_derived_freshness(fresh_snap, coupling_rules=rules_map)
    assert report_fresh["ok"] is True
    assert report_fresh["fresh"] == ["M1"]
    assert report_fresh["mismatched"] == []

    # Stale input_sha256 after primary parameter edit -> ok=False, reported without raising
    edited_snap = _mos_snapshot(
        w="4u",
        derived=DerivedState(
            status="valid",
            values={"wf": "1u"},
            input_sha256=sha,
            callback_order=("w", "fingers"),
            source="cdf-readback",
        ),
    )
    report_mismatch = verify_derived_freshness(edited_snap, coupling_rules=rules_map)
    assert report_mismatch["ok"] is False
    assert len(report_mismatch["mismatched"]) == 1
    assert report_mismatch["mismatched"][0]["instance"] == "M1"

    # Explicitly stale and unavailable -> reported cleanly with ok=True
    stale_snap = _mos_snapshot(derived=DerivedState(status="stale"))
    report_stale = verify_derived_freshness(stale_snap, coupling_rules=rules_map)
    assert report_stale["ok"] is True
    assert report_stale["stale"] == ["M1"]

    unavail_snap = _mos_snapshot(derived=None)
    report_unavail = verify_derived_freshness(unavail_snap, coupling_rules=rules_map)
    assert report_unavail["ok"] is True
    assert report_unavail["unavailable"] == ["M1"]


# ---------------------------------------------------------------------------
# 3. Derived-Aware Diff & Three-Way Merge
# ---------------------------------------------------------------------------


def test_diff_separates_derived_observations_from_electrical_truth() -> None:
    old = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "1u"}, source="cdf-readback")
    )
    new = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "1.01u"}, source="cdf-readback")
    )
    diff = diff_schematics(old, new)
    assert diff.has_electrical_changes is False
    assert diff.electrical.is_empty is True
    assert diff.has_derived_changes is True
    assert len(diff.derived_observation_changes) == 1
    assert diff.derived_observation_changes[0].param == "wf"
    assert diff.electrical.derived_observation_changes == diff.electrical.derived_param_changes
    assert "Derived observation changes" in format_diff_text(diff)


def test_merge_concurrent_w_and_fingers_produces_zero_conflicts_and_stale_derived() -> None:
    rules_map = {"tsmcN65/nch": (MOS_WIDTH_RULE,)}
    sha_base = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "2u", "fingers": 2}, {"w": "string", "fingers": "int"}, (MOS_WIDTH_RULE,)
    )
    sha_ours = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "4u", "fingers": 2}, {"w": "string", "fingers": "int"}, (MOS_WIDTH_RULE,)
    )
    sha_theirs = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "2u", "fingers": 4}, {"w": "string", "fingers": "int"}, (MOS_WIDTH_RULE,)
    )

    base = _mos_snapshot(
        w="2u",
        fingers=2,
        derived=DerivedState(
            status="valid",
            values={"wf": "1u", "totalW": "4u"},
            input_sha256=sha_base,
            callback_order=("w", "fingers"),
            source="cdf-readback",
        ),
    )
    ours = _mos_snapshot(
        w="4u",
        fingers=2,
        derived=DerivedState(
            status="valid",
            values={"wf": "2u", "totalW": "8u"},
            input_sha256=sha_ours,
            callback_order=("w", "fingers"),
            source="cdf-readback",
        ),
    )
    theirs = _mos_snapshot(
        w="2u",
        fingers=4,
        derived=DerivedState(
            status="valid",
            values={"wf": "500n", "totalW": "8u"},
            input_sha256=sha_theirs,
            callback_order=("w", "fingers"),
            source="cdf-readback",
        ),
    )

    result = merge_schematics(base, ours, theirs, coupling_rules=rules_map)
    assert result.ok is True
    assert result.conflicts == ()
    assert result.snapshot is not None
    merged_inst = result.snapshot.electrical.instances[0]
    assert merged_inst.params == {"fingers": 4, "w": "4u"}
    assert merged_inst.derived == DerivedState(
        status="stale",
        values={},
        input_sha256=None,
        callback_order=(),
        source="cdf-readback",
    )
    assert merged_inst.derived_params == {}


def test_merge_primary_vs_derived_and_derived_only_branches_never_conflict() -> None:
    sha_base = compute_cdf_input_sha256(
        "tsmcN65", "nch", "symbol", {"w": "2u", "fingers": 2}, {"w": "string", "fingers": "int"}
    )
    base = _mos_snapshot(
        w="2u",
        fingers=2,
        derived=DerivedState(
            status="valid",
            values={"wf": "1u"},
            input_sha256=sha_base,
            callback_order=("w", "fingers"),
            source="cdf-readback",
        ),
    )

    # One branch edits primary, other branch edits ONLY derived -> keep primary + stale
    ours_primary = _mos_snapshot(
        w="3u",
        fingers=2,
        derived=DerivedState(status="stale"),
    )
    theirs_derived = _mos_snapshot(
        w="2u",
        fingers=2,
        derived=DerivedState(
            status="valid",
            values={"wf": "1.05u"},
            input_sha256=sha_base,
            source="cdf-readback",
        ),
    )
    res_mixed = merge_schematics(base, ours_primary, theirs_derived)
    assert res_mixed.ok is True
    assert res_mixed.conflicts == ()
    assert res_mixed.snapshot is not None
    assert res_mixed.snapshot.electrical.instances[0].params["w"] == "3u"
    assert res_mixed.snapshot.electrical.instances[0].derived == DerivedState(
        status="stale", values={}, input_sha256=None, callback_order=(), source="cdf-readback"
    )

    # Both branches edit ONLY derived (different values) -> NO conflict, prefer valid base > ours > theirs
    ours_derived = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "1.1u"}, input_sha256=sha_base)
    )
    theirs_derived2 = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "1.2u"}, input_sha256=sha_base)
    )
    res_derived_only = merge_schematics(base, ours_derived, theirs_derived2)
    assert res_derived_only.ok is True
    assert res_derived_only.snapshot is not None
    assert res_derived_only.snapshot.electrical.instances[0].derived == base.electrical.instances[0].derived

    # Base stale -> prefer valid ours over valid theirs
    base_stale = _mos_snapshot(derived=DerivedState(status="stale"))
    res_prefer_ours = merge_schematics(base_stale, ours_derived, theirs_derived2)
    assert res_prefer_ours.ok is True
    assert res_prefer_ours.snapshot is not None
    assert res_prefer_ours.snapshot.electrical.instances[0].derived == ours_derived.electrical.instances[0].derived

    # Mismatched input_sha256 on all valid candidates -> stale, no conflict
    wrong_sha = "0" * 64
    base_wrong = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "1u"}, input_sha256=wrong_sha)
    )
    ours_wrong = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "2u"}, input_sha256=wrong_sha)
    )
    theirs_wrong = _mos_snapshot(
        derived=DerivedState(status="valid", values={"wf": "3u"}, input_sha256=wrong_sha)
    )
    res_wrong_sha = merge_schematics(base_wrong, ours_wrong, theirs_wrong)
    assert res_wrong_sha.ok is True
    assert res_wrong_sha.snapshot is not None
    assert res_wrong_sha.snapshot.electrical.instances[0].derived.status == "stale"

    # Same primary param edited differently -> conflict as before
    res_conflict = merge_schematics(
        base,
        _mos_snapshot(w="4u", derived=DerivedState(status="stale")),
        _mos_snapshot(w="6u", derived=DerivedState(status="stale")),
    )
    assert res_conflict.ok is False
    assert any(c.path == "instances.M1.params.w" for c in res_conflict.conflicts)


# ---------------------------------------------------------------------------
# 4. Annotation Shapes (Canonicalize, YAML, Diff, Merge, Roundtrip) & Handler
# ---------------------------------------------------------------------------


def test_annotation_shapes_canonicalize_yaml_diff_merge_and_roundtrip(tmp_path: Path) -> None:
    rect_unnorm = AnnotationShape(
        kind="rect",
        layer="text",
        purpose="drawing",
        points=((5.0000004, 4.0), (1.0, 2.0)),
        width=0.0,
    )
    line_rev = AnnotationShape(
        kind="line",
        layer="annotate",
        purpose="drawing",
        points=((3.0, 3.0), (0.0, 0.0)),
        width=0.05,
    )
    poly = AnnotationShape(
        kind="polygon",
        layer="device",
        purpose="drawing",
        points=((0.0, 0.0), (2.0, 0.0), (1.0, 2.0)),
        width=0.0,
    )

    canonical = canonicalize_placement(
        SchematicPlacement(annotations=(poly, line_rev, rect_unnorm))
    )
    assert len(canonical.annotations) == 3
    by_kind = {shape.kind: shape for shape in canonical.annotations}
    assert by_kind["rect"].points == ((1.0, 2.0), (5.0, 4.0))
    assert by_kind["line"].points == ((0.0, 0.0), (3.0, 3.0))
    assert by_kind["polygon"].points == ((0.0, 0.0), (2.0, 0.0), (1.0, 2.0))

    # Empty annotations omitted from YAML
    empty_yaml = yaml.safe_load(render_placement_yaml(SchematicPlacement()))
    assert "annotations" not in empty_yaml

    # Non-empty annotations roundtrip via disk snapshot
    snap = _mos_snapshot(annotations=(rect_unnorm, line_rev, poly))
    dest = write_snapshot(snap, tmp_path / "ann_snap")
    loaded = read_snapshot(dest, verify=True)
    assert loaded.placement.annotations == canonical.annotations
    assert placement_from_dict(placement_to_dict(snap.placement)) == snap.placement

    # Validation fail-closed
    for bad in (
        AnnotationShape(kind="circle", layer="text", points=((0, 0), (1, 1))),
        AnnotationShape(kind="rect", layer="text", points=((0, 0), (1, 1), (2, 2))),
        AnnotationShape(kind="line", layer="text", points=((0, 0),)),
        AnnotationShape(kind="polygon", layer="text", points=((0, 0), (1, 1))),
        AnnotationShape(kind="rect", layer="text", points=((0, 0), (1, 1)), width=-0.1),
    ):
        with pytest.raises(SchemaValidationError):
            canonicalize_placement(SchematicPlacement(annotations=(bad,)))

    # Diff reports annotations as placement-only
    base_snap = _mos_snapshot(annotations=(rect_unnorm,))
    added_snap = _mos_snapshot(annotations=(rect_unnorm, poly))
    d = diff_schematics(base_snap, added_snap)
    assert d.is_placement_only is True
    assert d.has_electrical_changes is False
    assert d.placement.annotations_added == (poly,)
    assert d.placement.annotations_removed == ()
    assert d.summary()["annotations_added"] == 1
    assert "Annotations added" in format_diff_text(d)

    # Merge merges disjoint annotation additions cleanly
    ours_ann = _mos_snapshot(annotations=(rect_unnorm, line_rev))
    theirs_ann = _mos_snapshot(annotations=(rect_unnorm, poly))
    merged = merge_schematics(base_snap, ours_ann, theirs_ann)
    assert merged.ok is True
    assert merged.snapshot is not None
    assert len(merged.snapshot.placement.annotations) == 3

    # Roundtrip accepts annotation jitter within coord_tol and rejects drift outside coord_tol
    within_tol = _mos_snapshot(
        annotations=(
            replace(by_kind["rect"], points=((5.00005, 4.0), (1.00005, 2.0))),
            replace(by_kind["line"], points=((3.0, 3.00005), (0.0, 0.0))),
            poly,
        )
    )
    assert verify_roundtrip(snap, within_tol, coord_tol=1e-4, raise_on_mismatch=False).ok

    drifted = _mos_snapshot(annotations=(rect_unnorm, poly))
    with pytest.raises(RoundtripMismatchError, match="annotations"):
        verify_roundtrip(snap, drifted)


def test_schematic_view_handler_registration_and_contract(tmp_path: Path) -> None:
    assert has_view_handler("schematic") is True
    handler = get_view_handler("schematic")
    assert isinstance(handler, SchematicViewHandler)
    assert isinstance(handler, ViewHandler)
    assert handler.view_type == "schematic"
    assert handler.capture_action == "capture_schematic"
    assert handler.restore_action == "restore_schematic"
    assert handler.source_file_candidates("SAR_ADC", "schematic") == (
        "SAR_ADC/schematic/sch.oa",
        "SAR_ADC/symbol/symbol.oa",
        "SAR_ADC/symbol/master.tag",
    )

    snap = _mos_snapshot()
    out = handler.write_snapshot(snap, tmp_path / "handler_snap")
    loaded = handler.read_snapshot(out, verify=True)
    assert loaded == snap
    assert handler.diff(snap, loaded).is_empty is True
    assert handler.merge(snap, snap, snap).ok is True


def test_legacy_v1_concurrent_primary_merge_marks_derived_stale() -> None:
    """Concurrent legacy v1 edits across both branches must invalidate derived to stale."""

    base = _mos_snapshot(w="2u", fingers=2, derived_params={"totalW": "4u"})
    # Ours edits w + totalW (legacy); theirs edits fingers (leaving totalW at base)
    ours = _mos_snapshot(w="4u", fingers=2, derived_params={"totalW": "8u"})
    theirs = _mos_snapshot(w="2u", fingers=4, derived_params={"totalW": "4u"})

    res = merge_schematics(base, ours, theirs)
    assert res.ok is True
    assert res.conflicts == ()
    assert res.snapshot is not None
    inst = res.snapshot.electrical.instances[0]
    assert inst.params == {"fingers": 4, "w": "4u"}
    assert inst.derived == DerivedState(
        status="stale",
        values={},
        input_sha256=None,
        callback_order=(),
        source="cdf-readback",
    )
    assert inst.derived_params == {}

    # Symmetric case: theirs edits fingers + totalW, ours edits w only -> also stale!
    ours_only_w = _mos_snapshot(w="4u", fingers=2, derived_params={"totalW": "4u"})
    theirs_f_and_der = _mos_snapshot(w="2u", fingers=4, derived_params={"totalW": "8u"})
    res_sym = merge_schematics(base, ours_only_w, theirs_f_and_der)
    assert res_sym.ok is True
    assert res_sym.snapshot is not None
    inst_sym = res_sym.snapshot.electrical.instances[0]
    assert inst_sym.derived == DerivedState(
        status="stale",
        values={},
        input_sha256=None,
        callback_order=(),
        source="cdf-readback",
    )
    assert inst_sym.derived_params == {}


def test_cdf_coupling_rule_schema_validation_and_acyclicity_everywhere(
    tmp_path: Path,
) -> None:
    from oa_git.project_state import write_project_config

    # 1) CdfCouplingRule.from_dict rejects malformed mappings, missing/empty name, bare strings (no char-splitting!)
    for bad_rule in (
        "not-a-mapping",
        [],
        {},  # missing name
        {"name": ""},  # empty name
        {"name": "   "},
        {"name": 123},
        {"name": "r1", "extra": 1},  # unknown key
        {"name": "r1", "members": "wf"},  # bare string!
        {"name": "r1", "primary": "w"},  # bare string!
        {"name": "r1", "derived": "totalW"},  # bare string!
        {"name": "r1", "order": "w"},  # bare string!
        {"name": "r1", "members": [1, 2]},  # non-str element
        {"name": "r1", "dependencies": "w"},  # non-mapping dependencies
        {"name": "r1", "dependencies": {"fingers": "w"}},  # bare string dep value!
        {"name": "r1", "dependencies": {"fingers": [123]}},  # non-str dep item
        {"name": "r1", "dependencies": {"": ["w"]}},  # empty dep key
    ):
        with pytest.raises(SchemaValidationError):
            CdfCouplingRule.from_dict(bad_rule)  # type: ignore[arg-type]

    # Valid rule parses cleanly
    good = CdfCouplingRule.from_dict(
        {
            "name": "mos_w",
            "members": ["w", "fingers", "wf"],
            "primary": ["w", "fingers"],
            "derived": ["wf"],
            "order": ["w", "fingers"],
            "dependencies": {"fingers": ["w"]},
        }
    )
    assert good.dependencies == {"fingers": ("w",)}

    # 2) Cyclic rules rejected with CdfParameterError('cdf_order_cycle') in project_state, merge, and guards
    cyclic_dict = {
        "name": "bad_cycle",
        "members": ["w", "fingers"],
        "primary": ["w", "fingers"],
        "dependencies": {"w": ["fingers"], "fingers": ["w"]},
    }
    cyclic_rule = CdfCouplingRule.from_dict(cyclic_dict)
    cyclic_map = {"tsmcN65/nch": (cyclic_rule,)}

    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        write_project_config(
            tmp_path / "proj_cycle",
            {
                "backend": "fixture",
                "cdf": {"max_passes": 3, "coupling_rules": {"tsmcN65/nch": [cyclic_dict]}},
            },
        )

    # Also malformed rule in config fails closed with SchemaValidationError
    with pytest.raises(SchemaValidationError):
        write_project_config(
            tmp_path / "proj_bad_rule",
            {
                "backend": "fixture",
                "cdf": {"coupling_rules": {"tsmcN65/nch": [{"name": "r", "primary": "w"}]}},
            },
        )

    snap = _mos_snapshot()
    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        merge_schematics(snap, snap, snap, coupling_rules=cyclic_map)

    with pytest.raises(CdfParameterError, match="cdf_order_cycle"):
        verify_derived_freshness(snap, coupling_rules=cyclic_map)
