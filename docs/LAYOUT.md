# oa-git Physical Layout Architecture & Reference (`layout.oa`)

## 1. Overview & Design Principles

`oa-git` supports Cadence OpenAccess physical layouts (`maskLayout` / `layout.oa`) through the same unified `oa-git/2` contract, CLI, and warm daemon infrastructure as Schematic and Symbol views:

- **Official SKILL API Only & MIT Clean-Room**: All OpenAccess reads and writes use exclusively documented Cadence SKILL APIs (`db*`, `dd*`, `le*`, `rod*`, `tech*`, `cdfParseFloatString`). Raw `.oa` binary parsing/generation and GUI/private APIs (`leHi*`, `geHi*`, `schHi*`, `cdfGetParam`, `cdfGetParameterList`, `dbReplaceProp`) are strictly prohibited.
- **Unified Manifest v2 (`oa-git/2`)**: Uses `view_type: "layout"`, `content_sha256`, and `full_sha256` with `electrical_sha256: null` and `placement_sha256: null`, matching `oa_git.view_contract` byte-for-byte.
- **Deterministic JSONL + Deduplicated Definitions + Multiset Fidelity**:
  - Layout micron coordinates are quantized to 6 decimal places (sub-nanometer precision at `DBUPerUU=1000`), while SI float PCell/user parameters (e.g. `l=6e-8`, `fw=2.4e-7`, `cap=1.5e-15`) preserve 12 significant digits (`%.12g`) without fixed-decimal underflow.
  - `export_layout.il` streams JSON in $O(N)$ directly to `outfile` with atomic `.tmp -> renameFile` write and `%.12g` float formatting (zero $O(N^2)$ `strcat` bloat and zero 6-digit `%g` truncation on large-chip coordinates `> 1000 um`).
  - Static PDK bloat (`lxComponentType`, Virtuoso display preferences, timestamps) is stripped, and repeated PCell/via/property payloads are deduplicated into `definitions.jsonl`.
  - **Strict Multiset Preservation**: Identical or overlapping physical objects (especially stacked/duplicated Vias, Rects, Paths, Polygons) are **never silently merged or deduplicated**—exact multiplicity is preserved across export, snapshot, diff, merge, and restore. Repeated captures are 100% byte-identical.
- **3-Tier Semantic Diff**: Separates high-impact **Connectivity/Net/Pin/PCell & Device changes** from **Pure Geometry/Routing/Placement & Pin-BBox Moves** and **Annotation/Auxiliary Layer changes**, with compact Git-diff-style object summaries, `--stat` diffstat bars, and Git revision diff (`diff HEAD~1 HEAD`).
- **Object-Level Three-Way Merge with Spatial Overlap Detection**: Cleanly merges concurrent non-overlapping edits across different objects, layers, parameters, or spatial regions, while Failing-Closed on conflicting field edits or newly added overlapping physical shapes on the same `(layer, purpose)` (reporting exact `overlap_bbox`, net collision with `(DIFFERENT NETS)` callout, and `ours`/`theirs` geometry).
- **Fail-Closed Atomic Draft-and-Publish Restore**: Enforces library allowlists, live `*.cdslck*`/`*.lck` lock rejection, omission blocking, fresh draft reconstruction, in-draft PCell parameter, connectivity, and non-mosaic object count verification, `dbSave`, `dbCopyCellView` publish, complete draft cleanup (`dbPurge` + `ddDeleteObj`), and post-restore readback verification.

---

## 2. Canonical Snapshot Storage (`oa-git/2`)

```text
<snapshots_root>/<library>/<cell>/<view>/
  manifest.json      # oa-git/2 manifest (view_type="layout", content_sha256, full_sha256)
  objects.jsonl      # Deterministically sorted, one canonical layout object per line
  definitions.jsonl  # [optional] Content-addressed deduplicated PCell/via/props definitions
  omissions.json     # [optional] Written iff omissions_count > 0; deleted when complete
```

### Supported OpenAccess Layout Primitives

