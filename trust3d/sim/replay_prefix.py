"""Replay ALFRED low-level actions up to a target event."""


ALLOWED_API_KEYS = {
    "action",
    "forceAction",
    "moveMagnitude",
    "objectId",
    "placeStationary",
    "receptacleObjectId",
}


class ReplayError(RuntimeError):
    pass


def sanitize_action(action):
    return {key: action[key] for key in action if key in ALLOWED_API_KEYS}


def _look_action(controller, action_name):
    agent = controller.last_event.metadata["agent"]
    delta = -15 if action_name == "LookUp" else 15
    return controller.step(
        {
            "action": "TeleportFull",
            "x": agent["position"]["x"],
            "y": agent["position"]["y"],
            "z": agent["position"]["z"],
            "rotation": agent["rotation"]["y"],
            "horizon": agent["cameraHorizon"] + delta,
            "tempRenderChange": True,
            "renderNormalsImage": False,
            "renderImage": True,
            "renderDepthImage": True,
            "renderClassImage": False,
            "renderObjectImage": True,
        }
    )


def replay_action(controller, action, action_index=None):
    command = sanitize_action(action)
    action_name = command.get("action")
    if not action_name:
        raise ReplayError("action has no name")
    if action_name in {"LookUp", "LookDown"}:
        event = _look_action(controller, action_name)
    else:
        event = controller.step(command)
    if not event.metadata.get("lastActionSuccess", False):
        location = "" if action_index is None else " at index {}".format(action_index)
        raise ReplayError(
            "{}{} failed: {}".format(
                action_name,
                location,
                event.metadata.get("errorMessage", ""),
            )
        )
    return event, command


def replay_prefix(controller, trajectory, stop_index):
    low_actions = trajectory.get("plan", {}).get("low_actions", [])
    if stop_index < 0 or stop_index >= len(low_actions):
        raise ReplayError("target action index is outside plan.low_actions")
    commands = []
    event = controller.last_event
    for action_index, low_action in enumerate(low_actions[:stop_index]):
        api_action = low_action.get("api_action", {})
        event, command = replay_action(controller, api_action, action_index)
        commands.append(command)
    return event, commands


def source_action(trajectory, action_index):
    try:
        api_action = trajectory["plan"]["low_actions"][action_index]["api_action"]
    except (KeyError, IndexError, TypeError):
        raise ReplayError("source action is missing at index {}".format(action_index))
    return sanitize_action(api_action)
