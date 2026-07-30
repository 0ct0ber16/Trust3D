"""只读诊断已冻结 CUT3R checkpoint 的相对姿态与中心 crop。"""

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from trust3d.data.select_events import read_jsonl
from trust3d.eval.evaluate_spatial import PRIMARY_ROUTE
from trust3d.geometry.egocentric import world_to_egocentric
from trust3d.geometry.run_cut3r import _atomic_json, _source_contexts


def _pose_camera_to_world(pose):
    yaw = math.radians(float(pose["rotation_y"]))
    pitch = math.radians(float(pose.get("horizon", 0.0)))
    pitch_matrix = np.asarray(
        [
            [1.0, 0.0, 0.0],
            [0.0, math.cos(pitch), -math.sin(pitch)],
            [0.0, math.sin(pitch), math.cos(pitch)],
        ]
    )
    yaw_matrix = np.asarray(
        [
            [math.cos(yaw), 0.0, math.sin(yaw)],
            [0.0, 1.0, 0.0],
            [-math.sin(yaw), 0.0, math.cos(yaw)],
        ]
    )
    value = np.eye(4)
    value[:3, :3] = yaw_matrix @ pitch_matrix
    value[:3, 3] = [pose["x"], pose["y"], pose["z"]]
    return value


def _gt_trajectories(context):
    stable = context["branches"]["risk_stable"]["verification"]
    stale = context["branches"]["risk_stale"]["verification"]
    query = context["query_pose"]
    historical_target = stable["target"]["pose"]
    historical_donor = stable["donor"]["pose"]
    return {
        "stable": [
            _pose_camera_to_world(historical_target),
            _pose_camera_to_world(historical_donor),
            _pose_camera_to_world(query),
            _pose_camera_to_world(stable["target"]["pose"]),
            _pose_camera_to_world(stable["donor"]["pose"]),
        ],
        "stale": [
            _pose_camera_to_world(historical_target),
            _pose_camera_to_world(historical_donor),
            _pose_camera_to_world(query),
            _pose_camera_to_world(stale["target"]["pose"]),
            _pose_camera_to_world(stale["donor"]["pose"]),
        ],
    }


def _rotation_error_degrees(first, second):
    delta = first.T @ second
    cosine = np.clip((np.trace(delta) - 1.0) / 2.0, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosine)))


def _direction_error_degrees(predicted, expected):
    first_norm = np.linalg.norm(predicted)
    second_norm = np.linalg.norm(expected)
    if first_norm <= 1e-8 or second_norm <= 1e-8:
        return None
    cosine = np.clip(
        float(np.dot(predicted, expected) / (first_norm * second_norm)),
        -1.0,
        1.0,
    )
    return float(np.degrees(np.arccos(cosine)))


def _relative_pose_diagnostics(predicted, expected):
    pairs = sorted(
        set(((0, 1), (1, 2), (2, 3), (3, 4), (2, 0), (2, 1), (2, 4)))
    )
    values = []
    for first, second in pairs:
        pred_first = predicted[first]
        pred_second = predicted[second]
        gt_first = expected[first]
        gt_second = expected[second]
        pred_relative_rotation = pred_first[:3, :3].T @ pred_second[:3, :3]
        gt_relative_rotation = gt_first[:3, :3].T @ gt_second[:3, :3]
        pred_direction = pred_first[:3, :3].T @ (
            pred_second[:3, 3] - pred_first[:3, 3]
        )
        gt_direction = gt_first[:3, :3].T @ (
            gt_second[:3, 3] - gt_first[:3, 3]
        )
        values.append(
            {
                "pair": [first, second],
                "rotation_error_degrees": _rotation_error_degrees(
                    pred_relative_rotation, gt_relative_rotation
                ),
                "translation_direction_error_degrees": _direction_error_degrees(
                    pred_direction, gt_direction
                ),
            }
        )
    return values


def _resize_mask(mask, width, height):
    image = Image.fromarray(np.asarray(mask, dtype=np.uint8) * 255)
    return np.asarray(
        image.resize((width, height), Image.Resampling.NEAREST), dtype=np.uint8
    ) > 0


