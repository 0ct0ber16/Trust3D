import json
import math
from pathlib import Path

import numpy as np

from trust3d.eval.diagnose_cut3r import _pose_camera_to_world
from trust3d.eval.diagnose_gate7_layers import (
    _bootstrap_metrics,
    apply_similarity,
    contract_answers_world,
    fit_similarity,
    legacy_answers_world,
    planar_answers_predicted,
)
from trust3d.geometry.diagnostic_grounding import summarize_selector
from trust3d.geometry.run_cut3r import (
    _valid_checkpoint as valid_cut3r_checkpoint,
    point_in_camera,
)
from trust3d.geometry.run_vggt import (
    _diagnostic_manifest_fields,
    _valid_checkpoint as valid_vggt_checkpoint,
)


def _point_from_local(pose, right, forward, up=0.0):
    yaw = math.radians(pose["rotation_y"])
    return {
        "x": pose["x"] + math.cos(yaw) * right + math.sin(yaw) * forward,
        "y": pose["y"] + up,
        "z": pose["z"] - math.sin(yaw) * right + math.cos(yaw) * forward,
    }


def test_diagnosis_config_is_frozen_and_minimal():
    config = json.loads(
        Path("configs/gate7_failure_diagnosis.json").read_text(encoding="utf-8")
    )
    assert config["diagnostic_only"] is True
    assert config["selectors"] == ["center_0.12", "gt_bbox", "gt_mask"]
    assert config["counts"] == {
        "groups": 30,
        "questions": 360,
        "questions_per_group": 12,
    }
    assert len(config["pilot_group_ids"]) == 3
    assert config["resources"]["maximum_gpu_utilization_percent"] == 10
    assert config["backends"]["cut3r"]["minimum_free_gpu_mib"] == 16384
    assert config["backends"]["vggt"]["minimum_free_gpu_mib"] == 20480


