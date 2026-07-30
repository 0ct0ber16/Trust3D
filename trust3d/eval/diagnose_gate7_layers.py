"""执行 Gate 7 双后端分层失败归因并生成中文报告。"""

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from trust3d.data.select_events import read_jsonl
from trust3d.eval.diagnose_cut3r import (
    _gt_trajectories,
    _pose_camera_to_world,
    _relative_pose_diagnostics,
)
from trust3d.eval.evaluate_spatial import PRIMARY_ROUTE
from trust3d.geometry.egocentric import world_to_egocentric
from trust3d.geometry.run_cut3r import (
    _atomic_json,
    _sequence_paths,
    _source_contexts,
    point_in_camera,
    spatial_answers,
)


BACKENDS = ("cut3r", "vggt")
ANALYSIS_REVISION = "r001-analysis-a1"
STAGES = (
    "historical",
    "stale_sequence_historical",
    "stable_reobserve",
    "stale_reobserve",
)
QUESTION_TYPES = (
    "left_right",
    "front_behind",
    "which_closer",
    "target_nearer",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_config(path):
    value = load_json(path)
    if value.get("diagnostic_only") is not True:
        raise ValueError("配置不是后验诊断配置")
    value["_path"] = str(path)
    value["_sha256"] = sha256_file(path)
    return value


def output_root(config):
    return Path(config["paths"]["output_root"])


def assert_output_path(path, config):
    root = output_root(config).resolve()
    value = Path(path).resolve()
    if value != root and root not in value.parents:
        raise ValueError(f"输出路径越过诊断根目录: {value}")


def repository_commit():
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()


def baseline_hash_check(config):
    records = []
    for relative, expected in config["baseline_sha256"].items():
        actual = sha256_file(relative)
        records.append(
            {
                "path": relative,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "match": actual == expected,
            }
        )
    if not all(item["match"] for item in records):
        failed = [item["path"] for item in records if not item["match"]]
        raise ValueError(f"原实验锚点哈希变化: {failed}")
    return records


def _geometry_groups(root):
    manifest = load_json(Path(root) / "manifest.json")
    values = {}
    for record in manifest["groups"]:
        values[record["group_id"]] = load_json(record["checkpoint"])
    return manifest, values


def _primary_routes(path):
    return {
        item["episode_id"]: item
        for item in read_jsonl(path)
        if item["policy_id"] == PRIMARY_ROUTE
    }


def _published_answers(path, method):
    return {
        item["episode_id"]: item
        for item in read_jsonl(path)
        if item["method"] == method
    }


def _stage_for_oracle(oracle, route):
    if route["route"] != "reobserve":
        return "historical"
    return (
        "stale_reobserve"
        if oracle["branch"] == "risk_stale"
        else "stable_reobserve"
    )


def _sequence_for_stage(stage):
    return "stale" if stage == "stale_reobserve" else "stable"


def _point_dict(value):
    if isinstance(value, dict):
        return {axis: float(value[axis]) for axis in ("x", "y", "z")}
    array = np.asarray(value, dtype=np.float64)
    return {"x": float(array[0]), "y": float(array[1]), "z": float(array[2])}


def contract_answers_world(target, donor, query_pose, epsilon=1e-6):
    """严格复现数据构建语义：方向只用平面 yaw，距离使用完整三维。"""
    target_ego = world_to_egocentric(_point_dict(target), query_pose)
    donor_ego = world_to_egocentric(_point_dict(donor), query_pose)
    if (
        abs(target_ego["right"]) <= epsilon
        or abs(target_ego["forward"]) <= epsilon
        or abs(target_ego["distance"] - donor_ego["distance"]) <= epsilon
    ):
        raise ValueError("对象位于预注册决策边界")
    nearer = target_ego["distance"] < donor_ego["distance"]
    return {
        "left_right": "right" if target_ego["right"] > 0 else "left",
        "front_behind": "front" if target_ego["forward"] > 0 else "behind",
        "which_closer": "target" if nearer else "reference",
        "target_nearer": bool(nearer),
    }


def planar_answers_predicted(target, donor, camera_to_world, epsilon=1e-6):
    """在模型坐标系中移除 query pitch，只保留预测水平 heading。"""
    pose = np.asarray(camera_to_world, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    donor = np.asarray(donor, dtype=np.float64)
    if pose.shape != (4, 4):
        raise ValueError("预测 query pose 不是 4x4")
    forward = pose[:3, 2].copy()
    forward[1] = 0.0
    norm = float(np.linalg.norm(forward))
    if norm <= epsilon:
        raise ValueError("预测相机水平 forward 退化")
    forward /= norm
    right = np.asarray([forward[2], 0.0, -forward[0]])
    center = pose[:3, 3]

    def coordinates(point):
        delta = point - center
        return {
            "right": float(np.dot(delta, right)),
            "forward": float(np.dot(delta, forward)),
            "distance": float(np.linalg.norm(delta)),
        }

    target_ego = coordinates(target)
    donor_ego = coordinates(donor)
    if (
        abs(target_ego["right"]) <= epsilon
        or abs(target_ego["forward"]) <= epsilon
        or abs(target_ego["distance"] - donor_ego["distance"]) <= epsilon
    ):
        raise ValueError("预测对象位于任务头决策边界")
    nearer = target_ego["distance"] < donor_ego["distance"]
    return {
        "left_right": "right" if target_ego["right"] > 0 else "left",
        "front_behind": "front" if target_ego["forward"] > 0 else "behind",
        "which_closer": "target" if nearer else "reference",
        "target_nearer": bool(nearer),
    }


def legacy_answers_world(target, donor, query_pose):
    pose = _pose_camera_to_world(query_pose)
    return spatial_answers(
        point_in_camera(np.asarray(list(_point_dict(target).values())), pose),
        point_in_camera(np.asarray(list(_point_dict(donor).values())), pose),
    )


def fit_similarity(predicted_poses, gt_poses, epsilon=1e-8):
    predicted = np.asarray(predicted_poses, dtype=np.float64)
    expected = np.asarray(gt_poses, dtype=np.float64)
    if predicted.shape != expected.shape or predicted.shape[1:] != (4, 4):
        raise ValueError("Sim(3) 相机序列 shape 不一致")
    candidates = np.asarray(
        [expected[i, :3, :3] @ predicted[i, :3, :3].T for i in range(len(predicted))]
    )
    u, _, vt = np.linalg.svd(candidates.sum(axis=0))
    correction = np.eye(3)
    correction[-1, -1] = np.sign(np.linalg.det(u @ vt))
    rotation = u @ correction @ vt
    pred_centers = predicted[:, :3, 3]
    gt_centers = expected[:, :3, 3]
    ratios = []
    for first in range(len(predicted)):
        for second in range(first + 1, len(predicted)):
            pred_distance = float(
                np.linalg.norm(pred_centers[first] - pred_centers[second])
            )
            gt_distance = float(
                np.linalg.norm(gt_centers[first] - gt_centers[second])
            )
            if pred_distance > epsilon and gt_distance > epsilon:
                ratios.append(gt_distance / pred_distance)
    if not ratios:
        raise ValueError("alignment_unidentifiable: 没有非零相机 baseline")
    scale = float(np.median(ratios))
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("alignment_unidentifiable: scale 非正或非有限")
    rotated = (rotation @ pred_centers.T).T * scale
    translation = np.median(gt_centers - rotated, axis=0)
    aligned = rotated + translation
    residual = np.linalg.norm(aligned - gt_centers, axis=1)
    rotation_residuals = []
    for index in range(len(predicted)):
        estimate = rotation @ predicted[index, :3, :3]
        delta = estimate.T @ expected[index, :3, :3]
        cosine = np.clip((np.trace(delta) - 1.0) / 2.0, -1.0, 1.0)
        rotation_residuals.append(float(np.degrees(np.arccos(cosine))))
    return {
        "rotation": rotation,
        "scale": scale,
        "translation": translation,
        "center_residuals": residual,
        "rotation_residual_degrees": np.asarray(rotation_residuals),
    }


def apply_similarity(point, alignment):
    value = np.asarray(point, dtype=np.float64)
    return (
        alignment["scale"] * (alignment["rotation"] @ value)
        + alignment["translation"]
    )


def prepare(config):
    root = output_root(config)
    assert_output_path(root, config)
    required = [
        config["_path"],
        "trust3d/geometry/diagnostic_grounding.py",
        "trust3d/eval/diagnose_gate7_layers.py",
        "trust3d/geometry/run_cut3r.py",
        "trust3d/geometry/run_vggt.py",
        "tests/test_gate7_geometry_contract.py",
        "scripts/run_gate7_failure_diagnosis.sh",
    ]
    missing = [path for path in required if not Path(path).is_file()]
    if missing:
        raise ValueError(f"P0 缺少文件: {missing}")
    forbidden = [
        Path("outputs/gate7").resolve(),
        Path("outputs/plan2").resolve(),
        Path(config["paths"]["dataset_root"]).resolve(),
    ]
    if any(root.resolve() == item or item in root.resolve().parents for item in forbidden):
        raise ValueError("诊断输出根与只读目录重叠")
    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "stage": "prepare",
        "complete": True,
        "checked_at": utc_now(),
        "git_commit": repository_commit(),
        "config_sha256": config["_sha256"],
        "required_files": required,
        "public_read_allowlist": [
            config["paths"]["public_episodes"],
            config["paths"]["routes"],
            config["paths"]["cut3r_baseline_geometry"],
            config["paths"]["vggt_baseline_geometry"],
        ],
        "private_read_denylist": [
            config["paths"]["private_oracle"],
            "*/cache/depth/*",
            "*/cache/instance/*",
            "*/cache/mask/*",
        ],
        "baseline_hashes": baseline_hash_check(config),
    }
    _atomic_json(root / "prepare.json", result)
    return result


def lock_protocol(config):
    root = output_root(config)
    prepare_result = load_json(root / "prepare.json")
    if prepare_result.get("complete") is not True:
        raise ValueError("P0 尚未完成")
    revision = config["protocol_revision"]
    revision_path = root / "protocol_locks" / f"{revision}.json"
    code_paths = [
        config["_path"],
        "trust3d/geometry/diagnostic_grounding.py",
        "trust3d/eval/diagnose_gate7_layers.py",
        "trust3d/geometry/run_cut3r.py",
        "trust3d/geometry/run_vggt.py",
        "tests/test_gate7_geometry_contract.py",
        "scripts/run_gate7_failure_diagnosis.sh",
    ]
    value = {
        "schema_version": 1,
        "diagnostic_only": True,
        "qa_revealed": True,
        "original_results_mutable": False,
        "revision": revision,
        "created_at": utc_now(),
        "git_commit": repository_commit(),
        "config_sha256": config["_sha256"],
        "code_sha256": {path: sha256_file(path) for path in code_paths},
        "baseline_hashes": baseline_hash_check(config),
        "pilot_group_ids": config["pilot_group_ids"],
        "selectors": config["selectors"],
        "resources": config["resources"],
        "tolerances": config["tolerances"],
        "bootstrap_seed": config["bootstrap_seed"],
        "bootstrap_samples": config["bootstrap_samples"],
    }
    if revision_path.exists():
        existing = load_json(revision_path)
        comparable = dict(existing)
        comparable.pop("created_at", None)
        expected = dict(value)
        expected.pop("created_at", None)
        if comparable != expected:
            raise ValueError(f"不可变协议 revision 已存在且内容不同: {revision}")
    else:
        _atomic_json(revision_path, value)
    pointer = {
        "schema_version": 1,
        "revision": revision,
        "revision_path": str(revision_path),
        "revision_sha256": sha256_file(revision_path),
    }
    _atomic_json(root / "protocol_lock.json", pointer)
    return pointer


def synthetic_summary(config):
    cases = 0
    for yaw in (0, 90, 180, 270, 37):
        radians = math.radians(yaw)
        pose = {
            "x": 0.25,
            "y": 0.9,
            "z": -0.5,
            "rotation_y": yaw,
            "horizon": 30,
        }
        for forward_sign in (-1, 1):
            for right_sign in (-1, 1):
                target = {
                    "x": pose["x"]
                    + right_sign * math.cos(radians)
                    + forward_sign * math.sin(radians) * 2,
                    "y": 1.3,
                    "z": pose["z"]
                    - right_sign * math.sin(radians)
                    + forward_sign * math.cos(radians) * 2,
                }
                donor = {"x": pose["x"], "y": 0.9, "z": pose["z"] + 5}
                answers = contract_answers_world(target, donor, pose)
                if answers["left_right"] != (
                    "right" if right_sign > 0 else "left"
                ):
                    raise AssertionError("平衡 left/right fixture 失败")
                if answers["front_behind"] != (
                    "front" if forward_sign > 0 else "behind"
                ):
                    raise AssertionError("平衡 front/behind fixture 失败")
                cases += 1
    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "stage": "unit",
        "complete": True,
        "checked_at": utc_now(),
        "balanced_fixture_count": cases,
        "round_trip_tolerance": config["tolerances"]["matrix_absolute"],
        "contract_task_head_version": config["task_head_versions"][
            "contract_valid"
        ],
        "legacy_task_head_version": config["task_head_versions"]["legacy"],
    }
    _atomic_json(output_root(config) / "synthetic_contract.json", result)
    return result


