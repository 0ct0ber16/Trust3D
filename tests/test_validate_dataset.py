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
                "verification_cost": 3,
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


def test_gate3_two_question_fixture_passes_full_data_checks(tmp_path):
    paths = _fixture(tmp_path)
    public = [json.loads(line) for line in paths[0].read_text().splitlines()]
    private = [json.loads(line) for line in paths[1].read_text().splitlines()]
    replays = [json.loads(line) for line in paths[2].read_text().splitlines()]

    for name in (
        "history_depth.npy",
        "history_instance.png",
        "verification.png",
        "verification_depth.npy",
        "verification_instance.png",
    ):
        (tmp_path / name).write_text("fixture")

    expanded_public = []
    expanded_private = []
    expanded_replays = []
    for question_index in (0, 1):
        for record in public:
            value = dict(record)
            base_id = value["episode_id"]
            value["episode_id"] = "{}_q{}".format(base_id, question_index)
            value["question_index"] = question_index
            value["question"] = (
                "Is the cabinet currently open?"
                if question_index == 0
                else "Is the cabinet open at this moment?"
            )
            value["history_observation"] = {
                "rgb": "history.png",
                "depth": "history_depth.npy",
                "instance": "history_instance.png",
            }
            value["query_observation"] = {
                "rgb": value["query_frame"],
                "depth": "history_depth.npy",
                "instance": "history_instance.png",
            }
            expanded_public.append(value)

        for record in private:
            value = dict(record)
            base_id = value["episode_id"]
            value["episode_id"] = "{}_q{}".format(base_id, question_index)
            value["question_index"] = question_index
            value["target_visible_pixel_count"] = 100
            value["verification_observation"] = {
                "rgb": "verification.png",
                "depth": "verification_depth.npy",
                "instance": "verification_instance.png",
            }
            expanded_private.append(value)

        for record in replays:
            value = dict(record)
            value["episode_id"] = "{}_q{}".format(
                value["episode_id"], question_index
            )
            expanded_replays.append(value)

    _jsonl(paths[0], expanded_public)
    _jsonl(paths[1], expanded_private)
    _jsonl(paths[2], expanded_replays)
    paths[3].write_text(
        json.dumps(
            {
                "selected_source_events": 1,
                "questions_per_branch": 2,
                "files": {},
            }
        )
    )

    report = validate(
        *paths,
        replay_twice=True,
        minimum_source_events=1,
        gate=3,
    )

    assert report["acceptance"]["gate3_pass"] is True
    assert report["public_episode_count"] == 6
    assert report["public_full_observation_count"] == 6
    assert min(report["answer_fractions"].values()) >= 0.30


def test_gate3_rejects_branch_dependent_public_verification_cost(tmp_path):
    paths = _fixture(tmp_path)
    public = [json.loads(line) for line in paths[0].read_text().splitlines()]
    public[0]["verification_cost"] = 4
    _jsonl(paths[0], public)

    report = validate(*paths, minimum_source_events=1, gate=3)

    assert report["acceptance"]["public_verification_cost_group_invariant"] is False
    assert report["acceptance"]["gate3_pass"] is False
