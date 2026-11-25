# oa-git Symbol Subsystem (`oa-git-symbol/1`)

`oa-git` provides lightweight, deterministic version control for Cadence OpenAccess `schematicSymbol` CellViews alongside `schematic` and `layout`.

## Snapshot Directory Layout (`oa-git/2`)

```text
snapshots/<library>/<cell>/<view>/
├── manifest.json     # schema: "oa-git/2", view_type: "symbol", content_sha256, full_sha256
├── symbol.yaml       # schema: "oa-git-symbol/1"
└── omissions.json    # present only when manifest.omissions_count > 0
```

- `manifest.json` uses the universal `oa-git/2` manifest contract (`electrical_sha256: null`, `placement_sha256: null`, `content_sha256` covering `symbol.yaml`, `full_sha256` binding `view_type`, `cellview`, `files`, `omissions_count`, and `metadata`).
- Any file tampering, symlink injection, stale `omissions.json`, or `cellview` mismatch fails closed with `SnapshotIntegrityError`.

## `symbol.yaml` Schema (`oa-git-symbol/1`)

```yaml
schema: oa-git-symbol/1
cellview:
  library: pll_28nm_lib
  cell: LC_vco
  view: symbol
pins:
  - name: S<2:0>
    direction: inputOutput
    xy: [1.2, -0.5]
    orient: R0
    pin_figure:
      kind: rect
      layer: pin
      purpose: drawing
      bbox: [[1.175, -0.525], [1.225, -0.475]]
port_order:
  - VDD
  - VSS
  - S<2:0>
graphics:
  shapes:
    - kind: arc
      layer: device
      purpose: drawing
      arc_bbox: [[1.5, -0.2], [1.7, 0.0]]
      ellipse_bbox: [[1.4, -0.2], [1.8, 0.2]]
    - kind: ellipse
      layer: device
      purpose: drawing
      bbox: [[2.0, -0.4], [2.4, 0.0]]
    - kind: path
      layer: device
      purpose: drawing1
      points: [[0.0, 0.0], [0.5, 0.0]]
      width: 0.0125
  labels:
    - text: S<2:0>
      xy: [1.3, -0.5]
      layer: pin
      purpose: label
      justify: centerLeft
      orient: R0
      font: stick
      height: 0.0625
      label_type: normalLabel
  nlp_labels:
    - text: "[@instanceName]"
      xy: [2.2, 0.35]
      layer: instance
      purpose: label
      justify: centerCenter
      orient: R0
      font: stick
      height: 0.0625
      label_type: NLPLabel
  il_labels:
    - text: cdsName()
      xy: [0.0, 0.5]
      layer: annotate
      purpose: drawing7
      justify: lowerLeft
      orient: R0
      font: stick
      height: 0.0625
      label_type: ILLabel
  selection_box: [[1.1625, -1.4625], [3.3625, 0.4625]]
  origin: [0.0, 0.0]
  bbox: [[1.1625, -1.4625], [3.3625, 0.4625]]
properties:
  vendorName: TSMC
  partName: LC_vco
```

### Canonicalization & Separation Rules

- **Coordinates**: All numeric coordinates are finite, quantized to 6 decimal places, with `-0.0` normalized to `0.0`. Axis-aligned boxes (`bbox`, `selection_box`, `ellipse_bbox`) are normalized to `((min_x, min_y), (max_x, max_y))`. Ordered `arc_bbox` endpoints preserve their OA start/stop order.
- **Pins (`pins`)**: Unique non-empty names (bus names such as `DOUTP<9:0>` and `S<2:0>` are supported), validated against `VALID_PORT_DIRECTIONS` and `VALID_ORIENTS`, sorted by `name`. Pin figures default to `rect|pin:drawing` with `bbox`, and also support `polygon`, `ellipse`, `circle`, or `inst` (`lib`/`cell`/`view`).
- **Port Order (`port_order`)**: Preserved in exact positional order (never alphabetically re-sorted) to protect positional netlisting.
- **Strict Separation**: Pin figures and `rect|instance:drawing` selection boxes are never duplicated into `graphics.shapes`.
- **Shape Kinds**: Canonical set `{line, rect, polygon, path, arc, ellipse, circle}` (`rectangle` normalizes to `rect`). Unsupported shapes/properties are recorded in `omissions.json`.

## Three-Domain Semantic Diff & Three-Way Merge

`oa_git.symbol_diff.diff_symbols(old, new)` splits all changes into three domains:

| Domain | Tracked Changes |
|---|---|
| `pin_contract_changes` | Pin added/removed (renames are never guessed), existing pin `direction` or `pin_figure` changed, `port_order` changed, cellview scalar `properties` changed |
| `pin_geometry_changes` | Existing pin `xy` or `orient` moved without modifying pin contract |
| `graphics_changes` | `shapes`, `labels`, `nlp_labels`, `il_labels` (multiset diffs), `selection_box`, and `origin` |

`oa_git.symbol_merge.merge_symbols(base, ours, theirs)` merges per-pin fields independently (`direction`, `pin_figure`, `xy`, `orient`), merges `properties` per key, merges shapes/labels as canonical multisets, and checks positional `port_order` / `selection_box` / `origin` atomically. Any conflict produces `ok=False`, `snapshot=None`, and sorted `SymbolMergeConflict` entries.

## Official IC26.1 SKILL Mapping

Both `src/oa_git/skill/export_symbol.il` (`capture_symbol`) and `src/oa_git/skill/restore_symbol.il` (`restore_symbol`) use exclusively official `db*` / `dd*` APIs with unique `oagitSym*` procedure names and frozen `oagit_common.il` helpers.

| Symbol Element | Export (`export_symbol.il`) | Restore (`restore_symbol.il`) |
|---|---|---|
| CellView open | `dbFindOpenCellViewByName` / `dbOpenCellViewByType(... "r")`, rejects `modifiedButNotSaved` | Fresh draft only: `dbOpenCellViewByType(lib draft view "schematicSymbol" "w")` |
| Terminal & pin | `cv~>terminals`, `term~>pins`, `pin~>fig` (tracked in `pinFigIds` hash table) | `dbMakeNet`, `dbCreateTerm`, `dbCreateRect`/`dbCreateEllipse`/`dbCreatePolygon`/`dbCreateInst`, `dbCreatePin` |
| Selection box | First `rect` on `instance:drawing` -> `graphics.selection_box` | `dbCreateRect(cv '("instance" "drawing") selection_box)` |
| Body shapes | `line`, `rect`, `polygon`, `path` (`width`), `arc` (`arcBBox` with IC26.1 `bBox` fallback, `ellipseBBox`), `ellipse`, `circle` | `dbCreateLine`, `dbCreateRect`, `dbCreatePolygon`, `dbCreatePath`, `dbCreateArc(cv lpp ellipseBBox arcBBox)`, `dbCreateEllipse` |
| Labels | `label` & `textDisplay` classified by `labelType` -> `normalLabel` / `NLPLabel` / `ILLabel` | `dbCreateLabel` + `lbl~>labelType = "NLPLabel"` / `"ILLabel"` |
| `portOrder` & props | `portOrder` (`ILList`) -> `port_order`; filters `pin#`, `viewSubType`, `*LastChanged` / `*LastUpdated` | `dbDeletePropByName` + `dbCreateProp(cv "portOrder" "ILList" ...)` and scalar props |
| Atomic publish | Read-only; closes opened `cv` | Pin contract verify -> `dbSave` -> `dbCopyCellView(cv lib cell view nil nil t)` -> `dbPurge` + `ddDeleteObj` draft cleanup |
