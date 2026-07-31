"""Top-level protocol freeze, status, supervision, and reporting."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from trust3d.agents.evidence import validate_packet
from trust3d.parallel_v2.common import (
    CHECKPOINT_ROOT,
    OUTPUT_ROOT,
    ROOT,
    atomic_bytes,
    atomic_json,
    baseline_manifest_path,
    cpu_admitted,
    load_json,
    mark_stage,
    path_manifest,
    protocol,
    repository_commit,
    repository_dirty,
    resource_snapshot,
    sha256_file,
    stage_complete,
    utc_now,
    verify_baseline,
    write_status,
)


BASELINE_PATHS = (
    "Trust3D_服务器最小可执行实验方案.md",
    "Trust3D_服务器最小可执行实验方案2.md",
    "Trust3D_最小可执行实验详细报告.md",
    "Trust3D_服务器最小可执行实验方案2详细报告.md",
    "Trust3D_Gate7_CUT3R_VGGT失败原因诊断报告.md",
    "outputs/gate0",
    "outputs/gate2",
    "outputs/gate3",
    "outputs/gate4",
    "outputs/gate5",
    "outputs/gate6",
    "outputs/gate7",
    "outputs/plan2",
    "outputs/gate7_diagnosis",
)


def preflight():
    if not os.environ.get("TMUX"):
        raise RuntimeError("parallel-v2 must run inside tmux")
    snapshot = resource_snapshot()
    admitted = cpu_admitted(snapshot)
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "complete": admitted,
        "tmux": os.environ.get("TMUX"),
        "tmux_pane": os.environ.get("TMUX_PANE"),
        "cwd": str(Path.cwd()),
        "git_commit": repository_commit(),
        "git_dirty": repository_dirty(),
        "resources": snapshot,
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT_ROOT / "protocol/preflight.json", value)
    if not admitted:
        raise RuntimeError("CPU, memory, or disk admission failed")
    return value


def protocol_check():
    preflight()
    manifest_path = baseline_manifest_path()
    if manifest_path.is_file():
        checked = verify_baseline()
    else:
        files = path_manifest(BASELINE_PATHS)
        atomic_json(
            manifest_path,
            {
                "schema_version": 1,
                "protocol_revision": "parallel-v2",
                "created_at": utc_now(),
                "files": files,
            },
        )
        checked = verify_baseline()
    protocol_value = protocol()
    if tuple(protocol_value["route_tie_break"]) != (
        "USE_CURRENT_VIEW",
        "RETRIEVE_HISTORY",
        "QUERY_3D_MEMORY",
        "REOBSERVE",
        "ABSTAIN",
    ):
        raise ValueError("route enum/order changed")
    sample_packet = {
        "schema_version": 1,
        "episode_id": "protocol",
        "query_id": "protocol",
        "object_id": "object",
        "predicate": "attribute",
        "value": True,
        "source": "history",
        "observed_at": 0,
        "valid_until": 1,
        "reference_frame": "world",
        "pose_convention": "camera_to_world",
        "confidence": 0.99,
        "is_observed": True,
        "provenance": [],
        "cost": {
            "move_steps": 0,
            "new_observations": 0,
            "vlm_calls": 0,
            "geometry_calls": 0,
            "wall_seconds": 0.0,
        },
    }
    validate_packet(sample_packet)
    private_directories = [
        ROOT / "data/episodes/parallel_v2/gt5",
        ROOT / "data/episodes/parallel_v2/gate7_fix",
    ]
    for directory in private_directories:
        if directory.exists() and ROOT not in directory.resolve().parents:
            raise RuntimeError(f"parallel-v2 data escaped workspace: {directory}")
    result = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "complete": True,
        "baseline_file_count": len(checked),
        "baseline_all_match": all(item["match"] for item in checked),
        "evidence_packet_contract": True,
        "public_private_contract": True,
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT_ROOT / "protocol/validation.json", result)
    mark_stage(
        "baseline_manifest",
        "complete",
        [manifest_path],
        "不可覆盖基线已冻结并验证。",
        "protocol",
    )
    mark_stage(
        "protocol",
        "complete",
        [OUTPUT_ROOT / "protocol/validation.json"],
        "共同协议与公开/私有边界通过。",
        "freeze",
    )
    return result


def _source_paths():
    roots = [ROOT / "configs", ROOT / "trust3d", ROOT / "tests", ROOT / "scripts"]
    paths = []
    for root in roots:
        paths.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and not path.name.endswith((".pyc", ".exit"))
        )
    return sorted(paths)


def freeze():
    if not stage_complete("protocol"):
        raise RuntimeError("protocol stage is not complete")
    verify_baseline()
    if repository_dirty():
        raise RuntimeError("reviewed source must be committed before freeze")
    files = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in _source_paths()
    ]
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "created_at": utc_now(),
        "git_commit": repository_commit(),
        "git_dirty": False,
        "files": files,
        "protocol_sha256": sha256_file(ROOT / "configs/parallel_v2_protocol.json"),
    }
    atomic_json(OUTPUT_ROOT / "protocol/code_lock.json", value)
    mark_stage(
        "freeze",
        "complete",
        [OUTPUT_ROOT / "protocol/code_lock.json"],
        "代码、配置和协议已冻结。",
        "start",
    )
    return value


def verify_code_lock():
    lock = load_json(OUTPUT_ROOT / "protocol/code_lock.json")
    mismatches = []
    for record in lock["files"]:
        path = ROOT / record["path"]
        actual = sha256_file(path) if path.is_file() else None
        if actual != record["sha256"]:
            mismatches.append(
                {"path": record["path"], "expected": record["sha256"], "actual": actual}
            )
    if mismatches:
        raise RuntimeError("code lock mismatch: " + json.dumps(mismatches, ensure_ascii=False))
    return {"complete": True, "file_count": len(lock["files"]), "checked_at": utc_now()}


def status():
    components = {}
    status_root = OUTPUT_ROOT / "status"
    for name in ("orchestrator", "gt_five_route", "gate7_fix", "integration"):
        path = status_root / f"{name}.json"
        components[name] = load_json(path) if path.is_file() else {"state": "not_started"}
    stages = {}
    stage_root = CHECKPOINT_ROOT / "stages"
    if stage_root.is_dir():
        for path in sorted(stage_root.glob("*.json")):
            stages[path.stem] = load_json(path)
    value = {
        "schema_version": 1,
        "captured_at": utc_now(),
        "components": components,
        "stages": stages,
        "resources": resource_snapshot(),
    }
    atomic_json(OUTPUT_ROOT / "status.json", value)
    return value


def report():
    a_path = OUTPUT_ROOT / "gt_five_route/report.json"
    b_path = OUTPUT_ROOT / "gate7_fix/report.json"
    c_path = OUTPUT_ROOT / "integration/report.json"
    lines = {
        "a": load_json(a_path) if a_path.is_file() else {"status": "not_complete", "complete": False},
        "b": load_json(b_path) if b_path.is_file() else {"status": "not_complete", "complete": False},
        "c": load_json(c_path) if c_path.is_file() else {"status": "not_run", "complete": False},
    }
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "generated_at": utc_now(),
        "gt_five_route": lines["a"],
        "gate7_fix": lines["b"],
        "integration": lines["c"],
        "idea_controller_validated": bool(lines["a"].get("complete")),
        "gate7_repaired": bool(lines["b"].get("complete")),
        "end_to_end_validated": bool(lines["c"].get("complete")),
    }
    atomic_json(OUTPUT_ROOT / "final_decision.json", value)
    report_lines = [
        "# Trust3D GT 五路与 RGB 几何联合实验总报告",
        "",
        f"- GT 五路控制实验：`{lines['a'].get('status')}`",
        f"- Gate 7 修复：`{lines['b'].get('status')}`",
        f"- RGB 五路联合实验：`{lines['c'].get('status')}`",
        "",
        "## 结论",
        "",
        "三条结论分别记账：GT 控制器上界、RGB 几何修复和端到端联合结果互不替代。联合实验失败不会倒推否定已经通过的 GT 控制实验。",
        "",
    ]
    atomic_bytes(
        ROOT / "Trust3D_GT五路与RGB几何联合实验报告.md",
        "\n".join(report_lines).encode("utf-8"),
    )
    return value


def supervise():
    write_status("orchestrator", "running", "持续监控 A/B 完成状态。")
    while True:
        verify_baseline()
        a_path = OUTPUT_ROOT / "gt_five_route/report.json"
        b_path = OUTPUT_ROOT / "gate7_fix/report.json"
        if a_path.is_file() and b_path.is_file():
            a = load_json(a_path)
            b = load_json(b_path)
            if a.get("complete") and b.get("complete"):
                write_status("orchestrator", "launching_integration", "A/B 均通过，启动 C 线。")
                return 10
            report()
            write_status(
                "orchestrator",
                "complete_without_integration",
                "A/B 至少一线未通过预注册准入，按协议不运行 C 线。",
            )
            return 0
        time.sleep(5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=["preflight", "protocol", "freeze", "verify-freeze", "status", "supervise", "report"],
    )
    args = parser.parse_args(argv)
    functions = {
        "preflight": preflight,
        "protocol": protocol_check,
        "freeze": freeze,
        "verify-freeze": verify_code_lock,
        "status": status,
        "supervise": supervise,
        "report": report,
    }
    result = functions[args.mode]()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
