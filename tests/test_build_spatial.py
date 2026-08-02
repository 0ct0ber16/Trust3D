from types import SimpleNamespace

import pytest

from trust3d.sim.spatial_intervention import swap_objects
from trust3d.sim.state_hash import find_object_by_name


def test_find_object_by_name_rebinds_after_object_id_changes():
    metadata = {
        "objects": [
            {"name": "Fork_1", "objectId": "Fork|new-position"},
            {"name": "Fork_2", "objectId": "Fork|other-position"},
        ]
    }

    rebound = find_object_by_name(metadata, "Fork_1")

    assert rebound["objectId"] == "Fork|new-position"


def test_find_object_by_name_rejects_missing_or_ambiguous_names():
    with pytest.raises(KeyError, match="matches=0"):
        find_object_by_name({"objects": []}, "Fork_1")

    duplicate = {
        "objects": [
            {"name": "Fork_1", "objectId": "Fork|first"},
            {"name": "Fork_1", "objectId": "Fork|second"},
        ]
    }
    with pytest.raises(KeyError, match="matches=2"):
        find_object_by_name(duplicate, "Fork_1")


class _ReencodingController:
    def __init__(self):
        self.objects = [
            {
                "name": "Fork_1",
                "objectId": "old-target",
                "pickupable": True,
                "position": {"x": 0.0, "y": 0.9, "z": 0.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
            {
                "name": "Fork_2",
                "objectId": "old-donor",
                "pickupable": True,
                "position": {"x": 2.0, "y": 0.9, "z": 0.0},
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        ]
        self.last_event = self._event(reencode=False)

    def _event(self, reencode=True):
        if reencode:
            for obj in self.objects:
                position = obj["position"]
                obj["objectId"] = "{}|{:.2f}|{:.2f}|{:.2f}".format(
                    obj["name"], position["x"], position["y"], position["z"]
                )
        self.last_event = SimpleNamespace(
            metadata={"lastActionSuccess": True, "objects": self.objects}
        )
        return self.last_event

    def step(self, action):
        if action["action"] == "TeleportObject":
            selected = next(
                obj for obj in self.objects if obj["objectId"] == action["objectId"]
            )
            selected["position"] = {
                axis: float(action[axis]) for axis in ("x", "y", "z")
            }
        elif action["action"] != "SetObjectPoses":
            raise AssertionError(action)
        return self._event()


def test_swap_objects_rebinds_ids_after_each_position_change():
    controller = _ReencodingController()

    event, audit = swap_objects(controller, "old-target", "old-donor")

    by_name = {obj["name"]: obj for obj in event.metadata["objects"]}
    assert by_name["Fork_1"]["position"]["x"] == 2.0
    assert by_name["Fork_2"]["position"]["x"] == 0.0
    assert audit["method"] == "TeleportObject"
    assert audit["target_position_error_m"] == 0.0
    assert audit["donor_position_error_m"] == 0.0
