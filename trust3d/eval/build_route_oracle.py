"""Validate the sealed five-route counterfactual oracle contract."""

from __future__ import annotations

from collections import Counter
from typing import Any

from trust3d.agents.evidence import ROUTES


def validate_oracle(
    public_records: list[dict[str, Any]],
    private_records: list[dict[str, Any]],
    minimum_margin: float,
):
    public_by_id = {item["episode_id"]: item for item in public_records}
    private_by_id = {item["episode_id"]: item for item in private_records}
    if len(public_by_id) != len(public_records) or set(public_by_id) != set(private_by_id):
        raise ValueError("public/private oracle records do not match")
    signatures: dict[bytes, str] = {}
    support = Counter()
    ambiguous = 0
    near_tie = 0
    for episode_id, private in private_by_id.items():
        losses = private["route_losses"]
        if set(losses) != set(ROUTES) or any(value is None for value in losses.values()):
            raise ValueError(f"incomplete route loss table: {episode_id}")
        ordered = sorted(ROUTES, key=lambda route: losses[route])
        if ordered[0] != private["oracle_best_route"] or ordered[1] != private["second_best_route"]:
            raise ValueError(f"oracle ordering mismatch: {episode_id}")
        margin = losses[ordered[1]] - losses[ordered[0]]
        if abs(margin - private["route_loss_margin"]) > 1e-12:
            raise ValueError(f"oracle margin mismatch: {episode_id}")
        signature = repr(
            (
                public_by_id[episode_id]["predicate"],
                public_by_id[episode_id]["route_capabilities"],
                [(packet["source"], packet["valid_until"], packet["value"] is not None) for packet in public_by_id[episode_id]["evidence_packets"]],
            )
        ).encode("utf-8")
        if signature in signatures and signatures[signature] != ordered[0]:
            ambiguous += 1
        signatures[signature] = ordered[0]
        support[ordered[0]] += 1
        near_tie += margin < minimum_margin
    return {
        "complete": True,
        "group_count": len(private_records),
        "per_route_support": {route: support[route] for route in ROUTES},
        "publicly_ambiguous_count": ambiguous,
        "near_tie_count": near_tie,
    }
