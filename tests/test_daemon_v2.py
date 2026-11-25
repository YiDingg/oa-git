"""Offline unit & integration tests for Git-like project state, dirty index,
SSH ControlMaster, and headless Virtuoso daemon (v0.2).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any

import pytest

from oa_git.daemon import (
    DEFAULT_DAEMON_IDLE_TIMEOUT_S,
    SCP_BIN,
    SSH_BIN,
    DaemonCrashError,
    DaemonUnavailableError,
    SshMuxManager,
    VirtuosoDaemonClient,
    build_daemon_driver_skill,
    compute_daemon_id,
    compute_skill_bundle_sha256,
)
from oa_git.model import (
    BATCH_PROTOCOL_V1,
    CONFIG_SCHEMA_V1,
    CONFIG_SCHEMA_V2,
    INDEX_SCHEMA_V1,
    SPOOL_PROTOCOL_V1,
    CellViewRef,
    Instance,
    InstancePlacement,
    Net,
    Port,
    SchemaValidationError,
    SchematicElectrical,
    SchematicPlacement,
)
from oa_git.project_state import (
    IndexEntry,
    SourceFileFingerprint,
    classify_cellview_state,
    default_source_file_candidates,
    discover_project_root,
    fingerprint_source_files,
    load_index,
    load_project_config,
    remove_index_entry,
    save_index,
    update_index_entry,
    write_project_config,
)
from oa_git.snapshot import build_schematic_snapshot, snapshot_dir_for, write_snapshot
import oa_git.project_state as project_state_mod


ROOT = Path(__file__).resolve().parents[1]
DAEMON_IL = ROOT / "src" / "oa_git" / "skill" / "daemon.il"


def _make_sample_snapshot(cv: CellViewRef):
    electrical = SchematicElectrical(
        ports=(Port(name="IN", direction="input"),),
        nets=(Net(name="IN"),),
        instances=(
            Instance(
                name="M1",
                lib="pdk",
                cell="nch",
                view="symbol",
                connections={"G": "IN"},
                params={"w": "2u"},
                param_types={"w": "string"},
            ),
        ),
    )
    placement = SchematicPlacement(
        instances=(InstancePlacement(name="M1", xy=(1.0, 2.0), orient="R0"),),
    )
    return build_schematic_snapshot(cv, electrical, placement)


def _balanced_skill_parentheses(source: str) -> None:
    depth = 0
    state = "normal"
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if state == "string":
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                state = "normal"
            index += 1
            continue
        if state == "line_comment":
            if char in "\r\n":
                state = "normal"
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "normal"
                index += 2
            else:
                index += 1
            continue
        if char == '"':
            state = "string"
        elif char == ";":
            state = "line_comment"
        elif char == "/" and next_char == "*":
            state = "block_comment"
            index += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise AssertionError(f"unmatched closing parenthesis at offset {index}")
        index += 1
    assert state == "normal", f"unterminated SKILL {state}"
    assert depth == 0, f"unbalanced parentheses, depth={depth}"


# ---------------------------------------------------------------------------
# 1. Root Discovery & Project Config Tests
# ---------------------------------------------------------------------------


def test_discover_project_root_v2_from_nested_subdir(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    nested = project / "sub" / "deep"
    nested.mkdir(parents=True)
    cfg_path = write_project_config(
        project,
        {
            "snapshots_dir": "snapshots",
            "cds_lib": "eda/cds.lib",
            "oa_libs": {"work": "oa/work"},
            "allow_libraries": ["work"],
            "backend": "virtuoso",
        },
    )
    found_root, found_cfg, schema = discover_project_root(nested)
    assert found_root == project.resolve()
    assert found_cfg == cfg_path
    assert schema == CONFIG_SCHEMA_V2
    # .oagit directory must be 0700
    assert stat.S_IMODE((project / ".oagit").stat().st_mode) == 0o700


def test_discover_project_root_legacy_v1_fallback_preserves_file(tmp_path: Path) -> None:
    project = tmp_path / "legacy_proj"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    legacy_path = project / ".oagit.json"
    legacy_payload = {
        "schema": CONFIG_SCHEMA_V1,
        "snapshots_dir": "snapshots",
        "cds_lib": "cds.lib",
        "allow_libraries": ["work"],
        "backend": "virtuoso",
        "virtuoso_cmd": None,
        "ssh_host": None,
        "remote_workdir": None,
        "timeout_s": 120,
    }
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    found_root, found_cfg, schema = discover_project_root(nested)
    assert found_root == project.resolve()
    assert found_cfg == legacy_path
    assert schema == CONFIG_SCHEMA_V1
    assert legacy_path.is_file()  # Never deleted

    normalized = load_project_config(project)
    assert normalized["schema"] == CONFIG_SCHEMA_V1
    assert normalized["timeout_s"] == 120
    assert normalized["virtuoso_cmd"] == "virtuoso"
    assert normalized["remote_workdir"] == "/tmp/oagit"
    assert normalized["oa_libs"] == {}
    assert normalized["daemon"]["enabled"] is True
    assert normalized["daemon"]["protocol"] == SPOOL_PROTOCOL_V1
    assert normalized["cdf"]["max_passes"] == 3


def test_discover_project_root_explicit_and_missing(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    write_project_config(parent, {"backend": "fixture"})

    # Explicit root inspects ONLY that root, never walking upward
    with pytest.raises(SchemaValidationError, match="oagit init"):
        discover_project_root(child, explicit_root=child)

    found_root, _, schema = discover_project_root(explicit_root=parent)
    assert found_root == parent.resolve()
    assert schema == CONFIG_SCHEMA_V2

    empty = tmp_path / "no_config_anywhere"
    empty.mkdir()
    with pytest.raises(SchemaValidationError, match="oagit init"):
        discover_project_root(empty)


def test_write_project_config_v2_formatting_force_and_secret_rejection(tmp_path: Path) -> None:
    cfg_path = write_project_config(
        tmp_path,
        {
            "oa_libs": {"work": str(tmp_path / "oa" / "work")},
            "allow_libraries": ["work", "work", "analog"],
            "cds_lib": str(tmp_path / "eda" / "cds.lib"),
            "daemon": {"enabled": True, "idle_timeout_s": 45, "poll_ms": 50},
            "cdf": {"max_passes": 4, "coupling_rules": {"pdk/nch": [{"name": "w"}]}},
        },
    )
    raw = cfg_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert raw == json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    assert parsed["schema"] == CONFIG_SCHEMA_V2
    assert parsed["oa_libs"] == {"work": "oa/work"}
    assert parsed["cds_lib"] == "eda/cds.lib"
    assert parsed["allow_libraries"] == ["work", "analog"]
    assert parsed["daemon"]["idle_timeout_s"] == 45
    assert parsed["cdf"]["max_passes"] == 4

    with pytest.raises(SchemaValidationError, match="already exists"):
        write_project_config(tmp_path, {"backend": "fixture"}, force=False)

    write_project_config(tmp_path, {"backend": "fixture"}, force=True)
    assert load_project_config(tmp_path)["backend"] == "fixture"

    with pytest.raises(SchemaValidationError, match="sensitive"):
        write_project_config(tmp_path, {"password": "leak"}, force=True)


# ---------------------------------------------------------------------------
# 2. Dirty Index & Fingerprinting Tests
# ---------------------------------------------------------------------------


def test_fingerprint_source_files_fast_path_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib_root = tmp_path / "oa" / "work"
    sch = lib_root / "opamp" / "schematic" / "sch.oa"
    sch.parent.mkdir(parents=True)
    sch.write_bytes(b"OA_SCH_BYTES_V1")

    cv = CellViewRef("work", "opamp", "schematic")
    candidates = default_source_file_candidates(cv)
    fps1 = fingerprint_source_files(lib_root, candidates)
    assert len(fps1) == 3
    assert fps1[0].exists is True
    assert fps1[0].size == len(b"OA_SCH_BYTES_V1")
    assert fps1[0].sha256 == hashlib.sha256(b"OA_SCH_BYTES_V1").hexdigest()
    assert fps1[1].exists is False
    assert fps1[1].size == 0
    assert fps1[1].mtime_ns is None
    assert fps1[1].sha256 is None

    # Fast path must NOT call _sha256_file when exists/size/mtime_ns match
    def _boom(_path: Path, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("fast path unexpectedly hashed unchanged file")

    monkeypatch.setattr(project_state_mod, "_sha256_file", _boom)
    fps2 = fingerprint_source_files(lib_root, candidates, previous=fps1)
    assert fps2 == fps1


def test_classify_cellview_state_clean_mtime_dirty_missing_unknown(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "snapshots"
    lib_root = tmp_path / "oa" / "work"
    cv = CellViewRef("work", "opamp", "schematic")
    sch = lib_root / "opamp" / "schematic" / "sch.oa"
    sch.parent.mkdir(parents=True)
    sch.write_bytes(b"SCH_CONTENT_V1")

    snap = _make_sample_snapshot(cv)
    write_snapshot(snap, snapshot_dir_for(snapshots_dir, cv))

    cands = default_source_file_candidates(cv)
    fps = fingerprint_source_files(lib_root, cands)
    entry = IndexEntry(
        cellview=cv,
        source_files=fps,
        dependencies=("work/nch/symbol",),
        snapshot="snapshots/work/opamp/schematic",
        snapshot_full_sha256=snap.manifest.full_sha256,
        state="clean",
    )
    entries = {cv.key: entry}

    # Exact match -> clean
    state, _ = classify_cellview_state(
        cv, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state == "clean"

    # mtime-only change with identical SHA-256 -> STILL clean (zero Virtuoso launch!)
    st = sch.stat()
    os.utime(sch, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    state_mtime, fps_mtime = classify_cellview_state(
        cv, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state_mtime == "clean"
    assert fps_mtime[0].mtime_ns != fps[0].mtime_ns
    assert fps_mtime[0].sha256 == fps[0].sha256

    # Content change -> dirty
    sch.write_bytes(b"SCH_CONTENT_V2_MODIFIED")
    state_dirty, _ = classify_cellview_state(
        cv, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state_dirty == "dirty"

    # Restore original content, tamper snapshot -> dirty
    sch.write_bytes(b"SCH_CONTENT_V1")
    (snapshot_dir_for(snapshots_dir, cv) / "circuit.yaml").write_text("tampered: true\n", encoding="utf-8")
    state_tampered, _ = classify_cellview_state(
        cv, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state_tampered == "dirty"

    # Restore valid snapshot, delete primary OA file while snapshot exists -> unknown (never guess)
    write_snapshot(snap, snapshot_dir_for(snapshots_dir, cv))
    sch.unlink()
    state_unknown_missing_oa, _ = classify_cellview_state(
        cv, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state_unknown_missing_oa == "unknown"

    # Unknown OA lib -> unknown
    state_unknown_lib, _ = classify_cellview_state(
        cv, oa_libs={}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state_unknown_lib == "unknown"

    # Neither OA source nor snapshot -> missing
    cv_absent = CellViewRef("work", "nonexistent", "schematic")
    state_missing, _ = classify_cellview_state(
        cv_absent, oa_libs={"work": lib_root}, snapshots_dir=snapshots_dir, index_entries=entries
    )
    assert state_missing == "missing"


def test_index_flock_and_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flock_ops: list[int] = []
    real_flock = fcntl.flock

    def _spy_flock(fd: int, operation: int) -> None:
        flock_ops.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(project_state_mod.fcntl, "flock", _spy_flock)

    cv1 = CellViewRef("work", "c1", "schematic")
    cv2 = CellViewRef("work", "c2", "symbol")
    fp = SourceFileFingerprint("c1/schematic/sch.oa", False, 0, None, None)
    e1 = IndexEntry(cellview=cv1, source_files=(fp,), dependencies=("work/c2/symbol", "work/c2/symbol"))
    e2 = IndexEntry(cellview=cv2, source_files=())

    update_index_entry(tmp_path, e1)
    update_index_entry(tmp_path, e2)

    loaded = load_index(tmp_path)
    assert set(loaded) == {cv1.key, cv2.key}
    assert loaded[cv1.key].dependencies == ("work/c2/symbol",)

    raw = json.loads((tmp_path / ".oagit" / "index.json").read_text(encoding="utf-8"))
    assert raw["schema"] == INDEX_SCHEMA_V1
    assert fcntl.LOCK_EX in flock_ops
    assert fcntl.LOCK_SH in flock_ops
    assert not list((tmp_path / ".oagit").glob("*.tmp"))

    assert remove_index_entry(tmp_path, cv2.key) is True
    assert set(load_index(tmp_path)) == {cv1.key}
    save_index(tmp_path, loaded)
    assert set(load_index(tmp_path)) == {cv1.key, cv2.key}


# ---------------------------------------------------------------------------
# 3. Daemon ID & SSH ControlMaster Tests
# ---------------------------------------------------------------------------


def test_compute_daemon_id_and_skill_bundle_hash(tmp_path: Path) -> None:
    cds = tmp_path / "cds.lib"
    cds.write_text("DEFINE work ./work\n", encoding="utf-8")
    link = tmp_path / "cds_link.lib"
    link.symlink_to(cds)

    h1 = compute_skill_bundle_sha256(ROOT / "src" / "oa_git" / "skill")
    assert len(h1) == 64

    id1 = compute_daemon_id(str(cds), None, "virtuoso", "/tmp/oagit", h1)
    id2 = compute_daemon_id(str(link), None, "virtuoso", "/tmp/oagit", h1)
    id_ssh = compute_daemon_id("/eda/cds.lib", "eda-server", "virtuoso", "/tmp/oagit", h1)
    id_diff_skill = compute_daemon_id(str(cds), None, "virtuoso", "/tmp/oagit", "0" * 64)

    assert len(id1) == 16
    assert id1 == id2  # realpath canonicalization for local cds.lib
    assert id1 != id_ssh
    assert id1 != id_diff_skill


def test_ssh_mux_manager_args_lifecycle_and_no_ssh_config_touch() -> None:
    import shutil
    import tempfile

    short_root = Path(tempfile.mkdtemp(dir="/tmp", prefix="og_"))
    try:
        calls: list[list[str]] = []

        def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        mux = SshMuxManager(short_root, persist_s=60, runner=fake_runner)
        sock = mux.ensure_master("eda-server")

        assert len(str(sock).encode("utf-8")) <= 88
        assert sock.parent == short_root.resolve() / ".oagit" / "ssh"
        assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
        assert calls[0][:2] == [SSH_BIN, "-MNf"]
        assert "ControlMaster=auto" in calls[0]
        assert "ControlPersist=60" in calls[0]
        assert f"ControlPath={sock}" in calls[0]
        assert calls[0][-1] == "eda-server"

        ssh_args = mux.ssh_base_args("eda-server")
        scp_args = mux.scp_base_args("eda-server")
        assert ssh_args[0] == SSH_BIN
        assert scp_args[0] == SCP_BIN
        assert f"ControlPath={sock}" in ssh_args
        assert f"ControlPath={sock}" in scp_args

        # Create fake socket file and confirm stop_master exits and removes it
        sock.write_text("", encoding="utf-8")
        assert mux.stop_master("eda-server") is True
        assert not sock.exists()
        assert calls[-1][:3] == [SSH_BIN, "-O", "exit"]
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


def test_ssh_mux_manager_sun_path_fallback_when_over_88_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep_root = tmp_path / ("a" * 40) / ("b" * 40)
    deep_root.mkdir(parents=True)

    # Case 1: Short XDG_RUNTIME_DIR is used with 0700 permissions
    import shutil
    import tempfile

    fake_xdg = Path(tempfile.mkdtemp(dir="/tmp", prefix="ogx_"))
    try:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(fake_xdg))
        mux_xdg = SshMuxManager(deep_root)
        sock_xdg = mux_xdg.control_path("eda-server")
        assert len(str(sock_xdg).encode("utf-8")) <= 88
        assert sock_xdg.parent == fake_xdg / f"oagit-ssh-{os.getuid()}"
        assert stat.S_IMODE(sock_xdg.parent.stat().st_mode) == 0o700
    finally:
        shutil.rmtree(fake_xdg, ignore_errors=True)

    # Case 2: Unset XDG_RUNTIME_DIR falls back to /tmp/oagit-ssh-<uid>/<hash>.sock (0700)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    mux_tmp = SshMuxManager(deep_root)
    sock_tmp = mux_tmp.control_path("eda-server")
    assert len(str(sock_tmp).encode("utf-8")) <= 88
    assert sock_tmp.parent == Path(f"/tmp/oagit-ssh-{os.getuid()}")
    assert stat.S_IMODE(sock_tmp.parent.stat().st_mode) == 0o700


def test_remote_daemon_status_and_poll_fail_fast_on_dead_pid(tmp_path: Path) -> None:
    remote_pid_alive = [True]
    ssh_cmds: list[str] = []
    now = [0.0]

    def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == SCP_BIN:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN and "-O" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        shell = argv[-1]
        ssh_cmds.append(shell)
        if "cat" in shell and "daemon.json" in shell:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "protocol": SPOOL_PROTOCOL_V1,
                        "daemon_id": client.daemon_id,
                        "state": "ready",
                        "pid": 43210,
                    }
                ),
                "",
            )
        if shell.startswith("kill -0 43210"):
            return subprocess.CompletedProcess(argv, 0 if remote_pid_alive[0] else 1, "", "")
        if shell.startswith("mv "):
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "kill -0 43210" in shell and "exit 43" in shell:
            return subprocess.CompletedProcess(argv, 42 if remote_pid_alive[0] else 43, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib="/remote/cds.lib",
        ssh_host="eda-server",
        runner=fake_runner,
        sleep_fn=lambda dt: now.__setitem__(0, now[0] + dt),
        clock_fn=lambda: now[0],
        timeout_s=180,
    )
    client._write_local_state("ready", pid=43210)

    # Alive remote PID -> status() verifies via kill -0 and reports running=True
    st_alive = client.status()
    assert st_alive["running"] is True
    assert st_alive["state"] == "ready"
    assert any("kill -0 43210" in c for c in ssh_cmds)

    # Dead remote PID -> status() reports state="dead", running=False
    remote_pid_alive[0] = False
    st_dead = client.status()
    assert st_dead["running"] is False
    assert st_dead["state"] == "dead"

    # Remote spool poll fails fast on rc=43 (remote PID dead) without waiting 180s timeout!
    with pytest.raises(DaemonCrashError, match="pid=43210.*died before answering"):
        client.execute_request(
            "ping",
            'setq(oagitRequest \'(nil action "ping"))\n',
            request_id="rem-crash-1",
            timeout_s=180.0,
        )
    assert now[0] == 0.0  # Failed immediately on first poll!


def test_daemon_stop_cleans_ssh_mux_when_status_raises_daemon_error(tmp_path: Path) -> None:
    exit_calls: list[list[str]] = []

    def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == SSH_BIN and "-O" in argv and "check" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN and "-O" in argv and "exit" in argv:
            exit_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        # Simulate SSH transport failure during status() probe
        raise OSError("ssh connection broken")

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib="/remote/cds.lib",
        ssh_host="eda-server",
        runner=fake_runner,
    )
    client._write_local_state("ready", pid=9999)

    res = client.stop()
    assert res["stopped"] is True
    assert res["ssh_master_stopped"] is True
    assert len(exit_calls) == 1
    assert client.last_daemon_error is not None


def test_daemon_start_recomputes_daemon_id_on_cds_lib_change(tmp_path: Path) -> None:
    cds1 = tmp_path / "cds1.lib"
    cds2 = tmp_path / "cds2.lib"
    cds1.write_text("DEFINE a ./a\n", encoding="utf-8")
    cds2.write_text("DEFINE b ./b\n", encoding="utf-8")

    launches: list[tuple[str, list[str]]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> _FakeProc:
        proc = _FakeProc(pid=10000 + len(launches))
        launches.append((client.daemon_id, list(cmd)))
        client.local_state_file.write_text(
            json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "daemon_id": client.daemon_id,
                    "state": "ready",
                    "pid": proc.pid,
                }
            ),
            encoding="utf-8",
        )
        return proc

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds1,
        popen_factory=fake_popen,
        pid_alive_fn=lambda _pid: True,
    )
    id1 = client.daemon_id
    st1 = client.start()
    assert st1["daemon_id"] == id1
    assert st1["reused"] is False
    assert len(launches) == 1

    # Passing a different cds_lib must recompute daemon_id and launch a new daemon!
    st2 = client.start(cds_lib=cds2)
    assert client.daemon_id != id1
    assert st2["daemon_id"] == client.daemon_id
    assert st2["reused"] is False
    assert len(launches) == 2
    assert str(cds2) in launches[1][1]


# ---------------------------------------------------------------------------
# 4. Local Spool IPC, Idle/Crash Handling & Single Fallback Tests
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, pid: int = 424242, exit_code: int | None = None) -> None:
        self.pid = pid
        self._exit_code = exit_code

    def poll(self) -> int | None:
        return self._exit_code


def test_daemon_local_start_reuse_atomic_spool_and_stop(tmp_path: Path) -> None:
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    launches: list[list[str]] = []
    proc = _FakeProc(pid=55555)
    now = [100.0]

    def fake_popen(cmd: list[str], **_kwargs: Any) -> _FakeProc:
        launches.append(list(cmd))
        # Simulate daemon.il writing daemon.json ready state on startup
        state_file = client.local_state_file
        state_file.write_text(
            json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "daemon_id": client.daemon_id,
                    "state": "ready",
                    "pid": proc.pid,
                }
            ),
            encoding="utf-8",
        )
        return proc

    def fake_sleep(dt: float) -> None:
        now[0] += dt
        # Simulate daemon.il polling spool dir and answering *.req.il atomically
        for req_file in sorted(client.local_spool_dir.glob("*.req.il")):
            req_id = req_file.name[:-7]
            text = req_file.read_text(encoding="utf-8")
            req_file.unlink()
            resp_payload = {
                "protocol": SPOOL_PROTOCOL_V1,
                "request_id": req_id,
                "ok": True,
                "stopping": '"stop"' in text,
                "echo": True,
            }
            tmp_resp = client.local_spool_dir / f"{req_id}.resp.json.tmp"
            tmp_resp.write_text(json.dumps(resp_payload), encoding="utf-8")
            os.replace(tmp_resp, client.local_spool_dir / f"{req_id}.resp.json")
            if '"stop"' in text:
                proc._exit_code = 0

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds,
        popen_factory=fake_popen,
        sleep_fn=fake_sleep,
        clock_fn=lambda: now[0],
        pid_alive_fn=lambda pid: pid == proc.pid and proc.poll() is None,
    )

    st1 = client.start()
    assert st1["running"] is True
    assert st1["reused"] is False
    assert len(launches) == 1
    assert "-nograph" in launches[0] and "-nocdsinit" in launches[0]
    assert stat.S_IMODE(client.local_daemon_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(client.local_spool_dir.stat().st_mode) == 0o700

    # Second start reuses running daemon with zero extra launches
    st2 = client.start()
    assert st2["reused"] is True
    assert len(launches) == 1

    # Execute spool request
    resp = client.execute_request(
        "ping",
        'setq(oagitRequest \'(nil action "ping" request_id "req-1"))\n',
        request_id="req-1",
    )
    assert resp["ok"] is True
    assert resp["request_id"] == "req-1"
    assert not (client.local_spool_dir / "req-1.req.il.tmp").exists()

    # Duplicate request_id rejected
    with pytest.raises(DaemonCrashError, match="duplicate"):
        client.execute_request("ping", 'setq(oagitRequest \'(nil action "ping"))\n', request_id="req-1")

    # Stop transitions daemon cleanly
    stop_res = client.stop()
    assert stop_res["stopped"] is True
    assert stop_res["was_running"] is True
    assert client.status()["running"] is False


def test_daemon_spool_validation_errors_mismatch_malformed_timeout_dead(tmp_path: Path) -> None:
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    now = [0.0]
    proc = _FakeProc(pid=77777)
    mode = ["mismatch"]

    def fake_popen(cmd: list[str], **_kwargs: Any) -> _FakeProc:
        client.local_state_file.write_text(
            json.dumps({"protocol": SPOOL_PROTOCOL_V1, "daemon_id": client.daemon_id, "state": "ready", "pid": proc.pid}),
            encoding="utf-8",
        )
        return proc

    def fake_sleep(dt: float) -> None:
        now[0] += max(dt, 0.5)
        for req_file in sorted(client.local_spool_dir.glob("*.req.il")):
            req_id = req_file.name[:-7]
            req_file.unlink()
            resp_file = client.local_spool_dir / f"{req_id}.resp.json"
            if mode[0] == "mismatch":
                resp_file.write_text(
                    json.dumps({"protocol": SPOOL_PROTOCOL_V1, "request_id": "WRONG-ID", "ok": True}),
                    encoding="utf-8",
                )
            elif mode[0] == "bad_protocol":
                resp_file.write_text(
                    json.dumps({"protocol": "unknown/99", "request_id": req_id, "ok": True}),
                    encoding="utf-8",
                )
            elif mode[0] == "malformed":
                resp_file.write_text("{not valid json", encoding="utf-8")
            elif mode[0] == "crash":
                proc._exit_code = 139
            elif mode[0] == "timeout":
                pass

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds,
        popen_factory=fake_popen,
        sleep_fn=fake_sleep,
        clock_fn=lambda: now[0],
        pid_alive_fn=lambda pid: proc.poll() is None,
    )
    client.start()

    with pytest.raises(DaemonCrashError, match="request_id mismatch"):
        client.execute_request("ping", "setq(oagitRequest '(nil action \"ping\"))\n", request_id="r-mis")

    mode[0] = "bad_protocol"
    with pytest.raises(DaemonCrashError, match="protocol mismatch"):
        client.execute_request("ping", "setq(oagitRequest '(nil action \"ping\"))\n", request_id="r-proto")

    mode[0] = "malformed"
    with pytest.raises(DaemonCrashError, match="malformed JSON"):
        client.execute_request("ping", "setq(oagitRequest '(nil action \"ping\"))\n", request_id="r-badjson")

    mode[0] = "timeout"
    with pytest.raises(DaemonCrashError, match="timed out"):
        client.execute_request(
            "ping", "setq(oagitRequest '(nil action \"ping\"))\n", request_id="r-to", timeout_s=1.0
        )

    mode[0] = "crash"
    with pytest.raises(DaemonCrashError, match="died before answering"):
        client.execute_request("ping", "setq(oagitRequest '(nil action \"ping\"))\n", request_id="r-dead")


def test_daemon_execute_with_fallback_crashes_once_and_never_masquerades(tmp_path: Path) -> None:
    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")
    now = [0.0]
    proc = _FakeProc(pid=88888)
    fallback_calls = 0

    def fake_popen(cmd: list[str], **_kwargs: Any) -> _FakeProc:
        client.local_state_file.write_text(
            json.dumps({"protocol": SPOOL_PROTOCOL_V1, "daemon_id": client.daemon_id, "state": "ready", "pid": proc.pid}),
            encoding="utf-8",
        )
        return proc

    def crash_on_poll(dt: float) -> None:
        now[0] += dt
        proc._exit_code = 139  # Daemon crashes mid-request

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds,
        popen_factory=fake_popen,
        sleep_fn=crash_on_poll,
        clock_fn=lambda: now[0],
        pid_alive_fn=lambda pid: proc.poll() is None,
    )

    def one_shot() -> dict[str, Any]:
        nonlocal fallback_calls
        fallback_calls += 1
        return {
            "protocol": BATCH_PROTOCOL_V1,
            "request_id": "req-fb",
            "ok": True,
            "results": [],
            "discovered": [],
            "cycles": [],
            "errors": [],
            "session": {"daemon_id": None, "reused": False},
        }

    payload, transport = client.execute_with_fallback(
        "capture_batch",
        "setq(oagitRequest '(nil action \"capture_batch\"))\n",
        request_id="req-fb",
        one_shot_fn=one_shot,
    )
    assert transport == "one_shot_fallback"
    assert fallback_calls == 1
    assert client.last_daemon_error is not None
    assert "died before answering" in client.last_daemon_error
    assert payload["session"]["transport"] == "one_shot_fallback"
    assert payload["session"]["reused"] is False
    assert payload["session"]["daemon_error"] == client.last_daemon_error
    assert client.status()["state"] == "dead"

    # If one_shot_fn ALSO fails, original error propagates without a second retry
    fail_calls = 0

    def failing_one_shot() -> dict[str, Any]:
        nonlocal fail_calls
        fail_calls += 1
        raise RuntimeError("one-shot native failure")

    client2 = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds,
        popen_factory=lambda *a, **k: (_ for _ in ()).throw(OSError("virtuoso not found")),
    )
    with pytest.raises(RuntimeError, match="one-shot native failure"):
        client2.execute_with_fallback(
            "capture_batch",
            "setq(oagitRequest '(nil action \"capture_batch\"))\n",
            request_id="req-fb2",
            one_shot_fn=failing_one_shot,
        )
    assert fail_calls == 1


def test_daemon_driver_skill_is_balanced(tmp_path: Path) -> None:
    driver = build_daemon_driver_skill(
        daemon_id="0123456789abcdef",
        daemon_dir=tmp_path / "daemon",
        spool_dir=tmp_path / "daemon" / "spool",
        skill_dir=tmp_path / "daemon",
        idle_timeout_s=60,
        poll_ms=100,
    )
    _balanced_skill_parentheses(driver)
    assert "oagitDaemonConfig" in driver
    assert "oagitDaemonMain(oagitDaemonConfig)" in driver


# ---------------------------------------------------------------------------
# 5. SKILL Static Contract Checks for daemon.il
# ---------------------------------------------------------------------------


def test_daemon_skill_script_static_contracts() -> None:
    source = DAEMON_IL.read_text(encoding="utf-8")
    _balanced_skill_parentheses(source)

    for forbidden in (
        "schHi",
        "leHi",
        "geHi",
        "cdfGetParam",
        "cdfGetParameterList",
        "dbReplaceProp",
        "stringToChar",
        "cond((nil",
    ):
        assert forbidden not in source, f"daemon.il contains forbidden token {forbidden!r}"

    # All defined procedures must be prefixed with oagitDaemon*
    proc_names = re.findall(r"procedure\(([A-Za-z0-9_]+)\(", source)
    assert proc_names, "daemon.il must define procedures"
    for name in proc_names:
        assert name.startswith("oagitDaemon"), f"unexpected procedure prefix: {name}"

    for required in (
        "oagitDaemonMain",
        "oagitDaemonLoadScripts",
        "oagitDaemonAllowlistedActionP",
        "oagitDaemonSafePathP",
        "oagitDaemonWriteAtomicJson",
        "oagitDaemonCloseReadOnlyCellViews",
        "oagitDaemonProcessSpoolFile",
        "oagitDaemonPollSleep",
        "export_schematic.il",
        "restore_schematic.il",
        "export_symbol.il",
        "restore_symbol.il",
        "export_layout.il",
        "restore_layout.il",
        "batch_capture.il",
        "batch_restore.il",
        "oagit-spool/1",
        "\"stopping\"",
        "renameFile",
        "deleteFile",
        "dbGetOpenCellViews",
        "ddUpdateLibList",
        "exit(0)",
    ):
        assert required in source

    # Fix #3: pcreMatchp for '..' alternation, simplifyFilename(..., nil) for symlink resolution,
    # and renRes && car(renRes) after errset(renameFile ...)
    safe_path_proc = source[
        source.index("procedure(oagitDaemonSafePathP") : source.index(
            "procedure(oagitDaemonSafeNameP"
        )
    ]
    assert 'pcreMatchp("(^|/)\\\\.\\\\.(/|$)" targetPath)' in safe_path_proc
    assert 'rexMatchp("(^|/)\\\\.\\\\.(/|$)"' not in safe_path_proc
    assert "simplifyFilename(targetPath nil)" in safe_path_proc
    assert "simplifyFilename(rootDir nil)" in safe_path_proc
    assert "simplifyFilename(targetPath t)" not in safe_path_proc
    assert "simplifyFilename(rootDir t)" not in safe_path_proc

    proc_spool = source[
        source.index("procedure(oagitDaemonProcessSpoolFile") : source.index(
            "procedure(oagitDaemonListPendingRequests"
        )
    ]
    assert "renRes = errset(renameFile(reqPath procPath) t)" in proc_spool
    assert "when(renRes && car(renRes)" in proc_spool

    # Fix #7: oagitDaemonErrorPayload emits empty results/discovered/cycles/errors for batch actions
    err_proc = source[
        source.index("procedure(oagitDaemonErrorPayload") : source.index(
            "procedure(oagitDaemonCaptureSingleSchematic"
        )
    ]
    for batch_field in ('"results"', '"discovered"', '"cycles"', '"errors"'):
        assert batch_field in err_proc, f"oagitDaemonErrorPayload missing {batch_field}"

    # v0.3.0 hardening: idleTimeout == 0 disables auto-exit & idle backoff uses zero-fork ipcSleep(1)
    main_proc = source[source.index("procedure(oagitDaemonMain") :]
    assert "idleTimeout > 0 && (nowSec - lastActive) >= idleTimeout" in main_proc
    assert "idleTimeout >= 0" not in source
    assert "(nowSec - lastActive) >= 5" in main_proc
    assert "ipcSleep(1)" in main_proc


def test_remote_daemon_launch_command_detaches_without_subshell_pipe_hang(
    tmp_path: Path,
) -> None:
    """Live IC26.1 regression: launch command must use ';' (not '&& ... &'),
    detach with setsid when present, and use unique per-launch log files."""

    ssh_shells: list[str] = []
    launched = [False]

    def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == SCP_BIN:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN and "-O" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN and "-M" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        shell = argv[-1]
        ssh_shells.append(shell)
        if "nohup" in shell:
            launched[0] = True
            return subprocess.CompletedProcess(argv, 0, "55123\n", "")
        if "cat" in shell and "daemon.json" in shell:
            if not launched[0]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "protocol": SPOOL_PROTOCOL_V1,
                        "daemon_id": client.daemon_id,
                        "state": "ready",
                        "pid": 55123,
                    }
                ),
                "",
            )
        if shell.startswith("kill -0 55123"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib="/remote/cds.lib",
        ssh_host="eda-server",
        virtuoso_cmd="/opt/eda/bin/virtuoso",
        runner=fake_runner,
    )
    info = client.start()
    assert info["running"] is True
    assert info["pid"] == 55123

    launch_cmds = [s for s in ssh_shells if "nohup" in s]
    assert len(launch_cmds) == 1
    cmd = launch_cmds[0]
    assert "|| exit 1;" in cmd
    assert "&& nohup" not in cmd
    assert "command -v setsid" in cmd
    assert "nohup setsid env" in cmd
    assert "virtuoso-" in cmd and ".log" in cmd
    assert "launch-" in cmd and ".log" in cmd
    assert "</dev/null >/dev/null" not in cmd


def test_remote_daemon_fd_close_redirects_ssh_n_and_layout_skill_bundle_staging(
    tmp_path: Path,
) -> None:
    """Confirm remote launch closes inherited fds 3..9, passes ssh -n, and stages layout SKILL."""

    from oa_git.daemon import DAEMON_SKILL_SCRIPTS

    assert "export_layout.il" in DAEMON_SKILL_SCRIPTS
    assert "restore_layout.il" in DAEMON_SKILL_SCRIPTS

    ssh_argvs: list[list[str]] = []
    scp_uploads: list[str] = []
    launched = [False]

    def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == SCP_BIN:
            scp_uploads.append(argv[-2])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN:
            ssh_argvs.append(list(argv))
            if "-O" in argv or "-MNf" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            shell = argv[-1]
            if "nohup" in shell:
                launched[0] = True
                return subprocess.CompletedProcess(argv, 0, "61234\n", "")
            if "cat" in shell and "daemon.json" in shell:
                if not launched[0]:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "protocol": SPOOL_PROTOCOL_V1,
                            "daemon_id": client.daemon_id,
                            "state": "ready",
                            "pid": 61234,
                        }
                    ),
                    "",
                )
        return subprocess.CompletedProcess(argv, 0, "", "")

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib="/remote/cds.lib",
        ssh_host="eda-server",
        virtuoso_cmd="/opt/eda/bin/virtuoso",
        runner=fake_runner,
    )
    info = client.start()
    assert info["running"] is True

    # Verify layout SKILL scripts were staged locally and uploaded via SCP
    assert (client.local_daemon_dir / "export_layout.il").is_file()
    assert (client.local_daemon_dir / "restore_layout.il").is_file()
    uploaded_names = {Path(p).name for p in scp_uploads}
    assert "export_layout.il" in uploaded_names
    assert "restore_layout.il" in uploaded_names

    # Verify launch SSH invocation passes -n and closes fds 3..9
    launch_argvs = [a for a in ssh_argvs if "nohup" in a[-1]]
    assert len(launch_argvs) == 1
    launch_argv = launch_argvs[0]
    assert "-n" in launch_argv[:-1]
    launch_shell = launch_argv[-1]
    assert "3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-" in launch_shell
    assert launch_shell.count("3>&- 4>&- 5>&- 6>&- 7>&- 8>&- 9>&-") == 2


def test_default_idle_timeout_is_one_hour(tmp_path: Path) -> None:
    """Default daemon idle timeout across config, backend, client, and SKILL is 3600s (1h)."""
    from oa_git.project_state import DEFAULT_DAEMON_CONFIG
    from oa_git.virtuoso_backend import VirtuosoSkillBackend

    assert DEFAULT_DAEMON_CONFIG["idle_timeout_s"] == 3600
    backend = VirtuosoSkillBackend(daemon_enabled=True, project_root=tmp_path)
    assert backend.daemon_idle_timeout_s == 3600

    client = VirtuosoDaemonClient(project_root=tmp_path, cds_lib=tmp_path / "cds.lib")
    assert client.idle_timeout_s == 3600
    assert client.ssh_mux.persist_s == 3600

    driver = build_daemon_driver_skill(
        daemon_id="0123456789abcdef",
        daemon_dir=tmp_path / "d",
        spool_dir=tmp_path / "d" / "spool",
        skill_dir=tmp_path / "skill",
    )
    assert "idle_timeout_s 3600" in driver
    daemon_il = (Path(__file__).resolve().parents[1] / "src" / "oa_git" / "skill" / "daemon.il").read_text(encoding="utf-8")
    assert "config->idle_timeout_s 3600" in daemon_il


def test_ssh_mux_manager_stale_socket_unlink_keepalive_and_default_persist(
    tmp_path: Path,
) -> None:
    """SshMuxManager defaults to 3600s persist, passes ServerAlive keepalive, and unlinks stale sockets."""

    assert SshMuxManager(tmp_path).persist_s == DEFAULT_DAEMON_IDLE_TIMEOUT_S == 3600
    assert SshMuxManager(tmp_path, persist_s=120).persist_s == 120

    calls: list[list[str]] = []
    sock_exists_at_launch: list[bool] = []
    check_alive = [False]

    def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if "-O" in argv and "check" in argv:
            return subprocess.CompletedProcess(argv, 0 if check_alive[0] else 255, "", "")
        if "-MNf" in argv:
            sock_exists_at_launch.append(sock.exists())
            sock.write_text("active", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    mux = SshMuxManager(tmp_path, runner=fake_runner)
    sock = mux.control_path("eda-server")
    # Plant a stale socket file before ensure_master()
    sock.write_text("stale", encoding="utf-8")
    assert sock.exists()

    ensured = mux.ensure_master("eda-server")
    assert ensured == sock
    assert sock_exists_at_launch == [False]  # Stale socket unlinked BEFORE ssh -MNf!
    assert calls[0][:3] == [SSH_BIN, "-O", "check"]
    launch_argv = calls[1]
    assert launch_argv[:2] == [SSH_BIN, "-MNf"]
    assert "ControlPersist=3600" in launch_argv
    assert "ServerAliveInterval=30" in launch_argv
    assert "ServerAliveCountMax=3" in launch_argv

    ssh_args = mux.ssh_base_args("eda-server")
    assert "ServerAliveInterval=30" in ssh_args
    assert "ServerAliveCountMax=3" in ssh_args

    # When master is alive, ensure_master() reuses existing socket without relaunching
    check_alive[0] = True
    calls.clear()
    assert mux.ensure_master("eda-server") == sock
    assert len(calls) == 1
    assert calls[0][:3] == [SSH_BIN, "-O", "check"]


def test_daemon_start_idle_timeout_validation_and_ssh_mux_persist_sync(
    tmp_path: Path,
) -> None:
    """start(idle_timeout_s=...) rejects bool/negative values and syncs ssh_mux.persist_s."""

    cds = tmp_path / "cds.lib"
    cds.write_text("", encoding="utf-8")

    with pytest.raises(SchemaValidationError, match="idle_timeout_s"):
        VirtuosoDaemonClient(project_root=tmp_path, cds_lib=cds, idle_timeout_s=-5)
    with pytest.raises(SchemaValidationError, match="idle_timeout_s"):
        VirtuosoDaemonClient(project_root=tmp_path, cds_lib=cds, idle_timeout_s=True)  # type: ignore[arg-type]

    zero_client = VirtuosoDaemonClient(project_root=tmp_path, cds_lib=cds, idle_timeout_s=0)
    assert zero_client.ssh_mux.persist_s == DEFAULT_DAEMON_IDLE_TIMEOUT_S

    proc = _FakeProc(pid=66111)

    def fake_popen(_cmd: list[str], **_kwargs: Any) -> _FakeProc:
        client.local_state_file.write_text(
            json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "daemon_id": client.daemon_id,
                    "state": "ready",
                    "pid": proc.pid,
                }
            ),
            encoding="utf-8",
        )
        return proc

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib=cds,
        popen_factory=fake_popen,
        pid_alive_fn=lambda pid: pid == proc.pid and proc.poll() is None,
    )

    for bad_idle in (-1, True, False):
        with pytest.raises(SchemaValidationError, match="idle_timeout_s"):
            client.start(idle_timeout_s=bad_idle)  # type: ignore[arg-type]

    # idle_timeout_s=30 clamps ssh_mux.persist_s to 60
    st1 = client.start(idle_timeout_s=30)
    assert st1["running"] is True
    assert client.ssh_mux.persist_s == 60

    # idle_timeout_s=7200 syncs ssh_mux.persist_s to 7200
    st2 = client.start(idle_timeout_s=7200)
    assert st2["reused"] is True
    assert client.ssh_mux.persist_s == 7200

    # idle_timeout_s=0 disables auto-exit and sets ssh_mux.persist_s to 3600
    proc._exit_code = 0
    proc2 = _FakeProc(pid=66112)
    client.popen_factory = lambda _cmd, **_kw: (
        client.local_state_file.write_text(
            json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "daemon_id": client.daemon_id,
                    "state": "ready",
                    "pid": proc2.pid,
                }
            ),
            encoding="utf-8",
        ),
        proc2,
    )[1]
    client.pid_alive_fn = lambda pid: pid == proc2.pid and proc2.poll() is None
    st3 = client.start(idle_timeout_s=0)
    assert st3["running"] is True
    assert st3["reused"] is False
    assert client.ssh_mux.persist_s == DEFAULT_DAEMON_IDLE_TIMEOUT_S
    driver_text = (client.local_daemon_dir / "daemon_driver.il").read_text(encoding="utf-8")
    assert "idle_timeout_s 0" in driver_text


def test_remote_daemon_mux_drop_orphan_reuse_stop_and_heartbeat_staleness(
    tmp_path: Path,
) -> None:
    """After SSH mux drop, _start_remote reuses and stop() terminates orphaned remote daemons;
    remote/local status() captures heartbeat and detects stale epoch timestamps."""

    now_epoch = [1_700_000_100.0]
    mux_alive = [False]
    remote_alive = [True]
    remote_hb = [f"{int(now_epoch[0]) - 5} dummy"]
    ssh_shells: list[str] = []
    scp_uploads: list[str] = []
    stop_requests_seen: list[str] = []

    def fake_runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == SCP_BIN:
            scp_uploads.append(argv[-2])
            if argv[-1].endswith(".req.il.tmp'") or argv[-1].endswith(".req.il.tmp"):
                stop_requests_seen.append(Path(argv[-2]).read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN and "-O" in argv and "check" in argv:
            return subprocess.CompletedProcess(argv, 0 if mux_alive[0] else 255, "", "")
        if argv[0] == SSH_BIN and "-MNf" in argv:
            mux_alive[0] = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == SSH_BIN and "-O" in argv and "exit" in argv:
            mux_alive[0] = False
            return subprocess.CompletedProcess(argv, 0, "", "")

        shell = argv[-1]
        ssh_shells.append(shell)
        if "__OAGIT_HEARTBEAT_END__" in shell:
            state_json = json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "daemon_id": client.daemon_id,
                    "state": "ready",
                    "pid": 77701,
                }
            )
            pid_flag = "alive" if remote_alive[0] else "dead"
            out = (
                f"{state_json}\n__OAGIT_STATE_END__\n"
                f"{remote_hb[0]}\n__OAGIT_HEARTBEAT_END__\n{pid_flag}\n"
            )
            return subprocess.CompletedProcess(argv, 0, out, "")
        if "__OAGIT_STATE_END__" in shell:
            state_json = json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "daemon_id": client.daemon_id,
                    "state": "ready",
                    "pid": 77701,
                }
            )
            out = f"{state_json}\n__OAGIT_STATE_END__\n{remote_hb[0]}\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if shell.startswith("kill -0 77701"):
            return subprocess.CompletedProcess(argv, 0 if remote_alive[0] else 1, "", "")
        if shell.startswith("mv "):
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "exit 42" in shell:
            # Extract request_id from remote response path and reply to stop request
            match = re.search(r"/(stop-[A-Za-z0-9_.-]+)\.resp\.json", shell)
            assert match is not None
            req_id = match.group(1)
            remote_alive[0] = False
            resp = json.dumps(
                {
                    "protocol": SPOOL_PROTOCOL_V1,
                    "request_id": req_id,
                    "ok": True,
                    "stopping": True,
                    "daemon_id": client.daemon_id,
                }
            )
            return subprocess.CompletedProcess(argv, 0, resp, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    client = VirtuosoDaemonClient(
        project_root=tmp_path,
        cds_lib="/remote/cds.lib",
        ssh_host="eda-server",
        runner=fake_runner,
        clock_fn=lambda: now_epoch[0],
        sleep_fn=lambda dt: now_epoch.__setitem__(0, now_epoch[0] + dt),
    )
    remote_hb[0] = f"{int(now_epoch[0]) - 5} {client.daemon_id}"

    # Simulate local state present from earlier session, but SSH ControlMaster has dropped
    client._write_local_state("ready", pid=77701)
    assert client.status()["running"] is False

    # 1. start() re-establishes mux, probes remote live daemon, and reuses WITHOUT launching!
    reused_status = client.start()
    assert reused_status["running"] is True
    assert reused_status["reused"] is True
    assert reused_status["pid"] == 77701
    assert reused_status["heartbeat"] == remote_hb[0]
    assert not any("nohup" in s for s in ssh_shells)
    assert not any("rm -f" in s and "daemon.json" in s for s in ssh_shells)
    assert scp_uploads == []

    # 2. Stale remote heartbeat (> 120s old) causes status() to report dead
    remote_hb[0] = f"{int(now_epoch[0]) - 300} {client.daemon_id}"
    stale_st = client.status()
    assert stale_st["running"] is False
    assert stale_st["state"] == "dead"
    assert stale_st["heartbeat"] == remote_hb[0]

    # 3. Restore fresh heartbeat, drop mux again -> stop() recovers mux and stops orphan!
    remote_hb[0] = f"{int(now_epoch[0]) - 2} {client.daemon_id}"
    mux_alive[0] = False
    assert client.status()["running"] is False

    stop_res = client.stop()
    assert stop_res["stopped"] is True
    assert stop_res["was_running"] is True
    assert stop_res["stop_response"] is not None
    assert stop_res["stop_response"]["stopping"] is True
    assert stop_res["ssh_master_stopped"] is True
    assert len(stop_requests_seen) == 1
    assert '"stop"' in stop_requests_seen[0]
    assert client._read_local_state()["state"] == "stopped"  # type: ignore[index]

    # 4. Local mode heartbeat staleness check vs synthetic monotonic clock
    local_cds = tmp_path / "local_cds.lib"
    local_cds.write_text("", encoding="utf-8")
    local_client = VirtuosoDaemonClient(
        project_root=tmp_path / "local_proj",
        cds_lib=local_cds,
        clock_fn=lambda: now_epoch[0],
        pid_alive_fn=lambda _pid: True,
    )
    local_client._write_local_state("ready", pid=88111)
    local_client.local_heartbeat_file.write_text(
        f"{int(now_epoch[0]) - 200} {local_client.daemon_id}\n",
        encoding="utf-8",
    )
    local_stale = local_client.status()
    assert local_stale["running"] is False
    assert local_stale["state"] == "dead"

    # Synthetic clock starting at 0.0 does NOT spuriously mark epoch heartbeat as stale
    synth_client = VirtuosoDaemonClient(
        project_root=tmp_path / "local_proj",
        cds_lib=local_cds,
        clock_fn=lambda: 0.0,
        pid_alive_fn=lambda _pid: True,
    )
    assert synth_client.status()["running"] is True
