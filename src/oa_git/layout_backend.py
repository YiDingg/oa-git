"""Backend seam, SKILL DPL-JSON codec, roundtrip verifier, and ViewHandler for layouts.

This module implements the Track 2 Layout backend and ``view_contract.ViewHandler``
integration without modifying any Track 1 shared files:

- :class:`LayoutHandler`: conforms 100% to ``oa_git.view_contract.ViewHandler``.
- :func:`ensure_no_layout_omissions` & :func:`verify_layout_roundtrip`: Fail-Closed
  safety guards and full physical/connectivity/PCell roundtrip verification.
- DPL-JSON wire codec matching ``export_layout.il`` and ``restore_layout.il``.
- :class:`FixtureLayoutBackend`: deterministic offline backend for tests and CI.
- :class:`VirtuosoLayoutBackend`: isolated local/SSH Virtuoso runner with optional
  ``SkillRequestRunner`` delegation and 1-time transient SIGSEGV crash retry.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
import inspect
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
import uuid

from .bridge import to_skill_literal
from .guards import (
    ensure_library_allowed,
    ensure_no_live_locks,
    parse_engineering_number,
    probe_locks,
)
from .layout_model import (
    BBox,
    BridgeExecutionError,
    CdfParameterError,
    CellViewRef,
    DisallowedLibraryError,
    FailClosedError,
    LAYOUT_VIEW_TYPE,
    LayoutBlockage,
    LayoutBoundary,
    LayoutEllipse,
    LayoutFigGroup,
    LayoutInstance,
    LayoutLabel,
    LayoutLine,
    LayoutManifest,
    LayoutMosaic,
    LayoutNet,
    LayoutObject,
    LayoutPath,
    LayoutPathSeg,
    LayoutPin,
    LayoutPolygon,
    LayoutRect,
    LayoutRestoreReceipt,
    LayoutSnapshot,
    LayoutVia,
    LiveLockError,
    OmissionRecord,
    ParamValue,
    Point2D,
    RoundtripMismatchError,
    SchemaValidationError,
    UnsupportedObjectError,
)
from .layout_snapshot import (
    build_layout_snapshot,
    layout_object_from_dict,
    read_layout_snapshot,
    write_layout_snapshot,
)
from .view_contract import register_view_handler
from .virtuoso_backend import build_driver_skill

if TYPE_CHECKING:
    from .layout_diff import LayoutDiff
    from .layout_merge import LayoutMergeResult


Runner = Callable[..., subprocess.CompletedProcess[str]]

_SKILL_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_?$%*!<>~=:+\-./]*$")

_LAYOUT_CAPTURE_KEYS = frozenset(
    {
        "ok",
        "error",
        "error_code",
        "cellview",
        "modified_but_not_saved",
        "metadata",
        "objects",
        "properties",
        "omissions",
    }
)

_LAYOUT_RESTORE_KEYS = frozenset(
    {
        "ok",
        "error",
        "error_code",
        "target",
        "published",
        "draft_cell",
        "pcell_verified",
        "connectivity_verified",
        "readback_verified",
        "object_counts",
        "pcell_mismatches",
        "connectivity_mismatches",
        "details",
    }
)

_LAYOUT_RESTORE_ERROR_TYPES: dict[str, type[FailClosedError]] = {
    "disallowed_library": DisallowedLibraryError,
    "live_lock": LiveLockError,
    "unsupported_object": UnsupportedObjectError,
    "pcell_parameter_error": CdfParameterError,
    "pcell_param_error": CdfParameterError,
    "pcell_param_mismatch": CdfParameterError,
    "cdf_parameter_error": CdfParameterError,
    "connectivity_mismatch": RoundtripMismatchError,
    "object_count_mismatch": RoundtripMismatchError,
}

_TRANSIENT_CRASH_CODES = frozenset({139, -11, 134, -6})
_TRANSIENT_CRASH_PATTERNS = (
    "corrupted double-linked list",
    "segmentation fault",
    "sigsegv",
)


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _schema_error(path: str, message: str) -> SchemaValidationError:
    return SchemaValidationError(f"{path}: {message}")


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _schema_error(path, "must be a mapping")
    if any(type(key) is not str for key in value):
        raise _schema_error(path, "keys must be strings")
    return value


def _require_sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise _schema_error(path, "must be a sequence")
    return value


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise _schema_error(path, "must be a bool")
    return value


def _require_text(value: Any, path: str, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value.strip()):
        raise _schema_error(
            path, "must be a non-empty string" if nonempty else "must be a string"
        )
    return value


def _require_int(value: Any, path: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _schema_error(path, f"must be an int >= {minimum}")
    return value


def _cellview_from_payload(value: Any, path: str) -> CellViewRef:
    mapping = _require_mapping(value, path)
    expected = {"library", "cell", "view"}
    if set(mapping) != expected:
        raise _schema_error(path, f"must contain exactly {sorted(expected)!r}")
    return CellViewRef(
        library=_require_text(mapping["library"], f"{path}.library"),
        cell=_require_text(mapping["cell"], f"{path}.cell"),
        view=_require_text(mapping["view"], f"{path}.view"),
    )


def _normalize_output_path(path: Path, field: str) -> str:
    if not isinstance(path, Path):
        raise _schema_error(field, "must be a pathlib.Path")
    raw = str(path)
    if not raw.strip() or "\x00" in raw:
        raise _schema_error(field, "must be a valid non-empty path")
    if path.is_absolute():
        return raw
    try:
        return str(path.expanduser().resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise _schema_error(field, f"cannot resolve path: {exc}") from exc


def _plain_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_value(item) for item in value]
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    return value


def _diagnostic_value(value: Any, *, limit: int = 160) -> str:
    rendered = repr(_plain_value(value))
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


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


def _normalise_structured_error(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten structured ``error: {code, message}`` payloads emitted by SKILL scripts."""

    if payload.get("ok") is not False or not isinstance(payload.get("error"), Mapping):
        return dict(payload)
    data = dict(payload)
    error = payload["error"]
    if not data.get("error_code") and isinstance(error.get("code"), str):
        data["error_code"] = error["code"]
    message = error.get("message")
    data["error"] = str(message) if message is not None else str(error)
    return data


# ---------------------------------------------------------------------------
# 1. ViewHandler Implementation (conforms 100% to oa_git.view_contract.ViewHandler)
# ---------------------------------------------------------------------------


class LayoutHandler:
    """ViewHandler plugin for OpenAccess layouts (``view_type="layout"``)."""

    view_type: Literal["layout"] = LAYOUT_VIEW_TYPE
    capture_action: str = "capture_layout"
    restore_action: str = "restore_layout"

    def source_file_candidates(self, cell: str, view: str) -> tuple[str, ...]:
        """Return relative paths under the OA library root for dirty-index checks."""

        return (f"{cell}/{view}/layout.oa", f"{cell}/{view}/master.tag")

    def read_snapshot(self, snapshot_dir: Path, *, verify: bool = True) -> LayoutSnapshot:
        return read_layout_snapshot(snapshot_dir, verify=verify)

    def write_snapshot(self, snapshot: LayoutSnapshot, dest_dir: Path) -> Path:
        return write_layout_snapshot(snapshot, dest_dir)

    def diff(self, old: LayoutSnapshot, new: LayoutSnapshot) -> LayoutDiff:
        from .layout_diff import diff_layouts

        return diff_layouts(old, new)

    def merge(
        self,
        base: LayoutSnapshot,
        ours: LayoutSnapshot,
        theirs: LayoutSnapshot,
    ) -> LayoutMergeResult:
        from .layout_merge import merge_layouts

        return merge_layouts(base, ours, theirs)


register_view_handler("layout", LayoutHandler())


# ---------------------------------------------------------------------------
# 2. Safety Guards & Roundtrip Verification
# ---------------------------------------------------------------------------


def ensure_no_layout_omissions(
    snapshot: LayoutSnapshot, *, allow_omissions: bool = False
) -> None:
    """Reject layout snapshots containing omissions unless explicitly allowed."""

    if not isinstance(snapshot, LayoutSnapshot):
        raise SchemaValidationError("snapshot must be a LayoutSnapshot")
    if type(allow_omissions) is not bool:
        raise UnsupportedObjectError("allow_omissions must be a bool")
    omissions = tuple(snapshot.omissions)
    omissions_count = snapshot.manifest.omissions_count
    if type(omissions_count) is not int or omissions_count < 0:
        raise UnsupportedObjectError("manifest.omissions_count must be a non-negative int")
    if allow_omissions or (not omissions and omissions_count <= 0):
        return

    details: list[str] = []
    if omissions:
        details.extend(
            f"{record.kind}:{record.identifier} ({record.reason})" for record in omissions
        )
    if omissions_count > len(omissions):
        details.append(f"manifest.omissions_count={omissions_count}")
    detail_text = "; ".join(details) or f"manifest.omissions_count={omissions_count}"
    raise UnsupportedObjectError(f"Layout snapshot contains omissions: {detail_text}")


@dataclass(frozen=True)
class LayoutRoundtripReport:
    """Structured diagnostic result of :func:`verify_layout_roundtrip`."""

    ok: bool
    mismatches: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mismatches": [dict(item) for item in self.mismatches],
        }


def _strict_equal(expected: Any, actual: Any) -> bool:
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, Mapping):
        if set(expected) != set(actual):
            return False
        return all(_strict_equal(expected[k], actual[k]) for k in expected)
    if isinstance(expected, (tuple, list)):
        if len(expected) != len(actual):
            return False
        return all(_strict_equal(left, right) for left, right in zip(expected, actual))
    try:
        return bool(expected == actual)
    except (TypeError, ValueError):
        return False


def _coord_close(a: float, b: float, tol: float) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tol)
    except (TypeError, ValueError, OverflowError):
        return False


def _pt_close(p1: Point2D, p2: Point2D, tol: float) -> bool:
    return (
        len(p1) == 2
        and len(p2) == 2
        and _coord_close(p1[0], p2[0], tol)
        and _coord_close(p1[1], p2[1], tol)
    )


def _bbox_close(b1: BBox | None, b2: BBox | None, tol: float) -> bool:
    if b1 is None and b2 is None:
        return True
    if b1 is None or b2 is None:
        return False
    return _pt_close(b1[0], b2[0], tol) and _pt_close(b1[1], b2[1], tol)


def _polyline_close(
    pts1: Sequence[Point2D], pts2: Sequence[Point2D], tol: float
) -> bool:
    return len(pts1) == len(pts2) and all(
        _pt_close(p1, p2, tol) for p1, p2 in zip(pts1, pts2)
    )


def _ring_close(pts1: Sequence[Point2D], pts2: Sequence[Point2D], tol: float) -> bool:
    n = len(pts1)
    if n != len(pts2):
        return False
    if n == 0:
        return True
    if all(_pt_close(p1, p2, tol) for p1, p2 in zip(pts1, pts2)):
        return True
    # Allow cyclic start-vertex shift when near-tied vertices swap after float jitter.
    first = pts1[0]
    for offset in range(1, n):
        if _pt_close(first, pts2[offset], tol):
            if all(
                _pt_close(pts1[i], pts2[(i + offset) % n], tol) for i in range(1, n)
            ):
                return True
    return False