| Object Type (`type`) | OA Source | Key Canonicalization |
|---|---|---|
| `net` | `cv~>nets` | Sorted by `name`; preserves `sig_type` (incl. `"supply"`), `is_global`, `num_bits` |
| `pin` | `cv~>terminals -> pins -> figs` | Deduplicated from `cv~>shapes` via `pinFigIds`; supports `rect`/`polygon`/`ellipse`/`path_seg`/`path` pin figures and multi-figure pins (`dbAddFigToPin`); preserves `direction`, `net`, `layer:purpose`, `custom_style`, sorted `access_dir` |
| `instance` | `cv~>instances` (excluding mosaic template `mosaicInst`) | Formal PCell params from `superMaster~>parameters~>value` (12-sig-fig SI float safe) deduplicated into `definitions.jsonl`; `connections` from `instTerms`; static PDK bloat stripped |
| `mosaic` | `cv~>mosaics` | Simple/PCell array with `rows`, `columns`, `u_x`, `u_y`, formal PCell params, and mosaic user props; excluded from `cv~>instances` so mosaics are never double-exported |
| `via` | `cv~>vias` (`stdVia` / `customVia`) | `via_def`, `origin`, `orient`, `net`, typed `overrideParams` (default-filtered against 13-element `stdViaDef~>params`) deduplicated when repeated; multiset multiplicity strictly preserved |
| `rect` | `cv~>shapes` (`rect`) | Normalized `bbox = ((minX, minY), (maxX, maxY))` |
| `path_seg` | `cv~>shapes` (`pathSeg`) | Endpoint order normalized (`begin_pt <= end_pt`, swapping `begin/end` styles, extensions, and `dbGetPathSegStyle` 6-element `custom_style` chamfer tuple `(b0,b1,b2,e0,e1,e2) -> (e0,e1,e2,b0,b1,b2)`) |
| `path` | `cv~>shapes` (`path`) | Quantized centerline `points`, `width`, `path_style`, `begin_ext`, `end_ext` |
| `polygon` | `cv~>shapes` (`polygon`) | Duplicate closing point stripped; ring rotated to min `(x, y)` vertex preserving winding |
| `ellipse`, `line` | `cv~>shapes` | Quantized `bbox` / `points`, `layer:purpose`, `net` |
| `label` | `cv~>shapes` (`label`) | Preserves `text`, `xy`, `layer:purpose`, `justify`, `orient`, `font`, `height`, `label_type` (`normalLabel`/`NLPLabel`/`ILLabel`), `net` |
| `blockage` | `cv~>blockages` | `blockage_type`, optional `layer`, canonicalized `points` |
| `boundary` | `cv~>prBoundary`, `areaBoundary`/`areaBoundaries`, `snapBoundary`/`snapBoundaries`, `clusterBoundaries` | Canonicalized ring `points` (deduplicated across singleton and plural OA attributes) |
| `fig_group` | `cv~>figGroups` | `name`, `origin`, `orient`, `group_type` (read-only XL tags like `"modgen"` captured for diff, normalized cleanly to OA `"none"` on `dbCreateFigGroup` restore) |
| `cell_properties` | Filtered `cv~>prop` | Only user scalar properties (SI float safe); strips Virtuoso GUI display settings & OA timestamps |

---

## 3. Module Architecture & Integration Seam

