"""Camera convention primitives used by the repaired Gate 7 adapter."""

from __future__ import annotations

import numpy as np


def as_homogeneous(matrix) -> np.ndarray:
    value = np.asarray(matrix, dtype=np.float64)
    if value.shape != (4, 4) or not np.all(np.isfinite(value)):
        raise ValueError("camera transform must be a finite 4x4 matrix")
    if not np.allclose(value[3], [0.0, 0.0, 0.0, 1.0], atol=1e-9):
        raise ValueError("camera transform has an invalid homogeneous row")
    return value


def camera_to_world_to_world_to_camera(matrix) -> np.ndarray:
    return np.linalg.inv(as_homogeneous(matrix))


def transform_point(matrix, point) -> np.ndarray:
    transform = as_homogeneous(matrix)
    value = np.asarray(point, dtype=np.float64)
    if value.shape != (3,) or not np.all(np.isfinite(value)):
        raise ValueError("point must be a finite 3-vector")
    return (transform @ np.append(value, 1.0))[:3]


def opencv_to_opengl_camera(point) -> np.ndarray:
    value = np.asarray(point, dtype=np.float64)
    if value.shape != (3,):
        raise ValueError("point must have shape (3,)")
    return value * np.asarray([1.0, -1.0, -1.0])


def planar_answers(target, donor, camera_to_world, epsilon: float = 1e-6):
    pose = as_homogeneous(camera_to_world)
    target = np.asarray(target, dtype=np.float64)
    donor = np.asarray(donor, dtype=np.float64)
    forward = pose[:3, 2].copy()
    forward[1] = 0.0
    norm = float(np.linalg.norm(forward))
    if norm <= epsilon:
        raise ValueError("predicted planar forward vector is degenerate")
    forward /= norm
    right = np.asarray([forward[2], 0.0, -forward[0]])
    center = pose[:3, 3]

    def project(point):
        delta = point - center
        return (
            float(np.dot(delta, right)),
            float(np.dot(delta, forward)),
            float(np.linalg.norm(delta)),
        )

    target_right, target_forward, target_distance = project(target)
    _, _, donor_distance = project(donor)
    if min(
        abs(target_right),
        abs(target_forward),
        abs(target_distance - donor_distance),
    ) <= epsilon:
        raise ValueError("point lies on a preregistered decision boundary")
    target_nearer = target_distance < donor_distance
    return {
        "left_right": "right" if target_right > 0 else "left",
        "front_behind": "front" if target_forward > 0 else "behind",
        "which_closer": "target" if target_nearer else "reference",
        "target_nearer": target_nearer,
    }
