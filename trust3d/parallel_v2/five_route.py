"""Execute and evaluate the GT five-route experiment."""

from __future__ import annotations

import argparse
import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from trust3d.agents.evidence import ROUTES, choose_route
from trust3d.agents.gt_evidence_provider import execute_route
from trust3d.eval.build_route_oracle import validate_oracle
from trust3d.parallel_v2.common import (
    CHECKPOINT_ROOT,
    OUTPUT_ROOT,
    ROOT,
    atomic_bytes,
    atomic_json,
    atomic_jsonl,
    canonical_bytes,
    load_json,
    load_jsonl,
    protocol,
    sha256_bytes,
    sha256_file,
    stage_complete,
    utc_now,
)


DATA_ROOT = ROOT / "data/episodes/parallel_v2/gt5"
OUTPUT = OUTPUT_ROOT / "gt_five_route"
UNIT_ROOT = CHECKPOINT_ROOT / "units/gt_five_route"
POLICIES = (
    "always_current",
    "always_history",
    "always_3d_memory",
    "always_reobserve",
    "always_abstain",
    "legacy_two_route",
    "trust3d_five_route",
)


def _paths(split: str):
    return DATA_ROOT / f"{split}_public.jsonl", DATA_ROOT / f"{split}_private.jsonl"


def _load_split(split: str):
    public_path, private_path = _paths(split)
    return load_jsonl(public_path), load_jsonl(private_path)


def _route(public: dict[str, Any], policy_id: str) -> str:
    if policy_id == "always_current":
        return "USE_CURRENT_VIEW"
    if policy_id == "always_history":
        return "RETRIEVE_HISTORY"
    if policy_id == "always_3d_memory":
        return "QUERY_3D_MEMORY"
    if policy_id == "always_reobserve":
        return "REOBSERVE"
    if policy_id == "always_abstain":
        return "ABSTAIN"
    if policy_id == "legacy_two_route":
        if public["predicate"] == "attribute":
            valid_history = any(
                packet["source"] == "history"
                and packet["is_observed"]
                and packet["valid_until"] >= public["query_time"]
                for packet in public["evidence_packets"]
            )
            return "RETRIEVE_HISTORY" if valid_history else "REOBSERVE"
        valid_3d = any(
            packet["source"] in {"gt_3d", "rgb_3d"}
            and packet["is_observed"]
            and packet["valid_until"] >= public["query_time"]
            for packet in public["evidence_packets"]
        )
        return "QUERY_3D_MEMORY" if valid_3d else "REOBSERVE"
    if policy_id == "trust3d_five_route":
        return choose_route(public, protocol()["max_error_probability"])
    raise ValueError(policy_id)


def unit():
    public, private = _load_split("pilot")
    oracle = validate_oracle(
        public, private, protocol()["minimum_route_loss_margin"]
    )
    chosen = Counter(_route(item, "trust3d_five_route") for item in public)
    expected = Counter(item["oracle_best_route"] for item in private)
    if chosen != expected or any(chosen[route] < 4 for route in ROUTES):
        raise AssertionError("five-route synthetic contract did not cover all routes")
    result = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "stage": "gt5_unit",
        "complete": True,
        "checked_at": utc_now(),
        "synthetic_case_count": len(public),
        "selected_routes": dict(chosen),
        "oracle": oracle,
    }
    atomic_json(OUTPUT / "unit.json", result)
    return result


