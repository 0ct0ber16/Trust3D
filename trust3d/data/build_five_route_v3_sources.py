"""Plan and audit fresh source events for the GT five-route v3 replication set."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path
from typing import Any

from trust3d.parallel_v2.common import (
    ROOT,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    sha256_file,
    utc_now,
)


DATA = ROOT / "data/episodes/parallel_v3/gt5"
OUTPUT = ROOT / "outputs/parallel_v3/gt_five_route"
PROTOCOL_PATH = ROOT / "configs/gt_five_route_v3_protocol.json"
RAW_CANDIDATES = ROOT / "outputs/gate1/candidates.jsonl"
MVP_ROOT = DATA / "fresh_mvp"
SPATIAL_ROOT = DATA / "fresh_spatial"
MVP_CANDIDATES = DATA / "fresh_mvp_candidates.jsonl"
SPATIAL_CANDIDATES = DATA / "fresh_spatial_candidates.json"
EMPTY_EXCLUSIONS = DATA / "fresh_empty_exclusions.json"
SOURCE_PLAN = DATA / "fresh_source_plan.json"


def _protocol() -> dict[str, Any]:
    value = load_json(PROTOCOL_PATH)
    if value.get("protocol_revision") != "gt-five-route-v3":
        raise ValueError("unexpected v3 protocol revision")
    return value


def _stable(value: str, namespace: str) -> str:
    seed = int(_protocol()["seed"])
    return hashlib.sha256(f"{seed}|{namespace}|{value}".encode("utf-8")).hexdigest()


def _candidate_catalog() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    candidates = load_jsonl(RAW_CANDIDATES)
    by_id = {item["candidate_id"]: item for item in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("raw candidate IDs are not unique")
    return candidates, by_id


def _selection_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return list(load_json(path).get("candidates", []))


def _excluded_sources(
    by_id: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str], dict[str, int]]:
    excluded_ids: set[str] = set()
    excluded_source_json: set[str] = set()
    reasons: Counter[str] = Counter()

    def add(candidate: dict[str, Any], reason: str) -> None:
        if candidate["candidate_id"] not in excluded_ids:
            reasons[reason] += 1
        excluded_ids.add(candidate["candidate_id"])
        excluded_source_json.add(candidate["source_json"])

    legacy = load_json(ROOT / "data/episodes/spatial30/selection.json")
    for candidate_id in legacy["candidate_ids"]:
        add(by_id[candidate_id], "legacy_gate7")

    for split in ("pilot", "holdout"):
        path = ROOT / f"data/episodes/parallel_v2/gate7_fix/{split}_source_selection.json"
        for candidate in _selection_candidates(path):
            add(candidate, f"gate7_fix_{split}_pool")

    by_prefix = {candidate_id[:24]: item for candidate_id, item in by_id.items()}
    for layer in ("pilot", "final"):
        path = ROOT / f"data/episodes/parallel_v2/gt5/{layer}_public.jsonl"
        for item in load_jsonl(path):
            prefix = item["source_group_id"].split("_", 1)[-1]
            candidate = by_prefix.get(prefix)
            if candidate is not None:
                add(candidate, f"gt5_v2_{layer}")

    for path in (
        ROOT / "data/episodes/parallel_v2/integration/source_selection.json",
        ROOT / "data/episodes/parallel_v3/integration/source_selection.json",
    ):
        for candidate in _selection_candidates(path):
            add(candidate, "integration_pool")

    return excluded_ids, excluded_source_json, dict(sorted(reasons.items()))


def _round_robin_scenes(
    candidates: list[dict[str, Any]], count: int, namespace: str
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            _stable(item["scene"], namespace + "-scene"),
            _stable(item["candidate_id"], namespace + "-candidate"),
        ),
    )
    selected: list[dict[str, Any]] = []
    used_scenes: set[str] = set()
    used_sources: set[str] = set()
    for unique_scene_pass in (True, False):
        for item in ordered:
            if item["source_json"] in used_sources:
                continue
            if unique_scene_pass and item["scene"] in used_scenes:
                continue
            selected.append(item)
            used_scenes.add(item["scene"])
            used_sources.add(item["source_json"])
            if len(selected) == count:
                return selected
    raise RuntimeError(f"fresh source pool too small: {len(selected)}/{count}")


def plan() -> dict[str, Any]:
    protocol = _protocol()
    candidates, by_id = _candidate_catalog()
    excluded_ids, excluded_source_json, exclusion_reasons = _excluded_sources(by_id)
    eligible = [
        item
        for item in candidates
        if item["candidate_id"] not in excluded_ids
        and item["source_json"] not in excluded_source_json
    ]
    mvp_candidates = _round_robin_scenes(
        eligible, int(protocol["fresh_mvp_candidate_pool"]), "gt5-v3-fresh-mvp"
    )
    mvp_sources = {item["source_json"] for item in mvp_candidates}
    spatial_candidates = _round_robin_scenes(
        [item for item in eligible if item["source_json"] not in mvp_sources],
        int(protocol["fresh_spatial_candidate_pool"]),
        "gt5-v3-fresh-spatial",
    )

    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    atomic_jsonl(MVP_CANDIDATES, mvp_candidates)
    atomic_json(
        SPATIAL_CANDIDATES,
        {
            "schema_version": 1,
            "seed": protocol["seed"],
            "candidates": spatial_candidates,
        },
    )
    atomic_json(EMPTY_EXCLUSIONS, {"schema_version": 1, "group_ids": []})
    spatial_sources = {item["source_json"] for item in spatial_candidates}
    value = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "generated_at": utc_now(),
        "raw_candidate_count": len(candidates),
        "eligible_candidate_count": len(eligible),
        "excluded_candidate_count": len(excluded_ids),
        "excluded_source_json_count": len(excluded_source_json),
        "exclusion_reasons": exclusion_reasons,
        "fresh_mvp_candidate_count": len(mvp_candidates),
        "fresh_spatial_candidate_count": len(spatial_candidates),
        "fresh_source_overlap_count": len(mvp_sources & spatial_sources),
        "fresh_mvp_scene_count": len({item["scene"] for item in mvp_candidates}),
        "fresh_spatial_scene_count": len({item["scene"] for item in spatial_candidates}),
        "cross_pool_scene_overlap_count": len(
            {item["scene"] for item in mvp_candidates}
            & {item["scene"] for item in spatial_candidates}
        ),
        "raw_candidates_sha256": sha256_file(RAW_CANDIDATES),
        "mvp_candidates_sha256": sha256_file(MVP_CANDIDATES),
        "spatial_candidates_sha256": sha256_file(SPATIAL_CANDIDATES),
    }
    if value["fresh_source_overlap_count"]:
        raise AssertionError("fresh MVP and spatial source pools overlap")
    atomic_json(SOURCE_PLAN, value)
    atomic_json(OUTPUT / "fresh_source_plan.json", value)
    return value


def audit_generated() -> dict[str, Any]:
    plan_value = load_json(SOURCE_PLAN)
    mvp_public = load_jsonl(MVP_ROOT / "episodes_public.jsonl")
    spatial_public = load_jsonl(SPATIAL_ROOT / "episodes_public.jsonl")
    mvp_groups = {item["group_id"] for item in mvp_public}
    spatial_groups = {item["group_id"] for item in spatial_public}
    if mvp_groups & spatial_groups:
        raise AssertionError("generated MVP and spatial group IDs overlap")
    os.chmod(MVP_ROOT / "oracle_private.jsonl", 0o600)
    os.chmod(SPATIAL_ROOT / "oracle_private.jsonl", 0o600)
    result = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "complete": len(mvp_groups) >= 32 and len(spatial_groups) == 8,
        "checked_at": utc_now(),
        "fresh_mvp_group_count": len(mvp_groups),
        "fresh_spatial_group_count": len(spatial_groups),
        "cross_pool_group_overlap_count": len(mvp_groups & spatial_groups),
        "fresh_source_plan_sha256": sha256_file(SOURCE_PLAN),
        "fresh_mvp_public_sha256": sha256_file(MVP_ROOT / "episodes_public.jsonl"),
        "fresh_mvp_private_sha256": sha256_file(MVP_ROOT / "oracle_private.jsonl"),
        "fresh_mvp_manifest_sha256": sha256_file(MVP_ROOT / "manifest.json"),
        "fresh_spatial_public_sha256": sha256_file(SPATIAL_ROOT / "episodes_public.jsonl"),
        "fresh_spatial_private_sha256": sha256_file(SPATIAL_ROOT / "oracle_private.jsonl"),
        "fresh_spatial_manifest_sha256": sha256_file(SPATIAL_ROOT / "manifest.json"),
        "planned_mvp_candidate_count": plan_value["fresh_mvp_candidate_count"],
        "planned_spatial_candidate_count": plan_value["fresh_spatial_candidate_count"],
    }
    atomic_json(OUTPUT / "fresh_source_audit.json", result)
    if not result["complete"]:
        raise RuntimeError(
            "fresh source generation incomplete: "
            f"mvp={len(mvp_groups)}/32 spatial={len(spatial_groups)}/8"
        )
    return result
