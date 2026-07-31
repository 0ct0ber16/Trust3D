"""Build the sealed GT five-route pilot and final datasets."""

from __future__ import annotations

import argparse
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from trust3d.agents.evidence import ROUTES, validate_packet
from trust3d.parallel_v2.common import (
    ROOT,
    assert_public_record,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    protocol,
    sha256_file,
    utc_now,
)


def _group(values: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in values:
        grouped[value["group_id"]].append(value)
    for records in grouped.values():
        records.sort(key=lambda item: item["episode_id"])
    return grouped


def _stable_order(group_ids: list[str], seed: int, namespace: str) -> list[str]:
    return sorted(
        group_ids,
        key=lambda group_id: hashlib.sha256(
            f"{seed}|{namespace}|{group_id}".encode("ascii")
        ).hexdigest(),
    )


def _paired_groups(public_path: Path, private_path: Path):
    public = load_jsonl(public_path)
    private = load_jsonl(private_path)
    private_by_id = {item["episode_id"]: item for item in private}
    if len(private_by_id) != len(private) or {item["episode_id"] for item in public} != set(private_by_id):
        raise ValueError(f"public/private episode mismatch: {public_path}")
    public_groups = _group(public)
    private_groups = _group(private)
    if set(public_groups) != set(private_groups):
        raise ValueError(f"public/private group mismatch: {public_path}")
    return public_groups, private_groups


def _choose_pair(
    public_records: list[dict[str, Any]],
    private_records: list[dict[str, Any]],
    preferred_branch: str,
):
    public_by_id = {item["episode_id"]: item for item in public_records}
    candidates = [item for item in private_records if item.get("branch") == preferred_branch]
    if not candidates:
        candidates = private_records
    private = sorted(candidates, key=lambda item: item["episode_id"])[0]
    return public_by_id[private["episode_id"]], private


def _predicate(source: str, public: dict[str, Any]) -> str:
    if source == "mvp":
        return "attribute"
    question_type = public["question"]["type"]
    return "distance" if question_type in {"which_closer", "target_nearer"} else question_type


def _answer(source: str, private: dict[str, Any], historical: bool = False):
    if source == "mvp":
        return private["historical_answer" if historical else "current_answer"]
    return private["historical_answer_gt" if historical else "current_answer_gt"]


def _object_id(source: str, public: dict[str, Any]) -> str:
    if source == "mvp":
        return public["program"]["subject"]
    return public["question"]["target_object_id"]


def _packet(
    episode_id: str,
    predicate: str,
    object_id: str,
    source: str,
    value: Any,
    observed_at: int,
    valid_until: int,
    confidence: float,
    cost: dict[str, Any],
    source_episode_id: str,
):
    packet = {
        "schema_version": 1,
        "episode_id": episode_id,
        "query_id": episode_id,
        "object_id": object_id,
        "predicate": predicate,
        "value": value,
        "source": source,
        "observed_at": observed_at,
        "valid_until": valid_until,
        "reference_frame": "world" if source in {"history", "gt_3d"} else "current_egocentric",
        "pose_convention": "camera_to_world",
        "confidence": confidence,
        "is_observed": value is not None,
        "provenance": [{"source_episode_id": source_episode_id, "provider": "gt-five-route-v1"}],
        "cost": dict(cost),
    }
    validate_packet(packet)
    return packet


def _route_cost(config: dict[str, Any], route: str, source_public: dict[str, Any]):
    cost = dict(config["route_costs"][route])
    if route == "REOBSERVE":
        cost["move_steps"] = int(source_public.get("verification_cost", cost["move_steps"]))
    return cost


def _cost_loss(cost: dict[str, Any], protocol_value: dict[str, Any]) -> float:
    weights = protocol_value["cost_weights"]
    return sum(float(cost[key]) * float(weights[key]) for key in cost)


def _record(
    split: str,
    index: int,
    route: str,
    source: str,
    source_public: dict[str, Any],
    source_private: dict[str, Any],
    config: dict[str, Any],
    protocol_value: dict[str, Any],
):
    group_id = f"pv2_{split}_{index:03d}_{source_private['group_id']}"
    episode_id = f"pv2e_{split}_{index:03d}_{source_private['episode_id']}"
    predicate = _predicate(source, source_public)
    current_answer = _answer(source, source_private)
    historical_answer = _answer(source, source_private, historical=True)
    object_id = _object_id(source, source_public)
    query_time = 20
    packets = []
    capabilities = {"current_view": False, "reobserve": False}
    estimated_error = {name: 1.0 for name in ROUTES}
    availability = {name: False for name in ROUTES}
    availability["ABSTAIN"] = True
    route_answers = {name: None for name in ROUTES}
    route_answers["ABSTAIN"] = None

    if route == "USE_CURRENT_VIEW":
        capabilities["current_view"] = True
        availability["USE_CURRENT_VIEW"] = True
        route_answers["USE_CURRENT_VIEW"] = current_answer
        packets.append(
            _packet(
                episode_id,
                predicate,
                object_id,
                "current_view",
                current_answer,
                query_time,
                query_time,
                0.99,
                _route_cost(config, route, source_public),
                source_private["episode_id"],
            )
        )
        estimated_error["USE_CURRENT_VIEW"] = 0.01
    elif route == "RETRIEVE_HISTORY":
        availability["RETRIEVE_HISTORY"] = True
        route_answers["RETRIEVE_HISTORY"] = current_answer
        packets.append(
            _packet(
                episode_id,
                "attribute",
                object_id,
                "history",
                current_answer,
                19,
                40,
                0.99,
                _route_cost(config, route, source_public),
                source_private["episode_id"],
            )
        )
        estimated_error["RETRIEVE_HISTORY"] = 0.01
    elif route == "QUERY_3D_MEMORY":
        availability["QUERY_3D_MEMORY"] = True
        route_answers["QUERY_3D_MEMORY"] = current_answer
        packets.append(
            _packet(
                episode_id,
                predicate,
                object_id,
                "gt_3d",
                current_answer,
                19,
                40,
                0.99,
                _route_cost(config, route, source_public),
                source_private["episode_id"],
            )
        )
        estimated_error["QUERY_3D_MEMORY"] = 0.01
    elif route == "REOBSERVE":
        capabilities["reobserve"] = True
        availability["RETRIEVE_HISTORY"] = True
        availability["REOBSERVE"] = True
        route_answers["RETRIEVE_HISTORY"] = historical_answer
        route_answers["REOBSERVE"] = current_answer
        packets.append(
            _packet(
                episode_id,
                predicate,
                object_id,
                "history",
                historical_answer,
                0,
                5,
                0.99,
                _route_cost(config, "RETRIEVE_HISTORY", source_public),
                source_private["episode_id"],
            )
        )
        estimated_error["REOBSERVE"] = 0.01
    elif route != "ABSTAIN":
        raise ValueError(route)

    candidate_costs = {
        name: _route_cost(config, name, source_public) for name in ROUTES
    }
    route_losses = {}
    for name in ROUTES:
        if name == "ABSTAIN":
            route_losses[name] = float(protocol_value["cost_weights"]["abstain"])
        elif not availability[name]:
            route_losses[name] = 1.5
        else:
            error = 0.0 if route_answers[name] == current_answer else 1.0
            route_losses[name] = error + _cost_loss(candidate_costs[name], protocol_value)
    ordered = sorted(
        ROUTES,
        key=lambda name: (route_losses[name], protocol_value["route_tie_break"].index(name)),
    )
    margin = route_losses[ordered[1]] - route_losses[ordered[0]]
    if ordered[0] != route:
        raise AssertionError(f"constructed oracle mismatch: expected={route} actual={ordered[0]}")

    public = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "episode_id": episode_id,
        "group_id": group_id,
        "query_id": episode_id,
        "source_dataset": source,
        "source_group_id": source_private["group_id"],
        "source_episode_id": source_private["episode_id"],
        "split": source_public.get("split", "unknown"),
        "question": source_public.get("question", source_public.get("program")),
        "predicate": predicate,
        "object_id": object_id,
        "query_time": query_time,
        "required_facts": [predicate],
        "route_capabilities": capabilities,
        "candidate_costs": candidate_costs,
        "estimated_route_error": estimated_error,
        "evidence_packets": packets,
        "online_source": {
            "eligible": source == "mvp",
            "source_episode_id": source_private["episode_id"],
            "source_group_id": source_private["group_id"],
        },
    }
    assert_public_record(public)
    private = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "episode_id": episode_id,
        "group_id": group_id,
        "source_group_id": source_private["group_id"],
        "private_answer": current_answer,
        "route_answers": route_answers,
        "route_available": availability,
        "route_losses": route_losses,
        "oracle_best_route": ordered[0],
        "second_best_route": ordered[1],
        "route_loss_margin": margin,
        "publicly_ambiguous": False,
        "near_tie": margin < protocol_value["minimum_route_loss_margin"],
    }
    return public, private


