import copy

import pytest

from trust3d.data.select_events import select_candidates, selection_summary
from trust3d.sim.replay_prefix import ReplayError, replay_prefix
from trust3d.sim.restore_scene import restore_scene
from trust3d.sim.state_hash import canonical_pose, state_hash
from trust3d.sim.visibility_oracle import shortest_grid_distances, teleport_to_pose


class Event:
    def __init__(self, metadata):
        self.metadata = metadata


class FakeController:
    def __init__(self, fail_action=None):
        self.actions = []
        self.fail_action = fail_action
        self.last_event = Event(
            {
                "lastActionSuccess": True,
                "agent": {
                    "position": {"x": 1.0, "y": 0.9, "z": 2.0},
                    "rotation": {"x": 0.0, "y": 90.0, "z": 0.0},
                    "cameraHorizon": 30.0,
                },
                "objects": [],
            }
        )

    def reset(self, scene):
        self.actions.append({"action": "Reset", "scene": scene})

    def step(self, action):
        self.actions.append(action)
        metadata = copy.deepcopy(self.last_event.metadata)
        metadata["lastActionSuccess"] = action.get("action") != self.fail_action
        metadata["errorMessage"] = "fixture failure"
        if action.get("action") == "TeleportFull":
            metadata["agent"]["cameraHorizon"] = action["horizon"]
        self.last_event = Event(metadata)
        return self.last_event


def _trajectory():
    return {
        "scene": {
            "floor_plan": "FloorPlan1",
            "object_poses": [{"objectName": "Mug_asset"}],
            "object_toggles": [{"objectType": "DeskLamp", "isOn": False}],
            "dirty_and_empty": False,
            "init_action": {
                "action": "TeleportFull",
                "x": 0,
                "y": 0.9,
                "z": 0,
                "rotation": 0,
                "horizon": 30,
            },
        },
        "plan": {
            "low_actions": [
                {"api_action": {"action": "LookDown", "forceAction": True}},
                {"api_action": {"action": "MoveAhead", "forceAction": True}},
                {
                    "api_action": {
                        "action": "OpenObject",
                        "objectId": "Cabinet|1",
                    }
                },
            ]
        },
    }


def test_restore_scene_uses_alfred_restore_order():
    controller = FakeController()

    restore_scene(controller, _trajectory())

    assert [action["action"] for action in controller.actions] == [
        "Reset",
        "Initialize",
        "SetObjectToggles",
        "SetObjectPoses",
        "TeleportFull",
    ]


def test_replay_uses_alfred_fifteen_degree_look_actions():
    controller = FakeController()
    controller.last_event.metadata["agent"]["cameraHorizon"] = 30.0

    _, commands = replay_prefix(controller, _trajectory(), 2)

    assert commands == [
        {"action": "LookDown", "forceAction": True},
        {"action": "MoveAhead", "forceAction": True},
    ]
    assert controller.actions[0]["action"] == "TeleportFull"
    assert controller.actions[0]["horizon"] == 45.0


def test_replay_failure_includes_source_index():
    controller = FakeController(fail_action="MoveAhead")

    with pytest.raises(ReplayError, match="index 1"):
        replay_prefix(controller, _trajectory(), 2)


def _candidate(index, split, scene, action, object_type):
    return {
        "candidate_id": "id-{}".format(index),
        "split": split,
        "scene": scene,
        "trajectory_id": "trajectory-{}".format(index),
        "action": action,
        "target_object_type": object_type,
        "prefix_length": index,
        "same_type_object_count": 1,
        "same_type_count_is_lower_bound": False,
        "mvp_whitelist": True,
    }


def test_selection_is_deterministic_and_half_valid_unseen():
    candidates = []
    actions = ["OpenObject", "CloseObject"]
    types = ["Cabinet", "Drawer", "Fridge", "Microwave"]
    for index in range(12):
        candidates.append(
            _candidate(
                index,
                "valid_unseen",
                "FloorPlan{}".format(200 + index % 4),
                actions[index % 2],
                types[index % 4],
            )
        )
    for index in range(12, 30):
        candidates.append(
            _candidate(
                index,
                "train" if index % 2 else "valid_seen",
                "FloorPlan{}".format(index),
                actions[index % 2],
                types[index % 4],
            )
        )

    first = select_candidates(candidates, 20, seed=17)
    second = select_candidates(list(reversed(candidates)), 20, seed=17)
    summary = selection_summary(first, 17)

    assert [item["candidate_id"] for item in first] == [
        item["candidate_id"] for item in second
    ]
    assert summary["split_counts"]["valid_unseen"] == 10
    assert summary["distinct_trajectories"] == 20
    assert summary["distinct_scenes"] >= 14


def test_state_hash_is_stable_for_float_noise_and_object_order():
    metadata = {
        "agent": {
            "position": {"x": 1.0, "y": 0.9, "z": 2.0},
            "rotation": {"x": 0.0, "y": 90.0, "z": 0.0},
            "cameraHorizon": 30.0,
        },
        "objects": [
            {
                "objectId": "Other|1",
                "objectType": "Other",
                "isOpen": False,
            },
            {
                "objectId": "Cabinet|1",
                "objectType": "Cabinet",
                "position": {"x": 1.0000001, "y": 1.0, "z": 2.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                "isOpen": False,
            },
        ],
    }
    changed_order = copy.deepcopy(metadata)
    changed_order["objects"].reverse()
    changed_order["objects"][0]["position"]["x"] = 1.0000002

    assert state_hash(metadata, "Cabinet|1") == state_hash(
        changed_order, "Cabinet|1"
    )
    assert canonical_pose(metadata)["rotation_y"] == 90.0


def test_grid_distances_follow_reachable_adjacency():
    positions = [
        {"x": 0.0, "y": 0.9, "z": 0.0},
        {"x": 0.25, "y": 0.9, "z": 0.0},
        {"x": 0.5, "y": 0.9, "z": 0.0},
        {"x": 0.5, "y": 0.9, "z": 0.25},
    ]

    distances = shortest_grid_distances(positions, (0, 0))

    assert distances[(0, 0)] == 0
    assert distances[(2, 1)] == 3


def test_query_teleport_forces_past_held_object_collision_checks():
    controller = FakeController()
    pose = {"x": 1, "y": 0.9, "z": 2, "rotation_y": 90, "horizon": 30}

    teleport_to_pose(controller, pose)

    assert controller.actions[-1]["action"] == "TeleportFull"
    assert controller.actions[-1]["forceAction"] is True
