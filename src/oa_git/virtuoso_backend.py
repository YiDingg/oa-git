"""Headless Virtuoso/SKILL implementation of the :mod:`oa_git` backend.

The backend deliberately keeps all native execution behind a small runner seam.
The default runner is :func:`subprocess.run`, while tests and integrations can
inject a compatible callable without needing a Virtuoso installation or an SSH
connection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any
import uuid

if TYPE_CHECKING:
    from .layout_model import LayoutRestoreReceipt, LayoutSnapshot

from .batch_backend import (
    DEFAULT_MAX_HIER_DEPTH,
    DEFAULT_SKIP_LIBRARIES,
    parse_batch_capture_response,
    parse_batch_restore_response,
    schematic_snapshot_to_restore_item,
)
from .bridge import (
    parse_restore_response,
    raw_capture_to_snapshot,
    render_capture_request_skill,
    render_restore_request_skill,
    to_skill_literal,
)
from .daemon import VirtuosoDaemonClient
from .guards import (
    ensure_library_allowed,
    ensure_no_live_locks,
    ensure_no_omissions,
    verify_roundtrip,
)
from .model import (
    BATCH_PROTOCOL_V1,
    SPOOL_PROTOCOL_V1,
    BridgeExecutionError,
    CdfCouplingRule,
    CellViewRef,
    FailClosedError,
    RestoreReceipt,
    SchemaValidationError,
    SchematicSnapshot,
)
from .symbol_model import SymbolSnapshot
from .symbol_snapshot import (
    build_symbol_restore_request_dict,
    parse_symbol_restore_response,
    raw_symbol_capture_to_snapshot,
    verify_symbol_roundtrip,
)
from .view_contract import (
    BatchCaptureItem,
    BatchCaptureResult,
    OaSession,
    infer_view_type,
)

_ACTION_SCRIPTS: dict[str, str] = {
    "capture_schematic": "export_schematic.il",
    "restore_schematic": "restore_schematic.il",
    "capture_symbol": "export_symbol.il",
    "restore_symbol": "restore_symbol.il",
    "capture_layout": "export_layout.il",
    "restore_layout": "restore_layout.il",
    "capture_batch": "batch_capture.il",
    "restore_batch": "batch_restore.il",
}

_ACTION_HELPER_SCRIPTS: dict[str, tuple[str, ...]] = {
    "capture_batch": (
        "export_schematic.il",
        "export_symbol.il",
        "export_layout.il",
    ),
    "restore_batch": (
        "export_schematic.il",
        "restore_schematic.il",
        "export_symbol.il",
        "restore_symbol.il",
        "export_layout.il",
        "restore_layout.il",
    ),
}

_CRASH_RETURN_CODES: frozenset[int] = frozenset({139, -11, 134, -6})
_CRASH_SIGNATURES: tuple[str, ...] = (
    "corrupted double-linked list",
    "segmentation fault",
    "sigsegv",
    "double free or corruption",
    "aborted (core dumped)",
)


class _VirtuosoSignalCrashError(BridgeExecutionError):
    """Internal signal/heap crash marker eligible for bounded isolated retry."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _skill_string(value: str) -> str:
    """Render a path as a SKILL string literal."""

    escaped: list[str] = []
    for character in str(value):
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def build_driver_skill(
    common_il: str | Path,
    request_il: str | Path,
    action_il: str | Path,
    output_json: str | Path,
    *,
    extra_scripts: Sequence[str | Path] = (),
) -> str:
    """Build the small SKILL launcher used for one isolated run.

    The native action scripts are intentionally loaded inside ``errset``.  A
    failed load or an action which returns without writing its response cannot
    leave Python waiting indefinitely because the driver writes a deterministic
    fallback error directly with SKILL's file primitives and exits.
    """

    common = _skill_string(str(common_il))
    request = _skill_string(str(request_il))
    action = _skill_string(str(action_il))
    output = _skill_string(str(output_json))
    extra_loads = "".join(
        f"      load({_skill_string(str(script))})\n" for script in extra_scripts
    )
    return (
        "let((oagitDriverResult)\n"
        "  oagitDriverResult = errset(\n"
        "    progn(\n"
        f"      load({common})\n"
        f"{extra_loads}"
        f"      load({request})\n"
        f"      load({action})\n"
        "    )\n"
        "    t\n"
        "  )\n"
        f"  unless(isFile({output})\n"
        "    let((oagitFallbackPort oagitFallbackText)\n"
        "      oagitFallbackText = if(oagitDriverResult\n"
        "          \"{\\\"ok\\\":false,\\\"error\\\":\\\"SKILL action did not write response.json\\\",\\\"error_code\\\":\\\"driver_error\\\"}\"\n"
        "          \"{\\\"ok\\\":false,\\\"error\\\":\\\"SKILL action load failed\\\",\\\"error_code\\\":\\\"driver_error\\\"}\")\n"
        f"      oagitFallbackPort = outfile({output} \"w\")\n"
        "      when(oagitFallbackPort\n"
        "        fprintf(oagitFallbackPort \"%s\" oagitFallbackText)\n"
        "        close(oagitFallbackPort))\n"
        "    )\n"
        "  )\n"
        ")\n"
        "exit(0)\n"
    )


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""
    if len(data) <= limit:
        return data
    return data[-limit:]


def _result_text(result: subprocess.CompletedProcess[str] | None, field: str) -> str:
    if result is None:
        return ""
    value = getattr(result, field, "")
    if value is None:
        return ""
    return str(value)


def _is_signal_crash(
    result: subprocess.CompletedProcess[str] | None,
    run_dir: Path,
) -> bool:
    """Detect TSMC65/IC26.1 native heap/signal crashes when no valid response exists."""

    if result is not None:
        return_code = getattr(result, "returncode", 0)
        if return_code in _CRASH_RETURN_CODES:
            return True
    chunks = [
        _result_text(result, "stderr"),
        _result_text(result, "stdout"),
        _tail(run_dir / "virtuoso.log", limit=8000),
        _tail(run_dir / "CDS.log", limit=8000),
    ]
    combined = "\n".join(chunks).lower()
    return any(sig in combined for sig in _CRASH_SIGNATURES)


