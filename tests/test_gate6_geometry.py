import math
import json

import numpy as np
import pytest

from trust3d.geometry.egocentric import (
    diagnostic_query_yaw,
    object_center,
    rgbd_mask_centroid,
    spatial_labels,
    world_to_egocentric,
)
from trust3d.data.build_spatial import _pixel_difference, _query_candidate
from trust3d.eval.evaluate_spatial import evaluate
from trust3d.sim.spatial_intervention import choose_scene_pair, swap_objects


def test_object_center_prefers_axis_aligned_box():
    obj = {
        "position": {"x": 9, "y": 9, "z": 9},
        "axisAlignedBoundingBox": {"center": {"x": 1, "y": 2, "z": 3}},
    }
    assert object_center(obj) == {"x": 1.0, "y": 2.0, "z": 3.0}


@pytest.mark.parametrize(
    "yaw,point,expected",
    [
        (0, {"x": 1, "y": 0, "z": 2}, (1, 2)),
        (90, {"x": 2, "y": 0, "z": -1}, (1, 2)),
        (180, {"x": -1, "y": 0, "z": -2}, (1, 2)),
        (270, {"x": -2, "y": 0, "z": 1}, (1, 2)),
    ],
)
def test_world_to_egocentric_matches_thor_yaw(yaw, point, expected):
    pose = {"x": 0, "y": 0, "z": 0, "rotation_y": yaw}
    ego = world_to_egocentric(point, pose)
    assert ego["right"] == pytest.approx(expected[0])
    assert ego["forward"] == pytest.approx(expected[1])


def test_diagnostic_yaw_places_changed_positions_on_opposite_sides():
    origin = {"x": 0, "y": 0, "z": 0}
    old = {"x": -1, "y": 0, "z": 3}
    new = {"x": 1, "y": 0, "z": 3}
    yaw = diagnostic_query_yaw(origin, old, new)
    pose = dict(origin, rotation_y=yaw)
    old_labels = spatial_labels(old, pose)
    new_labels = spatial_labels(new, pose)
    assert old_labels["left_right"] != new_labels["left_right"]
    assert old_labels["front_behind"] == "behind"
    assert new_labels["front_behind"] == "behind"


def test_rgbd_center_pixel_respects_yaw_and_depth_scale():
    depth = np.array([[2000.0]])
    mask = np.array([[True]])
    point = rgbd_mask_centroid(
        depth,
        mask,
        {"x": 1, "y": 2, "z": 3},
        rotation_y=90,
        horizon=0,
        depth_scale=0.001,
    )
    assert point["x"] == pytest.approx(3.0)
    assert point["y"] == pytest.approx(2.0)
    assert point["z"] == pytest.approx(3.0)
    assert math.isfinite(point["x"])


def test_query_candidate_separates_changed_positions():
    value = _query_candidate(
        {"x": 0, "y": 0, "z": 0},
        {"x": -1, "y": 0, "z": 3},
        {"x": 2, "y": 0, "z": 4},
    )
    assert value is not None
    _, pose = value
    assert spatial_labels({"x": -1, "y": 0, "z": 3}, pose)[
        "left_right"
    ] != spatial_labels({"x": 2, "y": 0, "z": 4}, pose)["left_right"]


def test_pixel_difference_is_normalized_and_counts_changed_pixels():
    first = np.zeros((2, 2, 3), dtype=np.uint8)
    second = first.copy()
    second[0, 0] = 255
    report = _pixel_difference(first, second, scale=255.0)
    assert report["exact_match"] is False
    assert report["changed_pixel_fraction"] == pytest.approx(0.25)
    assert report["mean_absolute_difference"] == pytest.approx(0.25)


class _FakeEvent:
    def __init__(self, objects):
        self.metadata = {"objects": objects, "lastActionSuccess": True}


class _FakeController:
    def __init__(self):
        self.last_event = _FakeEvent(
            [
                {
                    "objectId": "a",
                    "name": "A",
                    "pickupable": True,
                    "position": {"x": 0, "y": 0, "z": 0},
                    "rotation": {"x": 0, "y": 0, "z": 0},
                },
                {
                    "objectId": "b",
                    "name": "B",
                    "pickupable": True,
                    "position": {"x": 1, "y": 0, "z": 0},
                    "rotation": {"x": 0, "y": 0, "z": 0},
                },
            ]
        )

    def step(self, action):
        positions = {
            item["objectName"]: item["position"] for item in action["objectPoses"]
        }
        for obj in self.last_event.metadata["objects"]:
            obj["position"] = dict(positions[obj["name"]])
        return self.last_event


def test_swap_objects_verifies_both_positions():
    controller = _FakeController()
    _, report = swap_objects(controller, "a", "b")
    assert report["target_position_error_m"] == 0
    assert controller.last_event.metadata["objects"][0]["position"]["x"] == 1


def test_scene_pair_does_not_require_same_frame_visibility():
    event = _FakeEvent(
        [
            {
                "objectId": "a",
                "name": "A",
                "objectType": "Apple",
                "pickupable": True,
                "visible": False,
                "isPickedUp": False,
                "position": {"x": 0, "y": 0, "z": 0},
            },
            {
                "objectId": "b",
                "name": "B",
                "objectType": "Apple",
                "pickupable": True,
                "visible": False,
                "isPickedUp": False,
                "position": {"x": 1, "y": 0, "z": 0},
            },
        ]
    )
    target, donor = choose_scene_pair(event)
    assert {target["objectId"], donor["objectId"]} == {"a", "b"}


def _write_jsonl(path, values):
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in values),
        encoding="utf-8",
    )


def test_spatial_evaluator_applies_freshness_and_cost_criteria(tmp_path):
    public = []
    private = []
    routes = []
    index = 0
    for branch in ("fresh_stable", "risk_stable", "risk_stale"):
        for question_type in (
            "left_right",
            "front_behind",
            "which_closer",
            "target_nearer",
        ):
            index += 1
            episode_id = "e{}".format(index)
            stale = branch == "risk_stale"
            historical = "old" if stale else "answer"
            public.append({"episode_id": episode_id})
            private.append(
                {
                    "episode_id": episode_id,
                    "group_id": "g1",
                    "branch": branch,
                    "question_type": question_type,
                    "historical_answer_gt": historical,
                    "current_answer_gt": "answer",
                    "historical_answer_rgbd": historical,
                    "current_answer_rgbd": "answer",
                    "deterministic_tool_answer": "answer",
                    "simulator_oracle_answer": "answer",
                    "memory_is_stale": stale,
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
    report = evaluate(
        public_path,
        private_path,
        routes_path,
        tmp_path / "predictions.jsonl",
        tmp_path / "report.json",
    )
    assert report["gate6_pass"] is True
    assert report["observation_saving_vs_always_reobserve"] == pytest.approx(
        1 / 3
    )
