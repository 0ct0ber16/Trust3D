import inspect
import json

from trust3d.agents.online_episode import _build_units, run_online
from trust3d.data.build_branches import _episode_id, _group_id
from trust3d.eval.validate_online import validate


def _jsonl(path, values):
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def test_online_runner_api_cannot_receive_private_oracle():
    parameters = set(inspect.signature(run_online).parameters)

    assert not any("private" in name or "oracle" in name for name in parameters)


def test_environment_mapping_covers_public_without_private_records(tmp_path):
    candidate = {"candidate_id": "a" * 64}
    public = []
    for branch in ("fresh_stable", "risk_stable", "risk_stale"):
        for question_index in (0, 1):
            public.append(
                {
                    "episode_id": _episode_id(
                        candidate["candidate_id"], branch, 17, question_index
                    )
                }
            )
    units = _build_units(
        public,
        {"candidates": [candidate]},
        {
            "questions_per_branch": 2,
            "seed": 17,
            "excluded_group_ids": [],
        },
        {"group_ids": []},
        tmp_path,
    )

    assert len(units) == 3
    assert sum(len(item["public_records"]) for item in units) == 6


def test_gate5_validation_accepts_evidence_grounded_online_traces(tmp_path):
    group_id = _group_id("b" * 64)
    public = []
    private = []
    offline = []
    traces = []
    (tmp_path / "history.png").write_text("history")
    (tmp_path / "online.png").write_text("online")
    for question_index in (0, 1):
        for branch in ("fresh_stable", "risk_stable", "risk_stale"):
            episode_id = "e_{}_{}".format(question_index, branch)
            stale = branch == "risk_stale"
            reobserve = branch != "fresh_stable"
            current = stale
            public.append({"episode_id": episode_id, "group_id": group_id})
            private.append(
                {
                    "episode_id": episode_id,
                    "group_id": group_id,
                    "branch": branch,
                    "historical_answer": False,
                    "current_answer": current,
                    "memory_is_stale": stale,
                    "shortest_verification_cost": 3,
                }
            )
            offline.append(
                {
                    "episode_id": episode_id,
                    "policy_id": "trust3d_lambda_0.01",
                    "route": "reobserve" if reobserve else "trust_memory",
                    "predicted_answer": current,
                    "correct": True,
                }
            )
            traces.append(
                {
                    "episode_id": episode_id,
                    "group_id": group_id,
                    "selected_route": "REOBSERVE" if reobserve else "TRUST_MEMORY",
                    "movement_steps": 3 if reobserve else 0,
                    "movement_action_count": 3 if reobserve else 0,
                    "action_failure_count": 0,
                    "new_frame_ids": ["online.png"] if reobserve else [],
                    "invalidated_fact_ids": ["old"] if stale else [],
                    "answer": current,
                    "answer_evidence": [
                        "online.png" if reobserve else "history.png"
                    ],
                }
            )

    public_path = tmp_path / "public.jsonl"
    private_path = tmp_path / "private.jsonl"
    offline_path = tmp_path / "offline.jsonl"
    traces_path = tmp_path / "traces.jsonl"
    manifest_path = tmp_path / "manifest.json"
    _jsonl(public_path, public)
    _jsonl(private_path, private)
    _jsonl(offline_path, offline)
    _jsonl(traces_path, traces)
    manifest_path.write_text(
        json.dumps(
            {
                "completed_unit_count": 3,
                "expected_unit_count": 3,
                "pending_unit_count": 0,
            }
        )
    )

    report = validate(
        traces_path,
        public_path,
        private_path,
        offline_path,
        manifest_path,
        tmp_path / "validation.json",
        root=tmp_path,
    )

    assert report["acceptance"]["gate5_pass"] is True
    assert report["online_answer_accuracy"] == 1.0
    assert report["reobserve_rate"] == 2.0 / 3.0
