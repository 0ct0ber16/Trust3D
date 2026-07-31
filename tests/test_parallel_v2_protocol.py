import json
from pathlib import Path

import numpy as np
from PIL import Image

from trust3d.agents.evidence import choose_route, validate_packet
from trust3d.data.build_five_route import _reobserve_oracle_eligible
from trust3d.eval.evaluate_gate7_fix import evaluate_sealed, seal_predictions
from trust3d.geometry.camera_contract import (
    camera_to_world_to_world_to_camera,
    opencv_to_opengl_camera,
    planar_answers,
    transform_point,
)
from trust3d.geometry.rgb_grounding import saliency_box
from trust3d.geometry.run_cut3r import (
    _install_inference_guard,
    _load_sequence_manifest,
)
from trust3d.parallel_v2 import five_route, integration
from trust3d.parallel_v2.common import (
    _porcelain_has_relevant_changes,
    atomic_json,
    atomic_jsonl,
    load_jsonl,
    sha256_file,
)


def packet(source, value, predicate="attribute", valid_until=20):
    return {
        "schema_version": 1,
        "episode_id": "e",
        "query_id": "q",
        "object_id": "o",
        "predicate": predicate,
        "value": value,
        "source": source,
        "observed_at": 0,
        "valid_until": valid_until,
        "reference_frame": "world",
        "pose_convention": "camera_to_world",
        "confidence": 0.99,
        "is_observed": value is not None,
        "provenance": [],
        "cost": {
            "move_steps": 0,
            "new_observations": 0,
            "vlm_calls": 0,
            "geometry_calls": 0,
            "wall_seconds": 0.0,
        },
    }


def public(predicate, packets=(), current=False, reobserve=False):
    return {
        "query_time": 20,
        "predicate": predicate,
        "route_capabilities": {
            "current_view": current,
            "reobserve": reobserve,
        },
        "estimated_route_error": {"REOBSERVE": 0.01},
        "evidence_packets": list(packets),
    }


def test_all_five_routes_are_identifiable_from_public_input():
    current = packet("current_view", "right", "left_right")
    history = packet("history", True)
    memory = packet("gt_3d", "front", "front_behind")
    assert choose_route(public("left_right", [current], current=True)) == "USE_CURRENT_VIEW"
    assert choose_route(public("attribute", [history])) == "RETRIEVE_HISTORY"
    assert choose_route(public("front_behind", [memory])) == "QUERY_3D_MEMORY"
    assert choose_route(public("attribute", reobserve=True)) == "REOBSERVE"
    assert choose_route(public("attribute")) == "ABSTAIN"
    for value in (current, history, memory):
        validate_packet(value)


def test_stale_history_cannot_be_routed_as_fresh():
    stale = packet("history", True, valid_until=5)
    assert choose_route(public("attribute", [stale], reobserve=True)) == "REOBSERVE"


def test_camera_contract_round_trip_axes_and_rotations():
    assert np.allclose(opencv_to_opengl_camera([1, 2, 3]), [1, -2, -3])
    point = np.asarray([1.0, 2.0, 3.0])
    for yaw in (0, 90, 180, 270):
        radians = np.deg2rad(yaw)
        forward = np.asarray([np.sin(radians), 0, np.cos(radians)])
        right = np.asarray([np.cos(radians), 0, -np.sin(radians)])
        pose = np.eye(4)
        pose[:3, 0] = right
        pose[:3, 2] = forward
        pose[:3, 3] = [0.5, 1.0, -0.5]
        inverse = camera_to_world_to_world_to_camera(pose)
        assert np.allclose(transform_point(pose, transform_point(inverse, point)), point, atol=1e-6)
        target = pose[:3, 3] + right + 2 * forward
        donor = pose[:3, 3] - 2 * right + 5 * forward
        answers = planar_answers(target, donor, pose)
        assert answers["left_right"] == "right"
        assert answers["front_behind"] == "front"
        swapped = planar_answers(donor, target, pose)
        assert answers["target_nearer"] != swapped["target_nearer"]


def test_rgb_grounding_is_deterministic_and_bounded(tmp_path):
    image = np.zeros((64, 64, 3), dtype=np.uint8)
    image[20:40, 35:55] = [255, 32, 32]
    path = tmp_path / "rgb.png"
    Image.fromarray(image).save(path)
    first = saliency_box(path, (64, 64))
    second = saliency_box(path, (64, 64))
    assert first == second
    x0, y0, x1, y1 = first["box_xyxy"]
    assert 0 <= x0 < x1 <= 64
    assert 0 <= y0 < y1 <= 64
    assert first["method"] == "rgb_saliency_v1"


