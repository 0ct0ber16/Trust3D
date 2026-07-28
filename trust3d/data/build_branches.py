"""Build restartable Fresh/Risk Stable/Stale Gate 2 episode branches."""

import argparse
import hashlib
import json
import os
import platform
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from trust3d.data.select_events import (
    read_jsonl,
    select_candidates,
    selection_summary,
    write_selection,
)
from trust3d.sim.replay_prefix import replay_action, replay_prefix, source_action
from trust3d.sim.restore_scene import restore_scene
from trust3d.sim.state_hash import canonical_pose, find_object, state_hash
from trust3d.sim.visibility_oracle import (
    choose_hidden_query_pose,
    find_verification_pose,
    teleport_to_pose,
    verify_cached_pose,
)


BUILD_VERSION = 1
BRANCHES = ("fresh_stable", "risk_stable", "risk_stale")
QUESTION_TEMPLATES = (
    "Is the {object_type} currently open?",
    "Is the {object_type} open at this moment?",
)


def _canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _atomic_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _atomic_png(path, frame):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    Image.fromarray(frame).save(str(temporary), format="PNG")
    temporary.replace(path)
    return _sha256_file(path)


def _atomic_npy(path, array):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        np.save(handle, array, allow_pickle=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return _sha256_file(path)


def _cache_observation(output, stem, event, modalities):
    paths = {}
    artifacts = []
    if "rgb" in modalities:
        path = output / "cache" / "rgb" / (stem + ".png")
        digest = _atomic_png(path, event.frame)
        paths["rgb"] = _relative(path, output)
        artifacts.append({"path": paths["rgb"], "sha256": digest})
    if "depth" in modalities:
        if event.depth_frame is None:
            raise RuntimeError("depth frame is unavailable")
        path = output / "cache" / "depth" / (stem + ".npy")
        digest = _atomic_npy(path, np.asarray(event.depth_frame))
        paths["depth"] = _relative(path, output)
        artifacts.append({"path": paths["depth"], "sha256": digest})
    if "instance" in modalities:
        if event.instance_segmentation_frame is None:
            raise RuntimeError("instance segmentation frame is unavailable")
        path = output / "cache" / "instance" / (stem + ".png")
        digest = _atomic_png(path, event.instance_segmentation_frame)
        paths["instance"] = _relative(path, output)
        artifacts.append({"path": paths["instance"], "sha256": digest})
    return paths, artifacts


def _target_pixel_count(event, target_object_id):
    masks = getattr(event, "instance_masks", None)
    if not isinstance(masks, dict) or target_object_id not in masks:
        raise RuntimeError("target instance mask is unavailable")
    mask = np.asarray(masks[target_object_id])
    return int(np.count_nonzero(mask)), int(mask.size)


def _checkpoint_payload(value):
    payload = dict(value)
    payload.pop("checkpoint_sha256", None)
    return payload


def _write_checkpoint(path, value):
    payload = _checkpoint_payload(value)
    value = dict(payload)
    value["checkpoint_sha256"] = _sha256_bytes(_canonical_bytes(payload))
    _atomic_json(path, value)


def _load_checkpoint(path, expected):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        digest = value.get("checkpoint_sha256")
        if digest != _sha256_bytes(_canonical_bytes(_checkpoint_payload(value))):
            return None
        for key, expected_value in expected.items():
            if value.get(key) != expected_value:
                return None
        for artifact in value.get("artifacts", []):
            artifact_path = path.parents[2] / artifact["path"]
            if not artifact_path.is_file():
                return None
            if _sha256_file(artifact_path) != artifact["sha256"]:
                return None
        return value
    except (OSError, ValueError, KeyError):
        return None


def _load_trajectory(alfred_json, candidate):
    path = Path(alfred_json) / candidate["source_json"]
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), _sha256_bytes(raw)


def _episode_id(candidate_id, branch, seed, question_index=0):
    value = "{}|{}|{}".format(candidate_id, branch, seed)
    if question_index:
        value += "|q{}".format(question_index)
    raw = value.encode("ascii")
    return "e_" + hashlib.sha256(raw).hexdigest()[:24]


def _group_id(candidate_id):
    return "g_" + candidate_id[:24]


def _relative(path, root):
    return Path(path).relative_to(root).as_posix()


