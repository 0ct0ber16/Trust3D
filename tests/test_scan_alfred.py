import json

from trust3d.data.scan_alfred import scan_dataset, write_outputs


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _trajectory(task_id="trial_fixture"):
    return {
        "task_id": task_id,
        "task_type": "fixture_task",
        "scene": {"floor_plan": "FloorPlan1", "scene_num": 1},
        "plan": {
            "low_actions": [
                {
                    "high_idx": 0,
                    "api_action": {
                        "action": "OpenObject",
                        "objectId": "Cabinet|+00.00|+00.00|+00.00",
                    },
                },
                {"high_idx": 0, "api_action": {"action": "MoveAhead"}},
                {
                    "high_idx": 1,
                    "api_action": {
                        "action": "CloseObject",
                        "objectId": "Cabinet|+00.00|+00.00|+00.00",
                    },
                },
                {
                    "high_idx": 2,
                    "api_action": {
                        "action": "OpenObject",
                        "objectId": "Drawer|+01.00|+00.00|+00.00",
                    },
                },
            ]
        },
    }


def _dataset(tmp_path):
    alfred_root = tmp_path / "alfred"
    json_root = alfred_root / "data" / "json_2.1.0"
    source = json_root / "valid_unseen" / "task" / "trial" / "traj_data.json"
    _write_json(source, _trajectory())
    _write_json(
        alfred_root / "gen" / "layouts" / "FloorPlan1-openable.json",
        {
            "Cabinet|+00.00|+00.00|+00.00": [0, 0, 0, 0],
            "Cabinet|+02.00|+00.00|+00.00": [0, 0, 0, 0],
            "Drawer|+01.00|+00.00|+00.00": [0, 0, 0, 0],
        },
    )
    return json_root


def test_scan_extracts_traceable_events_and_scene_counts(tmp_path):
    candidates, stats = scan_dataset(
        _dataset(tmp_path), ["OpenObject", "CloseObject"]
    )

    assert [item["action_index"] for item in candidates] == [0, 2, 3]
    assert candidates[0]["source_action_path"] == "plan.low_actions[0].api_action"
    assert candidates[0]["same_type_object_count"] == 2
    assert candidates[0]["same_type_count_is_lower_bound"] is False
    assert candidates[2]["same_type_object_count"] == 1
    assert candidates[0]["mvp_whitelist"] is True
    assert stats["candidates_by_split"] == {"valid_unseen": 3}
    assert stats["action_validation_error_count"] == 0


def test_scan_derives_state_before_each_open_close_event(tmp_path):
    candidates, _ = scan_dataset(
        _dataset(tmp_path), ["OpenObject", "CloseObject"]
    )

    assert candidates[0]["initial_state"]["is_open"] is False
    assert candidates[1]["initial_state"]["is_open"] is True
    assert candidates[2]["initial_state"]["is_open"] is False
    assert candidates[1]["prefix_length"] == 2


def test_first_close_uses_precondition_and_movable_pose_count(tmp_path):
    json_root = _dataset(tmp_path)
    source = json_root / "valid_unseen" / "task" / "trial" / "traj_data.json"
    trajectory = _trajectory()
    trajectory["scene"]["object_poses"] = [
        {"objectName": "Laptop_asset", "position": {}},
        {"objectName": "Laptop_asset", "position": {}},
    ]
    trajectory["plan"]["low_actions"] = [
        {
            "high_idx": 0,
            "api_action": {
                "action": "CloseObject",
                "objectId": "Laptop|+00.00|+00.00|+00.00",
            },
        }
    ]
    _write_json(source, trajectory)

    candidates, stats = scan_dataset(json_root, ["CloseObject"])

    assert candidates[0]["initial_state"]["is_open"] is True
    assert candidates[0]["same_type_object_count"] == 2
    assert candidates[0]["same_type_count_source"] == "scene.object_poses"
    assert candidates[0]["same_type_count_is_lower_bound"] is False
    assert candidates[0]["mvp_whitelist"] is False
    assert stats["action_validation_error_count"] == 0


def test_action_ids_provide_a_marked_count_lower_bound(tmp_path):
    json_root = _dataset(tmp_path)
    layout = tmp_path / "alfred" / "gen" / "layouts" / "FloorPlan1-openable.json"
    _write_json(layout, {})

    candidates, stats = scan_dataset(json_root, ["OpenObject"])

    assert candidates[0]["same_type_object_count"] == 1
    assert candidates[0]["same_type_count_source"] == "trajectory action IDs"
    assert candidates[0]["same_type_count_is_lower_bound"] is True
    assert stats["action_validation_error_count"] == 0


def test_scan_records_parse_errors_without_losing_valid_trajectories(tmp_path):
    json_root = _dataset(tmp_path)
    bad = json_root / "train" / "bad" / "trial" / "traj_data.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not-json", encoding="utf-8")

    candidates, stats = scan_dataset(json_root, ["OpenObject"])

    assert len(candidates) == 2
    assert stats["trajectory_file_count"] == 2
    assert stats["trajectories_parsed"] == 1
    assert stats["parse_error_count"] == 1
    assert stats["parse_error_rate"] == 0.5


def test_hidden_test_plans_are_skipped_not_parse_errors(tmp_path):
    json_root = _dataset(tmp_path)
    hidden = json_root / "tests_seen" / "trial_hidden" / "traj_data.json"
    _write_json(hidden, {"task_id": "trial_hidden", "scene": {}})

    candidates, stats = scan_dataset(json_root, ["OpenObject"])

    assert len(candidates) == 2
    assert stats["trajectory_file_count"] == 2
    assert stats["processable_trajectory_count"] == 1
    assert stats["trajectories_parsed"] == 1
    assert stats["skipped_unannotated_count"] == 1
    assert stats["skipped_unannotated_by_split"] == {"tests_seen": 1}
    assert stats["parse_error_count"] == 0


def test_outputs_are_jsonl_and_deterministic_json(tmp_path):
    candidates, stats = scan_dataset(_dataset(tmp_path), ["OpenObject"])
    output = tmp_path / "outputs" / "candidates.jsonl"
    stats_output = tmp_path / "outputs" / "stats.json"

    write_outputs(candidates, stats, output, stats_output)

    records = [json.loads(line) for line in output.read_text().splitlines()]
    saved_stats = json.loads(stats_output.read_text())
    assert records == candidates
    assert saved_stats["dataset_manifest"]["sha256"] == stats["dataset_manifest"][
        "sha256"
    ]