def _power_audit(public, private):
    private_by_id = {item["episode_id"]: item for item in private}
    differences = []
    cost_reductions = []
    weights = protocol()["cost_weights"]

    def cost(value):
        return sum(float(value[key]) * float(weights[key]) for key in value)

    for record in public:
        oracle = private_by_id[record["episode_id"]]
        trust = execute_route(record, oracle, _route(record, "trust3d_five_route"))
        reobserve = execute_route(record, oracle, "REOBSERVE")
        trust_correct = float(trust["answer"] == oracle["private_answer"])
        reobserve_correct = float(reobserve["answer"] == oracle["private_answer"])
        differences.append(trust_correct - reobserve_correct)
        baseline_cost = cost(reobserve["cost"])
        cost_reductions.append(
            (baseline_cost - cost(trust["cost"])) / baseline_cost
            if baseline_cost > 0
            else 0.0
        )
    accuracy_std = float(np.std(differences, ddof=1)) if len(differences) > 1 else 0.0
    cost_std = float(np.std(cost_reductions, ddof=1)) if len(cost_reductions) > 1 else 0.0
    z_alpha, z_power = 1.96, 0.841621
    accuracy_effect = 0.02
    cost_effect = 0.20
    n_accuracy = math.ceil(((z_alpha + z_power) * accuracy_std / accuracy_effect) ** 2) if accuracy_std else 1
    n_cost = math.ceil(((z_alpha + z_power) * cost_std / cost_effect) ** 2) if cost_std else 1
    required = max(50, n_accuracy, n_cost)
    return {
        "alpha": 0.05,
        "power": 0.8,
        "paired_accuracy_std": accuracy_std,
        "paired_cost_reduction_std": cost_std,
        "required_accuracy_groups": n_accuracy,
        "required_cost_groups": n_cost,
        "required_final_groups": required,
        "available_final_groups": protocol()["datasets"]["gt5_final_groups"],
        "adequately_powered": required <= protocol()["datasets"]["gt5_final_groups"],
    }


def pilot():
    public, private = _load_split("pilot")
    selected = [_route(item, "trust3d_five_route") for item in public]
    private_by_id = {item["episode_id"]: item for item in private}
    if any(
        route != private_by_id[item["episode_id"]]["oracle_best_route"]
        for item, route in zip(public, selected)
    ):
        raise AssertionError("pilot route mismatch")
    power = _power_audit(public, private)
    if not power["adequately_powered"]:
        raise RuntimeError("inconclusive_underpowered")
    lock = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "adapter_revision": "gt-five-route-v1",
        "created_at": utc_now(),
        "route_enum": list(ROUTES),
        "max_error_probability": protocol()["max_error_probability"],
        "router_source_sha256": sha256_file(ROOT / "trust3d/agents/evidence.py"),
        "protocol_sha256": sha256_file(ROOT / "configs/parallel_v2_protocol.json"),
        "pilot_public_sha256": sha256_file(_paths("pilot")[0]),
        "power_audit": power,
    }
    path = OUTPUT / "router_lock.json"
    if path.exists() and load_json(path) != lock:
        existing = load_json(path)
        comparable_existing = {key: value for key, value in existing.items() if key != "created_at"}
        comparable_new = {key: value for key, value in lock.items() if key != "created_at"}
        if comparable_existing != comparable_new:
            raise RuntimeError("immutable router lock already exists with different content")
        return existing
    atomic_json(path, lock)
    return lock


def _unit_fingerprint(public, private, policy_id):
    return sha256_bytes(
        canonical_bytes(
            {
                "protocol": sha256_file(ROOT / "configs/parallel_v2_protocol.json"),
                "public": public,
                "private": private,
                "policy_id": policy_id,
                "router": sha256_file(ROOT / "trust3d/agents/evidence.py"),
            }
        )
    )


def _evaluate_unit(public, private, policy_id):
    route = _route(public, policy_id)
    execution = execute_route(public, private, route)
    correct = execution["answer"] == private["private_answer"] if execution["answered"] else False
    return {
        **execution,
        "policy_id": policy_id,
        "correct": correct,
        "oracle_best_route": private["oracle_best_route"],
        "route_regret": private["route_losses"][route] - min(private["route_losses"].values()),
        "unsafe_answer": execution["answered"] and not correct,
        "false_abstain": route == "ABSTAIN" and private["oracle_best_route"] != "ABSTAIN",
    }


