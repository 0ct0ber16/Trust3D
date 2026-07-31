from __future__ import annotations

import itertools
import json

import pytest

from trust3d.data.build_five_route_v3_sources import (
    MVP_CANDIDATES,
    SPATIAL_CANDIDATES,
    plan as plan_fresh_sources,
)
from trust3d.eval.five_route_oracle_v3 import cost_scalar, validate_dataset
from trust3d.eval.five_route_statistics_v3 import (
    clopper_pearson_upper,
    scene_cluster_cost_bootstrap,
    tango_score_interval,
    tango_score_lower,
)
from trust3d.parallel_v2.common import ROOT, load_jsonl
from trust3d.parallel_v2.common import load_json
from trust3d.parallel_v3.five_route import (
    _contract_case,
    _online_source_costs,
    _private_access_guard,
    protocol,
)
from trust3d.parallel_v3.router import choose_route


def test_full_factor_router_contract():
    for values in itertools.product((False, True), repeat=5):
        public, expected = _contract_case(*values)
        assert choose_route(public, 0.05) == expected


def test_router_rejects_private_and_wrong_target():
    public, _ = _contract_case(False, True, False, False, False)
    public["private_answer"] = "secret"
    with pytest.raises(ValueError):
        choose_route(public, 0.05)

    public, _ = _contract_case(False, True, False, False, False)
    public["evidence_packets"][0]["object_id"] = "donor"
    assert choose_route(public, 0.05) != "USE_CURRENT_VIEW"


def test_independent_oracle_matches_development20():
    public = load_jsonl(
        ROOT / "data/episodes/parallel_v2/gt5/pilot_public.jsonl"
    )
    private = load_jsonl(
        ROOT / "data/episodes/parallel_v2/gt5/pilot_private.jsonl"
    )
    result = validate_dataset(public, private, protocol())
    assert result["group_count"] == 20
    assert set(result["per_route_support"].values()) == {4}
    assert result["stored_loss_mismatch_count"] == 0


def test_negative_cost_mutation_is_killed():
    value = {
        "move_steps": -1,
        "new_observations": 0,
        "vlm_calls": 0,
        "geometry_calls": 0,
        "wall_seconds": 0.0,
    }
    with pytest.raises(ValueError):
        cost_scalar(value, protocol())


def test_online_cost_ledger_uses_executed_round_zero_path(tmp_path):
    checkpoint = tmp_path / "source/checkpoints/candidate"
    checkpoint.mkdir(parents=True)
    (checkpoint / "risk_stale.round-0.json").write_text(
        json.dumps(
            {
                "episode_id": "episode-0",
                "branch": "risk_stale",
                "verification": {"cost": 7},
            }
        )
    )
    (checkpoint / "risk_stale.round-1.json").write_text(
        json.dumps(
            {
                "episode_id": "episode-1",
                "branch": "risk_stale",
                "verification": {"cost": 99},
            }
        )
    )

    costs = _online_source_costs({"sealed": tmp_path / "source"})

    assert costs == {
        "episode-0": ("sealed", 7),
        "episode-1": ("sealed", 7),
    }


def test_exact_and_paired_statistics_references():
    assert clopper_pearson_upper(0, 75) == pytest.approx(
        1.0 - 0.05 ** (1.0 / 75.0), abs=1e-12
    )
    symmetric = {"n11": 40, "n10": 10, "n01": 10, "n00": 40}
    lower, upper = tango_score_interval(symmetric)
    assert lower < 0 < upper
    assert lower == pytest.approx(-upper, abs=1e-7)
    pilot = {"n11": 0, "n10": 12, "n01": 0, "n00": 8}
    assert tango_score_lower(pilot) > 0


def test_scene_cluster_bootstrap_constant_reference():
    records = [
        {"scene_id": f"s{index}", "candidate_cost": 1.0, "baseline_cost": 2.0}
        for index in range(10)
    ]
    result = scene_cluster_cost_bootstrap(records, 1000, 20260731)
    assert result["point_estimate"] == pytest.approx(0.5)
    assert result["one_sided_95_lower"] == pytest.approx(0.5)


def test_private_path_guard_blocks_open():
    with _private_access_guard() as attempts:
        with pytest.raises(PermissionError):
            open("data/example_private.jsonl", "r", encoding="utf-8")
    assert attempts == ["data/example_private.jsonl"]


def test_fresh_source_plan_excludes_gate7_and_separates_pools():
    result = plan_fresh_sources()
    mvp = load_jsonl(MVP_CANDIDATES)
    spatial = load_json(SPATIAL_CANDIDATES)["candidates"]
    selected_ids = {item["candidate_id"] for item in mvp + spatial}
    selected_sources = {item["source_json"] for item in mvp + spatial}
    excluded = []
    legacy_ids = load_json(
        ROOT / "data/episodes/spatial30/selection.json"
    )["candidate_ids"]
    excluded.extend(legacy_ids)
    for split in ("pilot", "holdout"):
        value = load_json(
            ROOT
            / f"data/episodes/parallel_v2/gate7_fix/{split}_source_selection.json"
        )
        excluded.extend(item["candidate_id"] for item in value["candidates"])
        assert not selected_sources & {
            item["source_json"] for item in value["candidates"]
        }
    assert not selected_ids & set(excluded)
    assert len(mvp) == 40
    assert len(spatial) == 24
    assert result["fresh_source_overlap_count"] == 0
