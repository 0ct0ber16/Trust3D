import json

from trust3d.data.validate_dataset import validate


def _jsonl(path, values):
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def _fixture(tmp_path):
    public = []
    private = []
    replays = []
    group_id = "g_fixture"
    query_pose = {"x": 0, "y": 0.9, "z": 0, "rotation_y": 0, "horizon": 30}
    for name in ("actions.json", "history.png"):
        (tmp_path / name).write_text("fixture")
    branches = ["fresh_stable", "risk_stable", "risk_stale"]
    for index, branch in enumerate(branches):
        episode_id = "e_{}".format(index)
        query_frame = "query_{}.png".format(index)
        (tmp_path / query_frame).write_text("fixture")
        public.append(
            {
                "episode_id": episode_id,
                "group_id": group_id,
                "split": "valid_unseen",
                "scene": "FloorPlan1",
                "seed": 17,
                "history_actions": "actions.json",
                "history_frames": ["history.png"],
                "query_frame": query_frame,
                "query_pose": query_pose,
                "elapsed_steps": 0 if branch == "fresh_stable" else 30,
                "public_context": {
                    "intervention_window": branch != "fresh_stable",
                    "scope": "room",
                },
                "question": "Is the cabinet currently open?",
                "program": {
                    "op": "GetState",
                    "subject": "Cabinet|1",
                    "attribute": "isOpen",
                },
            }
        )
        stale = branch == "risk_stale"
        private.append(
            {
                "episode_id": episode_id,
                "group_id": group_id,
                "branch": branch,
                "historical_answer": False,
                "current_answer": stale,
                "memory_is_stale": stale,
                "verification_pose": query_pose,
                "shortest_verification_cost": 3,
                "target_visible_from_query": False,
            }
        )
        for replay_round in range(2):
            replays.append(
                {
                    "episode_id": episode_id,
                    "replay_round": replay_round,
                    "state_hash": "hash-{}".format(index),
                    "current_answer": stale,
                    "query_frame_raw_sha256": "same-risk-frame"
                    if branch != "fresh_stable"
                    else "fresh-frame",
                }
            )

    public_path = tmp_path / "episodes_public.jsonl"
    private_path = tmp_path / "oracle_private.jsonl"
    replay_path = tmp_path / "replay_records.jsonl"
    _jsonl(public_path, public)
    _jsonl(private_path, private)
    _jsonl(replay_path, replays)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"files": {}}))
    return public_path, private_path, replay_path, manifest_path


def test_valid_three_branch_fixture_passes(tmp_path):
    paths = _fixture(tmp_path)

    report = validate(*paths, replay_twice=True, minimum_source_events=1)

    assert report["acceptance"]["gate2_pass"] is True
    assert report["replay_state_hash_match_rate"] == 1.0
    assert report["verification_pose_rate"] == 1.0


def test_private_answer_in_public_record_fails_leak_check(tmp_path):
    paths = _fixture(tmp_path)
    public = [json.loads(line) for line in paths[0].read_text().splitlines()]
    public[0]["current_answer"] = False
    _jsonl(paths[0], public)

    report = validate(*paths, replay_twice=True, minimum_source_events=1)

    assert report["acceptance"]["public_private_separation"] is False
    assert report["acceptance"]["gate2_pass"] is False