def test_parallel_protocol_config_is_frozen():
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root / "configs/parallel_v2_protocol.json").read_text())
    assert value["protocol_revision"] == "parallel-v2"
    assert value["minimum_coverage"] == 0.75
    assert value["bootstrap_groups"] == 10000
    assert value["resources"]["minimum_free_gpu_mib"] == 20480


def test_reobserve_sources_respect_registered_abstain_margin():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "configs/five_route_gt_v1.json").read_text())
    registered = json.loads(
        (root / "configs/parallel_v2_protocol.json").read_text()
    )
    assert _reobserve_oracle_eligible(
        [{"verification_cost": 17}], config, registered
    )
    assert not _reobserve_oracle_eligible(
        [{"verification_cost": 18}], config, registered
    )


def test_repository_dirty_ignores_only_untracked_parallel_runtime_outputs():
    runtime_output = "?? outputs/parallel_v2/gate7_fix/result.json\0"
    assert not _porcelain_has_relevant_changes(runtime_output)
    assert _porcelain_has_relevant_changes(runtime_output + "?? notes.txt\0")
    assert _porcelain_has_relevant_changes(" M trust3d/parallel_v2/common.py\0")
    assert _porcelain_has_relevant_changes(
        " M outputs/parallel_v2/gate7_fix/result.json\0"
    )


def test_underpowered_pilot_writes_terminal_scientific_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(five_route, "OUTPUT", tmp_path / "output")
    monkeypatch.setattr(five_route, "ROOT", tmp_path)
    power = {
        "alpha": 0.05,
        "power": 0.8,
        "paired_accuracy_std": 0.5,
        "paired_cost_reduction_std": 0.25,
        "required_accuracy_groups": 4904,
        "required_cost_groups": 13,
        "required_final_groups": 4904,
        "available_final_groups": 60,
        "adequately_powered": False,
    }
    result = five_route._write_underpowered_result(power)
    assert result["status"] == "failed_scientific"
    assert result["reason"] == "inconclusive_underpowered"
    assert not result["complete"]
    assert json.loads((five_route.OUTPUT / "power_audit.json").read_text())[
        "decision"
    ] == "inconclusive_underpowered"
    assert "不把统计功效不足解释为五路路由 idea 失败" in (
        tmp_path / "Trust3D_GT五路路由实验报告.md"
    ).read_text()


def test_rgb_sequence_manifest_is_hash_checked_and_private_paths_are_blocked(tmp_path):
    dataset = tmp_path / "dataset"
    frames = []
    for scenario in range(2):
        records = []
        for index in range(5):
            path = dataset / "inference_rgb" / "g" / f"scenario_{scenario}" / f"frame_{index}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.full((4, 4, 3), scenario * 10 + index, dtype=np.uint8)).save(path)
            records.append({"path": str(path), "sha256": sha256_file(path)})
        frames.append({"scenario_id": f"scenario_{scenario}", "frames": records})
    manifest = tmp_path / "rgb_sequences.json"
    atomic_json(
        manifest,
        {
            "schema_version": 1,
            "input_mode": "rgb_only_counterfactual_sequences",
            "group_count": 1,
            "groups": [{"group_id": "g", "scenarios": frames}],
        },
    )
    loaded = _load_sequence_manifest(manifest)
    assert set(loaded["g"]) == {"stable", "stale"}

    private = dataset / "oracle_private.jsonl"
    private.write_text("{}\n", encoding="utf-8")
    _install_inference_guard(dataset, dataset / "checkpoints")
    try:
        private.read_text(encoding="utf-8")
    except PermissionError:
        pass
    else:
        raise AssertionError("private file access was not blocked")


def _geometry_stage(target_x, answer):
    return {
        "target": {
            "world": [target_x, 0.0, 2.0],
            "confidence_median": 2.0,
            "grounding": {"confidence": 0.9},
        },
        "donor": {
            "world": [0.0, 0.0, 4.0],
            "confidence_median": 2.0,
            "grounding": {"confidence": 0.9},
        },
        "query_camera_to_world": np.eye(4).tolist(),
        "answers": {"left_right": answer},
    }


