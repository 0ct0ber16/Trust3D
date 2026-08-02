"""Stable hashes for a target object's state and the Agent query pose."""

import hashlib
import json


TARGET_FIELDS = (
    "objectId",
    "objectType",
    "position",
    "rotation",
    "isOpen",
    "isToggled",
    "isPickedUp",
    "isSliced",
    "isDirty",
    "isCooked",
    "isBroken",
)


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        return round(value, 5)
    return value


def find_object(metadata, object_id):
    for obj in metadata.get("objects", []):
        if obj.get("objectId") == object_id:
            return obj
    raise KeyError("object not found in metadata: {}".format(object_id))


def find_object_by_name(metadata, object_name):
    matches = [
        obj
        for obj in metadata.get("objects", [])
        if obj.get("name") == object_name
    ]
    if len(matches) != 1:
        raise KeyError(
            "object name must resolve uniquely: {} (matches={})".format(
                object_name, len(matches)
            )
        )
    return matches[0]


def canonical_pose(metadata):
    agent = metadata["agent"]
    return _canonical(
        {
            "x": agent["position"]["x"],
            "y": agent["position"]["y"],
            "z": agent["position"]["z"],
            "rotation_y": agent["rotation"]["y"],
            "horizon": agent["cameraHorizon"],
        }
    )


def critical_state(metadata, object_id):
    obj = find_object(metadata, object_id)
    return {
        "target": _canonical(
            {field: obj[field] for field in TARGET_FIELDS if field in obj}
        ),
        "agent_pose": canonical_pose(metadata),
    }


def state_hash(metadata, object_id):
    payload = json.dumps(
        critical_state(metadata, object_id),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
