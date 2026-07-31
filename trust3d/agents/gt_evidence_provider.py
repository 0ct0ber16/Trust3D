"""Execute a selected route against sealed GT five-route records."""

from __future__ import annotations

from typing import Any

from trust3d.agents.evidence import ROUTES


def execute_route(
    public: dict[str, Any], private: dict[str, Any], route: str
) -> dict[str, Any]:
    if route not in ROUTES:
        raise ValueError(f"unsupported route: {route}")
    if public["episode_id"] != private["episode_id"]:
        raise ValueError("public/private episode mismatch")
    available = bool(private["route_available"].get(route))
    answer = private["route_answers"].get(route) if available else None
    return {
        "episode_id": public["episode_id"],
        "group_id": public["group_id"],
        "route": route,
        "answer": answer,
        "answered": answer is not None,
        "route_available": available,
        "cost": public["candidate_costs"][route],
    }
