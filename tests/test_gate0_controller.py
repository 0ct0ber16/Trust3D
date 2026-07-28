import copy

import numpy as np

from trust3d.sim.controller import object_state_hash, validate_runs


def _metadata():
    return {
        "agent": {"position": {"x": 0.0, "y": 0.9, "z": 0.0}},
        "objects": [
            {
                "objectId": "Cabinet|1",
                "objectType": "Cabinet",
                "position": {"x": 1.0, "y": 1.0, "z": 2.0},
                "rotation": {"x": 0.0, "y": 90.0, "z": 0.0},
                "isOpen": False,
            },
            {
                "objectId": "Drawer|1",
                "objectType": "Drawer",
                "position": {"x": 2.0, "y": 1.0, "z": 1.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
                "isOpen": True,
            },
        ],
    }


def _run(metadata=None):
    return {
        "rgb": np.full((4, 4, 3), 32, dtype=np.uint8),
        "depth": np.ones((4, 4), dtype=np.float32),
        "instance_segmentation": np.zeros((4, 4, 3), dtype=np.uint8),
        "metadata": _metadata() if metadata is None else metadata,
        "action": {"success": True},
        "state_hash": object_state_hash(_metadata() if metadata is None else metadata),
    }


def test_object_state_hash_is_independent_of_object_order():
    first = _metadata()
    second = copy.deepcopy(first)
    second["objects"].reverse()

    assert object_state_hash(first) == object_state_hash(second)


def test_object_state_hash_changes_with_dynamic_state():
    first = _metadata()
    second = copy.deepcopy(first)
    second["objects"][0]["isOpen"] = True

    assert object_state_hash(first) != object_state_hash(second)


def test_validation_accepts_complete_deterministic_runs():
    first = _run()
    second = _run()

    assert all(validate_runs(first, second).values())


def test_validation_rejects_black_rgb_and_state_drift():
    first = _run()
    second_metadata = copy.deepcopy(_metadata())
    second_metadata["objects"][0]["isOpen"] = True
    second = _run(second_metadata)
    first["rgb"].fill(0)

    checks = validate_runs(first, second)

    assert checks["rgb_non_black"] is False
    assert checks["initial_object_state_deterministic"] is False