def _pcell_param_equal(expected: Any, actual: Any, *, rel_tol: float) -> bool:
    if _strict_equal(expected, actual):
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return False
    exp_num = parse_engineering_number(expected)
    act_num = parse_engineering_number(actual)
    if exp_num is None or act_num is None:
        return False
    return math.isclose(exp_num, act_num, rel_tol=rel_tol, abs_tol=0.0)


def _via_param_equal(
    expected: Any, actual: Any, *, coord_tol: float, rel_tol: float
) -> bool:
    if type(expected) is bool or type(actual) is bool:
        return expected is actual
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False
        for left, right in zip(expected, actual):
            if type(left) is bool or type(right) is bool:
                return False
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                return False
            if not math.isclose(float(left), float(right), rel_tol=rel_tol, abs_tol=coord_tol):
                return False
        return True
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(
            float(expected), float(actual), rel_tol=rel_tol, abs_tol=coord_tol
        )
    return _strict_equal(expected, actual)


_STD_VIA_IMPLICIT_DEFAULTS: dict[str, Any] = {
    "cutRows": 1,
    "cutColumns": 1,
    "layer1Offset": [0.0, 0.0],
    "layer2Offset": [0.0, 0.0],
    "originOffset": [0.0, 0.0],
    "imp1Enc": [0.0, 0.0],
    "imp2Enc": [0.0, 0.0],
}


def _via_params_equal(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    coord_tol: float,
    rel_tol: float,
) -> bool:
    for key in set(expected) | set(actual):
        if key in expected and key in actual:
            if not _via_param_equal(
                expected[key], actual[key], coord_tol=coord_tol, rel_tol=rel_tol
            ):
                return False
        else:
            if key not in _STD_VIA_IMPLICIT_DEFAULTS:
                return False
            present = expected[key] if key in expected else actual[key]
            if not _via_param_equal(
                present,
                _STD_VIA_IMPLICIT_DEFAULTS[key],
                coord_tol=coord_tol,
                rel_tol=rel_tol,
            ):
                return False
    return True


def _prop_value_equal(expected: Any, actual: Any, *, coord_tol: float) -> bool:
    if _strict_equal(expected, actual):
        return True
    if type(expected) is float and type(actual) is float:
        return math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-18)
    return False


def _props_equal(
    expected: Mapping[str, ParamValue],
    actual: Mapping[str, ParamValue],
    *,
    coord_tol: float,
) -> bool:
    if set(expected) != set(actual):
        return False
    return all(
        _prop_value_equal(expected[k], actual[k], coord_tol=coord_tol) for k in expected
    )


def _mismatch(
    category: str, path: str, expected: Any, actual: Any, message: str
) -> dict[str, Any]:
    return {
        "category": category,
        "path": path,
        "expected": _plain_value(expected),
        "actual": _plain_value(actual),
        "message": message,
    }


def _compare_props_dict(
    mismatches: list[dict[str, Any]],
    *,
    category: str,
    path: str,
    expected: Mapping[str, ParamValue],
    actual: Mapping[str, ParamValue],
    coord_tol: float,
) -> None:
    for key in sorted(set(expected) | set(actual)):
        item_path = f"{path}[{key!r}]"
        if key not in actual:
            mismatches.append(
                _mismatch(category, item_path, expected[key], None, f"Missing property {key!r}")
            )
        elif key not in expected:
            mismatches.append(
                _mismatch(category, item_path, None, actual[key], f"Unexpected property {key!r}")
            )
        elif not _prop_value_equal(expected[key], actual[key], coord_tol=coord_tol):
            mismatches.append(
                _mismatch(
                    category,
                    item_path,
                    expected[key],
                    actual[key],
                    f"Property {key!r} differs",
                )
            )


def _compare_pcell_params(
    mismatches: list[dict[str, Any]],
    *,
    category: str,
    path: str,
    expected: Mapping[str, ParamValue],
    actual: Mapping[str, ParamValue],
    rel_tol: float,
) -> None:
    for key in sorted(set(expected) | set(actual)):
        item_path = f"{path}[{key!r}]"
        if key not in actual:
            mismatches.append(
                _mismatch(
                    category, item_path, expected[key], None, f"Missing PCell parameter {key!r}"
                )
            )
        elif key not in expected:
            mismatches.append(
                _mismatch(
                    category,
                    item_path,
                    None,
                    actual[key],
                    f"Unexpected PCell parameter {key!r}",
                )
            )
        elif not _pcell_param_equal(expected[key], actual[key], rel_tol=rel_tol):
            mismatches.append(
                _mismatch(
                    category,
                    item_path,
                    expected[key],
                    actual[key],
                    f"PCell parameter {key!r} differs",
                )
            )


def _match_multiset(
    mismatches: list[dict[str, Any]],
    *,
    category: str,
    expected_items: Sequence[Any],
    actual_items: Sequence[Any],
    bucket_fn: Callable[[Any], Any],
    equal_fn: Callable[[Any, Any], bool],
    exact_key_fn: Callable[[Any], str] | None = None,
) -> None:
    """Fast O(N) exact-elimination + bucketed tolerance multiset comparison."""

    exp_buckets: dict[Any, list[Any]] = {}
    act_buckets: dict[Any, list[Any]] = {}
    for item in expected_items:
        exp_buckets.setdefault(bucket_fn(item), []).append(item)
    for item in actual_items:
        act_buckets.setdefault(bucket_fn(item), []).append(item)

    all_buckets = sorted(set(exp_buckets) | set(act_buckets), key=repr)
    for bucket in all_buckets:
        exp_list = exp_buckets.get(bucket, [])
        act_list = act_buckets.get(bucket, [])

        # Fast path: eliminate identical elements in O(N) before tolerance scan.
        if exact_key_fn is not None and exp_list and act_list:
            act_by_key: dict[str, list[Any]] = {}
            for item in act_list:
                act_by_key.setdefault(exact_key_fn(item), []).append(item)
            rem_exp: list[Any] = []
            for item in exp_list:
                k = exact_key_fn(item)
                matches = act_by_key.get(k)
                if matches:
                    matches.pop()
                    if not matches:
                        del act_by_key[k]
                else:
                    rem_exp.append(item)
            rem_act: list[Any] = []
            for leftover in act_by_key.values():
                rem_act.extend(leftover)
        else:
            rem_exp = list(exp_list)
            rem_act = list(act_list)

        # Tolerance matching on remaining elements within the same bucket.
        still_exp: list[Any] = []
        for exp_item in rem_exp:
            matched_idx: int | None = None
            for idx, act_item in enumerate(rem_act):
                if equal_fn(exp_item, act_item):
                    matched_idx = idx
                    break
            if matched_idx is not None:
                rem_act.pop(matched_idx)
            else:
                still_exp.append(exp_item)

        for idx, exp_item in enumerate(still_exp):
            mismatches.append(
                _mismatch(
                    category,
                    f"{category}[missing:{idx}]",
                    exp_item,
                    None,
                    f"Missing or mismatched {category} object",
                )
            )
        for idx, act_item in enumerate(rem_act):
            mismatches.append(
                _mismatch(
                    category,
                    f"{category}[unexpected:{idx}]",
                    None,
                    act_item,
                    f"Unexpected {category} object",
                )
            )