def offline():
    if not (OUTPUT / "router_lock.json").is_file():
        raise RuntimeError("router lock is missing")
    public, private = _load_split("final")
    private_by_id = {item["episode_id"]: item for item in private}
    predictions = []
    for public_record in public:
        private_record = private_by_id[public_record["episode_id"]]
        for policy_id in POLICIES:
            fingerprint = _unit_fingerprint(public_record, private_record, policy_id)
            checkpoint = UNIT_ROOT / policy_id / f"{public_record['group_id']}.json"
            value = None
            if checkpoint.is_file():
                candidate = load_json(checkpoint)
                if candidate.get("status") == "complete" and candidate.get("fingerprint") == fingerprint:
                    value = candidate["prediction"]
            if value is None:
                value = _evaluate_unit(public_record, private_record, policy_id)
                atomic_json(
                    checkpoint,
                    {
                        "schema_version": 1,
                        "status": "complete",
                        "completed_at": utc_now(),
                        "fingerprint": fingerprint,
                        "prediction": value,
                        "prediction_sha256": sha256_bytes(canonical_bytes(value)),
                    },
                )
            predictions.append(value)
    predictions.sort(key=lambda item: (item["policy_id"], item["episode_id"]))
    predictions_path = OUTPUT / "predictions.jsonl"
    atomic_jsonl(predictions_path, predictions)
    metrics = evaluate_predictions(predictions, private)
    metrics["predictions_sha256"] = sha256_file(predictions_path)
    metrics_path = OUTPUT / "metrics.json"
    if metrics_path.is_file():
        existing = load_json(metrics_path)
        existing_comparable = {key: value for key, value in existing.items() if key != "generated_at"}
        metrics_comparable = {key: value for key, value in metrics.items() if key != "generated_at"}
        if existing_comparable == metrics_comparable:
            metrics["generated_at"] = existing["generated_at"]
    atomic_json(metrics_path, metrics)
    return metrics


def _bootstrap_pair(records, baseline, field, samples, seed):
    candidate_by_group = {item["group_id"]: float(item[field]) for item in records}
    baseline_by_group = {item["group_id"]: float(item[field]) for item in baseline}
    groups = sorted(set(candidate_by_group) & set(baseline_by_group))
    differences = np.asarray(
        [candidate_by_group[group] - baseline_by_group[group] for group in groups],
        dtype=np.float64,
    )
    rng = np.random.default_rng(seed)
    draws = rng.choice(differences, size=(samples, len(groups)), replace=True).mean(axis=1)
    return {
        "group_count": len(groups),
        "point_estimate": float(differences.mean()),
        "ci95": [float(value) for value in np.quantile(draws, [0.025, 0.975])],
        "samples": samples,
        "seed": seed,
    }