def _crop_metrics(stage, observations, dataset_root):
    values = {}
    for role in ("target", "donor"):
        crop = stage[role]["crop_xyxy"]
        x0, y0, x1, y1 = crop
        side = x1 - x0
        width = 2 * (x0 + side // 2)
        height = 2 * (y0 + side // 2)
        mask_path = Path(dataset_root) / observations[role]["masks"][role]
        mask = _resize_mask(np.load(mask_path), width, height)
        selected = mask[y0:y1, x0:x1]
        inside = int(np.count_nonzero(selected))
        crop_pixels = int(selected.size)
        mask_pixels = int(np.count_nonzero(mask))
        values[role] = {
            "crop_xyxy": crop,
            "mapped_shape": [height, width],
            "crop_purity": inside / crop_pixels if crop_pixels else None,
            "mask_recall": inside / mask_pixels if mask_pixels else None,
            "mask_pixel_count": mask_pixels,
        }
    return values


def _group_diagnostics(group, context, dataset_root):
    expected = _gt_trajectories(context)
    relative_pose = {}
    for scenario in ("stable", "stale"):
        predicted = [
            np.asarray(value, dtype=np.float64)
            for value in group["camera_trajectories"][scenario]
        ]
        relative_pose[scenario] = _relative_pose_diagnostics(
            predicted, expected[scenario]
        )
    history = context["history"]["observations"]
    return {
        "relative_pose": relative_pose,
        "crop": {
            "historical": _crop_metrics(group["historical"], history, dataset_root),
            "stale_sequence_historical": _crop_metrics(
                group["stale_sequence_historical"], history, dataset_root
            ),
            "stable_reobserve": _crop_metrics(
                group["stable_reobserve"],
                context["branches"]["risk_stable"]["observations"],
                dataset_root,
            ),
            "stale_reobserve": _crop_metrics(
                group["stale_reobserve"],
                context["branches"]["risk_stale"]["observations"],
                dataset_root,
            ),
        },
    }


def _decision_margin(oracle, route, context):
    source = (
        context["branches"][oracle["branch"]]["gt"]
        if route["route"] == "reobserve"
        else context["history"]["gt"]
    )
    target = world_to_egocentric(source["target"], context["query_pose"])
    donor = world_to_egocentric(source["donor"], context["query_pose"])
    if oracle["question_type"] == "left_right":
        return abs(target["right"])
    if oracle["question_type"] == "front_behind":
        return abs(target["forward"])
    return abs(target["distance"] - donor["distance"])


def _error_strata(predictions, private, routes, contexts):
    prediction_by_id = {
        value["episode_id"]: value
        for value in predictions
        if value["method"] == "trust3d_cut3r"
    }
    route_by_id = {
        value["episode_id"]: value
        for value in routes
        if value["policy_id"] == PRIMARY_ROUTE
    }
    grouped = defaultdict(list)
    records = []
    for oracle in private:
        prediction = prediction_by_id[oracle["episode_id"]]
        context = contexts[oracle["group_id"]]
        record = {
            "episode_id": oracle["episode_id"],
            "group_id": oracle["group_id"],
            "branch": oracle["branch"],
            "question_type": oracle["question_type"],
            "object_type": context.get("target_object_type", "unknown"),
            "correct": bool(prediction["correct"]),
            "decision_margin": _decision_margin(
                oracle, route_by_id[oracle["episode_id"]], context
            ),
        }
        records.append(record)
        for key in (
            f"branch/{record['branch']}",
            f"question_type/{record['question_type']}",
            f"object_type/{record['object_type']}",
        ):
            grouped[key].append(record)
    return {
        "groups": {
            key: {
                "episode_count": len(values),
                "error_rate": 1
                - sum(value["correct"] for value in values) / len(values),
                "decision_margin_median": float(
                    np.median([value["decision_margin"] for value in values])
                ),
            }
            for key, values in sorted(grouped.items())
        },
        "front_behind_current_labels": dict(
            sorted(
                (label, sum(
                    item["question_type"] == "front_behind"
                    and item["current_answer_gt"] == label
                    for item in private
                ))
                for label in ("front", "behind")
            )
        ),
    }


def diagnose(args):
    if not os.environ.get("TMUX"):
        raise RuntimeError("CUT3R 诊断只能在 tmux 中执行")
    contexts = _source_contexts(args.source_checkpoints)
    manifest = json.loads(
        (args.geometry / "manifest.json").read_text(encoding="utf-8")
    )
    per_group = {}
    failures = []
    for item in manifest["groups"]:
        group_id = item["group_id"]
        try:
            group = json.loads(Path(item["checkpoint"]).read_text(encoding="utf-8"))
            per_group[group_id] = _group_diagnostics(
                group, contexts[group_id], args.dataset_root
            )
        except Exception as error:
            failures.append(
                {
                    "group_id": group_id,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    report = {
        "schema_version": 1,
        "diagnostic_only": True,
        "private_data_used_by_adapter": False,
        "dense_selected_point_mask_available": False,
        "dense_reprojection_reported": False,
        "group_count": len(per_group),
        "failure_count": len(failures),
        "failures": failures,
        "groups": per_group,
        "error_strata": _error_strata(
            read_jsonl(args.predictions),
            read_jsonl(args.private),
            read_jsonl(args.routes),
            contexts,
        ),
    }
    _atomic_json(args.output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--source-checkpoints", required=True, type=Path)
    parser.add_argument("--dataset-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = diagnose(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["failure_count"] == 0 and report["group_count"] == 30 else 1


if __name__ == "__main__":
    raise SystemExit(main())