def _exact_json_key(obj: Any) -> str:
    return json.dumps(_plain_value(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _via_exact_key(via: LayoutVia) -> str:
    # Exclude auto-generated OA via.name so identical vias match in O(1).
    payload = _plain_value(via)
    payload.pop("name", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _pin_exact_key(pin: LayoutPin) -> str:
    payload = _plain_value(pin)
    if not pin.pin_name:
        payload.pop("pin_name", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def verify_layout_roundtrip(
    expected: LayoutSnapshot,
    actual: LayoutSnapshot,
    *,
    coord_tol: float = 1e-5,
    param_rel_tol: float = 1e-9,
    raise_on_mismatch: bool = True,
) -> LayoutRoundtripReport:
    """Verify full layout roundtrip fidelity across connectivity, PCells, vias, and shapes."""

    if not isinstance(expected, LayoutSnapshot):
        raise SchemaValidationError("expected must be a LayoutSnapshot")
    if not isinstance(actual, LayoutSnapshot):
        raise SchemaValidationError("actual must be a LayoutSnapshot")
    if coord_tol < 0 or not math.isfinite(coord_tol):
        raise ValueError("coord_tol must be a finite non-negative number")
    if param_rel_tol < 0 or not math.isfinite(param_rel_tol):
        raise ValueError("param_rel_tol must be a finite non-negative number")
    if type(raise_on_mismatch) is not bool:
        raise SchemaValidationError("raise_on_mismatch must be a bool")

    mismatches: list[dict[str, Any]] = []

    # 1. Nets (keyed by name)
    exp_nets = {o.name: o for o in expected.objects if isinstance(o, LayoutNet)}
    act_nets = {o.name: o for o in actual.objects if isinstance(o, LayoutNet)}
    for name in sorted(set(exp_nets) | set(act_nets)):
        path = f"nets[{name!r}]"
        if name not in act_nets:
            mismatches.append(_mismatch("net", path, exp_nets[name], None, f"Missing net {name!r}"))
            continue
        if name not in exp_nets:
            mismatches.append(_mismatch("net", path, None, act_nets[name], f"Unexpected net {name!r}"))
            continue
        en, an = exp_nets[name], act_nets[name]
        for attr in ("is_global", "num_bits", "sig_type"):
            ev, av = getattr(en, attr), getattr(an, attr)
            if not _strict_equal(ev, av):
                mismatches.append(
                    _mismatch("net", f"{path}.{attr}", ev, av, f"Net {name!r} {attr} differs")
                )
        _compare_props_dict(
            mismatches,
            category="net",
            path=f"{path}.props",
            expected=en.props,
            actual=an.props,
            coord_tol=coord_tol,
        )

    # 2. Instances (keyed by name)
    exp_insts = {o.name: o for o in expected.objects if isinstance(o, LayoutInstance)}
    act_insts = {o.name: o for o in actual.objects if isinstance(o, LayoutInstance)}
    for name in sorted(set(exp_insts) | set(act_insts)):
        path = f"instances[{name!r}]"
        if name not in act_insts:
            mismatches.append(
                _mismatch("instance", path, exp_insts[name], None, f"Missing instance {name!r}")
            )
            continue
        if name not in exp_insts:
            mismatches.append(
                _mismatch("instance", path, None, act_insts[name], f"Unexpected instance {name!r}")
            )
            continue
        ei, ai = exp_insts[name], act_insts[name]
        for attr in ("lib", "cell", "view", "orient", "is_pcell"):
            ev, av = getattr(ei, attr), getattr(ai, attr)
            if not _strict_equal(ev, av):
                mismatches.append(
                    _mismatch(
                        "instance", f"{path}.{attr}", ev, av, f"Instance {name!r} {attr} differs"
                    )
                )
        if not _pt_close(ei.xy, ai.xy, coord_tol):
            mismatches.append(
                _mismatch("instance", f"{path}.xy", ei.xy, ai.xy, f"Instance {name!r} xy differs")
            )
        _compare_pcell_params(
            mismatches,
            category="instance",
            path=f"{path}.pcell_params",
            expected=ei.pcell_params,
            actual=ai.pcell_params,
            rel_tol=param_rel_tol,
        )
        for term in sorted(set(ei.connections) | set(ai.connections)):
            cpath = f"{path}.connections[{term!r}]"
            if term not in ai.connections:
                mismatches.append(
                    _mismatch(
                        "instance",
                        cpath,
                        ei.connections[term],
                        None,
                        f"Missing connection on {name!r}.{term}",
                    )
                )
            elif term not in ei.connections:
                mismatches.append(
                    _mismatch(
                        "instance",
                        cpath,
                        None,
                        ai.connections[term],
                        f"Unexpected connection on {name!r}.{term}",
                    )
                )
            elif ei.connections[term] != ai.connections[term]:
                mismatches.append(
                    _mismatch(
                        "instance",
                        cpath,
                        ei.connections[term],
                        ai.connections[term],
                        f"Connection on {name!r}.{term} differs",
                    )
                )
        _compare_props_dict(
            mismatches,
            category="instance",
            path=f"{path}.props",
            expected=ei.props,
            actual=ai.props,
            coord_tol=coord_tol,
        )

    # 3. Mosaics (keyed by name)
    exp_mosaics = {o.name: o for o in expected.objects if isinstance(o, LayoutMosaic)}
    act_mosaics = {o.name: o for o in actual.objects if isinstance(o, LayoutMosaic)}
    for name in sorted(set(exp_mosaics) | set(act_mosaics)):
        path = f"mosaics[{name!r}]"
        if name not in act_mosaics:
            mismatches.append(
                _mismatch("mosaic", path, exp_mosaics[name], None, f"Missing mosaic {name!r}")
            )
            continue
        if name not in exp_mosaics:
            mismatches.append(
                _mismatch("mosaic", path, None, act_mosaics[name], f"Unexpected mosaic {name!r}")
            )
            continue
        em, am = exp_mosaics[name], act_mosaics[name]
        for attr in ("lib", "cell", "view", "orient", "rows", "columns", "is_pcell"):
            ev, av = getattr(em, attr), getattr(am, attr)
            if not _strict_equal(ev, av):
                mismatches.append(
                    _mismatch(
                        "mosaic", f"{path}.{attr}", ev, av, f"Mosaic {name!r} {attr} differs"
                    )
                )
        if not _pt_close(em.xy, am.xy, coord_tol):
            mismatches.append(
                _mismatch("mosaic", f"{path}.xy", em.xy, am.xy, f"Mosaic {name!r} xy differs")
            )
        if not _coord_close(em.u_x, am.u_x, coord_tol):
            mismatches.append(
                _mismatch("mosaic", f"{path}.u_x", em.u_x, am.u_x, f"Mosaic {name!r} u_x differs")
            )
        if not _coord_close(em.u_y, am.u_y, coord_tol):
            mismatches.append(
                _mismatch("mosaic", f"{path}.u_y", em.u_y, am.u_y, f"Mosaic {name!r} u_y differs")
            )
        _compare_pcell_params(
            mismatches,
            category="mosaic",
            path=f"{path}.pcell_params",
            expected=em.pcell_params,
            actual=am.pcell_params,
            rel_tol=param_rel_tol,
        )
        _compare_props_dict(
            mismatches,
            category="mosaic",
            path=f"{path}.props",
            expected=em.props,
            actual=am.props,
            coord_tol=coord_tol,
        )

    # 4. FigGroups (keyed by name)
    exp_fgs = {o.name: o for o in expected.objects if isinstance(o, LayoutFigGroup)}
    act_fgs = {o.name: o for o in actual.objects if isinstance(o, LayoutFigGroup)}
    for name in sorted(set(exp_fgs) | set(act_fgs)):
        path = f"fig_groups[{name!r}]"
        if name not in act_fgs:
            mismatches.append(
                _mismatch("fig_group", path, exp_fgs[name], None, f"Missing fig_group {name!r}")
            )
            continue
        if name not in exp_fgs:
            mismatches.append(
                _mismatch("fig_group", path, None, act_fgs[name], f"Unexpected fig_group {name!r}")
            )
            continue
        ef, af = exp_fgs[name], act_fgs[name]
        if not _pt_close(ef.origin, af.origin, coord_tol):
            mismatches.append(
                _mismatch(
                    "fig_group",
                    f"{path}.origin",
                    ef.origin,
                    af.origin,
                    f"FigGroup {name!r} origin differs",
                )
            )
        if not _strict_equal(ef.orient, af.orient):
            mismatches.append(
                _mismatch(
                    "fig_group",
                    f"{path}.orient",
                    ef.orient,
                    af.orient,
                    f"FigGroup {name!r} orient differs",
                )
            )
        # In OpenAccess, dbCreateFigGroup always creates type="none" and fg~>type is
        # read-only (DB-370034), so XL-only tags like "modgen" normalize to "none".
        if not _strict_equal(ef.group_type, af.group_type) and not (
            af.group_type == "none" and ef.group_type != "none"
        ):
            mismatches.append(
                _mismatch(
                    "fig_group",
                    f"{path}.group_type",
                    ef.group_type,
                    af.group_type,
                    f"FigGroup {name!r} group_type differs",
                )
            )
        _compare_props_dict(
            mismatches,
            category="fig_group",
            path=f"{path}.props",
            expected=ef.props,
            actual=af.props,
            coord_tol=coord_tol,
        )

    # 5. Pins
    def _pin_equal(a: LayoutPin, b: LayoutPin) -> bool:
        if not (
            a.name == b.name
            and a.net == b.net
            and a.direction == b.direction
            and a.layer == b.layer
            and a.purpose == b.purpose
            and a.shape_kind == b.shape_kind
            and (not a.pin_name or a.pin_name == b.pin_name)
            and a.access_dir == b.access_dir
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ):
            return False
        if a.shape_kind in ("rect", "ellipse"):
            return _bbox_close(a.bbox, b.bbox, coord_tol)
        if a.shape_kind == "polygon":
            return _ring_close(a.points, b.points, coord_tol)
        if a.shape_kind == "path":
            return (
                a.path_style == b.path_style
                and _coord_close(a.width, b.width, coord_tol)
                and _coord_close(a.begin_ext, b.begin_ext, coord_tol)
                and _coord_close(a.end_ext, b.end_ext, coord_tol)
                and _polyline_close(a.points, b.points, coord_tol)
            )
        if a.shape_kind == "path_seg":
            if not _coord_close(a.width, b.width, coord_tol):
                return False
            if a.begin_pt is None or a.end_pt is None or b.begin_pt is None or b.end_pt is None:
                return False
            def _cs_close(x: tuple[float, ...], y: tuple[float, ...]) -> bool:
                return len(x) == len(y) and all(
                    _coord_close(u, v, coord_tol) for u, v in zip(x, y)
                )
            fwd = (
                _pt_close(a.begin_pt, b.begin_pt, coord_tol)
                and _pt_close(a.end_pt, b.end_pt, coord_tol)
                and a.begin_style == b.begin_style
                and a.end_style == b.end_style
                and _coord_close(a.begin_ext, b.begin_ext, coord_tol)
                and _coord_close(a.end_ext, b.end_ext, coord_tol)
                and _cs_close(a.custom_style, b.custom_style)
            )
            if fwd:
                return True
            b_cs_rev = b.custom_style[3:] + b.custom_style[:3] if len(b.custom_style) == 6 else b.custom_style
            return (
                _pt_close(a.begin_pt, b.end_pt, coord_tol)
                and _pt_close(a.end_pt, b.begin_pt, coord_tol)
                and a.begin_style == b.end_style
                and a.end_style == b.begin_style
                and _coord_close(a.begin_ext, b.end_ext, coord_tol)
                and _coord_close(a.end_ext, b.begin_ext, coord_tol)
                and _cs_close(a.custom_style, b_cs_rev)
            )
        return False

    _match_multiset(
        mismatches,
        category="pin",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutPin)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutPin)],
        bucket_fn=lambda p: (p.name, p.net, p.layer, p.purpose, p.shape_kind),
        equal_fn=_pin_equal,
        exact_key_fn=_pin_exact_key,
    )

    # 6. Vias (via_def, via_kind, origin, orient, net, params, props)
    _match_multiset(
        mismatches,
        category="via",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutVia)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutVia)],
        bucket_fn=lambda v: (v.via_def, v.via_kind, v.orient, v.net),
        equal_fn=lambda a, b: (
            a.via_def == b.via_def
            and a.via_kind == b.via_kind
            and a.orient == b.orient
            and a.net == b.net
            and _pt_close(a.origin, b.origin, coord_tol)
            and _via_params_equal(
                a.params, b.params, coord_tol=coord_tol, rel_tol=param_rel_tol
            )
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_via_exact_key,
    )

    # 7. Physical shapes
    _match_multiset(
        mismatches,
        category="rect",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutRect)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutRect)],
        bucket_fn=lambda r: (r.layer, r.purpose, r.net),
        equal_fn=lambda a, b: (
            a.layer == b.layer
            and a.purpose == b.purpose
            and a.net == b.net
            and _bbox_close(a.bbox, b.bbox, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    def _path_seg_equal(a: LayoutPathSeg, b: LayoutPathSeg) -> bool:
        if not (
            a.layer == b.layer
            and a.purpose == b.purpose
            and a.net == b.net
            and _coord_close(a.width, b.width, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ):
            return False
        def _cs_close(x: tuple[float, ...], y: tuple[float, ...]) -> bool:
            return len(x) == len(y) and all(
                _coord_close(u, v, coord_tol) for u, v in zip(x, y)
            )
        fwd = (
            _pt_close(a.begin_pt, b.begin_pt, coord_tol)
            and _pt_close(a.end_pt, b.end_pt, coord_tol)
            and a.begin_style == b.begin_style
            and a.end_style == b.end_style
            and _coord_close(a.begin_ext, b.begin_ext, coord_tol)
            and _coord_close(a.end_ext, b.end_ext, coord_tol)
            and _cs_close(a.custom_style, b.custom_style)
        )
        if fwd:
            return True
        b_cs_rev = b.custom_style[3:] + b.custom_style[:3] if len(b.custom_style) == 6 else b.custom_style
        return (
            _pt_close(a.begin_pt, b.end_pt, coord_tol)
            and _pt_close(a.end_pt, b.begin_pt, coord_tol)
            and a.begin_style == b.end_style
            and a.end_style == b.begin_style
            and _coord_close(a.begin_ext, b.end_ext, coord_tol)
            and _coord_close(a.end_ext, b.begin_ext, coord_tol)
            and _cs_close(a.custom_style, b_cs_rev)
        )

    _match_multiset(
        mismatches,
        category="path_seg",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutPathSeg)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutPathSeg)],
        bucket_fn=lambda s: (s.layer, s.purpose, s.net),
        equal_fn=_path_seg_equal,
        exact_key_fn=_exact_json_key,
    )

    _match_multiset(
        mismatches,
        category="path",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutPath)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutPath)],
        bucket_fn=lambda p: (p.layer, p.purpose, p.net, len(p.points)),
        equal_fn=lambda a, b: (
            a.layer == b.layer
            and a.purpose == b.purpose
            and a.net == b.net
            and a.path_style == b.path_style
            and _coord_close(a.width, b.width, coord_tol)
            and _coord_close(a.begin_ext, b.begin_ext, coord_tol)
            and _coord_close(a.end_ext, b.end_ext, coord_tol)
            and _polyline_close(a.points, b.points, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    _match_multiset(
        mismatches,
        category="polygon",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutPolygon)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutPolygon)],
        bucket_fn=lambda p: (p.layer, p.purpose, p.net, len(p.points)),
        equal_fn=lambda a, b: (
            a.layer == b.layer
            and a.purpose == b.purpose
            and a.net == b.net
            and _ring_close(a.points, b.points, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    _match_multiset(
        mismatches,
        category="ellipse",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutEllipse)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutEllipse)],
        bucket_fn=lambda e: (e.layer, e.purpose, e.net),
        equal_fn=lambda a, b: (
            a.layer == b.layer
            and a.purpose == b.purpose
            and a.net == b.net
            and _bbox_close(a.bbox, b.bbox, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    _match_multiset(
        mismatches,
        category="line",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutLine)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutLine)],
        bucket_fn=lambda ln: (ln.layer, ln.purpose, ln.net, len(ln.points)),
        equal_fn=lambda a, b: (
            a.layer == b.layer
            and a.purpose == b.purpose
            and a.net == b.net
            and _polyline_close(a.points, b.points, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    # 8. Labels
    _match_multiset(
        mismatches,
        category="label",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutLabel)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutLabel)],
        bucket_fn=lambda lb: (lb.layer, lb.purpose, lb.text, lb.net),
        equal_fn=lambda a, b: (
            a.text == b.text
            and a.layer == b.layer
            and a.purpose == b.purpose
            and a.justify == b.justify
            and a.orient == b.orient
            and a.font == b.font
            and a.label_type == b.label_type
            and a.net == b.net
            and _pt_close(a.xy, b.xy, coord_tol)
            and _coord_close(a.height, b.height, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    # 9. Blockages
    _match_multiset(
        mismatches,
        category="blockage",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutBlockage)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutBlockage)],
        bucket_fn=lambda blk: (blk.blockage_type, blk.layer, len(blk.points)),
        equal_fn=lambda a, b: (
            a.blockage_type == b.blockage_type
            and a.layer == b.layer
            and _ring_close(a.points, b.points, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    # 10. Boundaries
    _match_multiset(
        mismatches,
        category="boundary",
        expected_items=[o for o in expected.objects if isinstance(o, LayoutBoundary)],
        actual_items=[o for o in actual.objects if isinstance(o, LayoutBoundary)],
        bucket_fn=lambda bnd: (bnd.boundary_kind, bnd.name, len(bnd.points)),
        equal_fn=lambda a, b: (
            a.boundary_kind == b.boundary_kind
            and a.name == b.name
            and _ring_close(a.points, b.points, coord_tol)
            and _props_equal(a.props, b.props, coord_tol=coord_tol)
        ),
        exact_key_fn=_exact_json_key,
    )

    # 11. Cell properties
    _compare_props_dict(
        mismatches,
        category="cell_properties",
        path="cell_properties",
        expected=expected.cell_properties,
        actual=actual.cell_properties,
        coord_tol=coord_tol,
    )

    report = LayoutRoundtripReport(ok=not mismatches, mismatches=tuple(mismatches))
    if mismatches and raise_on_mismatch:
        summary_parts = [
            f"{m['category']}:{m['path']} expected={_diagnostic_value(m['expected'])} "
            f"actual={_diagnostic_value(m['actual'])} ({m['message']})"
            for m in mismatches[:5]
        ]
        if len(mismatches) > 5:
            summary_parts.append(f"... and {len(mismatches) - 5} more")
        raise RoundtripMismatchError(
            f"Layout roundtrip mismatch ({len(mismatches)} issue(s)): "
            + "; ".join(summary_parts)
        )
    return report


# ---------------------------------------------------------------------------
# 3. DPL-JSON Wire Codec (Exact SKILL Contract)
# ---------------------------------------------------------------------------


def _skill_list(rendered_items: Iterable[str]) -> str:
    items = tuple(rendered_items)
    return "nil" if not items else "(" + " ".join(items) + ")"


def _skill_dpl(fields: Iterable[tuple[str, str]]) -> str:
    parts = ["nil"]
    for key, rendered_value in fields:
        if not _SKILL_SYMBOL_RE.fullmatch(key):
            raise SchemaValidationError(f"invalid SKILL DPL key {key!r}")
        parts.extend((key, rendered_value))
    return "(" + " ".join(parts) + ")"


def _infer_prop_type(value: Any) -> str:
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "int"
    if type(value) is float:
        return "float"
    if type(value) is str:
        return "string"
    raise SchemaValidationError(
        f"layout property value must be a scalar ParamValue, got {type(value).__name__}"
    )


def _prop_triples_wire(
    values: Mapping[str, ParamValue],
    explicit_types: Mapping[str, str] | None = None,
) -> list[list[Any]]:
    triples: list[list[Any]] = []
    for key in sorted(values):
        val = values[key]
        if explicit_types is not None:
            if key not in explicit_types:
                raise SchemaValidationError(f"missing explicit type for key {key!r}")
            vtype = explicit_types[key]
        else:
            vtype = _infer_prop_type(val)
        triples.append([key, val, vtype])
    return triples


def _via_param_wire_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and not isinstance(value, (str, bytes)):
        return list(value)
    return value


def _via_params_wire(params: Mapping[str, Any]) -> list[list[Any]]:
    return [[key, _via_param_wire_value(params[key])] for key in sorted(params)]


def _render_dpl_from_dict(record: Mapping[str, Any], key_order: Sequence[str]) -> str:
    fields: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key in key_order:
        if key in record:
            fields.append((key, to_skill_literal(record[key])))
            seen.add(key)
    for key in sorted(record):
        if key not in seen:
            fields.append((key, to_skill_literal(record[key])))
    return _skill_dpl(fields)


def _net_wire_dict(net: LayoutNet) -> dict[str, Any]:
    return {
        "name": net.name,
        "is_global": net.is_global,
        "num_bits": net.num_bits,
        "sig_type": net.sig_type,
        "props": _prop_triples_wire(net.props),
    }


def _boundary_wire_dict(boundary: LayoutBoundary) -> dict[str, Any]:
    return {
        "boundary_kind": boundary.boundary_kind,
        "points": [list(pt) for pt in boundary.points],
        "name": boundary.name,
        "props": _prop_triples_wire(boundary.props),
    }


def _instance_wire_dict(inst: LayoutInstance) -> dict[str, Any]:
    return {
        "name": inst.name,
        "lib": inst.lib,
        "cell": inst.cell,
        "view": inst.view,
        "xy": list(inst.xy),
        "orient": inst.orient,
        "is_pcell": inst.is_pcell,
        "pcell_params": _prop_triples_wire(inst.pcell_params, inst.pcell_param_types),
        "connections": [[term, net] for term, net in sorted(inst.connections.items())],
        "props": _prop_triples_wire(inst.props, inst.prop_types),
    }


def _mosaic_wire_dict(mosaic: LayoutMosaic) -> dict[str, Any]:
    return {
        "name": mosaic.name,
        "lib": mosaic.lib,
        "cell": mosaic.cell,
        "view": mosaic.view,
        "xy": list(mosaic.xy),
        "orient": mosaic.orient,
        "rows": mosaic.rows,
        "columns": mosaic.columns,
        "u_x": mosaic.u_x,
        "u_y": mosaic.u_y,
        "is_pcell": mosaic.is_pcell,
        "pcell_params": _prop_triples_wire(mosaic.pcell_params, mosaic.pcell_param_types),
        "props": _prop_triples_wire(mosaic.props, mosaic.prop_types),
    }


def _via_wire_dict(via: LayoutVia) -> dict[str, Any]:
    return {
        "via_def": via.via_def,
        "via_kind": via.via_kind,
        "origin": list(via.origin),
        "orient": via.orient,
        "net": via.net,
        "name": via.name,
        "params": _via_params_wire(via.params),
        "props": _prop_triples_wire(via.props),
    }


def _shape_wire_dict(
    shape: LayoutRect | LayoutPathSeg | LayoutPath | LayoutPolygon | LayoutEllipse | LayoutLine,
) -> dict[str, Any]:
    if isinstance(shape, LayoutRect):
        return {
            "type": "rect",
            "layer": shape.layer,
            "purpose": shape.purpose,
            "bbox": [list(shape.bbox[0]), list(shape.bbox[1])],
            "net": shape.net,
            "props": _prop_triples_wire(shape.props),
        }
    if isinstance(shape, LayoutPathSeg):
        return {
            "type": "path_seg",
            "layer": shape.layer,
            "purpose": shape.purpose,
            "begin_pt": list(shape.begin_pt),
            "end_pt": list(shape.end_pt),
            "width": shape.width,
            "begin_style": shape.begin_style,
            "end_style": shape.end_style,
            "begin_ext": shape.begin_ext,
            "end_ext": shape.end_ext,
            "custom_style": list(shape.custom_style),
            "net": shape.net,
            "props": _prop_triples_wire(shape.props),
        }
    if isinstance(shape, LayoutPath):
        return {
            "type": "path",
            "layer": shape.layer,
            "purpose": shape.purpose,
            "points": [list(pt) for pt in shape.points],
            "width": shape.width,
            "path_style": shape.path_style,
            "begin_ext": shape.begin_ext,
            "end_ext": shape.end_ext,
            "net": shape.net,
            "props": _prop_triples_wire(shape.props),
        }
    if isinstance(shape, LayoutPolygon):
        return {
            "type": "polygon",
            "layer": shape.layer,
            "purpose": shape.purpose,
            "points": [list(pt) for pt in shape.points],
            "net": shape.net,
            "props": _prop_triples_wire(shape.props),
        }
    if isinstance(shape, LayoutEllipse):
        return {
            "type": "ellipse",
            "layer": shape.layer,
            "purpose": shape.purpose,
            "bbox": [list(shape.bbox[0]), list(shape.bbox[1])],
            "net": shape.net,
            "props": _prop_triples_wire(shape.props),
        }
    if isinstance(shape, LayoutLine):
        return {
            "type": "line",
            "layer": shape.layer,
            "purpose": shape.purpose,
            "points": [list(pt) for pt in shape.points],
            "net": shape.net,
            "props": _prop_triples_wire(shape.props),
        }
    raise SchemaValidationError(f"unsupported layout shape type: {type(shape).__name__}")


def _pin_wire_dict(pin: LayoutPin) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": pin.name,
        "pin_name": pin.pin_name,
        "net": pin.net,
        "direction": pin.direction,
        "layer": pin.layer,
        "purpose": pin.purpose,
        "shape_kind": pin.shape_kind,
        "bbox": [list(pin.bbox[0]), list(pin.bbox[1])] if pin.bbox is not None else None,
        "points": [list(pt) for pt in pin.points],
        "access_dir": list(pin.access_dir),
        "props": _prop_triples_wire(pin.props),
    }
    if pin.shape_kind == "path_seg" and pin.begin_pt is not None and pin.end_pt is not None:
        out["begin_pt"] = list(pin.begin_pt)
        out["end_pt"] = list(pin.end_pt)
        out["width"] = pin.width
        out["begin_style"] = pin.begin_style
        out["end_style"] = pin.end_style
        out["begin_ext"] = pin.begin_ext
        out["end_ext"] = pin.end_ext
        if pin.custom_style:
            out["custom_style"] = list(pin.custom_style)
    elif pin.shape_kind == "path":
        out["width"] = pin.width
        out["path_style"] = pin.path_style
        out["begin_ext"] = pin.begin_ext
        out["end_ext"] = pin.end_ext
    return out


def _label_wire_dict(label: LayoutLabel) -> dict[str, Any]:
    return {
        "text": label.text,
        "xy": list(label.xy),
        "layer": label.layer,
        "purpose": label.purpose,
        "justify": label.justify,
        "orient": label.orient,
        "font": label.font,
        "height": label.height,
        "label_type": label.label_type,
        "net": label.net,
        "props": _prop_triples_wire(label.props),
    }


def _blockage_wire_dict(blockage: LayoutBlockage) -> dict[str, Any]:
    return {
        "blockage_type": blockage.blockage_type,
        "points": [list(pt) for pt in blockage.points],
        "layer": blockage.layer,
        "props": _prop_triples_wire(blockage.props),
    }


def _fig_group_wire_dict(fg: LayoutFigGroup) -> dict[str, Any]:
    return {
        "name": fg.name,
        "origin": list(fg.origin),
        "orient": fg.orient,
        "group_type": fg.group_type,
        "props": _prop_triples_wire(fg.props),
    }


def build_capture_layout_request_dict(cellview: CellViewRef) -> dict[str, Any]:
    """Build a transport-agnostic request dict for ``SkillRequestRunner.run_skill_request``."""

    if not isinstance(cellview, CellViewRef):
        raise _schema_error("cellview", "must be a CellViewRef")
    return {
        "action": "capture_layout",
        "library": cellview.library,
        "cell": cellview.cell,
        "view": cellview.view,
    }


def render_capture_layout_request_skill(cellview: CellViewRef, output_path: Path) -> str:
    """Render the complete SKILL request file for ``capture_layout``."""

    if not isinstance(cellview, CellViewRef):
        raise _schema_error("cellview", "must be a CellViewRef")
    response_path = _normalize_output_path(output_path, "output_path")
    request = _skill_dpl(
        (
            ("action", to_skill_literal("capture_layout")),
            ("library", to_skill_literal(cellview.library)),
            ("cell", to_skill_literal(cellview.cell)),
            ("view", to_skill_literal(cellview.view)),
            ("output_path", to_skill_literal(response_path)),
        )
    )
    return "setq(oagitRequest '" + request + ")\n"


def _validate_restore_request_inputs(
    snapshot: LayoutSnapshot,
    target: CellViewRef,
    *,
    draft_cell: str,
    overwrite: bool,
    allow_libraries: Iterable[str],
) -> tuple[LayoutSnapshot, tuple[str, ...]]:
    if not isinstance(snapshot, LayoutSnapshot):
        raise _schema_error("snapshot", "must be a LayoutSnapshot")
    if not isinstance(snapshot.manifest, LayoutManifest):
        raise _schema_error("snapshot.manifest", "must be a LayoutManifest")
    canonical = build_layout_snapshot(
        snapshot.manifest.cellview,
        snapshot.objects,
        cell_properties=snapshot.cell_properties,
        omissions=snapshot.omissions,
        metadata=snapshot.manifest.metadata,
    )
    if not isinstance(target, CellViewRef):
        raise _schema_error("target", "must be a CellViewRef")
    if (
        not isinstance(draft_cell, str)
        or not draft_cell.strip()
        or "/" in draft_cell
        or "\\" in draft_cell
    ):
        raise _schema_error("draft_cell", "must be a non-empty cell name")
    if draft_cell == target.cell:
        raise _schema_error("draft_cell", "must be distinct from target.cell")
    if type(overwrite) is not bool:
        raise _schema_error("overwrite", "must be a bool")
    try:
        libraries = tuple(allow_libraries)
    except TypeError as exc:
        raise _schema_error("allow_libraries", "must be iterable") from exc
    if any(type(value) is not str or not value.strip() for value in libraries):
        raise _schema_error("allow_libraries", "must contain non-empty strings")
    return canonical, libraries


def build_restore_layout_request_dict(
    snapshot: LayoutSnapshot,
    target: CellViewRef,
    *,
    draft_cell: str,
    overwrite: bool,
    allow_libraries: Iterable[str],
) -> dict[str, Any]:
    """Build a canonical restore request dict for ``SkillRequestRunner.run_skill_request``."""

    canonical, libraries = _validate_restore_request_inputs(
        snapshot,
        target,
        draft_cell=draft_cell,
        overwrite=overwrite,
        allow_libraries=allow_libraries,
    )
    nets: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    instances: list[dict[str, Any]] = []
    mosaics: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    shapes: list[dict[str, Any]] = []
    pins: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    blockages: list[dict[str, Any]] = []
    fig_groups: list[dict[str, Any]] = []

    for obj in canonical.objects:
        if isinstance(obj, LayoutNet):
            nets.append(_net_wire_dict(obj))
        elif isinstance(obj, LayoutBoundary):
            boundaries.append(_boundary_wire_dict(obj))
        elif isinstance(obj, LayoutInstance):
            instances.append(_instance_wire_dict(obj))
        elif isinstance(obj, LayoutMosaic):
            mosaics.append(_mosaic_wire_dict(obj))
        elif isinstance(obj, LayoutVia):
            vias.append(_via_wire_dict(obj))
        elif isinstance(
            obj,
            (
                LayoutRect,
                LayoutPathSeg,
                LayoutPath,
                LayoutPolygon,
                LayoutEllipse,
                LayoutLine,
            ),
        ):
            shapes.append(_shape_wire_dict(obj))
        elif isinstance(obj, LayoutPin):
            pins.append(_pin_wire_dict(obj))
        elif isinstance(obj, LayoutLabel):
            labels.append(_label_wire_dict(obj))
        elif isinstance(obj, LayoutBlockage):
            blockages.append(_blockage_wire_dict(obj))
        elif isinstance(obj, LayoutFigGroup):
            fig_groups.append(_fig_group_wire_dict(obj))

    return {
        "action": "restore_layout",
        "target": {
            "library": target.library,
            "cell": target.cell,
            "view": target.view,
        },
        "draft_cell": draft_cell,
        "overwrite": overwrite,
        "allow_libraries": list(libraries),
        "nets": nets,
        "boundaries": boundaries,
        "instances": instances,
        "mosaics": mosaics,
        "vias": vias,
        "shapes": shapes,
        "pins": pins,
        "labels": labels,
        "blockages": blockages,
        "fig_groups": fig_groups,
        "properties": _prop_triples_wire(canonical.cell_properties),
    }


def render_restore_layout_request_skill(
    snapshot: LayoutSnapshot,
    target: CellViewRef,
    *,
    draft_cell: str,
    overwrite: bool,
    allow_libraries: Iterable[str],
    output_path: Path,
) -> str:
    """Render the complete SKILL request file for ``restore_layout``."""

    req = build_restore_layout_request_dict(
        snapshot,
        target,
        draft_cell=draft_cell,
        overwrite=overwrite,
        allow_libraries=allow_libraries,
    )
    response_path = _normalize_output_path(output_path, "output_path")

    rendered_target = _skill_dpl(
        (
            ("library", to_skill_literal(target.library)),
            ("cell", to_skill_literal(target.cell)),
            ("view", to_skill_literal(target.view)),
        )
    )
    rendered_nets = _skill_list(
        _render_dpl_from_dict(item, ("name", "is_global", "num_bits", "sig_type", "props"))
        for item in req["nets"]
    )
    rendered_boundaries = _skill_list(
        _render_dpl_from_dict(item, ("boundary_kind", "points", "name", "props"))
        for item in req["boundaries"]
    )
    rendered_instances = _skill_list(
        _render_dpl_from_dict(
            item,
            (
                "name",
                "lib",
                "cell",
                "view",
                "xy",
                "orient",
                "is_pcell",
                "pcell_params",
                "connections",
                "props",
            ),
        )
        for item in req["instances"]
    )
    rendered_mosaics = _skill_list(
        _render_dpl_from_dict(
            item,
            (
                "name",
                "lib",
                "cell",
                "view",
                "xy",
                "orient",
                "rows",
                "columns",
                "u_x",
                "u_y",
                "is_pcell",
                "pcell_params",
                "props",
            ),
        )
        for item in req["mosaics"]
    )
    rendered_vias = _skill_list(
        _render_dpl_from_dict(
            item,
            ("via_def", "via_kind", "origin", "orient", "net", "name", "params", "props"),
        )
        for item in req["vias"]
    )
    rendered_shapes = _skill_list(
        _render_dpl_from_dict(
            item,
            (
                "type",
                "layer",
                "purpose",
                "bbox",
                "points",
                "begin_pt",
                "end_pt",
                "width",
                "path_style",
                "begin_style",
                "end_style",
                "begin_ext",
                "end_ext",
                "net",
                "props",
            ),
        )
        for item in req["shapes"]
    )
    rendered_pins = _skill_list(
        _render_dpl_from_dict(
            item,
            (
                "name",
                "pin_name",
                "net",
                "direction",
                "layer",
                "purpose",
                "shape_kind",
                "bbox",
                "points",
                "begin_pt",
                "end_pt",
                "width",
                "begin_style",
                "end_style",
                "begin_ext",
                "end_ext",
                "path_style",
                "access_dir",
                "props",
            ),
        )
        for item in req["pins"]
    )
    rendered_labels = _skill_list(
        _render_dpl_from_dict(
            item,
            (
                "text",
                "xy",
                "layer",
                "purpose",
                "justify",
                "orient",
                "font",
                "height",
                "label_type",
                "net",
                "props",
            ),
        )
        for item in req["labels"]
    )
    rendered_blockages = _skill_list(
        _render_dpl_from_dict(item, ("blockage_type", "points", "layer", "props"))
        for item in req["blockages"]
    )
    rendered_fig_groups = _skill_list(
        _render_dpl_from_dict(item, ("name", "origin", "orient", "group_type", "props"))
        for item in req["fig_groups"]
    )

    request = _skill_dpl(
        (
            ("action", to_skill_literal("restore_layout")),
            ("target", rendered_target),
            ("draft_cell", to_skill_literal(req["draft_cell"])),
            ("overwrite", to_skill_literal(req["overwrite"])),
            ("allow_libraries", to_skill_literal(req["allow_libraries"])),
            ("output_path", to_skill_literal(response_path)),
            ("nets", rendered_nets),
            ("boundaries", rendered_boundaries),
            ("instances", rendered_instances),
            ("mosaics", rendered_mosaics),
            ("vias", rendered_vias),
            ("shapes", rendered_shapes),
            ("pins", rendered_pins),
            ("labels", rendered_labels),
            ("blockages", rendered_blockages),
            ("fig_groups", rendered_fig_groups),
            ("properties", to_skill_literal(req["properties"])),
        )
    )
    return "setq(oagitRequest '" + request + ")\n"


def raw_layout_capture_to_snapshot(payload: Mapping[str, Any]) -> LayoutSnapshot:
    """Validate an ``export_layout.il`` JSON response and build a canonical snapshot."""

    raw_data = _require_mapping(payload, "layout capture response")
    data = _normalise_structured_error(raw_data)

    if "ok" not in data:
        raise _schema_error("layout capture response", "missing 'ok'")
    ok = _require_bool(data["ok"], "layout capture response.ok")
    if not ok:
        code = data.get("error_code")
        if code is not None and type(code) is not str:
            raise _schema_error("layout capture response.error_code", "must be a string or null")
        detail = data.get("error") or "layout capture backend returned ok=false"
        message = f"{code}: {detail}" if code else str(detail)
        raise BridgeExecutionError(message)

    envelope_keys = {"protocol", "request_id", "session", "action", "transport"}
    unknown = set(data) - _LAYOUT_CAPTURE_KEYS - envelope_keys
    if unknown:
        raise _schema_error(
            "layout capture response", f"unsupported keys {sorted(unknown)!r}"
        )
    for required in (
        "cellview",
        "modified_but_not_saved",
        "metadata",
        "objects",
        "properties",
        "omissions",
    ):
        if required not in data:
            raise _schema_error("layout capture response", f"missing {required!r}")

    modified = _require_bool(
        data["modified_but_not_saved"], "layout capture response.modified_but_not_saved"
    )
    if modified:
        raise FailClosedError("layout capture response reports modified_but_not_saved=true")

    if data.get("error") is not None:
        _require_text(data["error"], "layout capture response.error", nonempty=False)
    if data.get("error_code") is not None:
        _require_text(data["error_code"], "layout capture response.error_code", nonempty=False)

    cellview = _cellview_from_payload(data["cellview"], "layout capture response.cellview")
    metadata = dict(_require_mapping(data["metadata"], "layout capture response.metadata"))
    properties = dict(
        _require_mapping(data["properties"], "layout capture response.properties")
    )
    raw_objects = _require_sequence(data["objects"], "layout capture response.objects")
    objects: list[LayoutObject] = [
        layout_object_from_dict(item, path=f"layout capture response.objects[{idx}]")
        for idx, item in enumerate(raw_objects)
    ]

    raw_omissions = _require_sequence(data["omissions"], "layout capture response.omissions")
    omissions: list[OmissionRecord] = []
    for idx, item in enumerate(raw_omissions):
        omission = _require_mapping(item, f"layout capture response.omissions[{idx}]")
        allowed = {"kind", "identifier", "reason", "details"}
        if set(omission) - allowed or not {"kind", "identifier", "reason"}.issubset(omission):
            raise _schema_error(
                f"layout capture response.omissions[{idx}]", "invalid omission fields"
            )
        details = _require_mapping(
            omission.get("details", {}),
            f"layout capture response.omissions[{idx}].details",
        )
        omissions.append(
            OmissionRecord(
                kind=_require_text(
                    omission["kind"], f"layout capture response.omissions[{idx}].kind"
                ),
                identifier=_require_text(
                    omission["identifier"],
                    f"layout capture response.omissions[{idx}].identifier",
                ),
                reason=_require_text(
                    omission["reason"], f"layout capture response.omissions[{idx}].reason"
                ),
                details=dict(details),
            )
        )

    return build_layout_snapshot(
        cellview,
        objects,
        cell_properties=properties,
        omissions=tuple(omissions),
        metadata=metadata,
    )


def _restore_error_message(payload: Mapping[str, Any], code: Any) -> str:
    detail = payload.get("error")
    if detail is None or detail == "":
        detail = "layout restore backend returned ok=false"
    extras: list[str] = []
    for field in ("pcell_mismatches", "connectivity_mismatches"):
        items = payload.get(field)
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)) and items:
            sample = list(items[:5])
            extras.append(f"{field}={sample!r}")
    suffix = f" ({'; '.join(extras)})" if extras else ""
    if code is None:
        return f"{detail}{suffix}"
    return f"{code}: {detail}{suffix}"


