"""用私有真值评估公开输入路由，保持路由与答案判定隔离。"""

import argparse
import json
from pathlib import Path

from trust3d.data.select_events import read_jsonl


ROUTES = {"trust_memory", "reobserve"}
FORBIDDEN_ROUTE_KEYS = {
    "branch",
    "current_answer",
    "historical_answer",
    "memory_is_stale",
    "predicted_answer",
}


def _atomic_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def _prediction(route, oracle):
    selected_route = route["route"]
    if selected_route not in ROUTES:
        raise ValueError("未知 route: {}".format(selected_route))
    answer = (
        oracle["historical_answer"]
        if selected_route == "trust_memory"
        else oracle["current_answer"]
    )
    reobserved = selected_route == "reobserve"
    return {
        "episode_id": oracle["episode_id"],
        "group_id": oracle["group_id"],
        "branch": oracle["branch"],
        "method": route["method"],
        "policy_id": route["policy_id"],
        "route": selected_route,
        "predicted_answer": answer,
        "current_answer": oracle["current_answer"],
        "historical_answer": oracle["historical_answer"],
        "correct": answer == oracle["current_answer"],
        "memory_is_stale": oracle["memory_is_stale"],
        "new_observation_count": 1 if reobserved else 0,
        "move_steps": oracle["shortest_verification_cost"] if reobserved else 0,
        "verification_cost": (
            oracle["shortest_verification_cost"] if reobserved else 0
        ),
        "shortest_verification_cost": oracle["shortest_verification_cost"],
        "cost_weight": route.get("cost_weight"),
    }


def evaluate(routes_path, oracle_path, output_path, include_clairvoyant=False):
    routes = read_jsonl(routes_path)
    oracle = read_jsonl(oracle_path)
    oracle_by_id = {item["episode_id"]: item for item in oracle}
    if len(oracle_by_id) != len(oracle):
        raise ValueError("私有 oracle episode_id 必须唯一")

    route_keys = set()
    predictions = []
    for route in routes:
        leaked = sorted(set(route) & FORBIDDEN_ROUTE_KEYS)
        if leaked:
            raise ValueError("路由输出包含私有字段: {}".format(", ".join(leaked)))
        episode_id = route["episode_id"]
        if episode_id not in oracle_by_id:
            raise ValueError("路由缺少对应 oracle: {}".format(episode_id))
        key = (route["policy_id"], episode_id)
        if key in route_keys:
            raise ValueError("重复的 policy/episode 路由: {}".format(key))
        route_keys.add(key)
        predictions.append(_prediction(route, oracle_by_id[episode_id]))

    policies = sorted({item["policy_id"] for item in routes})
    for policy_id in policies:
        policy_ids = {
            item["episode_id"] for item in routes if item["policy_id"] == policy_id
        }
        if policy_ids != set(oracle_by_id):
            raise ValueError("policy {} 未覆盖全部 episode".format(policy_id))

    if include_clairvoyant:
        for item in oracle:
            route = {
                "episode_id": item["episode_id"],
                "method": "clairvoyant_oracle",
                "policy_id": "clairvoyant_oracle",
                "route": "reobserve" if item["memory_is_stale"] else "trust_memory",
            }
            predictions.append(_prediction(route, item))

    predictions.sort(key=lambda item: (item["policy_id"], item["episode_id"]))
    _atomic_jsonl(output_path, predictions)
    return {
        "oracle_episode_count": len(oracle),
        "prediction_count": len(predictions),
        "policy_ids": sorted({item["policy_id"] for item in predictions}),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--include-clairvoyant", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = evaluate(
        args.routes,
        args.oracle,
        args.output,
        include_clairvoyant=args.include_clairvoyant,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