def _is_post_exit_cxt_teardown_crash(
    result: subprocess.CompletedProcess[str] | None,
    run_dir: Path,
    payload: Mapping[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Return True iff Virtuoso reached ``Memory report: on exit`` before a .cxt teardown crash."""

    if result is None or not isinstance(payload, Mapping):
        return False, None
    returncode = getattr(result, "returncode", 0)
    if returncode not in _CRASH_RETURN_CODES:
        return False, None
    if payload.get("ok") is not True:
        return False, None
    vlog_lower = _tail(run_dir / "virtuoso.log", limit=8000).lower()
    if "memory report: on exit" not in vlog_lower:
        return False, None
    stderr_lower = _result_text(result, "stderr").lower()
    matched = next(
        (sig for sig in _CRASH_SIGNATURES if sig in stderr_lower or sig in vlog_lower),
        f"returncode={returncode}",
    )
    return True, {
        "returncode": returncode,
        "signature": matched,
        "reason": "post_exit_cxt_teardown_after_valid_response",
    }


def _normalise_restore_error(payload: Mapping[str, object]) -> dict[str, object]:
    """Accept the structured ``error`` object emitted by restore_schematic.il.

    Older bridge parsers accept a text error, while the native restore script
    includes both ``code`` and ``message`` in its error object.  Preserve the
    wire code and expose its human-readable message to the shared parser.
    """

    if payload.get("ok") is not False or not isinstance(payload.get("error"), Mapping):
        return dict(payload)
    data = dict(payload)
    error = payload["error"]
    if not data.get("error_code") and isinstance(error.get("code"), str):
        data["error_code"] = error["code"]
    message = error.get("message")
    data["error"] = str(message) if message is not None else str(error)
    return data


def _normalise_coupling_rules_map(
    coupling_rules: Mapping[str, Sequence[CdfCouplingRule | Mapping[str, Any]]] | None,
) -> dict[str, tuple[CdfCouplingRule, ...]]:
    if coupling_rules is None:
        return {}
    if not isinstance(coupling_rules, Mapping):
        raise SchemaValidationError("coupling_rules must be a mapping or None")
    out: dict[str, tuple[CdfCouplingRule, ...]] = {}
    for key, rules in coupling_rules.items():
        if not isinstance(key, str) or not key.strip():
            raise SchemaValidationError(f"invalid coupling_rules master key {key!r}")
        parsed: list[CdfCouplingRule] = []
        for item in rules:
            if isinstance(item, CdfCouplingRule):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(CdfCouplingRule.from_dict(dict(item)))
            else:
                raise SchemaValidationError(
                    f"coupling_rules[{key!r}] entries must be CdfCouplingRule or mapping"
                )
        out[key] = tuple(parsed)
    return out


def _batch_symbol_result_to_restore_payload(
    item: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate a batch_restore.il symbol item into parse_symbol_restore_response shape."""

    payload: dict[str, Any] = {
        "ok": item.get("ok"),
        "error": item.get("error"),
        "error_code": item.get("error_code"),
        "target": (
            item["target"].to_dict()
            if isinstance(item.get("target"), CellViewRef)
            else item.get("target")
        ),
        "published": item.get("published", False),
        "draft_cell": item.get("draft_cell"),
        "pin_contract_verified": bool(
            item.get("pin_contract_verified", item.get("cdf_verified", False))
        ),
        "readback_verified": bool(item.get("readback_verified", False)),
        "pin_mismatches": list(
            item.get("pin_mismatches", item.get("connectivity_mismatches") or ())
        ),
    }
    if item.get("symbol_check") is not None:
        payload["symbol_check"] = dict(item["symbol_check"])
    elif item.get("sch_check") is not None:
        payload["symbol_check"] = dict(item["sch_check"])
    return payload


def _batch_schematic_result_to_restore_payload(
    item: Mapping[str, Any],
    *,
    transport: str,
) -> dict[str, Any]:
    """Translate a batch_restore.il schematic item into parse_restore_response shape."""

    payload: dict[str, Any] = {
        "ok": item.get("ok"),
        "error": item.get("error"),
        "error_code": item.get("error_code"),
        "target": (
            item["target"].to_dict()
            if isinstance(item.get("target"), CellViewRef)
            else item.get("target")
        ),
        "published": item.get("published", False),
        "draft_cell": item.get("draft_cell"),
        "sch_check": dict(item.get("sch_check") or {}),
        "cdf_verified": bool(item.get("cdf_verified", False)),
        "readback_verified": bool(item.get("readback_verified", False)),
        "connectivity_mismatches": list(item.get("connectivity_mismatches") or ()),
        "transport": transport,
    }
    for opt in ("derived_status", "derived_refresh", "cdf_passes", "callback_order"):
        if opt in item and item[opt] is not None:
            payload[opt] = item[opt]
    return payload


class VirtuosoSkillBackend:
    """Execute the Phase 2/v0.2 SKILL bridge in isolated one-shot, batch, or daemon mode."""

    def __init__(
        self,
        *,
        virtuoso_cmd: str | None = None,
        ssh_host: str | None = None,
        remote_workdir: str | None = None,
        timeout_s: int = 180,
        replay_flag: str = "-restore",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        skill_dir: Path | None = None,
        keep_artifacts: bool = False,
        crash_retries: int = 1,
        daemon_enabled: bool = False,
        daemon_idle_timeout_s: int = 3600,
        daemon_startup_timeout_s: int = 30,
        daemon_poll_ms: int = 100,
        project_root: Path | None = None,
        inline_readback: bool = False,
        popen_factory: Callable[..., Any] | None = None,
    ) -> None:
        if type(crash_retries) is not int or crash_retries < 0:
            raise SchemaValidationError("crash_retries must be a non-negative int")
        if type(daemon_enabled) is not bool:
            raise SchemaValidationError("daemon_enabled must be a bool")
        if type(daemon_idle_timeout_s) is not int or daemon_idle_timeout_s < 0:
            raise SchemaValidationError("daemon_idle_timeout_s must be an int >= 0")
        if type(daemon_startup_timeout_s) is not int or daemon_startup_timeout_s <= 0:
            raise SchemaValidationError("daemon_startup_timeout_s must be a positive int")
        if type(daemon_poll_ms) is not int or daemon_poll_ms <= 0:
            raise SchemaValidationError("daemon_poll_ms must be a positive int")
        if type(inline_readback) is not bool:
            raise SchemaValidationError("inline_readback must be a bool")

        self.virtuoso_cmd = virtuoso_cmd or "virtuoso"
        self.ssh_host = ssh_host
        self.remote_workdir = remote_workdir or "/tmp/oagit_runs"
        self.timeout_s = timeout_s
        self.replay_flag = replay_flag
        self.runner: Runner = runner or subprocess.run
        self.skill_dir = (
            Path(skill_dir)
            if skill_dir is not None
            else Path(__file__).resolve().parent / "skill"
        )
        self.keep_artifacts = keep_artifacts
        self.crash_retries = crash_retries
        self.daemon_enabled = daemon_enabled
        self.daemon_idle_timeout_s = daemon_idle_timeout_s
        self.daemon_startup_timeout_s = daemon_startup_timeout_s
        self.daemon_poll_ms = daemon_poll_ms
        self.project_root = Path(project_root) if project_root is not None else None
        self.inline_readback = inline_readback
        self.popen_factory = popen_factory

        # Read-only diagnostic hooks for callers and tests.
        self.last_run_id: str | None = None
        self.last_run_dir: Path | None = None
        self.last_transport: str = "one_shot"
        self.last_daemon_error: str | None = None
        self.last_teardown_warning: dict[str, Any] | None = None
        self._daemon_clients: dict[str, VirtuosoDaemonClient] = {}

    def _new_run(self) -> tuple[str, Path]:
        run_id = uuid.uuid4().hex
        run_dir = Path(tempfile.mkdtemp(prefix=f"oagit_{run_id}_"))
        self.last_run_id = run_id
        self.last_run_dir = run_dir
        (run_dir / "tmp").mkdir()
        return run_id, run_dir

    def _skill_source(self, filename: str) -> Path:
        source = self.skill_dir / filename
        if not source.is_file():
            raise BridgeExecutionError(f"missing SKILL bridge file: {source}")
        return source

    def _extra_scripts_for_action(self, action: str) -> tuple[str, ...]:
        helpers = _ACTION_HELPER_SCRIPTS.get(action, ())
        return tuple(name for name in helpers if (self.skill_dir / name).is_file())

    def _prepare_bundle(
        self,
        run_dir: Path,
        *,
        request_text: str,
        action_name: str,
        output_json: Path,
        path_root: Path | None = None,
        extra_scripts: Sequence[str] = (),
    ) -> dict[str, Any]:
        common = run_dir / "oagit_common.il"
        action = run_dir / action_name
        request = run_dir / "request.il"
        driver = run_dir / "driver.il"
        runtime_root = path_root or run_dir
        shutil.copy2(self._skill_source("oagit_common.il"), common)
        staged_extras: list[Path] = []
        runtime_extras: list[Path] = []
        for helper_name in extra_scripts:
            if helper_name in {"oagit_common.il", action_name}:
                continue
            dst = run_dir / helper_name
            shutil.copy2(self._skill_source(helper_name), dst)
            staged_extras.append(dst)
            runtime_extras.append(runtime_root / helper_name)
        shutil.copy2(self._skill_source(action_name), action)
        request.write_text(request_text, encoding="utf-8")
        driver.write_text(
            build_driver_skill(
                runtime_root / common.name,
                runtime_root / request.name,
                runtime_root / action.name,
                runtime_root / output_json.name,
                extra_scripts=runtime_extras,
            ),
            encoding="utf-8",
        )
        return {
            "common": common,
            "action": action,
            "request": request,
            "driver": driver,
            "output": output_json,
            "_upload_files": [common, *staged_extras, action, request, driver],
        }

    def _environment(self, run_dir: Path, *, root: Path | None = None) -> dict[str, str]:
        base = os.environ.copy()
        workdir = root or run_dir
        base.update(
            {
                "ENABLE_SOS705": "0",
                "OAGIT_AUTO_EXIT": "1",
                "IC261_WORKDIR": str(workdir),
                "CDS_LOG_PATH": str(workdir / "CDS.log"),
                "TMPDIR": str(workdir / "tmp"),
                "OAGIT_COMMON_IL": str(workdir / "oagit_common.il"),
                "OAGIT_REQUEST_IL": str(workdir / "request.il"),
            }
        )
        return base

    def _validate_cds_lib(
        self, cds_lib: Path | str | None, operation: str
    ) -> Path | str:
        """Validate the local or remote ``cds.lib`` command-line value.

        Local runs accept a :class:`~pathlib.Path` or non-empty string (coerced
        to :class:`~pathlib.Path`).  SSH runs accept either a Path or a
        non-empty string, and interpret that value on the remote EDA host.  The
        remote value is never checked against the local filesystem.
        """

        if cds_lib is None:
            raise BridgeExecutionError(f"{operation} requires a cds_lib path")
        if self.ssh_host:
            if isinstance(cds_lib, Path):
                remote_path = str(cds_lib)
            elif isinstance(cds_lib, str):
                remote_path = cds_lib
            else:
                raise BridgeExecutionError(
                    "SSH cds_lib must be a non-empty remote path string or pathlib.Path"
                )
            if not remote_path.strip() or "\x00" in remote_path:
                raise BridgeExecutionError(
                    "SSH cds_lib must be a non-empty remote path string"
                )
            return remote_path
        if isinstance(cds_lib, str):
            if not cds_lib.strip() or "\x00" in cds_lib:
                raise BridgeExecutionError("local cds_lib must be a non-empty path")
            return Path(cds_lib)
        if not isinstance(cds_lib, Path):
            raise BridgeExecutionError("local cds_lib must be a pathlib.Path")
        return cds_lib

    def _local_command(self, cds_lib: Path | str, run_dir: Path) -> list[str]:
        return [
            self.virtuoso_cmd,
            "-nograph",
            "-nocdsinit",
            "-cdslib",
            str(cds_lib),
            "-log",
            str(run_dir / "virtuoso.log"),
            self.replay_flag,
            str(run_dir / "driver.il"),
        ]

    def _invoke(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                command,
                cwd=str(cwd),
                env=dict(env),
                timeout=self.timeout_s,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            timeout_detail = ""
            timeout_stderr = getattr(exc, "stderr", None)
            timeout_output = getattr(exc, "output", None)
            if timeout_stderr:
                timeout_detail += f"\nstderr tail:\n{str(timeout_stderr)[-4000:]}"
            if timeout_output:
                timeout_detail += f"\nstdout tail:\n{str(timeout_output)[-2000:]}"
            raise BridgeExecutionError(
                f"Virtuoso command timed out after {self.timeout_s}s: "
                f"{shlex.join(command)}{timeout_detail}{self._diagnostics(None, cwd)}"
            ) from exc
        except OSError as exc:
            raise BridgeExecutionError(
                f"unable to execute {shlex.join(command)}: {exc}"
                f"{self._diagnostics(None, cwd)}"
            ) from exc
        except Exception as exc:
            if isinstance(exc, BridgeExecutionError):
                raise
            raise BridgeExecutionError(
                f"Virtuoso runner failed for {shlex.join(command)}: {exc}"
                f"{self._diagnostics(None, cwd)}"
            ) from exc
        return result

    def _diagnostics(
        self,
        result: subprocess.CompletedProcess[str] | None,
        run_dir: Path,
    ) -> str:
        parts: list[str] = []
        stderr = _result_text(result, "stderr").strip()
        stdout = _result_text(result, "stdout").strip()
        if stderr:
            parts.append(f"stderr tail:\n{stderr[-4000:]}")
        if stdout:
            parts.append(f"stdout tail:\n{stdout[-2000:]}")
        for filename in ("virtuoso.log", "CDS.log"):
            text = _tail(run_dir / filename).strip()
            if text:
                parts.append(f"{filename} tail:\n{text}")
        if not parts:
            return ""
        return "\n" + "\n".join(parts)

    def _read_response(
        self,
        run_dir: Path,
        result: subprocess.CompletedProcess[str] | None,
        *,
        command: list[str],
    ) -> dict[str, object]:
        response = run_dir / "response.json"
        if not response.is_file():
            return_code = getattr(result, "returncode", None)
            suffix = f" (returncode={return_code})" if return_code is not None else ""
            message = (
                f"Virtuoso run did not produce response.json{suffix}"
                f" for {shlex.join(command)}{self._diagnostics(result, run_dir)}"
            )
            if _is_signal_crash(result, run_dir):
                raise _VirtuosoSignalCrashError(message)
            raise BridgeExecutionError(message)
        try:
            payload = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = (
                f"invalid response.json from Virtuoso: {exc}"
                f"{self._diagnostics(result, run_dir)}"
            )
            if _is_signal_crash(result, run_dir):
                raise _VirtuosoSignalCrashError(message) from exc
            raise BridgeExecutionError(message) from exc
        if not isinstance(payload, dict):
            raise BridgeExecutionError(
                "invalid response.json from Virtuoso: top level must be an object"
                f"{self._diagnostics(result, run_dir)}"
            )
        return payload

    def _check_result_code(
        self,
        result: subprocess.CompletedProcess[str] | None,
        run_dir: Path,
        command: list[str],
    ) -> None:
        if result is None:
            return
        return_code = getattr(result, "returncode", 0)
        if return_code:
            raise BridgeExecutionError(
                f"Virtuoso command exited with status {return_code}: "
                f"{shlex.join(command)}{self._diagnostics(result, run_dir)}"
            )

    def _run_local(
        self,
        run_dir: Path,
        cds_lib: Path | str,
    ) -> dict[str, object]:
        env = self._environment(run_dir)
        command = self._local_command(cds_lib, run_dir)
        result = self._invoke(command, cwd=run_dir, env=env)
        payload = self._read_response(run_dir, result, command=command)
        is_teardown, info = _is_post_exit_cxt_teardown_crash(result, run_dir, payload)
        if is_teardown:
            self.last_teardown_warning = info
            return payload
        self._check_result_code(result, run_dir, command)
        return payload

    def _remote_path(self, run_id: str) -> str:
        return f"{self.remote_workdir.rstrip('/')}/{run_id}"

    def _remote_shell(self, argv: Iterable[str], run_dir: str) -> str:
        env_values = {
            "ENABLE_SOS705": "0",
            "OAGIT_AUTO_EXIT": "1",
            "IC261_WORKDIR": run_dir,
            "CDS_LOG_PATH": f"{run_dir}/CDS.log",
            "TMPDIR": f"{run_dir}/tmp",
            "OAGIT_COMMON_IL": f"{run_dir}/oagit_common.il",
            "OAGIT_REQUEST_IL": f"{run_dir}/request.il",
        }
        assignments = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in env_values.items()
        )
        return (
            f"cd {shlex.quote(run_dir)} && mkdir -p {shlex.quote(run_dir + '/tmp')} && "
            f"env {assignments} {shlex.join(list(argv))}"
        )

    def _ssh_command(self, shell_command: str) -> list[str]:
        if not self.ssh_host:
            raise BridgeExecutionError("SSH host is not configured")
        return ["/usr/bin/ssh", self.ssh_host, shell_command]

    def _scp_spec(self, path: str) -> str:
        """Quote a remote path while preserving the configured SSH alias."""

        if not self.ssh_host:
            raise BridgeExecutionError("SSH host is not configured")
        return f"{self.ssh_host}:{shlex.quote(path)}"

    def _scp_upload(
        self,
        source: Path,
        destination: str,
        *,
        run_dir: Path,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return self._invoke(
            ["/usr/bin/scp", str(source), self._scp_spec(destination)],
            cwd=run_dir,
            env=env,
        )

    def _scp_download(
        self,
        source: str,
        destination: Path,
        *,
        run_dir: Path,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return self._invoke(
            ["/usr/bin/scp", self._scp_spec(source), str(destination)],
            cwd=run_dir,
            env=env,
        )

    def _best_effort_remote_download(
        self,
        remote_source: str,
        local_destination: Path,
        *,
        run_dir: Path,
        env: Mapping[str, str],
    ) -> None:
        try:
            result = self._scp_download(
                remote_source,
                local_destination,
                run_dir=run_dir,
                env=env,
            )
            if getattr(result, "returncode", 0):
                return
        except BridgeExecutionError:
            return

    def _run_remote(
        self,
        run_id: str,
        run_dir: Path,
        cds_lib: Path | str,
        bundle: Mapping[str, Any],
    ) -> dict[str, object]:
        if not self.ssh_host:
            raise BridgeExecutionError("SSH host is not configured")
        remote_dir = self._remote_path(run_id)
        local_env = self._environment(run_dir, root=run_dir)
        remote_command = self._remote_shell(
            self._local_command(cds_lib, Path(remote_dir)), remote_dir
        )
        ssh_mkdir = self._ssh_command(
            f"mkdir -p {shlex.quote(remote_dir)} {shlex.quote(remote_dir + '/tmp')}"
        )
        response_result: subprocess.CompletedProcess[str] | None = None
        command_result: subprocess.CompletedProcess[str] | None = None
        upload_files: Sequence[Path] = bundle.get(
            "_upload_files",
            (bundle["common"], bundle["action"], bundle["request"], bundle["driver"]),
        )
        try:
            mkdir_result = self._invoke(ssh_mkdir, cwd=run_dir, env=local_env)
            self._check_result_code(mkdir_result, run_dir, ssh_mkdir)
            for source in upload_files:
                upload_result = self._scp_upload(
                    source,
                    f"{remote_dir}/{source.name}",
                    run_dir=run_dir,
                    env=local_env,
                )
                upload_command = [
                    "/usr/bin/scp",
                    str(source),
                    self._scp_spec(f"{remote_dir}/{source.name}"),
                ]
                self._check_result_code(upload_result, run_dir, upload_command)
            try:
                command_result = self._invoke(
                    self._ssh_command(remote_command),
                    cwd=run_dir,
                    env=local_env,
                )
            except BridgeExecutionError as exc:
                self._best_effort_remote_download(
                    f"{remote_dir}/response.json",
                    run_dir / "response.json",
                    run_dir=run_dir,
                    env=local_env,
                )
                self._best_effort_remote_download(
                    f"{remote_dir}/virtuoso.log",
                    run_dir / "virtuoso.log",
                    run_dir=run_dir,
                    env=local_env,
                )
                diagnostics = self._diagnostics(None, run_dir)
                raise BridgeExecutionError(f"{exc}{diagnostics}") from exc
            try:
                response_result = self._scp_download(
                    f"{remote_dir}/response.json",
                    run_dir / "response.json",
                    run_dir=run_dir,
                    env=local_env,
                )
            except BridgeExecutionError:
                # The missing response path is diagnosed below with the SSH
                # command's stderr and any downloaded Virtuoso log.
                response_result = None

            response_exists = (run_dir / "response.json").is_file()
            command_failed = bool(getattr(command_result, "returncode", 0))
            if command_failed or not response_exists:
                self._best_effort_remote_download(
                    f"{remote_dir}/virtuoso.log",
                    run_dir / "virtuoso.log",
                    run_dir=run_dir,
                    env=local_env,
                )
            try:
                payload = self._read_response(
                    run_dir,
                    command_result if command_result is not None else response_result,
                    command=self._ssh_command(remote_command),
                )
            except _VirtuosoSignalCrashError as exc:
                self._best_effort_remote_download(
                    f"{remote_dir}/virtuoso.log",
                    run_dir / "virtuoso.log",
                    run_dir=run_dir,
                    env=local_env,
                )
                diagnostics = self._diagnostics(
                    command_result if command_result is not None else response_result,
                    run_dir,
                )
                raise _VirtuosoSignalCrashError(f"{exc}{diagnostics}") from exc
            except BridgeExecutionError as exc:
                self._best_effort_remote_download(
                    f"{remote_dir}/virtuoso.log",
                    run_dir / "virtuoso.log",
                    run_dir=run_dir,
                    env=local_env,
                )
                diagnostics = self._diagnostics(
                    command_result if command_result is not None else response_result,
                    run_dir,
                )
                if _is_signal_crash(
                    command_result if command_result is not None else response_result,
                    run_dir,
                ):
                    raise _VirtuosoSignalCrashError(f"{exc}{diagnostics}") from exc
                raise BridgeExecutionError(f"{exc}{diagnostics}") from exc
            if payload.get("ok") is False and not command_failed:
                self._best_effort_remote_download(
                    f"{remote_dir}/virtuoso.log",
                    run_dir / "virtuoso.log",
                    run_dir=run_dir,
                    env=local_env,
                )
            if command_failed and payload.get("ok") is not False:
                self._best_effort_remote_download(
                    f"{remote_dir}/virtuoso.log",
                    run_dir / "virtuoso.log",
                    run_dir=run_dir,
                    env=local_env,
                )
                is_teardown, info = _is_post_exit_cxt_teardown_crash(
                    command_result, run_dir, payload
                )
                if is_teardown:
                    self.last_teardown_warning = info
                    return payload
                self._check_result_code(
                    command_result,
                    run_dir,
                    self._ssh_command(remote_command),
                )
            return payload
        finally:
            if not self.keep_artifacts:
                cleanup = self._ssh_command(f"rm -rf -- {shlex.quote(remote_dir)}")
                try:
                    self._invoke(cleanup, cwd=run_dir, env=local_env)
                except BridgeExecutionError:
                    # Preserve the actual run failure.  Cleanup is best effort
                    # because a disconnected SSH session must not hide it.
                    pass

    def _execute_one_shot(
        self,
        cds_lib: Path | str,
        *,
        action_name: str,
        extra_scripts: Sequence[str],
        request_builder: Callable[[Path, Path], str],
    ) -> dict[str, object]:
        """Run one isolated SKILL invocation with bounded automatic crash retry.

        Retries ONLY when Virtuoso terminates with a signal/heap crash
        (exit 139/-11/134/-6 or ``corrupted double-linked list`` /
        ``Segmentation fault``) AND no valid ``response.json`` was written.
        Valid JSON business errors are never retried.
        """

        self.last_teardown_warning = None
        max_attempts = 1 + self.crash_retries
        for attempt in range(max_attempts):
            run_id, run_dir = self._new_run()
            output_json = run_dir / "response.json"
            try:
                runtime_root = (
                    Path(self._remote_path(run_id)) if self.ssh_host else run_dir
                )
                request_text = request_builder(runtime_root, output_json)
                bundle = self._prepare_bundle(
                    run_dir,
                    request_text=request_text,
                    action_name=action_name,
                    output_json=output_json,
                    path_root=runtime_root,
                    extra_scripts=extra_scripts,
                )
                if self.ssh_host:
                    payload = self._run_remote(run_id, run_dir, cds_lib, bundle)
                else:
                    payload = self._run_local(run_dir, cds_lib)
                return payload
            except _VirtuosoSignalCrashError as exc:
                if attempt + 1 < max_attempts:
                    continue
                raise BridgeExecutionError(
                    f"Virtuoso native crash after {attempt + 1} attempt(s): {exc}"
                ) from exc
            finally:
                if not self.keep_artifacts:
                    shutil.rmtree(run_dir, ignore_errors=True)
        raise AssertionError("unreachable")  # pragma: no cover

    def _should_use_daemon(self, session: OaSession | None) -> bool:
        if not self.daemon_enabled:
            return False
        if session is not None and session.transport == "one_shot":
            return False
        return True

    def get_daemon_client(self, cds_lib: Path | str) -> VirtuosoDaemonClient:
        key = str(cds_lib)
        client = self._daemon_clients.get(key)
        if client is None:
            root = self.project_root or Path.cwd()
            client = VirtuosoDaemonClient(
                project_root=root,
                cds_lib=cds_lib,
                virtuoso_cmd=self.virtuoso_cmd,
                ssh_host=self.ssh_host,
                remote_workdir=self.remote_workdir,
                skill_dir=self.skill_dir,
                idle_timeout_s=self.daemon_idle_timeout_s,
                startup_timeout_s=self.daemon_startup_timeout_s,
                poll_ms=self.daemon_poll_ms,
                timeout_s=self.timeout_s,
                replay_flag=self.replay_flag,
                runner=self.runner,
                popen_factory=self.popen_factory,
                keep_artifacts=self.keep_artifacts,
            )
            self._daemon_clients[key] = client
        return client

    def _execute_via_daemon(
        self,
        action: str,
        cds_lib: Path | str,
        *,
        request_id: str,
        daemon_request_builder: Callable[[Path], str],
        one_shot_fn: Callable[[], Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], str]:
        client = self.get_daemon_client(cds_lib)
        resp_path = client.runtime_spool_dir / f"{request_id}.resp.json"
        req_il_text = daemon_request_builder(resp_path)
        payload, transport = client.execute_with_fallback(
            action,
            req_il_text,
            request_id=request_id,
            one_shot_fn=one_shot_fn,
            timeout_s=self.timeout_s,
        )
        self.last_transport = transport
        self.last_daemon_error = client.last_daemon_error
        return payload, transport

    def capture_schematic(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> SchematicSnapshot:
        validated_cds_lib = self._validate_cds_lib(cds_lib, "capture_schematic")
        if not isinstance(cellview, CellViewRef):
            raise BridgeExecutionError("cellview must be a CellViewRef")

        if self._should_use_daemon(session):
            req_id = f"cap-sch-{uuid.uuid4().hex[:12]}"

            def _daemon_req(resp_path: Path) -> str:
                return (
                    "setq(oagitRequest '"
                    + to_skill_literal(
                        {
                            "protocol": SPOOL_PROTOCOL_V1,
                            "request_id": req_id,
                            "action": "capture_schematic",
                            "library": cellview.library,
                            "cell": cellview.cell,
                            "view": cellview.view,
                            "output_path": str(resp_path),
                        }
                    )
                    + ")\n"
                )

            def _one_shot() -> Mapping[str, Any]:
                return self._execute_one_shot(
                    validated_cds_lib,
                    action_name="export_schematic.il",
                    extra_scripts=(),
                    request_builder=lambda rt, out: render_capture_request_skill(
                        cellview, rt / out.name
                    ),
                )

            raw_payload, _transport = self._execute_via_daemon(
                "capture_schematic",
                validated_cds_lib,
                request_id=req_id,
                daemon_request_builder=_daemon_req,
                one_shot_fn=_one_shot,
            )
            payload = self._unwrap_daemon_capture(raw_payload)
        else:
            self.last_transport = "one_shot"
            self.last_daemon_error = None
            payload = self._execute_one_shot(
                validated_cds_lib,
                action_name="export_schematic.il",
                extra_scripts=(),
                request_builder=lambda rt, out: render_capture_request_skill(
                    cellview, rt / out.name
                ),
            )
        try:
            return raw_capture_to_snapshot(payload)
        except FailClosedError:
            raise
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid capture response: {exc}") from exc

    def _unwrap_daemon_capture(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if payload.get("protocol") == SPOOL_PROTOCOL_V1:
            if payload.get("ok") is False:
                return {
                    "ok": False,
                    "error": payload.get("error") or "daemon capture failed",
                }
            if isinstance(payload.get("capture"), Mapping):
                return payload["capture"]
        return payload

    def capture_symbol(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> SymbolSnapshot:
        validated_cds_lib = self._validate_cds_lib(cds_lib, "capture_symbol")
        if not isinstance(cellview, CellViewRef):
            raise BridgeExecutionError("cellview must be a CellViewRef")
        if cellview.view != "symbol":
            raise SchemaValidationError("cellview.view must be 'symbol'")
        payload = self.run_skill_request(
            "capture_symbol",
            {
                "library": cellview.library,
                "cell": cellview.cell,
                "view": cellview.view,
            },
            cds_lib=validated_cds_lib,
            session=session,
        )
        unwrapped = self._unwrap_daemon_capture(payload)
        try:
            return raw_symbol_capture_to_snapshot(unwrapped)
        except FailClosedError:
            raise
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid symbol capture response: {exc}") from exc

    def capture_batch(
        self,
        cellviews: Sequence[CellViewRef],
        *,
        cds_lib: Path | str | None = None,
        hier: bool = False,
        max_depth: int = DEFAULT_MAX_HIER_DEPTH,
        recompute_derived: bool = False,
        include_symbols: bool = False,
        skip_libraries: Sequence[str] = DEFAULT_SKIP_LIBRARIES,
        session: OaSession | None = None,
    ) -> BatchCaptureResult:
        validated_cds_lib = self._validate_cds_lib(cds_lib, "capture_batch")
        if isinstance(cellviews, (str, bytes)) or not isinstance(cellviews, Sequence) or not cellviews:
            raise SchemaValidationError("cellviews must be a non-empty sequence of CellViewRef")
        for idx, cv in enumerate(cellviews):
            if not isinstance(cv, CellViewRef):
                raise SchemaValidationError(f"cellviews[{idx}] must be a CellViewRef")
        if type(hier) is not bool:
            raise SchemaValidationError("hier must be a bool")
        if type(max_depth) is not int or max_depth < 0:
            raise SchemaValidationError("max_depth must be a non-negative int")
        if type(recompute_derived) is not bool:
            raise SchemaValidationError("recompute_derived must be a bool")
        if type(include_symbols) is not bool:
            raise SchemaValidationError("include_symbols must be a bool")

        req_id = f"batch-cap-{uuid.uuid4().hex[:12]}"
        request_payload: dict[str, Any] = {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": req_id,
            "cellviews": [cv.to_dict() for cv in cellviews],
            "options": {
                "hier": hier,
                "max_depth": max_depth,
                "recompute_derived": recompute_derived,
                "include_symbols": include_symbols,
                "skip_libraries": [str(lib) for lib in skip_libraries],
            },
        }
        raw_response = self.run_skill_request(
            "capture_batch",
            request_payload,
            cds_lib=validated_cds_lib,
            session=session,
        )
        try:
            parsed = parse_batch_capture_response(raw_response, expected_request_id=req_id)
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid batch capture response: {exc}") from exc

        for err in parsed["errors"]:
            if isinstance(err, Mapping) and err.get("code") == "hierarchy_depth_exceeded":
                raise BridgeExecutionError(
                    f"hierarchy_depth_exceeded: {err.get('message') or err}"
                )

        if not parsed["ok"] and not parsed["results"]:
            err_code = parsed.get("error_code")
            err_msg = parsed.get("error")
            if not err_msg and parsed["errors"]:
                first_err = parsed["errors"][0]
                if isinstance(first_err, Mapping):
                    err_code = err_code or first_err.get("code")
                    err_msg = first_err.get("message") or str(first_err)
                else:
                    err_msg = str(first_err)
            detail = (
                f"{err_code}: {err_msg}"
                if err_code and err_msg
                else str(err_msg or err_code or "batch capture failed")
            )
            raise BridgeExecutionError(detail)

        items: list[BatchCaptureItem] = []
        for entry in parsed["results"]:
            cv: CellViewRef = entry["cellview"]
            if entry["ok"]:
                capture_map = entry["capture"]
                try:
                    view_type = infer_view_type(cv.view)
                    if view_type is None and isinstance(capture_map.get("metadata"), Mapping):
                        cv_type = capture_map["metadata"].get("cellview_type")
                        if cv_type == "maskLayout":
                            view_type = "layout"
                        elif cv_type == "schematic":
                            view_type = "schematic"
                        elif cv_type == "schematicSymbol":
                            view_type = "symbol"
                    if view_type == "schematic":
                        snap: Any = raw_capture_to_snapshot(capture_map)
                    elif view_type == "symbol":
                        snap = raw_symbol_capture_to_snapshot(capture_map)
                    elif view_type == "layout":
                        from .layout_backend import raw_layout_capture_to_snapshot

                        snap = raw_layout_capture_to_snapshot(capture_map)
                    else:
                        raise BridgeExecutionError(f"unsupported captured view: {cv.view!r}")
                except (FailClosedError, SchemaValidationError) as exc:
                    items.append(
                        BatchCaptureItem(
                            cellview=cv,
                            snapshot=None,
                            skipped=False,
                            reason="parse_failed",
                            error=str(exc),
                        )
                    )
                else:
                    items.append(
                        BatchCaptureItem(
                            cellview=cv,
                            snapshot=snap,
                            skipped=False,
                        )
                    )
            else:
                items.append(
                    BatchCaptureItem(
                        cellview=cv,
                        snapshot=None,
                        skipped=False,
                        reason=entry.get("error_code"),
                        error=entry.get("error") or "batch item capture failed",
                    )
                )

        session_info = dict(parsed["session"])
        session_info.setdefault("transport", self.last_transport)
        if self.last_daemon_error and "daemon_error" not in session_info:
            session_info["daemon_error"] = self.last_daemon_error
        if parsed["errors"]:
            session_info["errors"] = list(parsed["errors"])

        return BatchCaptureResult(
            items=tuple(items),
            discovered=tuple(parsed["discovered"]),
            cycles=tuple(parsed["cycles"]),
            session=session_info,
        )

    def _build_inline_schematic_restore_item(
        self,
        snapshot: SchematicSnapshot,
        target: CellViewRef,
        *,
        draft_cell: str,
        overwrite: bool,
        allow_libraries: tuple[str, ...],
        recompute_derived: bool,
        max_passes: int,
        coupling_rules: Mapping[str, tuple[CdfCouplingRule, ...]],
    ) -> dict[str, Any]:
        return schematic_snapshot_to_restore_item(
            snapshot,
            target,
            draft_cell=draft_cell,
            overwrite=overwrite,
            allow_libraries=allow_libraries,
            recompute_derived=recompute_derived,
            max_passes=max_passes,
            coupling_rules=coupling_rules,
        )

    def restore_schematic(
        self,
        snapshot: SchematicSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        check_placement: bool = True,
        recompute_derived: bool = False,
        max_passes: int = 3,
        coupling_rules: Mapping[str, Sequence[CdfCouplingRule]] | None = None,
        inline_readback: bool | None = None,
        session: OaSession | None = None,
    ) -> RestoreReceipt:
        validated_cds_lib = self._validate_cds_lib(cds_lib, "restore_schematic")
        if not isinstance(snapshot, SchematicSnapshot):
            raise BridgeExecutionError("snapshot must be a SchematicSnapshot")
        if not isinstance(target, CellViewRef):
            raise BridgeExecutionError("target must be a CellViewRef")
        if type(overwrite) is not bool:
            raise SchemaValidationError("overwrite must be a bool")
        if type(allow_omissions) is not bool:
            raise SchemaValidationError("allow_omissions must be a bool")
        if type(verify_readback) is not bool:
            raise SchemaValidationError("verify_readback must be a bool")
        if type(check_placement) is not bool:
            raise SchemaValidationError("check_placement must be a bool")
        if type(recompute_derived) is not bool:
            raise SchemaValidationError("recompute_derived must be a bool")
        if type(max_passes) is not int or max_passes < 1:
            raise SchemaValidationError("max_passes must be a positive int")
        if inline_readback is not None and type(inline_readback) is not bool:
            raise SchemaValidationError("inline_readback must be a bool or None")
        if oa_view_dir is not None and not isinstance(oa_view_dir, Path):
            raise SchemaValidationError("oa_view_dir must be a pathlib.Path or None")

        try:
            libraries = tuple(allow_libraries)
        except TypeError:
            libraries = ()
        # This order is part of the fail-closed contract.  No temporary native
        # run is created until all Python-side guards have passed.
        ensure_library_allowed(target.library, libraries)
        ensure_no_omissions(snapshot, allow_omissions=allow_omissions)
        if oa_view_dir is not None:
            ensure_no_live_locks(oa_view_dir)

        rules_map = _normalise_coupling_rules_map(coupling_rules)
        draft_cell = f"{target.cell}__oagit_draft_{uuid.uuid4().hex[:10]}"
        effective_inline = (
            self.inline_readback if inline_readback is None else inline_readback
        )

        # Single-session inline readback path (batch_restore.il: 1 Virtuoso launch).
        if effective_inline and verify_readback:
            item = self._build_inline_schematic_restore_item(
                snapshot,
                target,
                draft_cell=draft_cell,
                overwrite=overwrite,
                allow_libraries=libraries,
                recompute_derived=recompute_derived,
                max_passes=max_passes,
                coupling_rules=rules_map,
            )
            req_id = f"batch-res-{uuid.uuid4().hex[:12]}"
            raw_batch = self.run_skill_request(
                "restore_batch",
                {
                    "protocol": BATCH_PROTOCOL_V1,
                    "request_id": req_id,
                    "items": [item],
                    "options": {"readback": True},
                },
                cds_lib=validated_cds_lib,
                session=session,
            )
            try:
                parsed_batch = parse_batch_restore_response(
                    raw_batch, expected_request_id=req_id
                )
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid batch restore response: {exc}") from exc
            if not parsed_batch["ok"] and not parsed_batch["results"]:
                err_code = parsed_batch.get("error_code")
                err_msg = parsed_batch.get("error")
                if not err_msg and parsed_batch["errors"]:
                    first_err = parsed_batch["errors"][0]
                    if isinstance(first_err, Mapping):
                        err_code = err_code or first_err.get("code")
                        err_msg = first_err.get("message") or str(first_err)
                    else:
                        err_msg = str(first_err)
                parse_restore_response(
                    {"ok": False, "error_code": err_code, "error": err_msg},
                    target,
                )
            if len(parsed_batch["results"]) != 1:
                raise BridgeExecutionError(
                    f"expected 1 restore result, got {len(parsed_batch['results'])}"
                )
            item_result = parsed_batch["results"][0]
            transport = str(
                parsed_batch["session"].get("transport") or self.last_transport
            )
            restore_payload = _batch_schematic_result_to_restore_payload(
                item_result, transport=transport
            )
            try:
                receipt = parse_restore_response(
                    _normalise_restore_error(restore_payload), target
                )
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid restore response: {exc}") from exc

            readback_raw = item_result.get("readback")
            if not isinstance(readback_raw, Mapping):
                raise BridgeExecutionError(
                    "inline readback payload missing from single-session restore response"
                )
            try:
                actual = raw_capture_to_snapshot(readback_raw)
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid inline readback response: {exc}") from exc
            verify_roundtrip(
                snapshot,
                actual,
                check_placement=check_placement,
                raise_on_mismatch=True,
            )
            if recompute_derived and receipt.derived_refresh is None:
                refreshed_map = {
                    inst.name: dict(inst.effective_derived.values)
                    for inst in actual.electrical.instances
                }
                return replace(
                    receipt,
                    readback_verified=True,
                    derived_status="valid",
                    derived_refresh=refreshed_map,
                    transport=transport,
                )
            return replace(receipt, readback_verified=True, transport=transport)

        # Standard restore path (daemon or one-shot).
        if self._should_use_daemon(session):
            item = self._build_inline_schematic_restore_item(
                snapshot,
                target,
                draft_cell=draft_cell,
                overwrite=overwrite,
                allow_libraries=libraries,
                recompute_derived=recompute_derived,
                max_passes=max_passes,
                coupling_rules=rules_map,
            )
            req_id = f"res-sch-{uuid.uuid4().hex[:12]}"

            def _daemon_req(resp_path: Path) -> str:
                req_dict = dict(item)
                req_dict["protocol"] = SPOOL_PROTOCOL_V1
                req_dict["request_id"] = req_id
                req_dict["action"] = "restore_schematic"
                req_dict["readback"] = verify_readback
                req_dict["output_path"] = str(resp_path)
                return "setq(oagitRequest '" + to_skill_literal(req_dict) + ")\n"

            def _one_shot() -> Mapping[str, Any]:
                return self._execute_one_shot(
                    validated_cds_lib,
                    action_name="restore_schematic.il",
                    extra_scripts=(),
                    request_builder=lambda rt, out: render_restore_request_skill(
                        snapshot,
                        target,
                        draft_cell=draft_cell,
                        overwrite=overwrite,
                        allow_libraries=libraries,
                        output_path=rt / out.name,
                        max_passes=max_passes,
                        recompute_derived=recompute_derived,
                        coupling_rules=rules_map,
                    ),
                )

            raw_resp, transport = self._execute_via_daemon(
                "restore_schematic",
                validated_cds_lib,
                request_id=req_id,
                daemon_request_builder=_daemon_req,
                one_shot_fn=_one_shot,
            )
            daemon_error = self.last_daemon_error
            inline_rb: Mapping[str, Any] | None = None
            if raw_resp.get("protocol") == SPOOL_PROTOCOL_V1:
                if raw_resp.get("ok") is False and not isinstance(raw_resp.get("result"), Mapping):
                    payload: dict[str, Any] = {
                        "ok": False,
                        "error": raw_resp.get("error"),
                        "error_code": raw_resp.get("error_code"),
                    }
                else:
                    res_obj = raw_resp.get("result")
                    if not isinstance(res_obj, Mapping):
                        raise BridgeExecutionError("daemon restore response missing 'result'")
                    inline_rb = (
                        res_obj.get("readback")
                        if isinstance(res_obj.get("readback"), Mapping)
                        else None
                    )
                    payload = _batch_schematic_result_to_restore_payload(
                        res_obj, transport=transport
                    )
                    for opt in ("derived_status", "derived_refresh", "cdf_passes", "callback_order"):
                        if opt not in payload and opt in raw_resp and raw_resp[opt] is not None:
                            payload[opt] = raw_resp[opt]
            else:
                payload = dict(raw_resp)
                payload["transport"] = transport

            try:
                receipt = parse_restore_response(_normalise_restore_error(payload), target)
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid restore response: {exc}") from exc

            details = dict(receipt.details)
            if daemon_error is not None:
                details["daemon_error"] = daemon_error

            if not verify_readback:
                return replace(receipt, details=details, transport=transport)

            if inline_rb is not None:
                try:
                    actual = raw_capture_to_snapshot(inline_rb)
                except FailClosedError:
                    raise
                except SchemaValidationError as exc:
                    raise BridgeExecutionError(f"invalid readback response: {exc}") from exc
            else:
                actual = self.capture_schematic(
                    target,
                    cds_lib=validated_cds_lib,
                    session=OaSession(
                        session_id=req_id,
                        transport="one_shot" if transport == "one_shot_fallback" else "daemon",
                    ),
                )
                self.last_transport = transport
                self.last_daemon_error = daemon_error
            verify_roundtrip(
                snapshot,
                actual,
                check_placement=check_placement,
                raise_on_mismatch=True,
            )
            if recompute_derived and receipt.derived_refresh is None:
                refreshed_map = {
                    inst.name: dict(inst.effective_derived.values)
                    for inst in actual.electrical.instances
                }
                return replace(
                    receipt,
                    details=details,
                    readback_verified=True,
                    derived_status="valid",
                    derived_refresh=refreshed_map,
                    transport=transport,
                )
            return replace(
                receipt,
                details=details,
                readback_verified=True,
                transport=transport,
            )

        # Classic v1 one-shot 2-step path (default when daemon_enabled=False, inline_readback=False).
        self.last_transport = "one_shot"
        self.last_daemon_error = None
        payload = self._execute_one_shot(
            validated_cds_lib,
            action_name="restore_schematic.il",
            extra_scripts=(),
            request_builder=lambda rt, out: render_restore_request_skill(
                snapshot,
                target,
                draft_cell=draft_cell,
                overwrite=overwrite,
                allow_libraries=libraries,
                output_path=rt / out.name,
                max_passes=max_passes,
                recompute_derived=recompute_derived,
                coupling_rules=rules_map,
            ),
        )
        try:
            receipt = parse_restore_response(_normalise_restore_error(payload), target)
        except FailClosedError:
            raise
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid restore response: {exc}") from exc

        if not verify_readback:
            return receipt
        actual = self.capture_schematic(target, cds_lib=validated_cds_lib, session=session)
        verify_roundtrip(
            snapshot,
            actual,
            check_placement=check_placement,
            raise_on_mismatch=True,
        )
        return replace(receipt, readback_verified=True)

    def restore_symbol(
        self,
        snapshot: SymbolSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        inline_readback: bool | None = None,
        session: OaSession | None = None,
    ) -> RestoreReceipt:
        validated_cds_lib = self._validate_cds_lib(cds_lib, "restore_symbol")
        if not isinstance(snapshot, SymbolSnapshot):
            raise BridgeExecutionError("snapshot must be a SymbolSnapshot")
        if not isinstance(target, CellViewRef):
            raise BridgeExecutionError("target must be a CellViewRef")
        if target.view != "symbol":
            raise SchemaValidationError("target.view must be 'symbol'")
        if type(overwrite) is not bool:
            raise SchemaValidationError("overwrite must be a bool")
        if type(allow_omissions) is not bool:
            raise SchemaValidationError("allow_omissions must be a bool")
        if type(verify_readback) is not bool:
            raise SchemaValidationError("verify_readback must be a bool")
        if inline_readback is not None and type(inline_readback) is not bool:
            raise SchemaValidationError("inline_readback must be a bool or None")
        if oa_view_dir is not None and not isinstance(oa_view_dir, Path):
            raise SchemaValidationError("oa_view_dir must be a pathlib.Path or None")

        try:
            libraries = tuple(allow_libraries)
        except TypeError:
            libraries = ()

        ensure_library_allowed(target.library, libraries)
        ensure_no_omissions(snapshot, allow_omissions=allow_omissions)  # type: ignore[arg-type]
        if oa_view_dir is not None:
            ensure_no_live_locks(oa_view_dir)

        draft_cell = f"{target.cell}__oagit_draft_{uuid.uuid4().hex[:10]}"
        req_dict = build_symbol_restore_request_dict(
            snapshot,
            target,
            draft_cell=draft_cell,
            overwrite=overwrite,
            allow_libraries=libraries,
        )
        effective_inline = (
            self.inline_readback if inline_readback is None else inline_readback
        )

        if effective_inline and verify_readback:
            req_id = f"batch-symres-{uuid.uuid4().hex[:12]}"
            raw_batch = self.run_skill_request(
                "restore_batch",
                {
                    "protocol": BATCH_PROTOCOL_V1,
                    "request_id": req_id,
                    "items": [req_dict],
                    "options": {"readback": True},
                },
                cds_lib=validated_cds_lib,
                session=session,
            )
            try:
                parsed_batch = parse_batch_restore_response(
                    raw_batch, expected_request_id=req_id
                )
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid batch symbol restore response: {exc}") from exc
            if not parsed_batch["ok"] and not parsed_batch["results"]:
                err_code = parsed_batch.get("error_code")
                err_msg = parsed_batch.get("error")
                if not err_msg and parsed_batch["errors"]:
                    first_err = parsed_batch["errors"][0]
                    if isinstance(first_err, Mapping):
                        err_code = err_code or first_err.get("code")
                        err_msg = first_err.get("message") or str(first_err)
                    else:
                        err_msg = str(first_err)
                parse_symbol_restore_response(
                    {"ok": False, "error_code": err_code, "error": err_msg},
                    target,
                )
            if len(parsed_batch["results"]) != 1:
                raise BridgeExecutionError(
                    f"expected 1 symbol restore result, got {len(parsed_batch['results'])}"
                )
            item_res = parsed_batch["results"][0]
            transport = str(
                parsed_batch["session"].get("transport") or self.last_transport
            )
            sym_payload = _batch_symbol_result_to_restore_payload(item_res)
            try:
                receipt = parse_symbol_restore_response(sym_payload, target)
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid symbol restore response: {exc}") from exc

            readback_raw = item_res.get("readback")
            if not isinstance(readback_raw, Mapping):
                raise BridgeExecutionError(
                    "inline symbol readback payload missing from batch restore response"
                )
            try:
                actual = raw_symbol_capture_to_snapshot(readback_raw)
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid symbol readback response: {exc}") from exc
            verify_symbol_roundtrip(snapshot, actual, raise_on_mismatch=True)
            return replace(receipt, readback_verified=True, transport=transport)

        raw_resp = self.run_skill_request(
            "restore_symbol",
            {**req_dict, "readback": verify_readback} if self._should_use_daemon(session) else req_dict,
            cds_lib=validated_cds_lib,
            session=session,
        )
        transport = self.last_transport
        daemon_error = self.last_daemon_error
        inline_rb: Mapping[str, Any] | None = None
        if raw_resp.get("protocol") == SPOOL_PROTOCOL_V1:
            if raw_resp.get("ok") is False and not isinstance(raw_resp.get("result"), Mapping):
                sym_payload = {
                    "ok": False,
                    "error": raw_resp.get("error"),
                    "error_code": raw_resp.get("error_code"),
                }
            else:
                res_obj = raw_resp.get("result")
                if not isinstance(res_obj, Mapping):
                    raise BridgeExecutionError("daemon symbol restore response missing 'result'")
                inline_rb = (
                    res_obj.get("readback")
                    if isinstance(res_obj.get("readback"), Mapping)
                    else None
                )
                sym_payload = _batch_symbol_result_to_restore_payload(res_obj)
        else:
            sym_payload = dict(raw_resp)

        try:
            receipt = parse_symbol_restore_response(sym_payload, target)
        except FailClosedError:
            raise
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid symbol restore response: {exc}") from exc

        details = dict(receipt.details)
        if daemon_error is not None:
            details["daemon_error"] = daemon_error

        if not verify_readback:
            return replace(receipt, details=details, transport=transport)
        if inline_rb is not None:
            try:
                actual = raw_symbol_capture_to_snapshot(inline_rb)
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid symbol readback response: {exc}") from exc
        else:
            rb_session = (
                OaSession(session_id="sym-rb", transport="one_shot")
                if transport == "one_shot_fallback"
                else session
            )
            actual = self.capture_symbol(
                target, cds_lib=validated_cds_lib, session=rb_session
            )
            self.last_transport = transport
            self.last_daemon_error = daemon_error
        verify_symbol_roundtrip(snapshot, actual, raise_on_mismatch=True)
        return replace(
            receipt,
            details=details,
            readback_verified=True,
            transport=transport,
        )

    def _layout_backend(self) -> Any:
        from .layout_backend import VirtuosoLayoutBackend

        return VirtuosoLayoutBackend(
            virtuoso_cmd=self.virtuoso_cmd,
            ssh_host=self.ssh_host,
            remote_workdir=self.remote_workdir,
            timeout_s=self.timeout_s,
            replay_flag=self.replay_flag,
            runner=self.runner,
            skill_dir=self.skill_dir,
            keep_artifacts=self.keep_artifacts,
            skill_runner=self,
            max_sigsegv_retries=self.crash_retries,
        )

    def capture_layout(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> LayoutSnapshot:
        return self._layout_backend().capture_layout(
            cellview, cds_lib=cds_lib, session=session
        )

    def restore_layout(
        self,
        snapshot: LayoutSnapshot,
        target: CellViewRef | None = None,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        verify_readback: bool = True,
        oa_view_dir: Path | None = None,
        session: OaSession | None = None,
    ) -> LayoutRestoreReceipt:
        from .layout_model import LayoutSnapshot as _LayoutSnapshot

        if not isinstance(snapshot, _LayoutSnapshot):
            raise BridgeExecutionError("snapshot must be a LayoutSnapshot")
        effective_target = snapshot.manifest.cellview if target is None else target
        return self._layout_backend().restore_layout(
            snapshot,
            effective_target,
            cds_lib=cds_lib,
            allow_libraries=allow_libraries,
            overwrite=overwrite,
            allow_omissions=allow_omissions,
            oa_view_dir=oa_view_dir,
            verify_readback=verify_readback,
            session=session,
        )

    def run_skill_request(
        self,
        action: str,
        request: Mapping[str, Any],
        *,
        cds_lib: Path | str | None = None,
        session: OaSession | None = None,
    ) -> Mapping[str, Any]:
        """Frozen transport seam for executing any registered SKILL action.

        Shared by schematic, symbol, layout, and batch actions so SSH, daemon,
        crash retry, and isolated runtime execution have a single owner.
        """

        if not isinstance(action, str) or action not in _ACTION_SCRIPTS:
            raise BridgeExecutionError(
                f"unsupported SKILL action {action!r}; expected one of {sorted(_ACTION_SCRIPTS)}"
            )
        if not isinstance(request, Mapping):
            raise SchemaValidationError("request must be a mapping")
        validated_cds_lib = self._validate_cds_lib(cds_lib, action)
        action_script = _ACTION_SCRIPTS[action]
        extra_scripts = self._extra_scripts_for_action(action)

        def _build_one_shot_request(runtime_root: Path, output_json: Path) -> str:
            payload_dict = dict(request)
            payload_dict["action"] = action
            payload_dict["output_path"] = str(runtime_root / output_json.name)
            if action in {"capture_batch", "restore_batch"}:
                payload_dict.setdefault("protocol", BATCH_PROTOCOL_V1)
                payload_dict["output_dir"] = str(runtime_root)
            return "setq(oagitRequest '" + to_skill_literal(payload_dict) + ")\n"

        def _one_shot_call() -> Mapping[str, Any]:
            return self._execute_one_shot(
                validated_cds_lib,
                action_name=action_script,
                extra_scripts=extra_scripts,
                request_builder=_build_one_shot_request,
            )

        if self._should_use_daemon(session):
            req_id = str(request.get("request_id") or f"req-{uuid.uuid4().hex[:16]}")

            def _build_daemon_request(resp_path: Path) -> str:
                payload_dict = dict(request)
                payload_dict["action"] = action
                payload_dict["request_id"] = req_id
                payload_dict["output_path"] = str(resp_path)
                if action in {"capture_batch", "restore_batch"}:
                    payload_dict["protocol"] = BATCH_PROTOCOL_V1
                    payload_dict["output_dir"] = str(resp_path.parent)
                else:
                    payload_dict.setdefault("protocol", SPOOL_PROTOCOL_V1)
                return "setq(oagitRequest '" + to_skill_literal(payload_dict) + ")\n"

            response, _transport = self._execute_via_daemon(
                action,
                validated_cds_lib,
                request_id=req_id,
                daemon_request_builder=_build_daemon_request,
                one_shot_fn=_one_shot_call,
            )
            return response

        self.last_transport = "one_shot"
        self.last_daemon_error = None
        return _one_shot_call()
