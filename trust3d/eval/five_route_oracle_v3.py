"""Independent counterfactual oracle for the v3 GT five-route experiment."""

from __future__ import annotations

from collections import Counter
import math
from typing import Any, Iterable


ROUTES = (
    "USE_CURRENT_VIEW",
    "RETRIEVE_HISTORY",
    "QUERY_3D_MEMORY",
    "REOBSERVE",
    "ABSTAIN",
)
COST_FIELDS = (
    "move_steps",
    "new_observations",
    "vlm_calls",
    "geometry_calls",
    "wall_seconds",
)


def cost_scalar(cost: dict[str, Any], protocol: dict[str, Any]) -> float:
    if set(cost) != set(COST_FIELDS):
        raise ValueError("invalid route cost fields")
    if any(
        isinstance(cost[key], bool)
        or not math.isfinite(float(cost[key]))
        or float(cost[key]) < 0
        for key in COST_FIELDS
    ):
        raise ValueError("route costs must be finite and non-negative")
    return sum(float(cost[key]) * float(protocol["cost_weights"][key]) for key in COST_FIELDS)


def route_losses(
    public: dict[str, Any], private: dict[str, Any], protocol: dict[str, Any]
) -> dict[str, float]:
    """Recompute all losses without importing the production router or v2 oracle."""
    if public["episode_id"] != private["episode_id"]:
        raise ValueError("public/private episode mismatch")
    if set(public["candidate_costs"]) != set(ROUTES):
        raise ValueError("candidate cost table is incomplete")
    if set(private["route_available"]) != set(ROUTES):
        raise ValueError("route availability table is incomplete")
    if set(private["route_answers"]) != set(ROUTES):
        raise ValueError("route answer table is incomplete")

    losses: dict[str, float] = {}
    for route in ROUTES:
        if route == "ABSTAIN":
            losses[route] = float(protocol["cost_weights"]["abstain"])
        elif not private["route_available"][route]:
            losses[route] = float(protocol["unavailable_route_loss"])
        else:
            answer_error = float(
                private["route_answers"][route] != private["private_answer"]
            )
            losses[route] = answer_error + cost_scalar(
                public["candidate_costs"][route], protocol
            )
    return losses


def oracle_for_record(
    public: dict[str, Any], private: dict[str, Any], protocol: dict[str, Any]
) -> dict[str, Any]:
    losses = route_losses(public, private, protocol)
    tie_break = {route: index for index, route in enumerate(protocol["route_tie_break"])}
    ordered = sorted(ROUTES, key=lambda route: (losses[route], tie_break[route]))
    return {
        "oracle_best_route": ordered[0],
        "second_best_route": ordered[1],
        "route_loss_margin": losses[ordered[1]] - losses[ordered[0]],
        "route_losses": losses,
    }


def execute_route(
    public: dict[str, Any], private: dict[str, Any], route: str
) -> dict[str, Any]:
    if route not in ROUTES:
        raise ValueError(f"unsupported route: {route}")
    if public["episode_id"] != private["episode_id"]:
        raise ValueError("public/private episode mismatch")
    available = bool(private["route_available"][route])
    answer = private["route_answers"][route] if available else None
    return {
        "episode_id": public["episode_id"],
        "group_id": public["group_id"],
        "route": route,
        "answer": answer,
        "answered": answer is not None,
        "route_available": available,
        "cost": public["candidate_costs"][route],
    }


def validate_dataset(
    public_records: Iterable[dict[str, Any]],
    private_records: Iterable[dict[str, Any]],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    public = list(public_records)
    private = list(private_records)
    public_by_id = {item["episode_id"]: item for item in public}
    private_by_id = {item["episode_id"]: item for item in private}
    if len(public_by_id) != len(public) or set(public_by_id) != set(private_by_id):
        raise ValueError("public/private records are not one-to-one")

    support: Counter[str] = Counter()
    near_tie = 0
    stored_loss_mismatches = 0
    for episode_id in sorted(public_by_id):
        stored = private_by_id[episode_id]
        computed = oracle_for_record(public_by_id[episode_id], stored, protocol)
        support[computed["oracle_best_route"]] += 1
        near_tie += computed["route_loss_margin"] < float(
            protocol["minimum_route_loss_margin"]
        )
        if computed["oracle_best_route"] != stored["oracle_best_route"]:
            raise ValueError(f"stored oracle route mismatch: {episode_id}")
        if computed["second_best_route"] != stored["second_best_route"]:
            raise ValueError(f"stored second-best route mismatch: {episode_id}")
        if abs(computed["route_loss_margin"] - stored["route_loss_margin"]) > 1e-12:
            raise ValueError(f"stored route margin mismatch: {episode_id}")
        for route in ROUTES:
            if abs(computed["route_losses"][route] - stored["route_losses"][route]) > 1e-12:
                stored_loss_mismatches += 1
    if stored_loss_mismatches:
        raise ValueError(f"stored route loss mismatches: {stored_loss_mismatches}")
    return {
        "complete": True,
        "group_count": len(public),
        "per_route_support": {route: support[route] for route in ROUTES},
        "near_tie_count": near_tie,
        "stored_loss_mismatch_count": stored_loss_mismatches,
    }
