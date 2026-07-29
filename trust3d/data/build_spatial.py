"""构建可恢复的 Gate 6 空间记忆实验，并隔离公开输入与私有真值。"""

import argparse
import hashlib
import json
import math
import platform
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

from trust3d.data.build_branches import (
    _atomic_json,
    _atomic_jsonl,
    _atomic_npy,
    _cache_observation,
    _canonical_bytes,
    _load_checkpoint,
    _load_trajectory,
    _record_failure,
    _sha256_bytes,
    _sha256_file,
    _write_checkpoint,
)
from trust3d.geometry.egocentric import (
    angular_separation,
    camera_position,
    diagnostic_query_yaw,
    object_center,
    planar_bearing,
    rgbd_mask_centroid,
    spatial_labels,
)
from trust3d.sim.replay_prefix import replay_prefix
from trust3d.sim.restore_scene import restore_scene
from trust3d.sim.spatial_intervention import (
    distance,
    scene_pairs,
    swap_objects,
)
from trust3d.sim.state_hash import canonical_pose, find_object
from trust3d.sim.visibility_oracle import (
    _reachable_positions,
    find_verification_pose,
    teleport_to_pose,
)


SPATIAL_BUILD_VERSION = 6
BRANCHES = ("fresh_stable", "risk_stable", "risk_stale")
QUESTIONS = (
    ("left_right", "目标在我的左侧还是右侧？"),
    ("front_behind", "目标在我的前方还是后方？"),
    ("which_closer", "目标和参照物中，哪一个离我更近？"),
    ("target_nearer", "目标是否比参照物离我更近？"),
)


def _group_id(candidate_id):
    return "s_" + candidate_id[:24]


def _episode_id(candidate_id, branch, question_type, seed):
    raw = "{}|{}|{}|{}|spatial".format(
        candidate_id, branch, question_type, seed
    ).encode("ascii")
    return "se_" + hashlib.sha256(raw).hexdigest()[:24]


def _checkpoint_path(output, candidate_id):
    return output / "checkpoints" / candidate_id / "context.json"


def _relative(path, root):
    return Path(path).relative_to(root).as_posix()


def _depth_scale(depth):
    valid = np.asarray(depth)[np.isfinite(depth) & (np.asarray(depth) > 0)]
    if valid.size == 0:
        raise RuntimeError("深度图没有有效像素")
    return 0.001 if float(np.median(valid)) > 20.0 else 1.0


def _capture_scene(output, stem, event, object_ids):
    observation, artifacts = _cache_observation(
        output, stem, event, ("rgb", "depth", "instance")
    )
    estimates = {}
    scale = _depth_scale(event.depth_frame)
    camera = camera_position(event.metadata)
    agent = event.metadata["agent"]
    field_of_view = float(event.metadata.get("fov", 90.0))
    masks = getattr(event, "instance_masks", {}) or {}
    mask_paths = {}
    for role, object_id in object_ids.items():
        if object_id not in masks:
            raise RuntimeError("目标实例掩码不可用: {}".format(object_id))
        mask = np.asarray(masks[object_id], dtype=np.uint8)
        if int(np.count_nonzero(mask)) < 1:
            raise RuntimeError("目标实例掩码为空: {}".format(object_id))
        path = output / "cache" / "mask" / (stem + "_" + role + ".npy")
        digest = _atomic_npy(path, mask)
        relative = _relative(path, output)
        mask_paths[role] = relative
        artifacts.append({"path": relative, "sha256": digest})
        estimates[role] = rgbd_mask_centroid(
            event.depth_frame,
            mask,
            camera,
            rotation_y=agent["rotation"]["y"],
            horizon=agent["cameraHorizon"],
            field_of_view=field_of_view,
            depth_scale=scale,
            radial_depth=True,
        )
    observation["masks"] = mask_paths
    return observation, artifacts, estimates


