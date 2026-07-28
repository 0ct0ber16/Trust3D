"""隔离 private oracle 后验收 Gate 5 在线 trace。"""

import argparse
import json
from pathlib import Path

from trust3d.data.build_branches import _atomic_json
from trust3d.data.select_events import read_jsonl


FORBIDDEN_TRACE_KEYS = {
    "branch",
    "current_answer",
    "hidden_intervention",
    "historical_answer",
    "memory_is_stale",
    "shortest_verification_cost",
    "source_candidate_id",
    "source_json",
    "state_hash",
    "verification_pose",
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            for nested in _walk_keys(child):
                yield nested
    elif isinstance(value, list):
        for child in value:
            for nested in _walk_keys(child):
                yield nested


def validate(
    traces_path,
    public_path,
    private_path,
    offline_predictions_path,
    manifest_path,
    output_path,
    primary_policy="trust3d_lambda_0.01",
    root=Path("."),
):
    traces = read_jsonl(traces_path)
    public = read_jsonl(public_path)
    private = read_jsonl(private_path)
    offline = [
        item
        for item in read_jsonl(offline_predictions_path)
        if item["policy_id"] == primary_policy
    ]
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    trace_by_id = {item["episode_id"]: item for item in traces}
    public_by_id = {item["episode_id"]: item for item in public}
    private_by_id = {item["episode_id"]: item for item in private}
    offline_by_id = {item["episode_id"]: item for item in offline}
    expected_ids = set(public_by_id)

    leak_ids = []
    missing_evidence = []
    invalidation_errors = []
    route_mismatches = []
    answer_disagreements = []
    movement_cost_mismatches = []
    incorrect = []
    stale_errors = []
    action_failures = 0
    action_attempts = 0
    reobserved = 0
    for episode_id, trace in trace_by_id.items():
        if set(_walk_keys(trace)) & FORBIDDEN_TRACE_KEYS:
            leak_ids.append(episode_id)
        oracle = private_by_id.get(episode_id)
        baseline = offline_by_id.get(episode_id)
        if oracle is None or baseline is None:
            continue
        route = trace["selected_route"].lower()
        if route != baseline["route"]:
            route_mismatches.append(episode_id)
        if trace["answer"] != baseline["predicted_answer"]:
            answer_disagreements.append(episode_id)
        if trace["answer"] != oracle["current_answer"]:
            incorrect.append(episode_id)
            if oracle["memory_is_stale"]:
                stale_errors.append(episode_id)
        reobserve = route == "reobserve"
        reobserved += int(reobserve)
        if reobserve and trace["movement_steps"] != oracle[
            "shortest_verification_cost"
        ]:
            movement_cost_mismatches.append(episode_id)
        invalidated = trace.get("invalidated_fact_ids", [])
        if bool(invalidated) != bool(oracle["memory_is_stale"] and reobserve):
            invalidation_errors.append(episode_id)
        evidence = trace.get("answer_evidence", [])
        new_frames = trace.get("new_frame_ids", [])
        if not evidence or (reobserve and not new_frames):
            missing_evidence.append(episode_id)
        for frame in evidence:
            if not (Path(root) / frame).is_file():
                missing_evidence.append(episode_id)
                break
        action_failures += int(trace.get("action_failure_count", 0))
        action_attempts += int(trace.get("movement_action_count", 0))

    count = len(traces)
    accuracy = 1.0 - len(incorrect) / float(max(count, 1))
    offline_accuracy = sum(item["correct"] for item in offline) / float(
        max(len(offline), 1)
    )
    stale_count = sum(item["memory_is_stale"] for item in private)
    stale_error_rate = len(stale_errors) / float(max(stale_count, 1))
    reobserve_rate = reobserved / float(max(count, 1))
    action_failure_rate = action_failures / float(
        max(action_attempts + action_failures, 1)
    )
    acceptance = {
        "all_online_units_completed": (
            manifest["completed_unit_count"] == manifest["expected_unit_count"]
            and manifest["pending_unit_count"] == 0
        ),
        "trace_ids_match_public_and_private": (
            set(trace_by_id) == expected_ids == set(private_by_id)
        ),
        "trace_ids_unique": len(trace_by_id) == len(traces),
        "online_accuracy_within_5_points_of_offline": (
            abs(accuracy - offline_accuracy) <= 0.05
        ),
        "online_routes_match_offline_controller": not route_mismatches,
        "online_answers_match_offline_simulation": not answer_disagreements,
        "action_failure_rate_below_5_percent": action_failure_rate < 0.05,
        "trace_contains_no_private_fields": not leak_ids,
        "all_answers_have_existing_evidence": not missing_evidence,
        "stale_conflicts_invalidate_old_fact": not invalidation_errors,
        "executed_cost_matches_shortest_planner": not movement_cost_mismatches,
        "stale_memory_error_reduction_at_least_30_points": (
            1.0 - stale_error_rate >= 0.30
        ),
        "new_observation_reduction_at_least_25_percent": (
            1.0 - reobserve_rate >= 0.25
        ),
    }
    acceptance["gate5_pass"] = all(acceptance.values())
    report = {
        "schema_version": 1,
        "primary_policy": primary_policy,
        "trace_count": count,
        "group_count": len({item["group_id"] for item in traces}),
        "online_answer_accuracy": accuracy,
        "offline_answer_accuracy": offline_accuracy,
        "online_offline_answer_disagreement_rate": len(answer_disagreements)
        / float(max(count, 1)),
        "stale_memory_error_rate": stale_error_rate,
        "reobserve_rate": reobserve_rate,
        "new_observation_count": reobserved,
        "movement_steps": sum(item["movement_steps"] for item in traces),
        "action_attempt_count": action_attempts,
        "action_failure_count": action_failures,
        "action_failure_rate": action_failure_rate,
        "invalidated_trace_count": sum(
            bool(item.get("invalidated_fact_ids")) for item in traces
        ),
        "leak_episode_ids": leak_ids[:20],
        "missing_evidence_episode_ids": missing_evidence[:20],
        "invalidation_error_episode_ids": invalidation_errors[:20],
        "route_mismatch_episode_ids": route_mismatches[:20],
        "answer_disagreement_episode_ids": answer_disagreements[:20],
        "movement_cost_mismatch_episode_ids": movement_cost_mismatches[:20],
        "incorrect_episode_ids": incorrect[:20],
        "acceptance": acceptance,
    }
    _atomic_json(output_path, report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", required=True, type=Path)
    parser.add_argument("--public", required=True, type=Path)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--offline-predictions", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--primary-policy", default="trust3d_lambda_0.01")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = validate(
        args.traces,
        args.public,
        args.private,
        args.offline_predictions,
        args.manifest,
        args.output,
        primary_policy=args.primary_policy,
        root=args.root,
    )
    print(json.dumps(report, sort_keys=True))
    if not report["acceptance"]["gate5_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