def parse_layout_restore_response(
    payload: Mapping[str, Any], target: CellViewRef
) -> LayoutRestoreReceipt:
    """Validate a ``restore_layout.il`` response and map wire error codes Fail-Closed."""

    raw_data = _require_mapping(payload, "layout restore response")
    data = _normalise_structured_error(raw_data)
    if not isinstance(target, CellViewRef):
        raise _schema_error("target", "must be a CellViewRef")

    if "ok" not in data:
        raise _schema_error("layout restore response", "missing 'ok'")
    ok = _require_bool(data["ok"], "layout restore response.ok")

    if not ok:
        code = data.get("error_code")
        if code is not None and type(code) is not str:
            raise _schema_error(
                "layout restore response.error_code", "must be a string or null"
            )
        if data.get("error") is not None:
            _require_text(data["error"], "layout restore response.error", nonempty=False)
        error_type = _LAYOUT_RESTORE_ERROR_TYPES.get(code or "", BridgeExecutionError)
        raise error_type(_restore_error_message(data, code))

    envelope_keys = {"protocol", "request_id", "session", "action", "transport"}
    unknown = set(data) - _LAYOUT_RESTORE_KEYS - envelope_keys
    if unknown:
        raise _schema_error(
            "layout restore response", f"unsupported keys {sorted(unknown)!r}"
        )

    response_target = _cellview_from_payload(
        data.get("target"), "layout restore response.target"
    )
    if response_target != target:
        raise RoundtripMismatchError(
            f"layout restore response target {response_target.key!r} does not match {target.key!r}"
        )

    published = _require_bool(
        data.get("published", False), "layout restore response.published"
    )
    draft_cell = data.get("draft_cell")
    if draft_cell is not None:
        _require_text(draft_cell, "layout restore response.draft_cell")

    pcell_verified = _require_bool(
        data.get("pcell_verified", False), "layout restore response.pcell_verified"
    )
    connectivity_verified = _require_bool(
        data.get("connectivity_verified", False),
        "layout restore response.connectivity_verified",
    )
    readback_verified = _require_bool(
        data.get("readback_verified", False),
        "layout restore response.readback_verified",
    )

    raw_counts = data.get("object_counts", {})
    counts_map = _require_mapping(raw_counts, "layout restore response.object_counts")
    object_counts = {
        key: _require_int(val, f"layout restore response.object_counts.{key}", minimum=0)
        for key, val in sorted(counts_map.items())
    }

    pcell_mismatches = _require_sequence(
        data.get("pcell_mismatches", []), "layout restore response.pcell_mismatches"
    )
    if pcell_mismatches:
        raise RoundtripMismatchError(
            f"layout restore response pcell_mismatches: {list(pcell_mismatches)!r}"
        )

    connectivity_mismatches = _require_sequence(
        data.get("connectivity_mismatches", []),
        "layout restore response.connectivity_mismatches",
    )
    if connectivity_mismatches:
        raise RoundtripMismatchError(
            f"layout restore response connectivity_mismatches: {list(connectivity_mismatches)!r}"
        )

    details: dict[str, Any] = {}
    if "details" in data and data["details"] is not None:
        details.update(
            _require_mapping(data["details"], "layout restore response.details")
        )
    details.setdefault("pcell_mismatches", [])
    details.setdefault("connectivity_mismatches", [])
    if data.get("error") is not None:
        _require_text(data["error"], "layout restore response.error", nonempty=False)
        details["error"] = data["error"]
    if data.get("error_code") is not None:
        _require_text(data["error_code"], "layout restore response.error_code", nonempty=False)
        details["error_code"] = data["error_code"]

    return LayoutRestoreReceipt(
        target=target,
        published=published,
        draft_cell=draft_cell,
        pcell_verified=pcell_verified,
        connectivity_verified=connectivity_verified,
        readback_verified=readback_verified,
        object_counts=object_counts,
        details=details,
    )