def _query_candidate(position, first, second):
    first_distance = distance(position, first)
    second_distance = distance(position, second)
    first_bearing = planar_bearing(position, first)
    second_bearing = planar_bearing(position, second)
    separation = angular_separation(first_bearing, second_bearing)
    if not 15.0 <= separation <= 150.0:
        return None
    if abs(first_distance - second_distance) < 0.5:
        return None
    yaw = diagnostic_query_yaw(position, first, second)
    pose = {
        "x": position["x"],
        "y": position["y"],
        "z": position["z"],
        "rotation_y": yaw,
        "horizon": 30.0,
    }
    first_labels = spatial_labels(first, pose)
    second_labels = spatial_labels(second, pose)
    if first_labels["left_right"] == second_labels["left_right"]:
        return None
    if first_labels["front_behind"] != "behind":
        return None
    if second_labels["front_behind"] != "behind":
        return None
    if min(first_labels["right_margin"], second_labels["right_margin"]) < 0.2:
        return None
    score = (-separation, -abs(first_distance - second_distance), pose["x"], pose["z"])
    return score, pose


def _choose_query_pose(controller, target_id, donor_id, first, second):
    original = canonical_pose(controller.last_event.metadata)
    candidates = []
    for position in _reachable_positions(controller):
        value = _query_candidate(position, first, second)
        if value is not None:
            candidates.append(value)
    try:
        for _, pose in sorted(candidates, key=lambda item: item[0]):
            event = teleport_to_pose(controller, pose)
            if find_object(event.metadata, target_id).get("visible", False):
                continue
            if find_object(event.metadata, donor_id).get("visible", False):
                continue
            return canonical_pose(event.metadata)
    finally:
        teleport_to_pose(controller, original)
    raise RuntimeError("找不到同时隐藏两个物体的诊断查询位姿")


def _simulator_answer(question_type, points, query_pose):
    target = points["target"]
    donor = points["donor"]
    if question_type in {"left_right", "front_behind"}:
        bearing = planar_bearing(query_pose, target)
        delta = (bearing - query_pose["rotation_y"] + 180.0) % 360.0 - 180.0
        if question_type == "left_right":
            return "right" if delta > 0 else "left"
        return "front" if abs(delta) < 90.0 else "behind"
    target_distance = distance(query_pose, target)
    donor_distance = distance(query_pose, donor)
    if question_type == "which_closer":
        return "target" if target_distance < donor_distance else "reference"
    if question_type == "target_nearer":
        return target_distance < donor_distance
    raise ValueError("未知空间问题: {}".format(question_type))


def _tool_answer(question_type, points, query_pose):
    if question_type in {"left_right", "front_behind"}:
        return spatial_labels(points["target"], query_pose)[question_type]
    target_distance = spatial_labels(points["target"], query_pose)["distance"]
    donor_distance = spatial_labels(points["donor"], query_pose)["distance"]
    if question_type == "which_closer":
        return "target" if target_distance < donor_distance else "reference"
    return target_distance < donor_distance


def _branch_geometry(points, rgbd, oracles, observations):
    return {
        "gt": points,
        "rgbd": rgbd,
        "verification": oracles,
        "observations": observations,
    }


def _pixel_difference(first, second, scale=1.0):
    first = np.asarray(first)
    second = np.asarray(second)
    if first.shape != second.shape:
        raise RuntimeError("查询帧形状不一致: {} != {}".format(first.shape, second.shape))
    difference = np.abs(first.astype(np.float64) - second.astype(np.float64))
    changed = difference > 0
    if changed.ndim == 3:
        changed = np.any(changed, axis=2)
    return {
        "exact_match": bool(np.array_equal(first, second)),
        "mean_absolute_difference": float(np.mean(difference) / scale),
        "maximum_absolute_difference": float(np.max(difference) / scale),
        "changed_pixel_fraction": float(np.mean(changed)),
    }


def _query_visual_difference(stable_pixels, stale_event):
    stale_pixels = {
        "rgb": stale_event.frame,
        "depth": stale_event.depth_frame,
        "instance": stale_event.instance_segmentation_frame,
    }
    return {
        modality: _pixel_difference(
            stable_pixels[modality],
            stale_pixels[modality],
            scale=255.0 if modality in {"rgb", "instance"} else 1.0,
        )
        for modality in stable_pixels
    }


