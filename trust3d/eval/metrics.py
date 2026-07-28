"""计算 Gate 4 指标、成组 bootstrap 置信区间和验收结论。"""

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from trust3d.data.select_events import read_jsonl


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _mean(values):
    return sum(values) / float(len(values)) if values else None


def _rate(numerator, denominator):
    return numerator / float(denominator) if denominator else None


def _policy_metrics(records, utility_weights):
    stale = [item for item in records if item["memory_is_stale"]]
    sufficient = [item for item in records if not item["memory_is_stale"]]
    reobserved = [item for item in records if item["route"] == "reobserve"]
    branch_metrics = {}
    for branch in sorted({item["branch"] for item in records}):
        subset = [item for item in records if item["branch"] == branch]
        branch_metrics[branch] = {
            "episode_count": len(subset),
            "accuracy": _mean([int(item["correct"]) for item in subset]),
            "reobserve_rate": _mean(
                [int(item["route"] == "reobserve") for item in subset]
            ),
            "average_verification_cost": _mean(
                [item["verification_cost"] for item in subset]
            ),
        }
    return {
        "episode_count": len(records),
        "answer_accuracy": _mean([int(item["correct"]) for item in records]),
        "stale_memory_error_rate": _rate(
            sum(
                not item["correct"]
                and item["predicted_answer"] == item["historical_answer"]
                for item in stale
            ),
            len(stale),
        ),
        "unnecessary_reobserve_rate": _rate(
            sum(item["route"] == "reobserve" for item in sufficient),
            len(sufficient),
        ),
        "reobserve_recall_on_risk_stale": _rate(
            sum(item["route"] == "reobserve" for item in stale), len(stale)
        ),
        "reobserve_rate": _rate(len(reobserved), len(records)),
        "new_observation_count": sum(
            item["new_observation_count"] for item in records
        ),
        "average_new_observation_count": _mean(
            [item["new_observation_count"] for item in records]
        ),
        "move_steps": sum(item["move_steps"] for item in records),
        "average_move_steps": _mean([item["move_steps"] for item in records]),
        "average_verification_cost": _mean(
            [item["verification_cost"] for item in records]
        ),
        "abstention_rate": 0.0,
        "expected_utility": {
            "{:g}".format(weight): _mean(
                [
                    int(item["correct"]) - weight * item["verification_cost"]
                    for item in records
                ]
            )
            for weight in utility_weights
        },
        "by_branch": branch_metrics,
    }


def _percentile(sorted_values, probability):
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def _interval(values):
    values = sorted(values)
    return {
        "lower": _percentile(values, 0.025),
        "median": _percentile(values, 0.5),
        "upper": _percentile(values, 0.975),
    }


def _component(records, utility_weights):
    stale = [item for item in records if item["memory_is_stale"]]
    return {
        "total": len(records),
        "correct": sum(item["correct"] for item in records),
        "stale_total": len(stale),
        "stale_errors": sum(
            not item["correct"]
            and item["predicted_answer"] == item["historical_answer"]
            for item in stale
        ),
        "observations": sum(item["new_observation_count"] for item in records),
        "utilities": {
            "{:g}".format(weight): sum(
                int(item["correct"]) - weight * item["verification_cost"]
                for item in records
            )
            for weight in utility_weights
        },
    }


def _sample_component(components, sampled_groups, utility_weights):
    result = {
        "total": 0,
        "correct": 0,
        "stale_total": 0,
        "stale_errors": 0,
        "observations": 0,
        "utilities": {"{:g}".format(weight): 0.0 for weight in utility_weights},
    }
    for group_id in sampled_groups:
        value = components[group_id]
        for key in ("total", "correct", "stale_total", "stale_errors", "observations"):
            result[key] += value[key]
        for key, amount in value["utilities"].items():
            result["utilities"][key] += amount
    return result


