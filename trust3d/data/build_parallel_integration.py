"""Build an integration holdout disjoint from A, B, and legacy sources."""

from __future__ import annotations

import argparse
import hashlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from trust3d.agents.evidence import ROUTES
from trust3d.data.build_gate7_holdout import write_rgb_sequence_manifest
from trust3d.data.build_spatial import run_spatial
from trust3d.parallel_v2.common import (
    ROOT,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    protocol,
    sha256_file,
    utc_now,
)


OUTPUT = ROOT / "outputs/parallel_v2/integration"
DATA = ROOT / "data/episodes/parallel_v2/integration"
CONDITIONS = {
    "USE_CURRENT_VIEW": "current_rgb_visible",
    "RETRIEVE_HISTORY": "fresh_cached_fact",
    "QUERY_3D_MEMORY": "fresh_rgb_geometry",
    "REOBSERVE": "stale_reachable",
    "ABSTAIN": "no_safe_evidence",
}


def _stable(value: str):
    return hashlib.sha256(
        f"{protocol()['seed']}|integration|{value}".encode("utf-8")
    ).hexdigest()


def _used_candidate_metadata():
    mvp = load_json(ROOT / "data/episodes/mvp/selection.json")["candidates"]
    by_prefix = {item["candidate_id"][:24]: item for item in mvp}
    used = []
    legacy_ids = load_json(ROOT / "data/episodes/spatial30/selection.json")["candidate_ids"]
    by_id = {item["candidate_id"]: item for item in mvp}
    used.extend(by_id[candidate_id] for candidate_id in legacy_ids)
    for split in ("pilot", "holdout"):
        selection = load_json(
            ROOT / f"data/episodes/parallel_v2/gate7_fix/{split}/selection.json"
        )
        source = load_json(
            ROOT / f"data/episodes/parallel_v2/gate7_fix/{split}_source_selection.json"
        )
        source_by_id = {item["candidate_id"]: item for item in source["candidates"]}
        used.extend(source_by_id[candidate_id] for candidate_id in selection["candidate_ids"])
    for item in load_jsonl(
        ROOT / "data/episodes/parallel_v2/gt5/final_public.jsonl"
    ):
        prefix = item["source_group_id"].split("_", 1)[-1]
        if prefix in by_prefix:
            used.append(by_prefix[prefix])
    unique = {item["candidate_id"]: item for item in used}
    return list(unique.values())


def _candidate_pool():
    used = _used_candidate_metadata()
    used_sources = {item["source_json"] for item in used}
    used_scenes = {item["scene"] for item in used}
    candidates = {}
    for item in load_jsonl(ROOT / "outputs/gate1/candidates.jsonl"):
        required = {"candidate_id", "source_json", "scene", "action_index"}
        if not required <= set(item):
            continue
        if item["source_json"] in used_sources or item["scene"] in used_scenes:
            continue
        candidates[item["candidate_id"]] = item
    ordered = sorted(candidates.values(), key=lambda item: _stable(item["candidate_id"]))
    if len(ordered) < protocol()["datasets"]["integration_groups"]:
        raise RuntimeError("integration source/scene-disjoint candidate pool is too small")
    return ordered, used_sources, used_scenes


def prepare_selection():
    candidates, used_sources, used_scenes = _candidate_pool()
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    target = protocol()["datasets"]["integration_groups"]
    pool_size = min(len(candidates), max(target * 4, target + 20))
    atomic_json(
        DATA / "source_selection.json",
        {
            "schema_version": 1,
            "seed": protocol()["seed"],
            "candidates": candidates[:pool_size],
        },
    )
    atomic_json(DATA / "empty_exclusions.json", {"schema_version": 1, "group_ids": []})
    return {
        "candidate_pool_count": len(candidates),
        "selected_pool_count": pool_size,
        "excluded_source_count": len(used_sources),
        "excluded_scene_count": len(used_scenes),
    }


def _group(values):
    grouped = defaultdict(list)
    for value in values:
        grouped[value["group_id"]].append(value)
    return grouped