def evaluate_predictions(predictions, private):
    private_by_id = {item["episode_id"]: item for item in private}
    by_policy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    weights = protocol()["cost_weights"]
    for item in predictions:
        value = dict(item)
        value["cost_scalar"] = sum(float(item["cost"][key]) * float(weights[key]) for key in item["cost"])
        by_policy[item["policy_id"]].append(value)
    metrics = {}
    for policy_id, records in sorted(by_policy.items()):
        answered = [item for item in records if item["answered"]]
        identifiable = [
            item
            for item in records
            if not private_by_id[item["episode_id"]]["publicly_ambiguous"]
            and not private_by_id[item["episode_id"]]["near_tie"]
        ]
        route_tp = Counter(
            item["route"]
            for item in identifiable
            if item["route"] == item["oracle_best_route"]
        )
        metrics[policy_id] = {
            "group_count": len(records),
            "accuracy": sum(item["correct"] for item in records) / len(records),
            "answered_accuracy": sum(item["correct"] for item in answered) / len(answered) if answered else None,
            "coverage": len(answered) / len(records),
            "selective_error": 1.0 - sum(item["correct"] for item in answered) / len(answered) if answered else None,
            "mean_cost": float(np.mean([item["cost_scalar"] for item in records])),
            "mean_route_regret": float(np.mean([item["route_regret"] for item in records])),
            "unsafe_answer_rate": sum(item["unsafe_answer"] for item in records) / len(records),
            "false_abstain_rate": sum(item["false_abstain"] for item in records) / len(records),
            "route_counts": dict(Counter(item["route"] for item in records)),
            "per_route_true_positive": {route: route_tp[route] for route in ROUTES},
        }
    trust = by_policy["trust3d_five_route"]
    reobserve = by_policy["always_reobserve"]
    accuracy_pair = _bootstrap_pair(
        trust,
        reobserve,
        "correct",
        protocol()["bootstrap_groups"],
        protocol()["seed"],
    )
    cost_pair = _bootstrap_pair(
        [{**item, "neg_cost": -item["cost_scalar"]} for item in trust],
        [{**item, "neg_cost": -item["cost_scalar"]} for item in reobserve],
        "neg_cost",
        protocol()["bootstrap_groups"],
        protocol()["seed"] + 1,
    )
    baseline_cost = metrics["always_reobserve"]["mean_cost"]
    cost_reduction = 1.0 - metrics["trust3d_five_route"]["mean_cost"] / baseline_cost
    trust_metrics = metrics["trust3d_five_route"]
    acceptance = {
        "coverage": trust_metrics["coverage"] >= protocol()["minimum_coverage"],
        "selective_error": trust_metrics["selective_error"] <= protocol()["max_error_probability"],
        "accuracy_noninferiority_point": accuracy_pair["point_estimate"] >= -protocol()["accuracy_noninferiority_margin_pp"] / 100,
        "accuracy_noninferiority_ci": accuracy_pair["ci95"][0] >= -protocol()["accuracy_noninferiority_margin_pp"] / 100,
        "cost_reduction_point": cost_reduction >= protocol()["minimum_cost_reduction"],
        "cost_reduction_ci": cost_pair["ci95"][0] > 0,
        "all_routes_true_positive": all(trust_metrics["per_route_true_positive"][route] > 0 for route in ROUTES),
        "false_abstain": trust_metrics["false_abstain_rate"] <= 0.05,
        "unsafe_answer": trust_metrics["unsafe_answer_rate"] <= protocol()["max_error_probability"],
    }
    return {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "generated_at": utc_now(),
        "metrics": metrics,
        "paired_accuracy_vs_always_reobserve": accuracy_pair,
        "paired_cost_advantage_vs_always_reobserve": cost_pair,
        "cost_reduction": cost_reduction,
        "acceptance": acceptance,
        "offline_pass": all(acceptance.values()),
    }


def recover():
    before = sha256_file(OUTPUT / "predictions.jsonl")
    metrics_before = sha256_file(OUTPUT / "metrics.json")
    result = offline()
    after = sha256_file(OUTPUT / "predictions.jsonl")
    metrics_after = sha256_file(OUTPUT / "metrics.json")
    recovery = {
        "schema_version": 1,
        "complete": before == after and metrics_before == metrics_after,
        "predictions_before": before,
        "predictions_after": after,
        "metrics_before": metrics_before,
        "metrics_after": metrics_after,
        "gpu_loaded": False,
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT / "checkpoint_recovery.json", recovery)
    if not recovery["complete"]:
        raise RuntimeError("GT five-route recovery changed sealed outputs")
    return result


def prepare_online():
    public, private = _load_split("final")
    private_by_id = {item["episode_id"]: item for item in private}
    source_public = {item["episode_id"]: item for item in load_jsonl(ROOT / "data/episodes/mvp/episodes_public.jsonl")}
    records = []
    mapping = []
    for item in public:
        oracle = private_by_id[item["episode_id"]]
        if oracle["oracle_best_route"] != "REOBSERVE" or not item["online_source"]["eligible"]:
            continue
        source_episode_id = item["source_episode_id"]
        if source_episode_id not in source_public:
            raise ValueError(f"online source episode missing: {source_episode_id}")
        records.append(source_public[source_episode_id])
        mapping.append({"parallel_episode_id": item["episode_id"], "source_episode_id": source_episode_id})
    if len(records) < 10:
        raise RuntimeError("online REOBSERVE support is below 10 groups")
    atomic_jsonl(DATA_ROOT / "online_reobserve_public.jsonl", records)
    atomic_json(OUTPUT / "online_mapping.json", {"records": mapping})
    return {"group_count": len(records), "episode_count": len(records)}


