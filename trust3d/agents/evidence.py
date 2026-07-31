"""EvidencePacket validation and five-route public routing."""

from __future__ import annotations

import math
from typing import Any


ROUTES = (
    "USE_CURRENT_VIEW",
    "RETRIEVE_HISTORY",
    "QUERY_3D_MEMORY",
    "REOBSERVE",
    "ABSTAIN",
)
SOURCES = {"current_view", "history", "gt_3d", "rgb_3d", "reobserve"}
PREDICATES = {"left_right", "front_behind", "distance", "attribute"}


def validate_cost(cost: dict[str, Any]) -> None:
    expected = {
        "move_steps",
        "new_observations",
        "vlm_calls",
        "geometry_calls",
        "wall_seconds",
    }
    if set(cost) != expected:
        raise ValueError("invalid cost fields")
    if any(isinstance(cost[key], bool) or cost[key] < 0 for key in expected):
        raise ValueError("cost values must be non-negative")


def validate_packet(packet: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "episode_id",
        "query_id",
        "object_id",
        "predicate",
        "value",
        "source",
        "observed_at",
        "valid_until",
        "reference_frame",
        "pose_convention",
        "confidence",
        "is_observed",
        "provenance",
        "cost",
    }
    if set(packet) != required or packet["schema_version"] != 1:
        raise ValueError("invalid EvidencePacket schema")
    if packet["source"] not in SOURCES or packet["predicate"] not in PREDICATES:
        raise ValueError("invalid EvidencePacket enum")
    if packet["reference_frame"] not in {"world", "current_egocentric"}:
        raise ValueError("invalid reference frame")
    if packet["pose_convention"] != "camera_to_world":
        raise ValueError("pose convention must be camera_to_world")
    confidence = packet["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence is outside [0, 1]")
    if packet["value"] is None and packet["is_observed"]:
        raise ValueError("observed packet cannot have null value")
    validate_cost(packet["cost"])


def choose_route(public: dict[str, Any], max_error_probability: float = 0.05) -> str:
    packets = public.get("evidence_packets", [])
    for packet in packets:
        validate_packet(packet)
    now = int(public["query_time"])

    def usable(source: str) -> list[dict[str, Any]]:
        return [
            packet
            for packet in packets
            if packet["source"] == source
            and packet["is_observed"]
            and packet["value"] is not None
            and packet["valid_until"] >= now
            and 1.0 - packet["confidence"] <= max_error_probability
        ]

    if public["route_capabilities"].get("current_view") and usable("current_view"):
        return "USE_CURRENT_VIEW"
    requires_geometry = public.get(
        "requires_geometry_recompute", public["predicate"] != "attribute"
    )
    if not requires_geometry and usable("history"):
        return "RETRIEVE_HISTORY"
    if requires_geometry and (
        usable("gt_3d") or usable("rgb_3d")
    ):
        return "QUERY_3D_MEMORY"
    if (
        public["route_capabilities"].get("reobserve")
        and public["estimated_route_error"].get("REOBSERVE", 1.0)
        <= max_error_probability
    ):
        return "REOBSERVE"
    return "ABSTAIN"
