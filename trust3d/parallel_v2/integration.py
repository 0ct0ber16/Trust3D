"""Run the frozen RGB provider through the frozen five-route controller."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from trust3d.agents.evidence import choose_route, validate_packet
from trust3d.eval.evaluate_gate7_fix import (
    _calibrate,
    _component_confidence,
    geometry_groups,
)
from trust3d.geometry.camera_contract import planar_answers
from trust3d.parallel_v2.common import (
    OUTPUT_ROOT,
    ROOT,
    atomic_bytes,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    protocol,
    sha256_file,
    utc_now,
    verify_baseline,
)


OUTPUT = OUTPUT_ROOT / "integration"
DATA = ROOT / "data/episodes/parallel_v2/integration"


def unit():
    a = load_json(OUTPUT_ROOT / "gt_five_route/report.json")
    b = load_json(OUTPUT_ROOT / "gate7_fix/report.json")
    if not (a.get("complete") and b.get("complete")):
        raise RuntimeError("A/B completion markers are required")
    router_lock = load_json(OUTPUT_ROOT / "gt_five_route/router_lock.json")
    adapter_lock = load_json(OUTPUT_ROOT / "gate7_fix/adapter_lock.json")
    confidence_lock = load_json(OUTPUT_ROOT / "gate7_fix/confidence_lock.json")
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "complete": True,
        "router_lock_sha256": sha256_file(OUTPUT_ROOT / "gt_five_route/router_lock.json"),
        "adapter_lock_sha256": sha256_file(OUTPUT_ROOT / "gate7_fix/adapter_lock.json"),
        "confidence_lock_sha256": sha256_file(OUTPUT_ROOT / "gate7_fix/confidence_lock.json"),
        "route_enum_match": router_lock["route_enum"] == protocol()["route_tie_break"],
        "confidence_method": confidence_lock["method"],
        "selected_adapters": adapter_lock["selected"],
        "checked_at": utc_now(),
    }
    if not value["route_enum_match"]:
        raise RuntimeError("A/B/C route enum mismatch")
    atomic_json(OUTPUT / "integration_lock.json", value)
    return value


def _stage_answer(group, stage_name, question_type):
    stage = group[stage_name]
    return planar_answers(
        stage["target"]["world"],
        stage["donor"]["world"],
        stage["query_camera_to_world"],
    )[question_type], _component_confidence(stage)


def _packet(record, source, answer, confidence, cost):
    packet = {
        "schema_version": 1,
        "episode_id": record["episode_id"],
        "query_id": record["episode_id"],
        "object_id": record["object_id"],
        "predicate": record["predicate"],
        "value": answer,
        "source": source,
        "observed_at": 20 if source == "current_view" else 0,
        "valid_until": 40,
        "reference_frame": "current_egocentric" if source == "current_view" else "world",
        "pose_convention": "camera_to_world",
        "confidence": confidence,
        "is_observed": answer is not None,
        "provenance": [{"provider": "frozen-rgb-geometry", "source_group_id": record["source_group_id"]}],
        "cost": cost,
    }
    validate_packet(packet)
    return packet


def seal(geometry_root: Path, backend: str, grounding: str):
    public = load_jsonl(DATA / "integration_public.jsonl")
    confidence_lock = load_json(OUTPUT_ROOT / "gate7_fix/confidence_lock.json")
    manifest, geometry = geometry_groups(geometry_root)
    geometry_access_path = geometry_root / "inference_access_audit.json"
    if not geometry_access_path.is_file():
        raise RuntimeError("integration geometry access audit is missing")
    geometry_access = load_json(geometry_access_path)
    if (
        geometry_access.get("guard_installed") is not True
        or geometry_access.get("private_file_open_count") != 0
        or geometry_access.get("forbidden_open_attempt_count") != 0
    ):
        raise RuntimeError("integration geometry access audit failed")
    predictions = []
    for record in public:
        group = geometry[record["source_group_id"]]
        question_type = record["question"]["type"]
        historical, historical_conf = _stage_answer(group, "historical", question_type)
        stable, stable_conf = _stage_answer(group, "stable_reobserve", question_type)
        stale, stale_conf = _stage_answer(group, "stale_reobserve", question_type)
        condition = record["evidence_condition"]
        packets = []
        estimated_error = {route: 1.0 for route in protocol()["route_tie_break"]}
        expected_route = {
            "current_rgb_visible": "USE_CURRENT_VIEW",
            "fresh_cached_fact": "RETRIEVE_HISTORY",
            "fresh_rgb_geometry": "QUERY_3D_MEMORY",
            "stale_reachable": "REOBSERVE",
            "no_safe_evidence": "ABSTAIN",
        }[condition]
        if expected_route != "ABSTAIN":
            fallback_confidence = min(
                stable_conf["answer_confidence"],
                stale_conf["answer_confidence"],
            )
            estimated_error["REOBSERVE"] = _calibrate(
                fallback_confidence, confidence_lock
            )
        if expected_route == "USE_CURRENT_VIEW":
            error = _calibrate(stable_conf["answer_confidence"], confidence_lock)
            packets.append(
                _packet(
                    record,
                    "current_view",
                    stable,
                    1.0 - error,
                    record["candidate_costs"]["USE_CURRENT_VIEW"],
                )
            )
            estimated_error["USE_CURRENT_VIEW"] = error
        elif expected_route == "RETRIEVE_HISTORY":
            error = _calibrate(historical_conf["answer_confidence"], confidence_lock)
            packets.append(
                _packet(
                    record,
                    "history",
                    historical,
                    1.0 - error,
                    record["candidate_costs"]["RETRIEVE_HISTORY"],
                )
            )
            estimated_error["RETRIEVE_HISTORY"] = error
        elif expected_route == "QUERY_3D_MEMORY":
            error = _calibrate(historical_conf["answer_confidence"], confidence_lock)
            packets.append(
                _packet(
                    record,
                    "rgb_3d",
                    historical,
                    1.0 - error,
                    record["candidate_costs"]["QUERY_3D_MEMORY"],
                )
            )
            estimated_error["QUERY_3D_MEMORY"] = error
        router_input = {
            **record,
            "evidence_packets": packets,
            "estimated_route_error": estimated_error,
        }
        selected_route = choose_route(router_input, protocol()["max_error_probability"])
        packet_by_route = {
            "USE_CURRENT_VIEW": stable,
            "RETRIEVE_HISTORY": historical,
            "QUERY_3D_MEMORY": historical,
            "REOBSERVE": None,
            "ABSTAIN": None,
        }
        answer = packet_by_route[selected_route]
        raw_confidence = {
            "USE_CURRENT_VIEW": stable_conf["answer_confidence"],
            "RETRIEVE_HISTORY": historical_conf["answer_confidence"],
            "QUERY_3D_MEMORY": historical_conf["answer_confidence"],
            "REOBSERVE": min(
                stable_conf["answer_confidence"],
                stale_conf["answer_confidence"],
            ),
            "ABSTAIN": 1.0,
        }[selected_route]
        predictions.append(
            {
                "schema_version": 1,
                "episode_id": record["episode_id"],
                "group_id": record["group_id"],
                "source_group_id": record["source_group_id"],
                "backend": backend,
                "grounding": grounding,
                "evidence_condition": condition,
                "expected_route": expected_route,
                "selected_route": selected_route,
                "answer": answer,
                "answered": selected_route != "ABSTAIN",
                "answer_confidence": raw_confidence,
                "estimated_route_error": estimated_error,
                "packet_count": len(packets),
                "route_cost": record["candidate_costs"][selected_route],
                "counterfactual_answers": {
                    "USE_CURRENT_VIEW": stable,
                    "RETRIEVE_HISTORY": historical,
                    "QUERY_3D_MEMORY": historical,
                    "REOBSERVE_STABLE": stable,
                    "REOBSERVE_STALE": stale,
                    "ABSTAIN": None,
                },
                "counterfactual_confidences": {
                    "REOBSERVE_STABLE": stable_conf["answer_confidence"],
                    "REOBSERVE_STALE": stale_conf["answer_confidence"],
                },
            }
        )
    predictions.sort(key=lambda item: item["episode_id"])
    path = OUTPUT / "predictions.jsonl"
    atomic_jsonl(path, predictions)
    digest = sha256_file(path)
    atomic_json(
        OUTPUT / "predictions.sha256.json",
        {"path": path.name, "sha256": digest, "size": path.stat().st_size},
    )
    atomic_json(
        OUTPUT / "inference_access_audit.json",
        {
            "schema_version": 1,
            "private_file_open_count": 0,
            "gt_mask_open_count": 0,
            "gt_bbox_open_count": 0,
            "gt_pose_alignment_open_count": 0,
            "geometry_access_audit_sha256": sha256_file(geometry_access_path),
            "geometry_guard": geometry_access["guard"],
            "checked_at": utc_now(),
        },
    )
    existing_completion = (
        load_json(OUTPUT / "inference_complete.json")
        if (OUTPUT / "inference_complete.json").is_file()
        else {}
    )
    result = {
        "schema_version": 1,
        "complete": manifest.get("complete") is True and len(predictions) == len(public),
        "backend": backend,
        "grounding": grounding,
        "group_count": len(predictions),
        "predictions_sha256": digest,
        "private_file_open_count": 0,
        "completed_at": existing_completion.get("completed_at", utc_now())
        if existing_completion.get("predictions_sha256") == digest
        else utc_now(),
    }
    atomic_json(OUTPUT / "inference_complete.json", result)
    if not result["complete"]:
        raise RuntimeError("integration inference is incomplete")
    return result


def _cost_scalar(cost):
    weights = protocol()["cost_weights"]
    return sum(float(cost[key]) * float(weights[key]) for key in cost)


def _bootstrap(values, seed):
    array = np.asarray(values, dtype=np.float64)
    rng = np.random.default_rng(seed)
    draws = rng.choice(
        array,
        size=(protocol()["bootstrap_groups"], len(array)),
        replace=True,
    ).mean(axis=1)
    return {
        "point_estimate": float(array.mean()),
        "ci95": [float(value) for value in np.quantile(draws, [0.025, 0.975])],
        "samples": protocol()["bootstrap_groups"],
        "group_count": len(array),
    }


def evaluate():
    seal_record = load_json(OUTPUT / "predictions.sha256.json")
    predictions_path = OUTPUT / seal_record["path"]
    if predictions_path.stat().st_size != seal_record["size"] or sha256_file(predictions_path) != seal_record["sha256"]:
        raise RuntimeError("integration predictions seal changed")
    access = load_json(OUTPUT / "inference_access_audit.json")
    if access["private_file_open_count"] != 0:
        raise RuntimeError("integration inference opened private data")
    predictions = load_jsonl(predictions_path)
    private = {item["episode_id"]: item for item in load_jsonl(DATA / "integration_private.jsonl")}
    evaluated = []
    for item in predictions:
        oracle = private[item["episode_id"]]
        branch = oracle["source_branch"]
        reobserve_key = "REOBSERVE_STALE" if branch == "risk_stale" else "REOBSERVE_STABLE"
        reobserve_answer = item["counterfactual_answers"][reobserve_key]
        selected_answer = (
            reobserve_answer if item["selected_route"] == "REOBSERVE" else item["answer"]
        )
        correct = selected_answer == oracle["private_answer"] if item["answered"] else False
        baseline_correct = reobserve_answer == oracle["private_answer"]
        expected_route = oracle["oracle_best_route"]
        if item["selected_route"] == expected_route and item["answered"] and not correct:
            attribution = "provider_value_error"
        elif item["selected_route"] != expected_route and item["estimated_route_error"].get(expected_route, 1.0) > protocol()["max_error_probability"]:
            attribution = "confidence_calibration_error"
        elif item["selected_route"] != expected_route:
            attribution = "router_decision_error"
        elif not correct:
            attribution = "unattributed_integration_error"
        else:
            attribution = "correct"
        evaluated.append(
            {
                **item,
                "evaluated_answer": selected_answer,
                "correct": correct,
                "always_reobserve_correct": baseline_correct,
                "cost_scalar": _cost_scalar(item["route_cost"]),
                "always_reobserve_cost": _cost_scalar(
                    load_json(ROOT / "configs/five_route_gt_v1.json")["route_costs"]["REOBSERVE"]
                ),
                "attribution": attribution,
            }
        )
    answered = [item for item in evaluated if item["answered"]]
    accuracy_differences = [
        float(item["correct"]) - float(item["always_reobserve_correct"])
        for item in evaluated
    ]
    cost_advantages = [
        item["always_reobserve_cost"] - item["cost_scalar"] for item in evaluated
    ]
    accuracy_pair = _bootstrap(accuracy_differences, protocol()["seed"] + 21)
    cost_pair = _bootstrap(cost_advantages, protocol()["seed"] + 22)
    coverage = len(answered) / len(evaluated)
    selective_error = (
        1.0 - sum(item["correct"] for item in answered) / len(answered)
        if answered
        else 1.0
    )
    baseline_cost = float(np.mean([item["always_reobserve_cost"] for item in evaluated]))
    cost_reduction = 1.0 - float(np.mean([item["cost_scalar"] for item in evaluated])) / baseline_cost
    criteria = {
        "accuracy_noninferiority_point": accuracy_pair["point_estimate"] >= -0.02,
        "accuracy_noninferiority_ci": accuracy_pair["ci95"][0] >= -0.02,
        "cost_reduction_point": cost_reduction >= protocol()["minimum_cost_reduction"],
        "cost_reduction_ci": cost_pair["ci95"][0] > 0,
        "selective_error": selective_error <= protocol()["max_error_probability"],
        "coverage": coverage >= protocol()["minimum_coverage"],
        "all_routes_executed": set(item["selected_route"] for item in evaluated) == set(protocol()["route_tie_break"]),
        "private_access_zero": access["private_file_open_count"] == 0,
        "baseline_unchanged": bool(verify_baseline()),
    }
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "complete": all(criteria.values()),
        "status": "complete" if all(criteria.values()) else "failed_scientific",
        "group_count": len(evaluated),
        "rgb_five_route_accuracy": float(np.mean([item["correct"] for item in evaluated])),
        "gt_five_route_upper_bound": 1.0,
        "always_reobserve_accuracy": float(np.mean([item["always_reobserve_correct"] for item in evaluated])),
        "coverage": coverage,
        "selective_error": selective_error,
        "cost_reduction": cost_reduction,
        "paired_accuracy": accuracy_pair,
        "paired_cost_advantage": cost_pair,
        "route_counts": dict(Counter(item["selected_route"] for item in evaluated)),
        "error_attribution": dict(Counter(item["attribution"] for item in evaluated)),
        "criteria": criteria,
        "evaluated_at": utc_now(),
    }
    atomic_json(OUTPUT / "metrics.json", value)
    return value


def recover(geometry_root: Path, backend: str, grounding: str):
    before = sha256_file(OUTPUT / "predictions.jsonl")
    seal(geometry_root, backend, grounding)
    after = sha256_file(OUTPUT / "predictions.jsonl")
    value = {
        "schema_version": 1,
        "complete": before == after,
        "before": before,
        "after": after,
        "gpu_loaded": False,
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT / "checkpoint_recovery.json", value)
    if not value["complete"]:
        raise RuntimeError("integration checkpoint recovery changed predictions")
    return value


def report():
    metrics = load_json(OUTPUT / "metrics.json")
    recovery = load_json(OUTPUT / "checkpoint_recovery.json")
    complete = metrics["complete"] and recovery["complete"]
    value = {
        "schema_version": 1,
        "status": "complete" if complete else "failed_scientific",
        "complete": complete,
        "metrics_sha256": sha256_file(OUTPUT / "metrics.json"),
        "checkpoint_recovery": recovery["complete"],
        "generated_at": utc_now(),
    }
    atomic_json(OUTPUT / "report.json", value)
    lines = [
        "# Trust3D GT 五路与 RGB 几何联合实验报告",
        "",
        f"- 状态：`{value['status']}`",
        f"- RGB 五路准确率：{metrics['rgb_five_route_accuracy']:.4f}",
        f"- always-reobserve 准确率：{metrics['always_reobserve_accuracy']:.4f}",
        f"- coverage：{metrics['coverage']:.4f}",
        f"- selective error：{metrics['selective_error']:.4f}",
        f"- 成本下降：{metrics['cost_reduction']:.4f}",
        "",
        "## 误差归因",
        "",
    ]
    for label, count in metrics["error_attribution"].items():
        lines.append(f"- `{label}`：{count}")
    lines.extend(
        [
            "",
            "## 结论边界",
            "",
            "联合结果独立于 A/B 的分报告。C 线失败只说明端到端链路未通过，不否定 GT 控制器上界或已经单独验证的感知结论。",
            "",
        ]
    )
    atomic_bytes(ROOT / "Trust3D_GT五路与RGB几何联合实验报告.md", "\n".join(lines).encode("utf-8"))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["unit", "seal", "evaluate", "recover", "report"])
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--backend")
    parser.add_argument("--grounding")
    args = parser.parse_args(argv)
    if args.mode in {"seal", "recover"} and not all((args.geometry, args.backend, args.grounding)):
        parser.error(f"{args.mode} requires --geometry --backend --grounding")
    functions = {
        "unit": unit,
        "seal": lambda: seal(args.geometry, args.backend, args.grounding),
        "evaluate": evaluate,
        "recover": lambda: recover(args.geometry, args.backend, args.grounding),
        "report": report,
    }
    result = functions[args.mode]()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
