"""Build the source-disjoint replication40 and locked confirmatory100."""

from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path
from typing import Any

from trust3d.data.build_five_route import (
    _choose_pair,
    _paired_groups,
    _record,
    _reobserve_oracle_eligible,
    _stable_order,
)
from trust3d.eval.five_route_oracle_v3 import ROUTES, validate_dataset
from trust3d.parallel_v2.common import (
    ROOT,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    sha256_file,
    utc_now,
)


V2_DATA = ROOT / "data/episodes/parallel_v2/gt5"
V3_DATA = ROOT / "data/episodes/parallel_v3/gt5"
OUTPUT = ROOT / "outputs/parallel_v3/gt_five_route"
PROTOCOL_PATH = ROOT / "configs/gt_five_route_v3_protocol.json"
V2_CONFIG_PATH = ROOT / "configs/five_route_gt_v1.json"
FRESH_MVP = V3_DATA / "fresh_mvp"
FRESH_SPATIAL = V3_DATA / "fresh_spatial"


def _protocol() -> dict[str, Any]:
    value = load_json(PROTOCOL_PATH)
    if value.get("protocol_revision") != "gt-five-route-v3":
        raise ValueError("unexpected v3 protocol revision")
    return value


def _rewrite_v3_record(
    public: dict[str, Any], private: dict[str, Any], layer: str, scene_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    public = copy.deepcopy(public)
    private = copy.deepcopy(private)
    old_episode = public["episode_id"]
    old_group = public["group_id"]
    new_episode = old_episode.replace("pv2e_", "pv3e_", 1)
    new_group = old_group.replace("pv2_", "pv3_", 1)
    public.update(
        {
            "protocol_revision": "gt-five-route-v3",
            "episode_id": new_episode,
            "group_id": new_group,
            "query_id": new_episode,
            "dataset_layer": layer,
            "scene_id": scene_id,
        }
    )
    for packet in public["evidence_packets"]:
        packet["episode_id"] = new_episode
        packet["query_id"] = new_episode
        for provenance in packet["provenance"]:
            provenance["provider"] = "gt-five-route-v3"
    private.update(
        {
            "protocol_revision": "gt-five-route-v3",
            "episode_id": new_episode,
            "group_id": new_group,
            "dataset_layer": layer,
        }
    )
    return public, private


def _source_scene_map() -> dict[str, str]:
    mapping = {}
    for dataset, path in (
        ("mvp", ROOT / "data/episodes/mvp/episodes_public.jsonl"),
        ("spatial", ROOT / "data/episodes/spatial30/episodes_public.jsonl"),
        ("mvp", FRESH_MVP / "episodes_public.jsonl"),
        ("spatial", FRESH_SPATIAL / "episodes_public.jsonl"),
    ):
        for item in load_jsonl(path):
            mapping[item["episode_id"]] = str(
                item.get("scene") or f"{dataset}:{item['group_id']}"
            )
    return mapping


def _eligible_sealed(protocol: dict[str, Any]) -> dict[str, Any]:
    public_path = V2_DATA / "final_public.jsonl"
    private_path = V2_DATA / "final_private.jsonl"
    public_hash = sha256_file(public_path)
    private_hash = sha256_file(private_path)
    expected = protocol["sealed_hashes"]
    disallowed = [
        ROOT / "outputs/parallel_v2/gt_five_route/predictions.jsonl",
        ROOT / "outputs/parallel_v2/gt_five_route/metrics.json",
        ROOT / "outputs/parallel_v2/gt_five_route/router_lock.json",
        Path("/224010104/Jerry/checkpoints/parallel_v2/stages/gt5_offline.json"),
    ]
    unexpected = [str(path) for path in disallowed if path.exists()]
    eligible = (
        public_hash == expected["public"]
        and private_hash == expected["private"]
        and not unexpected
    )
    return {
        "decision": "eligible_unrevealed" if eligible else "downgraded_to_development",
        "eligible": eligible,
        "public_sha256": public_hash,
        "private_sha256": private_hash,
        "expected_public_sha256": expected["public"],
        "expected_private_sha256": expected["private"],
        "unexpected_reveal_artifacts": unexpected,
        "checked_at": utc_now(),
    }


def sealed_audit() -> dict[str, Any]:
    protocol = _protocol()
    result = {
        "schema_version": 1,
        "protocol_revision": protocol["protocol_revision"],
        "v2_report_status": load_json(
            ROOT / "outputs/parallel_v2/gt_five_route/report.json"
        )["status"],
        **_eligible_sealed(protocol),
    }
    atomic_json(OUTPUT / "sealed_audit.json", result)
    return result


def _select_replication_sources(
    used: dict[str, set[str]],
    source_public: dict[str, dict[str, list[dict[str, Any]]]],
    config: dict[str, Any],
    v2_protocol: dict[str, Any],
    protocol: dict[str, Any],
) -> dict[str, list[str]]:
    seed = int(protocol["seed"])
    ordered = {
        source: _stable_order(
            sorted(groups), seed, f"gt5-v3-replication-{source}"
        )
        for source, groups in source_public.items()
    }
    selected: dict[str, list[str]] = {}
    count = int(protocol["replication_groups_per_route"])
    # Reserve scarce re-observation-capable MVP groups before the other MVP routes.
    route_order = (
        "REOBSERVE",
        "USE_CURRENT_VIEW",
        "RETRIEVE_HISTORY",
        "ABSTAIN",
        "QUERY_3D_MEMORY",
    )
    for route in route_order:
        source = "spatial" if route == "QUERY_3D_MEMORY" else "mvp"
        candidates = [value for value in ordered[source] if value not in used[source]]
        if route == "REOBSERVE":
            candidates = [
                value
                for value in candidates
                if _reobserve_oracle_eligible(
                    source_public[source][value], config, v2_protocol
                )
            ]
        chosen = candidates[:count]
        if len(chosen) != count:
            raise RuntimeError(
                f"insufficient source-disjoint groups for {route}: {len(chosen)}/{count}"
            )
        selected[route] = chosen
        used[source].update(chosen)
    return selected


def build() -> dict[str, Any]:
    protocol = _protocol()
    audit = sealed_audit()
    if not audit["eligible"]:
        raise RuntimeError(
            "sealed60 is not eligible; a new 60-group source pool is required before evaluation"
        )
    V3_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    V3_DATA.chmod(0o700)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    v2_config = load_json(V2_CONFIG_PATH)
    v2_protocol = load_json(ROOT / "configs/parallel_v2_protocol.json")
    mvp_public, mvp_private = _paired_groups(
        FRESH_MVP / "episodes_public.jsonl", FRESH_MVP / "oracle_private.jsonl"
    )
    spatial_public, spatial_private = _paired_groups(
        FRESH_SPATIAL / "episodes_public.jsonl", FRESH_SPATIAL / "oracle_private.jsonl"
    )
    source_public = {"mvp": mvp_public, "spatial": spatial_public}
    source_private = {"mvp": mvp_private, "spatial": spatial_private}

    development_public = load_jsonl(V2_DATA / "pilot_public.jsonl")
    development_private = load_jsonl(V2_DATA / "pilot_private.jsonl")
    sealed_public_original = load_jsonl(V2_DATA / "final_public.jsonl")
    sealed_private_original = load_jsonl(V2_DATA / "final_private.jsonl")
    used = {"mvp": set(), "spatial": set()}

    selected = _select_replication_sources(
        used, source_public, v2_config, v2_protocol, protocol
    )
    branch_by_route = {
        "USE_CURRENT_VIEW": "risk_stable",
        "RETRIEVE_HISTORY": "fresh_stable",
        "QUERY_3D_MEMORY": "fresh_stable",
        "REOBSERVE": "risk_stale",
        "ABSTAIN": "risk_stale",
    }
    scenes = _source_scene_map()
    replication_public = []
    replication_private = []
    index = 0
    for route in ROUTES:
        source = "spatial" if route == "QUERY_3D_MEMORY" else "mvp"
        for source_group_id in selected[route]:
            source_public_item, source_private_item = _choose_pair(
                source_public[source][source_group_id],
                source_private[source][source_group_id],
                branch_by_route[route],
            )
            built_public, built_private = _record(
                "replication",
                index,
                route,
                source,
                source_public_item,
                source_private_item,
                v2_config,
                v2_protocol,
            )
            scene_id = scenes[source_private_item["episode_id"]]
            built_public, built_private = _rewrite_v3_record(
                built_public, built_private, "replication40", scene_id
            )
            replication_public.append(built_public)
            replication_private.append(built_private)
            index += 1

    sealed_public = []
    sealed_private = []
    sealed_private_by_id = {
        item["episode_id"]: item for item in sealed_private_original
    }
    for original in sealed_public_original:
        scene_id = scenes[original["source_episode_id"]]
        public_copy = copy.deepcopy(original)
        private_copy = copy.deepcopy(sealed_private_by_id[original["episode_id"]])
        public_copy["dataset_layer"] = "sealed60"
        public_copy["scene_id"] = scene_id
        private_copy["dataset_layer"] = "sealed60"
        sealed_public.append(public_copy)
        sealed_private.append(private_copy)

    confirmatory_public = sorted(
        sealed_public + replication_public, key=lambda item: item["episode_id"]
    )
    confirmatory_private = sorted(
        sealed_private + replication_private, key=lambda item: item["episode_id"]
    )
    validation = validate_dataset(confirmatory_public, confirmatory_private, protocol)
    expected_support = int(protocol["groups_per_route"])
    if validation["group_count"] != int(protocol["confirmatory_groups"]):
        raise AssertionError("confirmatory group count mismatch")
    if any(
        validation["per_route_support"][route] != expected_support for route in ROUTES
    ):
        raise AssertionError("confirmatory route support mismatch")

    atomic_jsonl(V3_DATA / "replication_public.jsonl", replication_public)
    atomic_jsonl(
        V3_DATA / "replication_private.jsonl", replication_private, mode=0o600
    )
    atomic_jsonl(V3_DATA / "confirmatory_public.jsonl", confirmatory_public)
    atomic_jsonl(
        V3_DATA / "confirmatory_private.jsonl", confirmatory_private, mode=0o600
    )
    development_manifest = {
        "public_path": "data/episodes/parallel_v2/gt5/pilot_public.jsonl",
        "private_path": "data/episodes/parallel_v2/gt5/pilot_private.jsonl",
        "group_count": len(development_public),
        "public_sha256": sha256_file(V2_DATA / "pilot_public.jsonl"),
        "private_sha256": sha256_file(V2_DATA / "pilot_private.jsonl"),
        "confirmatory_eligible": False,
    }
    atomic_json(V3_DATA / "development_manifest.json", development_manifest)
    lineage = [
        {
            "episode_id": item["episode_id"],
            "group_id": item["group_id"],
            "dataset_layer": item["dataset_layer"],
            "source_dataset": item["source_dataset"],
            "source_group_id": item["source_group_id"],
            "source_episode_id": item["source_episode_id"],
            "scene_id": item["scene_id"],
        }
        for item in confirmatory_public
    ]
    atomic_json(V3_DATA / "source_lineage.json", {"records": lineage})

    source_ids = [item["source_group_id"] for item in confirmatory_public]
    if len(source_ids) != len(set(source_ids)):
        raise AssertionError("confirmatory source groups overlap")
    development_ids = {item["source_group_id"] for item in development_public}
    sealed_ids = {item["source_group_id"] for item in sealed_public_original}
    replication_ids = {item["source_group_id"] for item in replication_public}
    layer_overlap = {
        "development_sealed": sorted(development_ids & sealed_ids),
        "development_replication": sorted(development_ids & replication_ids),
        "sealed_replication": sorted(sealed_ids & replication_ids),
    }
    if any(layer_overlap.values()):
        raise AssertionError(f"v3 dataset layers overlap: {layer_overlap}")
    replication_support = Counter(
        item["oracle_best_route"] for item in replication_private
    )
    result = {
        "schema_version": 1,
        "protocol_revision": protocol["protocol_revision"],
        "complete": True,
        "generated_at": utc_now(),
        "sealed_audit": audit,
        "development_manifest": development_manifest,
        "sealed_group_count": len(sealed_public),
        "replication_group_count": len(replication_public),
        "confirmatory_group_count": len(confirmatory_public),
        "replication_per_route_support": {
            route: replication_support[route] for route in ROUTES
        },
        "confirmatory_validation": validation,
        "source_overlap_count": len(source_ids) - len(set(source_ids)),
        "layer_source_overlap": layer_overlap,
        "fresh_source_plan_sha256": sha256_file(V3_DATA / "fresh_source_plan.json"),
        "fresh_source_audit_sha256": sha256_file(OUTPUT / "fresh_source_audit.json"),
        "fresh_mvp_manifest_sha256": sha256_file(FRESH_MVP / "manifest.json"),
        "fresh_spatial_manifest_sha256": sha256_file(FRESH_SPATIAL / "manifest.json"),
        "replication_public_sha256": sha256_file(
            V3_DATA / "replication_public.jsonl"
        ),
        "replication_private_sha256": sha256_file(
            V3_DATA / "replication_private.jsonl"
        ),
        "confirmatory_public_sha256": sha256_file(
            V3_DATA / "confirmatory_public.jsonl"
        ),
        "confirmatory_private_sha256": sha256_file(
            V3_DATA / "confirmatory_private.jsonl"
        ),
    }
    atomic_json(OUTPUT / "prepare.json", result)
    atomic_json(OUTPUT / "confirmatory100_lock.json", result)
    return result


def main() -> int:
    print(build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
