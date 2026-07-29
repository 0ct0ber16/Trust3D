"""在隔离的模拟器环境中执行可恢复的 Gate 5 在线二路 Agent。"""

import hashlib
import json
import os
import platform
import traceback
from datetime import datetime
from pathlib import Path

from trust3d.agents.run_episode import (
    _load_config,
    route_public_episode,
)
from trust3d.data.build_branches import (
    BRANCHES,
    BUILD_VERSION,
    _atomic_json,
    _atomic_jsonl,
    _cache_observation,
    _canonical_bytes,
    _episode_id,
    _group_id,
    _load_checkpoint,
    _load_trajectory,
    _record_failure,
    _sha256_bytes,
    _sha256_file,
    _target_pixel_count,
    _write_checkpoint,
)
from trust3d.data.select_events import read_jsonl
from trust3d.sim.replay_prefix import replay_action, replay_prefix, source_action
from trust3d.sim.restore_scene import restore_scene
from trust3d.sim.state_hash import find_object, state_hash
from trust3d.sim.visibility_oracle import (
    execute_verification_path,
    teleport_to_pose,
)


ONLINE_BUILD_VERSION = 1


def _online_unit_id(candidate_id, branch):
    raw = "{}|{}|online".format(candidate_id, branch).encode("ascii")
    return hashlib.sha256(raw).hexdigest()[:24]


def _checkpoint_path(output, candidate_id, branch):
    return output / "checkpoints" / candidate_id / (branch + ".json")


def _source_checkpoint(source_root, candidate_id, branch, trajectory_sha, seed):
    return _load_checkpoint(
        source_root
        / "checkpoints"
        / candidate_id
        / (branch + ".round-0.json"),
        {
            "kind": "branch_round",
            "build_version": BUILD_VERSION,
            "candidate_id": candidate_id,
            "trajectory_sha256": trajectory_sha,
            "branch": branch,
            "replay_round": 0,
            "seed": seed,
            "cache_modalities": ["rgb", "depth", "instance"],
        },
    )


def _context_checkpoint(source_root, candidate_id, trajectory_sha, seed):
    return _load_checkpoint(
        source_root / "checkpoints" / candidate_id / "context.json",
        {
            "kind": "context",
            "build_version": BUILD_VERSION,
            "candidate_id": candidate_id,
            "trajectory_sha256": trajectory_sha,
            "seed": seed,
            "cache_modalities": ["rgb", "depth", "instance"],
        },
    )


def _reason_codes(route, episode):
    if route["route"] == "reobserve":
        return [
            "INTERVENTION_RISK",
            "HIGH_VOLATILITY",
            "EXPECTED_REOBSERVE_LOSS_LOWER",
        ]
    if episode["public_context"]["intervention_window"]:
        return ["INTERVENTION_RISK", "REOBSERVE_COST_TOO_HIGH"]
    return ["NO_INTERVENTION_WINDOW", "FRESH_FACT"]


