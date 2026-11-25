"""Offline unit tests for layout_diff.py and layout_merge.py."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from oa_git.layout_diff import (
    LayoutInstConnectivityChange,
    diff_layouts,
    format_layout_diff_stat,
    format_layout_diff_text,
    is_auxiliary_shape,
)
from oa_git.layout_merge import (
    LayoutMergeConflict,
    LayoutMergeResult,
    format_layout_merge_conflicts,
    merge_layouts,
)
from oa_git.layout_model import (
    CellViewRef,
    LayoutBlockage,
    LayoutBoundary,
    LayoutEllipse,
    LayoutFigGroup,
    LayoutInstance,
    LayoutLabel,
    LayoutLine,
    LayoutMosaic,
    LayoutNet,
    LayoutObject,
    LayoutPath,
    LayoutPathSeg,
    LayoutPin,
    LayoutPolygon,
    LayoutRect,
    LayoutSnapshot,
    LayoutVia,
    OmissionRecord,
    SchemaValidationError,
)
from oa_git.layout_snapshot import build_layout_snapshot

CV = CellViewRef("work", "inv2t", "layout")


def _snap(
    objects: tuple[LayoutObject, ...] = (),
    *,
    cellview: CellViewRef = CV,
    cell_properties: dict | None = None,
    omissions: tuple[OmissionRecord, ...] = (),
    metadata: dict | None = None,
) -> LayoutSnapshot:
    return build_layout_snapshot(
        cellview,
        objects,
        cell_properties=cell_properties,
        omissions=omissions,
        metadata=metadata,
    )


def _base_objects() -> tuple[LayoutObject, ...]:
    return (
        LayoutNet(name="VDD", is_global=False, num_bits=1, sig_type="power"),
        LayoutNet(name="VSS", is_global=False, num_bits=1, sig_type="ground"),
        LayoutNet(name="IN", is_global=False, num_bits=1, sig_type="signal"),
        LayoutNet(name="OUT", is_global=False, num_bits=1, sig_type="signal"),
        LayoutPin(
            name="IN",
            pin_name="P_IN",
            net="IN",
            direction="input",
            layer="M1",
            purpose="pin",
            shape_kind="rect",
            bbox=((0.0, 0.0), (0.2, 0.2)),
            access_dir=("left",),
        ),
        LayoutInstance(
            name="MP0",
            lib="pdk",
            cell="pch",
            view="layout",
            xy=(0.0, 2.0),
            orient="R0",
            is_pcell=True,
            pcell_params={"w": "2u", "l": "60n", "fingers": 2},
            pcell_param_types={"w": "string", "l": "string", "fingers": "int"},
            connections={"D": "OUT", "G": "IN", "S": "VDD", "B": "VDD"},
            props={"role": "pullup"},
        ),
        LayoutMosaic(
            name="CAP_ARR",
            lib="work",
            cell="ucap",
            view="layout",
            xy=(5.0, 0.0),
            orient="R0",
            rows=2,
            columns=2,
            u_x=1.0,
            u_y=1.0,
            is_pcell=True,
            pcell_params={"c": "10f"},
            pcell_param_types={"c": "string"},
        ),
        LayoutVia(
            via_def="M2_M1",
            origin=(1.0, 2.0),
            orient="R0",
            net="VDD",
            params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.04]},
        ),
        LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 0.2)), net="IN"),
        LayoutBoundary(
            boundary_kind="prBoundary",
            points=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)),
        ),
        LayoutFigGroup(name="PAIR", origin=(0.0, 0.0), orient="R0"),
        LayoutLabel(text="IN", xy=(0.1, 0.1), layer="M1", purpose="pin", net="IN"),
    )


# ---------------------------------------------------------------------------
# Task 1: 3-Tier Semantic Layout Diff Tests
# ---------------------------------------------------------------------------


def test_empty_diff_is_deterministic_and_json_serializable() -> None:
    snap_a = _snap(_base_objects(), cell_properties={"rule": "std"})
    snap_b = _snap(tuple(reversed(_base_objects())), cell_properties={"rule": "std"})

    diff = diff_layouts(snap_a, snap_b)

    assert diff.is_empty
    assert not diff.has_changes
    assert not diff.has_connectivity_changes
    assert not diff.has_geometry_changes
    assert not diff.has_annotation_changes
    assert diff.geometry.layer_deltas == {}

    as_dict = diff.to_dict()
    assert list(as_dict.keys()) == sorted(as_dict.keys())
    json.dumps(as_dict)
    assert format_layout_diff_text(diff) == (
        "work/inv2t/layout -> work/inv2t/layout\n"
        "Connectivity changes\n"
        "  none\n"
        "Geometry changes\n"
        "  none\n"
        "Annotation changes\n"
        "  none"
    )


def test_pure_geometry_move_and_routing_have_no_connectivity_or_annotation_changes() -> None:
    old = _snap(_base_objects())
    objs = list(_base_objects())
    # Move existing instance MP0 and mosaic CAP_ARR, replace M1 rect, add M1 path_seg & M2 rect,
    # move via to new location, update prBoundary & fig_group, add blockage.
    updated: list[LayoutObject] = []
    for obj in objs:
        if isinstance(obj, LayoutInstance) and obj.name == "MP0":
            updated.append(replace(obj, xy=(1.5, 3.0), orient="MY"))
        elif isinstance(obj, LayoutMosaic) and obj.name == "CAP_ARR":
            updated.append(replace(obj, xy=(6.0, 1.0), orient="R90"))
        elif isinstance(obj, LayoutVia):
            updated.append(replace(obj, origin=(3.0, 2.0)))
        elif isinstance(obj, LayoutRect) and obj.layer == "M1":
            updated.append(replace(obj, bbox=((0.0, 0.0), (2.0, 0.2))))
        elif isinstance(obj, LayoutBoundary) and obj.boundary_kind == "prBoundary":
            updated.append(
                replace(
                    obj,
                    points=((0.0, 0.0), (12.0, 0.0), (12.0, 10.0), (0.0, 10.0)),
                )
            )
        elif isinstance(obj, LayoutFigGroup) and obj.name == "PAIR":
            updated.append(replace(obj, origin=(1.0, 1.0)))
        else:
            updated.append(obj)
    updated.append(
        LayoutPathSeg(
            layer="M1",
            purpose="drawing",
            begin_pt=(2.0, 0.1),
            end_pt=(4.0, 0.1),
            width=0.2,
            net="IN",
        )
    )
    updated.append(
        LayoutRect(layer="M2", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0)), net="VDD")
    )
    updated.append(
        LayoutBlockage(
            blockage_type="routing",
            layer="M2",
            points=((4.0, 4.0), (5.0, 4.0), (5.0, 5.0), (4.0, 5.0)),
        )
    )

    new = _snap(tuple(updated))
    diff = diff_layouts(old, new)

    assert diff.has_changes
    assert not diff.has_connectivity_changes
    assert diff.has_geometry_changes
    assert not diff.has_annotation_changes
    assert len(diff.geometry.instance_moves) == 1
    assert diff.geometry.instance_moves[0].name == "MP0"
    assert diff.geometry.instance_moves[0].new_xy == (1.5, 3.0)
    assert diff.geometry.instance_moves[0].new_orient == "MY"
    assert len(diff.geometry.mosaic_moves) == 1
    assert diff.geometry.mosaic_moves[0].name == "CAP_ARR"
    assert len(diff.geometry.vias_added) == 1
    assert len(diff.geometry.vias_removed) == 1
    assert len(diff.geometry.blockages_added) == 1
    assert len(diff.geometry.boundaries_modified) == 1
    assert diff.geometry.boundaries_modified[0].key == "prBoundary"
    assert len(diff.geometry.fig_groups_modified) == 1
    assert diff.geometry.layer_deltas == {
        "M1:drawing": {"added": 2, "removed": 1},
        "M2:drawing": {"added": 1, "removed": 0},
    }


def test_connectivity_changes_pcell_pin_net_instterm_mosaic_and_via() -> None:
    old = _snap(_base_objects(), cell_properties={"simMode": True, "gridSpacing": 0.005})

    updated: list[LayoutObject] = []
    for obj in _base_objects():
        if isinstance(obj, LayoutNet) and obj.name == "VSS":
            continue  # removed net
        elif isinstance(obj, LayoutNet) and obj.name == "IN":
            updated.append(replace(obj, num_bits=2, sig_type="clock"))
        elif isinstance(obj, LayoutPin) and obj.name == "IN":
            updated.append(replace(obj, direction="inputOutput", net="OUT"))
        elif isinstance(obj, LayoutInstance) and obj.name == "MP0":
            updated.append(
                replace(
                    obj,
                    cell="pch_hvt",
                    pcell_params={"w": "4u", "l": "60n", "fingers": True},
                    pcell_param_types={"w": "string", "l": "string", "fingers": "boolean"},
                    connections={"D": "OUT", "G": "CLK", "S": "VDD", "B": "VDD"},
                    props={"role": "driver", "lxComponentType": "IGNORED_PDK_JUNK"},
                )
            )
        elif isinstance(obj, LayoutMosaic) and obj.name == "CAP_ARR":
            updated.append(
                replace(
                    obj,
                    rows=4,
                    pcell_params={"c": "20f"},
                    pcell_param_types={"c": "string"},
                )
            )
        elif isinstance(obj, LayoutVia):
            # Same (origin, orient), changed cutRows and net -> Tier 1 via_connectivity_changes
            updated.append(
                replace(
                    obj,
                    net="OUT",
                    params={"cutRows": 3, "cutColumns": 1, "layer1Enc": [0.03, 0.04]},
                )
            )
        else:
            updated.append(obj)

    updated.append(LayoutNet(name="CLK", sig_type="clock"))
    updated.append(LayoutInstance(name="MN0", lib="pdk", cell="nch", xy=(0.0, 0.0)))

    new = _snap(tuple(updated), cell_properties={"simMode": 1, "extra": "yes"})
    diff = diff_layouts(old, new)

    assert diff.has_connectivity_changes
    assert not diff.has_geometry_changes
    assert not diff.has_annotation_changes

    conn = diff.connectivity
    assert {(n.name, n.change_type) for n in conn.nets} == {
        ("CLK", "added"),
        ("IN", "modified"),
        ("VSS", "removed"),
    }
    assert len(conn.pins) == 1
    assert conn.pins[0].key == "IN:P_IN"
    assert conn.pins[0].change_type == "modified"
    assert set(conn.pins[0].changed_fields) == {"direction", "net"}
    assert conn.instances_added == ("MN0",)
    assert conn.instances_removed == ()
    assert len(conn.master_changes) == 1
    assert conn.master_changes[0].old_master == ("pdk", "pch", "layout")
    assert conn.master_changes[0].new_master == ("pdk", "pch_hvt", "layout")

    pcell_by_key = {(c.target_kind, c.instance, c.param): c for c in conn.pcell_param_changes}
    assert pcell_by_key[("instance", "MP0", "w")].old == "2u"
    assert pcell_by_key[("instance", "MP0", "w")].new == "4u"
    assert pcell_by_key[("instance", "MP0", "fingers")].old == 2
    assert pcell_by_key[("instance", "MP0", "fingers")].new is True
    assert pcell_by_key[("mosaic", "CAP_ARR", "c")].new == "20f"

    assert conn.inst_connectivity_changes == (
        LayoutInstConnectivityChange("MP0", "G", "IN", "CLK"),
    )
    assert len(conn.mosaic_changes) == 1
    assert conn.mosaic_changes[0].change_type == "modified"
    assert set(conn.mosaic_changes[0].changed_fields) == {"pcell_params", "rows"}

    assert len(conn.via_connectivity_changes) == 1
    vc = conn.via_connectivity_changes[0]
    assert vc.old_net == "VDD" and vc.new_net == "OUT"
    assert vc.old_cut_rows == 2 and vc.new_cut_rows == 3
    assert set(vc.changed_fields) == {"cutRows", "net"}

    assert {(c.scope, c.key) for c in conn.instance_prop_changes} == {("MP0", "role")}
    # Strict bool vs int: True -> 1 is detected in cell_property_changes.
    assert {c.key for c in conn.cell_property_changes} == {"extra", "simMode"}


def test_via_enclosure_only_edit_at_same_origin_is_geometry_only() -> None:
    old_via = LayoutVia(
        via_def="M2_M1",
        origin=(1.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.03, 0.04]},
    )
    new_via = replace(
        old_via, params={"cutRows": 2, "cutColumns": 1, "layer1Enc": [0.05, 0.05]}
    )
    diff = diff_layouts(_snap((old_via,)), _snap((new_via,)))

    assert not diff.has_connectivity_changes
    assert diff.has_geometry_changes
    assert diff.geometry.vias_removed == (old_via,)
    assert diff.geometry.vias_added == (new_via,)


def test_annotation_only_labels_and_auxiliary_shapes() -> None:
    old = _snap(_base_objects())
    aux_rect = LayoutRect(layer="text", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0)))
    aux_label_purpose = LayoutLine(
        layer="M1", purpose="label", points=((0.0, 0.0), (1.0, 1.0))
    )
    new_label = LayoutLabel(text="NOTE_A", xy=(2.0, 2.0), layer="text", purpose="drawing")

    assert is_auxiliary_shape(aux_rect)
    assert is_auxiliary_shape(aux_label_purpose)
    # Attached to a net -> physical, NOT auxiliary!
    assert not is_auxiliary_shape(replace(aux_rect, net="VDD"))

    new = _snap(_base_objects() + (aux_rect, aux_label_purpose, new_label))
    diff = diff_layouts(old, new)

    assert not diff.has_connectivity_changes
    assert not diff.has_geometry_changes
    assert diff.has_annotation_changes
    assert diff.annotation.labels_added == (new_label,)
    assert len(diff.annotation.aux_shapes_added) == 2
    assert diff.geometry.layer_deltas == {}


def test_connectivity_only_text_formatting_and_json_determinism() -> None:
    old = _snap(_base_objects())
    objs = [
        replace(o, xy=(5.0, 5.0)) if isinstance(o, LayoutInstance) else o
        for o in _base_objects()
    ]
    objs.append(LayoutLabel(text="COMMENT", xy=(0.0, 0.0), layer="text"))
    new = _snap(tuple(objs))
    diff = diff_layouts(old, new)

    conn_only_text = format_layout_diff_text(diff, connectivity_only=True)
    full_text = format_layout_diff_text(diff, connectivity_only=False)

    assert "Connectivity changes" in conn_only_text
    assert "Geometry changes" not in conn_only_text
    assert "Annotation changes" not in conn_only_text
    assert "Geometry changes" in full_text
    assert "Instance moves" in full_text
    assert "Annotation changes" in full_text
    assert "Labels added" in full_text

    # Reversed object order produces byte-identical JSON diff.
    diff_rev = diff_layouts(_snap(tuple(reversed(_base_objects()))), _snap(tuple(reversed(objs))))
    assert json.dumps(diff.to_dict(), sort_keys=True) == json.dumps(
        diff_rev.to_dict(), sort_keys=True
    )


def test_cellview_change_is_reported_without_object_changes() -> None:
    old = _snap(_base_objects(), cellview=CellViewRef("work", "inv_a", "layout"))
    new = _snap(_base_objects(), cellview=CellViewRef("work", "inv_b", "layout"))

    diff = diff_layouts(old, new)
    assert diff.cellview_changed
    assert diff.has_changes
    assert not diff.is_empty
    assert not diff.has_connectivity_changes
    assert not diff.has_geometry_changes
    assert not diff.has_annotation_changes


# ---------------------------------------------------------------------------
# Strict Multiset Fidelity for Stacked Vias & Pin Geometry vs Connectivity
# ---------------------------------------------------------------------------


def test_stacked_vias_tracked_as_clean_geometry_without_dup_via_warning() -> None:
    via = LayoutVia(
        via_def="M2_M1",
        origin=(1.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 2, "cutColumns": 1},
    )
    old = _snap((via,))
    new = _snap((via, via))

    diff = diff_layouts(old, new)

    assert not diff.has_connectivity_changes
    assert diff.has_geometry_changes
    assert diff.geometry.vias_added == (via,)
    assert diff.geometry.vias_removed == ()
    assert "duplicate_via_changes" not in diff.summary()
    assert "duplicate_via_changes" not in diff.to_dict()

    text = format_layout_diff_text(diff)
    assert "Vias added" in text
    assert "Duplicate / overlapping vias" not in text
    assert "DUPLICATE_VIAS" not in text

    # Reverse transition preserves exact multiset removal
    diff_dec = diff_layouts(new, old)
    assert diff_dec.geometry.vias_removed == (via,)
    assert diff_dec.geometry.vias_added == ()


def test_pin_bbox_move_classified_as_geometry_not_connectivity() -> None:
    pin_old = LayoutPin(
        name="IN",
        pin_name="P_IN",
        net="IN",
        direction="input",
        layer="M1",
        purpose="pin",
        shape_kind="rect",
        bbox=((0.0, 0.0), (0.2, 0.2)),
        access_dir=("left",),
    )
    pin_moved = replace(pin_old, bbox=((1.0, 2.0), (1.2, 2.2)))

    diff_move = diff_layouts(_snap((pin_old,)), _snap((pin_moved,)))
    assert not diff_move.has_connectivity_changes
    assert diff_move.connectivity.pins == ()
    assert diff_move.has_geometry_changes
    assert len(diff_move.geometry.pin_moves) == 1
    assert diff_move.geometry.pin_moves[0].changed_fields == ("bbox",)
    assert diff_move.summary()["pins"] == 0
    assert diff_move.summary()["pin_moves"] == 1

    stat_all = format_layout_diff_stat(diff_move)
    assert "Summary: connectivity=0, geometry=1, annotation=0" in stat_all
    assert "pin_moves=1" in stat_all
    stat_conn = format_layout_diff_stat(diff_move, connectivity_only=True)
    assert "No connectivity changes." in stat_conn
    full_text = format_layout_diff_text(diff_move)
    assert "Pin moves" in full_text

    # Changing layer or net/direction stays Tier 1 connectivity
    pin_layer = replace(pin_old, layer="M2", bbox=((1.0, 2.0), (1.2, 2.2)))
    diff_layer = diff_layouts(_snap((pin_old,)), _snap((pin_layer,)))
    assert diff_layer.has_connectivity_changes
    assert len(diff_layer.connectivity.pins) == 1
    assert set(diff_layer.connectivity.pins[0].changed_fields) == {"bbox", "layer"}
    assert diff_layer.geometry.pin_moves == ()


def test_multiset_preservation_for_duplicate_shapes_in_diff_and_merge() -> None:
    rect = LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0)), net="IN")
    seg = LayoutPathSeg(
        layer="M2",
        purpose="drawing",
        begin_pt=(0.0, 0.0),
        end_pt=(2.0, 0.0),
        width=0.2,
        net="VDD",
    )
    poly = LayoutPolygon(
        layer="PO",
        purpose="drawing",
        points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        net="IN",
    )
    via = LayoutVia(
        via_def="M2_M1",
        origin=(0.5, 0.5),
        orient="R0",
        net="VDD",
        params={"cutRows": 1, "cutColumns": 1},
    )

    base = _snap((rect, seg, poly, via))
    ours = _snap((rect, rect, seg, seg, poly, poly, via, via))
    extra_rect = LayoutRect(
        layer="M3", purpose="drawing", bbox=((5.0, 5.0), (6.0, 6.0)), net="OUT"
    )
    theirs = _snap((rect, seg, poly, via, extra_rect))

    # Diff preserves exact multiplicity
    diff = diff_layouts(base, ours)
    assert diff.geometry.shapes_added.count(rect) == 1
    assert diff.geometry.shapes_added.count(seg) == 1
    assert diff.geometry.shapes_added.count(poly) == 1
    assert diff.geometry.vias_added == (via,)
    assert diff.geometry.layer_deltas == {
        "M1:drawing": {"added": 1, "removed": 0},
        "M2:drawing": {"added": 1, "removed": 0},
        "PO:drawing": {"added": 1, "removed": 0},
    }

    # Merge preserves exact multiplicity (no silent deduplication)
    res = merge_layouts(base, ours, theirs)
    assert res.ok
    assert res.snapshot is not None
    assert res.snapshot.objects.count(rect) == 2
    assert res.snapshot.objects.count(seg) == 2
    assert res.snapshot.objects.count(poly) == 2
    assert res.snapshot.objects.count(via) == 2
    assert res.snapshot.objects.count(extra_rect) == 1

    # Divergent multiplicity edits (1 -> 2 vs 1 -> 3) Fail-Closed
    theirs_triple = _snap((rect, rect, rect, seg, poly, via))
    conflict_res = merge_layouts(base, ours, theirs_triple)
    assert not conflict_res.ok
    assert conflict_res.snapshot is None


# ---------------------------------------------------------------------------
# Task 2: Fail-Closed Three-Way Layout Merge Tests
# ---------------------------------------------------------------------------


def test_clean_merge_disjoint_layers_and_disjoint_same_layer_regions() -> None:
    base = _snap(_base_objects())
    ours_m1 = LayoutRect(layer="M1", purpose="drawing", bbox=((2.0, 0.0), (3.0, 0.2)), net="IN")
    theirs_m1 = LayoutRect(
        layer="M1", purpose="drawing", bbox=((5.0, 0.0), (6.0, 0.2)), net="OUT"
    )
    theirs_m2_same_xy = LayoutRect(
        layer="M2", purpose="drawing", bbox=((2.0, 0.0), (3.0, 0.2)), net="VDD"
    )

    ours = _snap(_base_objects() + (ours_m1,))
    theirs = _snap(_base_objects() + (theirs_m1, theirs_m2_same_xy))

    result = merge_layouts(base, ours, theirs)

    assert result.ok
    assert result.conflicts == ()
    assert result.snapshot is not None
    assert ours_m1 in result.snapshot.objects
    assert theirs_m1 in result.snapshot.objects
    assert theirs_m2_same_xy in result.snapshot.objects
    # Freshly computed Manifest v2 digests differ from base/ours/theirs.
    assert result.snapshot.manifest.content_sha256 != base.manifest.content_sha256
    assert result.snapshot.manifest.full_sha256 != ours.manifest.full_sha256


def test_clean_merge_same_instance_disjoint_pcell_params_and_placement() -> None:
    base = _snap(_base_objects(), cell_properties={"owner": "base"}, metadata={"rev": 1})

    ours_objs = tuple(
        replace(
            o,
            pcell_params={"w": "4u", "l": "60n", "fingers": 2},
            pcell_param_types={"w": "string", "l": "string", "fingers": "int"},
            connections={"D": "OUT", "G": "IN", "S": "VDD", "B": "VDD", "SUB": "VSS"},
        )
        if isinstance(o, LayoutInstance) and o.name == "MP0"
        else o
        for o in _base_objects()
    )
    theirs_objs = tuple(
        replace(
            o,
            xy=(3.5, 4.5),
            orient="MX",
            pcell_params={"w": "2u", "l": "90n", "fingers": 2},
            pcell_param_types={"w": "string", "l": "string", "fingers": "int"},
            props={"role": "pullup", "matched": True},
        )
        if isinstance(o, LayoutInstance) and o.name == "MP0"
        else o
        for o in _base_objects()
    )

    ours = _snap(ours_objs, cell_properties={"owner": "base", "oursFlag": True}, metadata={"rev": 1, "a": 1})
    theirs = _snap(theirs_objs, cell_properties={"owner": "base", "theirsFlag": 2}, metadata={"rev": 1, "b": 2})

    result = merge_layouts(base, ours, theirs)

    assert result.ok
    assert result.conflicts == ()
    assert result.snapshot is not None
    mp0 = next(
        o for o in result.snapshot.objects if isinstance(o, LayoutInstance) and o.name == "MP0"
    )
    assert mp0.xy == (3.5, 4.5)
    assert mp0.orient == "MX"
    assert mp0.pcell_params == {"fingers": 2, "l": "90n", "w": "4u"}
    assert mp0.connections["SUB"] == "VSS"
    assert mp0.props == {"matched": True, "role": "pullup"}
    assert result.snapshot.cell_properties == {"oursFlag": True, "owner": "base", "theirsFlag": 2}
    assert result.snapshot.manifest.metadata == {"a": 1, "b": 2, "rev": 1}


def test_clean_merge_disjoint_net_pin_label_boundary_and_convergent_additions() -> None:
    base = _snap(_base_objects())
    shared_rect = LayoutRect(
        layer="M1", purpose="drawing", bbox=((8.0, 8.0), (9.0, 9.0)), net="VDD"
    )

    ours_objs = list(_base_objects())
    ours_objs.append(LayoutNet(name="N_OURS", sig_type="signal"))
    ours_objs.append(
        LayoutLabel(text="OURS_NOTE", xy=(1.0, 1.0), layer="text", purpose="drawing")
    )
    ours_objs.append(shared_rect)

    theirs_objs = [
        replace(o, access_dir=("left", "top")) if isinstance(o, LayoutPin) else o
        for o in _base_objects()
    ]
    theirs_objs.append(LayoutNet(name="N_THEIRS", sig_type="clock"))
    theirs_objs.append(
        LayoutBoundary(
            boundary_kind="areaBoundary",
            name="ISO_WELL",
            points=((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)),
        )
    )
    theirs_objs.append(shared_rect)

    result = merge_layouts(base, _snap(tuple(ours_objs)), _snap(tuple(theirs_objs)))

    assert result.ok
    assert result.snapshot is not None
    net_names = {o.name for o in result.snapshot.objects if isinstance(o, LayoutNet)}
    assert {"N_OURS", "N_THEIRS"} <= net_names
    pin = next(o for o in result.snapshot.objects if isinstance(o, LayoutPin))
    assert pin.access_dir == ("left", "top")
    # Convergent identical addition merged once, with no spatial overlap false positive.
    assert result.snapshot.objects.count(shared_rect) == 1


def test_conflicting_pcell_param_instance_move_and_pin_edits_fail_closed() -> None:
    base = _snap(_base_objects())

    def tweak(
        objs: tuple[LayoutObject, ...],
        *,
        w_val,
        xy,
        pin_dir: str,
        pin_net: str,
    ) -> tuple[LayoutObject, ...]:
        out = []
        for o in objs:
            if isinstance(o, LayoutInstance) and o.name == "MP0":
                out.append(
                    replace(
                        o,
                        xy=xy,
                        pcell_params={"w": w_val, "l": "60n", "fingers": 2},
                        pcell_param_types={"w": "string", "l": "string", "fingers": "int"},
                    )
                )
            elif isinstance(o, LayoutPin) and o.name == "IN":
                out.append(replace(o, direction=pin_dir, net=pin_net))
            else:
                out.append(o)
        return tuple(out)

    ours = _snap(
        tweak(_base_objects(), w_val="4u", xy=(1.0, 2.0), pin_dir="output", pin_net="OUT")
    )
    theirs = _snap(
        tweak(_base_objects(), w_val="6u", xy=(2.0, 2.0), pin_dir="inputOutput", pin_net="VDD")
    )

    result = merge_layouts(base, ours, theirs)

    assert not result.ok
    assert result.snapshot is None
    by_path = {c.path: c for c in result.conflicts}
    assert by_path["instances.MP0.pcell_params.w"].domain == "connectivity"
    assert by_path["instances.MP0.pcell_params.w"].reason == "modify/modify"
    assert by_path["instances.MP0.xy"].domain == "geometry"
    assert by_path["instances.MP0.xy"].reason == "modify/modify"
    assert by_path["pins['IN:P_IN'].direction"].domain == "connectivity"
    assert by_path["pins['IN:P_IN'].net"].domain == "connectivity"

    # Serialization & formatting determinism
    payload = result.to_dict()
    assert payload["ok"] is False
    assert payload["snapshot"] is None
    json.dumps(payload)
    text = format_layout_merge_conflicts(result)
    assert "Layout merge conflicts" in text
    assert "[connectivity] instances.MP0.pcell_params.w" in text
    assert "[geometry] instances.MP0.xy" in text
    assert format_layout_merge_conflicts(LayoutMergeResult(True, None, ())) == "No merge conflicts."


def test_spatial_overlap_conflict_on_same_layer_and_override() -> None:
    base = _snap(_base_objects())
    ours_rect = LayoutRect(
        layer="M1", purpose="drawing", bbox=((2.0, 2.0), (4.0, 3.0)), net="IN"
    )
    theirs_rect = LayoutRect(
        layer="M1", purpose="drawing", bbox=((3.0, 2.5), (5.0, 3.5)), net="OUT"
    )
    ours = _snap(_base_objects() + (ours_rect,))
    theirs = _snap(_base_objects() + (theirs_rect,))

    result = merge_layouts(base, ours, theirs, check_spatial_overlap=True)
    assert not result.ok
    assert result.snapshot is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.domain == "geometry"
    assert conflict.reason == "spatial_overlap_on_same_layer"
    assert "M1:drawing" in conflict.path
    assert conflict.ours == ours_rect
    assert conflict.theirs == theirs_rect

    # Explicit override check_spatial_overlap=False allows merge to proceed.
    overridden = merge_layouts(base, ours, theirs, check_spatial_overlap=False)
    assert overridden.ok
    assert overridden.snapshot is not None
    assert ours_rect in overridden.snapshot.objects
    assert theirs_rect in overridden.snapshot.objects


def test_spatial_overlap_touching_different_nets_vs_same_net_and_pathseg_via() -> None:
    base = _snap(())

    # 1) Touching edge on DIFFERENT non-None nets -> electrical short conflict
    r_vdd = LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0)), net="VDD")
    r_vss = LayoutRect(layer="M1", purpose="drawing", bbox=((1.0, 0.0), (2.0, 1.0)), net="VSS")
    res_short = merge_layouts(base, _snap((r_vdd,)), _snap((r_vss,)))
    assert not res_short.ok
    assert res_short.conflicts[0].reason == "spatial_overlap_on_same_layer"

    # 2) Touching edge on SAME net -> clean abutment
    r_vdd_2 = LayoutRect(layer="M1", purpose="drawing", bbox=((1.0, 0.0), (2.0, 1.0)), net="VDD")
    res_abut = merge_layouts(base, _snap((r_vdd,)), _snap((r_vdd_2,)))
    assert res_abut.ok

    # 3) Overlapping PathSegs on same layer -> conflict
    seg_a = LayoutPathSeg(
        layer="M2", purpose="drawing", begin_pt=(0.0, 0.0), end_pt=(2.0, 0.0), width=0.2
    )
    seg_b = LayoutPathSeg(
        layer="M2", purpose="drawing", begin_pt=(1.0, -1.0), end_pt=(1.0, 1.0), width=0.2
    )
    res_seg = merge_layouts(base, _snap((seg_a,)), _snap((seg_b,)))
    assert not res_seg.ok
    assert res_seg.conflicts[0].reason == "spatial_overlap_on_same_layer"

    # 4) Overlapping newly added Vias on same via_def at same origin with different nets -> conflict
    via_a = LayoutVia(
        via_def="M2_M1", origin=(2.0, 2.0), orient="R0", net="VDD", params={"cutRows": 1}
    )
    via_b = LayoutVia(
        via_def="M2_M1", origin=(2.0, 2.0), orient="R0", net="VSS", params={"cutRows": 2}
    )
    res_via = merge_layouts(base, _snap((via_a,)), _snap((via_b,)))
    assert not res_via.ok
    assert res_via.snapshot is None
    assert res_via.conflicts[0].domain == "geometry"
    assert res_via.conflicts[0].reason == "spatial_overlap_via"

    # 5) Auxiliary text-layer rects overlapping across branches do NOT conflict
    aux_a = LayoutRect(layer="text", purpose="drawing", bbox=((0.0, 0.0), (2.0, 2.0)))
    aux_b = LayoutRect(layer="text", purpose="drawing", bbox=((1.0, 1.0), (3.0, 3.0)))
    res_aux = merge_layouts(base, _snap((aux_a,)), _snap((aux_b,)))
    assert res_aux.ok


def test_merge_rejects_mismatched_cellview_identity() -> None:
    base = _snap(_base_objects())
    other = _snap(_base_objects(), cellview=CellViewRef("work", "other_cell", "layout"))

    with pytest.raises(SchemaValidationError):
        merge_layouts(base, other, base)


def test_merge_delete_modify_and_strict_bool_vs_int_conflicts() -> None:
    base = _snap(
        (
            LayoutNet(name="N1", sig_type="signal"),
            LayoutInstance(
                name="M1",
                lib="pdk",
                cell="nch",
                is_pcell=True,
                pcell_params={"flag": 1},
                pcell_param_types={"flag": "int"},
            ),
        )
    )
    ours = _snap(
        (
            LayoutInstance(
                name="M1",
                lib="pdk",
                cell="nch",
                is_pcell=True,
                pcell_params={"flag": True},
                pcell_param_types={"flag": "boolean"},
            ),
        )
    )
    theirs = _snap(
        (
            LayoutNet(name="N1", sig_type="clock"),
            LayoutInstance(
                name="M1",
                lib="pdk",
                cell="nch",
                is_pcell=True,
                pcell_params={"flag": 2},
                pcell_param_types={"flag": "int"},
            ),
        )
    )

    result = merge_layouts(base, ours, theirs)
    assert not result.ok
    assert result.snapshot is None
    by_path = {c.path: c for c in result.conflicts}
    assert by_path["nets.N1"].reason == "delete/modify"
    flag_conflict = by_path["instances.M1.pcell_params.flag"]
    assert flag_conflict.base == 1 and type(flag_conflict.base) is int
    assert flag_conflict.ours is True and type(flag_conflict.ours) is bool
    assert flag_conflict.theirs == 2 and type(flag_conflict.theirs) is int


def test_merge_rejects_pcell_flag_and_params_inconsistency() -> None:
    base = _snap(
        (
            LayoutInstance(
                name="M1",
                lib="pdk",
                cell="nch",
                is_pcell=True,
                pcell_params={"w": "2u"},
                pcell_param_types={"w": "string"},
            ),
        )
    )
    ours = _snap(
        (
            LayoutInstance(
                name="M1",
                lib="pdk",
                cell="nch",
                is_pcell=False,
                pcell_params={},
                pcell_param_types={},
            ),
        )
    )
    theirs = _snap(
        (
            LayoutInstance(
                name="M1",
                lib="pdk",
                cell="nch",
                is_pcell=True,
                pcell_params={"w": "2u", "l": "60n"},
                pcell_param_types={"w": "string", "l": "string"},
            ),
        )
    )
    res = merge_layouts(base, ours, theirs)
    assert not res.ok
    assert res.snapshot is None
    assert any(c.reason == "pcell_flag_param_inconsistency" for c in res.conflicts)


def test_format_layout_diff_stat_and_summary_and_warning_markers() -> None:
    empty_diff = diff_layouts(_snap(_base_objects()), _snap(_base_objects()))
    empty_stat = format_layout_diff_stat(empty_diff)
    assert empty_stat == "work/inv2t/layout -> work/inv2t/layout\nNo layout changes."

    via = LayoutVia(
        via_def="M2_M1",
        origin=(1.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 2, "cutColumns": 1},
    )
    old_rect = LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 0.2)), net="IN")
    new_rect_1 = LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (2.0, 0.2)), net="IN")
    new_rect_2 = LayoutRect(layer="M1", purpose="drawing", bbox=((3.0, 0.0), (4.0, 0.2)), net="OUT")
    new_m2 = LayoutRect(layer="M2", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0)), net="VDD")
    new_label = LayoutLabel(text="REV2", xy=(0.5, 0.5), layer="text", purpose="drawing")

    old = _snap((LayoutNet(name="VDD", sig_type="power"), via, old_rect))
    new = _snap(
        (
            LayoutNet(name="VDD", sig_type="power"),
            LayoutNet(name="CLK", sig_type="clock"),
            via,
            via,
            new_rect_1,
            new_rect_2,
            new_m2,
            new_label,
        )
    )
    diff = diff_layouts(old, new)

    stat_text = format_layout_diff_stat(diff)
    assert "work/inv2t/layout -> work/inv2t/layout" in stat_text
    assert "Summary: connectivity=1, geometry=5, annotation=1" in stat_text
    assert "DUPLICATE_VIAS" not in stat_text
    assert "M1:drawing +2 / -1" in stat_text
    assert "++-" in stat_text
    assert "M2:drawing +1 / -0" in stat_text
    assert "Duplicate / overlapping vias" not in stat_text

    # connectivity_only stat omits layers/geometry/annotation
    conn_stat = format_layout_diff_stat(diff, connectivity_only=True)
    assert "Summary: connectivity=1" in conn_stat
    assert "Layers:" not in conn_stat
    assert "DUPLICATE_VIAS" not in conn_stat

    # Full text diff includes Summary line without duplicate-via warning noise
    full_text = format_layout_diff_text(diff)
    assert "Summary: connectivity=1, geometry=5, annotation=1" in full_text
    assert "Duplicate / overlapping vias" not in full_text


def test_format_layout_merge_conflicts_highlights_spatial_shape_and_via_collisions() -> None:
    base = _snap(())
    ours_rect = LayoutRect(
        layer="M1", purpose="drawing", bbox=((2.0, 2.0), (4.0, 3.0)), net="IN"
    )
    theirs_rect = LayoutRect(
        layer="M1", purpose="drawing", bbox=((3.0, 2.5), (5.0, 3.5)), net="OUT"
    )
    ours_via = LayoutVia(
        via_def="M2_M1",
        origin=(2.0, 2.0),
        orient="R0",
        net="VDD",
        params={"cutRows": 1, "cutColumns": 1, "cutWidth": 0.1, "cutHeight": 0.1},
    )
    theirs_via = LayoutVia(
        via_def="M2_M1",
        origin=(2.0, 2.0),
        orient="R0",
        net="VSS",
        params={"cutRows": 2, "cutColumns": 1, "cutWidth": 0.1, "cutHeight": 0.1},
    )
    result = merge_layouts(
        base,
        _snap((ours_rect, ours_via)),
        _snap((theirs_rect, theirs_via)),
    )
    assert not result.ok
    text = format_layout_merge_conflicts(result)
    assert "Layout merge conflicts (2 total: 2 geometry)" in text
    assert "[geometry] shapes.M1:drawing[ours=0,theirs=0]: spatial_overlap_on_same_layer" in text
    assert "layer=M1:drawing" in text
    assert "overlap_bbox=((3.0, 2.5), (4.0, 3.0))" in text
    assert "nets='IN' vs 'OUT' (DIFFERENT NETS)" in text
    assert "ours:   rect layer=M1:drawing net='IN' bbox=((2.0, 2.0), (4.0, 3.0))" in text
    assert "theirs: rect layer=M1:drawing net='OUT' bbox=((3.0, 2.5), (5.0, 3.5))" in text
    assert "[geometry] vias.M2_M1@(2.0,2.0)[ours=0,theirs=0]: spatial_overlap_via" in text
    assert "via_def=M2_M1" in text
    assert "nets='VDD' vs 'VSS' (DIFFERENT NETS)" in text
    assert "ours:   via via_def=M2_M1 origin=(2.0, 2.0) orient=R0 net='VDD' cuts=1x1" in text
    assert "theirs: via via_def=M2_M1 origin=(2.0, 2.0) orient=R0 net='VSS' cuts=2x1" in text