def _bootstrap(
    records_by_policy,
    primary_policy,
    utility_weights,
    samples,
    seed,
):
    required = {"always_trust", "always_reobserve", primary_policy}
    required.update(
        "trust3d_lambda_{:g}".format(weight) for weight in utility_weights
    )
    missing = sorted(required - set(records_by_policy))
    if missing:
        raise ValueError("bootstrap 缺少 policy: {}".format(", ".join(missing)))

    grouped = {}
    expected_groups = None
    for policy_id in required:
        by_group = defaultdict(list)
        for record in records_by_policy[policy_id]:
            by_group[record["group_id"]].append(record)
        if expected_groups is None:
            expected_groups = set(by_group)
        elif set(by_group) != expected_groups:
            raise ValueError("各 policy 的 group 覆盖不一致")
        grouped[policy_id] = {
            group_id: _component(group_records, utility_weights)
            for group_id, group_records in by_group.items()
        }

    groups = sorted(expected_groups)
    rng = random.Random(seed)
    stale_reductions = []
    accuracy_gaps = []
    observation_reductions = []
    utility_improvements = {
        "{:g}".format(weight): [] for weight in utility_weights
    }
    for _ in range(samples):
        sampled = [groups[rng.randrange(len(groups))] for _ in groups]
        cache = {
            policy_id: _sample_component(
                grouped[policy_id], sampled, utility_weights
            )
            for policy_id in required
        }
        trust = cache["always_trust"]
        reobserve = cache["always_reobserve"]
        primary = cache[primary_policy]
        stale_reductions.append(
            _rate(trust["stale_errors"], trust["stale_total"])
            - _rate(primary["stale_errors"], primary["stale_total"])
        )
        accuracy_gaps.append(
            _rate(primary["correct"], primary["total"])
            - _rate(reobserve["correct"], reobserve["total"])
        )
        observation_reductions.append(
            _rate(reobserve["observations"], reobserve["total"])
            - _rate(primary["observations"], primary["total"])
        )
        for weight in utility_weights:
            key = "{:g}".format(weight)
            controller = cache["trust3d_lambda_{:g}".format(weight)]
            controller_utility = controller["utilities"][key] / float(
                controller["total"]
            )
            extreme_utility = max(
                trust["utilities"][key] / float(trust["total"]),
                reobserve["utilities"][key] / float(reobserve["total"]),
            )
            utility_improvements[key].append(
                controller_utility - extreme_utility
            )

    return {
        "samples": samples,
        "seed": seed,
        "group_count": len(groups),
        "stale_error_reduction": _interval(stale_reductions),
        "accuracy_gap_vs_always_reobserve": _interval(accuracy_gaps),
        "observation_reduction_vs_always_reobserve": _interval(
            observation_reductions
        ),
        "utility_improvement_vs_best_extreme": {
            key: _interval(values) for key, values in utility_improvements.items()
        },
    }


