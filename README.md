# oa-git

**Deterministic text serialization, semantic diff, object-level three-way merge, and Fail-Closed Git version control for Cadence OpenAccess (`sch.oa`, `symbol.oa`, and `layout.oa`) designs.**

`oa-git` is an MIT-licensed, lightweight, terminal-first, AI-agnostic tool that bridges Cadence Virtuoso OpenAccess CellViews and standard Git workflows—without ever parsing or reversing `.oa` binary files.

## Highlights

- **Out-of-the-Box `oagit init`**: Run `oagit init` inside any Virtuoso working directory containing `cds.lib` to automatically initialize Git, write a safe root-anchored `/<lib>/` `.gitignore` block, discover repo-local OA libraries (excluding Cadence built-ins, PDKs, `COMBINE`, and `Invisible` libraries), set a repo-local `allow_libraries` safety boundary, and detect `virtuoso` from `PATH` or `$CDSHOME`.
- **Minimal 7-Command OA CLI**: Exposes only 7 OA-specific commands (`init`, `status`, `capture`, `diff`, `merge`, `restore`, `daemon`), delegating `commit` / `push` / `branch` / `checkout` / `revert` to native `git`. All commands automatically handle **Schematic + Symbol + Layout** (including custom layout view names such as `layout_pulsic`) via `view_contract.py`.
- **Schematic (`sch.oa` -> `circuit.yaml` + `placement.yaml`)**: Deterministic canonical serialization with `params`/`derived` separation, multi-pass coupled CDF callback convergence (`--recompute-derived`), cross-instance shared CDF isolation, anonymous auto-net (`^net[0-9]+$`) bijection isomorphism, wide bus wires, annotation shapes, static PDK metadata filtering, and bounded `.cxt` crash retry (`exit 139`).
- **Symbol (`symbol.oa` -> `symbol.yaml`)**: Full support for bus pins (`DOUTP<9:0>`, `S<2:0>`), `rect|pin:drawing` pin figures, `line`/`rect`/`polygon`/`path`/`arc`/`ellipse`/`circle`, `selection_box`, `origin`/`bBox`, `normalLabel`/`NLPLabel`/`ILLabel`, `portOrder` + `ILList` `termOrder`, 3-domain semantic diff (pin contract / pin geometry / graphics), and 3-way merge.
- **Physical Layout (`layout.oa` -> `objects.jsonl` + `definitions.jsonl`)**: Supports all 15 physical OA object types (`net`, `pin`, `instance` incl. PCell params, `mosaic`, `via`, `rect`, `path_seg`, `path`, `polygon`, `ellipse`, `line`, `label`, `blockage`, `boundary`, `fig_group`), 3-domain semantic diff (Connectivity / Geometry / Annotation) with `--stat` and `--connectivity-only`, spatial-overlap-guarded 3-way merge (`--allow-spatial-overlap`), and atomic draft-and-publish restore with `pcell_verified` + `connectivity_verified` + `readback_verified`.
- **Git-Like Ergonomics & Warm Headless Daemon**: Upward `.oagit/config.json` discovery, dynamic read-time `cds.lib` sync + `.gitignore` trap warnings in `oagit status`, `.oagit/index.json` dirty-file skip across all 3 views (`< 0.2s` with zero Virtuoso calls on `--changed`), single-session hierarchical batch (`--hier --include-symbols`), and lazy auto-sleep Virtuoso Daemon (`oagit-spool/1`, default `3600s` / 1-hour idle timeout, `0` disables idle auto-exit, native `ipcSleep(1)` idle backoff) with optional FD-detached SSH `ControlMaster`.

---

## Why `oa-git`?

Cadence OpenAccess files (`sch.oa`, `symbol.oa`, `layout.oa`) are proprietary binary databases. Putting them directly in Git yields `Binary files differ`, breaking PR code reviews, line-level blame, and branch merges.

`oa-git` translates every CellView through **official documented Virtuoso SKILL APIs** (`db*`, `dd*`, `sch*`, `cdf*`, `rod*`, `tech*`) into a deterministic, human-reviewable snapshot directory under the unified `oa-git/2` manifest:

```text
snapshots/<library>/<cell>/<view>/
├── manifest.json      # Unified oa-git/2 manifest + SHA-256 integrity bindings
├── circuit.yaml       # [schematic] Electrical Truth: ports, nets, instances, primary params, derived state
├── placement.yaml     # [schematic] Placement Geometry: instance xy/orient, pins, wires, labels, annotations, sheet
├── symbol.yaml        # [symbol]    Pin contract (with pin_figure & port_order) + visual graphics & typed labels
├── objects.jsonl      # [layout]    Deterministically sorted OA physical objects (one JSON object per line)
├── definitions.jsonl  # [layout]    Deduplicated PCell / via / property definition table (when present)
└── omissions.json     # [optional]  Audit file (present only if unsupported objects were encountered)
```

Domain separation across all three views unlocks true Git collaboration:
- **Schematic**: Moving devices or tidying wires leaves `circuit.yaml` and `electrical_sha256` untouched; concurrent sizing edits to coupled CDF parameters merge cleanly via `derived.status: stale` and recompute legitimately via `oagit restore --recompute-derived`.
- **Symbol**: Moving pin labels or graphics produces zero diff under `--contract-only` / `--electrical-only`.
- **Layout**: Pure routing, placement, and pin-geometry (`bbox`) edits produce zero diff under `--connectivity-only`.

