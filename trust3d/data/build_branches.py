"""Build restartable Fresh/Risk Stable/Stale Gate 2 episode branches."""

import argparse
import hashlib
import json
import os
import platform
import traceback
from datetime import datetime
from pathlib import Path

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


def _episode_id(candidate_id, branch, seed):
    raw = "{}|{}|{}".format(candidate_id, branch, seed).encode("ascii")
    return "e_" + hashlib.sha256(raw).hexdigest()[:24]


def _group_id(candidate_id):
    return "g_" + candidate_id[:24]


def _relative(path, root):
    return Path(path).relative_to(root).as_posix()


def _build_context(controller, candidate, trajectory, trajectory_sha, output, seed):
    context_path = output / "checkpoints" / candidate["candidate_id"] / "context.json"
    expected = {
        "kind": "context",
        "build_version": BUILD_VERSION,
        "candidate_id": candidate["candidate_id"],
        "trajectory_sha256": trajectory_sha,
        "seed": seed,
    }
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
    frame_path = output / "cache" / "rgb" / (group_id + "_history.png")
    _atomic_json(actions_path, prefix_commands)
    frame_sha = _atomic_png(frame_path, history_frame)
    value = dict(expected)
    value.update(
        {
            "status": "success",
            "history_answer": history_answer,
            "history_pose": history_pose,
            "history_state_hash": history_state_hash,
            "query_pose": query_pose,
            "stable_verification": stable_oracle,
            "artifacts": [
                {
                    "path": _relative(actions_path, output),
                    "sha256": _sha256_file(actions_path),
                },
                {"path": _relative(frame_path, output), "sha256": frame_sha},
            ],
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

    event = teleport_to_pose(controller, context["query_pose"])
    final_hash = state_hash(event.metadata, target_id)
    artifacts = []
    if replay_round == 0:
        query_path = output / "cache" / "rgb" / (episode_id + "_query.png")
        png_sha = _atomic_png(query_path, query_frame)
        artifacts.append({"path": _relative(query_path, output), "sha256": png_sha})

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
            "target_visible_from_query": target_visible_from_query,
            "verification": oracle,
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


def _public_record(candidate, context, branch, checkpoint, output, seed):
    episode_id = checkpoint["episode_id"]
    group_id = checkpoint["group_id"]
    object_type = candidate["target_object_type"].lower()
    return {
        "episode_id": episode_id,
        "group_id": group_id,
        "split": candidate["split"],
        "scene": candidate["scene"],
        "seed": seed,
        "history_actions": context["artifacts"][0]["path"],
        "history_frames": [context["artifacts"][1]["path"]],
        "query_frame": "cache/rgb/{}_query.png".format(episode_id),
        "query_pose": context["query_pose"],
        "elapsed_steps": 0 if branch == "fresh_stable" else 30,
        "public_context": {
            "intervention_window": branch != "fresh_stable",
            "scope": "room",
        },
        "question": "Is the {} currently open?".format(object_type),
        "program": {
            "op": "GetState",
            "subject": candidate["target_object_id"],
            "attribute": "isOpen",
            "answer_type": "boolean",
        },
    }


def _private_record(candidate, branch, checkpoint):
    return {
        "episode_id": checkpoint["episode_id"],
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


def _aggregate(output, selected, contexts, checkpoints, branches, replay_runs, seed):
    public = []
    private = []
    replay_records = []
    complete_groups = 0
    for candidate in selected:
        candidate_id = candidate["candidate_id"]
        context = contexts.get(candidate_id)
        if context is None:
            continue
        group_complete = True
        for branch in branches:
            rounds = [
                checkpoints.get((candidate_id, branch, replay_round))
                for replay_round in range(replay_runs)
            ]
            if any(item is None for item in rounds):
                group_complete = False
                continue
            public.append(
                _public_record(candidate, context, branch, rounds[0], output, seed)
            )
            private.append(_private_record(candidate, branch, rounds[0]))
            replay_records.extend(rounds)
        if group_complete:
            complete_groups += 1

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
        "files": {
            name: _sha256_file(output / name)
            for name in (
                "episodes_public.jsonl",
                "oracle_private.jsonl",
                "replay_records.jsonl",
            )
        },
    }
    _atomic_json(output / "manifest.json", manifest)
    return manifest


def build(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    selection_path = output / "selection.json"
    candidates = read_jsonl(args.candidates)
    selected = select_candidates(candidates, args.limit, args.seed)
    summary = selection_summary(selected, args.seed)
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
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--branches", nargs="+", default=list(BRANCHES))
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--replay-runs", type=int, default=2)
    parser.add_argument("--max-units", type=int)
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
