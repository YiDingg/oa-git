"""Zero-config Cadence workspace discovery, Virtuoso detection, and Git hygiene.

This pure, offline-testable module implements oa-git v0.3 onboarding helpers:
- Top-level Cadence ``cds.lib`` parsing (without following INCLUDE/SOFTINCLUDE)
- Portable library classification separating writable workspace libraries from
  external design libraries, read-only/PDK libraries, built-in Cadence libraries,
  and Library Manager display-only entries
- Portable Virtuoso executable discovery
- Git repository initialization and marker-bounded ``.gitignore`` management
- Strictly read-only ``.gitignore`` health checks for ``oagit status``
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Literal

from .model import SchemaValidationError

MANAGED_GITIGNORE_START = "# >>> oa-git managed ignore >>>"
MANAGED_GITIGNORE_END = "# <<< oa-git managed ignore <<<"

BUILTIN_CADENCE_LIBS = frozenset(
    {
        "analogLib",
        "basic",
        "cdsDefTechLib",
        "US_8ths",
        "ahdlLib",
        "functional",
        "rfLib",
        "rfExamples",
        "rfTlineLib",
        "sample",
        "sheets",
        "avTech",
        "esd",
        "Cadence",
        "Animate_Cache",
    }
)

_PDK_TECH_MARKERS = ("tech.db", "libInit.il", "libInit.ile", "libInit.cxt")
_SYSTEM_ROOTS = (Path("/opt"), Path("/usr"), Path("/etc"), Path("/lib"))
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

_MACHINE_LOCAL_IGNORES = (
    ".oagit/index.json",
    ".oagit/index.lock",
    ".oagit/daemon/",
    ".oagit/ssh/",
    ".oagit/tmp/",
)

_CADENCE_RUNTIME_IGNORES = (
    "*.cdslck*",
    "*.lck",
    "*.oacache*",
    "*.*%",
    "CDS.log*",
    "libManager.log*",
    "panic.log*",
    ".cadence/",
)

Classification = Literal[
    "design_local",
    "design_external",
    "system_or_pdk",
    "invisible",
    "missing",
]


@dataclass(frozen=True)
class CdsLibEntry:
    """Classified ``DEFINE`` entry discovered from a workspace ``cds.lib``."""

    name: str
    raw_path: str
    resolved_path: Path
    exists: bool
    is_writable: bool
    in_workspace: bool
    classification: Classification
    reason: str


@dataclass(frozen=True)
class WorkspaceDiscovery:
    """Aggregated workspace discovery results from ``cds.lib``."""

    cds_lib: Path | None
    entries: tuple[CdsLibEntry, ...]
    oa_libs: dict[str, str]
    allow_libraries: tuple[str, ...]
    repo_local_ignore_paths: tuple[str, ...]
    excluded: tuple[CdsLibEntry, ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tokenize_cds_lib_line(line: str) -> list[str]:
    """Tokenize a single ``cds.lib`` line while respecting quotes and comments."""

    tokens: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None
    has_token = False
    i = 0
    length = len(line)
    while i < length:
        ch = line[i]
        if in_quote is not None:
            if (
                ch == "\\"
                and in_quote == '"'
                and i + 1 < length
                and line[i + 1] in ('"', "\\")
            ):
                buf.append(line[i + 1])
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "#":
            break
        if ch == "-" and i + 1 < length and line[i + 1] == "-":
            break
        if ch in ('"', "'"):
            in_quote = ch
            has_token = True
            i += 1
            continue
        if ch.isspace():
            if has_token:
                tokens.append("".join(buf))
                buf = []
                has_token = False
            i += 1
            continue
        buf.append(ch)
        has_token = True
        i += 1
    if has_token:
        tokens.append("".join(buf))
    return tokens


def _expand_env_vars(raw_path: str, env: Mapping[str, str]) -> tuple[str, bool]:
    """Expand ``$VAR`` and ``${VAR}`` using ``env``; report if any remained unresolved."""

    unresolved = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal unresolved
        var_name = match.group(1) or match.group(2)
        value = env.get(var_name)
        if value is None or value == "":
            unresolved = True
            return match.group(0)
        return value

    expanded = _ENV_VAR_RE.sub(_replace, raw_path)
    return expanded, unresolved


def _expand_tilde(raw_path: str, env: Mapping[str, str]) -> Path:
    if raw_path == "~" or raw_path.startswith("~/"):
        home = env.get("HOME")
        if home:
            suffix = raw_path[2:] if raw_path.startswith("~/") else ""
            return Path(home) / suffix if suffix else Path(home)
    return Path(raw_path).expanduser()


def _is_dir_writable(path: Path) -> bool:
    """Return True iff ``path`` is writable, respecting read-only mode bits even as uid 0."""

    try:
        st = path.stat()
    except OSError:
        return False
    if (stat.S_IMODE(st.st_mode) & 0o222) == 0:
        return False
    return os.access(path, os.W_OK)


def _is_same_or_under(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _is_system_or_eda_path(
    resolved_path: Path, raw_expanded: Path, env: Mapping[str, str]
) -> bool:
    cdshome_raw = env.get("CDSHOME")
    if cdshome_raw and cdshome_raw.strip():
        cdshome = _expand_tilde(cdshome_raw.strip(), env).resolve()
        if _is_same_or_under(resolved_path, cdshome):
            return True
    for sys_root in _SYSTEM_ROOTS:
        if _is_same_or_under(resolved_path, sys_root):
            return True
        if raw_expanded.is_absolute() and _is_same_or_under(raw_expanded, sys_root):
            return True
    return False


def _has_pdk_tech_markers(lib_dir: Path) -> bool:
    for marker in _PDK_TECH_MARKERS:
        if (lib_dir / marker).exists():
            return True
    return False


def _normalize_repo_local_path(raw: str) -> str | None:
    cleaned = raw.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip("/")
    if not cleaned or cleaned in {".", ".."}:
        return None
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target_mode: int
    try:
        target_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        target_mode = 0o644
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, target_mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# cds.lib parsing & workspace library discovery
# ---------------------------------------------------------------------------


def parse_cds_lib_statements(
    cds_lib_path: Path,
) -> tuple[list[tuple[str, str]], set[str], set[str]]:
    """Parse top-level statements from ``cds_lib_path`` without following includes.

    Returns ``(defines, invisible_libs, combined_containers)`` where:
    - ``defines`` is an ordered list of ``(lib_name, raw_path)`` pairs from ``DEFINE``
    - ``invisible_libs`` contains libraries marked ``ASSIGN <lib> DISPLAY Invisible``
    - ``combined_containers`` contains containers declared ``ASSIGN <name> COMBINE ...``
    """

    defines: list[tuple[str, str]] = []
    invisible_libs: set[str] = set()
    combined_containers: set[str] = set()

    text = Path(cds_lib_path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        tokens = _tokenize_cds_lib_line(line)
        if not tokens:
            continue
        keyword = tokens[0].upper()
        if keyword == "DEFINE":
            if len(tokens) >= 3 and tokens[1] and tokens[2]:
                defines.append((tokens[1], tokens[2]))
        elif keyword == "UNDEFINE":
            if len(tokens) >= 2 and tokens[1]:
                undef_name = tokens[1]
                defines = [(n, p) for (n, p) in defines if n != undef_name]
        elif keyword == "ASSIGN":
            if len(tokens) >= 4 and tokens[2].upper() == "DISPLAY":
                disp = tokens[3].lower()
                if disp == "invisible":
                    invisible_libs.add(tokens[1])
                elif disp == "visible":
                    invisible_libs.discard(tokens[1])
            elif len(tokens) >= 3 and tokens[2].upper() == "COMBINE":
                combined_containers.add(tokens[1])
    return defines, invisible_libs, combined_containers


def discover_workspace_libraries(
    root: Path,
    *,
    cds_lib: Path | None = None,
    snapshots_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> WorkspaceDiscovery:
    """Discover and classify Cadence OA libraries declared in a workspace ``cds.lib``."""

    resolved_root = Path(root).expanduser().resolve()
    eff_env: Mapping[str, str] = os.environ if env is None else env

    if cds_lib is None:
        candidate = resolved_root / "cds.lib"
        if not candidate.is_file():
            return WorkspaceDiscovery(
                cds_lib=None,
                entries=(),
                oa_libs={},
                allow_libraries=(),
                repo_local_ignore_paths=(),
                excluded=(),
            )
        resolved_cds_lib = candidate.resolve()
    else:
        cds_path = Path(cds_lib).expanduser()
        if not cds_path.is_absolute():
            cds_path = resolved_root / cds_path
        resolved_cds_lib = cds_path.resolve()
        if not resolved_cds_lib.is_file():
            return WorkspaceDiscovery(
                cds_lib=None,
                entries=(),
                oa_libs={},
                allow_libraries=(),
                repo_local_ignore_paths=(),
                excluded=(),
            )

    if snapshots_dir is None:
        resolved_snapshots = (resolved_root / "snapshots").resolve()
    else:
        snap_path = Path(snapshots_dir).expanduser()
        if not snap_path.is_absolute():
            snap_path = resolved_root / snap_path
        resolved_snapshots = snap_path.resolve()

    internal_dirs = (
        resolved_snapshots,
        (resolved_root / ".git").resolve(),
        (resolved_root / ".oagit").resolve(),
    )

    defines, invisible_libs, combined_containers = parse_cds_lib_statements(
        resolved_cds_lib
    )

    # Deduplicate duplicate DEFINE names: last DEFINE in cds.lib wins.
    deduped_defines: dict[str, str] = {}
    for lib_name, raw_path in defines:
        if lib_name in deduped_defines:
            del deduped_defines[lib_name]
        deduped_defines[lib_name] = raw_path

    entries: list[CdsLibEntry] = []
    oa_libs: dict[str, str] = {}
    allow_list: list[str] = []
    ignore_paths: list[str] = []
    excluded: list[CdsLibEntry] = []

    cds_base_dir = resolved_cds_lib.parent

    for name, raw_path in deduped_defines.items():
        expanded_str, has_unresolved = _expand_env_vars(raw_path, eff_env)
        expanded_path = _expand_tilde(expanded_str, eff_env)
        if has_unresolved:
            resolved_path = (
                expanded_path
                if expanded_path.is_absolute() or expanded_str.startswith("$")
                else cds_base_dir / expanded_path
            )
        else:
            candidate_path = (
                expanded_path
                if expanded_path.is_absolute()
                else cds_base_dir / expanded_path
            )
            resolved_path = candidate_path.resolve()

        # 1. Invisible or Library Manager COMBINE container
        if name in invisible_libs:
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=False if has_unresolved else resolved_path.is_dir(),
                is_writable=False,
                in_workspace=False
                if has_unresolved
                else (resolved_path != resolved_root and resolved_path.is_relative_to(resolved_root)),
                classification="invisible",
                reason="marked DISPLAY Invisible in cds.lib",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        if name in combined_containers:
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=False if has_unresolved else resolved_path.is_dir(),
                is_writable=False,
                in_workspace=False
                if has_unresolved
                else (resolved_path != resolved_root and resolved_path.is_relative_to(resolved_root)),
                classification="invisible",
                reason="Library Manager COMBINE container",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 2. Built-in Cadence library
        if name in BUILTIN_CADENCE_LIBS:
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=False if has_unresolved else resolved_path.is_dir(),
                is_writable=False,
                in_workspace=False
                if has_unresolved
                else (resolved_path != resolved_root and resolved_path.is_relative_to(resolved_root)),
                classification="system_or_pdk",
                reason="built-in Cadence library",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # Unresolved environment variable
        if has_unresolved:
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=False,
                is_writable=False,
                in_workspace=False,
                classification="system_or_pdk",
                reason="unresolved environment variable",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 3. Internal oa-git / git / snapshots directory
        if (
            any(_is_same_or_under(resolved_path, internal) for internal in internal_dirs)
            or ".git" in resolved_path.parts
            or ".oagit" in resolved_path.parts
        ):
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=resolved_path.is_dir(),
                is_writable=False,
                in_workspace=(
                    resolved_path != resolved_root
                    and resolved_path.is_relative_to(resolved_root)
                ),
                classification="invisible",
                reason="internal oa-git/git directory",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 4. System or EDA installation path
        if _is_system_or_eda_path(resolved_path, expanded_path, eff_env):
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=resolved_path.is_dir(),
                is_writable=False,
                in_workspace=(
                    resolved_path != resolved_root
                    and resolved_path.is_relative_to(resolved_root)
                ),
                classification="system_or_pdk",
                reason="system or EDA install path",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 5. Missing directory
        if not resolved_path.is_dir():
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=False,
                is_writable=False,
                in_workspace=(
                    resolved_path != resolved_root
                    and resolved_path.is_relative_to(resolved_root)
                ),
                classification="missing",
                reason="directory does not exist",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 6 & 7. Workspace containment and writability
        in_workspace = (resolved_path != resolved_root) and resolved_path.is_relative_to(
            resolved_root
        )
        is_writable = _is_dir_writable(resolved_path)

        # 8. Read-only library directory
        if not is_writable:
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=True,
                is_writable=False,
                in_workspace=in_workspace,
                classification="system_or_pdk",
                reason="read-only library directory",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 9. External PDK technology library heuristic
        if not in_workspace and _has_pdk_tech_markers(resolved_path):
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=True,
                is_writable=True,
                in_workspace=False,
                classification="system_or_pdk",
                reason="PDK technology library",
            )
            entries.append(entry)
            excluded.append(entry)
            continue

        # 10. Writable workspace library (design_local)
        if in_workspace:
            rel_posix = resolved_path.relative_to(resolved_root).as_posix()
            entry = CdsLibEntry(
                name=name,
                raw_path=raw_path,
                resolved_path=resolved_path,
                exists=True,
                is_writable=True,
                in_workspace=True,
                classification="design_local",
                reason="writable workspace library",
            )
            entries.append(entry)
            oa_libs[name] = rel_posix
            allow_list.append(name)
            ignore_paths.append(rel_posix)
            continue

        # 11. Writable external design library (design_external)
        entry = CdsLibEntry(
            name=name,
            raw_path=raw_path,
            resolved_path=resolved_path,
            exists=True,
            is_writable=True,
            in_workspace=False,
            classification="design_external",
            reason="writable external design library",
        )
        entries.append(entry)
        oa_libs[name] = str(resolved_path)

    sorted_oa_libs = {k: oa_libs[k] for k in sorted(oa_libs)}
    sorted_allow = tuple(sorted(set(allow_list)))
    sorted_ignores = tuple(sorted(set(ignore_paths)))

    return WorkspaceDiscovery(
        cds_lib=resolved_cds_lib,
        entries=tuple(entries),
        oa_libs=sorted_oa_libs,
        allow_libraries=sorted_allow,
        repo_local_ignore_paths=sorted_ignores,
        excluded=tuple(excluded),
    )


# ---------------------------------------------------------------------------
# Virtuoso command detection
# ---------------------------------------------------------------------------


def detect_virtuoso_cmd(
    *,
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """Detect a portable local Virtuoso command name or executable path."""

    eff_env: Mapping[str, str] = os.environ if env is None else env

    explicit = eff_env.get("OAGIT_VIRTUOSO_CMD")
    if explicit and explicit.strip():
        return explicit.strip()

    def _lookup(cmd: str) -> str | None:
        if which_fn is shutil.which and env is not None:
            return shutil.which(cmd, path=eff_env.get("PATH", ""))
        return which_fn(cmd)

    if _lookup("virtuoso"):
        return "virtuoso"
    if _lookup("virtuoso261"):
        return "virtuoso261"

    cdshome = eff_env.get("CDSHOME")
    if cdshome and cdshome.strip():
        candidate = (
            _expand_tilde(cdshome.strip(), eff_env)
            / "tools"
            / "dfII"
            / "bin"
            / "virtuoso"
        )
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


# ---------------------------------------------------------------------------
# Git repository & managed .gitignore helpers
# ---------------------------------------------------------------------------


def is_inside_git_repo(root: Path) -> Path | None:
    """Return resolved Git top-level directory if ``root`` is inside a Git repo."""

    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(resolved_root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output:
        return None
    return Path(output).resolve()


def ensure_git_repo(root: Path) -> tuple[bool, Path | None]:
    """Ensure ``root`` is inside a Git repository, running ``git init`` if needed.

    Returns ``(initialized, toplevel_path)``.
    """

    resolved_root = Path(root).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    toplevel = is_inside_git_repo(resolved_root)
    if toplevel is not None:
        return False, toplevel
    try:
        result = subprocess.run(
            ["git", "-C", str(resolved_root), "init"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False, None
    if result.returncode != 0:
        return False, None
    return True, resolved_root


def render_managed_gitignore_block(repo_local_ignore_paths: Sequence[str]) -> str:
    """Render the deterministic marker-bounded ``.gitignore`` block for oa-git."""

    normalized_libs = sorted(
        {
            norm
            for raw in repo_local_ignore_paths
            if (norm := _normalize_repo_local_path(raw)) is not None
        }
    )
    lines: list[str] = [
        MANAGED_GITIGNORE_START,
        "# oa-git machine-local runtime state",
        *_MACHINE_LOCAL_IGNORES,
        "# Cadence locks, caches, and logs",
        *_CADENCE_RUNTIME_IGNORES,
    ]
    if normalized_libs:
        lines.append("# Repo-local OA binary libraries (root-anchored so snapshots/ stays tracked)")
        for rel in normalized_libs:
            lines.append(f"/{rel}/")
    lines.append(MANAGED_GITIGNORE_END)
    return "\n".join(lines) + "\n"


def update_managed_gitignore(
    root: Path,
    repo_local_ignore_paths: Sequence[str],
) -> tuple[Path, bool]:
    """Idempotently create or update the oa-git managed block in ``<root>/.gitignore``.

    Preserves all existing user lines outside the managed markers. Writes
    atomically only when file content changes.
    """

    resolved_root = Path(root).expanduser().resolve()
    gitignore_path = resolved_root / ".gitignore"
    block = render_managed_gitignore_block(repo_local_ignore_paths)

    if gitignore_path.is_file():
        existing = gitignore_path.read_text(encoding="utf-8")
        lines = existing.splitlines(keepends=True)
        start_indices = [
            idx
            for idx, line in enumerate(lines)
            if line.rstrip("\r\n") == MANAGED_GITIGNORE_START
        ]
        end_indices = [
            idx
            for idx, line in enumerate(lines)
            if line.rstrip("\r\n") == MANAGED_GITIGNORE_END
        ]

        if not start_indices and not end_indices:
            if not existing:
                new_content = block
            elif existing.endswith("\n\n"):
                new_content = existing + block
            elif existing.endswith("\n"):
                new_content = existing + "\n" + block
            else:
                new_content = existing + "\n\n" + block
        elif (
            len(start_indices) == 1
            and len(end_indices) == 1
            and start_indices[0] < end_indices[0]
        ):
            start_idx = start_indices[0]
            end_idx = end_indices[0]
            new_content = "".join(lines[:start_idx]) + block + "".join(lines[end_idx + 1 :])
        else:
            raise SchemaValidationError(
                f"malformed oa-git managed ignore markers in {gitignore_path}: "
                f"found {len(start_indices)} start marker(s) and "
                f"{len(end_indices)} end marker(s)"
            )

        if new_content == existing:
            return gitignore_path, False
    else:
        new_content = block

    _atomic_write_text(gitignore_path, new_content)
    return gitignore_path, True


def _git_check_ignored_batch(root: Path, candidates: Sequence[str]) -> set[str]:
    """Return the subset of ``candidates`` matched by Git ignore rules (read-only)."""

    if not candidates:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"],
            input="\0".join(candidates) + "\0",
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return set()
    if result.returncode not in (0, 1):
        return set()
    return {item for item in result.stdout.split("\0") if item}


def check_gitignore_health(
    root: Path,
    *,
    snapshots_dir: Path,
    discovery: WorkspaceDiscovery,
) -> dict[str, list[str]]:
    """Perform strictly read-only ``.gitignore`` health checks for ``oagit status``."""

    resolved_root = Path(root).expanduser().resolve()
    git_root = is_inside_git_repo(resolved_root)
    if git_root is None:
        return {"unignored_oa_libs": [], "ignored_snapshot_paths": []}

    resolved_snapshots = Path(snapshots_dir).expanduser()
    if not resolved_snapshots.is_absolute():
        resolved_snapshots = resolved_root / resolved_snapshots
    resolved_snapshots = resolved_snapshots.resolve()

    oa_lib_candidates: list[tuple[str, str]] = []
    for entry in discovery.entries:
        if entry.classification == "design_local" and entry.resolved_path.is_dir():
            try:
                rel = entry.resolved_path.relative_to(resolved_root).as_posix()
            except ValueError:
                continue
            oa_lib_candidates.append((entry.name, f"{rel}/"))

    snapshot_candidates: list[str] = []
    if resolved_snapshots.is_relative_to(resolved_root):
        snapshots_rel = resolved_snapshots.relative_to(resolved_root).as_posix()
        prefix = "" if snapshots_rel in {"", "."} else f"{snapshots_rel}/"
        if prefix:
            snapshot_candidates.append(prefix)
        if resolved_snapshots.is_dir():
            for manifest in sorted(resolved_snapshots.rglob("manifest.json"), key=str):
                if manifest.is_file() or manifest.is_symlink():
                    try:
                        rel_manifest = manifest.relative_to(resolved_root).as_posix()
                    except ValueError:
                        continue
                    snapshot_candidates.append(rel_manifest)
        for entry in discovery.entries:
            if entry.classification == "design_local":
                snapshot_candidates.append(f"{prefix}{entry.name}/")

    all_candidates = list(
        dict.fromkeys([path for _, path in oa_lib_candidates] + snapshot_candidates)
    )
    ignored = _git_check_ignored_batch(resolved_root, all_candidates)

    unignored_oa_libs = sorted(
        {
            lib_name
            for lib_name, rel_slash in oa_lib_candidates
            if rel_slash not in ignored
        }
    )
    ignored_snapshot_paths = sorted(
        {cand for cand in snapshot_candidates if cand in ignored}
    )

    return {
        "unignored_oa_libs": unignored_oa_libs,
        "ignored_snapshot_paths": ignored_snapshot_paths,
    }


__all__ = [
    "BUILTIN_CADENCE_LIBS",
    "CdsLibEntry",
    "MANAGED_GITIGNORE_END",
    "MANAGED_GITIGNORE_START",
    "WorkspaceDiscovery",
    "check_gitignore_health",
    "detect_virtuoso_cmd",
    "discover_workspace_libraries",
    "ensure_git_repo",
    "is_inside_git_repo",
    "parse_cds_lib_statements",
    "render_managed_gitignore_block",
    "update_managed_gitignore",
]