# ---------------------------------------------------------------------------
# 4. Layout Backends (Protocol, FixtureLayoutBackend, VirtuosoLayoutBackend)
# ---------------------------------------------------------------------------


@runtime_checkable
class LayoutBackend(Protocol):
    """Narrow backend contract for OpenAccess layout capture and restore."""

    def capture_layout(
        self, cellview: CellViewRef, *, cds_lib: Path | str | None = None
    ) -> LayoutSnapshot: ...

    def restore_layout(
        self,
        snapshot: LayoutSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
    ) -> LayoutRestoreReceipt: ...


def _fixture_key(value: Any) -> CellViewRef:
    if isinstance(value, CellViewRef):
        return value
    if type(value) is str:
        return CellViewRef.parse(value)
    raise SchemaValidationError(
        "fixture keys must be CellViewRef or '<lib>/<cell>/<view>' strings"
    )


def _canonical_fixture_layout_snapshot(snapshot: LayoutSnapshot) -> LayoutSnapshot:
    if not isinstance(snapshot, LayoutSnapshot):
        raise SchemaValidationError("fixture value must be a LayoutSnapshot")
    if not isinstance(snapshot.manifest, LayoutManifest):
        raise SchemaValidationError("fixture snapshot manifest must be a LayoutManifest")
    return build_layout_snapshot(
        snapshot.manifest.cellview,
        snapshot.objects,
        cell_properties=snapshot.cell_properties,
        omissions=snapshot.omissions,
        metadata=snapshot.manifest.metadata,
    )


