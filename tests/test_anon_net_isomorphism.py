"""Tests for P0-1 anonymous auto-net bijection isomorphism in guards and diff."""

from __future__ import annotations

from dataclasses import replace

import pytest

from oa_git.diff import diff_schematics
from oa_git.guards import (
    ANON_NET_RE,
    check_anonymous_net_isomorphism,
    explicit_net_names,
    is_anonymous_net,
    verify_roundtrip,
)
from oa_git.model import (
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    PinPlacement,
    Port,
    RoundtripMismatchError,
    SchematicElectrical,
    SchematicPlacement,
    SchematicSnapshot,
    WireLabel,
    WireSegment,
)
from oa_git.snapshot import build_schematic_snapshot


SAR_CELLVIEW = CellViewRef("adc_lib", "SAR_ADC", "schematic")


def _build_sar_like_snapshot(
    anon_names: list[str],
    *,
    explicit_wire_label_on_first: str | None = None,
    override_connections: dict[str, dict[str, str]] | None = None,
) -> SchematicSnapshot:
    """Build a 22-anonymous-net SAR_ADC-like chain with ports, globals, and a bus."""

    assert len(anon_names) == 22
    ports = (
        Port("CLK", "input"),
        Port("VINP", "input"),
        Port("VINN", "input"),
        Port("DOUTP<9:0>", "output"),
    )
    nets = [
        Net("CLK"),
        Net("VINP"),
        Net("VINN"),
        Net("DOUTP<9:0>", num_bits=10),
        Net("VDD", is_global=True),
        Net("VSS", is_global=True),
    ]
    seen_nets = {n.name for n in nets}
    for net_name in anon_names:
        if net_name not in seen_nets:
            nets.append(Net(net_name))
            seen_nets.add(net_name)

    instances: list[Instance] = []
    inst_placements: list[InstancePlacement] = []
    wires: list[WireSegment] = []

    # Create 23 stages chained through the 22 anonymous auto-nets
    for idx in range(23):
        name = f"I{idx}"
        in_net = "VINP" if idx == 0 else anon_names[idx - 1]
        out_net = "DOUTP<9:0>" if idx == 22 else anon_names[idx]
        conns = {
            "IN": in_net,
            "OUT": out_net,
            "CLK": "CLK",
            "VDD": "VDD",
            "VSS": "VSS",
        }
        if override_connections and name in override_connections:
            conns.update(override_connections[name])
            for net_name in conns.values():
                if net_name not in seen_nets:
                    nets.append(Net(net_name))
                    seen_nets.add(net_name)
        instances.append(
            Instance(
                name=name,
                lib="adc_lib",
                cell="sar_stage",
                connections=conns,
            )
        )
        inst_placements.append(InstancePlacement(name, (float(idx), 0.0), "R0"))

    for idx, net_name in enumerate(anon_names):
        labels: tuple[WireLabel, ...] = ()
        if idx == 0 and explicit_wire_label_on_first is not None:
            labels = (WireLabel(explicit_wire_label_on_first, (float(idx) + 0.5, 0.0)),)
        wires.append(
            WireSegment(
                points=((float(idx), 0.0), (float(idx + 1), 0.0)),
                net=net_name,
                labels=labels,
            )
        )

    pins = (
        PinPlacement("CLK", "CLK", "input", "basic", "ipin", "symbol", (0.0, 2.0)),
        PinPlacement("VINP", "VINP", "input", "basic", "ipin", "symbol", (0.0, 1.0)),
        PinPlacement("VINN", "VINN", "input", "basic", "ipin", "symbol", (0.0, -1.0)),
        PinPlacement("DOUTP<9:0>", "DOUTP<9:0>", "output", "basic", "opin", "symbol", (23.0, 0.0)),
    )

    return build_schematic_snapshot(
        SAR_CELLVIEW,
        SchematicElectrical(
            ports=ports,
            nets=tuple(nets),
            instances=tuple(instances),
        ),
        SchematicPlacement(
            instances=tuple(inst_placements),
            pins=pins,
            wires=tuple(wires),
        ),
    )