| Module | Responsibility |
|---|---|
| `src/oa_git/layout_model.py` | Immutable layout domain types, `LayoutManifest` (`oa-git/2`), `LayoutRestoreReceipt`, PDK/GUI junk filters |
| `src/oa_git/layout_snapshot.py` | Coordinate quantization, SI float canonicalization, geometry canonicalization (incl. custom `pathSeg` 6-tuple reversal), `definitions.jsonl` deduplication, atomic read/write, and Manifest v2 integrity verification |
| `src/oa_git/layout_diff.py` | 3-tier semantic diff (`connectivity` / `geometry` incl. pure pin geometry moves / `annotation`), per-layer delta stats, compact Git-like text & `--stat` formatting |
| `src/oa_git/layout_merge.py` | Field-level + multiset three-way merge with same-layer spatial overlap & net-collision conflict detection |
| `src/oa_git/layout_backend.py` | `LayoutHandler` (implements `view_contract.ViewHandler`), DPL-JSON wire codec, `verify_layout_roundtrip`, `FixtureLayoutBackend`, `VirtuosoLayoutBackend` (supports `SkillRequestRunner` / hot-daemon delegation + standalone local/SSH execution) |
| `src/oa_git/layout_cli.py` | Git-like standalone CLI (`capture`, `restore`, `status`, `diff`, `merge`, `verify`) with upward `.oagit/config.json` (v2) / `.oagit.json` (v1) discovery, dirty-index `--changed` skipping, hot daemon wiring (`--one-shot`/`--no-daemon`), and Git revision diff (`HEAD~1 HEAD`) |
| `src/oa_git/skill/export_layout.il` | Read-only official-API layout capture (`action="capture_layout"`, prefix `oagitLayoutExport*`, daemon alias `oagitLayoutRunExport`, `oagit-spool/1` envelope echo, $O(N)$ `%.12g` streaming atomic JSON) |
| `src/oa_git/skill/restore_layout.il` | Atomic Draft-and-Publish layout restore (`action="restore_layout"`, prefix `oagitLayoutRestore*`, daemon alias `oagitLayoutRunRestore`, `oagit-spool/1` envelope echo, atomic JSON write, multi-figure pin `dbAddFigToPin`, custom `pathSeg` 6-tuple restore, non-mosaic instance counting) |

### Unified View Registry & Daemon Integration

`LayoutHandler` is automatically registered under `"layout"` by `ensure_default_view_handlers()` in `src/oa_git/view_contract.py` (alongside `schematic` and `symbol`), so the unified `oagit` CLI transparently handles `layout` and custom `layout_*` views across `status`, `capture`, `diff`, `merge`, and `restore`:

```python
from oa_git.view_contract import ensure_default_view_handlers, get_view_handler

ensure_default_view_handlers()
layout_handler = get_view_handler("layout")
```

`VirtuosoLayoutBackend(skill_runner=virtuoso_backend)` reuses the unified `run_skill_request` seam, SSH `ControlMaster`, and lazy headless daemon (`oagit-spool/1`) with zero duplication.

---

## 4. Git-Like CLI Usage

Physical layouts are handled directly by the unified `oagit` CLI (`oagit status`, `oagit capture`, `oagit diff`, `oagit merge`, `oagit restore`), and the standalone `python -m oa_git.layout_cli` entrypoint remains available for backward compatibility. With `.oagit/config.json` (v2) present anywhere at or above the working directory, both automatically inherit `snapshots_dir`, `cds_lib`, `allow_libraries`, `oa_libs`, `backend`, `virtuoso_cmd`, `ssh_host`, `remote_workdir`, `timeout_s`, and `daemon` settings:

```bash
# Compact Git-status-like layout overview (integrity, dirty index, locks)
python -m oa_git.layout_cli status
python -m oa_git.layout_cli status --changed

# Capture layout (inherits .oagit/config.json; skips clean layouts with --changed)
python -m oa_git.layout_cli capture work/inv_dut/layout
python -m oa_git.layout_cli capture --changed --one-shot

# Git-revision 3-tier semantic diff (auto-infers single layout cellview, or pass --cellview)
python -m oa_git.layout_cli diff HEAD~1 HEAD
python -m oa_git.layout_cli diff HEAD~1 HEAD --stat
python -m oa_git.layout_cli diff HEAD~1 HEAD --connectivity-only

# Verify Manifest v2 integrity and inspect object/definition counts
python -m oa_git.layout_cli verify snapshots/work/inv_dut/layout

# Object-level three-way merge (Fail-Closed exit 1 on field or same-layer spatial collision)
python -m oa_git.layout_cli merge base_dir ours_dir theirs_dir -o merged_dir

# Fail-Closed atomic Draft-and-Publish restore with full readback verification
python -m oa_git.layout_cli restore snapshots/work/inv_dut/layout --target sandbox/inv_dut/layout --overwrite
```