def _load_layout_fixture_path(path: Path) -> dict[CellViewRef, LayoutSnapshot]:
    if path.is_file():
        try:
            if path.name == "manifest.json":
                snapshot = read_layout_snapshot(path.parent, verify=True)
            else:
                with path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                snapshot = raw_layout_capture_to_snapshot(payload)
        except Exception as exc:
            if isinstance(exc, (BridgeExecutionError, FailClosedError, SchemaValidationError)):
                raise
            raise BridgeExecutionError(f"unable to load layout fixture file {path}: {exc}") from exc
        return {snapshot.manifest.cellview: snapshot}

    if not path.exists() or not path.is_dir():
        raise BridgeExecutionError(f"layout fixture directory does not exist: {path}")

    candidates: dict[CellViewRef, LayoutSnapshot] = {}

    for manifest_path in sorted(path.rglob("manifest.json")):
        snapshot_dir = manifest_path.parent
        try:
            snapshot = read_layout_snapshot(snapshot_dir, verify=True)
        except Exception as exc:
            if isinstance(exc, (BridgeExecutionError, FailClosedError, SchemaValidationError)):
                raise
            raise BridgeExecutionError(
                f"unable to load layout fixture snapshot {snapshot_dir}: {exc}"
            ) from exc
        key = snapshot.manifest.cellview
        if key in candidates:
            raise SchemaValidationError(f"duplicate layout fixture CellViewRef {key.key}")
        candidates[key] = snapshot

    for json_path in sorted(path.rglob("*.json")):
        if json_path.name in {"manifest.json", "omissions.json"}:
            continue
        try:
            with json_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeExecutionError(
                f"unable to read layout fixture JSON {json_path}: {exc}"
            ) from exc
        if (
            not isinstance(payload, Mapping)
            or "ok" not in payload
            or "cellview" not in payload
            or "objects" not in payload
        ):
            continue
        snapshot = raw_layout_capture_to_snapshot(payload)
        key = snapshot.manifest.cellview
        if key in candidates:
            raise SchemaValidationError(f"duplicate layout fixture CellViewRef {key.key}")
        candidates[key] = snapshot

    return candidates