def calculate(
    predictions_path,
    oracle_path,
    output_path,
    bootstrap_samples=10000,
    config_path=Path("configs/mvp.yaml"),
):
    predictions = read_jsonl(predictions_path)
    oracle = read_jsonl(oracle_path)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    utility_weights = config["utility_cost_weights"]
    primary_policy = "trust3d_lambda_{:g}".format(
        config["trust3d"]["primary_cost_weight"]
    )

    oracle_ids = {item["episode_id"] for item in oracle}
    records_by_policy = defaultdict(list)
    for record in predictions:
        records_by_policy[record["policy_id"]].append(record)
    for policy_id, records in records_by_policy.items():
        if {item["episode_id"] for item in records} != oracle_ids:
            raise ValueError("policy {} 的 episode 覆盖不完整".format(policy_id))

    policy_metrics = {
        policy_id: _policy_metrics(records, utility_weights)
        for policy_id, records in sorted(records_by_policy.items())
    }
    if primary_policy not in policy_metrics:
        raise ValueError("缺少主 Trust3D policy: {}".format(primary_policy))

    bootstrap = _bootstrap(
        records_by_policy,
        primary_policy,
        utility_weights,
        bootstrap_samples,
        config["bootstrap_seed"],
    )
    primary = policy_metrics[primary_policy]
    always_trust = policy_metrics["always_trust"]
    always_reobserve = policy_metrics["always_reobserve"]
    stale_reduction = (
        always_trust["stale_memory_error_rate"]
        - primary["stale_memory_error_rate"]
    )
    accuracy_gap = (
        primary["answer_accuracy"] - always_reobserve["answer_accuracy"]
    )
    observation_reduction = (
        always_reobserve["average_new_observation_count"]
        - primary["average_new_observation_count"]
    )

    utility_improvements = {}
    for weight in utility_weights:
        key = "{:g}".format(weight)
        controller = policy_metrics["trust3d_lambda_{:g}".format(weight)]
        best_extreme = max(
            always_trust["expected_utility"][key],
            always_reobserve["expected_utility"][key],
        )
        utility_improvements[key] = (
            controller["expected_utility"][key] - best_extreme
        )
    improved_weights = [
        key for key, value in utility_improvements.items() if value > 0
    ]
    confident_weights = [
        key
        for key, interval in bootstrap[
            "utility_improvement_vs_best_extreme"
        ].items()
        if interval["lower"] > 0
    ]

    acceptance = {
        "stale_error_reduction_at_least_30_points": stale_reduction >= 0.30,
        "accuracy_within_5_points_of_always_reobserve": accuracy_gap >= -0.05,
        "new_observation_reduction_at_least_25_percent": (
            observation_reduction >= 0.25
        ),
        "utility_improves_at_three_or_more_weights": len(improved_weights) >= 3,
        "bootstrap_stale_reduction_above_zero": (
            bootstrap["stale_error_reduction"]["lower"] > 0
        ),
        "bootstrap_accuracy_gap_within_5_points": (
            bootstrap["accuracy_gap_vs_always_reobserve"]["lower"] >= -0.05
        ),
        "bootstrap_observation_reduction_above_zero": (
            bootstrap["observation_reduction_vs_always_reobserve"]["lower"]
            > 0
        ),
        "bootstrap_utility_improves_at_three_or_more_weights": (
            len(confident_weights) >= 3
        ),
    }
    acceptance["gate4_pass"] = all(acceptance.values())

    report = {
        "schema_version": 1,
        "episode_count": len(oracle),
        "group_count": len({item["group_id"] for item in oracle}),
        "primary_policy": primary_policy,
        "policy_metrics": policy_metrics,
        "comparisons": {
            "stale_error_reduction": stale_reduction,
            "accuracy_gap_vs_always_reobserve": accuracy_gap,
            "new_observation_reduction_vs_always_reobserve": (
                observation_reduction
            ),
            "utility_improvement_vs_best_extreme": utility_improvements,
            "utility_improved_weights": improved_weights,
            "bootstrap_confident_utility_weights": confident_weights,
        },
        "bootstrap": bootstrap,
        "pareto_points": [
            {
                "policy_id": policy_id,
                "answer_accuracy": values["answer_accuracy"],
                "average_verification_cost": values[
                    "average_verification_cost"
                ],
                "average_new_observation_count": values[
                    "average_new_observation_count"
                ],
            }
            for policy_id, values in sorted(policy_metrics.items())
        ],
        "acceptance": acceptance,
    }
    _atomic_json(output_path, report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--group-key", default="group_id")
    parser.add_argument("--bootstrap", type=int, default=10000)
    parser.add_argument("--config", type=Path, default=Path("configs/mvp.yaml"))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.group_key != "group_id":
        raise ValueError("当前仅支持 group_id 成组 bootstrap")
    report = calculate(
        args.predictions,
        args.oracle,
        args.output,
        bootstrap_samples=args.bootstrap,
        config_path=args.config,
    )
    print(json.dumps(report["acceptance"], sort_keys=True))
    if not report["acceptance"]["gate4_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
