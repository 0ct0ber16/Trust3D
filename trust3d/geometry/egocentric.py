"""世界坐标、第一视角坐标和 RGB-D 点云之间的确定性转换。"""

import math

import numpy as np


class GeometryError(ValueError):
    pass


def object_center(obj):
    """优先返回物体包围盒中心，缺失时退回模拟器位置。"""
    box = obj.get("axisAlignedBoundingBox") or {}
    center = box.get("center") or obj.get("position")
    if not isinstance(center, dict) or not {"x", "y", "z"} <= set(center):
        raise GeometryError("object has no usable 3D center")
    return {axis: float(center[axis]) for axis in ("x", "y", "z")}


def planar_bearing(origin, target):
    return math.degrees(
        math.atan2(target["x"] - origin["x"], target["z"] - origin["z"])
    ) % 360.0


def angular_separation(first, second):
    return abs((float(first) - float(second) + 180.0) % 360.0 - 180.0)


def diagnostic_query_yaw(origin, first, second):
    """让两个位置都在身后、分居左右，暴露陈旧位置事实。"""
    first_yaw = math.radians(planar_bearing(origin, first))
    second_yaw = math.radians(planar_bearing(origin, second))
    x = math.sin(first_yaw) + math.sin(second_yaw)
    z = math.cos(first_yaw) + math.cos(second_yaw)
    if math.hypot(x, z) < 1e-8:
        raise GeometryError("positions have opposite bearings")
    midpoint = math.degrees(math.atan2(x, z)) % 360.0
    return (midpoint + 180.0) % 360.0


def world_to_egocentric(point, agent_pose):
    dx = float(point["x"]) - float(agent_pose["x"])
    dy = float(point["y"]) - float(agent_pose["y"])
    dz = float(point["z"]) - float(agent_pose["z"])
    yaw = math.radians(float(agent_pose["rotation_y"]))
    return {
        "right": math.cos(yaw) * dx - math.sin(yaw) * dz,
        "up": dy,
        "forward": math.sin(yaw) * dx + math.cos(yaw) * dz,
        "distance": math.sqrt(dx * dx + dy * dy + dz * dz),
    }


def spatial_labels(point, agent_pose, epsilon=1e-6):
    ego = world_to_egocentric(point, agent_pose)
    if abs(ego["right"]) <= epsilon or abs(ego["forward"]) <= epsilon:
        raise GeometryError("point lies on an egocentric decision boundary")
    return {
        "left_right": "right" if ego["right"] > 0 else "left",
        "front_behind": "front" if ego["forward"] > 0 else "behind",
        "distance": ego["distance"],
        "right_margin": abs(ego["right"]),
        "forward_margin": abs(ego["forward"]),
    }


def camera_position(metadata):
    value = metadata.get("cameraPosition")
    if not isinstance(value, dict):
        value = metadata.get("agent", {}).get("position")
    if not isinstance(value, dict) or not {"x", "y", "z"} <= set(value):
        raise GeometryError("metadata has no camera position")
    return {axis: float(value[axis]) for axis in ("x", "y", "z")}


def rgbd_mask_centroid(
    depth,
    mask,
    camera,
    rotation_y,
    horizon,
    field_of_view=90.0,
    depth_scale=1.0,
    radial_depth=True,
):
    """将目标掩码的深度像素反投影，并返回稳健的世界坐标中位数。"""
    depth = np.asarray(depth, dtype=np.float64)
    mask = np.asarray(mask, dtype=bool)
    if depth.ndim != 2 or mask.shape != depth.shape:
        raise GeometryError("depth and mask must be equally sized 2D arrays")
    valid = mask & np.isfinite(depth) & (depth > 0)
    rows, columns = np.nonzero(valid)
    if len(rows) == 0:
        raise GeometryError("target mask has no valid depth pixels")

    height, width = depth.shape
    focal = 0.5 * height / math.tan(math.radians(field_of_view) / 2.0)
    x_camera = (columns + 0.5 - width / 2.0) / focal
    y_camera = -(rows + 0.5 - height / 2.0) / focal
    z_camera = np.ones_like(x_camera)
    values = depth[rows, columns] * float(depth_scale)
    if radial_depth:
        values = values / np.sqrt(
            x_camera * x_camera + y_camera * y_camera + z_camera * z_camera
        )
    x_camera *= values
    y_camera *= values
    z_camera *= values

    pitch = math.radians(float(horizon))
    y_pitched = math.cos(pitch) * y_camera - math.sin(pitch) * z_camera
    z_pitched = math.sin(pitch) * y_camera + math.cos(pitch) * z_camera
    yaw = math.radians(float(rotation_y))
    x_world = math.cos(yaw) * x_camera + math.sin(yaw) * z_pitched
    z_world = -math.sin(yaw) * x_camera + math.cos(yaw) * z_pitched
    points = {
        "x": x_world + float(camera["x"]),
        "y": y_pitched + float(camera["y"]),
        "z": z_world + float(camera["z"]),
    }
    return {axis: float(np.median(values)) for axis, values in points.items()}
