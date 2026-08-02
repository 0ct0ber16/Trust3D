"""Gate 6 使用的可审计物体位置交换。"""

import math

import numpy as np

from trust3d.sim.state_hash import find_object, find_object_by_name


class SpatialInterventionError(RuntimeError):
    pass


def distance(first, second):
    return math.sqrt(
        sum((float(first[axis]) - float(second[axis])) ** 2 for axis in "xyz")
    )


def visible_pickupables(event, minimum_pixels=64):
    masks = getattr(event, "instance_masks", {}) or {}
    values = []
    for obj in event.metadata.get("objects", []):
        mask = masks.get(obj.get("objectId"))
        pixels = int(np.count_nonzero(mask)) if mask is not None else 0
        if (
            obj.get("pickupable")
            and obj.get("visible")
            and not obj.get("isPickedUp")
            and obj.get("name")
            and pixels >= minimum_pixels
        ):
            values.append((obj, pixels))
    return values


def scene_pickupables(event):
    """返回当前场景中可用于位置干预、且没有被 Agent 拿起的物体。"""
    return [
        obj
        for obj in event.metadata.get("objects", [])
        if obj.get("pickupable")
        and not obj.get("isPickedUp")
        and obj.get("name")
        and isinstance(obj.get("position"), dict)
    ]


def scene_pairs(event, minimum_distance=0.75):
    """按间距降序返回同类型物体对，供后续逐对验证可见性。"""
    objects = sorted(scene_pickupables(event), key=lambda item: item["objectId"])
    candidates = []
    for index, target in enumerate(objects):
        for donor in objects[index + 1 :]:
            separation = distance(target["position"], donor["position"])
            if separation < minimum_distance:
                continue
            same_type = donor.get("objectType") == target.get("objectType")
            if not same_type:
                continue
            candidates.append(
                (
                    -separation,
                    target["objectId"],
                    donor["objectId"],
                    target,
                    donor,
                )
            )
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[:3])
    return [(item[-2], item[-1]) for item in candidates]


def choose_scene_pair(event, minimum_distance=0.75):
    """返回优先物体对；完整数据构造会逐对检查可见性。"""
    pairs = scene_pairs(event, minimum_distance=minimum_distance)
    return pairs[0] if pairs else None


def choose_visible_pair(event, minimum_distance=0.75, minimum_pixels=64):
    visible = visible_pickupables(event, minimum_pixels=minimum_pixels)
    candidates = []
    for target, target_pixels in visible:
        for donor, donor_pixels in visible:
            if donor["objectId"] == target["objectId"]:
                continue
            separation = distance(target["position"], donor["position"])
            if separation < minimum_distance:
                continue
            same_type = donor.get("objectType") == target.get("objectType")
            candidates.append(
                (
                    not same_type,
                    -min(target_pixels, donor_pixels),
                    -separation,
                    target["objectId"],
                    donor["objectId"],
                    target,
                    donor,
                )
            )
    if not candidates:
        return None
    selected = min(candidates, key=lambda item: item[:5])
    return selected[-2], selected[-1]


def _pose_record(obj, position):
    return {
        "objectName": obj["name"],
        "position": dict(position),
        "rotation": dict(obj["rotation"]),
    }


def _position_error(event, object_name, expected):
    return distance(
        find_object_by_name(event.metadata, object_name)["position"], expected
    )


def _teleport_object(controller, obj, position):
    event = controller.step(
        {
            "action": "TeleportObject",
            "objectId": obj["objectId"],
            "x": float(position["x"]),
            "y": float(position["y"]),
            "z": float(position["z"]),
            "rotation": dict(obj["rotation"]),
            "forceAction": True,
        }
    )
    if not event.metadata.get("lastActionSuccess", False):
        raise SpatialInterventionError(
            "TeleportObject failed: {}".format(
                event.metadata.get("errorMessage", "")
            )
        )
    return event


def swap_objects(controller, target_id, donor_id, tolerance=0.05):
    objects = controller.last_event.metadata.get("objects", [])
    target = find_object(controller.last_event.metadata, target_id)
    donor = find_object(controller.last_event.metadata, donor_id)
    target_name = target["name"]
    donor_name = donor["name"]
    target_position = dict(target["position"])
    donor_position = dict(donor["position"])
    object_poses = []
    for obj in objects:
        if not obj.get("pickupable") and obj.get("objectId") not in {
            target_id,
            donor_id,
        }:
            continue
        if not obj.get("name") or not isinstance(obj.get("rotation"), dict):
            continue
        position = obj.get("position")
        if obj.get("objectId") == target_id:
            position = donor_position
        elif obj.get("objectId") == donor_id:
            position = target_position
        object_poses.append(_pose_record(obj, position))
    event = controller.step(
        {
            "action": "SetObjectPoses",
            "objectPoses": object_poses,
        }
    )
    if not event.metadata.get("lastActionSuccess", False):
        raise SpatialInterventionError(
            "SetObjectPoses failed: {}".format(
                event.metadata.get("errorMessage", "")
            )
        )
    target_error = _position_error(event, target_name, donor_position)
    donor_error = _position_error(event, donor_name, target_position)
    method = "SetObjectPoses"
    if target_error > tolerance or donor_error > tolerance:
        temporary = dict(target_position)
        temporary["y"] = float(temporary["y"]) + 2.0
        target = find_object_by_name(controller.last_event.metadata, target_name)
        _teleport_object(controller, target, temporary)
        donor = find_object_by_name(controller.last_event.metadata, donor_name)
        _teleport_object(controller, donor, target_position)
        target = find_object_by_name(controller.last_event.metadata, target_name)
        event = _teleport_object(controller, target, donor_position)
        target_error = _position_error(event, target_name, donor_position)
        donor_error = _position_error(event, donor_name, target_position)
        method = "TeleportObject"
    if target_error > tolerance or donor_error > tolerance:
        raise SpatialInterventionError(
            "position swap error exceeds tolerance: {:.4f}, {:.4f}".format(
                target_error, donor_error
            )
        )
    return event, {
        "target_old_position": target_position,
        "donor_old_position": donor_position,
        "target_position_error_m": target_error,
        "donor_position_error_m": donor_error,
        "method": method,
    }
