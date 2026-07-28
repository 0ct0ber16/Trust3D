"""Find hidden query poses and shortest reachable target verification poses."""

import hashlib
import math
from collections import deque

from trust3d.sim.state_hash import canonical_pose, find_object


class VisibilityError(RuntimeError):
    pass


def _position_key(position, grid_size=0.25):
    return (
        int(round(position["x"] / grid_size)),
        int(round(position["z"] / grid_size)),
    )


def shortest_grid_distances(positions, start, grid_size=0.25):
    by_key = {_position_key(position, grid_size): position for position in positions}
    if not by_key:
        return {}
    start_key = min(
        by_key,
        key=lambda key: (key[0] - start[0]) ** 2 + (key[1] - start[1]) ** 2,
    )
    distances = {start_key: 0}
    queue = deque([start_key])
    while queue:
        key = queue.popleft()
        for neighbor in (
            (key[0] - 1, key[1]),
            (key[0] + 1, key[1]),
            (key[0], key[1] - 1),
            (key[0], key[1] + 1),
        ):
            if neighbor in by_key and neighbor not in distances:
                distances[neighbor] = distances[key] + 1
                queue.append(neighbor)
    return {key: distances[key] for key in distances}


def teleport_to_pose(controller, pose):
    event = controller.step(
        {
            "action": "TeleportFull",
            "x": pose["x"],
            "y": pose["y"],
            "z": pose["z"],
            "rotation": pose["rotation_y"],
            "horizon": pose["horizon"],
        }
    )
    if not event.metadata.get("lastActionSuccess", False):
        raise VisibilityError(
            "TeleportFull failed: {}".format(event.metadata.get("errorMessage", ""))
        )
    return event


def _reachable_positions(controller):
    event = controller.step({"action": "GetReachablePositions"})
    if not event.metadata.get("lastActionSuccess", False):
        raise VisibilityError("GetReachablePositions failed")
    positions = event.metadata.get("actionReturn")
    if not isinstance(positions, list) or not positions:
        raise VisibilityError("GetReachablePositions returned no positions")
    return positions


def _yaw_toward(position, target_position):
    return math.degrees(
        math.atan2(
            target_position["x"] - position["x"],
            target_position["z"] - position["z"],
        )
    ) % 360.0


def _angle_steps(first, second, increment):
    difference = abs((first - second + 180.0) % 360.0 - 180.0)
    return int(math.ceil(difference / increment - 1e-8))


def choose_hidden_query_pose(controller, target_object_id, seed=17):
    original_pose = canonical_pose(controller.last_event.metadata)
    target = find_object(controller.last_event.metadata, target_object_id)
    target_position = target["position"]
    positions = _reachable_positions(controller)

    def rank(position):
        distance = (position["x"] - target_position["x"]) ** 2 + (
            position["z"] - target_position["z"]
        ) ** 2
        tie = hashlib.sha256(
            "{}|{:.4f}|{:.4f}".format(seed, position["x"], position["z"]).encode(
                "ascii"
            )
        ).hexdigest()
        return (-distance, tie)

    try:
        for position in sorted(positions, key=rank):
            away = (_yaw_toward(position, target_position) + 180.0) % 360.0
            pose = {
                "x": position["x"],
                "y": position["y"],
                "z": position["z"],
                "rotation_y": away,
                "horizon": 30.0,
            }
            event = teleport_to_pose(controller, pose)
            if not find_object(event.metadata, target_object_id).get("visible", False):
                return canonical_pose(event.metadata)
    finally:
        teleport_to_pose(controller, original_pose)
    raise VisibilityError("no reachable query pose hides target")


def _pose_candidates(positions, distances, query_pose, target_position):
    candidates = []
    for position in positions:
        key = _position_key(position)
        if key not in distances:
            continue
        planar_distance = math.sqrt(
            (position["x"] - target_position["x"]) ** 2
            + (position["z"] - target_position["z"]) ** 2
        )
        if planar_distance > 1.75:
            continue
        toward = _yaw_toward(position, target_position)
        rotations = {toward, round(toward / 90.0) * 90.0 % 360.0}
        horizons = {0.0, 30.0, 60.0, query_pose["horizon"]}
        for rotation in rotations:
            for horizon in horizons:
                cost = (
                    distances[key]
                    + _angle_steps(query_pose["rotation_y"], rotation, 90.0)
                    + int(abs(query_pose["horizon"] - horizon) / 15.0)
                )
                candidates.append(
                    (
                        cost,
                        planar_distance,
                        rotation,
                        horizon,
                        {
                            "x": position["x"],
                            "y": position["y"],
                            "z": position["z"],
                            "rotation_y": rotation,
                            "horizon": horizon,
                        },
                    )
                )
    return sorted(candidates, key=lambda item: item[:4])


def find_verification_pose(
    controller, target_object_id, query_pose, preferred_pose=None
):
    teleport_to_pose(controller, query_pose)
    target = find_object(controller.last_event.metadata, target_object_id)
    if target.get("visible", False):
        return {"pose": canonical_pose(controller.last_event.metadata), "cost": 0}

    positions = _reachable_positions(controller)
    start_key = _position_key(query_pose)
    distances = shortest_grid_distances(positions, start_key)
    candidates = _pose_candidates(
        positions, distances, query_pose, target["position"]
    )
    try:
        for cost, _, _, _, pose in candidates:
            event = teleport_to_pose(controller, pose)
            if find_object(event.metadata, target_object_id).get("visible", False):
                return {"pose": canonical_pose(event.metadata), "cost": cost}
        if preferred_pose is not None:
            event = teleport_to_pose(controller, preferred_pose)
            if find_object(event.metadata, target_object_id).get("visible", False):
                preferred_key = _position_key(preferred_pose)
                return {
                    "pose": canonical_pose(event.metadata),
                    "cost": distances.get(preferred_key),
                }
    finally:
        teleport_to_pose(controller, query_pose)
    raise VisibilityError("no reachable pose can see target")


def verify_cached_pose(controller, target_object_id, query_pose, oracle):
    try:
        event = teleport_to_pose(controller, oracle["pose"])
        if not find_object(event.metadata, target_object_id).get("visible", False):
            raise VisibilityError("cached verification pose no longer sees target")
    finally:
        teleport_to_pose(controller, query_pose)
    return oracle