def _invoke_failure_hook(hook: Any, operation: str, context: Mapping[str, Any]) -> None:
    if hook is None:
        return
    selected = hook
    if isinstance(hook, Mapping):
        selected = hook.get(operation)
        if selected is None:
            return
    try:
        if callable(selected):
            try:
                sig = inspect.signature(selected)
                positional = [
                    p
                    for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                has_varargs = any(p.kind is p.VAR_POSITIONAL for p in sig.parameters.values())
            except (TypeError, ValueError):
                positional, has_varargs = [], True
            if has_varargs or len(positional) >= 2:
                result = selected(operation, context)
            elif positional:
                result = selected(operation)
            else:
                result = selected()
        else:
            result = selected
    except FailClosedError:
        raise
    except Exception as exc:
        raise BridgeExecutionError(f"fixture {operation} hook failed: {exc}") from exc
    if isinstance(result, BaseException):
        if isinstance(result, FailClosedError):
            raise result
        raise BridgeExecutionError(str(result)) from result
    if result is False:
        raise BridgeExecutionError(f"fixture {operation} hook requested failure")
    if isinstance(result, str) and result:
        raise BridgeExecutionError(result)


def _invoke_readback_hook(
    hook: Any, cellview: CellViewRef, snapshot: LayoutSnapshot
) -> LayoutSnapshot:
    if hook is None:
        return snapshot
    selected = hook
    if isinstance(hook, Mapping):
        selected = hook.get(cellview, hook.get(cellview.key))
        if selected is None:
            return snapshot
    try:
        if callable(selected):
            try:
                sig = inspect.signature(selected)
                positional = [
                    p
                    for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                ]
                has_varargs = any(p.kind is p.VAR_POSITIONAL for p in sig.parameters.values())
            except (TypeError, ValueError):
                positional, has_varargs = [], True
            if has_varargs or len(positional) >= 2:
                result = selected(cellview, snapshot)
            elif positional:
                result = selected(snapshot)
            else:
                result = selected()
        else:
            result = selected
    except FailClosedError:
        raise
    except Exception as exc:
        raise BridgeExecutionError(f"fixture readback hook failed: {exc}") from exc
    if result is None:
        return snapshot
    if not isinstance(result, LayoutSnapshot):
        raise BridgeExecutionError(
            "fixture readback hook must return a LayoutSnapshot or None"
        )
    return result


class FixtureLayoutBackend:
    """Deterministic in-memory or on-disk backend implementing :class:`LayoutBackend`."""

    def __init__(
        self,
        fixtures: Mapping[Any, LayoutSnapshot] | Path | None = None,
        *,
        fixture_path: Path | None = None,
        readback_hook: Callable[..., LayoutSnapshot | None] | Mapping[Any, Any] | None = None,
        failure_hook: Callable[..., Any] | Mapping[str, Any] | Any | None = None,
    ) -> None:
        if fixtures is not None and fixture_path is not None:
            raise SchemaValidationError("provide fixtures or fixture_path, not both")
        source = fixture_path if fixture_path is not None else fixtures
        if isinstance(source, Path):
            loaded = _load_layout_fixture_path(source)
        elif source is None:
            loaded = {}
        elif isinstance(source, Mapping):
            loaded = {}
            for raw_key, raw_snapshot in source.items():
                key = _fixture_key(raw_key)
                canonical = _canonical_fixture_layout_snapshot(raw_snapshot)
                if canonical.manifest.cellview != key:
                    raise SchemaValidationError(
                        f"fixture key {key.key} does not match snapshot cellview "
                        f"{canonical.manifest.cellview.key}"
                    )
                if key in loaded:
                    raise SchemaValidationError(f"duplicate layout fixture CellViewRef {key.key}")
                loaded[key] = canonical
        else:
            raise SchemaValidationError("fixtures must be a mapping, Path, or None")
        self._snapshots: dict[CellViewRef, LayoutSnapshot] = loaded
        self.readback_hook = readback_hook
        self.failure_hook = failure_hook
        self._draft_counter = 0

    @property
    def snapshots(self) -> Mapping[CellViewRef, LayoutSnapshot]:
        return self._snapshots

    def capture_layout(
        self, cellview: CellViewRef, *, cds_lib: Path | str | None = None
    ) -> LayoutSnapshot:
        if not isinstance(cellview, CellViewRef):
            raise SchemaValidationError("cellview must be a CellViewRef")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")
        if cellview not in self._snapshots:
            raise BridgeExecutionError(f"fixture has no layout capture for {cellview.key}")
        snapshot = self._snapshots[cellview]
        _invoke_failure_hook(self.failure_hook, "capture", {"cellview": cellview})
        return _invoke_readback_hook(self.readback_hook, cellview, snapshot)

    def restore_layout(
        self,
        snapshot: LayoutSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
    ) -> LayoutRestoreReceipt:
        if not isinstance(snapshot, LayoutSnapshot):
            raise SchemaValidationError("snapshot must be a LayoutSnapshot")
        if not isinstance(target, CellViewRef):
            raise SchemaValidationError("target must be a CellViewRef")
        if cds_lib is not None and not isinstance(cds_lib, (Path, str)):
            raise SchemaValidationError("cds_lib must be a Path, str, or None")
        for field_name, value in (
            ("overwrite", overwrite),
            ("allow_omissions", allow_omissions),
            ("verify_readback", verify_readback),
        ):
            if type(value) is not bool:
                raise SchemaValidationError(f"{field_name} must be a bool")
        if oa_view_dir is not None and not isinstance(oa_view_dir, Path):
            raise SchemaValidationError("oa_view_dir must be a pathlib.Path or None")

        # Fail-Closed guard order: allowlist -> omissions -> live locks -> overwrite
        ensure_library_allowed(target.library, allow_libraries)
        ensure_no_layout_omissions(snapshot, allow_omissions=allow_omissions)
        if oa_view_dir is not None:
            ensure_no_live_locks(oa_view_dir)
        if target in self._snapshots and not overwrite:
            raise BridgeExecutionError(
                f"restore target already exists: {target.key}; pass overwrite=True"
            )

        _invoke_failure_hook(
            self.failure_hook,
            "restore",
            {"snapshot": snapshot, "target": target},
        )
        stored = build_layout_snapshot(
            target,
            snapshot.objects,
            cell_properties=snapshot.cell_properties,
            omissions=snapshot.omissions,
            metadata=snapshot.manifest.metadata,
        )
        previous = self._snapshots.get(target)
        had_previous = target in self._snapshots
        draft_cell = f"{target.cell}__oagit_fixture_draft_{self._draft_counter:04d}"
        self._draft_counter += 1

        try:
            _invoke_failure_hook(
                self.failure_hook,
                "write",
                {"snapshot": snapshot, "target": target, "draft_cell": draft_cell},
            )
            self._snapshots[target] = stored
            readback_verified = False
            if verify_readback:
                _invoke_failure_hook(
                    self.failure_hook,
                    "readback",
                    {"snapshot": snapshot, "target": target},
                )
                actual = self.capture_layout(target, cds_lib=cds_lib)
                verify_layout_roundtrip(snapshot, actual, raise_on_mismatch=True)
                readback_verified = True

            counts = dict(sorted(Counter(obj.obj_type for obj in stored.objects).items()))
            return LayoutRestoreReceipt(
                target=target,
                published=True,
                draft_cell=draft_cell,
                pcell_verified=True,
                connectivity_verified=True,
                readback_verified=readback_verified,
                object_counts=counts,
                details={
                    "backend": "fixture",
                    "source": snapshot.manifest.cellview.to_dict(),
                },
                transport="fixture",
            )
        except FailClosedError:
            if had_previous:
                self._snapshots[target] = previous  # type: ignore[assignment]
            else:
                self._snapshots.pop(target, None)
            raise
        except Exception as exc:
            if had_previous:
                self._snapshots[target] = previous  # type: ignore[assignment]
            else:
                self._snapshots.pop(target, None)
            if isinstance(exc, BridgeExecutionError):
                raise
            raise BridgeExecutionError(
                f"fixture layout restore failed for {target.key}: {exc}"
            ) from exc


class _TransientVirtuosoCrashError(BridgeExecutionError):
    """Internal signal for a retryable Virtuoso crash before response.json creation."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None,
        reason: str,
        diagnostic: str,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.reason = reason
        self.diagnostic = diagnostic


def _detect_transient_crash(
    result: subprocess.CompletedProcess[str] | None,
    run_dir: Path,
) -> tuple[bool, int | None, str]:
    returncode = getattr(result, "returncode", None) if result is not None else None
    combined_parts = [
        _result_text(result, "stderr"),
        _result_text(result, "stdout"),
        _tail(run_dir / "virtuoso.log", limit=2000),
        _tail(run_dir / "CDS.log", limit=2000),
    ]
    combined_lower = "\n".join(combined_parts).lower()

    if returncode in _TRANSIENT_CRASH_CODES:
        for pattern in _TRANSIENT_CRASH_PATTERNS:
            if pattern in combined_lower:
                return True, returncode, f"returncode={returncode} ({pattern})"
        return True, returncode, f"returncode={returncode} (SIGSEGV/SIGABRT)"

    for pattern in _TRANSIENT_CRASH_PATTERNS:
        if pattern in combined_lower:
            return True, returncode, f"matched {pattern!r} (returncode={returncode})"

    return False, returncode, ""


_POST_EXIT_HEAP_SIGNATURES = (
    "corrupted double-linked list",
    "free(): invalid",
    "double free",
    "munmap_chunk()",
)


def _is_post_exit_cxt_teardown_crash(
    result: subprocess.CompletedProcess[str] | None,
    run_dir: Path,
    payload: Mapping[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """Return True iff Virtuoso finished cleanly to ``on exit`` before a .cxt heap teardown crash."""

    if result is None or payload.get("ok") is not True:
        return False, None
    returncode = getattr(result, "returncode", 0)
    if returncode not in _TRANSIENT_CRASH_CODES:
        return False, None
    stderr_lower = _result_text(result, "stderr").lower()
    vlog_lower = _tail(run_dir / "virtuoso.log", limit=4000).lower()
    if "memory report: on exit" not in vlog_lower:
        return False, None
    matched = next(
        (sig for sig in _POST_EXIT_HEAP_SIGNATURES if sig in stderr_lower or sig in vlog_lower),
        None,
    )
    if matched is None:
        return False, None
    return True, {
        "returncode": returncode,
        "signature": matched,
        "reason": "post_exit_cxt_teardown_after_valid_response",
    }


def _call_skill_runner(
    skill_runner: Any,
    action: str,
    request: Mapping[str, Any],
    *,
    cds_lib: Path | str | None = None,
    session: Any | None = None,
) -> Mapping[str, Any]:
    fn = skill_runner.run_skill_request
    try:
        sig = inspect.signature(fn)
        accepts_session = "session" in sig.parameters or any(
            p.kind is p.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_session = True
    if accepts_session:
        return fn(action, request, cds_lib=cds_lib, session=session)
    return fn(action, request, cds_lib=cds_lib)


class VirtuosoLayoutBackend:
    """Headless Virtuoso/SKILL layout backend with optional SkillRequestRunner delegation."""

    def __init__(
        self,
        *,
        virtuoso_cmd: str | None = None,
        ssh_host: str | None = None,
        remote_workdir: str | None = None,
        timeout_s: int = 180,
        replay_flag: str = "-restore",
        runner: Runner | None = None,
        skill_dir: Path | None = None,
        keep_artifacts: bool = False,
        skill_runner: Any | None = None,
        max_sigsegv_retries: int = 1,
    ) -> None:
        if skill_runner is not None and not callable(
            getattr(skill_runner, "run_skill_request", None)
        ):
            raise SchemaValidationError(
                "skill_runner must provide a callable run_skill_request method"
            )
        if type(max_sigsegv_retries) is not int or max_sigsegv_retries < 0:
            raise SchemaValidationError("max_sigsegv_retries must be a non-negative int")

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
        self.skill_runner = skill_runner
        self.max_sigsegv_retries = max_sigsegv_retries
        self.last_run_id: str | None = None
        self.last_run_dir: Path | None = None
        self.last_retry_info: dict[str, Any] | None = None
        self.last_teardown_warning: dict[str, Any] | None = None
        self.last_transport: str = "one_shot"

    def _new_run(self) -> tuple[str, Path]:
        run_id = uuid.uuid4().hex
        run_dir = Path(tempfile.mkdtemp(prefix=f"oagit_layout_{run_id}_"))
        self.last_run_id = run_id
        self.last_run_dir = run_dir
        (run_dir / "tmp").mkdir()
        return run_id, run_dir

    def _skill_source(self, filename: str) -> Path:
        source = self.skill_dir / filename
        if not source.is_file():
            raise BridgeExecutionError(f"missing SKILL bridge file: {source}")
        return source

    def _prepare_bundle(
        self,
        run_dir: Path,
        *,
        request_text: str,
        action_name: str,
        output_json: Path,
        path_root: Path | None = None,
    ) -> dict[str, Path]:
        common = run_dir / "oagit_common.il"
        action = run_dir / action_name
        request = run_dir / "request.il"
        driver = run_dir / "driver.il"
        runtime_root = path_root or run_dir
        shutil.copy2(self._skill_source("oagit_common.il"), common)
        shutil.copy2(self._skill_source(action_name), action)
        request.write_text(request_text, encoding="utf-8")
        driver.write_text(
            build_driver_skill(
                runtime_root / common.name,
                runtime_root / request.name,
                runtime_root / action.name,
                runtime_root / output_json.name,
            ),
            encoding="utf-8",
        )
        return {
            "common": common,
            "action": action,
            "request": request,
            "driver": driver,
            "output": output_json,
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

    def _validate_cds_lib(self, cds_lib: Path | str | None, operation: str) -> Path | str:
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
            return self.runner(
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
    ) -> dict[str, Any]:
        response = run_dir / "response.json"
        if not response.is_file():
            is_crash, rc, reason = _detect_transient_crash(result, run_dir)
            diag = self._diagnostics(result, run_dir)
            suffix = f" (returncode={rc})" if rc is not None else ""
            message = (
                f"Virtuoso layout run did not produce response.json{suffix} "
                f"for {shlex.join(command)}{diag}"
            )
            if is_crash:
                raise _TransientVirtuosoCrashError(
                    message,
                    returncode=rc,
                    reason=reason,
                    diagnostic=diag.strip()[:1000],
                )
            raise BridgeExecutionError(message)
        try:
            payload = json.loads(response.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeExecutionError(
                f"invalid response.json from Virtuoso: {exc}"
                f"{self._diagnostics(result, run_dir)}"
            ) from exc
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

    def _run_local(self, run_dir: Path, cds_lib: Path | str) -> dict[str, Any]:
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
        bundle: Mapping[str, Path],
    ) -> dict[str, Any]:
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
        try:
            mkdir_result = self._invoke(ssh_mkdir, cwd=run_dir, env=local_env)
            self._check_result_code(mkdir_result, run_dir, ssh_mkdir)
            for key in ("common", "action", "request", "driver"):
                source = bundle[key]
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

            payload = self._read_response(
                run_dir,
                command_result if command_result is not None else response_result,
                command=self._ssh_command(remote_command),
            )
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
                    pass

    def _run_isolated_with_retry(
        self,
        *,
        action_name: str,
        cds_lib: Path | str,
        request_builder: Callable[[Path], str],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Run an isolated Virtuoso invocation with 1-time SIGSEGV crash retry."""

        self.last_retry_info = None
        retry_events: list[dict[str, Any]] = []
        max_attempts = 1 + self.max_sigsegv_retries

        for attempt in range(1, max_attempts + 1):
            run_id, run_dir = self._new_run()
            output_json = run_dir / "response.json"
            try:
                runtime_root = (
                    Path(self._remote_path(run_id)) if self.ssh_host else run_dir
                )
                request_text = request_builder(runtime_root / output_json.name)
                bundle = self._prepare_bundle(
                    run_dir,
                    request_text=request_text,
                    action_name=action_name,
                    output_json=output_json,
                    path_root=runtime_root,
                )
                if self.ssh_host:
                    payload = self._run_remote(run_id, run_dir, cds_lib, bundle)
                else:
                    payload = self._run_local(run_dir, cds_lib)
                retry_info = (
                    {
                        "retried": True,
                        "retry_count": len(retry_events),
                        "attempts": attempt,
                        "crash_events": retry_events,
                    }
                    if retry_events
                    else None
                )
                self.last_retry_info = retry_info
                return payload, retry_info
            except _TransientVirtuosoCrashError as exc:
                retry_events.append(
                    {
                        "attempt": attempt,
                        "run_id": run_id,
                        "returncode": exc.returncode,
                        "reason": exc.reason,
                        "diagnostic": exc.diagnostic,
                    }
                )
                if attempt >= max_attempts:
                    raise BridgeExecutionError(
                        f"{exc} (exhausted {len(retry_events)} isolated attempt(s))"
                    ) from exc
            finally:
                if not self.keep_artifacts:
                    shutil.rmtree(run_dir, ignore_errors=True)

        raise AssertionError("unreachable")  # pragma: no cover

    def capture_layout(
        self,
        cellview: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        session: Any | None = None,
    ) -> LayoutSnapshot:
        if not isinstance(cellview, CellViewRef):
            raise BridgeExecutionError("cellview must be a CellViewRef")

        if self.skill_runner is not None:
            request_dict = build_capture_layout_request_dict(cellview)
            payload = _call_skill_runner(
                self.skill_runner,
                "capture_layout",
                request_dict,
                cds_lib=cds_lib,
                session=session,
            )
            self.last_transport = str(
                payload.get("transport")
                or getattr(self.skill_runner, "last_transport", None)
                or getattr(session, "transport", None)
                or "skill_runner"
            )
            try:
                return raw_layout_capture_to_snapshot(payload)
            except FailClosedError:
                raise
            except SchemaValidationError as exc:
                raise BridgeExecutionError(f"invalid layout capture response: {exc}") from exc

        validated_cds_lib = self._validate_cds_lib(cds_lib, "capture_layout")
        payload, _ = self._run_isolated_with_retry(
            action_name="export_layout.il",
            cds_lib=validated_cds_lib,
            request_builder=lambda out_path: render_capture_layout_request_skill(
                cellview, out_path
            ),
        )
        self.last_transport = "one_shot"
        try:
            return raw_layout_capture_to_snapshot(payload)
        except FailClosedError:
            raise
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid layout capture response: {exc}") from exc

    def restore_layout(
        self,
        snapshot: LayoutSnapshot,
        target: CellViewRef,
        *,
        cds_lib: Path | str | None = None,
        allow_libraries: Iterable[str] = (),
        overwrite: bool = False,
        allow_omissions: bool = False,
        oa_view_dir: Path | None = None,
        verify_readback: bool = True,
        session: Any | None = None,
    ) -> LayoutRestoreReceipt:
        if not isinstance(snapshot, LayoutSnapshot):
            raise BridgeExecutionError("snapshot must be a LayoutSnapshot")
        if not isinstance(target, CellViewRef):
            raise BridgeExecutionError("target must be a CellViewRef")
        for field_name, value in (
            ("overwrite", overwrite),
            ("allow_omissions", allow_omissions),
            ("verify_readback", verify_readback),
        ):
            if type(value) is not bool:
                raise SchemaValidationError(f"{field_name} must be a bool")
        if oa_view_dir is not None and not isinstance(oa_view_dir, Path):
            raise SchemaValidationError("oa_view_dir must be a pathlib.Path or None")

        try:
            libraries = tuple(allow_libraries)
        except TypeError:
            libraries = ()

        # Fail-Closed guard order BEFORE launching Virtuoso or calling skill_runner:
        # 1. allowlist -> 2. omissions -> 3. live locks
        ensure_library_allowed(target.library, libraries)
        ensure_no_layout_omissions(snapshot, allow_omissions=allow_omissions)
        if oa_view_dir is not None:
            ensure_no_live_locks(oa_view_dir)

        retry_info: dict[str, Any] | None = None
        if self.skill_runner is not None:
            draft_cell = f"{target.cell}__oagit_draft_{uuid.uuid4().hex[:10]}"
            request_dict = build_restore_layout_request_dict(
                snapshot,
                target,
                draft_cell=draft_cell,
                overwrite=overwrite,
                allow_libraries=libraries,
            )
            payload = _call_skill_runner(
                self.skill_runner,
                "restore_layout",
                request_dict,
                cds_lib=cds_lib,
                session=session,
            )
            transport = str(
                payload.get("transport")
                or getattr(self.skill_runner, "last_transport", None)
                or getattr(session, "transport", None)
                or "skill_runner"
            )
            self.last_transport = transport
        else:
            validated_cds_lib = self._validate_cds_lib(cds_lib, "restore_layout")

            def _build_attempt_request(out_path: Path) -> str:
                attempt_draft = f"{target.cell}__oagit_draft_{uuid.uuid4().hex[:10]}"
                return render_restore_layout_request_skill(
                    snapshot,
                    target,
                    draft_cell=attempt_draft,
                    overwrite=overwrite,
                    allow_libraries=libraries,
                    output_path=out_path,
                )

            payload, retry_info = self._run_isolated_with_retry(
                action_name="restore_layout.il",
                cds_lib=validated_cds_lib,
                request_builder=_build_attempt_request,
            )
            transport = "one_shot"

        try:
            receipt = parse_layout_restore_response(payload, target)
        except FailClosedError:
            raise
        except SchemaValidationError as exc:
            raise BridgeExecutionError(f"invalid layout restore response: {exc}") from exc

        merged_details = dict(receipt.details)
        if retry_info is not None:
            merged_details["retry"] = retry_info
        receipt = replace(receipt, details=merged_details, transport=transport)

        if not verify_readback:
            return receipt

        actual = self.capture_layout(target, cds_lib=cds_lib, session=session)
        verify_layout_roundtrip(snapshot, actual, raise_on_mismatch=True)
        return replace(receipt, readback_verified=True)


def load_layout_backend(
    name: str,
    *,
    fixture_path: Path | None = None,
    virtuoso_cmd: str | None = None,
    ssh_host: str | None = None,
    remote_workdir: str | None = None,
    timeout_s: int = 180,
    skill_runner: Any | None = None,
) -> LayoutBackend:
    """Instantiate either ``FixtureLayoutBackend`` or ``VirtuosoLayoutBackend``."""

    if type(name) is not str:
        raise BridgeExecutionError("backend name must be a string")
    backend_name = name.strip().lower()
    if backend_name == "fixture":
        return FixtureLayoutBackend(fixture_path=fixture_path)
    if backend_name == "virtuoso":
        return VirtuosoLayoutBackend(
            virtuoso_cmd=virtuoso_cmd,
            ssh_host=ssh_host,
            remote_workdir=remote_workdir,
            timeout_s=timeout_s,
            skill_runner=skill_runner,
        )
    raise BridgeExecutionError(
        f"unknown layout backend {name!r}; expected 'fixture' or 'virtuoso'"
    )


__all__ = [
    "FixtureLayoutBackend",
    "LayoutBackend",
    "LayoutHandler",
    "LayoutRoundtripReport",
    "VirtuosoLayoutBackend",
    "build_capture_layout_request_dict",
    "build_restore_layout_request_dict",
    "ensure_library_allowed",
    "ensure_no_layout_omissions",
    "ensure_no_live_locks",
    "load_layout_backend",
    "parse_layout_restore_response",
    "probe_locks",
    "raw_layout_capture_to_snapshot",
    "render_capture_layout_request_skill",
    "render_restore_layout_request_skill",
    "verify_layout_roundtrip",
]