def _execute_unit(
    controller,
    unit,
    config,
    source_root,
    online_root,
    alfred_json,
    seed,
    config_sha256,
):
    candidate = unit["candidate"]
    candidate_id = candidate["candidate_id"]
    branch = unit["branch"]
    trajectory, trajectory_sha = _load_trajectory(alfred_json, candidate)
    context = _context_checkpoint(source_root, candidate_id, trajectory_sha, seed)
    source = _source_checkpoint(
        source_root, candidate_id, branch, trajectory_sha, seed
    )
    if context is None or source is None:
        raise RuntimeError("Gate 3 source checkpoint is invalid")

    expected = {
        "kind": "online_unit",
        "online_build_version": ONLINE_BUILD_VERSION,
        "candidate_id": candidate_id,
        "branch": branch,
        "source_checkpoint_sha256": source["checkpoint_sha256"],
        "config_sha256": config_sha256,
    }
    path = _checkpoint_path(online_root, candidate_id, branch)
    existing = _load_checkpoint(path, expected)
    if existing is not None:
        return existing

    print("[gate5] 阶段=恢复场景 单元={}.{}".format(candidate_id[:12], branch), flush=True)
    event = restore_scene(controller, trajectory)
    event, _ = replay_prefix(controller, trajectory, candidate["action_index"])
    target_id = candidate["target_object_id"]
    historical_object = find_object(event.metadata, target_id)
    if not historical_object.get("visible", False):
        raise RuntimeError("visibility-gated history adapter cannot see target")
    history_pixels, _ = _target_pixel_count(event, target_id)
    if history_pixels < 1:
        raise RuntimeError("history target mask is empty")
    historical_value = bool(historical_object["isOpen"])
    if historical_value != context["history_answer"]:
        raise RuntimeError("online history fact differs from context")
    if state_hash(event.metadata, target_id) != context["history_state_hash"]:
        raise RuntimeError("online history state hash differs from context")

    if branch == "risk_stale":
        print("[gate5] 阶段=执行隐藏干预 单元={}.{}".format(candidate_id[:12], branch), flush=True)
        action = source_action(trajectory, candidate["action_index"])
        event, _ = replay_action(controller, action, candidate["action_index"])

    print("[gate5] 阶段=进入查询位姿 单元={}.{}".format(candidate_id[:12], branch), flush=True)
    event = teleport_to_pose(controller, context["query_pose"])
    if find_object(event.metadata, target_id).get("visible", False):
        raise RuntimeError("target unexpectedly visible at online query pose")
    if state_hash(event.metadata, target_id) != source["state_hash"]:
        raise RuntimeError("online query state hash differs from Gate 3 replay")

    routes = [
        route_public_episode(record, "trust3d", config)
        for record in unit["public_records"]
    ]
    if len({item["route"] for item in routes}) != 1:
        raise RuntimeError("question templates selected different routes")
    selected_route = routes[0]["route"]
    artifacts = []
    action_records = []
    online_observation = None
    if selected_route == "reobserve":
        executor = os.environ.get("TRUST3D_ONLINE_EXECUTOR", "shortest_path")
        print(
            "[gate5] 阶段=执行验证路径 单元={}.{} 成本={} 执行器={}".format(
                candidate_id[:12],
                branch,
                source["verification"]["cost"],
                executor,
            ),
            flush=True,
        )
        if executor == "shortest_path":
            event, action_records = execute_verification_path(
                controller,
                context["query_pose"],
                source["verification"]["pose"],
            )
        elif executor == "verified_endpoint":
            event = teleport_to_pose(controller, source["verification"]["pose"])
            action_records = [
                {
                    "action": "TeleportFull",
                    "kind": "verified_endpoint",
                    "pose": source["verification"]["pose"],
                    "success": True,
                    "planner_cost": source["verification"]["cost"],
                }
            ]
        else:
            raise ValueError("未知在线执行器: {}".format(executor))
        print("[gate5] 阶段=验证路径完成 单元={}.{}".format(candidate_id[:12], branch), flush=True)
        visible_object = find_object(event.metadata, target_id)
        if not visible_object.get("visible", False):
            raise RuntimeError("target is not visible after online reobserve")
        visible_pixels, _ = _target_pixel_count(event, target_id)
        if visible_pixels < 1:
            raise RuntimeError("online verification target mask is empty")
        answer = bool(visible_object["isOpen"])
        online_observation, artifacts = _cache_observation(
            online_root,
            _online_unit_id(candidate_id, branch) + "_verification",
            event,
            ("rgb", "depth", "instance"),
        )
    else:
        answer = historical_value

    old_fact_id = "{}.isOpen@history".format(target_id)
    new_fact_id = "{}.isOpen@online".format(target_id)
    invalidated = (
        [old_fact_id]
        if selected_route == "reobserve" and answer != historical_value
        else []
    )
    traces = []
    for episode, route in zip(unit["public_records"], routes):
        if selected_route == "reobserve":
            new_frames = [
                "data/episodes/online/" + online_observation["rgb"]
            ]
            evidence = list(new_frames)
        else:
            new_frames = []
            evidence = [
                "data/episodes/mvp/" + episode["history_observation"]["rgb"]
            ]
        traces.append(
            {
                "episode_id": episode["episode_id"],
                "group_id": episode["group_id"],
                "selected_route": selected_route.upper(),
                "required_facts": ["{}.isOpen".format(target_id)],
                "fact_reliability_before": 1.0
                - route["estimated_error_probability"],
                "reason_codes": _reason_codes(route, episode),
                "movement_steps": source["verification"]["cost"]
                if selected_route == "reobserve"
                else 0,
                "movement_action_count": len(action_records),
                "action_failure_count": 0,
                "new_observation_count": 1
                if selected_route == "reobserve"
                else 0,
                "new_frame_ids": new_frames,
                "invalidated_fact_ids": invalidated,
                "answer": answer,
                "answer_evidence": evidence,
                "answer_source": "real_observation"
                if selected_route == "reobserve"
                else "historical_real_observation",
                "fact_before": {
                    "fact_id": old_fact_id,
                    "value": historical_value,
                    "source_type": "real_observation",
                    "volatility": "articulated_state",
                },
                "fact_after": {
                    "fact_id": new_fact_id,
                    "value": answer,
                    "source_type": "real_observation",
                    "invalidates": invalidated,
                }
                if selected_route == "reobserve"
                else None,
                "public_input_sha256": _sha256_bytes(
                    _canonical_bytes(episode)
                ),
            }
        )

    value = dict(expected)
    value.update(
        {
            "status": "success",
            "unit_id": _online_unit_id(candidate_id, branch),
            "episode_ids": [item["episode_id"] for item in traces],
            "traces": traces,
            "movement_actions": action_records,
            "artifacts": artifacts,
        }
    )
    _write_checkpoint(path, value)
    return _load_checkpoint(path, expected)


