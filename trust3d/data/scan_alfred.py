"""Scan ALFRED trajectory JSON without launching AI2-THOR."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


STATE_ACTIONS = {"OpenObject", "CloseObject"}
COUNTED_ACTIONS = {
    "OpenObject",
    "CloseObject",
    "ToggleObjectOn",
    "ToggleObjectOff",
    "PickupObject",
    "PutObject",
}
MVP_ACTIONS = {"OpenObject", "CloseObject"}
MVP_OBJECT_TYPES = {"Cabinet", "Drawer", "Fridge", "Microwave", "Safe"}
INITIAL_STATE_SOURCE = (
    "preceding low_actions, or the first OpenObject/CloseObject precondition"
)


def _counter_dict(counter):
    return {key: counter[key] for key in sorted(counter)}


def _detect_layout_root(alfred_json):
    alfred_root = alfred_json.parent.parent
    candidate = alfred_root / "gen" / "layouts"
    return candidate if candidate.is_dir() else None


def _load_layout_type_ids(scene, layout_root, cache):
    if scene in cache:
        return cache[scene]
    if layout_root is None:
        raise ValueError("ALFRED layout directory was not found")

    source = layout_root / (scene + "-openable.json")
    try:
        with source.open("r", encoding="utf-8") as handle:
            openable = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read {}: {}".format(source, exc))
    if not isinstance(openable, dict):
        raise ValueError("{} must contain an object-ID mapping".format(source))

    type_ids = {}
    for object_id in openable:
        if not isinstance(object_id, str) or "|" not in object_id:
            raise ValueError("{} contains an invalid object ID".format(source))
        object_type = object_id.split("|", 1)[0]
        type_ids.setdefault(object_type, set()).add(object_id)
    cache[scene] = (type_ids, source)
    return cache[scene]


def _candidate_id(split, trajectory_id, action_index, target_object_id):
    value = "\0".join(
        [split, trajectory_id, str(action_index), target_object_id]
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _scan_trajectory(data, source_json, layout_root, layout_cache, requested_events):
    if not isinstance(data, dict):
        raise ValueError("trajectory root must be a JSON object")

    scene_data = data.get("scene")
    plan = data.get("plan")
    if not isinstance(scene_data, dict) or not isinstance(plan, dict):
        raise ValueError("trajectory must contain scene and plan objects")

    scene = scene_data.get("floor_plan")
    trajectory_id = data.get("task_id")
    low_actions = plan.get("low_actions")
    if not isinstance(scene, str) or not scene:
        raise ValueError("scene.floor_plan must be a non-empty string")
    if not isinstance(trajectory_id, str) or not trajectory_id:
        raise ValueError("task_id must be a non-empty string")
    if not isinstance(low_actions, list):
        raise ValueError("plan.low_actions must be a list")

    layout_type_ids, layout_source = _load_layout_type_ids(
        scene, layout_root, layout_cache
    )
    pose_type_counts = Counter()
    object_poses = scene_data.get("object_poses", [])
    if isinstance(object_poses, list):
        for pose in object_poses:
            if not isinstance(pose, dict):
                continue
            object_name = pose.get("objectName")
            if isinstance(object_name, str) and object_name:
                pose_type_counts[object_name.split("_", 1)[0]] += 1
    trajectory_type_ids = {}
    for low_action in low_actions:
        if not isinstance(low_action, dict):
            continue
        api_action = low_action.get("api_action")
        if not isinstance(api_action, dict):
            continue
        for id_key in ("objectId", "receptacleObjectId"):
            object_id = api_action.get(id_key)
            if isinstance(object_id, str) and "|" in object_id:
                object_type = object_id.split("|", 1)[0]
                trajectory_type_ids.setdefault(object_type, set()).add(object_id)
    split = source_json.parts[0]
    object_is_open = {}
    action_counts = Counter()
    candidates = []
    action_errors = []

    for action_index, low_action in enumerate(low_actions):
        if not isinstance(low_action, dict):
            continue
        api_action = low_action.get("api_action")
        if not isinstance(api_action, dict):
            continue
        action = api_action.get("action")
        if action in COUNTED_ACTIONS:
            action_counts[action] += 1

        target_object_id = api_action.get("objectId")
        state_before = None
        if action in STATE_ACTIONS and isinstance(target_object_id, str):
            state_before = object_is_open.get(
                target_object_id, action == "CloseObject"
            )

        if action in requested_events:
            if not isinstance(target_object_id, str) or "|" not in target_object_id:
                action_errors.append(
                    {
                        "source_json": source_json.as_posix(),
                        "action_index": action_index,
                        "error": "requested event has no valid objectId",
                    }
                )
            else:
                target_type = target_object_id.split("|", 1)[0]
                layout_ids = layout_type_ids.get(target_type, set())
                action_ids = trajectory_type_ids.get(target_type, set())
                static_id_count = len(layout_ids | action_ids)
                pose_count = pose_type_counts.get(target_type, 0)
                if pose_count >= static_id_count and pose_count > 0:
                    same_type_count = pose_count
                    count_source = "scene.object_poses"
                    count_is_lower_bound = False
                else:
                    same_type_count = static_id_count
                    if layout_ids and action_ids.issubset(layout_ids):
                        count_source = layout_source.name
                        count_is_lower_bound = False
                    elif layout_ids:
                        count_source = layout_source.name + " + trajectory action IDs"
                        count_is_lower_bound = True
                    else:
                        count_source = "trajectory action IDs"
                        count_is_lower_bound = True

                action_is_mvp = action in MVP_ACTIONS
                object_is_mvp = target_type in MVP_OBJECT_TYPES
                candidates.append(
                    {
                        "candidate_id": _candidate_id(
                            split,
                            trajectory_id,
                            action_index,
                            target_object_id,
                        ),
                        "split": split,
                        "scene": scene,
                        "scene_num": scene_data.get("scene_num"),
                        "trajectory_id": trajectory_id,
                        "task_type": data.get("task_type"),
                        "action": action,
                        "action_index": action_index,
                        "high_action_index": low_action.get("high_idx"),
                        "target_object_id": target_object_id,
                        "target_object_type": target_type,
                        "prefix_length": action_index,
                        "initial_state": {
                            "is_open": state_before,
                            "source": INITIAL_STATE_SOURCE,
                        },
                        "same_type_object_count": same_type_count,
                        "same_type_count_source": count_source,
                        "same_type_count_is_lower_bound": count_is_lower_bound,
                        "mvp_action_whitelist": action_is_mvp,
                        "mvp_object_whitelist": object_is_mvp,
                        "mvp_whitelist": action_is_mvp and object_is_mvp,
                        "source_json": source_json.as_posix(),
                        "source_action_path": (
                            "plan.low_actions[{}].api_action".format(action_index)
                        ),
                    }
                )

        if action == "OpenObject" and isinstance(target_object_id, str):
            object_is_open[target_object_id] = True
        elif action == "CloseObject" and isinstance(target_object_id, str):
            object_is_open[target_object_id] = False

    return candidates, action_counts, action_errors


def scan_dataset(alfred_json, events, layout_root=None):
    alfred_json = Path(alfred_json).resolve()
    if not alfred_json.is_dir():
        raise ValueError("ALFRED JSON directory does not exist: {}".format(alfred_json))
    requested_events = set(events)
    if not requested_events:
        raise ValueError("at least one event is required")

    if layout_root is None:
        layout_root = _detect_layout_root(alfred_json)
    else:
        layout_root = Path(layout_root).resolve()
    if layout_root is None or not layout_root.is_dir():
        raise ValueError(
            "ALFRED layouts were not found; pass --layouts explicitly"
        )

    files = sorted(alfred_json.rglob("traj_data.json"))
    if not files:
        raise ValueError("no traj_data.json files found under {}".format(alfred_json))

    manifest = hashlib.sha256()
    candidates = []
    parse_errors = []
    action_errors = []
    skipped_unannotated = Counter()
    layout_cache = {}
    action_counts = Counter()
    split_trajectory_counts = Counter()
    total_bytes = 0
    parsed_count = 0

    for path in files:
        relative_path = path.relative_to(alfred_json)
        split_trajectory_counts[relative_path.parts[0]] += 1
        try:
            raw = path.read_bytes()
            total_bytes += len(raw)
            content_digest = hashlib.sha256(raw).hexdigest()
            manifest.update(relative_path.as_posix().encode("utf-8"))
            manifest.update(b"\0")
            manifest.update(content_digest.encode("ascii"))
            manifest.update(b"\n")
            data = json.loads(raw.decode("utf-8"))
            split = relative_path.parts[0]
            if (
                split in {"tests_seen", "tests_unseen"}
                and isinstance(data, dict)
                and "plan" not in data
            ):
                skipped_unannotated[split] += 1
                continue
            found, counts, found_errors = _scan_trajectory(
                data,
                relative_path,
                layout_root,
                layout_cache,
                requested_events,
            )
            candidates.extend(found)
            action_counts.update(counts)
            action_errors.extend(found_errors)
            parsed_count += 1
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            parse_errors.append(
                {
                    "source_json": relative_path.as_posix(),
                    "error": type(exc).__name__ + ": " + str(exc),
                }
            )

    candidates_by_split = Counter(item["split"] for item in candidates)
    candidates_by_action = Counter(item["action"] for item in candidates)
    candidates_by_object_type = Counter(
        item["target_object_type"] for item in candidates
    )
    mvp_count = sum(item["mvp_whitelist"] for item in candidates)
    processable_count = len(files) - sum(skipped_unannotated.values())
    parse_error_rate = len(parse_errors) / float(max(processable_count, 1))
    traceable = not action_errors and all(
        item.get("source_json")
        and isinstance(item.get("action_index"), int)
        and item.get("source_action_path")
        for item in candidates
    )
    acceptance = {
        "candidate_count_at_least_100": len(candidates) >= 100,
        "valid_unseen_count_at_least_20": candidates_by_split["valid_unseen"] >= 20,
        "parse_error_rate_below_1_percent": parse_error_rate < 0.01,
        "all_candidates_traceable": traceable,
    }
    acceptance["gate1_pass"] = all(acceptance.values())

    stats = {
        "schema_version": 1,
        "requested_events": sorted(requested_events),
        "mvp_actions": sorted(MVP_ACTIONS),
        "mvp_object_types": sorted(MVP_OBJECT_TYPES),
        "trajectory_file_count": len(files),
        "trajectory_json_bytes": total_bytes,
        "processable_trajectory_count": processable_count,
        "trajectories_parsed": parsed_count,
        "split_trajectory_counts": _counter_dict(split_trajectory_counts),
        "skipped_unannotated_count": sum(skipped_unannotated.values()),
        "skipped_unannotated_by_split": _counter_dict(skipped_unannotated),
        "parse_error_count": len(parse_errors),
        "parse_error_rate": parse_error_rate,
        "parse_error_examples": parse_errors[:20],
        "action_validation_error_count": len(action_errors),
        "action_validation_error_examples": action_errors[:20],
        "all_counted_action_counts": _counter_dict(action_counts),
        "candidate_count": len(candidates),
        "candidates_by_split": _counter_dict(candidates_by_split),
        "candidates_by_action": _counter_dict(candidates_by_action),
        "candidates_by_object_type": _counter_dict(candidates_by_object_type),
        "mvp_whitelist_count": mvp_count,
        "distinct_candidate_scenes": len({item["scene"] for item in candidates}),
        "distinct_candidate_trajectories": len(
            {(item["split"], item["trajectory_id"]) for item in candidates}
        ),
        "dataset_manifest": {
            "algorithm": "sha256(relative_path + NUL + sha256(file_bytes) + LF)",
            "sha256": manifest.hexdigest(),
        },
        "acceptance": acceptance,
    }
    return candidates, stats


def write_outputs(candidates, stats, output, stats_output):
    output = Path(output)
    stats_output = Path(stats_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stats_output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(candidate, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
    with stats_output.open("w", encoding="utf-8") as handle:
        json.dump(stats, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alfred-json", required=True, type=Path)
    parser.add_argument("--events", required=True, nargs="+")
    parser.add_argument("--layouts", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stats", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    candidates, stats = scan_dataset(args.alfred_json, args.events, args.layouts)
    write_outputs(candidates, stats, args.output, args.stats)
    print(
        "[gate1] scanned {} trajectories; found {} candidates; gate1_pass={}".format(
            stats["trajectory_file_count"],
            stats["candidate_count"],
            stats["acceptance"]["gate1_pass"],
        )
    )


if __name__ == "__main__":
    main()
