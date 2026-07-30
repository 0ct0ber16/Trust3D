"""在同一 dense geometry 上提取预注册的后验 grounding 摘要。"""

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image


SELECTORS = ("center_0.12", "gt_bbox", "gt_mask")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def role_mask_paths(context, dataset_root, scenario):
    """返回五帧序列中两个对象 stage 对应的 mask，绝不复用 query mask。"""
    root = Path(dataset_root)
    history = context["history"]["observations"]
    current = context["branches"][scenario]["observations"]
    return {
        "historical": {
            role: root / history[role]["masks"][role]
            for role in ("target", "donor")
        },
        "current": {
            role: root / current[role]["masks"][role]
            for role in ("target", "donor")
        },
    }


def mask_input_records(paths):
    records = []
    for stage, roles in sorted(paths.items()):
        for role, path in sorted(roles.items()):
            path = Path(path)
            if not path.is_file():
                raise FileNotFoundError(path)
            mask = np.load(path, allow_pickle=False)
            if mask.ndim != 2 or not np.any(mask):
                raise ValueError(f"mask 无效或为空: {path}")
            records.append(
                {
                    "stage": stage,
                    "role": role,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    "shape": [int(value) for value in mask.shape],
                    "nonzero_pixels": int(np.count_nonzero(mask)),
                }
            )
    return records


def resize_mask(mask, shape):
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2 or not np.any(mask):
        raise ValueError("GT mask 必须是非空二维数组")
    height, width = (int(shape[0]), int(shape[1]))
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    return np.asarray(
        image.resize((width, height), Image.Resampling.NEAREST), dtype=np.uint8
    ) > 0


def _center_region(shape, fraction):
    height, width = shape
    side = max(3, int(round(min(height, width) * fraction)))
    y0 = max(0, height // 2 - side // 2)
    x0 = max(0, width // 2 - side // 2)
    return x0, y0, min(width, x0 + side), min(height, y0 + side)


def _bbox(mask):
    rows, columns = np.nonzero(mask)
    if rows.size == 0:
        raise ValueError("映射后的 GT mask 为空")
    return (
        int(columns.min()),
        int(rows.min()),
        int(columns.max()) + 1,
        int(rows.max()) + 1,
    )


def _region_mask(shape, selector, gt_mask, fraction):
    selected = np.zeros(shape, dtype=bool)
    if selector == "center_0.12":
        xyxy = _center_region(shape, fraction)
        x0, y0, x1, y1 = xyxy
        selected[y0:y1, x0:x1] = True
    elif selector == "gt_bbox":
        xyxy = _bbox(gt_mask)
        x0, y0, x1, y1 = xyxy
        selected[y0:y1, x0:x1] = True
    elif selector == "gt_mask":
        xyxy = _bbox(gt_mask)
        selected = gt_mask.copy()
    else:
        raise ValueError(f"未知 selector: {selector}")
    return selected, xyxy


def summarize_selector(
    points,
    confidence,
    original_mask,
    mask_path,
    selector,
    crop_fraction=0.12,
    confidence_quantile=0.5,
):
    points = np.asarray(points, dtype=np.float64)
    confidence = np.asarray(confidence, dtype=np.float64)
    if points.ndim != 3 or points.shape[-1] != 3:
        raise ValueError("points 必须是 HxWx3")
    if confidence.shape != points.shape[:2]:
        raise ValueError("confidence 与 points 空间尺寸不一致")
    mapped_mask = resize_mask(original_mask, points.shape[:2])
    region, xyxy = _region_mask(
        points.shape[:2], selector, mapped_mask, crop_fraction
    )
    finite = np.isfinite(points).all(axis=-1) & np.isfinite(confidence)
    eligible = finite & region
    if not np.any(eligible):
        raise ValueError(f"{selector} 没有有限点")
    threshold = float(np.quantile(confidence[eligible], confidence_quantile))
    selected = eligible & (confidence >= threshold)
    if np.count_nonzero(selected) < 3:
        selected = eligible
    selected_points = points[selected]
    point = np.median(selected_points, axis=0)
    mad = np.median(np.abs(selected_points - point), axis=0)
    if not np.isfinite(point).all():
        raise ValueError(f"{selector} 点摘要包含非有限值")
    region_pixels = int(np.count_nonzero(region))
    mask_pixels = int(np.count_nonzero(mapped_mask))
    overlap = int(np.count_nonzero(region & mapped_mask))
    return {
        "selector": selector,
        "world": [float(value) for value in point],
        "mad_xyz": [float(value) for value in mad],
        "selected_pixel_count": int(np.count_nonzero(selected)),
        "eligible_pixel_count": int(np.count_nonzero(eligible)),
        "confidence_median": float(np.median(confidence[selected])),
        "confidence_threshold": threshold,
        "selection_xyxy": [int(value) for value in xyxy],
        "mapped_shape": [int(value) for value in points.shape[:2]],
        "region_pixel_count": region_pixels,
        "mask_pixel_count": mask_pixels,
        "mask_purity": overlap / region_pixels if region_pixels else None,
        "mask_recall": overlap / mask_pixels if mask_pixels else None,
        "mask_path": str(mask_path),
        "mask_sha256": sha256_file(mask_path),
        "mask_original_shape": [int(value) for value in original_mask.shape],
        "mask_resize": "nearest",
    }


def summarize_stage(
    world_maps,
    confidences,
    role_indices,
    role_paths,
    crop_fraction=0.12,
    confidence_quantile=0.5,
):
    values = {selector: {} for selector in SELECTORS}
    for role in ("target", "donor"):
        index = role_indices[role]
        mask_path = Path(role_paths[role])
        mask = np.load(mask_path, allow_pickle=False)
        for selector in SELECTORS:
            values[selector][role] = summarize_selector(
                world_maps[index],
                confidences[index],
                mask,
                mask_path,
                selector,
                crop_fraction,
                confidence_quantile,
            )
    return values
