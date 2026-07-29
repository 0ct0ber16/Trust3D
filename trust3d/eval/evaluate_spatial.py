"""评估 Gate 6 空间记忆方法、基线和预先声明的通过标准。"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from trust3d.data.build_branches import _atomic_json, _atomic_jsonl
from trust3d.data.select_events import read_jsonl


PRIMARY_ROUTE = "trust3d_lambda_0.01"
FORBIDDEN_ROUTE_KEYS = {
    "branch",
    "current_answer_gt",
    "current_answer_rgbd",
    "historical_answer_gt",
    "historical_answer_rgbd",
    "memory_is_stale",
    "simulator_oracle_answer",
}


def _prediction(
    oracle,
    method,
    answer,
    reobserve=False,
    oracle_method=False,
):
    observations = oracle["required_new_observations"] if reobserve else 0
    movement = oracle["shortest_verification_cost"] if reobserve else 0
    return {
        "episode_id": oracle["episode_id"],
        "group_id": oracle["group_id"],
        "branch": oracle["branch"],
        "question_type": oracle["question_type"],
        "method": method,
        "answer": answer,
        "correct": answer == oracle["current_answer_gt"],
        "new_observation_count": 0 if oracle_method else observations,
        "movement_steps": 0 if oracle_method else movement,
        "memory_is_stale": oracle["memory_is_stale"],
    }


def _build_predictions(oracle, primary_route):
    reobserve = primary_route["route"] == "reobserve"
    return [
        _prediction(oracle, "current_rgb", "unknown"),
        _prediction(oracle, "all_history_rgb", "unknown"),
        _prediction(
            oracle,
            "persistent_3d_gt",
            oracle["historical_answer_gt"],
        ),
        _prediction(
            oracle,
            "persistent_3d_rgbd",
            oracle["historical_answer_rgbd"],
        ),
        _prediction(
            oracle,
            "always_reobserve_gt",
            oracle["current_answer_gt"],
            reobserve=True,
        ),
        _prediction(
            oracle,
            "always_reobserve_rgbd",
            oracle["current_answer_rgbd"],
            reobserve=True,
        ),
        _prediction(
            oracle,
            "trust3d_gt",
            oracle["current_answer_gt"]
            if reobserve
            else oracle["historical_answer_gt"],
            reobserve=reobserve,
        ),
        _prediction(
            oracle,
            "trust3d_rgbd",
            oracle["current_answer_rgbd"]
            if reobserve
            else oracle["historical_answer_rgbd"],
            reobserve=reobserve,
        ),
        _prediction(
            oracle,
            "clairvoyant_oracle",
            oracle["current_answer_gt"],
            oracle_method=True,
        ),
    ]


def _metrics(predictions):
    grouped = defaultdict(list)
    for item in predictions:
        grouped[item["method"]].append(item)
    values = {}
    for method, records in sorted(grouped.items()):
        stale = [item for item in records if item["memory_is_stale"]]
        values[method] = {
            "episode_count": len(records),
            "accuracy": sum(item["correct"] for item in records) / len(records),
            "stale_accuracy": sum(item["correct"] for item in stale) / len(stale),
            "new_observation_count": sum(
                item["new_observation_count"] for item in records
            ),
            "movement_steps": sum(item["movement_steps"] for item in records),
        }
    return values


def evaluate(public_path, private_path, routes_path, predictions_path, report_path):
    public = read_jsonl(public_path)
    private = read_jsonl(private_path)
    routes = read_jsonl(routes_path)
    public_ids = {item["episode_id"] for item in public}
    private_by_id = {item["episode_id"]: item for item in private}
    if len(public_ids) != len(public) or len(private_by_id) != len(private):
        raise ValueError("Gate 6 episode_id 必须唯一")
    if public_ids != set(private_by_id):
        raise ValueError("Gate 6 公开 episode 与私有真值不一一对应")

    primary_routes = {}
    for route in routes:
        leaked = sorted(set(route) & FORBIDDEN_ROUTE_KEYS)
        if leaked:
            raise ValueError("路由输出包含私有字段: {}".format(", ".join(leaked)))
        if route["policy_id"] == PRIMARY_ROUTE:
            primary_routes[route["episode_id"]] = route
    if set(primary_routes) != public_ids:
        raise ValueError("Gate 6 主策略路由未覆盖全部 episode")

    predictions = []
    for episode_id in sorted(public_ids):
        predictions.extend(
            _build_predictions(private_by_id[episode_id], primary_routes[episode_id])
        )
    predictions.sort(key=lambda item: (item["method"], item["episode_id"]))
    _atomic_jsonl(predictions_path, predictions)
    metrics = _metrics(predictions)

    tool_error_rate = sum(
        item["deterministic_tool_answer"] != item["simulator_oracle_answer"]
        for item in private
    ) / len(private)
    current_accuracy = metrics["current_rgb"]["accuracy"]
    gt_gain = metrics["trust3d_gt"]["accuracy"] - current_accuracy
    rgbd_gain = metrics["trust3d_rgbd"]["accuracy"] - current_accuracy
    always_observations = metrics["always_reobserve_gt"]["new_observation_count"]
    trust_observations = metrics["trust3d_gt"]["new_observation_count"]
    observation_saving = 1.0 - trust_observations / always_observations
    stale_error_reduction_gt = (
        metrics["trust3d_gt"]["stale_accuracy"]
        - metrics["persistent_3d_gt"]["stale_accuracy"]
    )
    stale_error_reduction_rgbd = (
        metrics["trust3d_rgbd"]["stale_accuracy"]
        - metrics["persistent_3d_rgbd"]["stale_accuracy"]
    )
    criteria = {
        "gt_gain_at_least_10pp": gt_gain >= 0.10,
        "rgbd_gain_at_least_10pp": rgbd_gain >= 0.10,
        "observation_saving_at_least_25pct": observation_saving >= 0.25,
        "egocentric_tool_error_below_2pct": tool_error_rate < 0.02,
        "gt_stale_error_reduction_at_least_10pp": stale_error_reduction_gt >= 0.10,
        "rgbd_stale_error_reduction_at_least_10pp": stale_error_reduction_rgbd
        >= 0.10,
    }
    report = {
        "schema_version": 1,
        "gate6_pass": all(criteria.values()),
        "episode_count": len(private),
        "group_count": len({item["group_id"] for item in private}),
        "metrics": metrics,
        "gt_gain_over_current_rgb": gt_gain,
        "rgbd_gain_over_current_rgb": rgbd_gain,
        "observation_saving_vs_always_reobserve": observation_saving,
        "egocentric_tool_error_rate": tool_error_rate,
        "gt_stale_error_reduction": stale_error_reduction_gt,
        "rgbd_stale_error_reduction": stale_error_reduction_rgbd,
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
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = evaluate(
        args.public, args.private, args.routes, args.predictions, args.output
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["gate6_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