def test_gate7_seals_both_counterfactuals_before_private_evaluation(tmp_path):
    public_path = tmp_path / "episodes_public.jsonl"
    routes_path = tmp_path / "routes.jsonl"
    private_path = tmp_path / "oracle_private.jsonl"
    geometry_root = tmp_path / "geometry"
    output_root = tmp_path / "sealed"
    rgb = tmp_path / "query.png"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(rgb)
    public = [
        {
            "episode_id": episode_id,
            "group_id": "g",
            "query_observation": {"rgb": str(rgb)},
            "question": {"type": "left_right"},
        }
        for episode_id in ("stable", "stale")
    ]
    atomic_jsonl(public_path, public)
    atomic_jsonl(
        routes_path,
        [
            {
                "episode_id": item["episode_id"],
                "policy_id": "trust3d_lambda_0.01",
                "route": "reobserve",
            }
            for item in public
        ],
    )
    checkpoint = geometry_root / "checkpoints/g.json"
    atomic_json(
        checkpoint,
        {
            "group_id": "g",
            "status": "success",
            "historical": _geometry_stage(1.0, "right"),
            "stable_reobserve": _geometry_stage(1.0, "right"),
            "stale_reobserve": _geometry_stage(-1.0, "left"),
        },
    )
    atomic_json(
        geometry_root / "manifest.json",
        {
            "complete": True,
            "groups": [{"group_id": "g", "checkpoint": str(checkpoint)}],
        },
    )
    atomic_json(
        geometry_root / "inference_access_audit.json",
        {
            "guard": "python_audit_hook_v1",
            "guard_installed": True,
            "private_file_open_count": 0,
            "forbidden_open_attempt_count": 0,
        },
    )
    seal_predictions(
        public_path,
        routes_path,
        geometry_root,
        "cut3r",
        "rgb_saliency_v1",
        output_root,
    )
    sealed = load_jsonl(output_root / "predictions.jsonl")
    assert all(
        set(item["candidates"]) == {"stable_reobserve", "stale_reobserve"}
        for item in sealed
    )
    assert all("stage" not in item for item in sealed)
    atomic_jsonl(
        private_path,
        [
            {"episode_id": "stable", "branch": "risk_stable", "current_answer_gt": "right"},
            {"episode_id": "stale", "branch": "risk_stale", "current_answer_gt": "left"},
        ],
        mode=0o600,
    )
    metrics = evaluate_sealed(output_root, private_path)
    assert metrics["contract_valid_metric"]["accuracy"] == 1.0


def test_integration_defers_reobserve_scenario_resolution_to_evaluator(tmp_path, monkeypatch):
    data = tmp_path / "data"
    output_root = tmp_path / "outputs"
    output = output_root / "integration"
    geometry_root = tmp_path / "geometry"
    monkeypatch.setattr(integration, "DATA", data)
    monkeypatch.setattr(integration, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(integration, "OUTPUT", output)
    monkeypatch.setattr(integration, "verify_baseline", lambda: [{"match": True}])
    costs = json.loads(
        (Path(__file__).resolve().parents[1] / "configs/five_route_gt_v1.json").read_text()
    )["route_costs"]
    atomic_jsonl(
        data / "integration_public.jsonl",
        [
            {
                "episode_id": "e",
                "group_id": "i",
                "source_group_id": "g",
                "object_id": "o",
                "predicate": "left_right",
                "query_time": 20,
                "question": {"type": "left_right"},
                "evidence_condition": "stale_reachable",
                "route_capabilities": {"current_view": False, "reobserve": True},
                "candidate_costs": costs,
            }
        ],
    )
    atomic_jsonl(
        data / "integration_private.jsonl",
        [
            {
                "episode_id": "e",
                "source_branch": "risk_stale",
                "private_answer": "left",
                "oracle_best_route": "REOBSERVE",
            }
        ],
        mode=0o600,
    )
    atomic_json(
        output_root / "gate7_fix/confidence_lock.json",
        {
            "method": "equal_width_5_bin",
            "bins": [
                {"error_probability": 0.01} for _ in range(5)
            ],
        },
    )
    checkpoint = geometry_root / "checkpoints/g.json"
    atomic_json(
        checkpoint,
        {
            "group_id": "g",
            "status": "success",
            "historical": _geometry_stage(1.0, "right"),
            "stable_reobserve": _geometry_stage(1.0, "right"),
            "stale_reobserve": _geometry_stage(-1.0, "left"),
        },
    )
    atomic_json(
        geometry_root / "manifest.json",
        {
            "complete": True,
            "groups": [{"group_id": "g", "checkpoint": str(checkpoint)}],
        },
    )
    atomic_json(
        geometry_root / "inference_access_audit.json",
        {
            "guard": "python_audit_hook_v1",
            "guard_installed": True,
            "private_file_open_count": 0,
            "forbidden_open_attempt_count": 0,
        },
    )
    integration.seal(geometry_root, "cut3r", "rgb_saliency_v1")
    prediction = load_jsonl(output / "predictions.jsonl")[0]
    assert prediction["selected_route"] == "REOBSERVE"
    assert prediction["answer"] is None
    assert prediction["answered"] is True
    metrics = integration.evaluate()
    assert metrics["rgb_five_route_accuracy"] == 1.0