def _build_units(public, selection, source_manifest, exclusions, source_root):
    public_by_id = {item["episode_id"]: item for item in public}
    questions_per_branch = int(source_manifest["questions_per_branch"])
    excluded = set(exclusions.get("group_ids", []))
    if excluded != set(source_manifest.get("excluded_group_ids", [])):
        raise ValueError("Gate 3 manifest and exclusion config disagree")
    units = []
    for candidate in selection["candidates"]:
        candidate_id = candidate["candidate_id"]
        if _group_id(candidate_id) in excluded:
            continue
        for branch in BRANCHES:
            records = []
            for question_index in range(questions_per_branch):
                episode_id = _episode_id(
                    candidate_id,
                    branch,
                    source_manifest["seed"],
                    question_index,
                )
                if episode_id not in public_by_id:
                    raise ValueError("public episode is missing: {}".format(episode_id))
                records.append(public_by_id[episode_id])
            units.append(
                {
                    "candidate": candidate,
                    "branch": branch,
                    "public_records": sorted(
                        records, key=lambda item: item["episode_id"]
                    ),
                }
            )
    expected_ids = {
        item["episode_id"]
        for unit in units
        for item in unit["public_records"]
    }
    if expected_ids != set(public_by_id):
        raise ValueError("environment mapping does not cover public episodes exactly")
    return units