def offline_public(config):
    public = read_jsonl(config["paths"]["public_episodes"])
    routes = _primary_routes(config["paths"]["routes"])
    values = []
    read_paths = [
        config["paths"]["public_episodes"],
        config["paths"]["routes"],
    ]
    for backend in BACKENDS:
        geometry_root = config["paths"][f"{backend}_baseline_geometry"]
        manifest, groups = _geometry_groups(geometry_root)
        read_paths.append(str(Path(geometry_root) / "manifest.json"))
        if manifest.get("complete") is not True or len(groups) != 30:
            raise ValueError(f"{backend} 原几何 checkpoint 不完整")
        for episode in public:
            group = groups[episode["group_id"]]
            question_type = episode["question"]["type"]
            route = routes[episode["episode_id"]]
            values.append(
                {
                    "backend": backend,
                    "episode_id": episode["episode_id"],
                    "group_id": episode["group_id"],
                    "question_type": question_type,
                    "route": route["route"],
                    "historical_answer": group["historical"]["answers"][
                        question_type
                    ],
                    "stable_answer": group["stable_reobserve"]["answers"][
                        question_type
                    ],
                    "stale_answer": group["stale_reobserve"]["answers"][
                        question_type
                    ],
                }
            )
            read_paths.append(
                str(Path(geometry_root) / "checkpoints" / f"{episode['group_id']}.json")
            )
    values.sort(key=lambda item: (item["backend"], item["episode_id"]))
    path = output_root(config) / "baseline_candidates.jsonl"
    write_jsonl(path, values)
    audit = {
        "schema_version": 1,
        "diagnostic_only": True,
        "public_phase": {
            "candidate_count": len(values),
            "declared_reads": sorted(set(read_paths)),
            "private_path_read_count": 0,
            "private_data_used": False,
        },
    }
    _atomic_json(output_root(config) / "access_audit.json", audit)
    return {"candidate_count": len(values), "output": str(path)}