def _build_context(
    controller, candidate, trajectory, trajectory_sha, output, seed, modalities
):
    context_path = output / "checkpoints" / candidate["candidate_id"] / "context.json"
    expected = {
        "kind": "context",
        "build_version": BUILD_VERSION,
        "candidate_id": candidate["candidate_id"],
        "trajectory_sha256": trajectory_sha,
        "seed": seed,
    }
    if set(modalities) != {"rgb"}:
        expected["cache_modalities"] = list(modalities)
    existing = _load_checkpoint(context_path, expected)
    if existing is not None:
        print("[gate2] resume context " + candidate["candidate_id"][:12], flush=True)
        return existing

    event = restore_scene(controller, trajectory)
    event, prefix_commands = replay_prefix(
        controller, trajectory, candidate["action_index"]
    )
    target_id = candidate["target_object_id"]
    target = find_object(event.metadata, target_id)
    if not target.get("visible", False):
        raise RuntimeError("target is not visible at historical source pose")
    history_answer = bool(target["isOpen"])
    history_pose = canonical_pose(event.metadata)
    history_state_hash = state_hash(event.metadata, target_id)
    history_frame = event.frame.copy()
    query_pose = choose_hidden_query_pose(controller, target_id, seed)
    teleport_to_pose(controller, query_pose)
    stable_oracle = find_verification_pose(
        controller, target_id, query_pose, preferred_pose=history_pose
    )

    group_id = _group_id(candidate["candidate_id"])
    actions_path = output / "cache" / "actions" / (group_id + "_prefix.json")
    _atomic_json(actions_path, prefix_commands)
    if set(modalities) == {"rgb"}:
        frame_path = output / "cache" / "rgb" / (group_id + "_history.png")
        frame_sha = _atomic_png(frame_path, history_frame)
        history_observation = {"rgb": _relative(frame_path, output)}
        observation_artifacts = [
            {"path": history_observation["rgb"], "sha256": frame_sha}
        ]
    else:
        history_event = event
        history_event.frame = history_frame
        history_observation, observation_artifacts = _cache_observation(
            output, group_id + "_history", history_event, modalities
        )
    value = dict(expected)
    value.update(
        {
            "status": "success",
            "history_answer": history_answer,
            "history_pose": history_pose,
            "history_state_hash": history_state_hash,
            "query_pose": query_pose,
            "stable_verification": stable_oracle,
            "history_observation": history_observation,
            "artifacts": [
                {
                    "path": _relative(actions_path, output),
                    "sha256": _sha256_file(actions_path),
                },
            ]
            + observation_artifacts,
        }
    )
    _write_checkpoint(context_path, value)
    print("[gate2] wrote context " + candidate["candidate_id"][:12], flush=True)
    return _load_checkpoint(context_path, expected)


def _failure_path(output, unit_id):
    directory = output / "checkpoints" / "failures"
    directory.mkdir(parents=True, exist_ok=True)
    attempt = len(list(directory.glob(unit_id + ".attempt-*.json"))) + 1
    return directory / "{}.attempt-{:03d}.json".format(unit_id, attempt)


def _record_failure(output, unit_id, details):
    details = dict(details)
    details.update(
        {
            "status": "failure",
            "captured_at_utc": datetime.utcnow().replace(microsecond=0).isoformat()
            + "Z",
            "traceback": traceback.format_exc(),
        }
    )
    _atomic_json(_failure_path(output, unit_id), details)


