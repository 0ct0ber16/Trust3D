"""Validate Gate 2 public/private separation and replay invariants."""

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from trust3d.data.select_events import read_jsonl


EXPECTED_BRANCHES = {"fresh_stable", "risk_stable", "risk_stale"}
FORBIDDEN_PUBLIC_KEYS = {
    "branch",
    "current_answer",
    "hidden_intervention",
    "historical_answer",
    "intervention_applied",
    "memory_is_stale",
    "oracle_pose",
    "shortest_verification_cost",
    "state_hash",
    "verification_pose",
}
FORBIDDEN_PUBLIC_VALUES = ("risk_stale", "risk_stable", "fresh_stable")


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _public_artifacts(record, root):
    paths = [record.get("history_actions"), record.get("query_frame")]
    paths.extend(record.get("history_frames", []))
    for field in ("history_observation", "query_observation"):
        observation = record.get(field)
        if isinstance(observation, dict):
            paths.extend(observation.values())
    return sorted(
        {root / path for path in paths if isinstance(path, str)},
        key=lambda path: path.as_posix(),
    )


def _observation_artifacts(record, root, field):
    observation = record.get(field)
    if not isinstance(observation, dict):
        return []
    return [root / path for path in observation.values() if isinstance(path, str)]


def _distribution(values):
    values = sorted(value for value in values if value is not None)
    if not values:
        return {"count": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "count": len(values),
        "min": values[0],
        "median": statistics.median(values),
        "mean": statistics.mean(values),
        "max": values[-1],
    }


def _risk_public_payload(record):
    return {
        key: value
        for key, value in record.items()
        if key not in {"episode_id", "query_frame", "query_observation"}
    }