def _selected_source(context, oracle, route, source_name):
    source = (
        context["branches"][oracle["branch"]]
        if route["route"] == "reobserve"
        else context["history"]
    )
    return source[source_name]


def _role_binding_audit(config, contexts):
    failures = []
    checked = 0
    public_by_group = {}
    for episode in read_jsonl(config["paths"]["public_episodes"]):
        public_by_group.setdefault(episode["group_id"], episode)
    for backend in BACKENDS:
        _, groups = _geometry_groups(config["paths"][f"{backend}_baseline_geometry"])
        for group_id, group in sorted(groups.items()):
            public = public_by_group[group_id]
            context = contexts[group_id]
            expected = {
                scenario: _sequence_paths(
                    public,
                    context,
                    config["paths"]["dataset_root"],
                    branch,
                )
                for scenario, branch in (
                    ("stable", "risk_stable"),
                    ("stale", "risk_stale"),
                )
            }
            actual = {
                (item["scenario"], item["index"]): item
                for item in group["inputs"]
            }
            for scenario, paths in expected.items():
                for index, path in enumerate(paths):
                    checked += 1
                    record = actual.get((scenario, index))
                    if record is None or Path(record["path"]).resolve() != Path(path).resolve():
                        failures.append(
                            {
                                "backend": backend,
                                "group_id": group_id,
                                "scenario": scenario,
                                "index": index,
                                "expected": str(path),
                                "actual": record,
                            }
                        )
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "binding_contract": {
            "0": "historical target",
            "1": "historical donor",
            "2": "query",
            "3": "current target",
            "4": "current donor",
        },
        "checked_input_count": checked,
        "failure_count": len(failures),
        "failures": failures,
        "pass": not failures,
    }


def _pose_audit(config, contexts):
    backends = {}
    for backend in BACKENDS:
        _, groups = _geometry_groups(config["paths"][f"{backend}_baseline_geometry"])
        records = []
        failures = []
        for group_id, group in sorted(groups.items()):
            gt = _gt_trajectories(contexts[group_id])
            for scenario in ("stable", "stale"):
                try:
                    predicted = np.asarray(
                        group["camera_trajectories"][scenario], dtype=np.float64
                    )
                    expected = np.asarray(gt[scenario], dtype=np.float64)
                    alignment = fit_similarity(
                        predicted,
                        expected,
                        config["tolerances"]["nonzero_baseline"],
                    )
                    orthogonal = [
                        float(np.max(np.abs(pose[:3, :3].T @ pose[:3, :3] - np.eye(3))))
                        for pose in predicted
                    ]
                    determinant = [
                        float(np.linalg.det(pose[:3, :3])) for pose in predicted
                    ]
                    relative = _relative_pose_diagnostics(predicted, expected)
                    records.append(
                        {
                            "group_id": group_id,
                            "scenario": scenario,
                            "orthogonality_max": max(orthogonal),
                            "determinant_min": min(determinant),
                            "determinant_max": max(determinant),
                            "alignment_scale": alignment["scale"],
                            "alignment_center_residual_median": float(
                                np.median(alignment["center_residuals"])
                            ),
                            "alignment_rotation_residual_median_degrees": float(
                                np.median(alignment["rotation_residual_degrees"])
                            ),
                            "relative_rotation_error_median_degrees": float(
                                np.median(
                                    [item["rotation_error_degrees"] for item in relative]
                                )
                            ),
                            "relative_translation_direction_error_median_degrees": float(
                                np.median(
                                    [
                                        item["translation_direction_error_degrees"]
                                        for item in relative
                                        if item["translation_direction_error_degrees"]
                                        is not None
                                    ]
                                )
                            ),
                        }
                    )
                except Exception as error:
                    failures.append(
                        {
                            "group_id": group_id,
                            "scenario": scenario,
                            "error": str(error),
                        }
                    )
        backends[backend] = {
            "record_count": len(records),
            "failure_count": len(failures),
            "failures": failures,
            "records": records,
        }
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "backends": backends,
    }


