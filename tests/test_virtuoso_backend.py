from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import shlex
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from oa_git.bridge import load_backend, raw_capture_to_snapshot
from oa_git.model import (
    BridgeExecutionError,
    CdfParameterError,
    CellViewRef,
    DisallowedLibraryError,
    LiveLockError,
    RoundtripMismatchError,
    SchematicSnapshot,
    UnsupportedObjectError,
)
from oa_git.virtuoso_backend import VirtuosoSkillBackend, build_driver_skill


SOURCE = CellViewRef("work", "amp", "schematic")
TARGET = CellViewRef("sandbox", "amp", "schematic")


def capture_payload(cellview: CellViewRef = SOURCE) -> dict[str, object]:
    return {
        "ok": True,
        "error": None,
        "cellview": cellview.to_dict(),
        "modified_but_not_saved": False,
        "metadata": {"cellview_type": "schematic", "dbu_per_uu": 160.0},
        "ports": [{"name": "in", "direction": "input"}],
        "nets": [{"name": "in", "is_global": False, "num_bits": 1}],
        "instances": [
            {
                "name": "R1",
                "lib": "basic",
                "cell": "res",
                "view": "symbol",
                "xy": [1.0, 2.0],
                "orient": "R0",
                "connections": {"p": "in"},
                "params": {"r": "2k"},
                "param_types": {"r": "string"},
                "derived_params": {},
                "props": {},
            }
        ],
        "pins": [
            {
                "name": "in",
                "net": "in",
                "direction": "input",
                "lib": "basic",
                "cell": "ipin",
                "view": "symbol",
                "xy": [0.0, 2.0],
                "orient": "R0",
            }
        ],
        "wires": [
            {
                "points": [[0.0, 2.0], [1.0, 2.0]],
                "net": "in",
                "width": 0.0,
                "route_method": "full",
                "labels": [],
            }
        ],
        "labels": [],
        "properties": {},
        "constraints": {},
        "sheet": {},
        "omissions": [],
    }


def restore_payload(
    *,
    target: CellViewRef = TARGET,
    error_code: str | None = None,
) -> dict[str, object]:
    if error_code is not None:
        return {
            "ok": False,
            "error": "native restore failed",
            "error_code": error_code,
        }
    return {
        "ok": True,
        "target": target.to_dict(),
        "published": True,
        "draft_cell": "amp__oagit_draft_0123456789",
        "sch_check": {"errors": 0, "warnings": 1},
        "cdf_verified": True,
        "connectivity_mismatches": [],
    }


def make_snapshot() -> SchematicSnapshot:
    return raw_capture_to_snapshot(capture_payload())


