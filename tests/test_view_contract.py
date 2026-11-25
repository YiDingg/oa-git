"""Contract-freeze tests for oa-git/2 manifest, view_contract registry, and SkillRequestRunner seam."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from oa_git.model import (
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    CellViewRef,
    DerivedState,
    Instance,
    Manifest,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
    SnapshotIntegrityError,
)
from oa_git.snapshot import build_schematic_snapshot, read_snapshot, write_snapshot
from oa_git.view_contract import (
    RESERVED_VIEW_TYPES,
    BatchCaptureItem,
    BatchCaptureResult,
    OaSession,
    SkillRequestRunner,
    build_manifest_v2,
    canonical_json_bytes,
    get_view_handler,
    list_view_handlers,
    read_manifest_file,
    register_view_handler,
    verify_manifest_v2_integrity,
)
from oa_git.virtuoso_backend import VirtuosoSkillBackend


def test_reserved_view_types_and_manifest_v2_for_symbol_and_layout(tmp_path: Path) -> None:
    assert RESERVED_VIEW_TYPES == ("schematic", "symbol", "layout")

    # Symbol manifest v2
    sym_dir = tmp_path / "sym"
    sym_dir.mkdir()
    sym_bytes = b"schema: oa-git-symbol/1\n"
    (sym_dir / "symbol.yaml").write_bytes(sym_bytes)
    sym_manifest = build_manifest_v2(
        view_type="symbol",
        cellview=CellViewRef("work", "opamp", "symbol"),
        file_contents={"symbol.yaml": sym_bytes},
        metadata={"dbu_per_uu": 160.0},
    )
    assert sym_manifest.schema == SCHEMA_VERSION_V2
    assert sym_manifest.electrical_sha256 is None
    assert sym_manifest.placement_sha256 is None
    assert sym_manifest.content_sha256 is not None
    (sym_dir / "manifest.json").write_bytes(
        canonical_json_bytes(sym_manifest.to_dict())
    )
    verified = verify_manifest_v2_integrity(sym_dir, read_manifest_file(sym_dir))
    assert verified == sym_manifest

    # Layout manifest v2 (2-space pretty canonical form)
    lay_dir = tmp_path / "lay"
    lay_dir.mkdir()
    obj_bytes = b'{"kind":"rect"}\n'
    (lay_dir / "objects.jsonl").write_bytes(obj_bytes)
    lay_manifest = build_manifest_v2(
        view_type="layout",
        cellview=CellViewRef("work", "opamp", "layout"),
        file_contents={"objects.jsonl": obj_bytes},
    )
    (lay_dir / "manifest.json").write_text(
        json.dumps(lay_manifest.to_dict(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    assert verify_manifest_v2_integrity(lay_dir, read_manifest_file(lay_dir)) == lay_manifest

    # Tampering fails closed
    (lay_dir / "objects.jsonl").write_bytes(b'{"kind":"tampered"}\n')
    with pytest.raises(SnapshotIntegrityError):
        verify_manifest_v2_integrity(lay_dir, read_manifest_file(lay_dir))


def test_schematic_writes_v2_and_remains_backward_compatible_with_v1(tmp_path: Path) -> None:
    cv = CellViewRef("work", "amp", "schematic")
    snap = build_schematic_snapshot(cv, SchematicElectrical(), SchematicPlacement())
    assert snap.manifest.schema == SCHEMA_VERSION_V2
    assert snap.manifest.content_sha256 is not None
    out = write_snapshot(snap, tmp_path / "v2")
    loaded = read_snapshot(out, verify=True)
    assert loaded.manifest.schema == SCHEMA_VERSION_V2
    assert loaded.manifest.full_sha256 == snap.manifest.full_sha256

    # Re-encode as legacy oa-git/1 manifest and verify reader still accepts it
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    circuit_bytes = (out / "circuit.yaml").read_bytes()
    placement_bytes = (out / "placement.yaml").read_bytes()
    (v1_dir / "circuit.yaml").write_bytes(circuit_bytes)
    (v1_dir / "placement.yaml").write_bytes(placement_bytes)
    import oa_git.snapshot as snap_mod

    e_sha = snap_mod._sha256_bytes(circuit_bytes)
    p_sha = snap_mod._sha256_bytes(placement_bytes)
    v1_full = snap_mod._full_sha256(
        e_sha, p_sha, cellview=cv, omissions_count=0, omissions_sha256="", metadata={}
    )
    v1_manifest = Manifest(
        schema=SCHEMA_VERSION_V1,
        view_type="schematic",
        cellview=cv,
        electrical_sha256=e_sha,
        placement_sha256=p_sha,
        full_sha256=v1_full,
        files={"circuit.yaml": e_sha, "placement.yaml": p_sha},
        omissions_count=0,
        metadata={},
    )
    (v1_dir / "manifest.json").write_text(
        snap_mod._render_manifest_json(v1_manifest),
        encoding="utf-8",
    )
    loaded_v1 = read_snapshot(v1_dir, verify=True)
    assert loaded_v1.manifest.schema == SCHEMA_VERSION_V1


def test_derived_state_and_instance_legacy_compatibility() -> None:
    inst = Instance(name="M1", lib="tsmcN65", cell="nch", derived_params={"wf": "1u"})
    assert inst.derived is not None
    assert inst.derived.status == "valid"
    assert inst.derived.source == "legacy-import"
    assert inst.derived.values == {"wf": "1u"}

    with pytest.raises(SchemaValidationError):
        DerivedState(status="stale", values={"wf": "1u"})
    with pytest.raises(SchemaValidationError):
        DerivedState(status="unavailable", input_sha256="a" * 64)


def test_virtuoso_backend_implements_skill_request_runner_seam(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "oagit_common.il").write_text("; common\n", encoding="utf-8")
    (skill_dir / "export_layout.il").write_text("; layout\n", encoding="utf-8")

    def fake_runner(cmd: list[str], *, env: dict[str, str], timeout: int, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_dir = Path(env["IC261_WORKDIR"])
        (run_dir / "response.json").write_text(
            json.dumps({"ok": True, "view_type": "layout", "objects": []}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    backend = VirtuosoSkillBackend(runner=fake_runner, skill_dir=skill_dir)
    assert isinstance(backend, SkillRequestRunner)
    resp = backend.run_skill_request(
        "capture_layout",
        {"library": "work", "cell": "top", "view": "layout"},
        cds_lib=tmp_path / "cds.lib",
        session=OaSession(session_id="s1"),
    )
    assert resp["ok"] is True
    assert resp["view_type"] == "layout"


def test_batch_result_and_registry_validation() -> None:
    cv = CellViewRef("work", "top", "schematic")
    res = BatchCaptureResult(
        items=(BatchCaptureItem(cellview=cv, snapshot=None, skipped=True, reason="clean"),),
        discovered=(cv,),
        session={"transport": "one_shot"},
    )
    assert res.ok is True
    assert res.to_dict()["items"][0]["skipped"] is True

    with pytest.raises(SchemaValidationError):
        get_view_handler("unknown_view")


def test_manifest_v2_strict_schema_canonical_bytes_and_dangling_omissions_symlink(
    tmp_path: Path,
) -> None:
    cv = CellViewRef("work", "opamp", "symbol")
    sym_bytes = b"schema: oa-git-symbol/1\n"
    sym_manifest = build_manifest_v2(
        view_type="symbol",
        cellview=cv,
        file_contents={"symbol.yaml": sym_bytes},
    )
    valid_dict = sym_manifest.to_dict()

    # 1) Manifest.from_dict rejects non-mapping, missing required keys, bad types, unknown keys (NEVER KeyError/AttributeError)
    for bad_payload in (
        "not-a-dict",
        [],
        {},
        {"schema": SCHEMA_VERSION_V2, "view_type": "symbol"},  # missing cellview, full_sha256, files, content_sha256
        {**valid_dict, "cellview": {"library": "work"}},  # missing cell/view
        {**valid_dict, "cellview": "work/opamp/symbol"},  # non-mapping cellview
        {**valid_dict, "extra_key": 123},  # unknown key
        {**valid_dict, "files": ["symbol.yaml"]},  # non-mapping files
        {**valid_dict, "files": {"symbol.yaml": 42}},  # non-str hash
        {**valid_dict, "metadata": ["not", "mapping"]},  # non-mapping metadata
        {**valid_dict, "full_sha256": 12345},  # non-str full_sha256
        {**valid_dict, "content_sha256": None},  # missing content_sha256 in v2
        {**valid_dict, "electrical_sha256": "a" * 64},  # non-null electrical_sha256 on symbol
        {**valid_dict, "placement_sha256": "b" * 64},  # non-null placement_sha256 on symbol
        {**valid_dict, "view_type": "layout", "electrical_sha256": "c" * 64},
    ):
        with pytest.raises(SchemaValidationError):
            Manifest.from_dict(bad_payload)  # type: ignore[arg-type]

    # build_manifest_v2 also rejects non-null electrical/placement digests for non-schematic views
    with pytest.raises(SchemaValidationError):
        build_manifest_v2(
            view_type="symbol",
            cellview=cv,
            file_contents={"symbol.yaml": sym_bytes},
            electrical_sha256="a" * 64,
        )

    # 2) read_manifest_file raises SchemaValidationError (never KeyError) on malformed manifest.json
    malformed_dir = tmp_path / "malformed"
    malformed_dir.mkdir()
    (malformed_dir / "manifest.json").write_text(
        json.dumps({"schema": SCHEMA_VERSION_V2, "view_type": "symbol"}),
        encoding="utf-8",
    )
    with pytest.raises(SchemaValidationError):
        read_manifest_file(malformed_dir)

    # 3) verify_manifest_v2_integrity enforces canonical bytes & detects dangling symlink omissions.json
    sym_dir = tmp_path / "sym"
    sym_dir.mkdir()
    (sym_dir / "symbol.yaml").write_bytes(sym_bytes)

    # Non-canonical formatting (4-space indent) must fail closed with SnapshotIntegrityError
    (sym_dir / "manifest.json").write_text(
        json.dumps(valid_dict, sort_keys=True, indent=4) + "\n", encoding="utf-8"
    )
    with pytest.raises(SnapshotIntegrityError, match="canonical"):
        verify_manifest_v2_integrity(sym_dir, sym_manifest)

    # Both compact canonical and 2-space pretty canonical pass
    (sym_dir / "manifest.json").write_bytes(canonical_json_bytes(valid_dict))
    assert verify_manifest_v2_integrity(sym_dir, read_manifest_file(sym_dir)) == sym_manifest

    (sym_dir / "manifest.json").write_text(
        json.dumps(valid_dict, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    assert verify_manifest_v2_integrity(sym_dir, read_manifest_file(sym_dir)) == sym_manifest

    # Dangling symlink omissions.json when omissions_count == 0 must fail closed!
    dangling = sym_dir / "omissions.json"
    dangling.symlink_to(tmp_path / "nonexistent_target.json")
    assert not dangling.exists() and dangling.is_symlink()
    with pytest.raises(SnapshotIntegrityError, match="omissions.json"):
        verify_manifest_v2_integrity(sym_dir, read_manifest_file(sym_dir))
    dangling.unlink()

    # Non-schematic manifest object with non-null electrical_sha256 fails closed
    bad_sym_manifest = Manifest(
        schema=SCHEMA_VERSION_V2,
        view_type="symbol",
        cellview=cv,
        electrical_sha256="a" * 64,
        placement_sha256=None,
        full_sha256=sym_manifest.full_sha256,
        files=sym_manifest.files,
        omissions_count=0,
        metadata={},
        content_sha256=sym_manifest.content_sha256,
    )
    with pytest.raises(SchemaValidationError):
        verify_manifest_v2_integrity(sym_dir, bad_sym_manifest)


def test_infer_view_type_default_view_handlers_and_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import oa_git.view_contract as vc
    from oa_git.layout_backend import LayoutHandler
    from oa_git.view_contract import ensure_default_view_handlers, infer_view_type

    assert infer_view_type("schematic") == "schematic"
    assert infer_view_type("schematic_alt") == "schematic"
    assert infer_view_type("symbol") == "symbol"
    assert infer_view_type("symbol_small") == "symbol"
    assert infer_view_type("layout") == "layout"
    assert infer_view_type("layout_pulsic") == "layout"
    assert infer_view_type("layout_oap1") == "layout"
    assert infer_view_type("veriloga") is None
    # manifest_view_type wins when valid
    assert infer_view_type("custom_mask", manifest_view_type="layout") == "layout"
    assert infer_view_type("layout_pulsic", manifest_view_type="invalid") == "layout"

    ensure_default_view_handlers()
    assert list_view_handlers() == ("layout", "schematic", "symbol")
    assert isinstance(get_view_handler("layout"), LayoutHandler)

    # Explicit monkeypatch isolation of _VIEW_HANDLERS must still fail closed!
    monkeypatch.setattr(vc, "_VIEW_HANDLERS", {})
    assert list_view_handlers() == ()
    with pytest.raises(SchemaValidationError, match="No handler registered"):
        get_view_handler("layout")


def test_project_state_layout_dirty_index_custom_views_and_integrity(tmp_path: Path) -> None:
    from oa_git.layout_model import LayoutRect
    from oa_git.layout_snapshot import build_layout_snapshot, write_layout_snapshot
    from oa_git.project_state import (
        IndexEntry,
        classify_cellview_state,
        default_source_file_candidates,
        explain_cellview_state,
        fingerprint_source_files,
    )
    from oa_git.snapshot import snapshot_dir_for

    lib_root = tmp_path / "oa" / "work"
    snapshots_dir = tmp_path / "snapshots"

    # Standard & custom layout view names resolve layout.oa
    cv_custom = CellViewRef("work", "amp", "layout_pulsic")
    assert default_source_file_candidates(cv_custom) == (
        "amp/layout_pulsic/layout.oa",
        "amp/layout_pulsic/master.tag",
    )
    # Non-layout-prefixed view name resolves via manifest.json peek
    cv_alias = CellViewRef("work", "amp", "mask_v2")
    assert default_source_file_candidates(cv_alias) == ("amp/mask_v2/master.tag",)
    assert default_source_file_candidates(cv_alias, view_type="layout") == (
        "amp/mask_v2/layout.oa",
        "amp/mask_v2/master.tag",
    )

    for cv in (cv_custom, cv_alias):
        oa_file = lib_root / cv.cell / cv.view / "layout.oa"
        oa_file.parent.mkdir(parents=True, exist_ok=True)
        oa_file.write_bytes(b"OA_LAYOUT_V1")

    snap_alias = build_layout_snapshot(
        cv_alias,
        (LayoutRect(layer="M1", purpose="drawing", bbox=((0.0, 0.0), (1.0, 1.0))),),
    )
    snap_dir = write_layout_snapshot(snap_alias, snapshot_dir_for(snapshots_dir, cv_alias))
    fps = fingerprint_source_files(
        lib_root, default_source_file_candidates(cv_alias, view_type="layout")
    )
    entry = IndexEntry(
        cellview=cv_alias,
        source_files=fps,
        snapshot=f"snapshots/{cv_alias.key}",
        snapshot_full_sha256=snap_alias.manifest.full_sha256,
        state="clean",
    )
    entries = {cv_alias.key: entry}

    state, _ = classify_cellview_state(
        cv_alias, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    e_state, reason = explain_cellview_state(
        cv_alias, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state == "clean" and e_state == "clean" and reason == "clean"

    # Stale unreferenced definitions.jsonl is caught by verify_layout_snapshot_integrity -> dirty
    (snap_dir / "definitions.jsonl").write_text('{"id":"d0"}\n', encoding="utf-8")
    corrupt_state, corrupt_reason = explain_cellview_state(
        cv_alias, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert corrupt_state == "dirty"
    assert corrupt_reason == "snapshot corrupted"
    (snap_dir / "definitions.jsonl").unlink()

    # Modifying layout.oa flips mask_v2 to dirty via manifest.json peek!
    (lib_root / cv_alias.cell / cv_alias.view / "layout.oa").write_bytes(b"OA_LAYOUT_V2")
    mod_state, mod_reason = explain_cellview_state(
        cv_alias, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert mod_state == "dirty"
    assert mod_reason == "source modified"
