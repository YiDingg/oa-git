"""Lazy auto-sleep headless Virtuoso daemon, SSH ControlMaster, and spool IPC.

Zero HTTP server, zero SQLite/database: all IPC travels over strict 0700
filesystem spools using protocol ``oagit-spool/1`` (and ``oagit-batch/1`` for
batch payloads). Every subprocess and polling primitive is injectable so tests
run 100% offline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any
import uuid

from .bridge import to_skill_literal
from .model import (
    BATCH_PROTOCOL_V1,
    SPOOL_PROTOCOL_V1,
    BridgeExecutionError,
    SchemaValidationError,
)
from .project_state import ensure_oagit_dir

SSH_BIN = "/usr/bin/ssh"
SCP_BIN = "/usr/bin/scp"

DEFAULT_DAEMON_IDLE_TIMEOUT_S = 3600
DEFAULT_HEARTBEAT_STALE_S = 120

_REMOTE_STATE_END_MARKER = "__OAGIT_STATE_END__"
_REMOTE_HEARTBEAT_END_MARKER = "__OAGIT_HEARTBEAT_END__"

ALLOWLISTED_DAEMON_ACTIONS: frozenset[str] = frozenset(
    {
        "ping",
        "stop",
        "capture_schematic",
        "restore_schematic",
        "capture_symbol",
        "restore_symbol",
        "capture_layout",
        "restore_layout",
        "capture_batch",
        "restore_batch",
    }
)

DAEMON_SKILL_SCRIPTS: tuple[str, ...] = (
    "oagit_common.il",
    "daemon.il",
    "export_schematic.il",
    "restore_schematic.il",
    "export_symbol.il",
    "restore_symbol.il",
    "export_layout.il",
    "restore_layout.il",
    "batch_capture.il",
    "batch_restore.il",
)

_SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,254}$")

Runner = Callable[..., subprocess.CompletedProcess[str]]


# ---------------------------------------------------------------------------
# Daemon exception hierarchy
# ---------------------------------------------------------------------------


class DaemonError(BridgeExecutionError):
    """Base error for headless Virtuoso daemon lifecycle and spool failures."""


class DaemonUnavailableError(DaemonError):
    """Raised when the daemon cannot be started, reached, or is not running."""


class DaemonCrashError(DaemonError):
    """Raised when an active daemon crashes, times out, or emits invalid spool IPC."""


# ---------------------------------------------------------------------------
# Deterministic daemon identity & SKILL bundle hashing
# ---------------------------------------------------------------------------


def compute_skill_bundle_sha256(
    skill_dir: Path,
    filenames: Sequence[str] = DAEMON_SKILL_SCRIPTS,
) -> str:
    """Compute a deterministic SHA-256 over all present SKILL bridge files."""

    base = Path(skill_dir)
    hasher = hashlib.sha256()
    for name in sorted(set(filenames)):
        script_path = base / name
        if script_path.is_file():
            hasher.update(name.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(script_path.read_bytes())
            hasher.update(b"\x00")
    return hasher.hexdigest()


def compute_daemon_id(
    cds_lib: str,
    ssh_host: str | None,
    virtuoso_cmd: str,
    remote_workdir: str,
    skill_bundle_sha256: str,
) -> str:
    """Return the 16-char lowercase hex daemon session ID per Section 1.6."""

    if not isinstance(cds_lib, str) or not cds_lib.strip():
        raise SchemaValidationError("cds_lib must be a non-empty string")
    if not isinstance(virtuoso_cmd, str) or not virtuoso_cmd.strip():
        raise SchemaValidationError("virtuoso_cmd must be a non-empty string")
    if not isinstance(remote_workdir, str) or not remote_workdir.strip():
        raise SchemaValidationError("remote_workdir must be a non-empty string")
    if not isinstance(skill_bundle_sha256, str) or not skill_bundle_sha256.strip():
        raise SchemaValidationError("skill_bundle_sha256 must be a non-empty string")

    host_norm = "" if not ssh_host else str(ssh_host).strip()
    if host_norm:
        cds_norm = posixpath.normpath(cds_lib.strip())
    else:
        cds_norm = os.path.realpath(os.path.expanduser(cds_lib.strip()))

    canonical = "\n".join(
        (
            cds_norm,
            host_norm,
            virtuoso_cmd.strip(),
            posixpath.normpath(remote_workdir.strip()),
            skill_bundle_sha256.strip().lower(),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _ensure_0700_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _default_pid_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _validate_request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not _SAFE_REQUEST_ID_RE.fullmatch(request_id):
        raise SchemaValidationError(
            f"invalid daemon request_id {request_id!r}; must match {_SAFE_REQUEST_ID_RE.pattern}"
        )
    if ".." in request_id:
        raise SchemaValidationError(f"request_id must not contain '..': {request_id!r}")
    return request_id


def _validate_ssh_host(ssh_host: str) -> str:
    if not isinstance(ssh_host, str) or not _SAFE_SSH_HOST_RE.fullmatch(ssh_host):
        raise SchemaValidationError(f"invalid ssh_host alias: {ssh_host!r}")
    return ssh_host


def _parse_heartbeat_epoch(heartbeat: str | None) -> float | None:
    if not heartbeat:
        return None
    stripped = heartbeat.strip()
    if not stripped:
        return None
    first_token = stripped.split(None, 1)[0]
    try:
        ts = float(first_token)
    except ValueError:
        return None
    return ts if ts > 1_000_000_000 else None


def build_daemon_driver_skill(
    *,
    daemon_id: str,
    daemon_dir: str | Path,
    spool_dir: str | Path,
    skill_dir: str | Path,
    idle_timeout_s: int = DEFAULT_DAEMON_IDLE_TIMEOUT_S,
    poll_ms: int = 100,
) -> str:
    """Render the bootstrap SKILL file passed to ``virtuoso -restore``."""

    d_dir = str(daemon_dir)
    s_dir = str(spool_dir)
    sk_dir = str(skill_dir)
    common_il = posixpath.join(sk_dir, "oagit_common.il")
    daemon_il = posixpath.join(sk_dir, "daemon.il")
    state_file = posixpath.join(d_dir, "daemon.json")
    heartbeat_file = posixpath.join(d_dir, "heartbeat")

    cfg_dpl = to_skill_literal(
        {
            "protocol": SPOOL_PROTOCOL_V1,
            "daemon_id": daemon_id,
            "daemon_dir": d_dir,
            "spool_dir": s_dir,
            "skill_dir": sk_dir,
            "state_file": state_file,
            "heartbeat_file": heartbeat_file,
            "idle_timeout_s": int(idle_timeout_s),
            "poll_ms": int(poll_ms),
        }
    )
    return (
        f"setq(oagitDaemonConfig '{cfg_dpl})\n"
        "let((oagitBootRes)\n"
        "  oagitBootRes = errset(\n"
        "    progn(\n"
        f"      load({to_skill_literal(common_il)})\n"
        f"      load({to_skill_literal(daemon_il)})\n"
        "      oagitDaemonMain(oagitDaemonConfig)\n"
        "    )\n"
        "    t\n"
        "  )\n"
        "  unless(oagitBootRes exit(1))\n"
        ")\n"
    )


# ---------------------------------------------------------------------------
# SSH ControlMaster Manager (never touches ~/.ssh/config)
# ---------------------------------------------------------------------------


_SSH_CONTROL_PATH_MAX_BYTES = 88


def _ensure_private_runtime_dir(path: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        os.chmod(path, 0o700)
        return True
    except OSError:
        return False


class SshMuxManager:
    """Manage short-lived SSH ControlMaster sockets under ``.oagit/ssh/``."""

    def __init__(
        self,
        project_root: Path,
        *,
        persist_s: int = DEFAULT_DAEMON_IDLE_TIMEOUT_S,
        timeout_s: int = 30,
        runner: Runner | None = None,
    ) -> None:
        if type(persist_s) is not int or persist_s < 0:
            raise SchemaValidationError("persist_s must be an integer >= 0")
        if type(timeout_s) is not int or timeout_s <= 0:
            raise SchemaValidationError("timeout_s must be a positive integer")
        self.project_root = Path(project_root).expanduser().resolve()
        self.persist_s = persist_s
        self.timeout_s = timeout_s
        self.runner: Runner = runner or subprocess.run

    @property
    def ssh_dir(self) -> Path:
        return self.project_root / ".oagit" / "ssh"

    def control_path(self, ssh_host: str) -> Path:
        host = _validate_ssh_host(ssh_host)
        host_hash = hashlib.sha256(host.encode("utf-8")).hexdigest()[:16]
        preferred = self.ssh_dir / f"{host_hash}.sock"
        if len(str(preferred).encode("utf-8")) <= _SSH_CONTROL_PATH_MAX_BYTES:
            ensure_oagit_dir(self.project_root)
            _ensure_0700_dir(self.ssh_dir)
            return preferred

        # OpenSSH appends a random suffix to ControlPath when binding; keep the
        # socket path within the 88-byte sun_path budget via a 0700 short path.
        uid = os.getuid()
        scoped_hash = hashlib.sha256(
            f"{self.project_root}\x00{host}".encode("utf-8")
        ).hexdigest()[:16]
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if xdg_runtime:
            xdg_base = Path(xdg_runtime)
            if xdg_base.is_dir():
                xdg_dir = xdg_base / f"oagit-ssh-{uid}"
                xdg_candidate = xdg_dir / f"{scoped_hash}.sock"
                if (
                    len(str(xdg_candidate).encode("utf-8")) <= _SSH_CONTROL_PATH_MAX_BYTES
                    and _ensure_private_runtime_dir(xdg_dir)
                ):
                    return xdg_candidate

        tmp_dir = Path("/tmp") / f"oagit-ssh-{uid}"
        if not _ensure_private_runtime_dir(tmp_dir):
            raise DaemonUnavailableError(
                f"unable to prepare private SSH socket directory: {tmp_dir}"
            )
        return tmp_dir / f"{scoped_hash}.sock"

    def _mux_options_for_path(self, sock: Path) -> list[str]:
        return [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPersist={self.persist_s}",
            "-o",
            f"ControlPath={sock}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]

    def _mux_options(self, ssh_host: str) -> list[str]:
        return self._mux_options_for_path(self.control_path(ssh_host))

    def ssh_base_args(self, ssh_host: str) -> list[str]:
        """Return ``/usr/bin/ssh`` base argv using the isolated ControlMaster socket."""

        host = _validate_ssh_host(ssh_host)
        return [SSH_BIN, *self._mux_options(host), host]

    def scp_base_args(self, ssh_host: str) -> list[str]:
        """Return ``/usr/bin/scp`` base argv sharing the isolated ControlMaster socket."""

        host = _validate_ssh_host(ssh_host)
        return [SCP_BIN, *self._mux_options(host)]

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                argv,
                timeout=self.timeout_s,
                capture_output=True,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DaemonUnavailableError(
                f"SSH command failed ({shlex.join(argv)}): {exc}"
            ) from exc

    def check_master(self, ssh_host: str) -> bool:
        host = _validate_ssh_host(ssh_host)
        sock = self.control_path(host)
        argv = [SSH_BIN, "-O", "check", "-o", f"ControlPath={sock}", host]
        try:
            result = self._run(argv)
        except DaemonUnavailableError:
            return False
        return getattr(result, "returncode", 1) == 0

    def ensure_master(self, ssh_host: str) -> Path:
        """Ensure an active ControlMaster socket exists without touching ``~/.ssh/config``."""

        host = _validate_ssh_host(ssh_host)
        sock = self.control_path(host)
        if sock.exists() or sock.is_symlink():
            if self.check_master(host):
                return sock
            try:
                sock.unlink(missing_ok=True)
            except OSError:
                pass

        argv = [
            SSH_BIN,
            "-MNf",
            *self._mux_options_for_path(sock),
            host,
        ]
        result = self._run(argv)
        if getattr(result, "returncode", 0) != 0:
            stderr = str(getattr(result, "stderr", "") or "").strip()
            raise DaemonUnavailableError(
                f"failed to start SSH ControlMaster for {host!r} "
                f"(rc={getattr(result, 'returncode', None)}): {stderr}"
            )
        return sock

    def stop_master(self, ssh_host: str) -> bool:
        """Cleanly exit the ControlMaster and remove any stale socket file."""

        host = _validate_ssh_host(ssh_host)
        sock = self.control_path(host)
        argv = [SSH_BIN, "-O", "exit", "-o", f"ControlPath={sock}", host]
        ok = False
        try:
            result = self._run(argv)
            ok = getattr(result, "returncode", 1) == 0
        except DaemonUnavailableError:
            ok = False
        try:
            if sock.exists() or sock.is_symlink():
                sock.unlink()
        except OSError:
            pass
        return ok


# ---------------------------------------------------------------------------
# Headless Virtuoso Daemon Client (local & SSH spool IPC)
# ---------------------------------------------------------------------------


class VirtuosoDaemonClient:
    """Lazy, auto-sleep headless Virtuoso daemon client using 0700 filesystem spool IPC."""

    def __init__(
        self,
        *,
        project_root: Path,
        cds_lib: Path | str,
        virtuoso_cmd: str = "virtuoso",
        ssh_host: str | None = None,
        remote_workdir: str = "/tmp/oagit",
        skill_dir: Path | None = None,
        idle_timeout_s: int = DEFAULT_DAEMON_IDLE_TIMEOUT_S,
        startup_timeout_s: int = 30,
        poll_ms: int = 100,
        timeout_s: int = 180,
        heartbeat_stale_s: int = DEFAULT_HEARTBEAT_STALE_S,
        replay_flag: str = "-restore",
        runner: Runner | None = None,
        popen_factory: Callable[..., Any] | None = None,
        ssh_mux: SshMuxManager | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        clock_fn: Callable[[], float] | None = None,
        pid_alive_fn: Callable[[int], bool] | None = None,
        keep_artifacts: bool = False,
    ) -> None:
        if type(idle_timeout_s) is not int or idle_timeout_s < 0:
            raise SchemaValidationError("idle_timeout_s must be an integer >= 0")
        if type(heartbeat_stale_s) is not int or heartbeat_stale_s <= 0:
            raise SchemaValidationError("heartbeat_stale_s must be a positive integer")
        self.project_root = Path(project_root).expanduser().resolve()
        self.cds_lib = str(cds_lib)
        self.virtuoso_cmd = virtuoso_cmd or "virtuoso"
        self.ssh_host = _validate_ssh_host(ssh_host) if ssh_host else None
        self.remote_workdir = remote_workdir or "/tmp/oagit"
        self.skill_dir = (
            Path(skill_dir)
            if skill_dir is not None
            else Path(__file__).resolve().parent / "skill"
        )
        self.idle_timeout_s = idle_timeout_s
        self.startup_timeout_s = startup_timeout_s
        self.poll_ms = poll_ms
        self.timeout_s = timeout_s
        self.heartbeat_stale_s = heartbeat_stale_s
        self.replay_flag = replay_flag
        self.runner: Runner = runner or subprocess.run
        self.popen_factory: Callable[..., Any] = popen_factory or subprocess.Popen
        default_persist_s = (
            max(idle_timeout_s, 60)
            if idle_timeout_s > 0
            else DEFAULT_DAEMON_IDLE_TIMEOUT_S
        )
        self.ssh_mux = ssh_mux or SshMuxManager(
            self.project_root,
            persist_s=default_persist_s,
            runner=self.runner,
        )
        self.sleep_fn: Callable[[float], None] = sleep_fn or time.sleep
        self._clock_is_custom = clock_fn is not None
        self.clock_fn: Callable[[], float] = clock_fn or time.monotonic
        self.pid_alive_fn: Callable[[int], bool] = pid_alive_fn or _default_pid_alive
        self.keep_artifacts = keep_artifacts

        self.skill_bundle_sha256 = compute_skill_bundle_sha256(self.skill_dir)
        self.daemon_id = compute_daemon_id(
            self.cds_lib,
            self.ssh_host,
            self.virtuoso_cmd,
            self.remote_workdir,
            self.skill_bundle_sha256,
        )
        self.last_daemon_error: str | None = None
        self._proc: Any = None
        self._seen_request_ids: set[str] = set()

    @property
    def local_daemon_dir(self) -> Path:
        return self.project_root / ".oagit" / "daemon" / self.daemon_id

    @property
    def local_spool_dir(self) -> Path:
        return self.local_daemon_dir / "spool"

    @property
    def local_state_file(self) -> Path:
        return self.local_daemon_dir / "daemon.json"

    @property
    def local_heartbeat_file(self) -> Path:
        return self.local_daemon_dir / "heartbeat"

    @property
    def remote_daemon_dir(self) -> str:
        return f"{self.remote_workdir.rstrip('/')}/daemon/{self.daemon_id}"

    @property
    def remote_spool_dir(self) -> str:
        return f"{self.remote_daemon_dir}/spool"

    @property
    def runtime_spool_dir(self) -> Path:
        return Path(self.remote_spool_dir) if self.ssh_host else self.local_spool_dir

    def _prepare_local_dirs(self) -> None:
        ensure_oagit_dir(self.project_root)
        _ensure_0700_dir(self.project_root / ".oagit" / "daemon")
        _ensure_0700_dir(self.local_daemon_dir)
        _ensure_0700_dir(self.local_spool_dir)
        _ensure_0700_dir(self.local_daemon_dir / "tmp")

    def _stage_skill_bundle(
        self,
        *,
        idle_timeout_s: int,
        poll_ms: int,
    ) -> list[Path]:
        self._prepare_local_dirs()
        staged: list[Path] = []
        for required in ("oagit_common.il", "daemon.il"):
            src = self.skill_dir / required
            if not src.is_file():
                raise DaemonUnavailableError(f"missing required daemon SKILL script: {src}")
        for filename in DAEMON_SKILL_SCRIPTS:
            src = self.skill_dir / filename
            if src.is_file():
                dst = self.local_daemon_dir / filename
                shutil.copy2(src, dst)
                staged.append(dst)

        runtime_daemon_dir = self.remote_daemon_dir if self.ssh_host else str(self.local_daemon_dir)
        runtime_spool = self.remote_spool_dir if self.ssh_host else str(self.local_spool_dir)
        driver_text = build_daemon_driver_skill(
            daemon_id=self.daemon_id,
            daemon_dir=runtime_daemon_dir,
            spool_dir=runtime_spool,
            skill_dir=runtime_daemon_dir,
            idle_timeout_s=idle_timeout_s,
            poll_ms=poll_ms,
        )
        driver_path = self.local_daemon_dir / "daemon_driver.il"
        _atomic_write_text(driver_path, driver_text)
        staged.append(driver_path)
        return staged

    def _local_env(self) -> dict[str, str]:
        env = os.environ.copy()
        d_dir = str(self.local_daemon_dir)
        env.update(
            {
                "ENABLE_SOS705": "0",
                "OAGIT_AUTO_EXIT": "0",
                "IC261_WORKDIR": d_dir,
                "CDS_LOG_PATH": str(self.local_daemon_dir / "CDS.log"),
                "TMPDIR": str(self.local_daemon_dir / "tmp"),
                "OAGIT_COMMON_IL": str(self.local_daemon_dir / "oagit_common.il"),
            }
        )
        return env

    def _read_local_state(self) -> dict[str, Any] | None:
        if not self.local_state_file.is_file():
            return None
        try:
            payload = json.loads(self.local_state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_local_state(self, state: str, *, pid: int | None = None, error: str | None = None) -> None:
        self._prepare_local_dirs()
        payload: dict[str, Any] = {
            "protocol": SPOOL_PROTOCOL_V1,
            "daemon_id": self.daemon_id,
            "state": state,
            "pid": pid,
            "cds_lib": self.cds_lib,
            "ssh_host": self.ssh_host,
        }
        if error is not None:
            payload["last_error"] = error
        _atomic_write_text(
            self.local_state_file,
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
        )

    def _mark_dead(self, reason: str) -> None:
        self.last_daemon_error = reason
        self._proc = None
        try:
            self._write_local_state("dead", pid=None, error=reason)
        except OSError:
            pass

    def _is_local_proc_dead(self) -> bool:
        if self._proc is not None:
            polled = self._proc.poll()
            if polled is not None:
                return True
            return False
        state = self._read_local_state()
        if state is None:
            return True
        if state.get("state") in ("stopping", "stopped", "dead"):
            return True
        pid = state.get("pid")
        if isinstance(pid, int) and pid > 0:
            return not self.pid_alive_fn(pid)
        return False

    def _ssh_run(
        self,
        shell_command: str,
        *,
        timeout_s: float | None = None,
        extra_ssh_args: Sequence[str] = (),
    ) -> subprocess.CompletedProcess[str]:
        assert self.ssh_host is not None
        base = self.ssh_mux.ssh_base_args(self.ssh_host)
        if extra_ssh_args:
            argv = [base[0], *extra_ssh_args, *base[1:], shell_command]
        else:
            argv = [*base, shell_command]
        try:
            return self.runner(
                argv,
                timeout=timeout_s or self.timeout_s,
                capture_output=True,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DaemonCrashError(f"remote daemon SSH command failed: {exc}") from exc

    def _scp_upload(self, local_src: Path, remote_dst: str) -> None:
        assert self.ssh_host is not None
        argv = [
            *self.ssh_mux.scp_base_args(self.ssh_host),
            str(local_src),
            f"{self.ssh_host}:{shlex.quote(remote_dst)}",
        ]
        try:
            result = self.runner(
                argv,
                timeout=self.timeout_s,
                capture_output=True,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise DaemonCrashError(f"remote daemon SCP upload failed: {exc}") from exc
        if getattr(result, "returncode", 0) != 0:
            stderr = str(getattr(result, "stderr", "") or "").strip()
            raise DaemonCrashError(f"remote daemon SCP upload failed: {stderr}")

    def _remote_pid_alive(self, pid: int) -> bool:
        if type(pid) is not int or pid <= 0:
            return False
        probe = self._ssh_run(f"kill -0 {pid} 2>/dev/null", timeout_s=15)
        return getattr(probe, "returncode", 1) == 0

    def _current_epoch_time(self) -> float | None:
        if self._clock_is_custom:
            now_val = float(self.clock_fn())
            return now_val if now_val > 1_000_000_000 else None
        return time.time()

    def _is_heartbeat_stale(self, heartbeat: str | None) -> bool:
        hb_ts = _parse_heartbeat_epoch(heartbeat)
        if hb_ts is None:
            return False
        now_epoch = self._current_epoch_time()
        if now_epoch is None:
            return False
        return (now_epoch - hb_ts) > float(self.heartbeat_stale_s)

    def _probe_remote_live_daemon(self) -> dict[str, Any] | None:
        """Probe remote ``daemon.json``, ``heartbeat``, and PID liveness in one SSH call.

        Callers must ensure an active SSH ControlMaster (via ``ensure_master``) first.
        Returns a metadata dict if a live daemon matching ``self.daemon_id`` with
        ``state in ("ready", "running")`` is active on the remote host, else ``None``.
        """

        assert self.ssh_host is not None
        rdir = self.remote_daemon_dir
        q_state = shlex.quote(f"{rdir}/daemon.json")
        q_hb = shlex.quote(f"{rdir}/heartbeat")
        probe_cmd = (
            f"if [ ! -f {q_state} ]; then exit 0; fi; "
            f"cat {q_state}; "
            f"printf '\\n{_REMOTE_STATE_END_MARKER}\\n'; "
            f"if [ -f {q_hb} ]; then cat {q_hb}; fi; "
            f"printf '\\n{_REMOTE_HEARTBEAT_END_MARKER}\\n'; "
            f"pid=$(sed -n 's/.*\"pid\"[[:space:]]*:[[:space:]]*\\([0-9][0-9]*\\).*/\\1/p' {q_state} | head -n 1); "
            "if [ -n \"$pid\" ] && [ \"$pid\" -gt 0 ] 2>/dev/null && kill -0 \"$pid\" 2>/dev/null; "
            "then printf 'alive\\n'; else printf 'dead\\n'; fi"
        )
        try:
            probe = self._ssh_run(probe_cmd, timeout_s=15)
        except DaemonError:
            return None
        if getattr(probe, "returncode", 1) != 0:
            return None
        stdout = probe.stdout or ""
        if not stdout.strip():
            return None

        heartbeat: str | None = None
        pid_status: str | None = None
        if _REMOTE_STATE_END_MARKER in stdout:
            state_part, rest = stdout.split(_REMOTE_STATE_END_MARKER, 1)
            if _REMOTE_HEARTBEAT_END_MARKER in rest:
                hb_part, pid_part = rest.split(_REMOTE_HEARTBEAT_END_MARKER, 1)
                heartbeat = hb_part.strip() or None
                pid_status = pid_part.strip()
            else:
                heartbeat = rest.strip() or None
        else:
            state_part = stdout

        if not state_part.strip():
            return None
        try:
            remote_state = json.loads(state_part)
        except json.JSONDecodeError:
            return None
        if not isinstance(remote_state, dict):
            return None
        if remote_state.get("daemon_id") != self.daemon_id:
            return None
        state_name = str(remote_state.get("state", "stopped"))
        if state_name not in ("ready", "running"):
            return None
        raw_pid = remote_state.get("pid")
        pid = raw_pid if type(raw_pid) is int and raw_pid > 0 else None
        if pid is None:
            return None

        if pid_status is not None:
            alive = pid_status == "alive"
        else:
            alive = self._remote_pid_alive(pid)
        if not alive:
            return None
        if self._is_heartbeat_stale(heartbeat):
            return None

        return {
            "daemon_id": self.daemon_id,
            "state": state_name,
            "pid": pid,
            "heartbeat": heartbeat,
            "raw_state": remote_state,
        }

    def status(self) -> dict[str, Any]:
        """Inspect daemon liveness and metadata without mutating active state."""

        heartbeat: str | None = None
        if self.local_heartbeat_file.is_file():
            try:
                heartbeat = self.local_heartbeat_file.read_text(encoding="utf-8").strip()
            except OSError:
                heartbeat = None

        if self.ssh_host:
            running = False
            state_name = "stopped"
            pid: int | None = None
            state_data = self._read_local_state()
            if state_data and state_data.get("state") in ("ready", "running"):
                if self.ssh_mux.check_master(self.ssh_host):
                    q_state = shlex.quote(self.remote_daemon_dir + "/daemon.json")
                    q_hb = shlex.quote(self.remote_daemon_dir + "/heartbeat")
                    probe = self._ssh_run(
                        f"if [ -f {q_state} ]; then cat {q_state}; fi; "
                        f"printf '\\n{_REMOTE_STATE_END_MARKER}\\n'; "
                        f"if [ -f {q_hb} ]; then cat {q_hb}; fi",
                        timeout_s=15,
                    )
                    raw_out = probe.stdout or ""
                    if getattr(probe, "returncode", 1) == 0 and raw_out.strip():
                        if _REMOTE_STATE_END_MARKER in raw_out:
                            state_part, hb_part = raw_out.split(_REMOTE_STATE_END_MARKER, 1)
                            remote_hb = hb_part.strip() or None
                            if remote_hb is not None:
                                heartbeat = remote_hb
                        else:
                            state_part = raw_out
                        if state_part.strip():
                            try:
                                remote_state = json.loads(state_part)
                                state_name = str(remote_state.get("state", "stopped"))
                                raw_pid = remote_state.get("pid")
                                pid = raw_pid if type(raw_pid) is int else None
                                if state_name in ("ready", "running"):
                                    if (
                                        pid is not None
                                        and pid > 0
                                        and self._remote_pid_alive(pid)
                                        and not self._is_heartbeat_stale(heartbeat)
                                    ):
                                        running = True
                                    else:
                                        running = False
                                        state_name = "dead"
                            except json.JSONDecodeError:
                                state_name = "dead"
                        else:
                            state_name = "dead"
                    else:
                        state_name = "dead"
                else:
                    state_name = "stopped"
            return {
                "daemon_id": self.daemon_id,
                "protocol": SPOOL_PROTOCOL_V1,
                "running": running,
                "state": state_name,
                "pid": pid,
                "daemon_dir": self.remote_daemon_dir,
                "spool_dir": self.remote_spool_dir,
                "ssh_host": self.ssh_host,
                "cds_lib": self.cds_lib,
                "heartbeat": heartbeat,
                "last_error": self.last_daemon_error,
            }

        state_data = self._read_local_state()
        if state_data is None:
            return {
                "daemon_id": self.daemon_id,
                "protocol": SPOOL_PROTOCOL_V1,
                "running": False,
                "state": "stopped",
                "pid": None,
                "daemon_dir": str(self.local_daemon_dir),
                "spool_dir": str(self.local_spool_dir),
                "ssh_host": None,
                "cds_lib": self.cds_lib,
                "heartbeat": heartbeat,
                "last_error": self.last_daemon_error,
            }

        raw_state = str(state_data.get("state", "stopped"))
        pid = state_data.get("pid")
        pid_int = pid if type(pid) is int else None
        alive = False
        if raw_state in ("ready", "running"):
            if self._proc is not None:
                alive = self._proc.poll() is None
            elif pid_int is not None:
                alive = self.pid_alive_fn(pid_int)
            else:
                alive = False
            if alive and self._is_heartbeat_stale(heartbeat):
                alive = False
            if not alive:
                raw_state = "dead"

        return {
            "daemon_id": self.daemon_id,
            "protocol": SPOOL_PROTOCOL_V1,
            "running": alive,
            "state": raw_state,
            "pid": pid_int,
            "daemon_dir": str(self.local_daemon_dir),
            "spool_dir": str(self.local_spool_dir),
            "ssh_host": None,
            "cds_lib": self.cds_lib,
            "heartbeat": heartbeat,
            "last_error": state_data.get("last_error") or self.last_daemon_error,
        }

    def start(
        self,
        cds_lib: Path | str | None = None,
        *,
        idle_timeout_s: int | None = None,
        startup_timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Start the headless Virtuoso daemon (idempotent if already healthy)."""

        if cds_lib is not None:
            new_cds_lib = str(cds_lib)
            if new_cds_lib != self.cds_lib:
                self.cds_lib = new_cds_lib
                new_daemon_id = compute_daemon_id(
                    self.cds_lib,
                    self.ssh_host,
                    self.virtuoso_cmd,
                    self.remote_workdir,
                    self.skill_bundle_sha256,
                )
                if new_daemon_id != self.daemon_id:
                    self.daemon_id = new_daemon_id
                    self._proc = None
        effective_idle = self.idle_timeout_s if idle_timeout_s is None else idle_timeout_s
        if type(effective_idle) is not int or effective_idle < 0:
            raise SchemaValidationError("idle_timeout_s must be an integer >= 0")
        effective_startup = (
            self.startup_timeout_s if startup_timeout_s is None else startup_timeout_s
        )
        if type(effective_startup) is not int or effective_startup <= 0:
            raise SchemaValidationError("startup_timeout_s must be a positive integer")
        self.ssh_mux.persist_s = (
            max(effective_idle, 60)
            if effective_idle > 0
            else DEFAULT_DAEMON_IDLE_TIMEOUT_S
        )

        current = self.status()
        if current["running"]:
            current["reused"] = True
            return current

        staged_files = self._stage_skill_bundle(
            idle_timeout_s=effective_idle,
            poll_ms=self.poll_ms,
        )

        if self.ssh_host:
            return self._start_remote(
                staged_files,
                startup_timeout_s=effective_startup,
            )
        return self._start_local(startup_timeout_s=effective_startup)

    def _start_local(self, *, startup_timeout_s: int) -> dict[str, Any]:
        # Remove stale daemon.json from prior dead run before spawning.
        if self.local_state_file.exists():
            try:
                self.local_state_file.unlink()
            except OSError:
                pass

        command = [
            self.virtuoso_cmd,
            "-nograph",
            "-nocdsinit",
            "-cdslib",
            self.cds_lib,
            "-log",
            str(self.local_daemon_dir / "virtuoso.log"),
            self.replay_flag,
            str(self.local_daemon_dir / "daemon_driver.il"),
        ]
        try:
            proc = self.popen_factory(
                command,
                cwd=str(self.local_daemon_dir),
                env=self._local_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self._mark_dead(f"failed to launch Virtuoso daemon: {exc}")
            raise DaemonUnavailableError(
                f"unable to launch headless Virtuoso daemon ({shlex.join(command)}): {exc}"
            ) from exc

        self._proc = proc
        deadline = self.clock_fn() + float(startup_timeout_s)
        poll_sec = max(self.poll_ms / 1000.0, 0.005)
        while True:
            state = self._read_local_state()
            if (
                state is not None
                and state.get("daemon_id") == self.daemon_id
                and state.get("state") in ("ready", "running")
            ):
                result = self.status()
                result["reused"] = False
                return result
            polled = proc.poll()
            if polled is not None:
                self._mark_dead(f"Virtuoso daemon exited early with code {polled}")
                raise DaemonUnavailableError(
                    f"headless Virtuoso daemon exited before ready (returncode={polled})"
                )
            if self.clock_fn() >= deadline:
                self._mark_dead(f"Virtuoso daemon startup timed out after {startup_timeout_s}s")
                raise DaemonUnavailableError(
                    f"headless Virtuoso daemon did not become ready within {startup_timeout_s}s"
                )
            self.sleep_fn(poll_sec)

    def _start_remote(
        self,
        staged_files: Sequence[Path],
        *,
        startup_timeout_s: int,
    ) -> dict[str, Any]:
        assert self.ssh_host is not None
        self.ssh_mux.ensure_master(self.ssh_host)
        live = self._probe_remote_live_daemon()
        if live is not None:
            self._write_local_state(live["state"], pid=live["pid"])
            out = self.status()
            out["reused"] = True
            return out

        rdir = self.remote_daemon_dir
        mkdir_cmd = (
            f"mkdir -p -m 700 {shlex.quote(rdir)} {shlex.quote(rdir + '/spool')} "
            f"{shlex.quote(rdir + '/tmp')} && chmod 700 {shlex.quote(rdir)} "
            f"{shlex.quote(rdir + '/spool')} && rm -f {shlex.quote(rdir + '/daemon.json')}"
        )
        res = self._ssh_run(mkdir_cmd, timeout_s=30)
        if getattr(res, "returncode", 0) != 0:
            raise DaemonUnavailableError(
                f"failed to prepare remote daemon dir {rdir}: {res.stderr}"
            )

        for local_file in staged_files:
            self._scp_upload(local_file, f"{rdir}/{local_file.name}")

        remote_env = (
            f"ENABLE_SOS705=0 OAGIT_AUTO_EXIT=0 IC261_WORKDIR={shlex.quote(rdir)} "
            f"CDS_LOG_PATH={shlex.quote(rdir + '/CDS.log')} "
            f"TMPDIR={shlex.quote(rdir + '/tmp')} "
            f"OAGIT_COMMON_IL={shlex.quote(rdir + '/oagit_common.il')}"
        )
        launch_tag = uuid.uuid4().hex[:8]
        virt_cmd = shlex.join(
            [
                self.virtuoso_cmd,
                "-nograph",
                "-nocdsinit",
                "-cdslib",
                self.cds_lib,
                "-log",
                f"{rdir}/virtuoso-{launch_tag}.log",
                self.replay_flag,
                f"{rdir}/daemon_driver.il",
            ]
        )
        launch_log = shlex.quote(f"{rdir}/launch-{launch_tag}.log")
        fd_close = "3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-"
        # Use `;` (not `&&`) before the background `&`, detach with `setsid`
        # when available, close inherited non-stdio fds 3..9, and invoke ssh
        # with `-n` so Virtuoso/Xvfb/cdsMsgServer/libManager cannot keep any
        # SSH ControlMaster fd open and trigger a 30s timeout.
        launch_cmd = (
            f"cd {shlex.quote(rdir)} || exit 1; "
            f"if command -v setsid >/dev/null 2>&1; then "
            f"nohup setsid env {remote_env} {virt_cmd} </dev/null >{launch_log} 2>&1 {fd_close} & "
            f"else "
            f"nohup env {remote_env} {virt_cmd} </dev/null >{launch_log} 2>&1 {fd_close} & "
            f"fi; echo $!"
        )
        launch_res = self._ssh_run(launch_cmd, timeout_s=30, extra_ssh_args=("-n",))
        if getattr(launch_res, "returncode", 0) != 0:
            raise DaemonUnavailableError(
                f"failed to launch remote Virtuoso daemon: {launch_res.stderr}"
            )

        deadline = self.clock_fn() + float(startup_timeout_s)
        poll_sec = max(self.poll_ms / 1000.0, 0.01)
        while True:
            probe = self._ssh_run(
                f"if [ -f {shlex.quote(rdir + '/daemon.json')} ]; then "
                f"cat {shlex.quote(rdir + '/daemon.json')}; fi",
                timeout_s=15,
            )
            if getattr(probe, "returncode", 1) == 0 and (probe.stdout or "").strip():
                try:
                    remote_state = json.loads(probe.stdout)
                except json.JSONDecodeError:
                    remote_state = None
                if (
                    isinstance(remote_state, dict)
                    and remote_state.get("daemon_id") == self.daemon_id
                    and remote_state.get("state") in ("ready", "running")
                ):
                    self._write_local_state(
                        "ready",
                        pid=remote_state.get("pid") if isinstance(remote_state.get("pid"), int) else None,
                    )
                    out = self.status()
                    out["reused"] = False
                    return out
            if self.clock_fn() >= deadline:
                self._mark_dead(
                    f"remote Virtuoso daemon startup timed out after {startup_timeout_s}s"
                )
                raise DaemonUnavailableError(
                    f"remote Virtuoso daemon did not become ready within {startup_timeout_s}s"
                )
            self.sleep_fn(poll_sec)

    def stop(self, *, timeout_s: float = 15.0) -> dict[str, Any]:
        """Gracefully stop the daemon (and exit SSH ControlMaster in remote mode)."""

        local_state = self._read_local_state()
        try:
            st = self.status()
        except DaemonError as exc:
            self.last_daemon_error = str(exc)
            st = {"running": False, "state": "dead"}

        if (
            self.ssh_host
            and not st["running"]
            and (local_state is None or local_state.get("state") != "stopped")
        ):
            try:
                self.ssh_mux.ensure_master(self.ssh_host)
                live = self._probe_remote_live_daemon()
                if live is not None:
                    self._write_local_state(live["state"], pid=live["pid"])
                    st = {
                        "running": True,
                        "state": live["state"],
                        "pid": live["pid"],
                    }
            except DaemonError as exc:
                if self.last_daemon_error is None:
                    self.last_daemon_error = str(exc)

        stop_response: Mapping[str, Any] | None = None
        if st["running"]:
            req_id = f"stop-{uuid.uuid4().hex[:12]}"
            resp_path = str(self.runtime_spool_dir / f"{req_id}.resp.json")
            stop_il = (
                "setq(oagitRequest '"
                + to_skill_literal(
                    {
                        "protocol": SPOOL_PROTOCOL_V1,
                        "request_id": req_id,
                        "action": "stop",
                        "output_path": resp_path,
                    }
                )
                + ")\n"
            )
            try:
                stop_response = self.execute_request(
                    "stop",
                    stop_il,
                    request_id=req_id,
                    timeout_s=timeout_s,
                )
            except DaemonError:
                stop_response = None

        self._proc = None
        try:
            self._write_local_state("stopped", pid=None)
        except OSError:
            pass

        ssh_stopped = False
        if self.ssh_host:
            ssh_stopped = self.ssh_mux.stop_master(self.ssh_host)

        return {
            "daemon_id": self.daemon_id,
            "stopped": True,
            "was_running": bool(st["running"]),
            "stop_response": dict(stop_response) if stop_response is not None else None,
            "ssh_master_stopped": ssh_stopped if self.ssh_host else None,
        }

    def _validate_spool_response(
        self,
        raw_text: str,
        *,
        expected_request_id: str,
        expected_protocols: frozenset[str],
    ) -> dict[str, Any]:
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise DaemonCrashError(
                f"malformed JSON in daemon spool response for {expected_request_id}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise DaemonCrashError(
                f"daemon spool response for {expected_request_id} must be a JSON object"
            )
        resp_id = payload.get("request_id")
        if resp_id != expected_request_id:
            raise DaemonCrashError(
                f"daemon spool response request_id mismatch: expected {expected_request_id!r}, got {resp_id!r}"
            )
        proto = payload.get("protocol")
        if proto not in expected_protocols:
            raise DaemonCrashError(
                f"daemon spool response protocol mismatch: expected one of {sorted(expected_protocols)}, got {proto!r}"
            )
        return payload

    def execute_request(
        self,
        action: str,
        request_il_text: str,
        *,
        request_id: str | None = None,
        timeout_s: float | None = None,
        expected_protocols: Sequence[str] = (SPOOL_PROTOCOL_V1, BATCH_PROTOCOL_V1),
    ) -> Mapping[str, Any]:
        """Submit one atomic spool request and wait for its validated JSON response."""

        if action not in ALLOWLISTED_DAEMON_ACTIONS:
            raise SchemaValidationError(
                f"unsupported daemon action {action!r}; allowed: {sorted(ALLOWLISTED_DAEMON_ACTIONS)}"
            )
        if not isinstance(request_il_text, str) or not request_il_text.strip():
            raise SchemaValidationError("request_il_text must be a non-empty SKILL string")

        req_id = _validate_request_id(request_id or f"req-{uuid.uuid4().hex[:16]}")
        if req_id in self._seen_request_ids:
            raise DaemonCrashError(f"duplicate daemon request_id rejected: {req_id!r}")
        self._seen_request_ids.add(req_id)

        effective_timeout = float(self.timeout_s if timeout_s is None else timeout_s)
        allowed_protos = frozenset(expected_protocols)

        if self.ssh_host:
            return self._execute_remote_spool(
                req_id,
                request_il_text,
                timeout_s=effective_timeout,
                expected_protocols=allowed_protos,
            )
        return self._execute_local_spool(
            req_id,
            request_il_text,
            timeout_s=effective_timeout,
            expected_protocols=allowed_protos,
        )

    def _execute_local_spool(
        self,
        req_id: str,
        request_il_text: str,
        *,
        timeout_s: float,
        expected_protocols: frozenset[str],
    ) -> dict[str, Any]:
        self._prepare_local_dirs()
        spool = self.local_spool_dir
        req_tmp = spool / f"{req_id}.req.il.tmp"
        req_final = spool / f"{req_id}.req.il"
        resp_file = spool / f"{req_id}.resp.json"

        if resp_file.exists():
            raise DaemonCrashError(f"stale response already exists for request_id={req_id!r}")

        # Atomic write: <id>.req.il.tmp -> flush -> fsync -> os.replace(<id>.req.il)
        try:
            with req_tmp.open("w", encoding="utf-8") as handle:
                handle.write(request_il_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(req_tmp, 0o600)
            os.replace(req_tmp, req_final)
        except OSError as exc:
            try:
                req_tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise DaemonUnavailableError(f"failed to enqueue spool request {req_id}: {exc}") from exc

        deadline = self.clock_fn() + timeout_s
        poll_sec = max(self.poll_ms / 1000.0, 0.005)
        while True:
            if resp_file.is_file():
                try:
                    raw_text = resp_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise DaemonCrashError(
                        f"failed to read daemon response {resp_file}: {exc}"
                    ) from exc
                finally:
                    if not self.keep_artifacts:
                        try:
                            resp_file.unlink(missing_ok=True)
                        except OSError:
                            pass
                return self._validate_spool_response(
                    raw_text,
                    expected_request_id=req_id,
                    expected_protocols=expected_protocols,
                )

            if self._is_local_proc_dead():
                raise DaemonCrashError(
                    f"headless Virtuoso daemon died before answering request {req_id!r}"
                )

            if self.clock_fn() >= deadline:
                raise DaemonCrashError(
                    f"daemon spool request {req_id!r} timed out after {timeout_s:.1f}s"
                )
            self.sleep_fn(poll_sec)

    def _execute_remote_spool(
        self,
        req_id: str,
        request_il_text: str,
        *,
        timeout_s: float,
        expected_protocols: frozenset[str],
    ) -> dict[str, Any]:
        assert self.ssh_host is not None
        self._prepare_local_dirs()
        local_stage = self.local_spool_dir / f"{req_id}.req.il.stage"
        _atomic_write_text(local_stage, request_il_text)
        remote_tmp = f"{self.remote_spool_dir}/{req_id}.req.il.tmp"
        remote_req = f"{self.remote_spool_dir}/{req_id}.req.il"
        remote_resp = f"{self.remote_spool_dir}/{req_id}.resp.json"

        try:
            self._scp_upload(local_stage, remote_tmp)
        finally:
            if not self.keep_artifacts:
                local_stage.unlink(missing_ok=True)

        mv_res = self._ssh_run(
            f"mv {shlex.quote(remote_tmp)} {shlex.quote(remote_req)}",
            timeout_s=30,
        )
        if getattr(mv_res, "returncode", 0) != 0:
            raise DaemonCrashError(
                f"failed to commit remote spool request {req_id!r}: {mv_res.stderr}"
            )

        local_state = self._read_local_state()
        remote_pid: int | None = None
        if isinstance(local_state, dict):
            raw_pid = local_state.get("pid")
            if type(raw_pid) is int and raw_pid > 0:
                remote_pid = raw_pid

        q_resp = shlex.quote(remote_resp)
        if remote_pid is not None:
            poll_cmd = (
                f"if [ -f {q_resp} ]; then "
                f"cat {q_resp} && rm -f {q_resp}; "
                f"elif ! kill -0 {remote_pid} 2>/dev/null; then "
                f"if [ -f {q_resp} ]; then cat {q_resp} && rm -f {q_resp}; "
                "else exit 43; fi; "
                "else exit 42; fi"
            )
        else:
            poll_cmd = (
                f"if [ -f {q_resp} ]; then "
                f"cat {q_resp} && rm -f {q_resp}; "
                "else exit 42; fi"
            )

        deadline = self.clock_fn() + timeout_s
        poll_sec = max(self.poll_ms / 1000.0, 0.02)
        while True:
            fetch = self._ssh_run(poll_cmd, timeout_s=30)
            rc = getattr(fetch, "returncode", 1)
            if rc == 0:
                return self._validate_spool_response(
                    fetch.stdout or "",
                    expected_request_id=req_id,
                    expected_protocols=expected_protocols,
                )
            if rc == 43:
                raise DaemonCrashError(
                    f"remote headless Virtuoso daemon (pid={remote_pid}) died before answering request {req_id!r}"
                )
            if rc != 42:
                raise DaemonCrashError(
                    f"remote daemon poll failed (rc={rc}): {fetch.stderr}"
                )
            if self.clock_fn() >= deadline:
                raise DaemonCrashError(
                    f"remote daemon request {req_id!r} timed out after {timeout_s:.1f}s"
                )
            self.sleep_fn(poll_sec)

    def execute_with_fallback(
        self,
        action: str,
        request_il_text: str,
        *,
        request_id: str | None = None,
        one_shot_fn: Callable[[], Mapping[str, Any]],
        timeout_s: float | None = None,
    ) -> tuple[Mapping[str, Any], str]:
        """Lazy-start daemon and execute; on daemon crash/failure fall back ONCE to one-shot.

        Returns ``(response_payload, transport)`` where ``transport`` is either
        ``"daemon"`` or ``"one_shot_fallback"`` (never masquerading fallback as
        daemon success).
        """

        self.last_daemon_error = None
        try:
            if not self.status()["running"]:
                self.start()
            response = self.execute_request(
                action,
                request_il_text,
                request_id=request_id,
                timeout_s=timeout_s,
            )
            return response, "daemon"
        except DaemonError as exc:
            self._mark_dead(str(exc))

        fallback_raw = one_shot_fn()
        if isinstance(fallback_raw, Mapping):
            fallback_payload: dict[str, Any] = dict(fallback_raw)
            if isinstance(fallback_payload.get("session"), Mapping):
                session_copy = dict(fallback_payload["session"])
                session_copy["transport"] = "one_shot_fallback"
                session_copy["reused"] = False
                session_copy["daemon_error"] = self.last_daemon_error
                fallback_payload["session"] = session_copy
            return fallback_payload, "one_shot_fallback"
        return fallback_raw, "one_shot_fallback"


__all__ = [
    "SSH_BIN",
    "SCP_BIN",
    "DEFAULT_DAEMON_IDLE_TIMEOUT_S",
    "DEFAULT_HEARTBEAT_STALE_S",
    "ALLOWLISTED_DAEMON_ACTIONS",
    "DAEMON_SKILL_SCRIPTS",
    "DaemonError",
    "DaemonUnavailableError",
    "DaemonCrashError",
    "compute_skill_bundle_sha256",
    "compute_daemon_id",
    "build_daemon_driver_skill",
    "SshMuxManager",
    "VirtuosoDaemonClient",
]
