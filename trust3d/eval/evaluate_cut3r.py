"""评估 Gate 7 CUT3R 几何的 QA、失败率、成本和几何一致性。"""

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from trust3d.data.build_branches import _atomic_json, _atomic_jsonl
from trust3d.data.select_events import read_jsonl
from trust3d.eval.evaluate_spatial import FORBIDDEN_ROUTE_KEYS, PRIMARY_ROUTE


def _geometry_groups(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    groups = {}
    for item in manifest["groups"]:
        path = Path(item["checkpoint"])
        value = json.loads(path.read_text(encoding="utf-8"))
        groups[item["group_id"]] = value
    return manifest, groups


def _source_contexts(root):
    values = {}
    for path in sorted(Path(root).glob("*/context.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("status") == "success":
            values[item["group_id"]] = item
    return values


def _current_scenario(oracle):
    return "stale_reobserve" if oracle["branch"] == "risk_stale" else "stable_reobserve"


def _geometry_answer(geometry, scenario, question_type):
    if not geometry or geometry.get("status") != "success":
        return "unknown", True
    try:
        return geometry[scenario]["answers"][question_type], False
    except (KeyError, TypeError):
        return "unknown", True


def _prediction(oracle, method, answer, failure, reobserve=False):
    return {
        "episode_id": oracle["episode_id"],
        "group_id": oracle["group_id"],
        "branch": oracle["branch"],
        "question_type": oracle["question_type"],
        "method": method,
        "answer": answer,
        "correct": answer == oracle["current_answer_gt"],
        "geometry_failure": bool(failure),
        "new_observation_count": oracle["required_new_observations"]
        if reobserve
        else 0,
        "movement_steps": oracle["shortest_verification_cost"] if reobserve else 0,
        "memory_is_stale": oracle["memory_is_stale"],
    }


def _build_predictions(oracle, route, geometry):
    question_type = oracle["question_type"]
    historical, historical_failure = _geometry_answer(
        geometry, "historical", question_type
    )
    current, current_failure = _geometry_answer(
        geometry, _current_scenario(oracle), question_type
    )
    reobserve = route["route"] == "reobserve"
    routed = current if reobserve else historical
    routed_failure = current_failure if reobserve else historical_failure
    gt_answer = (
        oracle["current_answer_gt"] if reobserve else oracle["historical_answer_gt"]
    )
    return [
        _prediction(
            oracle,
            "persistent_3d_cut3r",
            historical,
            historical_failure,
        ),
        _prediction(
            oracle,
            "always_reobserve_cut3r",
            current,
            current_failure,
            reobserve=True,
        ),
        _prediction(
            oracle,
            "trust3d_cut3r",
            routed,
            routed_failure,
            reobserve=reobserve,
        ),
        _prediction(
            oracle,
            "trust3d_gt_reference",
            gt_answer,
            False,
            reobserve=reobserve,
        ),
        _prediction(
            oracle,
            "trust3d_rgbd_reference",
            oracle["current_answer_rgbd"]
            if reobserve
            else oracle["historical_answer_rgbd"],
            False,
            reobserve=reobserve,
        ),
    ]


def _metrics(predictions):
    grouped = defaultdict(list)
    for item in predictions:
        grouped[item["method"]].append(item)
    result = {}
    for method, records in sorted(grouped.items()):
        stale = [item for item in records if item["memory_is_stale"]]
        result[method] = {
            "episode_count": len(records),
            "accuracy": sum(item["correct"] for item in records) / len(records),
            "stale_accuracy": sum(item["correct"] for item in stale) / len(stale),
            "geometry_failure_count": sum(
                item["geometry_failure"] for item in records
            ),
            "new_observation_count": sum(
                item["new_observation_count"] for item in records
            ),
            "movement_steps": sum(item["movement_steps"] for item in records),
        }
    return result


def _translation(pose):
    value = np.asarray(pose, dtype=np.float64)
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError("无效相机位姿")
    return value[:3, 3]


def camera_revisit_errors(group):
    """返回相机在稳定和交换序列中的归一化重访漂移。"""
    values = []
    for scenario, pairs in (
        ("stable", ((0, 3), (1, 4))),
        ("stale", ((1, 3), (0, 4))),
    ):
        poses = group["camera_trajectories"][scenario]
        translations = [_translation(pose) for pose in poses]
        scale = float(np.linalg.norm(translations[0] - translations[1]))
        if not math.isfinite(scale) or scale <= 1e-8:
            raise ValueError("CUT3R 相机基线退化")
        values.extend(
            float(np.linalg.norm(translations[first] - translations[second]) / scale)
            for first, second in pairs
        )
    return values


def movable_object_revisit_errors(group):
    """用稳定重访和位置交换检查居中物体点的一致性。"""

    def point(stage, role):
        return np.asarray(stage[role]["world"], dtype=np.float64)

    values = []
    historical = group["historical"]
    stable = group["stable_reobserve"]
    stable_scale = float(
        np.linalg.norm(point(historical, "target") - point(historical, "donor"))
    )
    if stable_scale <= 1e-8:
        raise ValueError("稳定序列物体基线退化")
    values.extend(
        [
            float(
                np.linalg.norm(point(stable, "target") - point(historical, "target"))
                / stable_scale
            ),
            float(
                np.linalg.norm(point(stable, "donor") - point(historical, "donor"))
                / stable_scale
            ),
        ]
    )

    stale_history = group["stale_sequence_historical"]
    stale = group["stale_reobserve"]
    stale_scale = float(
        np.linalg.norm(
            point(stale_history, "target") - point(stale_history, "donor")
        )
    )
    if stale_scale <= 1e-8:
        raise ValueError("变化序列物体基线退化")
    values.extend(
        [
            float(
                np.linalg.norm(
                    point(stale, "target") - point(stale_history, "donor")
                )
                / stale_scale
            ),
            float(
                np.linalg.norm(
                    point(stale, "donor") - point(stale_history, "target")
                )
                / stale_scale
            ),
        ]
    )
    return values


def _distribution(values):
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return {"count": 0, "mean": None, "median": None, "p95": None}
    return {
        "count": int(values.size),
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "p95": float(np.quantile(values, 0.95)),
    }


def _geometry_diagnostics(groups):
    camera_errors = []
    object_errors = []
    diagnostic_failures = []
    successful = []
    for group_id, group in sorted(groups.items()):
        if group.get("status") != "success":
            continue
        try:
            camera_errors.extend(camera_revisit_errors(group))
            object_errors.extend(movable_object_revisit_errors(group))
            successful.append(group)
        except (KeyError, TypeError, ValueError) as error:
            diagnostic_failures.append(
                {"group_id": group_id, "error": str(error)}
            )
    stable_seconds = [
        item["timing"]["stable_sequence_seconds"] for item in successful
    ]
    stale_seconds = [
        item["timing"]["stale_sequence_seconds"] for item in successful
    ]
    reused_seconds = sum(stable_seconds) + sum(stale_seconds)
    per_question_rerun = 8 * sum(stable_seconds) + 4 * sum(stale_seconds)
    episode_count = 12 * len(successful)
    return {
        "static_structure_camera_revisit_error": _distribution(camera_errors),
        "movable_object_revisit_error": _distribution(object_errors),
        "diagnostic_failures": diagnostic_failures,
        "latency": {
            "stable_sequence_seconds": _distribution(stable_seconds),
            "stale_sequence_seconds": _distribution(stale_seconds),
            "state_reuse_total_seconds": reused_seconds,
            "amortized_seconds_per_question": reused_seconds / episode_count
            if episode_count
            else None,
            "per_question_rerun_estimated_seconds": per_question_rerun,
            "state_reuse_time_saving": 1.0 - reused_seconds / per_question_rerun
            if per_question_rerun
            else None,
        },
        "peak_allocated_bytes": max(
            (item["peak_allocated_bytes"] for item in successful), default=None
        ),
    }


def _object_type_metrics(predictions, contexts):
    by_id = {
        item["episode_id"]: item
        for item in predictions
        if item["method"] == "trust3d_cut3r"
    }
    grouped = defaultdict(list)
    for item in by_id.values():
        context = contexts.get(item["group_id"], {})
        object_type = context.get("target_object_type") or "unknown"
        grouped[object_type].append(item)
    return {
        object_type: {
            "episode_count": len(records),
            "error_rate": 1.0
            - sum(item["correct"] for item in records) / len(records),
        }
        for object_type, records in sorted(grouped.items())
    }


def evaluate(
    public_path,
    private_path,
    routes_path,
    geometry_root,
    source_checkpoints,
    predictions_path,
    report_path,
):
    public = read_jsonl(public_path)
    private = read_jsonl(private_path)
    routes = read_jsonl(routes_path)
    public_ids = {item["episode_id"] for item in public}
    private_by_id = {item["episode_id"]: item for item in private}
    if len(public_ids) != len(public) or len(private_by_id) != len(private):
        raise ValueError("Gate 7 episode_id 必须唯一")
    if public_ids != set(private_by_id):
        raise ValueError("Gate 7 公开 episode 与私有真值不一一对应")

    primary_routes = {}
    for route in routes:
        leaked = sorted(set(route) & FORBIDDEN_ROUTE_KEYS)
        if leaked:
            raise ValueError("路由输出包含私有字段: {}".format(", ".join(leaked)))
        if route["policy_id"] == PRIMARY_ROUTE:
            primary_routes[route["episode_id"]] = route
    if set(primary_routes) != public_ids:
        raise ValueError("Gate 7 主策略路由未覆盖全部 episode")

    manifest, geometry = _geometry_groups(geometry_root)
    contexts = _source_contexts(source_checkpoints)
    predictions = []
    for episode_id in sorted(public_ids):
        oracle = private_by_id[episode_id]
        predictions.extend(
            _build_predictions(
                oracle,
                primary_routes[episode_id],
                geometry.get(oracle["group_id"]),
            )
        )
    predictions.sort(key=lambda item: (item["method"], item["episode_id"]))
    _atomic_jsonl(predictions_path, predictions)
    metrics = _metrics(predictions)

    cut3r_accuracy = metrics["trust3d_cut3r"]["accuracy"]
    gt_accuracy = metrics["trust3d_gt_reference"]["accuracy"]
    qa_drop = gt_accuracy - cut3r_accuracy
    failed_groups = sum(
        item.get("status") != "success" for item in geometry.values()
    )
    group_failure_rate = failed_groups / len(geometry) if geometry else 1.0
    diagnostics = _geometry_diagnostics(geometry)
    if qa_drop <= 0.10:
        judgment = "可作为完整系统主结果"
    elif qa_drop <= 0.20:
        judgment = "保留为现实设置，并明确 backbone gap 限制"
    else:
        judgment = "主因果实验继续使用 GT/RGB-D，CUT3R 仅作失败分析"
    criteria = {
        "qa_drop_not_above_10pp": qa_drop <= 0.10,
        "all_requested_groups_completed": manifest.get("complete") is True,
        "geometry_group_failure_rate_zero": group_failure_rate == 0.0,
    }
    report = {
        "schema_version": 1,
        "gate7_pass": all(criteria.values()),
        "judgment": judgment,
        "episode_count": len(private),
        "group_count": len({item["group_id"] for item in private}),
        "checkpoint_sha256": manifest["checkpoint_sha256"],
        "cut3r_adapter_version": manifest["adapter_version"],
        "frame_sampling": "每组历史 target/donor、查询帧、当前 target/donor；512 长边；RGB-only",
        "grounding_boundary": "验证帧角色和目标居中先验；未读取 depth、instance mask 或私有答案。",
        "metrics": metrics,
        "gt_3d_accuracy": gt_accuracy,
        "cut3r_accuracy": cut3r_accuracy,
        "gt_to_cut3r_qa_drop": qa_drop,
        "camera_geometry_group_failure_rate": group_failure_rate,
        "query_geometry_failure_rate": metrics["trust3d_cut3r"][
            "geometry_failure_count"
        ]
        / len(private),
        "geometry_diagnostics": diagnostics,
        "movable_object_error_by_type": _object_type_metrics(
            predictions, contexts
        ),
        "criteria": criteria,
        "route_private_leak_count": 0,
    }
    _atomic_json(report_path, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", required=True, type=Path)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--geometry", required=True, type=Path)
    parser.add_argument(
        "--source-checkpoints",
        type=Path,
        default=Path("data/episodes/spatial30/checkpoints"),
    )
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = evaluate(
        args.public,
        args.private,
        args.routes,
        args.geometry,
        args.source_checkpoints,
        args.predictions,
        args.output,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["gate7_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