def _restore_history(controller, trajectory, action_index):
    event = restore_scene(controller, trajectory)
    event, _ = replay_prefix(controller, trajectory, action_index)
    return event


def _verification_geometry(controller, output, stem, query_pose, object_ids):
    points = {}
    rgbd = {}
    observations = {}
    oracles = {}
    artifacts = []
    for role, object_id in object_ids.items():
        oracle = find_verification_pose(controller, object_id, query_pose)
        if oracle["cost"] is None:
            raise RuntimeError("最短验证成本不可用")
        event = teleport_to_pose(controller, oracle["pose"])
        obj = find_object(event.metadata, object_id)
        if not obj.get("visible", False):
            raise RuntimeError("验证位姿没有看到物体: {}".format(object_id))
        observation, current_artifacts, estimates = _capture_scene(
            output, stem + "_" + role, event, {role: object_id}
        )
        points[role] = object_center(obj)
        rgbd[role] = estimates[role]
        observations[role] = observation
        oracles[role] = oracle
        artifacts.extend(current_artifacts)
        teleport_to_pose(controller, query_pose)
    return points, rgbd, observations, oracles, artifacts


def _build_context(controller, candidate, trajectory, trajectory_sha, output, seed):
    path = _checkpoint_path(output, candidate["candidate_id"])
    expected = {
        "kind": "spatial_context",
        "spatial_build_version": SPATIAL_BUILD_VERSION,
        "candidate_id": candidate["candidate_id"],
        "trajectory_sha256": trajectory_sha,
        "seed": seed,
    }
    existing = _load_checkpoint(path, expected)
    if existing is not None:
        return existing

    history_event = _restore_history(
        controller, trajectory, candidate["action_index"]
    )
    pairs = scene_pairs(history_event)
    if not pairs:
        raise RuntimeError("场景中没有相距足够远的两个可拾取物体")
    group_id = _group_id(candidate["candidate_id"])
    pair_failures = []
    for pair_index, pair in enumerate(pairs):
        if pair_index:
            history_event = _restore_history(
                controller, trajectory, candidate["action_index"]
            )
        target = find_object(history_event.metadata, pair[0]["objectId"])
        donor = find_object(history_event.metadata, pair[1]["objectId"])
        object_ids = {"target": target["objectId"], "donor": donor["objectId"]}
        initial_points = {
            "target": object_center(target),
            "donor": object_center(donor),
        }
        try:
            query_pose = _choose_query_pose(
                controller,
                target["objectId"],
                donor["objectId"],
                initial_points["target"],
                initial_points["donor"],
            )
            teleport_to_pose(controller, query_pose)
            history = _verification_geometry(
                controller,
                output,
                group_id + "_history",
                query_pose,
                object_ids,
            )
            (
                history_points,
                history_rgbd,
                history_observations,
                history_oracles,
                artifacts,
            ) = history
            break
        except Exception as exc:
            pair_failures.append(
                {
                    "target_object_id": target["objectId"],
                    "donor_object_id": donor["objectId"],
                    "error": type(exc).__name__ + ": " + str(exc),
                }
            )
    else:
        raise RuntimeError(
            "所有同类型物体对均未通过查询位姿与历史可见性验证: {}".format(
                json.dumps(pair_failures, ensure_ascii=False, sort_keys=True)
            )
        )

    stable_query_event = teleport_to_pose(controller, query_pose)
    if any(
        find_object(stable_query_event.metadata, object_id).get("visible", False)
        for object_id in object_ids.values()
    ):
        raise RuntimeError("稳定分支查询帧泄漏目标")
    stable_pixels = {
        "rgb": stable_query_event.frame.copy(),
        "depth": stable_query_event.depth_frame.copy(),
        "instance": stable_query_event.instance_segmentation_frame.copy(),
    }
    stable_query_observation, query_artifacts, _ = _capture_scene(
        output, group_id + "_query_stable", stable_query_event, {}
    )
    artifacts.extend(query_artifacts)
    stable_points = history_points
    stable_rgbd = history_rgbd
    stable_observations = history_observations
    stable_oracles = history_oracles

    _restore_history(controller, trajectory, candidate["action_index"])
    _, swap = swap_objects(controller, target["objectId"], donor["objectId"])
    stale_query_event = teleport_to_pose(controller, query_pose)
    if any(
        find_object(stale_query_event.metadata, object_id).get("visible", False)
        for object_id in object_ids.values()
    ):
        raise RuntimeError("陈旧分支查询帧泄漏目标")
    query_visual_difference = _query_visual_difference(
        stable_pixels, stale_query_event
    )
    if query_visual_difference["rgb"]["mean_absolute_difference"] > 0.01:
        raise RuntimeError(
            "风险分支查询 RGB 差异过大: {}".format(
                json.dumps(
                    query_visual_difference["rgb"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        )
    stale_query_observation, query_artifacts, _ = _capture_scene(
        output, group_id + "_query_stale", stale_query_event, {}
    )
    artifacts.extend(query_artifacts)
    stale = _verification_geometry(
        controller, output, group_id + "_stale", query_pose, object_ids
    )
    stale_points, stale_rgbd, stale_observations, stale_oracles, stale_artifacts = stale
    artifacts.extend(stale_artifacts)

    if distance(history_points["target"], stale_points["target"]) < 0.5:
        raise RuntimeError("目标位置变化不足 0.5 米")
    if max(
        stable_oracles["target"]["cost"] + stable_oracles["donor"]["cost"],
        stale_oracles["target"]["cost"] + stale_oracles["donor"]["cost"],
    ) >= 50:
        raise RuntimeError("双物体验证成本过高，主策略不会重新观察")
    changed_questions = sum(
        _simulator_answer(question_type, history_points, query_pose)
        != _simulator_answer(question_type, stale_points, query_pose)
        for question_type, _ in QUESTIONS
    )
    if changed_questions < 3:
        raise RuntimeError("位置交换没有改变至少三个空间问题答案")

    value = dict(expected)
    value.update(
        {
            "status": "success",
            "group_id": group_id,
            "target_object_id": target["objectId"],
            "target_object_type": target.get("objectType"),
            "donor_object_id": donor["objectId"],
            "donor_object_type": donor.get("objectType"),
            "query_pose": query_pose,
            "history": {
                "gt": history_points,
                "rgbd": history_rgbd,
                "observations": history_observations,
            },
            "branches": {
                "fresh_stable": _branch_geometry(
                    stable_points,
                    stable_rgbd,
                    stable_oracles,
                    stable_observations,
                ),
                "risk_stable": _branch_geometry(
                    stable_points,
                    stable_rgbd,
                    stable_oracles,
                    stable_observations,
                ),
                "risk_stale": _branch_geometry(
                    stale_points,
                    stale_rgbd,
                    stale_oracles,
                    stale_observations,
                ),
            },
            "query_observations": {
                "fresh_stable": stable_query_observation,
                "risk_stable": stable_query_observation,
                "risk_stale": stable_query_observation,
            },
            "stale_query_audit_observation": stale_query_observation,
            "query_visual_difference": query_visual_difference,
            "rejected_pair_count": len(pair_failures),
            "rejected_pairs": pair_failures,
            "position_intervention": swap,
            "changed_question_count": changed_questions,
            "artifacts": artifacts,
        }
    )
    _write_checkpoint(path, value)
    loaded = _load_checkpoint(path, expected)
    if loaded is None:
        raise RuntimeError("空间 context checkpoint 写入后校验失败")
    return loaded


def _verification_cost(context, branch, question_type):
    values = context["branches"][branch]["verification"]
    if question_type in {"left_right", "front_behind"}:
        return values["target"]["cost"]
    return values["target"]["cost"] + values["donor"]["cost"]


def _render_records(contexts, output, seed):
    public = []
    private = []
    for context in contexts:
        query_pose = context["query_pose"]
        for branch in BRANCHES:
            branch_data = context["branches"][branch]
            for question_type, question_text in QUESTIONS:
                episode_id = _episode_id(
                    context["candidate_id"], branch, question_type, seed
                )
                verification_cost = _verification_cost(
                    context, branch, question_type
                )
                public.append(
                    {
                        "episode_id": episode_id,
                        "group_id": context["group_id"],
                        "split": "valid_seen",
                        "question": {
                            "type": question_type,
                            "text": question_text,
                            "target_object_id": context["target_object_id"],
                            "reference_object_id": context["donor_object_id"]
                            if question_type in {"which_closer", "target_nearer"}
                            else None,
                        },
                        "elapsed_steps": 1 if branch == "fresh_stable" else 20,
                        "verification_cost": verification_cost,
                        "public_context": {
                            "intervention_window": branch != "fresh_stable",
                            "fact_type": "object_location",
                        },
                        "history_observations": {
                            role: {
                                key: "data/episodes/spatial30/" + value
                                for key, value in observation.items()
                                if key != "masks"
                            }
                            for role, observation in context["history"][
                                "observations"
                            ].items()
                        },
                        "query_observation": {
                            key: "data/episodes/spatial30/" + value
                            for key, value in context["query_observations"][branch].items()
                            if key == "rgb"
                        },
                    }
                )
                historical_gt = _simulator_answer(
                    question_type, context["history"]["gt"], query_pose
                )
                current_gt = _simulator_answer(
                    question_type, branch_data["gt"], query_pose
                )
                private.append(
                    {
                        "episode_id": episode_id,
                        "group_id": context["group_id"],
                        "branch": branch,
                        "question_type": question_type,
                        "historical_answer_gt": historical_gt,
                        "current_answer_gt": current_gt,
                        "historical_answer_rgbd": _tool_answer(
                            question_type, context["history"]["rgbd"], query_pose
                        ),
                        "current_answer_rgbd": _tool_answer(
                            question_type, branch_data["rgbd"], query_pose
                        ),
                        "deterministic_tool_answer": _tool_answer(
                            question_type, branch_data["gt"], query_pose
                        ),
                        "simulator_oracle_answer": current_gt,
                        "memory_is_stale": branch == "risk_stale",
                        "shortest_verification_cost": verification_cost,
                        "required_new_observations": 1
                        if question_type in {"left_right", "front_behind"}
                        else 2,
                    }
                )
    public.sort(key=lambda item: item["episode_id"])
    private.sort(key=lambda item: item["episode_id"])
    _atomic_jsonl(output / "episodes_public.jsonl", public)
    _atomic_jsonl(output / "oracle_private.jsonl", private)
    return public, private


def run_spatial(
    selection_path,
    alfred_json,
    exclusion_path,
    output,
    target_groups=30,
    seed=20260728,
    max_new_contexts=None,
):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    exclusions = json.loads(Path(exclusion_path).read_text(encoding="utf-8"))
    excluded = set(exclusions.get("group_ids", []))
    candidates = [
        item
        for item in selection["candidates"]
        if "g_" + item["candidate_id"][:24] not in excluded
    ]
    locked_path = output / "selection.json"
    locked_ids = None
    if locked_path.is_file():
        locked = json.loads(locked_path.read_text(encoding="utf-8"))
        locked_ids = locked["candidate_ids"]
        if len(locked_ids) != target_groups:
            raise RuntimeError("已锁定空间 selection 的样本数不匹配")
        by_id = {item["candidate_id"]: item for item in candidates}
        candidates = [by_id[candidate_id] for candidate_id in locked_ids]

    contexts = []
    missing = []
    for candidate in candidates:
        trajectory, trajectory_sha = _load_trajectory(alfred_json, candidate)
        del trajectory
        checkpoint = _load_checkpoint(
            _checkpoint_path(output, candidate["candidate_id"]),
            {
                "kind": "spatial_context",
                "spatial_build_version": SPATIAL_BUILD_VERSION,
                "candidate_id": candidate["candidate_id"],
                "trajectory_sha256": trajectory_sha,
                "seed": seed,
            },
        )
        if checkpoint is not None:
            contexts.append(checkpoint)
            if locked_ids is None and len(contexts) >= target_groups:
                break
        else:
            missing.append(candidate)

    simulator_started = False
    failures_this_run = 0
    new_contexts = 0
    if len(contexts) < target_groups and max_new_contexts != 0:
        from ai2thor.controller import Controller

        simulator_started = True
        controller = Controller(quality="Low")
        controller.start(player_screen_width=300, player_screen_height=300)
        try:
            for candidate in candidates:
                if any(
                    item["candidate_id"] == candidate["candidate_id"]
                    for item in contexts
                ):
                    continue
                if max_new_contexts is not None and new_contexts >= max_new_contexts:
                    break
                try:
                    trajectory, trajectory_sha = _load_trajectory(
                        alfred_json, candidate
                    )
                    context = _build_context(
                        controller,
                        candidate,
                        trajectory,
                        trajectory_sha,
                        output,
                        seed,
                    )
                    contexts.append(context)
                    new_contexts += 1
                    print(
                        "[gate6] wrote {}".format(candidate["candidate_id"][:12]),
                        flush=True,
                    )
                    if len(contexts) >= target_groups:
                        break
                except Exception as exc:
                    failures_this_run += 1
                    print(
                        "[gate6] FAILED {}: {}".format(
                            candidate["candidate_id"][:12], exc
                        ),
                        flush=True,
                    )
                    _record_failure(
                        output,
                        candidate["candidate_id"],
                        {
                            "candidate_id": candidate["candidate_id"],
                            "error": type(exc).__name__ + ": " + str(exc),
                            "captured_at_utc": datetime.utcnow()
                            .replace(microsecond=0)
                            .isoformat()
                            + "Z",
                            "traceback": traceback.format_exc(),
                        },
                    )
        finally:
            controller.stop()

    order = {item["candidate_id"]: index for index, item in enumerate(candidates)}
    contexts.sort(key=lambda item: order[item["candidate_id"]])
    contexts = contexts[:target_groups]
    if len(contexts) == target_groups and locked_ids is None:
        _atomic_json(
            locked_path,
            {
                "schema_version": 1,
                "seed": seed,
                "candidate_ids": [item["candidate_id"] for item in contexts],
            },
        )
    public, private = _render_records(contexts, output, seed)
    complete = len(contexts) == target_groups
    manifest = {
        "schema_version": 1,
        "host": platform.node(),
        "spatial_build_version": SPATIAL_BUILD_VERSION,
        "target_group_count": target_groups,
        "completed_group_count": len(contexts),
        "public_episode_count": len(public),
        "private_episode_count": len(private),
        "expected_episode_count": target_groups * len(BRANCHES) * len(QUESTIONS),
        "failures_this_run": failures_this_run,
        "new_contexts_this_run": new_contexts,
        "simulator_started": simulator_started,
        "complete": complete,
        "public_sha256": _sha256_file(output / "episodes_public.jsonl"),
        "private_sha256": _sha256_file(output / "oracle_private.jsonl"),
        "selection_sha256": _sha256_file(locked_path) if locked_path.is_file() else None,
        "source_selection_sha256": _sha256_file(selection_path),
    }
    _atomic_json(output / "manifest.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--alfred-json", required=True, type=Path)
    parser.add_argument("--exclude-groups", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-groups", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument("--max-new-contexts", type=int)
    args = parser.parse_args(argv)
    report = run_spatial(
        args.selection,
        args.alfred_json,
        args.exclude_groups,
        args.output,
        target_groups=args.target_groups,
        seed=args.seed,
        max_new_contexts=args.max_new_contexts,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["complete"] or args.max_new_contexts is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