def _build_branch_round(
    controller,
    candidate,
    trajectory,
    trajectory_sha,
    context,
    branch,
    replay_round,
    output,
    seed,
    modalities,
):
    episode_id = _episode_id(candidate["candidate_id"], branch, seed)
    checkpoint_path = (
        output
        / "checkpoints"
        / candidate["candidate_id"]
        / "{}.round-{}.json".format(branch, replay_round)
    )
    expected = {
        "kind": "branch_round",
        "build_version": BUILD_VERSION,
        "candidate_id": candidate["candidate_id"],
        "trajectory_sha256": trajectory_sha,
        "branch": branch,
        "replay_round": replay_round,
        "seed": seed,
    }
    if set(modalities) != {"rgb"}:
        expected["cache_modalities"] = list(modalities)
    existing = _load_checkpoint(checkpoint_path, expected)
    if existing is not None:
        print(
            "[gate2] resume {} {} round {}".format(
                candidate["candidate_id"][:12], branch, replay_round
            ),
            flush=True,
        )
        return existing

    event = restore_scene(controller, trajectory)
    event, _ = replay_prefix(controller, trajectory, candidate["action_index"])
    target_id = candidate["target_object_id"]
    historical_answer = bool(find_object(event.metadata, target_id)["isOpen"])
    if historical_answer != context["history_answer"]:
        raise RuntimeError("historical answer differs from context")
    if state_hash(event.metadata, target_id) != context["history_state_hash"]:
        raise RuntimeError("historical target/pose hash differs from context")

    applied_intervention = branch == "risk_stale"
    if applied_intervention:
        action = source_action(trajectory, candidate["action_index"])
        event, _ = replay_action(controller, action, candidate["action_index"])
    current_answer = bool(find_object(event.metadata, target_id)["isOpen"])
    if applied_intervention == (current_answer == historical_answer):
        raise RuntimeError("branch state relation is inconsistent")

    event = teleport_to_pose(controller, context["query_pose"])
    target_visible_from_query = bool(
        find_object(event.metadata, target_id).get("visible", False)
    )
    if target_visible_from_query:
        raise RuntimeError("target is visible from public query pose")
    query_frame = event.frame.copy()
    query_frame_sha = _sha256_bytes(query_frame.tobytes())
    query_event = event

    if branch == "risk_stale" and replay_round > 0:
        first_path = checkpoint_path.with_name("risk_stale.round-0.json")
        first = _load_checkpoint(
            first_path,
            dict(expected, replay_round=0),
        )
        if first is None:
            raise RuntimeError("risk_stale round 0 checkpoint is unavailable")
        oracle = verify_cached_pose(
            controller,
            target_id,
            context["query_pose"],
            first["verification"],
        )
    elif branch == "risk_stale":
        oracle = find_verification_pose(
            controller,
            target_id,
            context["query_pose"],
            preferred_pose=context["history_pose"],
        )
    else:
        oracle = verify_cached_pose(
            controller,
            target_id,
            context["query_pose"],
            context["stable_verification"],
        )

    verification_observation = None
    target_visible_pixel_count = None
    target_mask_pixel_count = None
    verification_event = None
    if set(modalities) != {"rgb"}:
        verification_event = teleport_to_pose(controller, oracle["pose"])
        target_visible_pixel_count, target_mask_pixel_count = _target_pixel_count(
            verification_event, target_id
        )
        if target_visible_pixel_count < 1:
            raise RuntimeError("verification pose has an empty target mask")

    event = teleport_to_pose(controller, context["query_pose"])
    final_hash = state_hash(event.metadata, target_id)
    artifacts = []
    query_observation = None
    if replay_round == 0:
        if set(modalities) == {"rgb"}:
            query_path = output / "cache" / "rgb" / (episode_id + "_query.png")
            png_sha = _atomic_png(query_path, query_frame)
            query_observation = {"rgb": _relative(query_path, output)}
            artifacts.append(
                {"path": query_observation["rgb"], "sha256": png_sha}
            )
        else:
            query_event.frame = query_frame
            query_observation, query_artifacts = _cache_observation(
                output, episode_id + "_query", query_event, modalities
            )
            verification_observation, verification_artifacts = _cache_observation(
                output, episode_id + "_verification", verification_event, modalities
            )
            artifacts.extend(query_artifacts)
            artifacts.extend(verification_artifacts)

    verification = dict(oracle)
    if target_visible_pixel_count is not None:
        verification.update(
            {
                "target_visible_pixel_count": target_visible_pixel_count,
                "frame_pixel_count": target_mask_pixel_count,
                "target_visible_fraction": target_visible_pixel_count
                / float(target_mask_pixel_count),
            }
        )

    value = dict(expected)
    value.update(
        {
            "status": "success",
            "episode_id": episode_id,
            "group_id": _group_id(candidate["candidate_id"]),
            "historical_answer": historical_answer,
            "current_answer": current_answer,
            "memory_is_stale": current_answer != historical_answer,
            "intervention_applied": applied_intervention,
            "query_pose": canonical_pose(event.metadata),
            "query_frame_raw_sha256": query_frame_sha,
            "query_observation": query_observation,
            "target_visible_from_query": target_visible_from_query,
            "verification": verification,
            "verification_observation": verification_observation,
            "state_hash": final_hash,
            "artifacts": artifacts,
        }
    )
    _write_checkpoint(checkpoint_path, value)
    print(
        "[gate2] wrote {} {} round {}".format(
            candidate["candidate_id"][:12], branch, replay_round
        ),
        flush=True,
    )
    return _load_checkpoint(checkpoint_path, expected)