def run_online(
    episodes_path,
    config_path,
    selection_path,
    source_root,
    online_root,
    alfred_json,
    exclusion_path,
    output_path,
    max_units=None,
):
    source_root = Path(source_root).resolve()
    online_root = Path(online_root).resolve()
    online_root.mkdir(parents=True, exist_ok=True)
    public = read_jsonl(episodes_path)
    config = _load_config(config_path)
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    source_manifest = json.loads(
        (source_root / "manifest.json").read_text(encoding="utf-8")
    )
    exclusions = json.loads(Path(exclusion_path).read_text(encoding="utf-8"))
    config_sha256 = _sha256_file(config_path)
    units = _build_units(
        public, selection, source_manifest, exclusions, source_root
    )
    seed = int(source_manifest["seed"])

    completed = {}
    pending = []
    for unit in units:
        candidate_id = unit["candidate"]["candidate_id"]
        branch = unit["branch"]
        trajectory, trajectory_sha = _load_trajectory(alfred_json, unit["candidate"])
        del trajectory
        source = _source_checkpoint(
            source_root, candidate_id, branch, trajectory_sha, seed
        )
        if source is None:
            raise RuntimeError("source branch checkpoint is unavailable")
        expected = {
            "kind": "online_unit",
            "online_build_version": ONLINE_BUILD_VERSION,
            "candidate_id": candidate_id,
            "branch": branch,
            "source_checkpoint_sha256": source["checkpoint_sha256"],
            "config_sha256": config_sha256,
        }
        checkpoint = _load_checkpoint(
            _checkpoint_path(online_root, candidate_id, branch), expected
        )
        key = (candidate_id, branch)
        if checkpoint is None:
            pending.append(unit)
        else:
            completed[key] = checkpoint

    failures_this_run = 0
    if pending and max_units != 0:
        from ai2thor.controller import Controller

        screen_size = int(os.environ.get("TRUST3D_ONLINE_SCREEN_SIZE", "300"))
        if screen_size < 100:
            raise ValueError("TRUST3D_ONLINE_SCREEN_SIZE 不能小于 100")
        print("[gate5] 在线渲染分辨率={}x{}".format(screen_size, screen_size), flush=True)
        controller = Controller(quality="Low")
        controller.start(
            player_screen_width=max(300, screen_size),
            player_screen_height=max(300, screen_size),
        )
        if screen_size < 300:
            event = controller.step(
                {"action": "ChangeResolution", "x": screen_size, "y": screen_size}
            )
            if not event.metadata.get("lastActionSuccess", False):
                raise RuntimeError(
                    "ChangeResolution failed: {}".format(
                        event.metadata.get("errorMessage", "")
                    )
                )
        try:
            for index, unit in enumerate(pending):
                if max_units is not None and index >= max_units:
                    break
                candidate_id = unit["candidate"]["candidate_id"]
                branch = unit["branch"]
                unit_id = "{}.{}".format(candidate_id, branch)
                try:
                    checkpoint = _execute_unit(
                        controller,
                        unit,
                        config,
                        source_root,
                        online_root,
                        alfred_json,
                        seed,
                        config_sha256,
                    )
                    completed[(candidate_id, branch)] = checkpoint
                    print(
                        "[gate5] wrote {} {}".format(candidate_id[:12], branch),
                        flush=True,
                    )
                except Exception as exc:
                    failures_this_run += 1
                    print(
                        "[gate5] FAILED {}: {}".format(unit_id, exc), flush=True
                    )
                    _record_failure(
                        online_root,
                        unit_id,
                        {
                            "unit_id": unit_id,
                            "candidate_id": candidate_id,
                            "branch": branch,
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

    traces = [
        trace
        for checkpoint in completed.values()
        for trace in checkpoint.get("traces", [])
    ]
    traces.sort(key=lambda item: item["episode_id"])
    _atomic_jsonl(output_path, traces)
    manifest = {
        "schema_version": 1,
        "host": platform.node(),
        "pid": os.getpid(),
        "expected_unit_count": len(units),
        "completed_unit_count": len(completed),
        "pending_unit_count": len(units) - len(completed),
        "failures_this_run": failures_this_run,
        "expected_trace_count": len(public),
        "trace_count": len(traces),
        "trace_sha256": _sha256_file(output_path),
        "config_sha256": config_sha256,
    }
    _atomic_json(online_root / "manifest.json", manifest)
    return manifest