def validate_online(traces_path: Path):
    traces = load_jsonl(traces_path)
    mapping = load_json(OUTPUT / "online_mapping.json")["records"]
    source_ids = {item["source_episode_id"] for item in mapping}
    primary = [item for item in traces if item.get("episode_id") in source_ids]
    by_id = {item["episode_id"]: item for item in primary}
    complete = set(by_id) == source_ids
    checks = {
        "all_source_episodes_completed": complete,
        "all_routes_reobserve": all(
            item.get("selected_route") == "REOBSERVE" for item in by_id.values()
        ),
        "new_observation_recorded": all(
            item.get("new_observation_count", 0) >= 1 for item in by_id.values()
        ),
        "no_duplicate_episode": len(primary) == len(by_id),
    }
    result = {
        "schema_version": 1,
        "complete": all(checks.values()),
        "backend": "ai2thor_shortest_visible_pose",
        "group_count": len(source_ids),
        "checks": checks,
        "traces_sha256": sha256_file(traces_path),
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT / "online_validation.json", result)
    if not result["complete"]:
        raise RuntimeError("GT five-route online validation failed")
    return result


def report():
    metrics = load_json(OUTPUT / "metrics.json")
    online = load_json(OUTPUT / "online_validation.json") if (OUTPUT / "online_validation.json").is_file() else {"complete": False}
    recovery = load_json(OUTPUT / "checkpoint_recovery.json") if (OUTPUT / "checkpoint_recovery.json").is_file() else {"complete": False}
    baseline_unchanged = stage_complete("baseline_manifest")
    complete = metrics["offline_pass"] and online["complete"] and recovery["complete"] and baseline_unchanged
    status = "complete" if complete else "failed_scientific"
    summary = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "status": status,
        "complete": complete,
        "offline_pass": metrics["offline_pass"],
        "online_pass": online["complete"],
        "checkpoint_recovery_pass": recovery["complete"],
        "baseline_unchanged": baseline_unchanged,
        "generated_at": utc_now(),
    }
    atomic_json(OUTPUT / "report.json", summary)
    trust = metrics["metrics"]["trust3d_five_route"]
    lines = [
        "# Trust3D GT 五路路由实验报告",
        "",
        f"- 实验状态：`{status}`",
        f"- final group 数：{trust['group_count']}",
        f"- 总体准确率：{trust['accuracy']:.4f}",
        f"- coverage：{trust['coverage']:.4f}",
        f"- selective error：{trust['selective_error']:.4f}",
        f"- 相对 always-reobserve 成本下降：{metrics['cost_reduction']:.4f}",
        f"- 在线 AI2-THOR 校验：{'通过' if online['complete'] else '未通过或尚未完成'}",
        f"- checkpoint 恢复校验：{'通过' if recovery['complete'] else '未通过'}",
        "",
        "## 五路选择",
        "",
    ]
    for route in ROUTES:
        lines.append(f"- `{route}`：{trust['route_counts'].get(route, 0)} groups")
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "GT provider 条件下的五路路由只在所有预注册门槛、在线执行和恢复校验均通过时记为完成；该结果不替代 RGB 几何端到端验证。",
            "",
        ]
    )
    report_path = ROOT / "Trust3D_GT五路路由实验报告.md"
    atomic_bytes(report_path, "\n".join(lines).encode("utf-8"))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["unit", "pilot", "offline", "recover", "prepare-online", "validate-online", "report"])
    parser.add_argument("--traces", type=Path, default=OUTPUT / "online_traces.jsonl")
    args = parser.parse_args(argv)
    functions = {
        "unit": unit,
        "pilot": pilot,
        "offline": offline,
        "recover": recover,
        "prepare-online": prepare_online,
        "validate-online": lambda: validate_online(args.traces),
        "report": report,
    }
    result = functions[args.mode]()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
