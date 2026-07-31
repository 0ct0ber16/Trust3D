"""CPU-side protocol, selection, sealing, and reports for Gate 7 repair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from trust3d.eval.evaluate_gate7_fix import (
    calibration_lock,
    evaluate_sealed,
    materialize_sealed_predictions,
    seal_predictions,
    verify_seal,
)
from trust3d.geometry.camera_contract import (
    camera_to_world_to_world_to_camera,
    opencv_to_opengl_camera,
    planar_answers,
    transform_point,
)
from trust3d.parallel_v2.common import (
    OUTPUT_ROOT,
    ROOT,
    atomic_bytes,
    atomic_json,
    load_json,
    load_jsonl,
    protocol,
    sha256_file,
    stage_complete,
    utc_now,
    verify_baseline,
)


OUTPUT = OUTPUT_ROOT / "gate7_fix"
DATA = ROOT / "data/episodes/parallel_v2/gate7_fix"
CONFIG_PATH = ROOT / "configs/gate7_fix_v1.json"
BACKENDS = ("cut3r", "vggt")
GROUNDINGS = ("center_crop", "rgb_saliency_v1")


def lock():
    verify_baseline()
    config = load_json(CONFIG_PATH)
    diagnosis = load_json(ROOT / "outputs/gate7_diagnosis/final_diagnosis.json")
    decision = load_json(ROOT / "outputs/plan2/final_decision.json")
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "created_at": utc_now(),
        "legacy_task_head": config["task_heads"]["legacy"],
        "contract_valid_task_head": config["task_heads"]["contract_valid"],
        "legacy30_only_for_regression": True,
        "legacy_cut3r_accuracy": decision["cut3r_accuracy"],
        "legacy_vggt_accuracy": decision["vggt_accuracy"],
        "diagnosis_revision": diagnosis["analysis_revision"],
        "diagnosis_sha256": sha256_file(ROOT / "outputs/gate7_diagnosis/final_diagnosis.json"),
        "config_sha256": sha256_file(CONFIG_PATH),
        "baseline_verified": True,
    }
    path = OUTPUT / "protocol_lock.json"
    if path.is_file():
        existing = load_json(path)
        comparable_existing = {
            key: item for key, item in existing.items() if key != "created_at"
        }
        comparable_new = {
            key: item for key, item in value.items() if key != "created_at"
        }
        if comparable_existing != comparable_new:
            raise RuntimeError("immutable Gate 7 protocol lock changed")
        return existing
    atomic_json(path, value)
    return value


def unit():
    tolerance = 1e-6
    cases = 0
    for yaw in (0, 90, 180, 270):
        radians = np.deg2rad(yaw)
        forward = np.asarray([np.sin(radians), 0.0, np.cos(radians)])
        right = np.asarray([np.cos(radians), 0.0, -np.sin(radians)])
        pose = np.eye(4)
        pose[:3, 0] = right
        pose[:3, 2] = forward
        pose[:3, 3] = [0.25, 0.9, -0.5]
        inverse = camera_to_world_to_world_to_camera(pose)
        point = np.asarray([1.25, 1.1, 2.5])
        round_trip = transform_point(pose, transform_point(inverse, point))
        if not np.allclose(round_trip, point, atol=tolerance, rtol=0):
            raise AssertionError("camera round trip failed")
        for right_sign in (-1, 1):
            for forward_sign in (-1, 1):
                target = pose[:3, 3] + right_sign * right + 2 * forward_sign * forward
                donor = pose[:3, 3] - 2 * right + 5 * forward
                answers = planar_answers(target, donor, pose)
                if answers["left_right"] != ("right" if right_sign > 0 else "left"):
                    raise AssertionError("left/right contract failed")
                if answers["front_behind"] != ("front" if forward_sign > 0 else "behind"):
                    raise AssertionError("front/behind contract failed")
                swapped = planar_answers(donor, target, pose)
                if swapped["target_nearer"] == answers["target_nearer"]:
                    raise AssertionError("donor/target swap contract failed")
                cases += 1
    if not np.allclose(opencv_to_opengl_camera([1, 2, 3]), [1, -2, -3]):
        raise AssertionError("OpenCV/OpenGL axis contract failed")
    result = {
        "schema_version": 1,
        "complete": True,
        "balanced_case_count": cases,
        "round_trip_tolerance": tolerance,
        "sim3_main_adapter_forbidden": True,
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT / "unit.json", result)
    return result


def prepare():
    built = load_json(OUTPUT / "prepare.json")
    if built.get("complete") is not True:
        raise RuntimeError("Gate 7 pilot/holdout data are incomplete")
    base = load_json(ROOT / "configs/plan2_vggt.json")
    config = load_json(CONFIG_PATH)
    for grounding in GROUNDINGS:
        value = dict(base)
        value["adapter_version"] = f"gate7-fix-vggt-{grounding}-v1"
        value["grounding"] = grounding
        value["grounding_config"] = config["grounding"]
        atomic_json(OUTPUT / "configs" / f"vggt_{grounding}.json", value)
    manifests = {}
    for split in ("pilot", "holdout"):
        root = DATA / split
        manifests[split] = {
            "public": str(root / "episodes_public.jsonl"),
            "private": str(root / "oracle_private.jsonl"),
            "public_sha256": sha256_file(root / "episodes_public.jsonl"),
            "private_sha256": sha256_file(root / "oracle_private.jsonl"),
            "source_selection_sha256": sha256_file(DATA / f"{split}_source_selection.json"),
            "rgb_sequence_manifest": str(root / "rgb_sequences.json"),
            "rgb_sequence_manifest_sha256": sha256_file(
                root / "rgb_sequences.json"
            ),
        }
    value = {
        "schema_version": 1,
        "complete": True,
        "generated_at": utc_now(),
        "splits": manifests,
        "grounding_candidates": list(GROUNDINGS),
    }
    atomic_json(OUTPUT / "dataset_lock.json", value)
    return value


def _pilot_metrics(backend, grounding):
    root = OUTPUT / "pilot" / f"{backend}_{grounding}"
    return evaluate_sealed(root, DATA / "pilot/oracle_private.jsonl")


def pilot_evaluate():
    existing_lock = OUTPUT / "adapter_lock.json"
    existing_value = load_json(existing_lock) if existing_lock.is_file() else None
    if (
        existing_value is not None
        and (OUTPUT / "confidence_lock.json").is_file()
        and (OUTPUT / "pilot_metrics.json").is_file()
    ):
        value = existing_value
        if sha256_file(OUTPUT / "confidence_lock.json") != value["confidence_lock_sha256"]:
            raise RuntimeError("immutable confidence lock changed")
        return value
    all_metrics = {}
    selected = {}
    evaluated_for_calibration = []
    for backend in BACKENDS:
        candidates = []
        for grounding in GROUNDINGS:
            metrics = _pilot_metrics(backend, grounding)
            all_metrics[f"{backend}/{grounding}"] = metrics
            candidates.append((metrics["qa_drop"], grounding, metrics))
        candidates.sort(key=lambda item: (item[0], 0 if item[1] == "center_crop" else 1))
        qa_drop, grounding, metrics = candidates[0]
        selected[backend] = {
            "grounding": grounding,
            "qa_drop": qa_drop,
            "contract_valid_accuracy": metrics["contract_valid_metric"]["accuracy"],
            "pilot_predictions_sha256": metrics["predictions_sha256"],
        }
        _, evaluated = materialize_sealed_predictions(
            OUTPUT / "pilot" / f"{backend}_{grounding}",
            DATA / "pilot/oracle_private.jsonl",
        )
        evaluated_for_calibration.extend(evaluated)
    confidence = calibration_lock(evaluated_for_calibration)
    confidence.update(
        {
            "protocol_revision": "parallel-v2",
            "pilot_only": True,
            "created_at": utc_now(),
        }
    )
    confidence_path = OUTPUT / "confidence_lock.json"
    if confidence_path.is_file():
        existing_confidence = load_json(confidence_path)
        comparable_existing = {
            key: value
            for key, value in existing_confidence.items()
            if key != "created_at"
        }
        comparable_new = {
            key: value for key, value in confidence.items() if key != "created_at"
        }
        if comparable_existing != comparable_new:
            raise RuntimeError("immutable confidence lock already exists with different content")
        confidence = existing_confidence
    else:
        atomic_json(confidence_path, confidence)
    lock_value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "created_at": utc_now(),
        "selected": selected,
        "task_head": "gate7-planar-yaw-v1",
        "confidence_lock_sha256": sha256_file(OUTPUT / "confidence_lock.json"),
        "pilot_private_sha256": sha256_file(DATA / "pilot/oracle_private.jsonl"),
    }
    if existing_value is not None:
        comparable_existing = {
            key: value for key, value in existing_value.items() if key != "created_at"
        }
        comparable_new = {
            key: value for key, value in lock_value.items() if key != "created_at"
        }
        if comparable_existing != comparable_new:
            raise RuntimeError("immutable adapter lock already exists with different content")
        lock_value = existing_value
    else:
        atomic_json(OUTPUT / "adapter_lock.json", lock_value)
    atomic_json(
        OUTPUT / "pilot_metrics.json",
        {
            "schema_version": 1,
            "metrics": all_metrics,
            "selected": selected,
            "task_head_gain": {
                key: value["contract_valid_metric"]["accuracy"] - value["legacy_metric"]["accuracy"]
                for key, value in all_metrics.items()
            },
            "grounding_gain": {
                backend: all_metrics[f"{backend}/rgb_saliency_v1"]["contract_valid_metric"]["accuracy"]
                - all_metrics[f"{backend}/center_crop"]["contract_valid_metric"]["accuracy"]
                for backend in BACKENDS
            },
        },
    )
    return lock_value


def seal(split, backend, grounding, geometry):
    confidence = load_json(OUTPUT / "confidence_lock.json") if split == "holdout" else None
    return seal_predictions(
        DATA / split / "episodes_public.jsonl",
        OUTPUT / split / "routes.jsonl",
        Path(geometry),
        backend,
        grounding,
        OUTPUT / split / f"{backend}_{grounding}",
        confidence_lock=confidence,
    )


def evaluate_holdout():
    adapter = load_json(OUTPUT / "adapter_lock.json")
    results = {}
    for backend in BACKENDS:
        grounding = adapter["selected"][backend]["grounding"]
        root = OUTPUT / "holdout" / f"{backend}_{grounding}"
        results[backend] = evaluate_sealed(root, DATA / "holdout/oracle_private.jsonl")
    best_backend = max(
        BACKENDS,
        key=lambda backend: results[backend]["contract_valid_metric"]["accuracy"],
    )
    best_drop = results[best_backend]["qa_drop"]
    if best_drop <= 0.10:
        level = "main_result_pass"
    elif best_drop <= 0.20:
        level = "restricted_result_pass"
    else:
        level = "failed_scientific"
    pilot = load_json(OUTPUT / "pilot_metrics.json")
    criteria = {
        "private_open_count_zero": all(
            load_json(OUTPUT / "holdout" / f"{backend}_{adapter['selected'][backend]['grounding']}" / "inference_access_audit.json")["private_file_open_count"] == 0
            for backend in BACKENDS
        ),
        "geometry_failure_rate_zero": all(value["geometry_group_failure_rate"] == 0 for value in results.values()),
        "contract_unit_pass": load_json(OUTPUT / "unit.json")["complete"],
        "legacy_regression_locked": load_json(OUTPUT / "protocol_lock.json")["baseline_verified"],
        "checkpoint_recovery": False,
        "qa_drop_not_above_20pp": best_drop <= 0.20,
    }
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "best_backend": best_backend,
        "best_qa_drop": best_drop,
        "result_level": level,
        "backends": results,
        "criteria": criteria,
        "gain_accounting": {
            "task_head_pilot": pilot["task_head_gain"],
            "grounding_pilot": pilot["grounding_gain"],
            "pose_contract": 0.0,
            "remaining_backbone_gap": {backend: results[backend]["qa_drop"] for backend in BACKENDS},
        },
        "generated_at": utc_now(),
    }
    atomic_json(OUTPUT / "holdout_metrics.json", value)
    return value


def recover():
    adapter = load_json(OUTPUT / "adapter_lock.json")
    records = []
    for backend in BACKENDS:
        grounding = adapter["selected"][backend]["grounding"]
        root = OUTPUT / "holdout" / f"{backend}_{grounding}"
        before = verify_seal(root)
        geometry = OUTPUT / "holdout_geometry" / f"{backend}_{grounding}"
        seal("holdout", backend, grounding, geometry)
        after = verify_seal(root)
        records.append({"backend": backend, "before": before, "after": after, "match": before == after})
    complete = all(item["match"] for item in records)
    value = {
        "schema_version": 1,
        "complete": complete,
        "gpu_loaded": False,
        "records": records,
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT / "checkpoint_recovery.json", value)
    metrics = load_json(OUTPUT / "holdout_metrics.json")
    metrics["criteria"]["checkpoint_recovery"] = complete
    atomic_json(OUTPUT / "holdout_metrics.json", metrics)
    if not complete:
        raise RuntimeError("Gate 7 checkpoint recovery failed")
    return value


def report():
    metrics = load_json(OUTPUT / "holdout_metrics.json")
    recovery = load_json(OUTPUT / "checkpoint_recovery.json")
    criteria = dict(metrics["criteria"])
    criteria["checkpoint_recovery"] = recovery["complete"]
    complete = all(criteria.values())
    status = "complete" if complete else "failed_scientific"
    value = {
        "schema_version": 1,
        "status": status,
        "complete": complete,
        "result_level": metrics["result_level"],
        "best_backend": metrics["best_backend"],
        "best_qa_drop": metrics["best_qa_drop"],
        "criteria": criteria,
        "generated_at": utc_now(),
    }
    atomic_json(OUTPUT / "report.json", value)
    lines = [
        "# Trust3D Gate 7 修复实验报告",
        "",
        f"- 状态：`{status}`",
        f"- 结果等级：`{metrics['result_level']}`",
        f"- 最佳后端：`{metrics['best_backend']}`",
        f"- 相对 GT/RGB-D 的 QA drop：{metrics['best_qa_drop']:.4f}",
        "",
        "## 双后端结果",
        "",
    ]
    for backend, result in metrics["backends"].items():
        lines.append(
            f"- `{backend}`：legacy accuracy={result['legacy_metric']['accuracy']:.4f}，contract-valid accuracy={result['contract_valid_metric']['accuracy']:.4f}，QA drop={result['qa_drop']:.4f}"
        )
    lines.extend(
        [
            "",
            "## 结论边界",
            "",
            "任务头、RGB-only grounding、相机契约和 backbone 误差分别记账。只有 holdout、访问审计、零几何失败和恢复测试全部通过，才允许该结果进入联合 C 线。",
            "",
        ]
    )
    atomic_bytes(ROOT / "Trust3D_Gate7修复实验报告.md", "\n".join(lines).encode("utf-8"))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["lock", "unit", "prepare", "pilot-evaluate", "seal", "evaluate", "recover", "report"])
    parser.add_argument("--split", choices=("pilot", "holdout"))
    parser.add_argument("--backend", choices=BACKENDS)
    parser.add_argument("--grounding", choices=GROUNDINGS)
    parser.add_argument("--geometry", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "seal":
        if not all((args.split, args.backend, args.grounding, args.geometry)):
            parser.error("seal requires --split --backend --grounding --geometry")
        result = seal(args.split, args.backend, args.grounding, args.geometry)
    else:
        functions = {
            "lock": lock,
            "unit": unit,
            "prepare": prepare,
            "pilot-evaluate": pilot_evaluate,
            "evaluate": evaluate_holdout,
            "recover": recover,
            "report": report,
        }
        result = functions[args.mode]()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