def test_diagnostic_resume_requires_all_selectors(tmp_path):
    common = {
        "status": "success",
        "fingerprint": "frozen",
        "historical": {},
        "stale_sequence_historical": {},
        "stable_reobserve": {},
        "stale_reobserve": {},
        "timing": {},
    }
    cases = (
        (valid_cut3r_checkpoint, common),
        (valid_vggt_checkpoint, {**common, "camera_trajectories": {}}),
    )
    for index, (validator, payload) in enumerate(cases):
        path = tmp_path / f"checkpoint-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert validator(path, "frozen") == payload
        assert validator(path, "frozen", require_diagnostics=True) is None
        payload["diagnostic_selectors"] = {
            selector: {} for selector in ("center_0.12", "gt_bbox", "gt_mask")
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert validator(path, "frozen", require_diagnostics=True) == payload


def test_vggt_diagnostic_manifest_declares_gt_mask_access():
    fields = _diagnostic_manifest_fields(
        {
            "_sha256": "config-sha256",
            "protocol_revision": "r000",
            "backends": {"vggt": {"adapter_version": "diagnostic-v1"}},
        }
    )
    assert fields["qa_revealed"] is True
    assert fields["uses_gt_mask"] is True
    assert fields["diagnostic_gt_mask_access_declared"] is True


def test_bootstrap_reports_unidentifiable_alignment_as_missing(tmp_path):
    (tmp_path / "task_head_audit.json").write_text(
        json.dumps({"task_semantics_mismatch": True}), encoding="utf-8"
    )
    config = {
        "bootstrap_samples": 100,
        "bootstrap_seed": 20260730,
        "counts": {"questions_per_group": 1},
        "paths": {"output_root": str(tmp_path)},
    }
    variants = (
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
    records = [
        {
            "backend": "cut3r",
            "group_id": "complete",
            "variant": variant,
            "correct": True,
        }
        for variant in variants
    ]
    records.extend(
        {
            "backend": "cut3r",
            "group_id": "zero-baseline",
            "variant": variant,
            "correct": True,
        }
        for variant in ("B0", "B1", "C0", "C1", "TL", "T0", "R0")
    )
    metrics = _bootstrap_metrics(records, config, "cut3r", variants)
    assert metrics["variants"]["B0"]["group_count"] == 2
    assert metrics["variants"]["C2"]["group_count"] == 1
    assert metrics["complete_case"]["group_count"] == 1
    assert metrics["complete_case"]["missing_group_ids"] == ["zero-baseline"]
    assert metrics["effects"]["pose_effect"]["complete_case_group_count"] == 1


def test_camera_round_trip_axis_and_determinant_contract():
    for yaw in (0, 37, 90, 180, 270):
        for horizon in (-30, 0, 30):
            pose = {
                "x": 1.25,
                "y": 0.901,
                "z": -2.0,
                "rotation_y": yaw,
                "horizon": horizon,
            }
            camera_to_world = _pose_camera_to_world(pose)
            world_to_camera = np.linalg.inv(camera_to_world)
            assert np.allclose(camera_to_world @ world_to_camera, np.eye(4), atol=1e-9)
            rotation = camera_to_world[:3, :3]
            assert np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-9)
            assert abs(np.linalg.det(rotation) - 1.0) < 1e-9
            local = np.asarray([0.4, -0.2, 3.0, 1.0])
            world = camera_to_world @ local
            recovered = world_to_camera @ world
            assert np.allclose(recovered, local, atol=1e-9)
            assert np.allclose(
                point_in_camera(world[:3], camera_to_world), local[:3], atol=1e-9
            )


def test_balanced_task_contract_ignores_horizon():
    cases = 0
    for yaw in (0, 37, 90, 180, 270):
        for horizon in (-30, 0, 30):
            pose = {
                "x": 0.25,
                "y": 0.901,
                "z": -0.5,
                "rotation_y": yaw,
                "horizon": horizon,
            }
            for right in (-1.0, 1.0):
                for forward in (-2.0, 2.0):
                    target = _point_from_local(pose, right, forward, up=0.6)
                    donor = _point_from_local(pose, -right, forward * 2.0, up=-0.2)
                    answers = contract_answers_world(target, donor, pose)
                    assert answers["left_right"] == (
                        "right" if right > 0 else "left"
                    )
                    assert answers["front_behind"] == (
                        "front" if forward > 0 else "behind"
                    )
                    cases += 1
    assert cases >= 32


def test_legacy_full_camera_z_can_disagree_with_planar_label():
    pose = {
        "x": 0.0,
        "y": 0.901,
        "z": 0.0,
        "rotation_y": 0.0,
        "horizon": 30.0,
    }
    target = {"x": 1.0, "y": 4.0, "z": 1.0}
    donor = {"x": -1.0, "y": 0.901, "z": 4.0}
    contract = contract_answers_world(target, donor, pose)
    legacy = legacy_answers_world(target, donor, pose)
    assert contract["front_behind"] == "front"
    assert legacy["front_behind"] == "behind"


def test_planar_predicted_head_matches_world_contract():
    pose = {
        "x": -0.5,
        "y": 0.901,
        "z": 0.25,
        "rotation_y": 73.0,
        "horizon": 30.0,
    }
    target = _point_from_local(pose, 1.0, -2.0, up=1.0)
    donor = _point_from_local(pose, -1.0, 4.0, up=-0.3)
    expected = contract_answers_world(target, donor, pose)
    actual = planar_answers_predicted(
        [target[axis] for axis in ("x", "y", "z")],
        [donor[axis] for axis in ("x", "y", "z")],
        _pose_camera_to_world(pose),
    )
    assert actual == expected


def test_donor_target_exchange_is_an_involution():
    pose = {"x": 0, "y": 0.9, "z": 0, "rotation_y": 15, "horizon": 30}
    target = _point_from_local(pose, 1, 2, up=0.2)
    donor = _point_from_local(pose, -1, 5, up=-0.1)
    original = contract_answers_world(target, donor, pose)
    swapped = contract_answers_world(donor, target, pose)
    restored = contract_answers_world(target, donor, pose)
    assert original == restored
    assert original["target_nearer"] is True
    assert swapped["target_nearer"] is False
    assert original["which_closer"] == "target"
    assert swapped["which_closer"] == "reference"


def test_similarity_alignment_recovers_known_transform():
    poses = []
    for index in range(5):
        pose = np.eye(4)
        angle = math.radians(index * 13)
        pose[:3, :3] = np.asarray(
            [
                [math.cos(angle), 0, math.sin(angle)],
                [0, 1, 0],
                [-math.sin(angle), 0, math.cos(angle)],
            ]
        )
        pose[:3, 3] = [index * 0.4, 0.1 * (index % 2), index * -0.2]
        poses.append(pose)
    poses = np.asarray(poses)
    angle = math.radians(41)
    rotation = np.asarray(
        [
            [math.cos(angle), 0, math.sin(angle)],
            [0, 1, 0],
            [-math.sin(angle), 0, math.cos(angle)],
        ]
    )
    scale = 2.7
    translation = np.asarray([1.0, -0.4, 3.0])
    expected = poses.copy()
    expected[:, :3, :3] = np.einsum("ij,njk->nik", rotation, poses[:, :3, :3])
    expected[:, :3, 3] = (
        scale * np.einsum("ij,nj->ni", rotation, poses[:, :3, 3]) + translation
    )
    alignment = fit_similarity(poses, expected)
    assert np.allclose(alignment["rotation"], rotation, atol=1e-8)
    assert abs(alignment["scale"] - scale) < 1e-8
    assert np.allclose(alignment["translation"], translation, atol=1e-8)
    point = np.asarray([0.3, 1.2, -0.9])
    assert np.allclose(
        apply_similarity(point, alignment), scale * rotation @ point + translation
    )


def test_grounding_selectors_use_same_dense_map(tmp_path):
    points = np.zeros((20, 20, 3), dtype=np.float64)
    yy, xx = np.mgrid[:20, :20]
    points[..., 0] = xx
    points[..., 1] = yy
    points[..., 2] = 1.0
    confidence = np.ones((20, 20), dtype=np.float64)
    mask = np.zeros((20, 20), dtype=np.uint8)
    mask[2:8, 12:18] = 1
    path = tmp_path / "mask.npy"
    np.save(path, mask)
    center = summarize_selector(points, confidence, mask, path, "center_0.12")
    bbox = summarize_selector(points, confidence, mask, path, "gt_bbox")
    exact = summarize_selector(points, confidence, mask, path, "gt_mask")
    assert center["mask_purity"] == 0
    assert bbox["mask_purity"] == 1
    assert exact["mask_purity"] == 1
    assert bbox["mask_recall"] == 1
    assert exact["mask_recall"] == 1
    assert np.allclose(bbox["world"], exact["world"])
