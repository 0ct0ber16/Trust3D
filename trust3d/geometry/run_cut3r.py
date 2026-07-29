"""仅用 RGB 流运行 CUT3R，并按 group 持久化可恢复几何结果。"""

import argparse
import hashlib
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from trust3d.data.select_events import read_jsonl


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
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


def _fingerprint(group_id, sequence_paths, checkpoint_sha256, image_size, crop_fraction):
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
        "inputs": inputs,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), inputs


def _valid_checkpoint(path, fingerprint):
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
    return value if required <= value.keys() else None


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


def _run_sequence(paths, model, device, image_size, crop_fraction):
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

    def geometry(target_index, donor_index):
        target = centered_point(
            world_maps[target_index], confidences[target_index], crop_fraction
        )
        donor = centered_point(
            world_maps[donor_index], confidences[donor_index], crop_fraction
        )
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
    del outputs, state_args, views
    return value


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

    checkpoint_sha256 = _sha256_file(args.checkpoint)
    public_sha256 = _sha256_file(args.episodes)
    contexts = _source_contexts(args.source_checkpoints)
    groups = _public_groups(args.episodes, args.group_id, args.max_groups)
    checkpoint_root = args.output / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    prepared = []
    results = []
    for group_id, public, episode_count in groups:
        context = contexts.get(group_id)
        if context is None:
            raise ValueError(f"缺少 group {group_id} 的 Gate 6 context checkpoint")
        sequences = {
            "stable": _sequence_paths(
                public, context, args.dataset_root, "risk_stable"
            ),
            "stale": _sequence_paths(public, context, args.dataset_root, "risk_stale"),
        }
        fingerprint, inputs = _fingerprint(
            group_id,
            sequences,
            checkpoint_sha256,
            args.image_size,
            args.center_crop_fraction,
        )
        path = checkpoint_root / f"{group_id}.json"
        existing = _valid_checkpoint(path, fingerprint)
        if existing is not None:
            print(f"resume={group_id} 已通过 fingerprint 校验，跳过推理", flush=True)
            results.append(existing)
            continue
        prepared.append(
            (group_id, episode_count, context, sequences, fingerprint, inputs, path)
        )

    model = None
    model_load_seconds = 0.0
    if prepared:
        model, model_load_seconds = _load_model(
            args.cut3r_root, args.checkpoint, args.device
        )
        print(f"model_load_seconds={model_load_seconds:.6f}", flush=True)

    for index, item in enumerate(prepared, start=1):
        group_id, episode_count, context, sequences, fingerprint, inputs, path = item
        print(
            f"group={group_id} progress={index}/{len(prepared)} start={_utc_now()}",
            flush=True,
        )
        base = {
            "schema_version": SCHEMA_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "group_id": group_id,
            "episode_count": episode_count,
            "fingerprint": fingerprint,
            "checkpoint_sha256": checkpoint_sha256,
            "image_size": args.image_size,
            "center_crop_fraction": args.center_crop_fraction,
            "input_mode": "rgb_only_centered_verification_frame",
            "grounding_note": "只使用目标居中验证帧的角色和中心区域，不读取 depth、instance mask 或私有答案。",
            "inputs": inputs,
        }
        try:
            stable = _run_sequence(
                sequences["stable"],
                model,
                args.device,
                args.image_size,
                args.center_crop_fraction,
            )
            stale = _run_sequence(
                sequences["stale"],
                model,
                args.device,
                args.image_size,
                args.center_crop_fraction,
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
        "adapter_version": ADAPTER_VERSION,
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
    _atomic_json(args.output / "manifest.json", manifest)
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
    parser.add_argument(
        "--cut3r-root", type=Path, default=Path("external/cut3r")
    )
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--image-size", type=int, default=512)
    parser.add_argument("--center-crop-fraction", type=float, default=0.12)
    parser.add_argument("--group-id")
    parser.add_argument("--max-groups", type=int)
    parser.add_argument("--continue-on-error", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = run(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