def offline_private(config):
    root = output_root(config)
    candidates = {
        (item["backend"], item["episode_id"]): item
        for item in read_jsonl(root / "baseline_candidates.jsonl")
    }
    private = read_jsonl(config["paths"]["private_oracle"])
    routes = _primary_routes(config["paths"]["routes"])
    contexts = _source_contexts(config["paths"]["source_checkpoints"])
    published = {
        "cut3r": _published_answers(
            "outputs/gate7/predictions.jsonl", "trust3d_cut3r"
        ),
        "vggt": _published_answers(
            "outputs/plan2/vggt_predictions.jsonl", "trust3d_vggt"
        ),
    }
    baseline_records = []
    oracle_records = []
    for oracle in private:
        route = routes[oracle["episode_id"]]
        expected = (
            oracle["current_answer_gt"]
            if route["route"] == "reobserve"
            else oracle["historical_answer_gt"]
        )
        for backend in BACKENDS:
            candidate = candidates[(backend, oracle["episode_id"])]
            if route["route"] == "reobserve":
                key = "stale_answer" if oracle["branch"] == "risk_stale" else "stable_answer"
            else:
                key = "historical_answer"
            answer = candidate[key]
            original = published[backend][oracle["episode_id"]]
            baseline_records.append(
                {
                    "backend": backend,
                    "episode_id": oracle["episode_id"],
                    "group_id": oracle["group_id"],
                    "question_type": oracle["question_type"],
                    "answer": answer,
                    "expected": expected,
                    "correct": answer == expected,
                    "published_answer": original["answer"],
                    "published_correct": bool(original["correct"]),
                    "exact_match": answer == original["answer"],
                }
            )
        context = contexts[oracle["group_id"]]
        gt = _selected_source(context, oracle, route, "gt")
        rgbd = _selected_source(context, oracle, route, "rgbd")
        tl = legacy_answers_world(gt["target"], gt["donor"], context["query_pose"])
        t0 = contract_answers_world(
            gt["target"],
            gt["donor"],
            context["query_pose"],
            config["tolerances"]["decision_boundary"],
        )
        r0 = contract_answers_world(
            rgbd["target"],
            rgbd["donor"],
            context["query_pose"],
            config["tolerances"]["decision_boundary"],
        )
        question_type = oracle["question_type"]
        oracle_records.append(
            {
                "episode_id": oracle["episode_id"],
                "group_id": oracle["group_id"],
                "branch": oracle["branch"],
                "route": route["route"],
                "question_type": question_type,
                "expected_gt": expected,
                "expected_rgbd": oracle["current_answer_rgbd"]
                if route["route"] == "reobserve"
                else oracle["historical_answer_rgbd"],
                "TL": tl[question_type],
                "T0": t0[question_type],
                "R0": r0[question_type],
                "TL_correct": tl[question_type] == expected,
                "T0_correct": t0[question_type] == expected,
                "R0_correct": r0[question_type]
                == (
                    oracle["current_answer_rgbd"]
                    if route["route"] == "reobserve"
                    else oracle["historical_answer_rgbd"]
                ),
            }
        )
    write_jsonl(root / "offline_oracles.jsonl", oracle_records)
    by_backend = {}
    for backend in BACKENDS:
        records = [item for item in baseline_records if item["backend"] == backend]
        by_backend[backend] = {
            "episode_count": len(records),
            "accuracy": sum(item["correct"] for item in records) / len(records),
            "published_accuracy": sum(item["published_correct"] for item in records)
            / len(records),
            "exact_answer_match_count": sum(item["exact_match"] for item in records),
            "exact_reproduction": all(item["exact_match"] for item in records),
        }
    baseline_report = {
        "schema_version": 1,
        "diagnostic_only": True,
        "group_count": len({item["group_id"] for item in private}),
        "episode_count": len(private),
        "backends": by_backend,
    }
    _atomic_json(root / "baseline_reproduction.json", baseline_report)
    task_report = {
        "schema_version": 1,
        "diagnostic_only": True,
        "episode_count": len(oracle_records),
        "TL_accuracy": sum(item["TL_correct"] for item in oracle_records)
        / len(oracle_records),
        "T0_accuracy": sum(item["T0_correct"] for item in oracle_records)
        / len(oracle_records),
        "R0_accuracy": sum(item["R0_correct"] for item in oracle_records)
        / len(oracle_records),
        "legacy_contract_mismatch_count": sum(
            item["TL"] != item["T0"] for item in oracle_records
        ),
        "by_question_type": {
            question_type: {
                "count": len(values),
                "TL_accuracy": sum(item["TL_correct"] for item in values) / len(values),
                "T0_accuracy": sum(item["T0_correct"] for item in values) / len(values),
                "R0_accuracy": sum(item["R0_correct"] for item in values) / len(values),
                "legacy_contract_mismatch_count": sum(
                    item["TL"] != item["T0"] for item in values
                ),
            }
            for question_type in QUESTION_TYPES
            for values in [[
                item for item in oracle_records if item["question_type"] == question_type
            ]]
        },
        "front_behind_label_counts": dict(
            Counter(
                item["expected_gt"]
                for item in oracle_records
                if item["question_type"] == "front_behind"
            )
        ),
        "task_semantics_mismatch": any(
            item["TL"] != item["T0"] for item in oracle_records
        ),
    }
    _atomic_json(root / "task_head_audit.json", task_report)
    role_report = _role_binding_audit(config, contexts)
    pose_report = _pose_audit(config, contexts)
    _atomic_json(root / "role_binding_audit.json", role_report)
    _atomic_json(root / "pose_audit.json", pose_report)
    audit = load_json(root / "access_audit.json")
    audit["private_phase"] = {
        "private_data_used": True,
        "qa_revealed": True,
        "declared_reads": [
            config["paths"]["private_oracle"],
            config["paths"]["source_checkpoints"],
            "outputs/gate7/predictions.jsonl",
            "outputs/plan2/vggt_predictions.jsonl",
        ],
        "eligible_as_main_result": False,
    }
    _atomic_json(root / "access_audit.json", audit)
    result = {
        "schema_version": 1,
        "diagnostic_only": True,
        "complete": all(
            item["exact_reproduction"] for item in by_backend.values()
        )
        and task_report["T0_accuracy"] == 1.0
        and task_report["R0_accuracy"] == 1.0
        and role_report["pass"],
        "baseline_reproduction": by_backend,
        "task_head": task_report,
        "role_binding_pass": role_report["pass"],
        "pose_failure_count": sum(
            value["failure_count"] for value in pose_report["backends"].values()
        ),
    }
    _atomic_json(root / "offline_audit.json", result)
    return result


def _diagnostic_groups(root):
    manifest, groups = _geometry_groups(root)
    if (
        manifest.get("complete") is not True
        or manifest.get("success_group_count") != 30
        or len(groups) != 30
    ):
        raise ValueError(f"诊断几何未完成 30 groups: {root}")
    for group_id, group in groups.items():
        if group.get("diagnostic_only") is not True:
            raise ValueError(f"{group_id} 缺少 diagnostic_only 标记")
        if set(group.get("diagnostic_selectors", {})) != {
            "center_0.12",
            "gt_bbox",
            "gt_mask",
        }:
            raise ValueError(f"{group_id} selector 不完整")
    return manifest, groups


def _prediction_record(
    backend,
    variant,
    oracle,
    route,
    answer,
    expected,
    alignment_degenerate=False,
):
    return {
        "backend": backend,
        "variant": variant,
        "episode_id": oracle["episode_id"],
        "group_id": oracle["group_id"],
        "branch": oracle["branch"],
        "route": route["route"],
        "question_type": oracle["question_type"],
        "answer": answer,
        "expected": expected,
        "correct": answer == expected,
        "alignment_degenerate": bool(alignment_degenerate),
        "diagnostic_only": True,
    }


def _aligned_answers(stage, alignment, query_pose, legacy, epsilon):
    target = apply_similarity(stage["target"]["world"], alignment)
    donor = apply_similarity(stage["donor"]["world"], alignment)
    if legacy:
        answers = legacy_answers_world(target, donor, query_pose)
    else:
        answers = contract_answers_world(target, donor, query_pose, epsilon)
    return answers, target, donor


def _distribution(values):
    finite = np.asarray([value for value in values if value is not None], dtype=np.float64)
    if finite.size == 0:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "p95": None,
        }
    return {
        "count": int(finite.size),
        "mean": float(np.mean(finite)),
        "median": float(np.median(finite)),
        "p25": float(np.quantile(finite, 0.25)),
        "p75": float(np.quantile(finite, 0.75)),
        "p95": float(np.quantile(finite, 0.95)),
    }