def _public_record(
    candidate, context, branch, checkpoint, output, seed, question_index,
    questions_per_branch
):
    episode_id = _episode_id(
        candidate["candidate_id"], branch, seed, question_index
    )
    group_id = checkpoint["group_id"]
    object_type = candidate["target_object_type"].lower()
    history_observation = context.get(
        "history_observation", {"rgb": context["artifacts"][1]["path"]}
    )
    query_observation = checkpoint.get("query_observation") or {
        "rgb": checkpoint["artifacts"][0]["path"]
    }
    record = {
        "episode_id": episode_id,
        "group_id": group_id,
        "split": candidate["split"],
        "scene": candidate["scene"],
        "seed": seed,
        "history_actions": context["artifacts"][0]["path"],
        "history_frames": [history_observation["rgb"]],
        "query_frame": query_observation["rgb"],
        "query_pose": context["query_pose"],
        "elapsed_steps": 0 if branch == "fresh_stable" else 30,
        # 只公开历史状态下计算的组内固定成本，避免分支状态泄漏。
        "verification_cost": context["stable_verification"]["cost"],
        "public_context": {
            "intervention_window": branch != "fresh_stable",
            "scope": "room",
        },
        "question": QUESTION_TEMPLATES[question_index].format(
            object_type=object_type
        ),
        "program": {
            "op": "GetState",
            "subject": candidate["target_object_id"],
            "attribute": "isOpen",
            "answer_type": "boolean",
        },
    }
    if questions_per_branch > 1:
        record["question_index"] = question_index
        record["history_observation"] = history_observation
        record["query_observation"] = query_observation
    return record


def _private_record(
    candidate, branch, checkpoint, seed, question_index, questions_per_branch
):
    record = {
        "episode_id": _episode_id(
            candidate["candidate_id"], branch, seed, question_index
        ),
        "group_id": checkpoint["group_id"],
        "branch": branch,
        "source_candidate_id": candidate["candidate_id"],
        "source_json": candidate["source_json"],
        "source_action_index": candidate["action_index"],
        "hidden_intervention": (
            {
                "action": candidate["action"],
                "target": candidate["target_object_id"],
            }
            if branch == "risk_stale"
            else None
        ),
        "historical_answer": checkpoint["historical_answer"],
        "current_answer": checkpoint["current_answer"],
        "memory_is_stale": checkpoint["memory_is_stale"],
        "target_visible_from_query": checkpoint["target_visible_from_query"],
        "shortest_verification_cost": checkpoint["verification"]["cost"],
        "verification_pose": checkpoint["verification"]["pose"],
        "state_hash": checkpoint["state_hash"],
    }
    if questions_per_branch > 1:
        record["question_index"] = question_index
        record["target_visible_pixel_count"] = checkpoint["verification"].get(
            "target_visible_pixel_count"
        )
        record["frame_pixel_count"] = checkpoint["verification"].get(
            "frame_pixel_count"
        )
        record["target_visible_fraction"] = checkpoint["verification"].get(
            "target_visible_fraction"
        )
        record["verification_observation"] = checkpoint.get(
            "verification_observation"
        )
    return record


def _replay_record(checkpoint, candidate, branch, seed, question_index):
    if question_index == 0:
        return checkpoint
    value = dict(checkpoint)
    value["source_checkpoint_sha256"] = value.pop("checkpoint_sha256")
    value["branch_episode_id"] = value["episode_id"]
    value["episode_id"] = _episode_id(
        candidate["candidate_id"], branch, seed, question_index
    )
    value["question_index"] = question_index
    return value