class LocalRunner:
    def __init__(self, responses: list[object]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        response = self.responses.popleft() if self.responses else None
        if response == "timeout":
            raise TimeoutExpired(command, kwargs.get("timeout", 0), stderr="timeout stderr")
        if response == "missing":
            Path(str(kwargs["cwd"]), "virtuoso.log").write_text(
                "native log tail", encoding="utf-8"
            )
        elif response == "malformed":
            Path(str(kwargs["cwd"]), "response.json").write_text(
                "{malformed", encoding="utf-8"
            )
        elif isinstance(response, dict):
            Path(str(kwargs["cwd"]), "response.json").write_text(
                json.dumps(response), encoding="utf-8"
            )
        return CompletedProcess(command, 0, "", "")


class RemoteRunner:
    """In-memory SSH/scp transport used to inspect the generated commands."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.remote_files: dict[str, bytes] = {}
        self.remote_dirs: set[str] = set()

    @staticmethod
    def _remote_dir(command: str) -> str:
        tokens = shlex.split(command)
        value = next(token for token in tokens if token.startswith("IC261_WORKDIR="))
        return value.split("=", 1)[1]

    def __call__(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if command[0] == "/usr/bin/ssh":
            shell = command[2]
            if shell.startswith("mkdir -p"):
                self.remote_dirs.add(shell.split()[2].strip("'"))
            elif shell.startswith("rm -rf"):
                self.remote_dirs.clear()
            elif "env ENABLE_SOS705=0" in shell:
                remote_dir = self._remote_dir(shell)
                response = self.responses.popleft()
                if isinstance(response, dict):
                    self.remote_files[f"{remote_dir}/response.json"] = json.dumps(
                        response
                    ).encode()
                self.remote_files[f"{remote_dir}/virtuoso.log"] = b"remote log tail"
            return CompletedProcess(command, 0, "", "")
        if command[0] == "/usr/bin/scp":
            source, destination = command[1:3]
            if ":" in source:
                remote_source = source.split(":", 1)[1]
                remote_source = shlex.split(remote_source)[0]
                Path(destination).write_bytes(self.remote_files[remote_source])
            return CompletedProcess(command, 0, "", "")
        response = self.responses.popleft()
        if isinstance(response, dict):
            Path(str(kwargs["cwd"]), "response.json").write_text(
                json.dumps(response), encoding="utf-8"
            )
        return CompletedProcess(command, 0, "", "")


def test_build_driver_skill_loads_files_and_has_fallback() -> None:
    rendered = build_driver_skill(
        "/tmp/oa git/common.il",
        "/tmp/oa git/request.il",
        "/tmp/oa git/export.il",
        "/tmp/oa git/response.json",
    )

    assert "errset" in rendered
    assert 'load("/tmp/oa git/common.il")' in rendered
    assert 'load("/tmp/oa git/request.il")' in rendered
    assert 'load("/tmp/oa git/export.il")' in rendered
    assert 'outfile("/tmp/oa git/response.json" "w")' in rendered
    assert "fprintf(oagitFallbackPort" in rendered
    assert "oagitFail" not in rendered
    assert "isFile" in rendered
    assert rendered.rstrip().endswith("exit(0)")


def test_local_capture_restore_and_readback_happy_path(tmp_path: Path) -> None:
    source_snapshot = make_snapshot()
    runner = LocalRunner(
        [
            capture_payload(SOURCE),
            restore_payload(),
            capture_payload(TARGET),
        ]
    )
    backend = VirtuosoSkillBackend(runner=runner)
    cds_lib = tmp_path / "cds.lib"

    captured = backend.capture_schematic(SOURCE, cds_lib=cds_lib)
    receipt = backend.restore_schematic(
        source_snapshot,
        TARGET,
        cds_lib=cds_lib,
        allow_libraries=("sandbox",),
    )

    assert captured.manifest.cellview == SOURCE
    assert receipt.target == TARGET
    assert receipt.published is True
    assert receipt.readback_verified is True
    assert len(runner.calls) == 3
    command, kwargs = runner.calls[0]
    assert command[1:3] == ["-nograph", "-nocdsinit"]
    assert command[command.index("-cdslib") + 1] == str(cds_lib)
    assert command[command.index("-restore")] == "-restore"
    assert kwargs["env"]["ENABLE_SOS705"] == "0"
    assert kwargs["env"]["OAGIT_AUTO_EXIT"] == "1"
    assert kwargs["env"]["TMPDIR"].endswith("/tmp")


def test_capture_and_restore_require_cds_lib() -> None:
    backend = VirtuosoSkillBackend(runner=LocalRunner([]))
    with pytest.raises(BridgeExecutionError):
        backend.capture_schematic(SOURCE)
    with pytest.raises(BridgeExecutionError):
        backend.restore_schematic(
            make_snapshot(),
            TARGET,
            allow_libraries=("sandbox",),
            verify_readback=False,
        )


def test_remote_command_transport_and_cleanup(tmp_path: Path) -> None:
    runner = RemoteRunner([capture_payload(SOURCE)])
    backend = VirtuosoSkillBackend(
        ssh_host="eda-server",
        remote_workdir="/tmp/oagit runs",
        virtuoso_cmd="/opt/cadence bin/virtuoso261",
        runner=runner,
    )
    backend.capture_schematic(
        SOURCE,
        cds_lib=Path("/tmp/cds lib/cds.lib"),
    )

    ssh_calls = [call for call in runner.calls if call[0][0] == "/usr/bin/ssh"]
    scp_calls = [call for call in runner.calls if call[0][0] == "/usr/bin/scp"]
    assert ssh_calls and scp_calls
    execution = next(call for call in ssh_calls if "env ENABLE_SOS705=0" in call[0][2])
    shell = execution[0][2]
    assert "ENABLE_SOS705=0" in shell
    assert "OAGIT_AUTO_EXIT=1" in shell
    assert "-nograph" in shell and "-nocdsinit" in shell
    assert "-cdslib '/tmp/cds lib/cds.lib'" in shell
    assert shlex.quote("/opt/cadence bin/virtuoso261") in shell
    assert any("rm -rf --" in call[0][2] for call in ssh_calls)
    assert all(call[0][0] in {"/usr/bin/ssh", "/usr/bin/scp"} for call in runner.calls)


def test_remote_cds_lib_accepts_remote_string_without_local_path_check() -> None:
    runner = RemoteRunner([capture_payload(SOURCE)])
    backend = VirtuosoSkillBackend(
        ssh_host="eda-server",
        remote_workdir="/tmp/oagit runs",
        runner=runner,
    )
    backend.capture_schematic(SOURCE, cds_lib="/remote/eda/cds.lib")

    execution = next(
        call for call in runner.calls
        if call[0][0] == "/usr/bin/ssh" and "env ENABLE_SOS705=0" in call[0][2]
    )
    assert "-cdslib /remote/eda/cds.lib" in execution[0][2]


def test_custom_replay_flag_is_preserved() -> None:
    backend = VirtuosoSkillBackend(replay_flag="-replay", runner=LocalRunner([]))
    assert backend.replay_flag == "-replay"


@pytest.mark.parametrize(
    ("error_code", "error_type"),
    [
        ("cdf_parameter_error", CdfParameterError),
        ("live_lock", LiveLockError),
        ("sch_check_failed", RoundtripMismatchError),
    ],
)
def test_skill_error_code_propagation(
    error_code: str, error_type: type[Exception], tmp_path: Path
) -> None:
    backend = VirtuosoSkillBackend(
        runner=LocalRunner([restore_payload(error_code=error_code)])
    )
    with pytest.raises(error_type):
        backend.restore_schematic(
            make_snapshot(),
            TARGET,
            cds_lib=tmp_path / "cds.lib",
            allow_libraries=("sandbox",),
            verify_readback=False,
        )


def test_python_guards_short_circuit_before_runner(tmp_path: Path) -> None:
    class ShouldNotRun:
        def __call__(self, *_args: object, **_kwargs: object) -> CompletedProcess[str]:
            raise AssertionError("Virtuoso must not start when a guard fails")

    backend = VirtuosoSkillBackend(runner=ShouldNotRun())
    snapshot = make_snapshot()
    with pytest.raises(DisallowedLibraryError):
        backend.restore_schematic(
            snapshot,
            TARGET,
            cds_lib=tmp_path / "cds.lib",
            allow_libraries=(),
        )

    omitted = raw_capture_to_snapshot(
        {**capture_payload(), "omissions": [{"kind": "shape", "identifier": "x", "reason": "unsupported", "details": {}}]}
    )
    with pytest.raises(UnsupportedObjectError):
        backend.restore_schematic(
            omitted,
            TARGET,
            cds_lib=tmp_path / "cds.lib",
            allow_libraries=("sandbox",),
        )

    lock_dir = tmp_path / "view"
    lock_dir.mkdir()
    (lock_dir / "edit.cdslck").write_text("lock", encoding="utf-8")
    with pytest.raises(LiveLockError):
        backend.restore_schematic(
            snapshot,
            TARGET,
            cds_lib=tmp_path / "cds.lib",
            allow_libraries=("sandbox",),
            oa_view_dir=lock_dir,
        )


def test_readback_drift_raises_roundtrip_mismatch(tmp_path: Path) -> None:
    drift = capture_payload(TARGET)
    drift["instances"] = [
        {
            **drift["instances"][0],  # type: ignore[index]
            "params": {"r": "3k"},
        }
    ]
    backend = VirtuosoSkillBackend(
        runner=LocalRunner([restore_payload(), drift])
    )
    with pytest.raises(RoundtripMismatchError):
        backend.restore_schematic(
            make_snapshot(),
            TARGET,
            cds_lib=tmp_path / "cds.lib",
            allow_libraries=("sandbox",),
        )


@pytest.mark.parametrize("mode", ["timeout", "missing", "malformed"])
def test_timeout_missing_and_malformed_response_fail_closed(
    mode: str, tmp_path: Path
) -> None:
    backend = VirtuosoSkillBackend(runner=LocalRunner([mode]), timeout_s=1)
    with pytest.raises(BridgeExecutionError):
        backend.capture_schematic(SOURCE, cds_lib=tmp_path / "cds.lib")


def test_load_backend_constructs_virtuoso_backend() -> None:
    backend = load_backend(
        "virtuoso",
        ssh_host="eda-server",
        virtuoso_cmd="/opt/eda/bin/virtuoso",
    )
    assert isinstance(backend, VirtuosoSkillBackend)
    assert backend.ssh_host == "eda-server"
    assert backend.virtuoso_cmd == "/opt/eda/bin/virtuoso"