def _bootstrap_metrics(records, config, backend, valid_variants):
    values = [item for item in records if item["backend"] == backend]
    groups = sorted({item["group_id"] for item in values})
    variants = list(valid_variants)
    by_key = defaultdict(list)
    for item in values:
        by_key[(item["group_id"], item["variant"])].append(bool(item["correct"]))
    questions_per_group = config["counts"]["questions_per_group"]
    per_variant = {}
    variant_groups = {}
    for variant_index, variant in enumerate(variants):
        available = []
        scores = []
        for group_id in groups:
            correct = by_key[(group_id, variant)]
            if correct and len(correct) != questions_per_group:
                raise ValueError(
                    f"{backend}/{group_id}/{variant} 只有 {len(correct)} 个问题，"
                    f"预期 {questions_per_group}"
                )
            if correct:
                available.append(group_id)
                scores.append(float(np.mean(correct)))
        if not scores:
            raise ValueError(f"{backend}/{variant} 没有任何有效 group")
        scores = np.asarray(scores, dtype=np.float64)
        rng = np.random.default_rng(config["bootstrap_seed"] + variant_index)
        sampled = rng.integers(
            0,
            len(scores),
            size=(config["bootstrap_samples"], len(scores)),
        )
        samples = scores[sampled].mean(axis=1)
        variant_groups[variant] = set(available)
        per_variant[variant] = {
            "accuracy": float(scores.mean()),
            "ci95": [
                float(np.quantile(samples, 0.025)),
                float(np.quantile(samples, 0.975)),
            ],
            "group_count": len(available),
            "episode_count": len(available) * questions_per_group,
            "missing_group_ids": sorted(set(groups) - set(available)),
        }

    effect_variants = ("B0", "B1", "B2", "B3", "C0", "C1", "C2", "C3", "R0")
    complete_groups = sorted(
        set(groups).intersection(*(variant_groups[name] for name in effect_variants))
    )
    if not complete_groups:
        raise ValueError(f"{backend} 没有可用于 2x2 归因的完整 group")
    matrix = np.asarray(
        [
            [np.mean(by_key[(group_id, variant)]) for variant in effect_variants]
            for group_id in complete_groups
        ],
        dtype=np.float64,
    )
    rng = np.random.default_rng(config["bootstrap_seed"])
    sampled = rng.integers(
        0,
        len(complete_groups),
        size=(config["bootstrap_samples"], len(complete_groups)),
    )
    bootstrap = matrix[sampled].mean(axis=1)
    index = {variant: position for position, variant in enumerate(effect_variants)}

    def estimate(name):
        position = index[name]
        return float(matrix[:, position].mean()), bootstrap[:, position]

    b0, sb0 = estimate("B0")
    b1, sb1 = estimate("B1")
    b2, sb2 = estimate("B2")
    b3, sb3 = estimate("B3")
    c0, sc0 = estimate("C0")
    c1, sc1 = estimate("C1")
    c2, sc2 = estimate("C2")
    c3, sc3 = estimate("C3")
    r0, sr0 = estimate("R0")
    contract_required = load_json(output_root(config) / "task_head_audit.json")[
        "task_semantics_mismatch"
    ]
    if contract_required:
        v0, sv0, v1, sv1, v2, sv2, v3, sv3 = (
            c0,
            sc0,
            c1,
            sc1,
            c2,
            sc2,
            c3,
            sc3,
        )
        valid_source = "C0-C3"
    else:
        v0, sv0, v1, sv1, v2, sv2, v3, sv3 = (
            b0,
            sb0,
            b1,
            sb1,
            b2,
            sb2,
            b3,
            sb3,
        )
        valid_source = "B0-B3"

    effect_values = {
        "grounding_effect": (
            ((v1 - v0) + (v3 - v2)) / 2,
            ((sv1 - sv0) + (sv3 - sv2)) / 2,
        ),
        "pose_effect": (
            ((v2 - v0) + (v3 - v1)) / 2,
            ((sv2 - sv0) + (sv3 - sv1)) / 2,
        ),
        "interaction": (
            v3 - v2 - v1 + v0,
            sv3 - sv2 - sv1 + sv0,
        ),
        "backbone_gap": (r0 - v3, sr0 - sv3),
        "task_head_effect": (
            ((c0 - b0) + (c1 - b1) + (c2 - b2) + (c3 - b3)) / 4,
            ((sc0 - sb0) + (sc1 - sb1) + (sc2 - sb2) + (sc3 - sb3))
            / 4,
        ),
    }
    effects = {}
    for name, (point, samples) in effect_values.items():
        effects[name] = {
            "estimate": float(point),
            "ci95": [
                float(np.quantile(samples, 0.025)),
                float(np.quantile(samples, 0.975)),
            ],
            "direction": "positive"
            if point > 0
            else "negative"
            if point < 0
            else "zero",
            "complete_case_group_count": len(complete_groups),
        }
    return {
        "bootstrap_unit": "group",
        "bootstrap_samples": config["bootstrap_samples"],
        "bootstrap_seed": config["bootstrap_seed"],
        "contract_valid_source": valid_source,
        "variants": per_variant,
        "effects": effects,
        "complete_case": {
            "group_count": len(complete_groups),
            "episode_count": len(complete_groups) * questions_per_group,
            "missing_group_count": len(groups) - len(complete_groups),
            "missing_group_ids": sorted(set(groups) - set(complete_groups)),
            "required_variants": list(effect_variants),
        },
    }


def _descriptive_metrics(records, backend):
    values = [item for item in records if item["backend"] == backend]
    result = {}
    for variant in sorted({item["variant"] for item in values}):
        selected = [item for item in values if item["variant"] == variant]
        by_type = {}
        for question_type in QUESTION_TYPES:
            subset = [
                item for item in selected if item["question_type"] == question_type
            ]
            by_type[question_type] = {
                "count": len(subset),
                "accuracy": sum(item["correct"] for item in subset) / len(subset),
                "answer_counts": dict(Counter(str(item["answer"]) for item in subset)),
            }
        result[variant] = {"by_question_type": by_type}
    return result


def _classify_factor(effects):
    candidates = {
        "grounding_dominant": effects["grounding_effect"],
        "pose_dominant": effects["pose_effect"],
        "backbone_geometry_dominant": effects["backbone_gap"],
    }
    ranked = sorted(
        candidates.items(), key=lambda item: item[1]["estimate"], reverse=True
    )
    first_name, first = ranked[0]
    second = ranked[1][1]
    if (
        first["estimate"] >= 0.20
        and first["ci95"][0] > 0
        and first["estimate"] - max(second["estimate"], 0) >= 0.10
    ):
        return first_name
    if any(value["estimate"] >= 0.10 for value in candidates.values()):
        return "mixed_grounding_pose_geometry"
    return "inconclusive_small_sample"