def _aggregate(
    output,
    selected,
    contexts,
    checkpoints,
    branches,
    replay_runs,
    seed,
    questions_per_branch,
    modalities,
    excluded_group_ids=(),
):
    public = []
    private = []
    replay_records = []
    complete_groups = 0
    excluded_group_ids = set(excluded_group_ids)
    for candidate in selected:
        candidate_id = candidate["candidate_id"]
        context = contexts.get(candidate_id)
        if context is None:
            continue
        if _group_id(candidate_id) in excluded_group_ids:
            continue
        branch_rounds = {}
        for branch in branches:
            rounds = [
                checkpoints.get((candidate_id, branch, replay_round))
                for replay_round in range(replay_runs)
            ]
            if all(item is not None for item in rounds):
                branch_rounds[branch] = rounds
        if set(branch_rounds) != set(branches):
            continue
        complete_groups += 1
        for branch in branches:
            rounds = branch_rounds[branch]
            for question_index in range(questions_per_branch):
                public.append(
                    _public_record(
                        candidate,
                        context,
                        branch,
                        rounds[0],
                        output,
                        seed,
                        question_index,
                        questions_per_branch,
                    )
                )
                private.append(
                    _private_record(
                        candidate,
                        branch,
                        rounds[0],
                        seed,
                        question_index,
                        questions_per_branch,
                    )
                )
                replay_records.extend(
                    _replay_record(
                        checkpoint,
                        candidate,
                        branch,
                        seed,
                        question_index,
                    )
                    for checkpoint in rounds
                )

    public.sort(key=lambda item: item["episode_id"])
    private.sort(key=lambda item: item["episode_id"])
    replay_records.sort(
        key=lambda item: (item["episode_id"], item["replay_round"])
    )
    _atomic_jsonl(output / "episodes_public.jsonl", public)
    _atomic_jsonl(output / "oracle_private.jsonl", private)
    _atomic_jsonl(output / "replay_records.jsonl", replay_records)
    manifest = {
        "build_version": BUILD_VERSION,
        "seed": seed,
        "selected_source_events": len(selected),
        "complete_source_events": complete_groups,
        "public_episode_count": len(public),
        "private_episode_count": len(private),
        "replay_record_count": len(replay_records),
        "branches": list(branches),
        "replay_runs": replay_runs,
        "excluded_source_event_count": len(excluded_group_ids),
        "excluded_group_ids": sorted(excluded_group_ids),
        "files": {
            name: _sha256_file(output / name)
            for name in (
                "episodes_public.jsonl",
                "oracle_private.jsonl",
                "replay_records.jsonl",
            )
        },
    }
    if questions_per_branch > 1:
        manifest["questions_per_branch"] = questions_per_branch
        manifest["cache_modalities"] = list(modalities)
    _atomic_json(output / "manifest.json", manifest)
    return manifest