def build(config_path: Path):
    config = load_json(config_path)
    protocol_value = protocol()
    seed = int(protocol_value["seed"])
    output = ROOT / config["output_root"]
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    mvp_public, mvp_private = _paired_groups(
        ROOT / config["mvp_public"], ROOT / config["mvp_private"]
    )
    spatial_public, spatial_private = _paired_groups(
        ROOT / config["spatial_public"], ROOT / config["spatial_private"]
    )
    mvp_ids = _stable_order(sorted(mvp_public), seed, "gt5-mvp")
    spatial_ids = _stable_order(sorted(spatial_public), seed, "gt5-spatial")
    counts = protocol_value["datasets"]
    split_counts = {
        "pilot": int(counts["gt5_pilot_groups"]),
        "final": int(counts["gt5_final_groups"]),
    }
    offsets = {"mvp": 0, "spatial": 0}
    manifests = {}
    all_sources: dict[str, set[str]] = {}
    branch_by_route = {
        "USE_CURRENT_VIEW": "risk_stable",
        "RETRIEVE_HISTORY": "fresh_stable",
        "QUERY_3D_MEMORY": "fresh_stable",
        "REOBSERVE": "risk_stale",
        "ABSTAIN": "risk_stale",
    }
    for split, count in split_counts.items():
        if count % len(ROUTES):
            raise ValueError("GT five-route split count must be divisible by five")
        per_route = count // len(ROUTES)
        records = []
        private_records = []
        source_ids: set[str] = set()
        index = 0
        for route in ROUTES:
            source = "spatial" if route == "QUERY_3D_MEMORY" else "mvp"
            source_ids_ordered = spatial_ids if source == "spatial" else mvp_ids
            public_groups = spatial_public if source == "spatial" else mvp_public
            private_groups = spatial_private if source == "spatial" else mvp_private
            selected = source_ids_ordered[offsets[source] : offsets[source] + per_route]
            if len(selected) != per_route:
                raise RuntimeError(f"insufficient {source} source groups")
            offsets[source] += per_route
            for source_group_id in selected:
                public_item, private_item = _choose_pair(
                    public_groups[source_group_id],
                    private_groups[source_group_id],
                    branch_by_route[route],
                )
                built_public, built_private = _record(
                    split,
                    index,
                    route,
                    source,
                    public_item,
                    private_item,
                    config,
                    protocol_value,
                )
                records.append(built_public)
                private_records.append(built_private)
                source_ids.add(source_group_id)
                index += 1
        if all_sources and source_ids & set.union(*all_sources.values()):
            raise AssertionError("pilot/final source groups overlap")
        all_sources[split] = source_ids
        public_path = output / f"{split}_public.jsonl"
        private_path = output / f"{split}_private.jsonl"
        atomic_jsonl(public_path, sorted(records, key=lambda item: item["episode_id"]))
        atomic_jsonl(private_path, sorted(private_records, key=lambda item: item["episode_id"]), mode=0o600)
        manifests[split] = {
            "group_count": len(records),
            "per_route_support": {
                route: sum(item["oracle_best_route"] == route for item in private_records)
                for route in ROUTES
            },
            "source_group_count": len(source_ids),
            "public_sha256": sha256_file(public_path),
            "private_sha256": sha256_file(private_path),
        }
    result = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "stage": "gt5_prepare",
        "complete": True,
        "generated_at": utc_now(),
        "config_sha256": sha256_file(config_path),
        "source_overlap_count": len(all_sources["pilot"] & all_sources["final"]),
        "splits": manifests,
    }
    prepare_path = ROOT / "outputs/parallel_v2/gt_five_route/prepare.json"
    atomic_json(prepare_path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/five_route_gt_v1.json"))
    args = parser.parse_args(argv)
    result = build(ROOT / args.config if not args.config.is_absolute() else args.config)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
