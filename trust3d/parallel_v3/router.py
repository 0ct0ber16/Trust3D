"""Frozen public-only five-route router used by the v3 confirmation."""

from __future__ import annotations

from typing import Any

from trust3d.agents.evidence import validate_packet


FORBIDDEN_PUBLIC_KEYS = {
    "branch",
    "current_answer",
    "memory_is_stale",
    "oracle_best_route",
    "private_answer",
    "route_losses",
}


def choose_route(public: dict[str, Any], max_error_probability: float) -> str:
    leaked = sorted(FORBIDDEN_PUBLIC_KEYS & set(public))
    if leaked:
        raise ValueError("private fields reached router: " + ", ".join(leaked))
    packets = public.get("evidence_packets", [])
    for packet in packets:
        validate_packet(packet)
    now = int(public["query_time"])
    required_object = public["object_id"]
    required_facts = set(public.get("required_facts", [public["predicate"]]))

    def usable(source: str) -> list[dict[str, Any]]:
        return [
            packet
            for packet in packets
            if packet["source"] == source
            and packet["object_id"] == required_object
            and packet["predicate"] in required_facts
            and packet["is_observed"]
            and packet["value"] is not None
            and packet["valid_until"] >= now
            and 1.0 - float(packet["confidence"])
            <= max_error_probability + 1e-12
        ]

    if public["route_capabilities"].get("current_view") and usable("current_view"):
        return "USE_CURRENT_VIEW"
    requires_geometry = public.get(
        "requires_geometry_recompute", public["predicate"] != "attribute"
    )
    if not requires_geometry and usable("history"):
        return "RETRIEVE_HISTORY"
    if requires_geometry and (usable("gt_3d") or usable("rgb_3d")):
        return "QUERY_3D_MEMORY"
    if (
        public["route_capabilities"].get("reobserve")
        and float(public["estimated_route_error"].get("REOBSERVE", 1.0))
        <= max_error_probability
    ):
        return "REOBSERVE"
    return "ABSTAIN"
