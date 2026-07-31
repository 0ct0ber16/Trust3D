"""Seal and evaluate repaired Gate 7 predictions with a private-data boundary."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from trust3d.geometry.camera_contract import planar_answers
from trust3d.parallel_v2.common import (
    ROOT,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    protocol,
    sha256_file,
    utc_now,
)


PRIMARY_POLICY = "trust3d_lambda_0.01"


def geometry_groups(root: Path):
    manifest = load_json(root / "manifest.json")
    groups = {}
    for record in manifest.get("groups", []):
        checkpoint = Path(record["checkpoint"])
        if not checkpoint.is_absolute():
            checkpoint = ROOT / checkpoint
        groups[record["group_id"]] = load_json(checkpoint)
    return manifest, groups


def primary_routes(path: Path):
    routes = {}
    for value in load_jsonl(path):
        if value["policy_id"] == PRIMARY_POLICY:
            routes[value["episode_id"]] = value
    return routes


def candidate_stages(route):
    if route["route"] == "reobserve":
        return ("stable_reobserve", "stale_reobserve")
    return ("historical",)


def _legacy_answer(stage, question_type):
    return stage["answers"][question_type]


def _contract_answer(stage, question_type):
    answers = planar_answers(
        stage["target"]["world"],
        stage["donor"]["world"],
        stage["query_camera_to_world"],
    )
    return answers[question_type]


def _component_confidence(stage):
    model_values = [
        max(0.0, float(stage[role].get("confidence_median", 0.0)))
        for role in ("target", "donor")
    ]
    geometry_confidence = float(
        np.mean([value / (1.0 + value) for value in model_values])
    )
    grounding_values = [
        float(stage[role].get("grounding", {}).get("confidence", 0.5))
        for role in ("target", "donor")
    ]
    grounding_confidence = float(np.mean(grounding_values))
    camera = np.asarray(stage["query_camera_to_world"], dtype=np.float64)
    camera_confidence = 1.0 if camera.shape == (4, 4) and np.isfinite(camera).all() else 0.0
    answer_confidence = float(
        max(1e-9, grounding_confidence * camera_confidence * geometry_confidence)
        ** (1.0 / 3.0)
    )
    return {
        "grounding_confidence": grounding_confidence,
        "camera_confidence": camera_confidence,
        "geometry_confidence": geometry_confidence,
        "answer_confidence": answer_confidence,
    }


def _calibrate(confidence, lock):
    index = min(int(float(confidence) * 5), 4)
    return float(lock["bins"][index]["error_probability"])


def seal_predictions(
    public_path: Path,
    routes_path: Path,
    geometry_root: Path,
    backend: str,
    grounding: str,
    output_root: Path,
    confidence_lock: dict[str, Any] = None,
):
    public = load_jsonl(public_path)
    routes = primary_routes(routes_path)
    manifest, geometry = geometry_groups(geometry_root)
    geometry_access_path = geometry_root / "inference_access_audit.json"
    if not geometry_access_path.is_file():
        raise RuntimeError("geometry inference access audit is missing")
    geometry_access = load_json(geometry_access_path)
    if (
        geometry_access.get("guard_installed") is not True
        or geometry_access.get("private_file_open_count") != 0
        or geometry_access.get("forbidden_open_attempt_count") != 0
    ):
        raise RuntimeError("geometry inference access audit failed")
    if set(routes) != {item["episode_id"] for item in public}:
        raise ValueError("primary routes do not cover public episodes")
    predictions = []
    failures = []
    for episode in public:
        group = geometry.get(episode["group_id"])
        if not group or group.get("status") != "success":
            failures.append(episode["group_id"])
            continue
        route = routes[episode["episode_id"]]
        try:
            candidates = {}
            for selected_stage in candidate_stages(route):
                stage = group[selected_stage]
                confidence = _component_confidence(stage)
                candidates[selected_stage] = {
                    "legacy_answer": _legacy_answer(
                        stage, episode["question"]["type"]
                    ),
                    "contract_valid_answer": _contract_answer(
                        stage, episode["question"]["type"]
                    ),
                    **confidence,
                    "calibrated_error_probability": _calibrate(
                        confidence["answer_confidence"], confidence_lock
                    )
                    if confidence_lock is not None
                    else None,
                }
            predictions.append(
                {
                    "schema_version": 1,
                    "episode_id": episode["episode_id"],
                    "group_id": episode["group_id"],
                    "backend": backend,
                    "grounding": grounding,
                    "route": route["route"],
                    "scenario_policy": "sealed_counterfactual_pair_v1",
                    "candidates": candidates,
                }
            )
        except (KeyError, ValueError) as error:
            failures.append(episode["group_id"])
            predictions.append(
                {
                    "schema_version": 1,
                    "episode_id": episode["episode_id"],
                    "group_id": episode["group_id"],
                    "backend": backend,
                    "grounding": grounding,
                    "route": route["route"],
                    "scenario_policy": "sealed_counterfactual_pair_v1",
                    "candidates": {},
                    "failure": f"{type(error).__name__}: {error}",
                }
            )
    predictions.sort(key=lambda item: item["episode_id"])
    output_root.mkdir(parents=True, exist_ok=True)
    predictions_path = output_root / "predictions.jsonl"
    atomic_jsonl(predictions_path, predictions)
    digest = sha256_file(predictions_path)
    atomic_json(
        output_root / "predictions.sha256.json",
        {"path": predictions_path.name, "sha256": digest, "size": predictions_path.stat().st_size},
    )
    access = {
        "schema_version": 1,
        "phase": "inference",
        "private_file_open_count": 0,
        "gt_mask_open_count": 0,
        "gt_bbox_open_count": 0,
        "gt_pose_alignment_open_count": 0,
        "public_sha256": sha256_file(public_path),
        "routes_sha256": sha256_file(routes_path),
        "geometry_manifest_sha256": sha256_file(geometry_root / "manifest.json"),
        "geometry_access_audit_sha256": sha256_file(geometry_access_path),
        "geometry_guard": geometry_access["guard"],
        "checked_at": utc_now(),
    }
    atomic_json(output_root / "inference_access_audit.json", access)
    complete = (
        manifest.get("complete") is True
        and len(predictions) == len(public)
        and not failures
    )
    existing_completion = (
        load_json(output_root / "inference_complete.json")
        if (output_root / "inference_complete.json").is_file()
        else {}
    )
    completion = {
        "schema_version": 1,
        "complete": complete,
        "backend": backend,
        "grounding": grounding,
        "episode_count": len(predictions),
        "group_failure_count": len(set(failures)),
        "predictions_sha256": digest,
        "private_file_open_count": 0,
        "completed_at": existing_completion.get("completed_at", utc_now())
        if existing_completion.get("predictions_sha256") == digest
        else utc_now(),
    }
    atomic_json(output_root / "inference_complete.json", completion)
    if not complete:
        raise RuntimeError(f"inference incomplete: {backend}/{grounding}")
    return completion


def verify_seal(output_root: Path):
    expected = load_json(output_root / "predictions.sha256.json")
    predictions = output_root / expected["path"]
    if predictions.stat().st_size != expected["size"] or sha256_file(predictions) != expected["sha256"]:
        raise RuntimeError("sealed Gate 7 predictions changed")
    access = load_json(output_root / "inference_access_audit.json")
    if any(
        access[key] != 0
        for key in (
            "private_file_open_count",
            "gt_mask_open_count",
            "gt_bbox_open_count",
            "gt_pose_alignment_open_count",
        )
    ):
        raise RuntimeError("private or diagnostic GT was opened during inference")
    return expected["sha256"]


def _paired_bootstrap(predictions, field, seed):
    by_group = defaultdict(list)
    for item in predictions:
        by_group[item["group_id"]].append(float(item[field]))
    group_values = np.asarray(
        [np.mean(by_group[group_id]) for group_id in sorted(by_group)], dtype=np.float64
    )
    rng = np.random.default_rng(seed)
    draws = rng.choice(
        group_values,
        size=(protocol()["bootstrap_groups"], len(group_values)),
        replace=True,
    ).mean(axis=1)
    return {
        "unit": "group",
        "group_count": len(group_values),
        "samples": protocol()["bootstrap_groups"],
        "seed": seed,
        "point_estimate": float(group_values.mean()),
        "ci95": [float(value) for value in np.quantile(draws, [0.025, 0.975])],
    }


def materialize_sealed_predictions(output_root: Path, private_path: Path):
    digest = verify_seal(output_root)
    predictions = load_jsonl(output_root / "predictions.jsonl")
    private = {item["episode_id"]: item for item in load_jsonl(private_path)}
    if set(private) != {item["episode_id"] for item in predictions}:
        raise ValueError("sealed predictions/private episodes mismatch")
    evaluated = []
    for prediction in predictions:
        oracle = private[prediction["episode_id"]]
        if prediction["route"] == "reobserve":
            stage = (
                "stale_reobserve"
                if oracle["branch"] == "risk_stale"
                else "stable_reobserve"
            )
        else:
            stage = "historical"
        candidate = prediction["candidates"].get(stage)
        if candidate is None:
            raise ValueError(
                f"sealed prediction is missing evaluator-selected stage: {stage}"
            )
        truth = oracle["current_answer_gt"]
        evaluated.append(
            {
                **prediction,
                **candidate,
                "stage": stage,
                "legacy_correct": candidate["legacy_answer"] == truth,
                "contract_valid_correct": candidate["contract_valid_answer"]
                == truth,
                "error": float(candidate["contract_valid_answer"] != truth),
            }
        )
    return digest, evaluated


def evaluate_sealed(output_root: Path, private_path: Path):
    digest, evaluated = materialize_sealed_predictions(output_root, private_path)
    legacy_accuracy = float(np.mean([item["legacy_correct"] for item in evaluated]))
    contract_accuracy = float(np.mean([item["contract_valid_correct"] for item in evaluated]))
    qa_drop = 1.0 - contract_accuracy
    confidence = np.asarray([item["answer_confidence"] for item in evaluated])
    errors = np.asarray([item["error"] for item in evaluated])
    calibration_values = [item["calibrated_error_probability"] for item in evaluated]
    if all(value is not None for value in calibration_values):
        calibrated = np.asarray(calibration_values, dtype=np.float64)
        brier = float(np.mean((calibrated - errors) ** 2)) if np.isfinite(calibrated).all() else None
    else:
        brier = None
    ece = 0.0
    for lower in np.linspace(0, 0.8, 5):
        mask = (confidence >= lower) & (confidence < lower + 0.2 + 1e-12)
        if np.any(mask):
            ece += float(mask.mean()) * abs(float(errors[mask].mean()) - (1.0 - float(confidence[mask].mean())))
    result = {
        "schema_version": 1,
        "backend": evaluated[0]["backend"] if evaluated else None,
        "grounding": evaluated[0]["grounding"] if evaluated else None,
        "episode_count": len(evaluated),
        "group_count": len({item["group_id"] for item in evaluated}),
        "geometry_group_failure_rate": 0.0,
        "legacy_metric": {"accuracy": legacy_accuracy},
        "contract_valid_metric": {"accuracy": contract_accuracy},
        "gt_rgbd_accuracy": 1.0,
        "qa_drop": qa_drop,
        "paired_contract_accuracy": _paired_bootstrap(
            [{**item, "candidate": float(item["contract_valid_correct"])} for item in evaluated],
            "candidate",
            protocol()["seed"] + 11,
        ),
        "calibration": {"brier": brier, "ece": ece},
        "predictions_sha256": digest,
        "private_evaluated_at": utc_now(),
    }
    atomic_json(output_root / "metrics.json", result)
    return result


def calibration_lock(evaluated_predictions):
    bins = []
    for index in range(5):
        lower, upper = index / 5, (index + 1) / 5
        records = [
            item
            for item in evaluated_predictions
            if lower <= item["answer_confidence"] < upper + (1e-12 if index == 4 else 0)
        ]
        error_probability = (
            float(np.mean([item["error"] for item in records])) if records else 1.0 - (lower + upper) / 2
        )
        bins.append(
            {
                "index": index,
                "lower": lower,
                "upper": upper,
                "count": len(records),
                "error_probability": float(np.clip(error_probability, 0.0, 1.0)),
            }
        )
    return {"schema_version": 1, "method": "equal_width_5_bin", "bins": bins}