---

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/oagit --help
```

Run the offline test suite (zero Cadence dependency):

```bash
.venv/bin/pytest
```

---

## Quickstart & Git-Like CLI Workflow

### 3-Command Zero-Config Quickstart (Local Virtuoso Workstation)

```bash
cd my_virtuoso_workdir   # contains cds.lib
oagit init               # auto git init, safe .gitignore, cds.lib discovery, PATH virtuoso probe
oagit capture my_lib/opamp/schematic my_lib/opamp/symbol my_lib/opamp/layout
git add snapshots/ .gitignore .oagit/config.json && git commit -m "feat: initial 3-view baseline"
```

### Workspace Sync, Init Flags & Remote SSH Workstation

When you create a new OA library in the Virtuoso GUI, `oagit status` and `oagit capture` automatically pick it up from `cds.lib` at read time. To persist newly discovered libraries into `.oagit/config.json` and refresh the managed root-anchored `/<lib>/` `.gitignore` block, run:

```bash
oagit init --sync
```

Common `oagit init` options:
- `--sync`: Re-parse `cds.lib`, update discovered libraries, and refresh the managed `.gitignore` block without overwriting unrelated settings.
- `--no-git`: Skip automatic `git init` and `.gitignore` management.
- `--no-auto-allow`: Do not auto-populate discovered repo-local libraries into `allow_libraries`.
- `--no-auto-discover`: Disable automatic `./cds.lib` library and `PATH`/`$CDSHOME` Virtuoso detection.
- `--v1`: Write legacy `.oagit.json` instead of the default v2 `.oagit/config.json`.
- `--backend fixture`: Initialize an offline fixture workspace for local testing/demos without Virtuoso.

For a **Remote SSH Workstation** (local Git repo driving Virtuoso over SSH):

```bash
oagit init \
  --ssh-host eda-server \
  --cds-lib /path/to/remote/cds.lib \
  --allow-library my_sandbox_lib
```

*(Optionally add `--virtuoso-cmd virtuoso`, `--remote-workdir /path/to/remote/workdir`, or `--oa-lib my_lib=/path/to/remote/my_lib` if your remote setup uses non-default paths. Config is discovered upward from any subdirectory, just like `.git/`.)*

### Capture Schematics, Symbols, Layouts, or Entire Hierarchies

```bash
# Capture all 3 views together (auto-detects schematic, symbol, layout, and custom layout_* views)
oagit capture my_lib/opamp/schematic my_lib/opamp/symbol my_lib/opamp/layout

# Custom non-standard layout view names (e.g. layout_pulsic) auto-detect, or override with --view-type layout
oagit capture my_lib/amp/layout_pulsic

# Entire schematic hierarchy + custom subcell symbols in ONE Virtuoso session
oagit capture my_lib/top_adc/schematic --hier --include-symbols

# Incremental sync: 0-arg capture defaults to --changed over all tracked CellViews (incl. layout.oa, < 0.2s skip!)
oagit capture

git add snapshots/
git commit -m "chore: capture opamp schematic, symbol, and layout"
```

### Inspect Status & Semantic Diffs Across Git History

```bash
# Verify integrity, dirty index (sch.oa / symbol.oa / layout.oa), OA locks, and .gitignore health
oagit status

# Working snapshots vs HEAD, or Git range across ALL changed views (schematic + symbol + layout)
oagit diff
oagit diff HEAD~1..HEAD

# Concise 1-line-per-view diff summary + layout layer histogram
oagit diff HEAD~1..HEAD --stat

# Domain-filtered semantic diffs
oagit diff HEAD~1..HEAD --electrical-only     # schematic electrical truth & symbol pin contract
oagit diff HEAD~1..HEAD --connectivity-only   # layout net/pin/PCell/via connectivity (plus schematic/symbol contract)
oagit diff HEAD~1..HEAD --contract-only       # symbol pin contract only
```

### Three-Way Merge and Fail-Closed Atomic Restore

```bash
# Object-level three-way merge (schematic, symbol, or layout; pass --allow-spatial-overlap for intentional layout overlap)
oagit merge my_lib/opamp/layout ours_dir/ theirs_dir/ -o merged_dir/

# Revert snapshots with native git, then restore any view safely into an allowlisted library
git checkout HEAD~1 -- snapshots/
oagit restore my_lib/opamp/schematic --target my_sandbox_lib/opamp/schematic --overwrite --inline-readback
oagit restore my_lib/opamp/symbol    --target my_sandbox_lib/opamp/symbol    --overwrite --inline-readback
oagit restore my_lib/opamp/layout    --target my_sandbox_lib/opamp/layout    --overwrite
```

### Warm Daemon Lifecycle

```bash
oagit daemon start      # optional: daemon also auto-starts lazily on first capture/restore
oagit daemon            # defaults to 'status'
oagit daemon stop
```

The headless daemon stays warm for **`3600s` (1 hour)** by default (`daemon.idle_timeout_s`, configurable via `--idle-timeout-s <N>`; set `0` to disable idle auto-exit) and uses native Cadence SKILL `ipcSleep(1)` idle backoff to keep idle CPU usage minimal. Transient daemon state and SSH `ControlMaster` sockets (`.oagit/daemon/`, `.oagit/ssh/`, `.oagit/index.json`, `.oagit/index.lock`) are excluded automatically by the managed `.gitignore` block written by `oagit init`.

---

## Architecture & Documentation

- [Architecture & Wire Contracts (`oa-git/2`)](docs/ARCHITECTURE.md)
- [Symbol Support (`symbol.yaml`)](docs/SYMBOL.md)
- [Physical Layout Support (`objects.jsonl` / `definitions.jsonl`)](docs/LAYOUT.md)
- [Lazy Daemon & Batch Spool (`oagit-spool/1`, `oagit-batch/1`)](docs/DAEMON.md)

## Compliance & License

- **License**: MIT ([LICENSE](LICENSE))
- **OpenAccess Compliance**: Uses only documented Virtuoso SKILL APIs; never reads or writes raw `.oa` binary bytes.