def test_explicit_net_names_and_is_anonymous_net() -> None:
    assert ANON_NET_RE.fullmatch("net0")
    assert ANON_NET_RE.fullmatch("net42")
    assert not ANON_NET_RE.fullmatch("net_1")
    assert not ANON_NET_RE.fullmatch("net1<0>")
    assert not ANON_NET_RE.fullmatch("NET1")

    snap = _build_sar_like_snapshot(
        [f"net{i}" for i in range(1, 23)],
        explicit_wire_label_on_first="net1",
    )
    explicit = explicit_net_names(snap)
    assert {"CLK", "VINP", "VINN", "DOUTP<9:0>", "VDD", "VSS", "net1"} <= explicit
    assert is_anonymous_net("net1", explicit) is False
    assert is_anonymous_net("net2", explicit) is True
    assert is_anonymous_net("DOUTP<9:0>", explicit) is False
    assert is_anonymous_net("VDD", explicit) is False


def test_sar_adc_22_net_permutation_passes_isomorphism_roundtrip_and_diff() -> None:
    expected_names = [f"net{i}" for i in range(1, 23)]
    # Cyclic + reversed permutation simulating Virtuoso net renumbering
    actual_names = [f"net{i}" for i in range(101, 123)]
    actual_names = actual_names[7:] + actual_names[:7]

    expected = _build_sar_like_snapshot(expected_names)
    actual = _build_sar_like_snapshot(actual_names)

    ok, mapping, mismatches = check_anonymous_net_isomorphism(
        {i.name: i.connections for i in expected.electrical.instances},
        {i.name: i.connections for i in actual.electrical.instances},
        explicit_net_names(expected),
        explicit_net_names(actual),
    )
    assert ok is True
    assert mismatches == []
    assert len(mapping) == 22
    assert mapping == dict(zip(expected_names, actual_names))

    # Also verify tuple-keyed connection mapping input shape
    flat_exp = {
        (inst.name, term): net
        for inst in expected.electrical.instances
        for term, net in inst.connections.items()
    }
    flat_act = {
        (inst.name, term): net
        for inst in actual.electrical.instances
        for term, net in inst.connections.items()
    }
    ok2, mapping2, mismatches2 = check_anonymous_net_isomorphism(
        flat_exp, flat_act, explicit_net_names(expected), explicit_net_names(actual)
    )
    assert ok2 is True and mapping2 == mapping and mismatches2 == []

    # Full roundtrip including electrical nets AND placement wires passes
    report = verify_roundtrip(expected, actual, raise_on_mismatch=False)
    assert report.ok is True
    assert report.electrical_ok is True
    assert report.placement_ok is True
    assert report.issues == ()

    # Semantic diff produces ZERO false connectivity, net, or wire changes
    diff = diff_schematics(expected, actual)
    assert diff.is_empty is True
    assert diff.has_electrical_changes is False
    assert diff.has_placement_changes is False
    assert diff.electrical.connectivity_changes == ()
    assert diff.electrical.nets == ()
    assert diff.placement.wires_added == ()
    assert diff.placement.wires_removed == ()


def test_shorted_anonymous_nets_fail_closed() -> None:
    expected = _build_sar_like_snapshot([f"net{i}" for i in range(1, 23)])
    # Short net1 and net2 into the same actual anonymous net 'net99'
    shorted_names = ["net99", "net99"] + [f"net{i}" for i in range(3, 23)]
    actual = _build_sar_like_snapshot(shorted_names)

    ok, mapping, mismatches = check_anonymous_net_isomorphism(
        {i.name: i.connections for i in expected.electrical.instances},
        {i.name: i.connections for i in actual.electrical.instances},
        explicit_net_names(expected),
        explicit_net_names(actual),
    )
    assert ok is False
    assert mapping == {}
    assert any("shorted" in msg for msg in mismatches)

    with pytest.raises(RoundtripMismatchError, match="shorted"):
        verify_roundtrip(expected, actual)

    diff = diff_schematics(expected, actual)
    assert diff.has_electrical_changes is True
    assert diff.electrical.connectivity_changes


