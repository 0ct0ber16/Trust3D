"""仅用 RGB 流运行 CUT3R，并按 group 持久化可恢复几何结果。"""

import argparse
import builtins
import hashlib
import io
import json
import os
import random
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from trust3d.data.select_events import read_jsonl
from trust3d.geometry.diagnostic_grounding import (
    mask_input_records,
    role_mask_paths,
    summarize_stage,
)
from trust3d.geometry.rgb_grounding import saliency_box


SCHEMA_VERSION = 1
ADAPTER_VERSION = "gate7-cut3r-rgb-v1"


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_diagnostic_config(path, backend):
    if path is None:
        return None
    path = Path(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("diagnostic_only") is not True:
        raise ValueError("诊断配置必须显式设置 diagnostic_only=true")
    expected = value.get("backends", {}).get(backend, {})
    if not expected.get("adapter_version"):
        raise ValueError(f"诊断配置缺少 backend: {backend}")
    value["_path"] = str(path)
    value["_sha256"] = _sha256_file(path)
    return value


def _assert_diagnostic_output(path, config):
    root = Path(config["paths"]["output_root"]).resolve()
    resolved = Path(path).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"诊断输出越界: {resolved}")


def _archive_existing(path):
    path = Path(path)
    if not path.exists():
        return
    archive = path.parent / "failed_attempts" / path.stem
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.move(str(path), str(archive / f"{stamp}.json"))