def build(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.questions_per_branch < 1 or args.questions_per_branch > len(
        QUESTION_TEMPLATES
    ):
        raise ValueError(
            "questions-per-branch must be between 1 and {}".format(
                len(QUESTION_TEMPLATES)
            )
        )
    cache_mode = args.cache_modalities
    if cache_mode == "auto":
        cache_mode = "full" if args.questions_per_branch > 1 else "rgb"
    modalities = (
        ("rgb", "depth", "instance") if cache_mode == "full" else ("rgb",)
    )
    excluded_group_ids = set()
    if args.exclude_groups is not None:
        exclusions = json.loads(args.exclude_groups.read_text(encoding="utf-8"))
        excluded_group_ids = set(exclusions.get("group_ids", []))
    selection_path = output / "selection.json"
    candidates = read_jsonl(args.candidates)
    selected = select_candidates(candidates, args.limit, args.seed)
    summary = selection_summary(selected, args.seed)
    selected_group_ids = {_group_id(item["candidate_id"]) for item in selected}
    unknown_exclusions = sorted(excluded_group_ids - selected_group_ids)
    if unknown_exclusions:
        raise ValueError(
            "excluded group_id is not selected: {}".format(
                ", ".join(unknown_exclusions)
            )
        )
    if selection_path.exists():
        existing = json.loads(selection_path.read_text(encoding="utf-8"))
        if existing.get("summary", {}).get("all_candidate_ids") != summary[
            "all_candidate_ids"
        ]:
            raise RuntimeError("existing selection checkpoint does not match inputs")
    else:
        write_selection(selected, summary, selection_path)

    branches = tuple(args.branches)
    if any(branch not in BRANCHES for branch in branches):
        raise ValueError("unsupported branch requested")
    if args.questions_per_branch > 1:
        build_config = {
            "build_version": BUILD_VERSION,
            "candidates_sha256": _sha256_file(args.candidates),
            "selected_candidate_ids": summary["all_candidate_ids"],
            "branches": list(branches),
            "seed": args.seed,
            "replay_runs": args.replay_runs,
            "questions_per_branch": args.questions_per_branch,
            "cache_modalities": list(modalities),
        }
        build_config_path = output / "build_config.json"
        if build_config_path.exists():
            existing_config = json.loads(
                build_config_path.read_text(encoding="utf-8")
            )
            if existing_config != build_config:
                raise RuntimeError("existing build config does not match inputs")
        else:
            _atomic_json(build_config_path, build_config)
    contexts = {}
    checkpoints = {}
    pending = []
    trajectory_cache = {}
    for candidate in selected:
        trajectory, trajectory_sha = _load_trajectory(args.alfred_json, candidate)
        trajectory_cache[candidate["candidate_id"]] = (trajectory, trajectory_sha)
        context_path = output / "checkpoints" / candidate["candidate_id"] / "context.json"
        context = _load_checkpoint(
            context_path,
            {
                "kind": "context",
                "build_version": BUILD_VERSION,
                "candidate_id": candidate["candidate_id"],
                "trajectory_sha256": trajectory_sha,
                "seed": args.seed,
            },
        )
        if context is not None:
            contexts[candidate["candidate_id"]] = context
        else:
            pending.append(("context", candidate, None, None))
        for branch in branches:
            for replay_round in range(args.replay_runs):
                checkpoint_path = (
                    output
                    / "checkpoints"
                    / candidate["candidate_id"]
                    / "{}.round-{}.json".format(branch, replay_round)
                )
                checkpoint = _load_checkpoint(
                    checkpoint_path,
                    {
                        "kind": "branch_round",
                        "build_version": BUILD_VERSION,
                        "candidate_id": candidate["candidate_id"],
                        "trajectory_sha256": trajectory_sha,
                        "branch": branch,
                        "replay_round": replay_round,
                        "seed": args.seed,
                    },
                )
                if checkpoint is not None:
                    checkpoints[(candidate["candidate_id"], branch, replay_round)] = checkpoint
                else:
                    pending.append(("branch", candidate, branch, replay_round))

    if pending:
        from ai2thor.controller import Controller

        controller = Controller(quality="Low")
        controller.start(player_screen_width=300, player_screen_height=300)
        completed_this_run = 0
        failed_contexts = set()
        try:
            for kind, candidate, branch, replay_round in pending:
                if args.max_units is not None and completed_this_run >= args.max_units:
                    break
                candidate_id = candidate["candidate_id"]
                if kind == "branch" and candidate_id in failed_contexts:
                    continue
                trajectory, trajectory_sha = trajectory_cache[candidate_id]
                unit_id = (
                    candidate_id + ".context"
                    if kind == "context"
                    else "{}.{}.round-{}".format(candidate_id, branch, replay_round)
                )
                try:
                    if candidate_id not in contexts:
                        contexts[candidate_id] = _build_context(
                            controller,
                            candidate,
                            trajectory,
                            trajectory_sha,
                            output,
                            args.seed,
                            modalities,
                        )
                        completed_this_run += 1
                        if kind == "context":
                            continue
                    checkpoint = _build_branch_round(
                        controller,
                        candidate,
                        trajectory,
                        trajectory_sha,
                        contexts[candidate_id],
                        branch,
                        replay_round,
                        output,
                        args.seed,
                        modalities,
                    )
                    checkpoints[(candidate_id, branch, replay_round)] = checkpoint
                    completed_this_run += 1
                except Exception as exc:
                    print("[gate2] FAILED {}: {}".format(unit_id, exc), flush=True)
                    _record_failure(
                        output,
                        unit_id,
                        {
                            "unit_id": unit_id,
                            "candidate_id": candidate_id,
                            "branch": branch,
                            "replay_round": replay_round,
                            "error": type(exc).__name__ + ": " + str(exc),
                        },
                    )
                    if candidate_id not in contexts:
                        failed_contexts.add(candidate_id)
                    completed_this_run += 1
        finally:
            controller.stop()

    manifest = _aggregate(
        output,
        selected,
        contexts,
        checkpoints,
        branches,
        args.replay_runs,
        args.seed,
        args.questions_per_branch,
        modalities,
        excluded_group_ids,
    )
    print("[gate2] manifest " + json.dumps(manifest, sort_keys=True), flush=True)
    return manifest


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument(
        "--alfred-json",
        type=Path,
        default=Path("external/alfred/data/json_2.1.0"),
    )
    parser.add_argument(
        "--limit", "--num-source-events", dest="limit", type=int, default=20
    )
    parser.add_argument("--branches", nargs="+", default=list(BRANCHES))
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--replay-runs", type=int, default=2)
    parser.add_argument("--questions-per-branch", type=int, default=1)
    parser.add_argument(
        "--cache-modalities",
        choices=("auto", "rgb", "full"),
        default="auto",
    )
    parser.add_argument("--max-units", type=int)
    parser.add_argument("--exclude-groups", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    print(
        "[gate2] start={} host={} pid={} cwd={}".format(
            datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            platform.node(),
            os.getpid(),
            os.getcwd(),
        ),
        flush=True,
    )
    build(args)


if __name__ == "__main__":
    main()