def test_split_anonymous_net_fails_closed() -> None:
    expected = _build_sar_like_snapshot([f"net{i}" for i in range(1, 23)])
    # Split net1 (between I0.OUT and I1.IN) into net50 and net51
    actual = _build_sar_like_snapshot(
        [f"net{i}" for i in range(50, 72)],
        override_connections={"I1": {"IN": "net999"}},
    )

    ok, mapping, mismatches = check_anonymous_net_isomorphism(
        {i.name: i.connections for i in expected.electrical.instances},
        {i.name: i.connections for i in actual.electrical.instances},
        explicit_net_names(expected),
        explicit_net_names(actual),
    )
    assert ok is False
    assert mapping == {}
    assert any("split" in msg for msg in mismatches)

    with pytest.raises(RoundtripMismatchError, match="split"):
        verify_roundtrip(expected, actual)

    diff = diff_schematics(expected, actual)
    assert diff.has_electrical_changes is True
    # Only the broken net1 terminals show connectivity changes; other 21 renumbered nets align!
    changed_terminals = {(c.instance, c.terminal) for c in diff.electrical.connectivity_changes}
    assert ("I0", "OUT") in changed_terminals
    assert ("I1", "IN") in changed_terminals
    assert len(changed_terminals) == 2


def test_named_net_mismatch_and_anon_to_named_mapping_fail_closed() -> None:
    expected = _build_sar_like_snapshot([f"net{i}" for i in range(1, 23)])

    # Named net mismatch: CLK -> VINN
    named_mismatch = _build_sar_like_snapshot(
        [f"net{i}" for i in range(1, 23)],
        override_connections={"I5": {"CLK": "VINN"}},
    )
    with pytest.raises(RoundtripMismatchError, match="named net mismatch"):
        verify_roundtrip(expected, named_mismatch)

    # Anonymous net mapped to a named net: net1 -> mid_named
    anon_to_named = _build_sar_like_snapshot(
        ["mid_named"] + [f"net{i}" for i in range(2, 23)]
    )
    with pytest.raises(RoundtripMismatchError, match="anonymous net and named net"):
        verify_roundtrip(expected, anon_to_named)


def test_explicit_wire_label_net1_is_treated_as_named_and_blocks_renumbering() -> None:
    expected = _build_sar_like_snapshot(
        [f"net{i}" for i in range(1, 23)],
        explicit_wire_label_on_first="net1",
    )
    # Renumber net1 -> net200 while keeping net2..net22 permuted: MUST FAIL!
    renamed = _build_sar_like_snapshot(
        ["net200"] + [f"net{i}" for i in range(201, 222)],
        explicit_wire_label_on_first="net200",
    )
    ok, _, mismatches = check_anonymous_net_isomorphism(
        {i.name: i.connections for i in expected.electrical.instances},
        {i.name: i.connections for i in renamed.electrical.instances},
        explicit_net_names(expected),
        explicit_net_names(renamed),
    )
    assert ok is False
    assert any("named net mismatch" in msg for msg in mismatches)

    with pytest.raises(RoundtripMismatchError):
        verify_roundtrip(expected, renamed)

    diff = diff_schematics(expected, renamed)
    assert diff.has_electrical_changes is True
    assert any(
        c.old_net == "net1" and c.new_net == "net200"
        for c in diff.electrical.connectivity_changes
    )

    # Keeping explicit net1 literal while permuting genuinely anonymous net2..net22 PASSES!
    valid_perm = _build_sar_like_snapshot(
        ["net1"] + [f"net{i}" for i in range(301, 322)],
        explicit_wire_label_on_first="net1",
    )
    assert verify_roundtrip(expected, valid_perm, raise_on_mismatch=False).ok is True
    assert diff_schematics(expected, valid_perm).is_empty is True
