"""Pre-registered finite-sample statistics for GT five-route v3."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

import numpy as np
from scipy.optimize import brentq, minimize_scalar
from scipy.stats import beta, norm


def clopper_pearson_upper(events: int, trials: int, alpha: float = 0.05) -> float:
    """One-sided exact binomial upper confidence limit."""
    if trials <= 0 or not 0 <= events <= trials:
        raise ValueError("invalid binomial counts")
    if events == trials:
        return 1.0
    return float(beta.ppf(1.0 - alpha, events + 1, trials - events))


def paired_counts(
    candidate: Iterable[bool], baseline: Iterable[bool]
) -> dict[str, int]:
    pairs = list(zip(candidate, baseline))
    if not pairs:
        raise ValueError("paired sample is empty")
    return {
        "n11": sum(bool(a) and bool(b) for a, b in pairs),
        "n10": sum(bool(a) and not bool(b) for a, b in pairs),
        "n01": sum(not bool(a) and bool(b) for a, b in pairs),
        "n00": sum(not bool(a) and not bool(b) for a, b in pairs),
    }


def _constrained_discordance(delta: float, counts: dict[str, int]) -> float:
    """Constrained MLE of p10+p01 for a fixed paired difference."""
    epsilon = 1e-10
    lower = abs(delta) + epsilon
    upper = 1.0 - epsilon
    if lower >= upper:
        return min(max(abs(delta), epsilon), 1.0 - epsilon)
    n10 = counts["n10"]
    n01 = counts["n01"]
    concordant = counts["n11"] + counts["n00"]

    def negative_log_likelihood(value: float) -> float:
        terms = []
        if n10:
            terms.append(n10 * math.log((value + delta) / 2.0))
        if n01:
            terms.append(n01 * math.log((value - delta) / 2.0))
        if concordant:
            terms.append(concordant * math.log(1.0 - value))
        return -sum(terms)

    result = minimize_scalar(
        negative_log_likelihood,
        bounds=(lower, upper),
        method="bounded",
        options={"xatol": 1e-13},
    )
    if not result.success:
        raise RuntimeError("paired-score nuisance optimization failed")
    return float(result.x)


def tango_score_z(delta: float, counts: dict[str, int]) -> float:
    """Constrained multinomial score statistic used by Tango's paired CI."""
    total = sum(counts.values())
    if total <= 0 or not -1.0 < delta < 1.0:
        raise ValueError("invalid paired-score input")
    discordance = _constrained_discordance(delta, counts)
    denominator = discordance * discordance - delta * delta
    if denominator <= 0:
        return math.copysign(math.inf, counts["n10"] - counts["n01"] - total * delta)

    score = counts["n10"] / (discordance + delta) - counts["n01"] / (
        discordance - delta
    )
    info_dd = total * discordance / denominator
    info_ds = -total * delta / denominator
    info_ss = total * (discordance / denominator + 1.0 / (1.0 - discordance))
    efficient_info = info_dd - info_ds * info_ds / info_ss
    if efficient_info <= 0:
        return math.copysign(math.inf, score)
    return float(score / math.sqrt(efficient_info))


def tango_score_lower(
    counts: dict[str, int], confidence: float = 0.95
) -> float:
    """One-sided matched-pair Tango score lower confidence limit."""
    total = sum(counts.values())
    if total <= 0:
        raise ValueError("paired sample is empty")
    observed = (counts["n10"] - counts["n01"]) / total
    target = float(norm.ppf(confidence))
    left = -1.0 + 1e-8
    right = min(observed - 1e-10, 1.0 - 1e-8)
    if right <= left:
        return -1.0

    def objective(delta: float) -> float:
        return tango_score_z(delta, counts) - target

    grid = np.linspace(left, right, 2001)
    values = []
    for value in grid:
        try:
            values.append(objective(float(value)))
        except (ValueError, RuntimeError, ZeroDivisionError):
            values.append(math.nan)
    brackets = []
    for index in range(len(grid) - 1):
        first, second = values[index], values[index + 1]
        if math.isfinite(first) and math.isfinite(second) and first * second <= 0:
            brackets.append((float(grid[index]), float(grid[index + 1])))
    if not brackets:
        finite = [(abs(value), float(point)) for point, value in zip(grid, values) if math.isfinite(value)]
        if not finite:
            raise RuntimeError("paired-score lower bound could not be bracketed")
        return min(finite)[1]
    low, high = brackets[-1]
    return float(brentq(objective, low, high, xtol=1e-12, rtol=1e-12))


def tango_score_interval(
    counts: dict[str, int], confidence: float = 0.95
) -> tuple[float, float]:
    lower = tango_score_lower(counts, confidence)
    swapped = dict(counts)
    swapped["n10"], swapped["n01"] = counts["n01"], counts["n10"]
    upper = -tango_score_lower(swapped, confidence)
    return lower, upper


def scene_cluster_cost_bootstrap(
    records: Iterable[dict[str, Any]], resamples: int, seed: int
) -> dict[str, Any]:
    """Bootstrap scenes and retain all groups belonging to a sampled scene."""
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for record in records:
        grouped[str(record["scene_id"])].append(
            (float(record["candidate_cost"]), float(record["baseline_cost"]))
        )
    scenes = sorted(grouped)
    if not scenes:
        raise ValueError("cost bootstrap has no scenes")
    candidate_sum = np.asarray(
        [sum(value[0] for value in grouped[scene]) for scene in scenes], dtype=np.float64
    )
    baseline_sum = np.asarray(
        [sum(value[1] for value in grouped[scene]) for scene in scenes], dtype=np.float64
    )
    group_count = np.asarray([len(grouped[scene]) for scene in scenes], dtype=np.float64)
    if baseline_sum.sum() <= 0:
        raise ValueError("always-reobserve baseline cost is zero")

    point = 1.0 - candidate_sum.sum() / baseline_sum.sum()
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(scenes), size=(resamples, len(scenes)))
    sampled_candidate = candidate_sum[draws].sum(axis=1)
    sampled_baseline = baseline_sum[draws].sum(axis=1)
    sampled_groups = group_count[draws].sum(axis=1)
    if np.any(sampled_baseline <= 0) or np.any(sampled_groups <= 0):
        raise RuntimeError("invalid scene bootstrap sample")
    reductions = 1.0 - sampled_candidate / sampled_baseline
    return {
        "scene_count": len(scenes),
        "group_count": int(group_count.sum()),
        "point_estimate": float(point),
        "one_sided_95_lower": float(np.quantile(reductions, 0.05)),
        "two_sided_95": [float(value) for value in np.quantile(reductions, [0.025, 0.975])],
        "resamples": int(resamples),
        "seed": int(seed),
    }


def power_sensitivity(total: int = 100) -> dict[str, Any]:
    """Pre-evaluation sensitivity grid; it never changes the fixed sample size."""
    rows = []
    for positive_rate in (0.10, 0.20, 0.40, 0.60):
        for negative_rate in (0.00, 0.02, 0.05, 0.10):
            if positive_rate + negative_rate > 1.0:
                continue
            n10 = round(total * positive_rate)
            n01 = round(total * negative_rate)
            counts = {"n11": 0, "n10": n10, "n01": n01, "n00": total - n10 - n01}
            rows.append(
                {
                    "positive_discordance": positive_rate,
                    "negative_discordance": negative_rate,
                    "point_difference": (n10 - n01) / total,
                    "tango_one_sided_95_lower": tango_score_lower(counts),
                }
            )
    return {"fixed_total": total, "rows": rows}
