import json

import numpy as np
import pytest

from trust3d.eval.evaluate_cut3r import (
    camera_revisit_errors,
    evaluate,
    movable_object_revisit_errors,
)
from trust3d.geometry.run_cut3r import (
    centered_point,
    point_in_camera,
    spatial_answers,
)


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


def test_centered_point_uses_high_confidence_center_crop():
    points = np.zeros((20, 30, 3), dtype=float)
    confidence = np.zeros((20, 30), dtype=float)
    points[8:12, 13:17] = [2.0, 3.0, 4.0]
    confidence[8:12, 13:17] = 10.0
    value = centered_point(
        points, confidence, crop_fraction=0.2, confidence_quantile=0.5
    )
    assert value["world"] == pytest.approx([2.0, 3.0, 4.0])
    assert value["selected_pixel_count"] > 0


def test_spatial_answers_use_query_camera_coordinates():
    pose = np.eye(4)
    pose[:3, 3] = [10.0, 0.0, 5.0]
    target = point_in_camera([8.0, 0.0, 7.0], pose)
    donor = point_in_camera([14.0, 0.0, 11.0], pose)
    answers = spatial_answers(target, donor)
    assert answers == {
        "left_right": "left",
        "front_behind": "front",
        "which_closer": "target",
        "target_nearer": True,
    }


def test_revisit_diagnostics_are_zero_for_exact_camera_and_object_swap():
    old_target = [0.0, 0.0, 1.0]
    old_donor = [1.0, 0.0, 1.0]
    group = {
        "camera_trajectories": {
            "stable": [_pose(), _pose(1), _pose(0, 0, 1), _pose(), _pose(1)],
            "stale": [_pose(), _pose(1), _pose(0, 0, 1), _pose(1), _pose()],
        },
        "historical": _stage(old_target, old_donor, {}),
        "stable_reobserve": _stage(old_target, old_donor, {}),
        "stale_sequence_historical": _stage(old_target, old_donor, {}),
        "stale_reobserve": _stage(old_donor, old_target, {}),
    }
    assert camera_revisit_errors(group) == pytest.approx([0, 0, 0, 0])
    assert movable_object_revisit_errors(group) == pytest.approx([0, 0, 0, 0])


def test_cut3r_evaluator_routes_private_branch_only_during_evaluation(tmp_path):
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
    for branch in ("fresh_stable", "risk_stable", "risk_stale"):
        for question_type in question_types:
            episode_id = f"{branch}-{question_type}"
            current = new_answers[question_type] if branch == "risk_stale" else old_answers[question_type]
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

    public_path = tmp_path / "public.jsonl"
    private_path = tmp_path / "private.jsonl"
    routes_path = tmp_path / "routes.jsonl"
    _write_jsonl(public_path, public)
    _write_jsonl(private_path, private)
    _write_jsonl(routes_path, routes)

    geometry_root = tmp_path / "geometry"
    group_path = geometry_root / "checkpoints" / "g1.json"
    group_path.parent.mkdir(parents=True)
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
            "stable_sequence_seconds": 2.0,
            "stale_sequence_seconds": 3.0,
        },
        "peak_allocated_bytes": 1024,
    }
    group_path.write_text(json.dumps(group), encoding="utf-8")
    (geometry_root / "manifest.json").write_text(
        json.dumps(
            {
                "complete": True,
                "checkpoint_sha256": "abc",
                "adapter_version": "test",
                "groups": [
                    {
                        "group_id": "g1",
                        "status": "success",
                        "checkpoint": str(group_path),
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

    report = evaluate(
        public_path,
        private_path,
        routes_path,
        geometry_root,
        tmp_path / "source",
        tmp_path / "predictions.jsonl",
        tmp_path / "report.json",
    )
    assert report["gate7_pass"] is True
    assert report["gt_to_cut3r_qa_drop"] == pytest.approx(0)
    assert report["geometry_diagnostics"]["latency"][
        "state_reuse_time_saving"
    ] == pytest.approx(1 - 5 / 28)
