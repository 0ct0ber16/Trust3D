"""Gate 0 smoke test for the pinned AI2-THOR simulator."""

import argparse
import hashlib
import json
import math
import os
import platform
import random
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image


OBJECT_STATE_FIELDS = (
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
    "temperature",
)


def _log(message):
    print("[gate0] " + message, flush=True)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _canonical_object_states(metadata):
    states = []
    for obj in metadata.get("objects", []):
        state = {
            field: _json_safe(obj[field])
            for field in OBJECT_STATE_FIELDS
            if field in obj
        }
        states.append(state)
    return sorted(states, key=lambda item: item.get("objectId", ""))


def object_state_hash(metadata):
    payload = json.dumps(
        _canonical_object_states(metadata),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _package_version(package_name):
    try:
        import importlib_metadata

        return importlib_metadata.version(package_name)
    except Exception:
        return "unknown"


def _git_commit():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "uncommitted"


def _simulator_build_record():
    try:
        from ai2thor.controller import Controller

        url, sha256 = Controller().build_url()
        return {"url": url, "sha256": sha256}
    except Exception as exc:
        return {"error": type(exc).__name__ + ": " + str(exc)}


def environment_record(args):
    return {
        "captured_at_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "command": [sys.executable] + sys.argv,
        "git_commit": _git_commit(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "display": os.environ.get("DISPLAY", "unset"),
        "scene": args.scene,
        "seed": args.seed,
        "screen": {"width": args.width, "height": args.height},
        "simulator_build": _simulator_build_record(),
        "packages": {
            "ai2thor": _package_version("ai2thor"),
            "flask": _package_version("Flask"),
            "numpy": _package_version("numpy"),
            "pillow": _package_version("Pillow"),
            "werkzeug": _package_version("Werkzeug"),
        },
    }


def _initialize_event(controller, scene):
    _log("resetting scene " + scene)
    controller.reset(scene)
    _log("initializing RGB, depth, and instance rendering")
    event = controller.step(
        {
            "action": "Initialize",
            "gridSize": 0.25,
            "renderDepthImage": True,
            "renderObjectImage": True,
            "visibilityDistance": 1.5,
        },
        raise_for_failure=True,
    )
    _log("initialization frame received")
    return event


def execute_once(scene, seed, width, height):
    from ai2thor.controller import Controller

    random.seed(seed)
    np.random.seed(seed)
    controller = Controller(quality="Low")
    started = False
    try:
        _log("starting a fresh Unity process")
        controller.start(
            player_screen_width=width,
            player_screen_height=height,
        )
        started = True
        _log("Unity process connected")
        event = _initialize_event(controller, scene)
        _log("copying initial observation and metadata")
        metadata = _json_safe(event.metadata)
        result = {
            "rgb": None if event.frame is None else np.asarray(event.frame).copy(),
            "depth": None
            if event.depth_frame is None
            else np.asarray(event.depth_frame).copy(),
            "instance_segmentation": None
            if event.instance_segmentation_frame is None
            else np.asarray(event.instance_segmentation_frame).copy(),
            "metadata": metadata,
            "state_hash": object_state_hash(metadata),
        }

        _log("executing legal action RotateRight")
        action_event = controller.step(
            {"action": "RotateRight"},
            raise_for_failure=True,
        )
        result["action"] = {
            "name": "RotateRight",
            "success": bool(action_event.metadata.get("lastActionSuccess", False)),
            "error_message": action_event.metadata.get("errorMessage", ""),
        }
        _log("RotateRight result received")
        return result
    finally:
        if started:
            _log("stopping Unity process")
            try:
                controller.stop()
            except Exception:
                pass
            _log("Unity process stopped")


def validate_runs(first, second):
    rgb = first.get("rgb")
    depth = first.get("depth")
    instance = first.get("instance_segmentation")
    metadata = first.get("metadata", {})
    checks = {
        "rgb_available": isinstance(rgb, np.ndarray) and rgb.size > 0,
        "rgb_non_black": isinstance(rgb, np.ndarray)
        and rgb.size > 0
        and bool(np.any(rgb != 0)),
        "depth_available": isinstance(depth, np.ndarray) and depth.size > 0,
        "depth_has_finite_values": isinstance(depth, np.ndarray)
        and depth.size > 0
        and bool(np.isfinite(depth).any()),
        "instance_segmentation_available": isinstance(instance, np.ndarray)
        and instance.size > 0,
        "agent_pose_available": isinstance(metadata.get("agent"), dict)
        and isinstance(metadata.get("agent", {}).get("position"), dict),
        "object_list_nonempty": isinstance(metadata.get("objects"), list)
        and len(metadata.get("objects", [])) > 0,
        "legal_action_succeeded": bool(first.get("action", {}).get("success")),
        "initial_object_state_deterministic": first.get("state_hash")
        == second.get("state_hash"),
    }
    return checks


def _write_json(path, value):
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_artifacts(output_dir, run):
    if isinstance(run.get("rgb"), np.ndarray):
        Image.fromarray(run["rgb"].astype(np.uint8)).save(output_dir / "rgb.png")
    if isinstance(run.get("depth"), np.ndarray):
        np.save(str(output_dir / "depth.npy"), run["depth"])
    if isinstance(run.get("instance_segmentation"), np.ndarray):
        Image.fromarray(run["instance_segmentation"].astype(np.uint8)).save(
            output_dir / "instance_segmentation.png"
        )
    _write_json(output_dir / "metadata.json", run["metadata"])


def run_smoke_test(args):
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "env.json", environment_record(args))

    try:
        _log("running first cold start")
        first = execute_once(args.scene, args.seed, args.width, args.height)
        _log("running second cold start")
        second = execute_once(args.scene, args.seed, args.width, args.height)
        checks = validate_runs(first, second)
        report = {
            "passed": all(checks.values()),
            "checks": checks,
            "scene": args.scene,
            "seed": args.seed,
            "state_hashes": [first["state_hash"], second["state_hash"]],
            "action": first["action"],
        }
        save_artifacts(output_dir, first)
    except Exception as exc:
        report = {
            "passed": False,
            "checks": {},
            "scene": args.scene,
            "seed": args.seed,
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "traceback": traceback.format_exc(),
        }

    _write_json(output_dir / "report.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--scene", default="FloorPlan1")
    parser.add_argument("--output", default="outputs/gate0")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--width", type=int, default=300)
    parser.add_argument("--height", type=int, default=300)
    args = parser.parse_args(argv)
    if not args.smoke_test:
        parser.error("--smoke-test is required for Gate 0")
    if args.width < 300 or args.height < 300:
        parser.error("AI2-THOR 2.1.0 requires a resolution of at least 300x300")
    return args


def main(argv=None):
    return run_smoke_test(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
