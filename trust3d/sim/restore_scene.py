"""Restore an ALFRED scene with the pinned AI2-THOR API."""


class SceneRestoreError(RuntimeError):
    pass


def _require_success(event, operation):
    metadata = event.metadata
    if not metadata.get("lastActionSuccess", False):
        raise SceneRestoreError(
            "{} failed: {}".format(operation, metadata.get("errorMessage", ""))
        )
    return event


def restore_scene(controller, trajectory):
    scene = trajectory.get("scene", {})
    scene_name = scene.get("floor_plan")
    if not scene_name:
        scene_num = scene.get("scene_num")
        if scene_num is None:
            raise SceneRestoreError("trajectory has no scene name or number")
        scene_name = "FloorPlan{}".format(scene_num)

    controller.reset(scene_name)
    event = controller.step(
        {
            "action": "Initialize",
            "gridSize": 0.25,
            "cameraY": 0.75,
            "renderImage": True,
            "renderDepthImage": True,
            "renderClassImage": False,
            "renderObjectImage": True,
            "visibilityDistance": 1.5,
            "makeAgentsVisible": False,
        }
    )
    _require_success(event, "Initialize")

    object_toggles = scene.get("object_toggles", [])
    if object_toggles:
        event = controller.step(
            {"action": "SetObjectToggles", "objectToggles": object_toggles}
        )
        _require_success(event, "SetObjectToggles")

    if scene.get("dirty_and_empty"):
        event = controller.step(
            {
                "action": "SetStateOfAllObjects",
                "StateChange": "CanBeDirty",
                "forceAction": True,
            }
        )
        _require_success(event, "SetStateOfAllObjects(CanBeDirty)")
        event = controller.step(
            {
                "action": "SetStateOfAllObjects",
                "StateChange": "CanBeFilled",
                "forceAction": False,
            }
        )
        _require_success(event, "SetStateOfAllObjects(CanBeFilled)")

    event = controller.step(
        {"action": "SetObjectPoses", "objectPoses": scene.get("object_poses", [])}
    )
    _require_success(event, "SetObjectPoses")

    init_action = scene.get("init_action")
    if not isinstance(init_action, dict):
        raise SceneRestoreError("trajectory has no scene.init_action")
    event = controller.step(dict(init_action))
    return _require_success(event, "scene.init_action")
