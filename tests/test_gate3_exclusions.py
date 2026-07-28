from trust3d.data.build_branches import _aggregate, _group_id


def test_aggregate_excludes_group_with_legacy_context_checkpoint(tmp_path):
    candidate_id = "candidate-1"
    manifest = _aggregate(
        tmp_path,
        [{"candidate_id": candidate_id}],
        {candidate_id: {"status": "complete"}},
        {},
        ("fresh_stable", "risk_stable", "risk_stale"),
        2,
        17,
        2,
        ("rgb", "depth", "instance"),
        {_group_id(candidate_id)},
    )

    assert manifest["complete_source_events"] == 0
    assert manifest["excluded_source_event_count"] == 1