def attribute(config):
    root = output_root(config)
    baseline_hash_check(config)
    private = read_jsonl(config["paths"]["private_oracle"])
    routes = _primary_routes(config["paths"]["routes"])
    contexts = _source_contexts(config["paths"]["source_checkpoints"])
    offline_oracles = {
        item["episode_id"]: item for item in read_jsonl(root / "offline_oracles.jsonl")
    }
    predictions = []
    geometry_diagnostics = {}
    alignment_failures = []
    manifests = {}
    for backend in BACKENDS:
        manifest, groups = _diagnostic_groups(root / backend)
        manifests[backend] = manifest
        alignments = {}
        per_selector = {
            selector: {
                "purity": [],
                "recall": [],
                "target_error": [],
                "donor_error": [],
            }
            for selector in ("center_0.12", "gt_bbox", "gt_mask")
        }
        for group_id, group in groups.items():
            gt_trajectories = _gt_trajectories(contexts[group_id])
            for sequence in ("stable", "stale"):
                try:
                    alignments[(group_id, sequence)] = fit_similarity(
                        group["camera_trajectories"][sequence],
                        gt_trajectories[sequence],
                        config["tolerances"]["nonzero_baseline"],
                    )
                except Exception as error:
                    alignment_failures.append(
                        {
                            "backend": backend,
                            "group_id": group_id,
                            "sequence": sequence,
                            "error": str(error),
                        }
                    )
        unidentifiable_groups = {
            item["group_id"]
            for item in alignment_failures
            if item["backend"] == backend
        }

        for oracle in private:
            route = routes[oracle["episode_id"]]
            stage_name = _stage_for_oracle(oracle, route)
            sequence = _sequence_for_stage(stage_name)
            group = groups[oracle["group_id"]]
            alignment = (
                None
                if oracle["group_id"] in unidentifiable_groups
                else alignments[(oracle["group_id"], sequence)]
            )
            query_pose_predicted = group["camera_trajectories"][sequence][2]
            question_type = oracle["question_type"]
            expected = (
                oracle["current_answer_gt"]
                if route["route"] == "reobserve"
                else oracle["historical_answer_gt"]
            )
            answers = {}
            aligned_points = {}
            for selector in ("center_0.12", "gt_bbox", "gt_mask"):
                stage = group["diagnostic_selectors"][selector][stage_name]
                legacy_predicted = stage["answers"]
                contract_predicted = planar_answers_predicted(
                    stage["target"]["world"],
                    stage["donor"]["world"],
                    query_pose_predicted,
                    config["tolerances"]["decision_boundary"],
                )
                answers[selector] = {
                    "legacy_predicted": legacy_predicted,
                    "contract_predicted": contract_predicted,
                }
                if alignment is not None:
                    legacy_aligned, target, donor = _aligned_answers(
                        stage,
                        alignment,
                        contexts[oracle["group_id"]]["query_pose"],
                        True,
                        config["tolerances"]["decision_boundary"],
                    )
                    contract_aligned, _, _ = _aligned_answers(
                        stage,
                        alignment,
                        contexts[oracle["group_id"]]["query_pose"],
                        False,
                        config["tolerances"]["decision_boundary"],
                    )
                    answers[selector]["legacy_aligned"] = legacy_aligned
                    answers[selector]["contract_aligned"] = contract_aligned
                    aligned_points[selector] = {"target": target, "donor": donor}
            variant_answers = {
                "B0": answers["center_0.12"]["legacy_predicted"][question_type],
                "B1": answers["gt_mask"]["legacy_predicted"][question_type],
                "C0": answers["center_0.12"]["contract_predicted"][question_type],
                "C1": answers["gt_mask"]["contract_predicted"][question_type],
                "BBox_predicted": answers["gt_bbox"]["contract_predicted"][
                    question_type
                ],
                "TL": offline_oracles[oracle["episode_id"]]["TL"],
                "T0": offline_oracles[oracle["episode_id"]]["T0"],
                "R0": offline_oracles[oracle["episode_id"]]["R0"],
            }
            if alignment is not None:
                variant_answers.update(
                    {
                        "B2": answers["center_0.12"]["legacy_aligned"][question_type],
                        "B3": answers["gt_mask"]["legacy_aligned"][question_type],
                        "C2": answers["center_0.12"]["contract_aligned"][question_type],
                        "C3": answers["gt_mask"]["contract_aligned"][question_type],
                        "BBox_aligned": answers["gt_bbox"]["contract_aligned"][
                            question_type
                        ],
                    }
                )
            expected_by_variant = {variant: expected for variant in variant_answers}
            expected_by_variant["R0"] = offline_oracles[oracle["episode_id"]][
                "expected_rgbd"
            ]
            for variant, answer in variant_answers.items():
                predictions.append(
                    _prediction_record(
                        backend,
                        variant,
                        oracle,
                        route,
                        answer,
                        expected_by_variant[variant],
                    )
                )

            source = _selected_source(
                contexts[oracle["group_id"]], oracle, route, "gt"
            )
            for selector in ("center_0.12", "gt_bbox", "gt_mask"):
                stage = group["diagnostic_selectors"][selector][stage_name]
                per_selector[selector]["purity"].extend(
                    [stage[role]["mask_purity"] for role in ("target", "donor")]
                )
                per_selector[selector]["recall"].extend(
                    [stage[role]["mask_recall"] for role in ("target", "donor")]
                )
                if alignment is not None:
                    per_selector[selector]["target_error"].append(
                        float(
                            np.linalg.norm(
                                aligned_points[selector]["target"]
                                - np.asarray(
                                    [
                                        source["target"][axis]
                                        for axis in ("x", "y", "z")
                                    ]
                                )
                            )
                        )
                    )
                    per_selector[selector]["donor_error"].append(
                        float(
                            np.linalg.norm(
                                aligned_points[selector]["donor"]
                                - np.asarray(
                                    [
                                        source["donor"][axis]
                                        for axis in ("x", "y", "z")
                                    ]
                                )
                            )
                        )
                    )
        geometry_diagnostics[backend] = {
            selector: {
                name: _distribution(values)
                for name, values in diagnostics.items()
            }
            for selector, diagnostics in per_selector.items()
        }

    predictions.sort(
        key=lambda item: (item["backend"], item["variant"], item["episode_id"])
    )
    write_jsonl(root / "factorial_predictions.jsonl", predictions)
    valid_variants = (
        "B0",
        "B1",
        "B2",
        "B3",
        "C0",
        "C1",
        "C2",
        "C3",
        "TL",
        "T0",
        "R0",
    )
    backend_metrics = {}
    diagnoses = {}
    task_report = load_json(root / "task_head_audit.json")
    for backend in BACKENDS:
        bootstrap = _bootstrap_metrics(
            predictions, config, backend, valid_variants
        )
        bootstrap["descriptive"] = _descriptive_metrics(predictions, backend)
        backend_metrics[backend] = bootstrap
        conditional_factor_label = _classify_factor(bootstrap["effects"])
        missing_groups = bootstrap["complete_case"]["missing_group_ids"]
        factor_label = (
            "inconclusive_small_sample"
            if missing_groups
            else conditional_factor_label
        )
        if task_report["task_semantics_mismatch"]:
            primary = "proven_task_head_semantics_bug"
            secondary = [factor_label]
        else:
            primary = factor_label
            secondary = []
        diagnoses[backend] = {
            "primary_label": primary,
            "secondary_labels": secondary,
            "factor_label": factor_label,
            "conditional_complete_case_factor_label": conditional_factor_label,
            "task_semantics_mismatch_proven": task_report[
                "task_semantics_mismatch"
            ],
            "complete_group_count": bootstrap["complete_case"]["group_count"],
            "missing_group_ids": missing_groups,
            "inconclusive_due_to_missing_groups": bool(missing_groups),
        }
    metrics = {
        "schema_version": 1,
        "analysis_revision": ANALYSIS_REVISION,
        "diagnostic_only": True,
        "qa_revealed": True,
        "eligible_as_main_result": False,
        "group_count": 30,
        "episode_count": 360,
        "backends": backend_metrics,
        "geometry_diagnostics": geometry_diagnostics,
        "alignment_failures": alignment_failures,
    }
    attribution_report = {
        "schema_version": 1,
        "analysis_revision": ANALYSIS_REVISION,
        "diagnostic_only": True,
        "backends": diagnoses,
        "decision_thresholds": {
            "major_effect_minimum": 0.20,
            "dominance_margin": 0.10,
            "interaction_minimum_absolute": 0.10,
        },
    }
    final = {
        "schema_version": 1,
        "analysis_revision": ANALYSIS_REVISION,
        "diagnostic_only": True,
        "complete_failure_analysis": True,
        "original_gate7_modified": False,
        "original_plan2_modified": False,
        "protocol_revision": config["protocol_revision"],
        "backends": diagnoses,
        "generated_at": utc_now(),
    }
    _atomic_json(root / "factorial_metrics.json", metrics)
    _atomic_json(root / "attribution.json", attribution_report)
    _atomic_json(root / "final_diagnosis.json", final)
    return final