def validate(
    public_path,
    private_path,
    replay_path,
    manifest_path,
    replay_twice=False,
    minimum_source_events=19,
    gate=2,
):
    if gate not in (2, 3):
        raise ValueError("gate must be 2 or 3")
    public_path = Path(public_path)
    private_path = Path(private_path)
    replay_path = Path(replay_path)
    manifest_path = Path(manifest_path)
    public = read_jsonl(public_path)
    private = read_jsonl(private_path)
    replays = read_jsonl(replay_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    public_by_id = {item["episode_id"]: item for item in public}
    private_by_id = {item["episode_id"]: item for item in private}
    replay_by_id = defaultdict(list)
    for item in replays:
        replay_by_id[item["episode_id"]].append(item)

    leak_episodes = []
    missing_artifacts = []
    missing_private_artifacts = []
    for record in public:
        keys = set(_walk_keys(record))
        encoded = json.dumps(record, sort_keys=True).lower()
        if keys & FORBIDDEN_PUBLIC_KEYS or any(
            value in encoded for value in FORBIDDEN_PUBLIC_VALUES
        ):
            leak_episodes.append(record["episode_id"])
        for path in _public_artifacts(record, public_path.parent):
            if not path.is_file():
                missing_artifacts.append(path.as_posix())
    for record in private:
        for path in _observation_artifacts(
            record, public_path.parent, "verification_observation"
        ):
            if not path.is_file():
                missing_private_artifacts.append(path.as_posix())

    group_private = defaultdict(list)
    for record in private:
        group_private[record["group_id"]].append(record)

    complete_groups = 0
    branch_relation_errors = []
    query_pose_errors = []
    risk_public_errors = []
    risk_query_frame_errors = []
    verification_cost_errors = []
    verification_cost_group_errors = []
    symbol_errors = []
    branch_question_errors = []
    verification_available = 0
    query_hidden_count = 0
    target_visible_pixels = []
    verification_costs = []
    expected_question_indices = set(
        range(int(manifest.get("questions_per_branch", 1)))
    )
    for group_id, records in group_private.items():
        by_question = defaultdict(dict)
        duplicate_key = False
        for item in records:
            question_index = int(item.get("question_index", 0))
            branch = item["branch"]
            if branch in by_question[question_index]:
                duplicate_key = True
            by_question[question_index][branch] = item
        group_complete = (
            not duplicate_key
            and set(by_question) == expected_question_indices
            and all(
                set(by_branch) == EXPECTED_BRANCHES
                for by_branch in by_question.values()
            )
        )
        if not group_complete:
            branch_question_errors.append(group_id)
            continue
        complete_groups += 1

        poses = {
            json.dumps(public_by_id[item["episode_id"]]["query_pose"], sort_keys=True)
            for item in records
            if item["episode_id"] in public_by_id
        }
        if len(poses) != 1:
            query_pose_errors.append(group_id)

        public_records = [
            public_by_id[item["episode_id"]]
            for item in records
            if item["episode_id"] in public_by_id
        ]
        public_costs = [item.get("verification_cost") for item in public_records]
        if len(public_records) != len(records) or any(
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or cost < 0
            for cost in public_costs
        ):
            verification_cost_errors.append(group_id)
        elif len(set(public_costs)) != 1:
            verification_cost_group_errors.append(group_id)

        for question_index, by_branch in by_question.items():
            fresh = by_branch["fresh_stable"]
            risk_stable = by_branch["risk_stable"]
            risk_stale = by_branch["risk_stale"]
            if fresh["historical_answer"] != fresh["current_answer"]:
                branch_relation_errors.append(fresh["episode_id"])
            if risk_stable["historical_answer"] != risk_stable["current_answer"]:
                branch_relation_errors.append(risk_stable["episode_id"])
            if risk_stale["historical_answer"] == risk_stale["current_answer"]:
                branch_relation_errors.append(risk_stale["episode_id"])

            stable_public = public_by_id.get(risk_stable["episode_id"])
            stale_public = public_by_id.get(risk_stale["episode_id"])
            if (
                stable_public is None
                or stale_public is None
                or _risk_public_payload(stable_public)
                != _risk_public_payload(stale_public)
            ):
                risk_public_errors.append(
                    "{}:q{}".format(group_id, question_index)
                )

            stable_rounds = replay_by_id.get(risk_stable["episode_id"], [])
            stale_rounds = replay_by_id.get(risk_stale["episode_id"], [])
            if stable_rounds and stale_rounds:
                if (
                    stable_rounds[0]["query_frame_raw_sha256"]
                    != stale_rounds[0]["query_frame_raw_sha256"]
                ):
                    risk_query_frame_errors.append(
                        "{}:q{}".format(group_id, question_index)
                    )

        for record in records:
            if record.get("verification_pose") is not None and record.get(
                "shortest_verification_cost"
            ) is not None:
                verification_available += 1
                verification_costs.append(record["shortest_verification_cost"])
            if not record.get("target_visible_from_query", True):
                query_hidden_count += 1
            if record.get("target_visible_pixel_count") is not None:
                target_visible_pixels.append(record["target_visible_pixel_count"])
            rounds = replay_by_id.get(record["episode_id"], [])
            if not rounds or any(
                replay["current_answer"] != record["current_answer"]
                for replay in rounds
            ):
                symbol_errors.append(record["episode_id"])

    deterministic_episodes = 0
    replay_complete_episodes = 0
    for episode_id in private_by_id:
        rounds = replay_by_id.get(episode_id, [])
        if len(rounds) >= 2:
            replay_complete_episodes += 1
            if len({item["state_hash"] for item in rounds}) == 1:
                deterministic_episodes += 1
    denominator = max(replay_complete_episodes, 1)
    deterministic_rate = deterministic_episodes / float(denominator)
    verification_rate = verification_available / float(max(len(private), 1))
    branch_counts = Counter(item["branch"] for item in private)
    answer_counts = Counter(
        "open" if item["current_answer"] else "closed" for item in private
    )
    answer_total = float(max(len(private), 1))
    answer_fractions = {
        key: answer_counts.get(key, 0) / answer_total for key in ("open", "closed")
    }
    public_group_splits = defaultdict(set)
    for record in public:
        public_group_splits[record["group_id"]].add(record.get("split"))
    split_group_errors = sorted(
        group_id
        for group_id, splits in public_group_splits.items()
        if len(splits) != 1
    )
    required_modalities = {"rgb", "depth", "instance"}
    public_full_observations = sum(
        required_modalities.issubset(record.get("history_observation", {}))
        and required_modalities.issubset(record.get("query_observation", {}))
        for record in public
    )
    private_full_observations = sum(
        required_modalities.issubset(record.get("verification_observation", {}))
        for record in private
    )
    selected_source_events = int(
        manifest.get("selected_source_events", len(group_private))
    )
    replay_success_rate = complete_groups / float(max(selected_source_events, 1))

    file_hashes_match = all(
        (public_path.parent / name).is_file()
        and _sha256_file(public_path.parent / name) == expected_hash
        for name, expected_hash in manifest.get("files", {}).items()
    )
    acceptance = {
        "source_events_at_least_minimum": complete_groups >= minimum_source_events,
        "replay_state_hash_rate_at_least_95_percent": deterministic_rate >= 0.95,
        "all_branch_query_poses_identical": not query_pose_errors,
        "risk_stale_answer_changes": not branch_relation_errors,
        "stable_answers_unchanged": not branch_relation_errors,
        "symbolic_answers_match_metadata": not symbol_errors,
        "verification_pose_rate_at_least_90_percent": verification_rate >= 0.90,
        "public_private_separation": not leak_episodes,
        "risk_pair_public_metadata_equal": not risk_public_errors,
        "public_artifacts_exist": not missing_artifacts,
        "episode_ids_match": set(public_by_id) == set(private_by_id),
        "manifest_hashes_match": file_hashes_match,
        "branch_question_structure_complete": not branch_question_errors,
        "episode_ids_unique": (
            len(public_by_id) == len(public) and len(private_by_id) == len(private)
        ),
    }
    if replay_twice:
        acceptance["all_episodes_replayed_twice"] = (
            replay_complete_episodes == len(private)
            and all(len(replay_by_id[item]) == 2 for item in private_by_id)
        )
    if gate == 3:
        acceptance.update(
            {
                "branch_ratio_is_1_to_1_to_1": (
                    set(branch_counts) == EXPECTED_BRANCHES
                    and len(set(branch_counts.values())) == 1
                ),
                "open_closed_answers_each_at_least_30_percent": min(
                    answer_fractions.values()
                )
                >= 0.30,
                "split_groups_do_not_leak": not split_group_errors,
                "private_artifacts_exist": not missing_private_artifacts,
                "full_observation_modalities_available": (
                    public_full_observations == len(public)
                    and private_full_observations == len(private)
                ),
                "public_verification_cost_available": not verification_cost_errors,
                "public_verification_cost_group_invariant": (
                    not verification_cost_group_errors
                ),
            }
        )
        acceptance["gate3_pass"] = all(acceptance.values())
    else:
        acceptance["gate2_pass"] = all(acceptance.values())

    return {
        "schema_version": 2 if gate == 3 else 1,
        "gate": gate,
        "minimum_source_events": minimum_source_events,
        "public_episode_count": len(public),
        "private_episode_count": len(private),
        "replay_record_count": len(replays),
        "complete_source_events": complete_groups,
        "selected_source_events": selected_source_events,
        "replay_success_rate": replay_success_rate,
        "branch_counts": dict(sorted(branch_counts.items())),
        "answer_counts": dict(sorted(answer_counts.items())),
        "answer_fractions": answer_fractions,
        "object_type_counts": dict(
            sorted(
                Counter(
                    public_by_id[item["episode_id"]]["program"]["subject"].split(
                        "|", 1
                    )[0]
                    for item in private
                    if item["episode_id"] in public_by_id
                ).items()
            )
        ),
        "replay_complete_episodes": replay_complete_episodes,
        "deterministic_episodes": deterministic_episodes,
        "replay_state_hash_match_rate": deterministic_rate,
        "verification_pose_available": verification_available,
        "verification_pose_rate": verification_rate,
        "verification_cost_distribution": _distribution(verification_costs),
        "target_visible_pixel_distribution": _distribution(target_visible_pixels),
        "elapsed_steps_distribution": _distribution(
            [record.get("elapsed_steps") for record in public]
        ),
        "target_hidden_at_query_count": query_hidden_count,
        "public_full_observation_count": public_full_observations,
        "private_full_observation_count": private_full_observations,
        "public_leak_episode_count": len(leak_episodes),
        "missing_public_artifact_count": len(missing_artifacts),
        "missing_private_artifact_count": len(missing_private_artifacts),
        "duplicate_public_episode_id_count": len(public) - len(public_by_id),
        "duplicate_private_episode_id_count": len(private) - len(private_by_id),
        "branch_relation_errors": branch_relation_errors[:20],
        "branch_question_error_groups": branch_question_errors[:20],
        "split_group_error_groups": split_group_errors[:20],
        "query_pose_error_groups": query_pose_errors[:20],
        "risk_public_error_groups": risk_public_errors[:20],
        "risk_query_frame_difference_groups": risk_query_frame_errors[:20],
        "verification_cost_error_groups": verification_cost_errors[:20],
        "verification_cost_group_error_groups": (
            verification_cost_group_errors[:20]
        ),
        "symbol_error_episodes": symbol_errors[:20],
        "input_sha256": {
            "public": _sha256_file(public_path),
            "private": _sha256_file(private_path),
            "replay_records": _sha256_file(replay_path),
            "manifest": _sha256_file(manifest_path),
        },
        "acceptance": acceptance,
    }


def _atomic_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", required=True, type=Path)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--replay-records", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--replay-twice", action="store_true")
    parser.add_argument("--gate", type=int, choices=(2, 3), default=2)
    parser.add_argument("--minimum-source-events", type=int)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    root = args.public.parent
    replay_path = args.replay_records or root / "replay_records.jsonl"
    manifest_path = args.manifest or root / "manifest.json"
    minimum_source_events = args.minimum_source_events
    if minimum_source_events is None:
        minimum_source_events = 90 if args.gate == 3 else 19
    report = validate(
        args.public,
        args.private,
        replay_path,
        manifest_path,
        replay_twice=args.replay_twice,
        minimum_source_events=minimum_source_events,
        gate=args.gate,
    )
    _atomic_report(args.report, report)
    pass_key = "gate{}_pass".format(args.gate)
    print(
        "[gate{}] validated {} source events; {}={}".format(
            args.gate,
            report["complete_source_events"],
            pass_key,
            report["acceptance"][pass_key],
        )
    )
    if not report["acceptance"][pass_key]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
