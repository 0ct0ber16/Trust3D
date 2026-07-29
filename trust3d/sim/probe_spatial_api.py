"""探测固定 AI2-THOR 版本中的物体位置干预和 RGB-D 元数据。"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from trust3d.data.build_branches import (
    _atomic_json,
    _load_trajectory,
    _sha256_file,
)
from trust3d.sim.replay_prefix import replay_prefix
from trust3d.sim.restore_scene import restore_scene
from trust3d.sim.spatial_intervention import (
    choose_scene_pair,
    distance,
    swap_objects,
)


def run_probe(controller, selection_path, alfred_json, output_path):
    selection = json.loads(Path(selection_path).read_text(encoding="utf-8"))
    selection_sha = _sha256_file(selection_path)
    output_path = Path(output_path)
    failures = []
    start_index = 0
    if output_path.is_file():
        checkpoint = json.loads(output_path.read_text(encoding="utf-8"))
        if (
            checkpoint.get("schema_version") == 6
            and checkpoint.get("selection_sha256") == selection_sha
            and checkpoint.get("status") == "in_progress"
        ):
            failures = checkpoint.get("failed_candidates", [])
            start_index = int(checkpoint.get("next_candidate_index", 0))

    for index, candidate in enumerate(selection["candidates"][start_index:], start_index):
        try:
            trajectory, trajectory_sha = _load_trajectory(alfred_json, candidate)
            event = restore_scene(controller, trajectory)
            event, _ = replay_prefix(
                controller, trajectory, candidate["action_index"]
            )
            pair = choose_scene_pair(event)
            if pair is None:
                raise RuntimeError("场景中没有相距足够远的可交换物体对")
            target, donor = pair
            old_target_position = dict(target["position"])
            old_donor_position = dict(donor["position"])
            depth = np.asarray(event.depth_frame)
            _, swap = swap_objects(
                controller, target["objectId"], donor["objectId"]
            )
            target_error = swap["target_position_error_m"]
            donor_error = swap["donor_position_error_m"]
            result = {
                "schema_version": 6,
                "status": "success",
                "captured_at_utc": datetime.utcnow()
                .replace(microsecond=0)
                .isoformat()
                + "Z",
                "selection_sha256": selection_sha,
                "gate6_api_probe_pass": True,
                "candidate_id": candidate["candidate_id"],
                "trajectory_sha256": trajectory_sha,
                "target_object_id": target["objectId"],
                "target_object_name": target["name"],
                "target_object_type": target.get("objectType"),
                "donor_object_id": donor["objectId"],
                "donor_object_name": donor["name"],
                "donor_object_type": donor.get("objectType"),
                "position_swap_distance_m": distance(
                    old_target_position, old_donor_position
                ),
                "target_position_error_m": target_error,
                "donor_position_error_m": donor_error,
                "intervention_method": swap["method"],
                "camera_position_available": isinstance(
                    event.metadata.get("cameraPosition"), dict
                ),
                "depth": {
                    "shape": list(depth.shape),
                    "dtype": str(depth.dtype),
                    "minimum": float(np.nanmin(depth)),
                    "median": float(np.nanmedian(depth)),
                    "maximum": float(np.nanmax(depth)),
                },
                "failed_candidates_before_success": failures,
            }
            _atomic_json(output_path, result)
            return result
        except Exception as exc:
            failures.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "reason": type(exc).__name__ + ": " + str(exc),
                }
            )
            _atomic_json(
                output_path,
                {
                    "schema_version": 6,
                    "status": "in_progress",
                    "captured_at_utc": datetime.utcnow()
                    .replace(microsecond=0)
                    .isoformat()
                    + "Z",
                    "selection_sha256": selection_sha,
                    "gate6_api_probe_pass": False,
                    "next_candidate_index": index + 1,
                    "failed_candidates": failures,
                },
            )
    result = {
        "schema_version": 6,
        "status": "failed",
        "captured_at_utc": datetime.utcnow()
        .replace(microsecond=0)
        .isoformat()
        + "Z",
        "selection_sha256": selection_sha,
        "gate6_api_probe_pass": False,
        "failed_candidates": failures,
    }
    _atomic_json(output_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", required=True)
    parser.add_argument("--alfred-json", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    from ai2thor.controller import Controller

    controller = Controller(quality="Low")
    controller.start(player_screen_width=300, player_screen_height=300)
    try:
        result = run_probe(
            controller, args.selection, args.alfred_json, args.output
        )
    finally:
        controller.stop()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["gate6_api_probe_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
