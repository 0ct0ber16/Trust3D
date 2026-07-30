"""用冻结 VGGT 从 Gate 7 的相同 RGB 序列恢复可续传几何。"""

import argparse
import hashlib
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

from trust3d.geometry.run_cut3r import (
    _public_groups,
    _sequence_paths,
    _source_contexts,
    centered_point,
    point_in_camera,
    spatial_answers,
)


SCHEMA_VERSION = 1


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
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


def _load_config(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "adapter_version",
        "repository_commit",
        "model_sha256",
        "model_file_bytes",
        "image_size",
        "image_preprocess",
        "dtype",
        "geometry_source",
        "center_crop_fraction",
        "diagnostic_crop_fractions",
        "confidence_quantile",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"Plan 2 配置缺少字段: {', '.join(missing)}")
    if value["image_size"] != 518 or value["image_preprocess"] != "pad":
        raise ValueError("VGGT 主配置必须固定为 518/pad")
    if value["dtype"] != "bfloat16":
        raise ValueError("VGGT 主配置必须固定为 bfloat16")
    if value["geometry_source"] != "depth_unprojection":
        raise ValueError("VGGT 主配置必须使用 depth_unprojection")
    return value


def _fingerprint(group_id, inputs, config, config_sha256, public_sha256, routes_sha256):
    payload = {
        "adapter_version": config["adapter_version"],
        "group_id": group_id,
        "repository_commit": config["repository_commit"],
        "checkpoint_sha256": config["model_sha256"],
        "config_sha256": config_sha256,
        "episodes_sha256": public_sha256,
        "routes_sha256": routes_sha256,
        "image_size": config["image_size"],
        "image_preprocess": config["image_preprocess"],
        "dtype": config["dtype"],
        "geometry_source": config["geometry_source"],
        "center_crop_fraction": config["center_crop_fraction"],
        "diagnostic_crop_fractions": config["diagnostic_crop_fractions"],
        "confidence_quantile": config["confidence_quantile"],
        "inputs": inputs,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _input_records(sequences):
    values = []
    for scenario, paths in sorted(sequences.items()):
        for index, path in enumerate(paths):
            values.append(
                {
                    "scenario": scenario,
                    "index": index,
                    "path": str(path),
                    "sha256": _sha256_file(path),
                }
            )
    return values


def _assert_cut3r_inputs(group_id, inputs, cut3r_geometry):
    path = Path(cut3r_geometry) / "checkpoints" / f"{group_id}.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("inputs") != inputs:
        raise ValueError(f"group {group_id} 的 VGGT 输入与 Gate 7 CUT3R 不一致")


def _valid_checkpoint(path, fingerprint):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    required = {
        "historical",
        "stale_sequence_historical",
        "stable_reobserve",
        "stale_reobserve",
        "camera_trajectories",
        "timing",
    }
    if (
        value.get("status") == "success"
        and value.get("fingerprint") == fingerprint
        and required <= set(value)
    ):
        return value
    return None


def _archive_existing(path):
    path = Path(path)
    if not path.exists():
        return
    archive = path.parent / "attempts" / path.stem
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.move(str(path), str(archive / f"{stamp}.json"))


def _load_model(vggt_root, checkpoint, device):
    import torch
    from safetensors.torch import load_file

    vggt_root = Path(vggt_root).resolve()
    sys.path.insert(0, str(vggt_root))
    from vggt.models.vggt import VGGT

    random.seed(20260730)
    np.random.seed(20260730)
    torch.manual_seed(20260730)
    started = time.perf_counter()
    model = VGGT(enable_point=False, enable_track=False)
    state = load_file(str(checkpoint), device="cpu")
    incompatible = model.load_state_dict(state, strict=False)
    missing = list(incompatible.missing_keys)
    unexpected = [
        key
        for key in incompatible.unexpected_keys
        if not key.startswith(("point_head.", "track_head."))
    ]
    del state
    if missing or unexpected:
        raise ValueError(
            f"VGGT 权重字段不兼容: missing={missing[:5]} unexpected={unexpected[:5]}"
        )
    model = model.to(torch.device(device)).eval()
    torch.cuda.synchronize()
    return model, time.perf_counter() - started


def _geometry(world_maps, confidences, poses, target_index, donor_index, fraction, quantile):
    target = centered_point(
        world_maps[target_index], confidences[target_index], fraction, quantile
    )
    donor = centered_point(
        world_maps[donor_index], confidences[donor_index], fraction, quantile
    )
    target["query_camera"] = point_in_camera(target["world"], poses[2])
    donor["query_camera"] = point_in_camera(donor["world"], poses[2])
    return {
        "target": target,
        "donor": donor,
        "query_camera_to_world": poses[2].tolist(),
        "answers": spatial_answers(target["query_camera"], donor["query_camera"]),
    }


def _run_sequence(paths, model, device, config):
    import torch

    from vggt.utils.geometry import (
        closed_form_inverse_se3,
        unproject_depth_map_to_point_map,
    )
    from vggt.utils.load_fn import load_and_preprocess_images
    from vggt.utils.pose_enc import pose_encoding_to_extri_intri

    images = load_and_preprocess_images(
        [str(path) for path in paths], mode=config["image_preprocess"]
    )
    if tuple(images.shape) != (5, 3, config["image_size"], config["image_size"]):
        raise ValueError(f"VGGT 输入 shape 不符合预期: {tuple(images.shape)}")
    images = images.to(torch.device(device), non_blocking=True)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    with torch.inference_mode():
        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            tokens, patch_start_idx = model.aggregator(images[None])
        pose_enc = model.camera_head(tokens)[-1]
        depth, depth_conf = model.depth_head(
            tokens, images=images[None], patch_start_idx=patch_start_idx
        )
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    peak_memory = int(torch.cuda.max_memory_allocated())
    extrinsic, intrinsic = pose_encoding_to_extri_intri(
        pose_enc, images.shape[-2:]
    )
    extrinsic = extrinsic.squeeze(0).float().cpu().numpy()
    intrinsic = intrinsic.squeeze(0).float().cpu().numpy()
    depth = depth.squeeze(0).float().cpu().numpy()
    confidence = depth_conf.squeeze(0).float().cpu().numpy()
    if not all(
        np.isfinite(value).all()
        for value in (extrinsic, intrinsic, depth, confidence)
    ):
        raise ValueError("VGGT 相机、深度或置信度包含非有限值")
    world_maps = unproject_depth_map_to_point_map(depth, extrinsic, intrinsic)
    poses = closed_form_inverse_se3(extrinsic)
    if not np.isfinite(world_maps).all() or not np.isfinite(poses).all():
        raise ValueError("VGGT 反投影世界点或相机位姿包含非有限值")

    quantile = config["confidence_quantile"]
    main_fraction = config["center_crop_fraction"]
    main = {
        "historical": _geometry(
            world_maps, confidence, poses, 0, 1, main_fraction, quantile
        ),
        "current": _geometry(
            world_maps, confidence, poses, 3, 4, main_fraction, quantile
        ),
    }
    diagnostics = {}
    for fraction in config["diagnostic_crop_fractions"]:
        diagnostics[str(fraction)] = {
            "historical": _geometry(
                world_maps, confidence, poses, 0, 1, fraction, quantile
            ),
            "current": _geometry(
                world_maps, confidence, poses, 3, 4, fraction, quantile
            ),
        }
    del images, tokens, pose_enc, depth_conf
    return {
        **main,
        "diagnostic_candidates": diagnostics,
        "camera_to_world": poses.tolist(),
        "camera_from_world": extrinsic.tolist(),
        "intrinsic": intrinsic.tolist(),
        "elapsed_seconds": elapsed,
        "peak_allocated_bytes": peak_memory,
        "frame_count": len(paths),
    }


def run(args):
    if not os.environ.get("TMUX"):
        raise RuntimeError("Plan 2 VGGT 只能在 tmux 中执行")
    config = _load_config(args.config)
    if Path(args.checkpoint).stat().st_size != config["model_file_bytes"]:
        raise ValueError("VGGT 权重大小与冻结配置不一致")
    checkpoint_sha256 = _sha256_file(args.checkpoint)
    if checkpoint_sha256 != config["model_sha256"]:
        raise ValueError("VGGT 权重 SHA256 与冻结配置不一致")
    config_sha256 = _sha256_file(args.config)
    public_sha256 = _sha256_file(args.episodes)
    routes_sha256 = _sha256_file(args.routes)
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
            "stale": _sequence_paths(
                public, context, args.dataset_root, "risk_stale"
            ),
        }
        inputs = _input_records(sequences)
        _assert_cut3r_inputs(group_id, inputs, args.cut3r_geometry)
        fingerprint = _fingerprint(
            group_id,
            inputs,
            config,
            config_sha256,
            public_sha256,
            routes_sha256,
        )
        path = checkpoint_root / f"{group_id}.json"
        existing = _valid_checkpoint(path, fingerprint)
        if existing is not None:
            print(f"resume={group_id} fingerprint 通过，跳过推理", flush=True)
            results.append(existing)
            continue
        prepared.append(
            (group_id, episode_count, sequences, fingerprint, inputs, path)
        )

    if not prepared:
        manifest_path = args.output / "manifest.json"
        if manifest_path.is_file():
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                existing_manifest.get("complete") is True
                and existing_manifest.get("requested_group_count") == len(groups)
                and existing_manifest.get("success_group_count") == len(groups)
            ):
                existing_manifest["model_load_seconds_this_run"] = 0.0
                print(json.dumps(existing_manifest, ensure_ascii=False, sort_keys=True))
                return existing_manifest
        manifest = _manifest(
            args,
            config,
            config_sha256,
            public_sha256,
            checkpoint_sha256,
            groups,
            results,
            0.0,
        )
        _atomic_json(args.output / "manifest.json", manifest)
        return manifest

    if str(args.device).split(":", 1)[0] != "cuda":
        raise RuntimeError("存在未完成 group 时禁止执行 VGGT CPU 推理")
    model, model_load_seconds = _load_model(
        args.vggt_root, args.checkpoint, args.device
    )
    print(f"model_load_seconds={model_load_seconds:.6f}", flush=True)
    for index, item in enumerate(prepared, start=1):
        group_id, episode_count, sequences, fingerprint, inputs, path = item
        _archive_existing(path)
        base = {
            "schema_version": SCHEMA_VERSION,
            "adapter_version": config["adapter_version"],
            "backend_id": "vggt",
            "repository_commit": config["repository_commit"],
            "group_id": group_id,
            "episode_count": episode_count,
            "fingerprint": fingerprint,
            "checkpoint_sha256": checkpoint_sha256,
            "config_sha256": config_sha256,
            "image_size": config["image_size"],
            "image_preprocess": config["image_preprocess"],
            "dtype": config["dtype"],
            "geometry_source": config["geometry_source"],
            "center_crop_fraction": config["center_crop_fraction"],
            "confidence_quantile": config["confidence_quantile"],
            "input_mode": "rgb_only_centered_verification_frame",
            "grounding_note": "只使用目标居中验证帧中心区域，不读取 GT 或私有答案。",
            "inputs": inputs,
        }
        print(
            f"group={group_id} progress={index}/{len(prepared)} start={_utc_now()}",
            flush=True,
        )
        try:
            stable = _run_sequence(sequences["stable"], model, args.device, config)
            stale = _run_sequence(sequences["stale"], model, args.device, config)
            diagnostic_candidates = {}
            for fraction in config["diagnostic_crop_fractions"]:
                key = str(fraction)
                diagnostic_candidates[key] = {
                    "historical": stable["diagnostic_candidates"][key]["historical"],
                    "stale_sequence_historical": stale["diagnostic_candidates"][key][
                        "historical"
                    ],
                    "stable_reobserve": stable["diagnostic_candidates"][key][
                        "current"
                    ],
                    "stale_reobserve": stale["diagnostic_candidates"][key][
                        "current"
                    ],
                }
            value = {
                **base,
                "status": "success",
                "completed_at": _utc_now(),
                "historical": stable["historical"],
                "stale_sequence_historical": stale["historical"],
                "stable_reobserve": stable["current"],
                "stale_reobserve": stale["current"],
                "diagnostic_candidates": diagnostic_candidates,
                "camera_trajectories": {
                    "stable": stable["camera_to_world"],
                    "stale": stale["camera_to_world"],
                },
                "camera_from_world": {
                    "stable": stable["camera_from_world"],
                    "stale": stale["camera_from_world"],
                },
                "intrinsics": {
                    "stable": stable["intrinsic"],
                    "stale": stale["intrinsic"],
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
                "private_file_open_count": 0,
            }
            _atomic_json(path, value)
            results.append(value)
            print(
                f"group={group_id} status=success seconds={value['timing']['total_seconds']:.6f}",
                flush=True,
            )
        except Exception as error:
            value = {
                **base,
                "status": "failure",
                "failed_at": _utc_now(),
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
            _atomic_json(path, value)
            results.append(value)
            print(
                f"group={group_id} status=failure error={type(error).__name__}: {error}",
                flush=True,
            )
            if not args.continue_on_error:
                break

    manifest = _manifest(
        args,
        config,
        config_sha256,
        public_sha256,
        checkpoint_sha256,
        groups,
        results,
        model_load_seconds,
    )
    _atomic_json(args.output / "manifest.json", manifest)
    return manifest


def _manifest(
    args,
    config,
    config_sha256,
    public_sha256,
    checkpoint_sha256,
    groups,
    results,
    model_load_seconds,
):
    success = [value for value in results if value.get("status") == "success"]
    failures = [value for value in results if value.get("status") != "success"]
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": config["adapter_version"],
        "backend_id": "vggt",
        "created_at": _utc_now(),
        "episodes_path": str(args.episodes),
        "episodes_sha256": public_sha256,
        "checkpoint_path": str(args.checkpoint),
        "checkpoint_sha256": checkpoint_sha256,
        "config_sha256": config_sha256,
        "requested_group_count": len(groups),
        "success_group_count": len(success),
        "failure_group_count": len(failures),
        "complete": len(success) == len(groups) and not failures,
        "model_load_seconds_this_run": model_load_seconds,
        "private_file_open_count": 0,
        "groups": [
            {
                "group_id": value["group_id"],
                "status": value["status"],
                "fingerprint": value["fingerprint"],
                "checkpoint": str(
                    args.output / "checkpoints" / f"{value['group_id']}.json"
                ),
            }
            for value in sorted(results, key=lambda item: item["group_id"])
        ],
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", required=True, type=Path)
    parser.add_argument("--routes", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--source-checkpoints",
        type=Path,
        default=Path("data/episodes/spatial30/checkpoints"),
    )
    parser.add_argument(
        "--dataset-root", type=Path, default=Path("data/episodes/spatial30")
    )
    parser.add_argument("--vggt-root", type=Path, default=Path("external/vggt"))
    parser.add_argument(
        "--cut3r-geometry", type=Path, default=Path("outputs/gate7/cut3r_geometry")
    )
    parser.add_argument("--device", default="cuda")
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