def _render_contract():
    source_public = load_jsonl(DATA / "source/episodes_public.jsonl")
    source_private = load_jsonl(DATA / "source/oracle_private.jsonl")
    public_groups = _group(source_public)
    private_groups = _group(source_private)
    if set(public_groups) != set(private_groups):
        raise ValueError("integration source public/private mismatch")
    private_by_episode = {item["episode_id"]: item for item in source_private}
    records = []
    private_records = []
    group_ids = sorted(public_groups, key=_stable)
    if len(group_ids) != protocol()["datasets"]["integration_groups"]:
        raise ValueError("integration group count mismatch")
    per_route = len(group_ids) // len(ROUTES)
    if per_route < 1 or per_route * len(ROUTES) != len(group_ids):
        raise ValueError("integration group count must be balanced across five routes")
    index = 0
    for route in ROUTES:
        for source_group_id in group_ids[index : index + per_route]:
            branch = "risk_stale" if route in {"REOBSERVE", "ABSTAIN"} else "fresh_stable"
            candidates = [
                item
                for item in public_groups[source_group_id]
                if private_by_episode[item["episode_id"]]["branch"] == branch
            ]
            source = sorted(candidates, key=lambda item: item["episode_id"])[0]
            oracle = private_by_episode[source["episode_id"]]
            group_id = f"iv2_{index:03d}_{source_group_id}"
            episode_id = f"iv2e_{index:03d}_{source['episode_id']}"
            question_type = source["question"]["type"]
            predicate = "distance" if question_type in {"which_closer", "target_nearer"} else question_type
            records.append(
                {
                    "schema_version": 1,
                    "protocol_revision": "parallel-v2",
                    "episode_id": episode_id,
                    "group_id": group_id,
                    "source_episode_id": source["episode_id"],
                    "source_group_id": source_group_id,
                    "question": source["question"],
                    "predicate": predicate,
                    "object_id": source["question"]["target_object_id"],
                    "query_time": 20,
                    "evidence_condition": CONDITIONS[route],
                    "requires_geometry_recompute": route == "QUERY_3D_MEMORY",
                    "route_capabilities": {
                        "current_view": route == "USE_CURRENT_VIEW",
                        "reobserve": route != "ABSTAIN",
                    },
                    "candidate_costs": load_json(ROOT / "configs/five_route_gt_v1.json")["route_costs"],
                    "source_public": source,
                }
            )
            private_records.append(
                {
                    "schema_version": 1,
                    "episode_id": episode_id,
                    "group_id": group_id,
                    "source_episode_id": source["episode_id"],
                    "private_answer": oracle["current_answer_gt"],
                    "oracle_best_route": route,
                    "source_branch": oracle["branch"],
                }
            )
            index += 1
    atomic_jsonl(DATA / "integration_public.jsonl", records)
    atomic_jsonl(DATA / "integration_private.jsonl", private_records, mode=0o600)
    return {
        "group_count": len(records),
        "per_route_support": {route: per_route for route in ROUTES},
        "public_sha256": sha256_file(DATA / "integration_public.jsonl"),
        "private_sha256": sha256_file(DATA / "integration_private.jsonl"),
    }


def build(max_new_contexts: Optional[int] = None):
    selection = prepare_selection()
    target = protocol()["datasets"]["integration_groups"]
    source_root = DATA / "source"
    report = run_spatial(
        DATA / "source_selection.json",
        ROOT / "external/alfred/data/json_2.1.0",
        DATA / "empty_exclusions.json",
        source_root,
        target_groups=target,
        seed=protocol()["seed"] + 3,
        max_new_contexts=max_new_contexts,
    )
    private_path = source_root / "oracle_private.jsonl"
    if private_path.is_file():
        os.chmod(private_path, 0o600)
    sequence_manifest = (
        write_rgb_sequence_manifest(source_root) if report["complete"] else None
    )
    contract = _render_contract() if report["complete"] else None
    value = {
        "schema_version": 1,
        "complete": report["complete"] and contract is not None,
        "generated_at": utc_now(),
        "selection": selection,
        "source": report,
        "rgb_sequence_manifest": sequence_manifest,
        "contract": contract,
    }
    atomic_json(OUTPUT / "prepare.json", value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["audit", "build"])
    parser.add_argument("--max-new-contexts", type=int)
    args = parser.parse_args(argv)
    if args.mode == "audit":
        candidates, sources, scenes = _candidate_pool()
        result = {
            "candidate_count": len(candidates),
            "excluded_source_count": len(sources),
            "excluded_scene_count": len(scenes),
        }
    else:
        result = build(args.max_new_contexts)
    print(result)
    return 0 if result.get("complete", True) else 3


if __name__ == "__main__":
    raise SystemExit(main())
