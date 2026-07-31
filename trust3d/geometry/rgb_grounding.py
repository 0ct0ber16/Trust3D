"""Deterministic RGB-only saliency grounding without mask or bbox labels."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def saliency_box(
    path: str | Path,
    output_shape: tuple[int, int],
    quantile: float = 0.82,
    minimum_fraction: float = 0.08,
    maximum_fraction: float = 0.30,
):
    height, width = output_shape
    image = np.asarray(
        Image.open(path).convert("RGB").resize((width, height)), dtype=np.float32
    ) / 255.0
    gray = image.mean(axis=2)
    gradient_y, gradient_x = np.gradient(gray)
    chroma = image.max(axis=2) - image.min(axis=2)
    saliency = np.hypot(gradient_x, gradient_y) + 0.35 * chroma
    yy, xx = np.mgrid[:height, :width]
    center_prior = np.exp(
        -2.0
        * (
            ((xx - (width - 1) / 2) / max(width, 1)) ** 2
            + ((yy - (height - 1) / 2) / max(height, 1)) ** 2
        )
    )
    score = saliency * center_prior
    threshold = float(np.quantile(score, quantile))
    selected = score >= threshold
    weights = np.where(selected, score, 0.0)
    total = float(weights.sum())
    if not np.isfinite(total) or total <= 1e-12:
        center_x = (width - 1) / 2
        center_y = (height - 1) / 2
        confidence = 0.0
    else:
        center_x = float((weights * xx).sum() / total)
        center_y = float((weights * yy).sum() / total)
        confidence = float(np.clip(score[selected].mean() / (score.mean() + 1e-8), 0, 5) / 5)
    fraction = float(
        np.clip(selected.mean() ** 0.5, minimum_fraction, maximum_fraction)
    )
    half_width = max(1, int(round(width * fraction / 2)))
    half_height = max(1, int(round(height * fraction / 2)))
    x0 = max(0, int(round(center_x)) - half_width)
    x1 = min(width, int(round(center_x)) + half_width)
    y0 = max(0, int(round(center_y)) - half_height)
    y1 = min(height, int(round(center_y)) + half_height)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("RGB saliency produced an empty box")
    return {
        "box_xyxy": [x0, y0, x1, y1],
        "confidence": confidence,
        "selected_fraction": float(selected.mean()),
        "method": "rgb_saliency_v1",
    }
