import json

import numpy as np
import pytest
from PIL import Image

from trust3d.agents.run_episode import run
from trust3d.data.audit_risk_frames import audit
from trust3d.eval.evaluate_routes import evaluate
from trust3d.eval.metrics import calculate
from trust3d.eval.plots import plot


def _jsonl(path, values):
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def _config(path):
    value = {
        "global_ttl_steps": 15,
        "fact_ttl_steps": {"articulated_state": 10},
        "trust3d": {
            "fresh_error_probability": 0.0,
            "risk_error_probability": 0.5,
            "max_error_probability": 0.1,
            "error_penalty": 1.0,
            "primary_cost_weight": 0.01,
            "route_cost_weights": [0.001, 0.005, 0.01, 0.02, 0.05],
        },
        "utility_cost_weights": [0.001, 0.005, 0.01, 0.02, 0.05],
        "bootstrap_seed": 17,
    }
    path.write_text(json.dumps(value))
    return path


def _episodes(tmp_path, group_count=4):
    public = []
    private = []
    branches = ("fresh_stable", "risk_stable", "risk_stale")
    for group_index in range(group_count):
        historical_answer = bool(group_index % 2)
        for question_index in (0, 1):
            for branch in branches:
                episode_id = "e_{}_{}_{}".format(
                    group_index, question_index, branch
                )
                public.append(
                    {
                        "episode_id": episode_id,
                        "group_id": "g_{}".format(group_index),
                        "split": "valid_unseen",
                        "elapsed_steps": 0 if branch == "fresh_stable" else 30,
                        "verification_cost": 5,
                        "public_context": {
                            "intervention_window": branch != "fresh_stable"
                        },
                    }
                )
                stale = branch == "risk_stale"
                private.append(
                    {
                        "episode_id": episode_id,
                        "group_id": "g_{}".format(group_index),
                        "branch": branch,
                        "historical_answer": historical_answer,
                        "current_answer": (
                            not historical_answer if stale else historical_answer
                        ),
                        "memory_is_stale": stale,
                        "shortest_verification_cost": 5,
                    }
                )
    public_path = tmp_path / "public.jsonl"
    private_path = tmp_path / "private.jsonl"
    _jsonl(public_path, public)
    _jsonl(private_path, private)
    return public_path, private_path


def test_gate4_pipeline_passes_core_acceptance(tmp_path):
    public_path, private_path = _episodes(tmp_path)
    config_path = _config(tmp_path / "mvp.yaml")
    routes_path = tmp_path / "routes.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    metrics_path = tmp_path / "metrics.json"

    route_report = run(
        public_path,
        [
            "always_trust",
            "always_reobserve",
            "global_ttl",
            "fact_freshness",
            "trust3d",
        ],
        config_path,
        routes_path,
    )
    evaluation = evaluate(
        routes_path, private_path, predictions_path, include_clairvoyant=True
    )
    report = calculate(
        predictions_path,
        private_path,
        metrics_path,
        bootstrap_samples=200,
        config_path=config_path,
    )

    assert route_report["episode_count"] == 24
    assert evaluation["prediction_count"] == 240
    assert report["acceptance"]["gate4_pass"] is True
    assert report["comparisons"]["stale_error_reduction"] == 1.0
    assert report["comparisons"][
        "new_observation_reduction_vs_always_reobserve"
    ] == pytest.approx(1.0 / 3.0)


def test_router_rejects_private_oracle_field(tmp_path):
    public_path, _ = _episodes(tmp_path, group_count=1)
    records = [json.loads(line) for line in public_path.read_text().splitlines()]
    records[0]["memory_is_stale"] = True
    _jsonl(public_path, records)

    with pytest.raises(ValueError, match="禁止字段"):
        run(
            public_path,
            ["always_trust"],
            _config(tmp_path / "mvp.yaml"),
            tmp_path / "routes.jsonl",
        )


def test_risk_frame_audit_accepts_quantization_noise(tmp_path):
    rgb_stable = np.zeros((100, 100, 3), dtype=np.uint8)
    rgb_stale = rgb_stable.copy()
    rgb_stale[0, 0, 0] = 1
    depth = np.ones((100, 100), dtype=np.float32)
    instance = np.zeros((100, 100, 3), dtype=np.uint8)
    Image.fromarray(rgb_stable).save(str(tmp_path / "stable_rgb.png"))
    Image.fromarray(rgb_stale).save(str(tmp_path / "stale_rgb.png"))
    Image.fromarray(instance).save(str(tmp_path / "instance.png"))
    np.save(str(tmp_path / "depth.npy"), depth)

    public = []
    private = []
    for branch, prefix in (("risk_stable", "stable"), ("risk_stale", "stale")):
        episode_id = "e_{}".format(prefix)
        public.append(
            {
                "episode_id": episode_id,
                "query_observation": {
                    "rgb": "{}_rgb.png".format(prefix),
                    "depth": "depth.npy",
                    "instance": "instance.png",
                },
            }
        )
        private.append(
            {
                "episode_id": episode_id,
                "group_id": "g_1",
                "question_index": 0,
                "branch": branch,
                "target_visible_from_query": False,
            }
        )
    public_path = tmp_path / "public.jsonl"
    private_path = tmp_path / "private.jsonl"
    _jsonl(public_path, public)
    _jsonl(private_path, private)

    report = audit(public_path, private_path, tmp_path / "audit.json")

    assert report["risk_frame_audit_pass"] is True
    assert report["maxima"]["rgb_max_absolute_difference"] == 1.0


def test_plot_outputs_nonblank_png_files(tmp_path):
    public_path, private_path = _episodes(tmp_path)
    config_path = _config(tmp_path / "mvp.yaml")
    routes_path = tmp_path / "routes.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    metrics_path = tmp_path / "metrics.json"
    run(
        public_path,
        ["always_trust", "always_reobserve", "global_ttl", "fact_freshness", "trust3d"],
        config_path,
        routes_path,
    )
    evaluate(routes_path, private_path, predictions_path, include_clairvoyant=True)
    calculate(
        predictions_path,
        private_path,
        metrics_path,
        bootstrap_samples=20,
        config_path=config_path,
    )

    manifest = plot(metrics_path, tmp_path / "plots")

    assert len(manifest["plots"]) == 2
    for name in manifest["plots"]:
        with Image.open(str(tmp_path / "plots" / name)) as image:
            assert image.size == (1200, 800)
            assert len(image.getcolors(maxcolors=1000000)) > 5