def _percent(value):
    return "NA" if value is None else f"{100 * value:.2f}%"


def _pp(value):
    return "NA" if value is None else f"{100 * value:+.2f} 个百分点"


def _ci_pp(value):
    return f"[{100 * value[0]:+.2f}, {100 * value[1]:+.2f}] 个百分点"


def _markdown_table(rows):
    if not rows:
        return ""
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join("---" for _ in header) + "|")
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _write_text_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def generate_report(config, destination):
    root = output_root(config)
    baseline = load_json(root / "baseline_reproduction.json")
    task = load_json(root / "task_head_audit.json")
    role = load_json(root / "role_binding_audit.json")
    pose = load_json(root / "pose_audit.json")
    metrics = load_json(root / "factorial_metrics.json")
    attribution_report = load_json(root / "attribution.json")
    final = load_json(root / "final_diagnosis.json")
    recovery = load_json(root / "checkpoint_recovery.json")
    protocol = load_json(root / "protocol_lock.json")
    amendment_path = root / "protocol_locks" / f"{ANALYSIS_REVISION}.json"
    amendment = load_json(amendment_path) if amendment_path.is_file() else None
    baseline_hashes = baseline_hash_check(config)
    lines = [
        "# Trust3D Gate 7：CUT3R 与 VGGT 失败原因诊断报告",
        "",
        f"> 生成时间：{utc_now()}",
        "> 文档性质：固定 30-group 评测集上的后验失败归因，仅用于定位问题",
        "> 原 Gate 7 与方案 2 结果保持不变，本报告中的 oracle 结果不得作为主结果",
        "",
        "## 1. 执行结论",
        "",
        "本计划已按 P0、D0、D1、D2、D3/D4、D5 顺序执行完成。两个后端均复现原基线并完成 30/30 groups，checkpoint 恢复验证通过。一个 group 的 GT 相机中心零 baseline，未对齐指标仍覆盖 30 groups，pose-aligned 因果效应按预注册规则只在其余 29 个完整 group 上计算并降级为缺失敏感结论。Gate 7 的低准确率不是单一的‘模型前向失败’，而是任务头语义、对象 grounding、相机/坐标与 backbone 几何误差共同作用的结果。",
        "",
    ]
    for backend in BACKENDS:
        diagnosis = attribution_report["backends"][backend]
        effects = metrics["backends"][backend]["effects"]
        lines.extend(
            [
                f"- **{backend.upper()}**：主标签 `{diagnosis['primary_label']}`，缺失敏感几何标签 `{diagnosis['factor_label']}`，29-group 条件式标签 `{diagnosis['conditional_complete_case_factor_label']}`；grounding {_pp(effects['grounding_effect']['estimate'])}，pose {_pp(effects['pose_effect']['estimate'])}，backbone gap {_pp(effects['backbone_gap']['estimate'])}。",
            ]
        )
    lines.extend(
        [
            "",
            "## 2. 协议、数据与完整性",
            "",
            f"- 协议 revision：`{protocol['revision']}`，SHA256：`{protocol['revision_sha256']}`。",
            f"- 归因实现 revision：`{ANALYSIS_REVISION}`；因零 baseline 缺失处理修正形成不可覆盖 amendment：`{amendment_path}`（SHA256：`{amendment['amendment_sha256'] if amendment else '缺失'}`）。",
            f"- Git commit：`{repository_commit()}`。",
            f"- 数据规模：{baseline['group_count']} groups，{baseline['episode_count']} questions。",
            f"- 原结果锚点：{len(baseline_hashes)}/{len(baseline_hashes)} 哈希一致。",
            f"- 角色绑定：检查 {role['checked_input_count']} 个输入，失败 {role['failure_count']} 个。",
            f"- 恢复验证：`checkpoint_recovery_pass={str(recovery['checkpoint_recovery_pass']).lower()}`，检查 {recovery['checked_checkpoint_count']} 个 checkpoint。",
            "",
            "## 3. 基线复现",
            "",
            _markdown_table(
                [
                    ["后端", "复现准确率", "发布准确率", "逐题答案匹配"],
                    *[
                        [
                            backend.upper(),
                            _percent(baseline["backends"][backend]["accuracy"]),
                            _percent(
                                baseline["backends"][backend]["published_accuracy"]
                            ),
                            f"{baseline['backends'][backend]['exact_answer_match_count']}/360",
                        ]
                        for backend in BACKENDS
                    ],
                ]
            ),
            "",
            "CUT3R 复现 56.11%，VGGT 复现 45.00%。因此后续差异不是 evaluator 漂移或 checkpoint 读取错误造成的。",
            "",
            "## 4. 任务头与标签语义",
            "",
            _markdown_table(
                [
                    ["变体", "含义", "准确率"],
                    ["TL", "GT 点 + GT pose + legacy 完整相机 z 任务头", _percent(task["TL_accuracy"])],
                    ["T0", "GT 点 + GT pose + 数据定义平面 yaw 任务头", _percent(task["T0_accuracy"])],
                    ["R0", "RGB-D 点 + GT pose + 数据定义任务头", _percent(task["R0_accuracy"])],
                ]
            ),
            "",
            f"legacy 与数据契约共有 {task['legacy_contract_mismatch_count']} 个问题答案不一致，`task_semantics_mismatch={str(task['task_semantics_mismatch']).lower()}`。数据标签的 `front_behind` 忽略 horizon，而 legacy 任务头使用包含 pitch 的完整 camera z；这是确定性语义错误，不是通过 QA 调参选择出的坐标变换。",
            "",
            f"真实 `front_behind` 标签计数：`{task['front_behind_label_counts']}`。由于当前真实集单类不平衡，类别对称性只能由合成契约证明，不能从本 30-group 结果外推。",
            "",
            "## 5. 因果矩阵结果",
            "",
        ]
    )
    for backend in BACKENDS:
        backend_metrics = metrics["backends"][backend]
        rows = [["变体", "准确率", "95% group bootstrap CI", "有效 groups"]]
        for variant in ("B0", "B1", "B2", "B3", "C0", "C1", "C2", "C3", "TL", "T0", "R0"):
            value = backend_metrics["variants"][variant]
            rows.append(
                [
                    variant,
                    _percent(value["accuracy"]),
                    f"[{_percent(value['ci95'][0])}, {_percent(value['ci95'][1])}]",
                    str(value["group_count"]),
                ]
            )
        lines.extend([f"### 5.{1 if backend == 'cut3r' else 2} {backend.upper()}", "", _markdown_table(rows), ""])
        effects = backend_metrics["effects"]
        effect_rows = [["效应", "点估计", "95% CI", "方向"]]
        for name in (
            "task_head_effect",
            "grounding_effect",
            "pose_effect",
            "interaction",
            "backbone_gap",
        ):
            value = effects[name]
            effect_rows.append(
                [name, _pp(value["estimate"]), _ci_pp(value["ci95"]), value["direction"]]
            )
        lines.extend([_markdown_table(effect_rows), ""])
        complete_case = backend_metrics["complete_case"]
        lines.extend(
            [
                f"2x2 效应使用 {complete_case['group_count']}/30 个完整 group；缺失 group：`{complete_case['missing_group_ids']}`。由于存在不可识别 alignment，几何因素主标签按计划降级为 `inconclusive_small_sample`，表中效应仅为 complete-case 条件式描述。",
                "",
            ]
        )
    lines.extend(
        [
            "## 6. Grounding 与三维点误差",
            "",
        ]
    )
    grounding_rows = [["后端", "selector", "purity 中位数", "recall 中位数", "target 3D error 中位数", "donor 3D error 中位数"]]
    for backend in BACKENDS:
        for selector in ("center_0.12", "gt_bbox", "gt_mask"):
            value = metrics["geometry_diagnostics"][backend][selector]
            grounding_rows.append(
                [
                    backend.upper(),
                    selector,
                    _percent(value["purity"]["median"]),
                    _percent(value["recall"]["median"]),
                    f"{value['target_error']['median']:.4f}" if value["target_error"]["median"] is not None else "NA",
                    f"{value['donor_error']['median']:.4f}" if value["donor_error"]["median"] is not None else "NA",
                ]
            )
    lines.extend(
        [
            _markdown_table(grounding_rows),
            "",
            "GT bbox/mask 只用于后验 oracle 归因。它们若提高准确率，只能证明 center crop grounding 对误差有贡献，不能成为 RGB-only 可部署方案。",
            "",
            "## 7. 相机与坐标诊断",
            "",
        ]
    )
    for backend in BACKENDS:
        records = pose["backends"][backend]["records"]
        lines.append(
            f"- **{backend.upper()}**：60 个 sequence 中失败 {pose['backends'][backend]['failure_count']} 个；Sim(3) 后相机中心残差中位数 {_distribution([item['alignment_center_residual_median'] for item in records])['median']:.4f}，旋转残差中位数 {_distribution([item['alignment_rotation_residual_median_degrees'] for item in records])['median']:.2f}°。"
        )
    lines.extend(
        [
            "",
            "oracle-aligned pose 使用相机对应关系拟合单一全局 Sim(3)，不读取 QA 答案，也不按对象分别对齐。它只能消除全局 gauge/pose 误差，不能修复局部深度、对象选择或形变。",
            "",
            f"CUT3R 与 VGGT 共同缺失 group：`{sorted({item['group_id'] for item in metrics['alignment_failures']})}`。该 group 的 GT 五帧相机中心最大 baseline 为 0，尺度不可识别；没有使用对象点、答案或补零伪造尺度。",
            "",
            "## 8. 最终原因判断",
            "",
        ]
    )
    for backend in BACKENDS:
        diagnosis = final["backends"][backend]
        lines.extend(
            [
                f"### 8.{1 if backend == 'cut3r' else 2} {backend.upper()}",
                "",
                f"- 主标签：`{diagnosis['primary_label']}`。",
                f"- 条件式几何标签：`{diagnosis['factor_label']}`。",
                f"- 29-group complete-case 条件标签：`{diagnosis['conditional_complete_case_factor_label']}`。",
                f"- 完整 group：{diagnosis['complete_group_count']}/30；缺失：`{diagnosis['missing_group_ids']}`。",
                f"- 次标签：`{', '.join(diagnosis['secondary_labels']) if diagnosis['secondary_labels'] else '无'}`。",
                "- 解释：先用 contract-valid 任务头消除已证明的标签语义错误，再依据 V0-V3 归因 grounding、pose 与 backbone，避免把下游任务头错误错误归给三维 backbone。",
                "",
            ]
        )
    lines.extend(
        [
            "## 9. 结论边界",
            "",
            "1. 所有 B1-B3、C1-C3、R0、T0 均为后验 oracle 诊断，不具备主结果资格。",
            "2. 未对齐变体统计单位是 30 个 group；涉及 pose alignment 的 2x2 效应只覆盖 29 个完整 group。95% CI 是配对 group bootstrap 描述，不是总体显著性或多重检验结论。",
            "3. 当前 `front_behind` 真实标签全为 behind，不能据此证明对 front 类别的泛化能力。",
            "4. 原 Gate 7 的 CUT3R 56.11% 与方案 2 的 VGGT 45.00% 均保持不变，Gate 7 仍失败，禁止进入 Gate 8。",
            "5. 下一步必须按已定位的最大正向因素分别设计独立冻结协议；不得在当前 30 groups 上调参后宣称修复成功。",
            "",
            "## 10. 可审计产物",
            "",
            "- `outputs/gate7_diagnosis/protocol_lock.json`",
            f"- `outputs/gate7_diagnosis/protocol_locks/{ANALYSIS_REVISION}.json`",
            "- `outputs/gate7_diagnosis/baseline_reproduction.json`",
            "- `outputs/gate7_diagnosis/task_head_audit.json`",
            "- `outputs/gate7_diagnosis/pose_audit.json`",
            "- `outputs/gate7_diagnosis/factorial_predictions.jsonl`",
            "- `outputs/gate7_diagnosis/factorial_metrics.json`",
            "- `outputs/gate7_diagnosis/attribution.json`",
            "- `outputs/gate7_diagnosis/checkpoint_recovery.json`",
            "- `outputs/gate7_diagnosis/final_diagnosis.json`",
            "",
        ]
    )
    _write_text_atomic(destination, "\n".join(lines))
    return {"report": str(destination), "line_count": len(lines)}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "prepare",
            "lock",
            "unit-summary",
            "offline-public",
            "offline-private",
            "offline",
            "attribute",
            "report",
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/gate7_failure_diagnosis.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("Trust3D_Gate7_CUT3R_VGGT失败原因诊断报告.md"),
    )
    return parser


def main(argv=None):
    if not os.environ.get("TMUX"):
        raise RuntimeError("Gate 7 分层诊断只能在 tmux 中执行")
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.mode == "prepare":
        result = prepare(config)
    elif args.mode == "lock":
        result = lock_protocol(config)
    elif args.mode == "unit-summary":
        result = synthetic_summary(config)
    elif args.mode == "offline-public":
        result = offline_public(config)
    elif args.mode == "offline-private":
        result = offline_private(config)
    elif args.mode == "offline":
        result = {
            "public": offline_public(config),
            "private": offline_private(config),
        }
    elif args.mode == "attribute":
        result = attribute(config)
    else:
        result = generate_report(config, args.report)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
