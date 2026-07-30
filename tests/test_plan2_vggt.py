import json

import numpy as np

from trust3d.eval.diagnose_cut3r import (
    _pose_camera_to_world,
    _relative_pose_diagnostics,
)
from trust3d.eval.evaluate_cut3r import evaluate_vggt
from trust3d.geometry.run_vggt import _fingerprint, _load_config


def _write_jsonl(path, values):
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )


def _pose(x=0.0, y=0.0, z=0.0):
    value = np.eye(4)
    value[:3, 3] = [x, y, z]
    return value.tolist()


def _stage(target, donor, answers):
    return {
        "target": {"world": target},
        "donor": {"world": donor},
        "answers": answers,
    }


def test_plan2_config_is_fully_locked():
    config = _load_config("configs/plan2_vggt.json")
    assert config["model_sha256"] == (
        "f164acf60724910d8fe1578bb499d800850c7bb0948db7555c413f9fbe60467e"
    )
    assert config["center_crop_fraction"] == 0.12
    assert config["diagnostic_crop_fractions"] == [0.08, 0.18]


def test_vggt_fingerprint_changes_with_an_input_hash():
    config = _load_config("configs/plan2_vggt.json")
    inputs = [{"path": "a.png", "sha256": "a", "scenario": "stable", "index": 0}]
    first = _fingerprint("g1", inputs, config, "config", "public", "routes")
    inputs[0]["sha256"] = "b"
    second = _fingerprint("g1", inputs, config, "config", "public", "routes")
    assert first != second


def test_ai2thor_camera_pose_and_relative_diagnostics_are_consistent():
    identity = _pose_camera_to_world(
        {"x": 1, "y": 2, "z": 3, "rotation_y": 0, "horizon": 0}
    )
    np.testing.assert_allclose(identity[:3, :3], np.eye(3))
    assert identity[:3, 3].tolist() == [1, 2, 3]
    poses = [np.asarray(_pose(x=index), dtype=float) for index in range(5)]
    diagnostics = _relative_pose_diagnostics(poses, poses)
    assert all(item["rotation_error_degrees"] == 0 for item in diagnostics)
    assert all(
        item["translation_direction_error_degrees"] in (0, None)
        for item in diagnostics
    )


def test_vggt_evaluator_reuses_gate7_routes_and_answers(tmp_path):
    question_types = (
        "left_right",
        "front_behind",
        "which_closer",
        "target_nearer",
    )
    old_answers = {
        "left_right": "left",
        "front_behind": "behind",
        "which_closer": "reference",
        "target_nearer": False,
    }
    new_answers = {
        "left_right": "right",
        "front_behind": "front",
        "which_closer": "target",
        "target_nearer": True,
    }
    public = []
    private = []
    routes = []
    reference = []
    for branch in ("fresh_stable", "risk_stable", "risk_stale"):
        for question_type in question_types:
            episode_id = f"{branch}-{question_type}"
            current = (
                new_answers[question_type]
                if branch == "risk_stale"
                else old_answers[question_type]
            )
            public.append({"episode_id": episode_id, "group_id": "g1"})
            private.append(
                {
                    "episode_id": episode_id,
                    "group_id": "g1",
                    "branch": branch,
                    "question_type": question_type,
                    "historical_answer_gt": old_answers[question_type],
                    "current_answer_gt": current,
                    "historical_answer_rgbd": old_answers[question_type],
                    "current_answer_rgbd": current,
                    "memory_is_stale": branch == "risk_stale",
                    "shortest_verification_cost": 4,
                    "required_new_observations": 1,
                }
            )
            routes.append(
                {
                    "episode_id": episode_id,
                    "policy_id": "trust3d_lambda_0.01",
                    "route": "trust_memory"
                    if branch == "fresh_stable"
                    else "reobserve",
                }
            )
            reference.append(
                {
                    "episode_id": episode_id,
                    "group_id": "g1",
                    "method": "trust3d_cut3r",
                    "correct": False,
                }
            )

    public_path = tmp_path / "public.jsonl"
    private_path = tmp_path / "private.jsonl"
    routes_path = tmp_path / "routes.jsonl"
    reference_path = tmp_path / "reference.jsonl"
    _write_jsonl(public_path, public)
    _write_jsonl(private_path, private)
    _write_jsonl(routes_path, routes)
    _write_jsonl(reference_path, reference)

    geometry_root = tmp_path / "geometry"
    checkpoint = geometry_root / "checkpoints" / "g1.json"
    checkpoint.parent.mkdir(parents=True)
    group = {
        "group_id": "g1",
        "status": "success",
        "historical": _stage([0, 0, 1], [1, 0, 1], old_answers),
        "stable_reobserve": _stage([0, 0, 1], [1, 0, 1], old_answers),
        "stale_sequence_historical": _stage([0, 0, 1], [1, 0, 1], old_answers),
        "stale_reobserve": _stage([1, 0, 1], [0, 0, 1], new_answers),
        "camera_trajectories": {
            "stable": [_pose(), _pose(1), _pose(0, 0, 1), _pose(), _pose(1)],
            "stale": [_pose(), _pose(1), _pose(0, 0, 1), _pose(1), _pose()],
        },
        "timing": {
            "stable_sequence_seconds": 1.0,
            "stale_sequence_seconds": 1.0,
        },
        "peak_allocated_bytes": 1024,
    }
    checkpoint.write_text(json.dumps(group), encoding="utf-8")
    (geometry_root / "manifest.json").write_text(
        json.dumps(
            {
                "complete": True,
                "checkpoint_sha256": "abc",
                "adapter_version": "test-vggt",
                "private_file_open_count": 0,
                "groups": [
                    {
                        "group_id": "g1",
                        "status": "success",
                        "checkpoint": str(checkpoint),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source" / "g1"
    source.mkdir(parents=True)
    (source / "context.json").write_text(
        json.dumps(
            {
                "status": "success",
                "group_id": "g1",
                "target_object_type": "Apple",
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_vggt(
        public_path,
        private_path,
        routes_path,
        geometry_root,
        tmp_path / "source",
        tmp_path / "predictions.jsonl",
        tmp_path / "validation.json",
        reference_path,
        bootstrap_samples=100,
    )
    assert report["gate7_vggt_pass"] is True
    assert report["vggt_accuracy"] == 1.0
    assert report["gt_to_vggt_qa_drop"] == 0.0
    assert report["paired_cut3r_comparison"]["accuracy_difference"] == 1.0
