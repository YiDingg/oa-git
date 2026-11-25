# oa-git

**Git-native version control for Cadence OpenAccess (`sch.oa`, `symbol.oa`, and `layout.oa`) designs.**

`oa-git` translates Virtuoso OpenAccess CellViews into deterministic, human-readable text snapshots using **official documented Cadence SKILL APIs only**—never parsing or writing raw `.oa` binaries. Standard Git handles all commits, branches, PR reviews, and reverts.

## Features

- **3 OpenAccess Views**: Full capture, semantic diff, 3-way merge, and atomic restore for **Schematic** (`circuit.yaml` + `placement.yaml`), **Symbol** (`symbol.yaml`), and **Layout** (`objects.jsonl` + `definitions.jsonl`, all 15 OA physical object types).
- **7-Command CLI + Native Git**: Only `init`, `status`, `capture`, `diff`, `merge`, `restore`, and `daemon`; everything else (`commit`, `push`, `branch`, `revert`) uses standard `git`.
- **Domain-Separated Diff & Merge**: Separates electrical/connectivity truth from placement/routing geometry so tidying wires or moving pins doesn't pollute electrical diffs; guards coupled CDF parameters and same-layer layout overlaps.
- **Fail-Closed & Fast**: Refuses unsaved buffers, live `*.cdslck` locks, and non-allowlisted libraries; restores atomically via draft-and-publish with readback verification. Local dirty index skips unchanged views in `< 0.2s`, and a lazy headless daemon stays warm for 1 hour by default (`idle_timeout_s = 3600`, configurable; `0` disables auto-exit).

## Snapshot Layout (`oa-git/2`)

```text
snapshots/<library>/<cell>/<view>/
├── manifest.json      # Unified oa-git/2 manifest + SHA-256 digests
├── circuit.yaml       # [schematic] Electrical truth (ports, nets, instances, CDF params)
├── placement.yaml     # [schematic] Placement geometry (xy/orient, wires, labels, annotations)
├── symbol.yaml        # [symbol]    Pin contract, portOrder, shapes, and labels
├── objects.jsonl      # [layout]    Deterministically sorted physical OA objects
└── definitions.jsonl  # [layout]    Deduplicated PCell / via / property definitions (optional)
```

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest   # offline test suite (no Cadence required)
```

## Quickstart

Run inside any Virtuoso working directory containing `cds.lib`:

```bash
# 1. Zero-config init (git init, safe .gitignore for OA dirs, cds.lib discovery, PATH virtuoso probe)
oagit init

# 2. Capture views (auto-detects schematic, symbol, layout, and custom layout_* views)
oagit capture my_lib/opamp/schematic my_lib/opamp/symbol my_lib/opamp/layout
oagit capture my_lib/top/schematic --hier --include-symbols   # full hierarchy in one session

# 3. Commit with standard Git
git add snapshots/ .gitignore .oagit/config.json
git commit -m "feat: initial baseline"
```

### Everyday Commands

```bash
oagit status                         # integrity, dirty index, OA locks, .gitignore checks
oagit capture                        # incremental sync (--changed across all tracked views)
oagit diff HEAD~1..HEAD --stat       # semantic diff across schematic, symbol, and layout
oagit diff --electrical-only         # filter out pure placement/routing changes
oagit diff --connectivity-only       # layout net/pin/PCell/via changes only
oagit restore my_lib/opamp/schematic --target my_lib/opamp/schematic --overwrite
oagit init --sync                    # refresh config & .gitignore after adding new OA libraries
oagit daemon [start|stop|status]     # optional; daemon auto-starts lazily when needed
```

*(For remote Virtuoso hosts over SSH, pass `--ssh-host <alias> --cds-lib /path/to/cds.lib` to `oagit init`.)*

## Documentation

- [Architecture & CLI Contract](docs/ARCHITECTURE.md)
- [Symbol Reference (`symbol.yaml`)](docs/SYMBOL.md)
- [Physical Layout Reference (`objects.jsonl`)](docs/LAYOUT.md)
- [Headless Daemon, Dirty Index & Batch Spool](docs/DAEMON.md)

## License

MIT ([LICENSE](LICENSE)) — clean-room implementation using documented Virtuoso SKILL APIs only.