def centered_point(points, confidence, crop_fraction=0.12, confidence_quantile=0.5):
    """从目标居中的验证帧中心区域提取高置信度三维中位点。"""
    points = np.asarray(points, dtype=np.float64)
    confidence = np.asarray(confidence, dtype=np.float64)
    if points.ndim != 3 or points.shape[-1] != 3:
        raise ValueError("points 必须是 HxWx3")
    if confidence.shape != points.shape[:2]:
        raise ValueError("confidence 与 points 空间尺寸不一致")
    if not 0 < crop_fraction <= 1:
        raise ValueError("crop_fraction 必须位于 (0, 1]")

    height, width = confidence.shape
    side = max(3, int(round(min(height, width) * crop_fraction)))
    y0 = max(0, height // 2 - side // 2)
    x0 = max(0, width // 2 - side // 2)
    y1 = min(height, y0 + side)
    x1 = min(width, x0 + side)
    crop_points = points[y0:y1, x0:x1]
    crop_confidence = confidence[y0:y1, x0:x1]
    valid = np.isfinite(crop_points).all(axis=-1) & np.isfinite(crop_confidence)
    if not np.any(valid):
        raise ValueError("中心区域没有有效 CUT3R 点")
    threshold = float(np.quantile(crop_confidence[valid], confidence_quantile))
    selected = valid & (crop_confidence >= threshold)
    if np.count_nonzero(selected) < 3:
        selected = valid
    point = np.median(crop_points[selected], axis=0)
    if not np.isfinite(point).all():
        raise ValueError("中心点估计不是有限值")
    return {
        "world": [float(value) for value in point],
        "selected_pixel_count": int(np.count_nonzero(selected)),
        "confidence_median": float(np.median(crop_confidence[selected])),
        "crop_xyxy": [int(x0), int(y0), int(x1), int(y1)],
    }


def boxed_point(points, confidence, box, confidence_quantile=0.5):
    points = np.asarray(points, dtype=np.float64)
    confidence = np.asarray(confidence, dtype=np.float64)
    x0, y0, x1, y1 = [int(value) for value in box]
    if points.ndim != 3 or points.shape[-1] != 3:
        raise ValueError("points 必须是 HxWx3")
    if confidence.shape != points.shape[:2]:
        raise ValueError("confidence 与 points 空间尺寸不一致")
    height, width = confidence.shape
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("RGB grounding box 越界")
    crop_points = points[y0:y1, x0:x1]
    crop_confidence = confidence[y0:y1, x0:x1]
    valid = np.isfinite(crop_points).all(axis=-1) & np.isfinite(crop_confidence)
    if not np.any(valid):
        raise ValueError("RGB grounding 区域没有有效三维点")
    threshold = float(np.quantile(crop_confidence[valid], confidence_quantile))
    selected = valid & (crop_confidence >= threshold)
    if np.count_nonzero(selected) < 3:
        selected = valid
    point = np.median(crop_points[selected], axis=0)
    if not np.isfinite(point).all():
        raise ValueError("RGB grounding 三维点不是有限值")
    return {
        "world": [float(value) for value in point],
        "selected_pixel_count": int(np.count_nonzero(selected)),
        "confidence_median": float(np.median(crop_confidence[selected])),
        "crop_xyxy": [x0, y0, x1, y1],
    }


def point_in_camera(point_world, camera_to_world):
    point_world = np.asarray(point_world, dtype=np.float64)
    camera_to_world = np.asarray(camera_to_world, dtype=np.float64)
    if point_world.shape != (3,) or camera_to_world.shape != (4, 4):
        raise ValueError("三维点或相机矩阵尺寸错误")
    rotation = camera_to_world[:3, :3]
    translation = camera_to_world[:3, 3]
    value = rotation.T @ (point_world - translation)
    return [float(item) for item in value]


def spatial_answers(target_camera, donor_camera):
    target = np.asarray(target_camera, dtype=np.float64)
    donor = np.asarray(donor_camera, dtype=np.float64)
    if target.shape != (3,) or donor.shape != (3,):
        raise ValueError("目标与参照点必须是三维坐标")
    if not np.isfinite(target).all() or not np.isfinite(donor).all():
        raise ValueError("目标与参照点包含非有限值")
    target_distance = float(np.linalg.norm(target))
    donor_distance = float(np.linalg.norm(donor))
    return {
        "left_right": "right" if target[0] > 0 else "left",
        "front_behind": "front" if target[2] > 0 else "behind",
        "which_closer": "target" if target_distance < donor_distance else "reference",
        "target_nearer": bool(target_distance < donor_distance),
    }


def _public_groups(path, selected_group=None, max_groups=None):
    grouped = {}
    for episode in read_jsonl(path):
        grouped.setdefault(episode["group_id"], []).append(episode)
    values = []
    for group_id in sorted(grouped):
        if selected_group and group_id != selected_group:
            continue
        episodes = grouped[group_id]
        first = episodes[0]
        signature = {
            "history_observations": first["history_observations"],
            "query_observation": first["query_observation"],
        }
        if any(
            {
                "history_observations": item["history_observations"],
                "query_observation": item["query_observation"],
            }
            != signature
            for item in episodes[1:]
        ):
            raise ValueError(f"group {group_id} 的公开 RGB 路径不一致")
        values.append((group_id, first, len(episodes)))
    if max_groups is not None:
        values = values[:max_groups]
    if selected_group and not values:
        raise ValueError(f"公开 episode 中不存在 group: {selected_group}")
    return values


def _source_contexts(root):
    contexts = {}
    for path in sorted(Path(root).glob("*/context.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("status") != "success" or not value.get("group_id"):
            continue
        value["_checkpoint_path"] = str(path)
        contexts[value["group_id"]] = value
    return contexts


class _InferenceAccessGuard:
    def __init__(self, forbidden_paths):
        self.forbidden_paths = tuple(Path(path).resolve() for path in forbidden_paths)
        self.forbidden_open_attempts = []

    def install(self):
        def check(raw_path):
            if not isinstance(raw_path, (str, bytes, os.PathLike)):
                return
            candidate = Path(os.fsdecode(raw_path)).resolve()
            for forbidden in self.forbidden_paths:
                if candidate == forbidden or forbidden in candidate.parents:
                    self.forbidden_open_attempts.append(str(candidate))
                    raise PermissionError(f"inference access guard blocked: {candidate}")

        def audit(event, arguments):
            if event != "open" or not arguments:
                return
            check(arguments[0])

        if hasattr(sys, "addaudithook"):
            sys.addaudithook(audit)
        else:
            original_builtin_open = builtins.open
            original_io_open = io.open

            def guarded_builtin_open(file, *args, **kwargs):
                check(file)
                return original_builtin_open(file, *args, **kwargs)

            def guarded_io_open(file, *args, **kwargs):
                check(file)
                return original_io_open(file, *args, **kwargs)

            builtins.open = guarded_builtin_open
            io.open = guarded_io_open
        return self


def _load_sequence_manifest(path):
    manifest_path = Path(path)
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if value.get("input_mode") != "rgb_only_counterfactual_sequences":
        raise ValueError("RGB sequence manifest input mode is invalid")
    groups = {}
    for group in value.get("groups", []):
        scenario_records = {
            item["scenario_id"]: item for item in group.get("scenarios", [])
        }
        if set(scenario_records) != {"scenario_0", "scenario_1"}:
            raise ValueError("RGB sequence manifest must contain two anonymous scenarios")
        sequences = {}
        for internal_name, scenario_id in (
            ("stable", "scenario_0"),
            ("stale", "scenario_1"),
        ):
            frames = scenario_records[scenario_id].get("frames", [])
            if len(frames) != 5:
                raise ValueError("each RGB sequence must contain exactly five frames")
            paths = []
            for frame in frames:
                frame_path = Path(frame["path"])
                if not frame_path.is_absolute():
                    frame_path = Path.cwd() / frame_path
                frame_path = frame_path.resolve()
                if "inference_rgb" not in frame_path.parts or not frame_path.is_file():
                    raise ValueError(f"sequence frame is outside sanitized RGB root: {frame_path}")
                if _sha256_file(frame_path) != frame["sha256"]:
                    raise ValueError(f"sequence frame hash mismatch: {frame_path}")
                paths.append(frame_path)
            sequences[internal_name] = paths
        groups[group["group_id"]] = sequences
    if len(groups) != value.get("group_count"):
        raise ValueError("RGB sequence manifest group count mismatch")
    return groups


def _install_inference_guard(dataset_root, source_checkpoints):
    dataset_root = Path(dataset_root)
    forbidden = [
        dataset_root / "oracle_private.jsonl",
        source_checkpoints,
        dataset_root / "cache/depth",
        dataset_root / "cache/instance",
        dataset_root / "cache/masks",
    ]
    return _InferenceAccessGuard(forbidden).install()


def _observation_rgb(dataset_root, observation):
    path = Path(dataset_root) / observation["rgb"]
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _sequence_paths(public, context, dataset_root, branch):
    history = public["history_observations"]
    query = Path(public["query_observation"]["rgb"])
    current = context["branches"][branch]["observations"]
    paths = [
        Path(history["target"]["rgb"]),
        Path(history["donor"]["rgb"]),
        query,
        _observation_rgb(dataset_root, current["target"]),
        _observation_rgb(dataset_root, current["donor"]),
    ]
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    return paths


def _fingerprint(
    group_id,
    sequence_paths,
    checkpoint_sha256,
    image_size,
    crop_fraction,
    grounding,
    grounding_parameters,
    diagnostic=None,
):
    inputs = []
    for scenario, paths in sorted(sequence_paths.items()):
        for index, path in enumerate(paths):
            inputs.append(
                {
                    "scenario": scenario,
                    "index": index,
                    "path": str(path),
                    "sha256": _sha256_file(path),
                }
            )
    payload = {
        "adapter_version": ADAPTER_VERSION,
        "group_id": group_id,
        "checkpoint_sha256": checkpoint_sha256,
        "image_size": image_size,
        "crop_fraction": crop_fraction,
        "grounding": grounding,
        "grounding_parameters": grounding_parameters,
        "inputs": inputs,
    }
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), inputs


def _valid_checkpoint(path, fingerprint, require_diagnostics=False):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if value.get("status") != "success" or value.get("fingerprint") != fingerprint:
        return None
    required = {
        "historical",
        "stale_sequence_historical",
        "stable_reobserve",
        "stale_reobserve",
        "timing",
    }
    if not required <= value.keys():
        return None
    if require_diagnostics and set(value.get("diagnostic_selectors", {})) != {
        "center_0.12",
        "gt_bbox",
        "gt_mask",
    }:
        return None
    return value


def _prepare_views(paths, image_size):
    import torch
    from src.dust3r.utils.image import load_images

    images = load_images([str(path) for path in paths], size=image_size, verbose=False)
    views = []
    for index, image in enumerate(images):
        views.append(
            {
                "img": image["img"],
                "ray_map": torch.full(
                    (
                        image["img"].shape[0],
                        6,
                        image["img"].shape[-2],
                        image["img"].shape[-1],
                    ),
                    torch.nan,
                ),
                "true_shape": torch.from_numpy(image["true_shape"]),
                "idx": index,
                "instance": str(index),
                "camera_pose": torch.from_numpy(
                    np.eye(4, dtype=np.float32)
                ).unsqueeze(0),
                "img_mask": torch.tensor(True).unsqueeze(0),
                "ray_mask": torch.tensor(False).unsqueeze(0),
                "update": torch.tensor(True).unsqueeze(0),
                "reset": torch.tensor(False).unsqueeze(0),
            }
        )
    return views


def _decorate_diagnostic_stage(stage, query_pose):
    for role in ("target", "donor"):
        stage[role]["query_camera"] = point_in_camera(
            stage[role]["world"], query_pose
        )
    stage["query_camera_to_world"] = query_pose.tolist()
    stage["answers"] = spatial_answers(
        stage["target"]["query_camera"], stage["donor"]["query_camera"]
    )
    return stage


def _run_sequence(
    paths,
    model,
    device,
    image_size,
    crop_fraction,
    grounding="center_crop",
    grounding_parameters=None,
    diagnostic_masks=None,
    diagnostic_config=None,
):
    import torch
    from src.dust3r.inference import inference
    from src.dust3r.utils.camera import pose_encoding_to_camera
    from src.dust3r.utils.geometry import geotrf

    device = torch.device(device)
    views = _prepare_views(paths, image_size)
    if device.type == "cuda":
        torch.cuda.synchronize(device)
        torch.cuda.reset_peak_memory_stats(device)
    started = time.perf_counter()
    outputs, state_args = inference(views, model, device, verbose=False)
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    elapsed = time.perf_counter() - started
    peak_memory = (
        int(torch.cuda.max_memory_allocated(device)) if device.type == "cuda" else 0
    )

    poses = []
    world_maps = []
    confidences = []
    for prediction in outputs["pred"]:
        pose = pose_encoding_to_camera(prediction["camera_pose"].clone())[0]
        points_self = prediction["pts3d_in_self_view"]
        points_world = geotrf(pose.unsqueeze(0), points_self)[0]
        poses.append(pose.cpu().numpy())
        world_maps.append(points_world.cpu().numpy())
        confidences.append(prediction["conf_self"][0].cpu().numpy())

    grounding_parameters = grounding_parameters or {}

    def grounded(index):
        if grounding == "center_crop":
            return centered_point(world_maps[index], confidences[index], crop_fraction)
        if grounding != "rgb_saliency_v1":
            raise ValueError(f"不支持的 RGB grounding: {grounding}")
        specification = saliency_box(
            paths[index],
            confidences[index].shape,
            quantile=float(grounding_parameters.get("saliency_quantile", 0.82)),
            minimum_fraction=float(grounding_parameters.get("minimum_fraction", 0.08)),
            maximum_fraction=float(grounding_parameters.get("maximum_fraction", 0.30)),
        )
        point = boxed_point(
            world_maps[index], confidences[index], specification["box_xyxy"]
        )
        point["grounding"] = specification
        return point

    def geometry(target_index, donor_index):
        target = grounded(target_index)
        donor = grounded(donor_index)
        target["query_camera"] = point_in_camera(target["world"], poses[2])
        donor["query_camera"] = point_in_camera(donor["world"], poses[2])
        return {
            "target": target,
            "donor": donor,
            "query_camera_to_world": poses[2].tolist(),
            "answers": spatial_answers(target["query_camera"], donor["query_camera"]),
        }

    value = {
        "historical": geometry(0, 1),
        "current": geometry(3, 4),
        "camera_to_world": [pose.tolist() for pose in poses],
        "elapsed_seconds": elapsed,
        "peak_allocated_bytes": peak_memory,
        "frame_count": len(paths),
    }
    if diagnostic_config is not None:
        quantile = float(diagnostic_config["confidence_quantile"])
        historical = summarize_stage(
            world_maps,
            confidences,
            {"target": 0, "donor": 1},
            diagnostic_masks["historical"],
            crop_fraction,
            quantile,
        )
        current = summarize_stage(
            world_maps,
            confidences,
            {"target": 3, "donor": 4},
            diagnostic_masks["current"],
            crop_fraction,
            quantile,
        )
        selectors = {}
        for selector in historical:
            selectors[selector] = {
                "historical": _decorate_diagnostic_stage(
                    historical[selector], poses[2]
                ),
                "current": _decorate_diagnostic_stage(current[selector], poses[2]),
            }
        tolerance = diagnostic_config["tolerances"]["center_point_absolute"]
        for stage_name in ("historical", "current"):
            for role in ("target", "donor"):
                expected = np.asarray(value[stage_name][role]["world"])
                actual = np.asarray(
                    selectors["center_0.12"][stage_name][role]["world"]
                )
                if not np.allclose(expected, actual, atol=tolerance, rtol=0):
                    raise ValueError("诊断 center 与原中心点实现不一致")
        value["diagnostic_selectors"] = selectors
    del outputs, state_args, views
    return value


def _diagnostic_mask_bundle(context, dataset_root):
    return {
        "stable": role_mask_paths(
            context, dataset_root, "risk_stable"
        ),
        "stale": role_mask_paths(context, dataset_root, "risk_stale"),
    }


def _combine_diagnostic_selectors(stable, stale):
    values = {}
    for selector in stable["diagnostic_selectors"]:
        values[selector] = {
            "historical": stable["diagnostic_selectors"][selector]["historical"],
            "stale_sequence_historical": stale["diagnostic_selectors"][selector][
                "historical"
            ],
            "stable_reobserve": stable["diagnostic_selectors"][selector]["current"],
            "stale_reobserve": stale["diagnostic_selectors"][selector]["current"],
        }
    return values


def _validate_baseline_reproduction(value, baseline_root, tolerance):
    path = Path(baseline_root) / "checkpoints" / f"{value['group_id']}.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    for stage in (
        "historical",
        "stale_sequence_historical",
        "stable_reobserve",
        "stale_reobserve",
    ):
        if value[stage]["answers"] != baseline[stage]["answers"]:
            raise ValueError(f"{stage} 未复现原 Gate 7 答案")
        for role in ("target", "donor"):
            if not np.allclose(
                value[stage][role]["world"],
                baseline[stage][role]["world"],
                atol=tolerance,
                rtol=0,
            ):
                raise ValueError(f"{stage}/{role} 未在容差内复现原中心点")


def _load_model(cut3r_root, checkpoint, device):
    import torch

    cut3r_root = Path(cut3r_root).resolve()
    sys.path.insert(0, str(cut3r_root))
    from add_ckpt_path import add_path_to_dust3r

    add_path_to_dust3r(str(checkpoint))
    device = torch.device(device)
    from src.dust3r.model import ARCroco3DStereo

    if device.type == "cpu":
        from models.curope.curope2d import cuRoPE2D, cuRoPE2D_func

        def cpu_rope_forward(module, tokens, positions):
            # 官方 attention 强制把 RoPE 输入转成 Half，但其 CPU kernel 只支持 Float。
            tokens = tokens.float()
            cuRoPE2D_func.apply(
                tokens.transpose(1, 2), positions, module.base, module.F0
            )
            return tokens

        cuRoPE2D.forward = cpu_rope_forward

    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    started = time.perf_counter()
    model = ARCroco3DStereo.from_pretrained(str(checkpoint)).to(device)
    model.eval()
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    return model, time.perf_counter() - started


def run(args):
    if not os.environ.get("TMUX"):
        raise RuntimeError("Gate 7 CUT3R 推理只能在 tmux 中执行")
    if str(args.device).split(":", 1)[0] not in {"cpu", "cuda"}:
        raise ValueError("device 只支持 cpu 或 cuda")

    diagnostic_config = _load_diagnostic_config(args.diagnostic_config, "cut3r")
    if args.sequence_manifest is not None and diagnostic_config is not None:
        raise ValueError("diagnostic GT mode cannot use the sealed RGB-only manifest")
    if diagnostic_config is not None:
        _assert_diagnostic_output(args.output, diagnostic_config)
    checkpoint_sha256 = _sha256_file(args.checkpoint)
    if diagnostic_config is not None:
        expected_sha = diagnostic_config["backends"]["cut3r"][
            "checkpoint_sha256"
        ]
        if checkpoint_sha256 != expected_sha:
            raise ValueError("CUT3R 权重与诊断配置不一致")
    public_sha256 = _sha256_file(args.episodes)
    access_guard = None
    sequence_groups = None
    if args.sequence_manifest is not None:
        access_guard = _install_inference_guard(
            args.dataset_root, args.source_checkpoints
        )
        sequence_groups = _load_sequence_manifest(args.sequence_manifest)
        contexts = {}
    else:
        contexts = _source_contexts(args.source_checkpoints)
    groups = _public_groups(args.episodes, args.group_id, args.max_groups)
    if diagnostic_config is not None and args.group_id is None:
        pilot = diagnostic_config["pilot_group_ids"]
        rank = {group_id: index for index, group_id in enumerate(pilot)}
        groups.sort(key=lambda item: (rank.get(item[0], len(rank)), item[0]))
    checkpoint_root = args.output / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    prepared = []
    results = []
    for group_id, public, episode_count in groups:
        if sequence_groups is not None:
            context = None
            sequences = sequence_groups.get(group_id)
            if sequences is None:
                raise ValueError(f"RGB sequence manifest is missing group {group_id}")
        else:
            context = contexts.get(group_id)
            if context is None:
                raise ValueError(f"缺少 group {group_id} 的 Gate 6 context checkpoint")
            sequences = {
                "stable": _sequence_paths(
                    public, context, args.dataset_root, "risk_stable"
                ),
                "stale": _sequence_paths(
                    public, context, args.dataset_root, "risk_stale"
                ),
            }
        masks = (
            _diagnostic_mask_bundle(context, args.dataset_root)
            if diagnostic_config is not None
            else None
        )
        diagnostic_fingerprint = None
        mask_inputs = []
        if diagnostic_config is not None:
            for scenario, paths in sorted(masks.items()):
                for record in mask_input_records(paths):
                    mask_inputs.append({"scenario": scenario, **record})
            diagnostic_fingerprint = {
                "protocol_revision": diagnostic_config["protocol_revision"],
                "config_sha256": diagnostic_config["_sha256"],
                "adapter_version": diagnostic_config["backends"]["cut3r"][
                    "adapter_version"
                ],
                "selectors": diagnostic_config["selectors"],
                "confidence_quantile": diagnostic_config[
                    "confidence_quantile"
                ],
                "mask_inputs": mask_inputs,
            }
        fingerprint, inputs = _fingerprint(
            group_id,
            sequences,
            checkpoint_sha256,
            args.image_size,
            args.center_crop_fraction,
            args.grounding,
            {
                "saliency_quantile": args.saliency_quantile,
                "minimum_fraction": args.grounding_minimum_fraction,
                "maximum_fraction": args.grounding_maximum_fraction,
            },
            diagnostic_fingerprint,
        )
        path = checkpoint_root / f"{group_id}.json"
        existing = _valid_checkpoint(
            path, fingerprint, require_diagnostics=diagnostic_config is not None
        )
        if existing is not None:
            print(f"resume={group_id} 已通过 fingerprint 校验，跳过推理", flush=True)
            results.append(existing)
            continue
        prepared.append(
            (
                group_id,
                episode_count,
                context,
                sequences,
                masks,
                mask_inputs,
                fingerprint,
                inputs,
                path,
            )
        )

    model = None
    model_load_seconds = 0.0
    if prepared:
        model, model_load_seconds = _load_model(
            args.cut3r_root, args.checkpoint, args.device
        )
        print(f"model_load_seconds={model_load_seconds:.6f}", flush=True)

    for index, item in enumerate(prepared, start=1):
        (
            group_id,
            episode_count,
            context,
            sequences,
            masks,
            mask_inputs,
            fingerprint,
            inputs,
            path,
        ) = item
        print(
            f"group={group_id} progress={index}/{len(prepared)} start={_utc_now()}",
            flush=True,
        )
        base = {
            "schema_version": SCHEMA_VERSION,
            "adapter_version": diagnostic_config["backends"]["cut3r"][
                "adapter_version"
            ]
            if diagnostic_config is not None
            else ADAPTER_VERSION,
            "group_id": group_id,
            "episode_count": episode_count,
            "fingerprint": fingerprint,
            "checkpoint_sha256": checkpoint_sha256,
            "image_size": args.image_size,
            "center_crop_fraction": args.center_crop_fraction,
            "grounding": args.grounding,
            "input_mode": "rgb_only_verification_frame",
            "grounding_note": "只读取 RGB；不读取 depth、instance mask、GT bbox 或私有答案。",
            "inputs": inputs,
        }
        if diagnostic_config is not None:
            base.update(
                {
                    "diagnostic_only": True,
                    "qa_revealed": True,
                    "uses_gt_mask": True,
                    "eligible_as_main_result": False,
                    "protocol_revision": diagnostic_config["protocol_revision"],
                    "diagnostic_config_sha256": diagnostic_config["_sha256"],
                    "mask_inputs": mask_inputs,
                }
            )
        try:
            stable = _run_sequence(
                sequences["stable"],
                model,
                args.device,
                args.image_size,
                args.center_crop_fraction,
                args.grounding,
                {
                    "saliency_quantile": args.saliency_quantile,
                    "minimum_fraction": args.grounding_minimum_fraction,
                    "maximum_fraction": args.grounding_maximum_fraction,
                },
                masks["stable"] if masks is not None else None,
                diagnostic_config,
            )
            stale = _run_sequence(
                sequences["stale"],
                model,
                args.device,
                args.image_size,
                args.center_crop_fraction,
                args.grounding,
                {
                    "saliency_quantile": args.saliency_quantile,
                    "minimum_fraction": args.grounding_minimum_fraction,
                    "maximum_fraction": args.grounding_maximum_fraction,
                },
                masks["stale"] if masks is not None else None,
                diagnostic_config,
            )
            value = dict(base)
            value.update(
                {
                    "status": "success",
                    "completed_at": _utc_now(),
                    "historical": stable["historical"],
                    "stale_sequence_historical": stale["historical"],
                    "stable_reobserve": stable["current"],
                    "stale_reobserve": stale["current"],
                    "camera_trajectories": {
                        "stable": stable["camera_to_world"],
                        "stale": stale["camera_to_world"],
                    },
                    "timing": {
                        "stable_sequence_seconds": stable["elapsed_seconds"],
                        "stale_sequence_seconds": stale["elapsed_seconds"],
                        "total_seconds": stable["elapsed_seconds"]
                        + stale["elapsed_seconds"],
                        "frame_count": stable["frame_count"] + stale["frame_count"],
                    },
                    "peak_allocated_bytes": max(
                        stable["peak_allocated_bytes"], stale["peak_allocated_bytes"]
                    ),
                }
            )
            if diagnostic_config is not None:
                value["diagnostic_selectors"] = _combine_diagnostic_selectors(
                    stable, stale
                )
                _validate_baseline_reproduction(
                    value,
                    diagnostic_config["paths"]["cut3r_baseline_geometry"],
                    diagnostic_config["tolerances"]["center_point_absolute"],
                )
                value["baseline_reproduction_pass"] = True
            _archive_existing(path)
            _atomic_json(path, value)
            results.append(value)
            print(
                f"group={group_id} status=success seconds={value['timing']['total_seconds']:.6f}",
                flush=True,
            )
        except Exception as error:
            value = dict(base)
            value.update(
                {
                    "status": "failure",
                    "failed_at": _utc_now(),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                }
            )
            _atomic_json(path, value)
            results.append(value)
            print(
                f"group={group_id} status=failure error={type(error).__name__}: {error}",
                flush=True,
            )
            if not args.continue_on_error:
                break

    success = [value for value in results if value.get("status") == "success"]
    failures = [value for value in results if value.get("status") != "success"]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": diagnostic_config["backends"]["cut3r"][
            "adapter_version"
        ]
        if diagnostic_config is not None
        else ADAPTER_VERSION,
        "created_at": _utc_now(),
        "episodes_path": str(args.episodes),
        "episodes_sha256": public_sha256,
        "checkpoint_path": str(args.checkpoint),
        "checkpoint_sha256": checkpoint_sha256,
        "requested_group_count": len(groups),
        "success_group_count": len(success),
        "failure_group_count": len(failures),
        "complete": len(success) == len(groups) and not failures,
        "model_load_seconds_this_run": model_load_seconds,
        "sequence_manifest_path": str(args.sequence_manifest)
        if args.sequence_manifest is not None
        else None,
        "sequence_manifest_sha256": _sha256_file(args.sequence_manifest)
        if args.sequence_manifest is not None
        else None,
        "groups": [
            {
                "group_id": value["group_id"],
                "status": value["status"],
                "fingerprint": value["fingerprint"],
                "checkpoint": str(checkpoint_root / f"{value['group_id']}.json"),
            }
            for value in sorted(results, key=lambda item: item["group_id"])
        ],
    }
    if diagnostic_config is not None:
        manifest.update(
            {
                "diagnostic_only": True,
                "qa_revealed": True,
                "uses_gt_mask": True,
                "eligible_as_main_result": False,
                "protocol_revision": diagnostic_config["protocol_revision"],
                "diagnostic_config_sha256": diagnostic_config["_sha256"],
            }
        )
    _atomic_json(args.output / "manifest.json", manifest)
    if access_guard is not None:
        _atomic_json(
            args.output / "inference_access_audit.json",
            {
                "schema_version": 1,
                "guard": "python_audit_hook_v1",
                "guard_installed": True,
                "private_file_open_count": len(
                    access_guard.forbidden_open_attempts
                ),
                "forbidden_open_attempt_count": len(
                    access_guard.forbidden_open_attempts
                ),
                "forbidden_open_attempts": access_guard.forbidden_open_attempts,
                "sequence_manifest_sha256": _sha256_file(args.sequence_manifest),
            },
        )
    return manifest


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--source-checkpoints",
        type=Path,
        default=Path("data/episodes/spatial30/checkpoints"),
    )
    parser.add_argument(
        "--dataset-root", type=Path, default=Path("data/episodes/spatial30")
    )
    parser.add_argument("--sequence-manifest", type=Path)
    parser.add_argument(
        "--cut3r-root", type=Path, default=Path("external/cut3r")
    )
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--image-size", type=int, default=512)
    parser.add_argument("--center-crop-fraction", type=float, default=0.12)
    parser.add_argument(
        "--grounding", choices=("center_crop", "rgb_saliency_v1"), default="center_crop"
    )
    parser.add_argument("--saliency-quantile", type=float, default=0.82)
    parser.add_argument("--grounding-minimum-fraction", type=float, default=0.08)
    parser.add_argument("--grounding-maximum-fraction", type=float, default=0.30)
    parser.add_argument("--group-id")
    parser.add_argument("--max-groups", type=int)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--diagnostic-config", type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = run(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
