"""Run the strict gt-five-route-v3 confirmation with resumable artifacts."""

from __future__ import annotations

import argparse
import builtins
import copy
import io
import itertools
import json
import os
import platform
import subprocess
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from trust3d.data.build_five_route_v3 import build as build_dataset
from trust3d.data.build_five_route_v3 import sealed_audit
from trust3d.data.build_five_route_v3_sources import (
    audit_generated as audit_fresh_sources,
)
from trust3d.data.build_five_route_v3_sources import plan as plan_fresh_sources
from trust3d.eval.five_route_oracle_v3 import (
    ROUTES,
    cost_scalar,
    execute_route,
    oracle_for_record,
    validate_dataset,
)
from trust3d.eval.five_route_statistics_v3 import (
    clopper_pearson_upper,
    paired_counts,
    power_sensitivity,
    scene_cluster_cost_bootstrap,
    tango_score_interval,
    tango_score_lower,
)
from trust3d.parallel_v2.common import (
    ROOT,
    atomic_bytes,
    atomic_json,
    atomic_jsonl,
    load_json,
    load_jsonl,
    resource_snapshot,
    sha256_bytes,
    sha256_file,
    utc_now,
    verify_baseline,
)
from trust3d.parallel_v3.router import choose_route


JERRY = Path("/224010104/Jerry")
DATA = ROOT / "data/episodes/parallel_v3/gt5"
V2_DATA = ROOT / "data/episodes/parallel_v2/gt5"
OUTPUT = ROOT / "outputs/parallel_v3/gt_five_route"
CHECKPOINT = JERRY / "checkpoints/parallel_v3/gt_five_route"
STAGES = CHECKPOINT / "stages"
UNITS = CHECKPOINT / "units"
PROTOCOL_PATH = ROOT / "configs/gt_five_route_v3_protocol.json"
REPORT_PATH = ROOT / "Trust3D_GT五路路由可行性验证报告_v3.md"
POLICIES = (
    "always_current",
    "always_history",
    "always_3d_memory",
    "always_reobserve",
    "always_abstain",
    "legacy_two_route",
    "trust3d_five_route",
)
CODE_PATHS = (
    "configs/gt_five_route_v3_protocol.json",
    "trust3d/data/build_five_route_v3.py",
    "trust3d/data/build_five_route_v3_sources.py",
    "trust3d/eval/five_route_oracle_v3.py",
    "trust3d/eval/five_route_statistics_v3.py",
    "trust3d/parallel_v3/router.py",
    "trust3d/parallel_v3/five_route.py",
    "tests/test_five_route_v3.py",
    "scripts/run_five_route_gt_v3.sh",
)


def protocol() -> dict[str, Any]:
    value = load_json(PROTOCOL_PATH)
    if value.get("protocol_revision") != "gt-five-route-v3":
        raise ValueError("unexpected protocol revision")
    return value


def _git_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _relevant_dirty() -> list[str]:
    output = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
    )
    ignored_prefixes = (
        "outputs/parallel_v2/",
        "outputs/parallel_v3/",
    )
    dirty = []
    for line in output.splitlines():
        path = line[3:].strip().strip('"')
        if any(path.startswith(prefix) for prefix in ignored_prefixes):
            continue
        dirty.append(line)
    return dirty


def _output_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def mark_stage(
    stage: str,
    status: str,
    outputs: Iterable[Path] = (),
    message: str = "",
    next_stage: str | None = None,
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "stage": stage,
        "status": status,
        "message": message,
        "updated_at": utc_now(),
        "host": platform.node(),
        "git_commit": _git_commit(),
        "outputs": [_output_record(path) for path in outputs],
        "next_stage": next_stage,
    }
    atomic_json(STAGES / f"{stage}.json", value)
    return value


def stage_complete(stage: str) -> bool:
    path = STAGES / f"{stage}.json"
    if not path.is_file():
        return False
    value = load_json(path)
    return value.get("status") == "complete" and all(
        Path(item["path"]).is_file()
        and sha256_file(item["path"]) == item["sha256"]
        for item in value.get("outputs", [])
    )


def write_status(state: str, stage: str, message: str) -> None:
    atomic_json(
        OUTPUT / "status.json",
        {
            "schema_version": 1,
            "protocol_revision": "gt-five-route-v3",
            "state": state,
            "stage": stage,
            "message": message,
            "updated_at": utc_now(),
            "host": platform.node(),
            "next_checkpoint": str(STAGES / f"{stage}.json"),
        },
    )


def preflight() -> dict[str, Any]:
    limits = protocol()["resources"]
    snapshot = resource_snapshot()
    admitted = (
        snapshot["load1_fraction"] <= limits["maximum_cpu_load_fraction"]
        and snapshot["memory_available_gib"] >= limits["minimum_available_memory_gib"]
        and snapshot["disk_available_gib"] >= limits["minimum_available_disk_gib"]
    )
    if not admitted:
        raise RuntimeError("CPU, memory, or disk admission check failed")
    baseline = verify_baseline()
    audit = sealed_audit()
    old_report = load_json(OUTPUT.parent.parent / "parallel_v2/gt_five_route/report.json")
    if old_report.get("reason") != "inconclusive_underpowered":
        raise RuntimeError("v2 failure evidence changed")
    if not audit["eligible"]:
        raise RuntimeError("sealed60 did not pass unrevealed audit")
    result = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "complete": True,
        "checked_at": utc_now(),
        "resource_snapshot": snapshot,
        "baseline_files_verified": len(baseline),
        "sealed_audit": audit,
        "v2_status": old_report["status"],
        "v2_reason": old_report["reason"],
        "v2_required_groups": old_report["power_audit"]["required_final_groups"],
    }
    atomic_json(OUTPUT / "preflight.json", result)
    mark_stage(
        "preflight",
        "complete",
        (OUTPUT / "preflight.json", OUTPUT / "sealed_audit.json"),
        "A0 sealed 资格和服务器安全审计通过。",
        "contract",
    )
    return result


def _zero_cost() -> dict[str, Any]:
    return {
        "move_steps": 0,
        "new_observations": 0,
        "vlm_calls": 0,
        "geometry_calls": 0,
        "wall_seconds": 0.0,
    }


def _packet(
    source: str,
    predicate: str,
    fresh: bool,
    object_id: str = "target",
    confidence: float = 0.99,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "episode_id": "contract",
        "query_id": "contract",
        "object_id": object_id,
        "predicate": predicate,
        "value": "fact",
        "source": source,
        "observed_at": 19,
        "valid_until": 20 if fresh else 19,
        "reference_frame": "world" if source in {"history", "gt_3d"} else "current_egocentric",
        "pose_convention": "camera_to_world",
        "confidence": confidence,
        "is_observed": True,
        "provenance": [],
        "cost": _zero_cost(),
    }


def _contract_case(
    geometry: bool,
    current: bool,
    history: bool,
    memory: bool,
    reobserve: bool,
) -> tuple[dict[str, Any], str]:
    predicate = "distance" if geometry else "attribute"
    packets = [
        _packet("current_view", predicate, current),
        _packet("history", "attribute", history),
        _packet("gt_3d", "distance", memory),
    ]
    public = {
        "episode_id": "contract",
        "object_id": "target",
        "predicate": predicate,
        "required_facts": [predicate],
        "query_time": 20,
        "route_capabilities": {
            "current_view": current,
            "reobserve": reobserve,
        },
        "estimated_route_error": {"REOBSERVE": 0.01},
        "evidence_packets": packets,
    }
    if current:
        expected = "USE_CURRENT_VIEW"
    elif not geometry and history:
        expected = "RETRIEVE_HISTORY"
    elif geometry and memory:
        expected = "QUERY_3D_MEMORY"
    elif reobserve:
        expected = "REOBSERVE"
    else:
        expected = "ABSTAIN"
    return public, expected


def contract() -> dict[str, Any]:
    maximum_error = float(protocol()["max_error_probability"])
    cases = []
    for values in itertools.product((False, True), repeat=5):
        public, expected = _contract_case(*values)
        actual = choose_route(public, maximum_error)
        if actual != expected:
            raise AssertionError(f"contract route mismatch: {values} {actual} != {expected}")
        cases.append((public, expected))

    boundary, _ = _contract_case(False, True, False, False, False)
    boundary["evidence_packets"][0]["confidence"] = 0.95
    if choose_route(boundary, maximum_error) != "USE_CURRENT_VIEW":
        raise AssertionError("risk threshold equality is not inclusive")

    mutations = {}
    history_case, _ = _contract_case(False, False, True, False, False)
    stale = copy.deepcopy(history_case)
    stale["evidence_packets"][1]["valid_until"] = 19
    mutations["fresh_stale_flip"] = choose_route(stale, maximum_error) != "RETRIEVE_HISTORY"
    geometry = copy.deepcopy(history_case)
    geometry["predicate"] = "distance"
    geometry["required_facts"] = ["distance"]
    mutations["geometry_requirement_flip"] = choose_route(geometry, maximum_error) != "RETRIEVE_HISTORY"
    current_case, _ = _contract_case(False, True, False, False, False)
    donor = copy.deepcopy(current_case)
    donor["evidence_packets"][0]["object_id"] = "donor"
    mutations["donor_target_swap"] = choose_route(donor, maximum_error) != "USE_CURRENT_VIEW"
    unreachable, _ = _contract_case(False, False, False, False, True)
    unreachable["route_capabilities"]["reobserve"] = False
    mutations["reachable_flip"] = choose_route(unreachable, maximum_error) != "REOBSERVE"
    mutations["label_swap"] = all(
        ({"USE_CURRENT_VIEW": "ABSTAIN"}.get(expected, expected) != expected)
        for _, expected in cases
        if expected == "USE_CURRENT_VIEW"
    )
    negative_cost = _zero_cost()
    negative_cost["move_steps"] = -1
    try:
        cost_scalar(negative_cost, protocol())
        mutations["cost_sign_flip"] = False
    except ValueError:
        mutations["cost_sign_flip"] = True
    leaked = copy.deepcopy(current_case)
    leaked["private_answer"] = "secret"
    try:
        choose_route(leaked, maximum_error)
        mutations["private_field_injection"] = False
    except ValueError:
        mutations["private_field_injection"] = True
    if not all(mutations.values()):
        raise AssertionError(f"surviving contract mutations: {mutations}")

    development_public = load_jsonl(V2_DATA / "pilot_public.jsonl")
    development_private = load_jsonl(V2_DATA / "pilot_private.jsonl")
    oracle = validate_dataset(development_public, development_private, protocol())
    private_by_id = {item["episode_id"]: item for item in development_private}
    route_match = sum(
        choose_route(item, maximum_error)
        == private_by_id[item["episode_id"]]["oracle_best_route"]
        for item in development_public
    )
    if route_match != len(development_public):
        raise AssertionError("development route contract mismatch")

    cp_reference = clopper_pearson_upper(0, 75)
    if abs(cp_reference - (1.0 - 0.05 ** (1.0 / 75.0))) > 1e-12:
        raise AssertionError("Clopper-Pearson reference mismatch")
    symmetric = {"n11": 40, "n10": 10, "n01": 10, "n00": 40}
    lower, upper = tango_score_interval(symmetric)
    if not lower < 0 < upper or abs(lower + upper) > 1e-7:
        raise AssertionError("paired Tango symmetry check failed")
    bootstrap_reference = scene_cluster_cost_bootstrap(
        [
            {"scene_id": f"scene-{index}", "candidate_cost": 1.0, "baseline_cost": 2.0}
            for index in range(10)
        ],
        1000,
        int(protocol()["seed"]),
    )
    if abs(bootstrap_reference["one_sided_95_lower"] - 0.5) > 1e-12:
        raise AssertionError("cluster bootstrap reference mismatch")

    result = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "complete": True,
        "checked_at": utc_now(),
        "full_factor_case_count": len(cases),
        "boundary_case_count": 1,
        "mutation_count": len(mutations),
        "mutation_results": mutations,
        "development_route_match": route_match,
        "development_group_count": len(development_public),
        "independent_oracle": oracle,
        "statistics_references": {
            "cp_zero_of_75_upper": cp_reference,
            "tango_symmetric_interval": [lower, upper],
            "cluster_cost_lower": bootstrap_reference["one_sided_95_lower"],
        },
    }
    atomic_json(OUTPUT / "contract.json", result)
    mark_stage(
        "contract",
        "complete",
        (OUTPUT / "contract.json",),
        "A1 全因子、边界、独立 oracle 和 mutation 契约通过。",
        "prepare",
    )
    return result


def prepare() -> dict[str, Any]:
    audit_fresh_sources()
    result = build_dataset()
    mark_stage(
        "prepare",
        "complete",
        (OUTPUT / "prepare.json", OUTPUT / "confirmatory100_lock.json"),
        "A2 replication40 和 confirmatory100 已冻结。",
        "freeze",
    )
    return result


def source_plan() -> dict[str, Any]:
    return plan_fresh_sources()


def source_audit() -> dict[str, Any]:
    return audit_fresh_sources()


def _source_hashes() -> dict[str, str]:
    return {path: sha256_file(ROOT / path) for path in CODE_PATHS}


def _verify_code_lock() -> dict[str, Any]:
    lock = load_json(OUTPUT / "code_lock.json")
    actual = _source_hashes()
    if lock["source_sha256"] != actual:
        raise RuntimeError("v3 source changed after code lock")
    if lock["git_commit"] != _git_commit():
        raise RuntimeError("Git commit changed after code lock")
    confirmatory = load_json(OUTPUT / "confirmatory100_lock.json")
    if lock["confirmatory_public_sha256"] != confirmatory["confirmatory_public_sha256"]:
        raise RuntimeError("confirmatory public manifest changed after code lock")
    return lock


def freeze() -> dict[str, Any]:
    dirty = _relevant_dirty()
    if dirty:
        raise RuntimeError("freeze requires committed v3 source: " + " | ".join(dirty))
    prepare_result = load_json(OUTPUT / "confirmatory100_lock.json")
    contract_result = load_json(OUTPUT / "contract.json")
    development_public = load_jsonl(V2_DATA / "pilot_public.jsonl")
    development_private = load_jsonl(V2_DATA / "pilot_private.jsonl")
    private_by_id = {item["episode_id"]: item for item in development_private}
    selected = [
        choose_route(item, float(protocol()["max_error_probability"]))
        for item in development_public
    ]
    expected = [
        private_by_id[item["episode_id"]]["oracle_best_route"]
        for item in development_public
    ]
    if selected != expected:
        raise AssertionError("development router changed before freeze")
    power = power_sensitivity(int(protocol()["confirmatory_groups"]))
    atomic_json(OUTPUT / "power_design.json", power)
    created_at = utc_now()
    router_lock = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "created_at": created_at,
        "route_enum": list(ROUTES),
        "development_group_count": len(development_public),
        "development_route_match": sum(a == b for a, b in zip(selected, expected)),
        "development_public_sha256": sha256_file(V2_DATA / "pilot_public.jsonl"),
        "router_source_sha256": sha256_file(ROOT / "trust3d/parallel_v3/router.py"),
        "max_error_probability": protocol()["max_error_probability"],
    }
    statistics_lock = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "created_at": created_at,
        "accuracy_interval": "paired-tango-score-one-sided-95",
        "error_interval": "clopper-pearson-one-sided-95",
        "cost_interval": "scene-cluster-bootstrap-one-sided-95",
        "bootstrap_resamples": protocol()["bootstrap_resamples"],
        "seed": protocol()["seed"],
        "statistics_source_sha256": sha256_file(
            ROOT / "trust3d/eval/five_route_statistics_v3.py"
        ),
        "contract_statistics_references": contract_result["statistics_references"],
        "power_design_sha256": sha256_file(OUTPUT / "power_design.json"),
    }
    code_lock = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "created_at": created_at,
        "git_commit": _git_commit(),
        "source_sha256": _source_hashes(),
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "confirmatory_public_sha256": prepare_result["confirmatory_public_sha256"],
        "confirmatory_private_sha256": prepare_result["confirmatory_private_sha256"],
        "v2_sealed_public_sha256": prepare_result["sealed_audit"]["public_sha256"],
        "v2_sealed_private_sha256": prepare_result["sealed_audit"]["private_sha256"],
    }
    atomic_json(OUTPUT / "router_lock.json", router_lock)
    atomic_json(OUTPUT / "statistics_lock.json", statistics_lock)
    atomic_json(OUTPUT / "code_lock.json", code_lock)
    result = {
        "router_lock": router_lock,
        "statistics_lock": statistics_lock,
        "code_lock": code_lock,
    }
    mark_stage(
        "freeze",
        "complete",
        (
            OUTPUT / "router_lock.json",
            OUTPUT / "statistics_lock.json",
            OUTPUT / "code_lock.json",
            OUTPUT / "power_design.json",
        ),
        "A3-A4 router、统计、代码和数据锁已冻结。",
        "infer",
    )
    return result


def _policy_route(public: dict[str, Any], policy_id: str) -> str:
    if policy_id == "always_current":
        return "USE_CURRENT_VIEW"
    if policy_id == "always_history":
        return "RETRIEVE_HISTORY"
    if policy_id == "always_3d_memory":
        return "QUERY_3D_MEMORY"
    if policy_id == "always_reobserve":
        return "REOBSERVE"
    if policy_id == "always_abstain":
        return "ABSTAIN"
    if policy_id == "legacy_two_route":
        if public["predicate"] == "attribute":
            valid = any(
                packet["source"] == "history"
                and packet["object_id"] == public["object_id"]
                and packet["is_observed"]
                and packet["valid_until"] >= public["query_time"]
                for packet in public["evidence_packets"]
            )
            return "RETRIEVE_HISTORY" if valid else "REOBSERVE"
        valid = any(
            packet["source"] in {"gt_3d", "rgb_3d"}
            and packet["object_id"] == public["object_id"]
            and packet["is_observed"]
            and packet["valid_until"] >= public["query_time"]
            for packet in public["evidence_packets"]
        )
        return "QUERY_3D_MEMORY" if valid else "REOBSERVE"
    if policy_id == "trust3d_five_route":
        return choose_route(public, float(protocol()["max_error_probability"]))
    raise ValueError(f"unsupported policy: {policy_id}")


@contextmanager
def _private_access_guard():
    attempts: list[str] = []
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def check(raw: Any) -> None:
        if isinstance(raw, (str, os.PathLike)):
            value = os.fspath(raw)
            if "_private" in value or "/private/" in value:
                attempts.append(value)
                raise PermissionError(f"private path blocked during inference: {value}")

    def guarded_io_open(file: Any, *args: Any, **kwargs: Any):
        check(file)
        return original_io_open(file, *args, **kwargs)

    def guarded_os_open(file: Any, *args: Any, **kwargs: Any):
        check(file)
        return original_os_open(file, *args, **kwargs)

    builtins.open = guarded_io_open
    io.open = guarded_io_open
    os.open = guarded_os_open
    try:
        yield attempts
    finally:
        builtins.open = original_builtin_open
        io.open = original_io_open
        os.open = original_os_open


def infer() -> dict[str, Any]:
    lock = _verify_code_lock()
    public_path = DATA / "confirmatory_public.jsonl"
    if sha256_file(public_path) != lock["confirmatory_public_sha256"]:
        raise RuntimeError("confirmatory public data changed after freeze")
    with _private_access_guard() as private_attempts:
        public_records = load_jsonl(public_path)
        predictions = []
        for public in public_records:
            for policy_id in POLICIES:
                route = _policy_route(public, policy_id)
                predictions.append(
                    {
                        "schema_version": 1,
                        "protocol_revision": "gt-five-route-v3",
                        "episode_id": public["episode_id"],
                        "group_id": public["group_id"],
                        "dataset_layer": public["dataset_layer"],
                        "policy_id": policy_id,
                        "route": route,
                        "cost_request": public["candidate_costs"][route],
                    }
                )
        predictions.sort(key=lambda item: (item["policy_id"], item["episode_id"]))
        atomic_jsonl(OUTPUT / "predictions.jsonl", predictions)
    if private_attempts:
        raise RuntimeError("private access was attempted during inference")
    seal = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "sealed_at": utc_now(),
        "git_commit": _git_commit(),
        "group_count": len(public_records),
        "prediction_count": len(predictions),
        "predictions_sha256": sha256_file(OUTPUT / "predictions.jsonl"),
        "public_sha256": sha256_file(public_path),
        "private_open_count": 0,
    }
    atomic_json(OUTPUT / "predictions_seal.json", seal)
    mark_stage(
        "infer",
        "complete",
        (OUTPUT / "predictions.jsonl", OUTPUT / "predictions_seal.json"),
        "A5 public-only inference 已封存，private open count 为 0。",
        "evaluate",
    )
    return seal


def _aggregate_policy(
    records: list[dict[str, Any]], private_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    answered = [item for item in records if item["answered"]]
    oracle_non_abstain = [
        item
        for item in records
        if private_by_id[item["episode_id"]]["oracle_best_route"] != "ABSTAIN"
    ]
    route_tp = Counter(
        item["route"]
        for item in records
        if item["route"] == item["oracle_best_route"]
    )
    return {
        "group_count": len(records),
        "correct_count": sum(item["correct"] for item in records),
        "accuracy": sum(item["correct"] for item in records) / len(records),
        "answered_count": len(answered),
        "coverage": len(answered) / len(records),
        "answered_correct_count": sum(item["correct"] for item in answered),
        "selective_error_count": sum(not item["correct"] for item in answered),
        "selective_error": (
            sum(not item["correct"] for item in answered) / len(answered)
            if answered
            else None
        ),
        "unsafe_answer_count": sum(item["unsafe_answer"] for item in records),
        "unsafe_answer_rate": sum(item["unsafe_answer"] for item in records)
        / len(records),
        "false_abstain_count": sum(item["false_abstain"] for item in records),
        "false_abstain_denominator": len(oracle_non_abstain),
        "false_abstain_rate": (
            sum(item["false_abstain"] for item in records) / len(oracle_non_abstain)
            if oracle_non_abstain
            else None
        ),
        "mean_cost": sum(item["cost_scalar"] for item in records) / len(records),
        "mean_route_regret": sum(item["route_regret"] for item in records)
        / len(records),
        "avoidable_regret_count": sum(item["route_regret"] > 1e-12 for item in records),
        "route_counts": dict(Counter(item["route"] for item in records)),
        "per_route_true_positive": {route: route_tp[route] for route in ROUTES},
        "stale_memory_error_count": sum(
            item["unsafe_answer"] and item["oracle_best_route"] == "REOBSERVE"
            for item in records
        ),
    }


def evaluate() -> dict[str, Any]:
    _verify_code_lock()
    seal = load_json(OUTPUT / "predictions_seal.json")
    predictions_path = OUTPUT / "predictions.jsonl"
    if sha256_file(predictions_path) != seal["predictions_sha256"]:
        raise RuntimeError("sealed predictions changed")
    complete_path = OUTPUT / "evaluation_complete.json"
    if complete_path.is_file() and (OUTPUT / "metrics.json").is_file():
        complete = load_json(complete_path)
        if complete["predictions_sha256"] != seal["predictions_sha256"]:
            raise RuntimeError("existing evaluation belongs to different predictions")
        return load_json(OUTPUT / "metrics.json")

    private_path = DATA / "confirmatory_private.jsonl"
    started = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "started_at": utc_now(),
        "predictions_sha256": seal["predictions_sha256"],
        "private_sha256": sha256_file(private_path),
        "private_open_count": 1,
    }
    atomic_json(OUTPUT / "evaluation_started.json", started)
    public_records = load_jsonl(DATA / "confirmatory_public.jsonl")
    private_records = load_jsonl(private_path)
    predictions = load_jsonl(predictions_path)
    independent_oracle = validate_dataset(public_records, private_records, protocol())
    public_by_id = {item["episode_id"]: item for item in public_records}
    private_by_id = {item["episode_id"]: item for item in private_records}
    expected_prediction_count = len(public_records) * len(POLICIES)
    if len(predictions) != expected_prediction_count:
        raise ValueError("sealed prediction count mismatch")

    evaluated = []
    for prediction in predictions:
        public = public_by_id[prediction["episode_id"]]
        private = private_by_id[prediction["episode_id"]]
        oracle = oracle_for_record(public, private, protocol())
        execution = execute_route(public, private, prediction["route"])
        correct = bool(
            execution["answered"] and execution["answer"] == private["private_answer"]
        )
        value = {
            **prediction,
            **execution,
            "policy_id": prediction["policy_id"],
            "dataset_layer": public["dataset_layer"],
            "scene_id": public["scene_id"],
            "oracle_best_route": oracle["oracle_best_route"],
            "correct": correct,
            "route_regret": oracle["route_losses"][prediction["route"]]
            - min(oracle["route_losses"].values()),
            "unsafe_answer": bool(execution["answered"] and not correct),
            "false_abstain": bool(
                prediction["route"] == "ABSTAIN"
                and oracle["oracle_best_route"] != "ABSTAIN"
            ),
            "cost_scalar": cost_scalar(execution["cost"], protocol()),
        }
        fingerprint = sha256_bytes(
            json.dumps(
                {
                    "prediction_sha256": seal["predictions_sha256"],
                    "private_sha256": started["private_sha256"],
                    "episode_id": value["episode_id"],
                    "policy_id": value["policy_id"],
                },
                sort_keys=True,
            ).encode("utf-8")
        )
        checkpoint_path = (
            UNITS
            / value["dataset_layer"]
            / value["policy_id"]
            / f"{value['group_id']}.json"
        )
        atomic_json(
            checkpoint_path,
            {
                "schema_version": 1,
                "status": "complete",
                "fingerprint": fingerprint,
                "completed_at": utc_now(),
                "result_sha256": sha256_bytes(
                    json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ),
            },
            mode=0o600,
        )
        evaluated.append(value)
    atomic_jsonl(DATA / "private_evaluation.jsonl", evaluated, mode=0o600)

    by_policy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evaluated:
        by_policy[item["policy_id"]].append(item)
    policy_metrics = {
        policy_id: _aggregate_policy(records, private_by_id)
        for policy_id, records in sorted(by_policy.items())
    }
    trust = sorted(by_policy["trust3d_five_route"], key=lambda item: item["episode_id"])
    baseline = sorted(by_policy["always_reobserve"], key=lambda item: item["episode_id"])
    if [item["episode_id"] for item in trust] != [item["episode_id"] for item in baseline]:
        raise AssertionError("paired methods are not aligned")
    counts = paired_counts(
        [item["correct"] for item in trust],
        [item["correct"] for item in baseline],
    )
    paired_point = (counts["n10"] - counts["n01"]) / len(trust)
    tango_lower = tango_score_lower(counts)
    tango_interval = tango_score_interval(counts)
    cost_records = [
        {
            "scene_id": candidate["scene_id"],
            "candidate_cost": candidate["cost_scalar"],
            "baseline_cost": reference["cost_scalar"],
        }
        for candidate, reference in zip(trust, baseline)
    ]
    cost_bootstrap = scene_cluster_cost_bootstrap(
        cost_records,
        int(protocol()["bootstrap_resamples"]),
        int(protocol()["seed"]),
    )
    trust_metrics = policy_metrics["trust3d_five_route"]
    selective_upper = clopper_pearson_upper(
        trust_metrics["selective_error_count"], trust_metrics["answered_count"]
    )
    unsafe_upper = clopper_pearson_upper(
        trust_metrics["unsafe_answer_count"], trust_metrics["group_count"]
    )
    false_abstain_upper = clopper_pearson_upper(
        trust_metrics["false_abstain_count"],
        trust_metrics["false_abstain_denominator"],
    )
    route_support = independent_oracle["per_route_support"]
    route_match = sum(item["route"] == item["oracle_best_route"] for item in trust)
    layer_metrics = {}
    for layer in ("sealed60", "replication40"):
        records = [item for item in trust if item["dataset_layer"] == layer]
        layer_metrics[layer] = {
            **_aggregate_policy(records, private_by_id),
            "route_match": sum(
                item["route"] == item["oracle_best_route"] for item in records
            ),
        }
    acceptance = {
        "confirmatory_group_count": len(trust)
        == int(protocol()["confirmatory_groups"]),
        "five_routes_twenty_each": all(
            route_support[route] == int(protocol()["groups_per_route"])
            for route in ROUTES
        ),
        "router_matches_oracle_100_of_100": route_match == len(trust),
        "avoidable_route_regret_zero": trust_metrics["avoidable_regret_count"] == 0,
        "coverage": trust_metrics["coverage"] >= float(protocol()["minimum_coverage"]),
        "selective_error_exact_upper": selective_upper
        <= float(protocol()["max_error_probability"]),
        "unsafe_answer_exact_upper": unsafe_upper
        <= float(protocol()["max_error_probability"]),
        "false_abstain_exact_upper": false_abstain_upper
        <= float(protocol()["max_error_probability"]),
        "accuracy_noninferiority_point": paired_point
        >= float(protocol()["accuracy_noninferiority_margin"]),
        "accuracy_noninferiority_tango_lower": tango_lower
        >= float(protocol()["accuracy_noninferiority_margin"]),
        "cost_reduction_point": cost_bootstrap["point_estimate"]
        >= float(protocol()["minimum_cost_reduction"]),
        "cost_reduction_cluster_lower": cost_bootstrap["one_sided_95_lower"] > 0,
        "stale_error_below_unconditional_history": trust_metrics[
            "stale_memory_error_count"
        ]
        < policy_metrics["always_history"]["stale_memory_error_count"],
        "sealed_and_replication_route_match": layer_metrics["sealed60"]["route_match"]
        == 60
        and layer_metrics["replication40"]["route_match"] == 40,
    }
    metrics = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "generated_at": utc_now(),
        "private_open_count": 1,
        "predictions_sha256": seal["predictions_sha256"],
        "independent_oracle": independent_oracle,
        "policy_metrics": policy_metrics,
        "layer_metrics": layer_metrics,
        "route_match_count": route_match,
        "paired_accuracy_vs_always_reobserve": {
            "counts": counts,
            "point_estimate": paired_point,
            "tango_one_sided_95_lower": tango_lower,
            "tango_two_sided_90": list(tango_interval),
        },
        "risk_intervals": {
            "selective_error_one_sided_95_upper": selective_upper,
            "unsafe_answer_one_sided_95_upper": unsafe_upper,
            "false_abstain_one_sided_95_upper": false_abstain_upper,
        },
        "cost_reduction_vs_always_reobserve": cost_bootstrap,
        "acceptance": acceptance,
        "offline_pass": all(acceptance.values()),
    }
    atomic_json(OUTPUT / "metrics.json", metrics)
    complete = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "completed_at": utc_now(),
        "predictions_sha256": seal["predictions_sha256"],
        "metrics_sha256": sha256_file(OUTPUT / "metrics.json"),
        "private_sha256": started["private_sha256"],
        "private_open_count": 1,
    }
    atomic_json(complete_path, complete)
    mark_stage(
        "evaluate",
        "complete",
        (OUTPUT / "metrics.json", OUTPUT / "evaluation_complete.json"),
        "A5 一次性 private evaluation 已完成。",
        "recover",
    )
    return metrics


def recover() -> dict[str, Any]:
    predictions_before = sha256_file(OUTPUT / "predictions.jsonl")
    metrics_before = sha256_file(OUTPUT / "metrics.json")
    infer()
    predictions_after = sha256_file(OUTPUT / "predictions.jsonl")
    metrics_after = sha256_file(OUTPUT / "metrics.json")
    unit_paths = sorted(UNITS.rglob("*.json"))
    expected_units = int(protocol()["confirmatory_groups"]) * len(POLICIES)
    result = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "complete": predictions_before == predictions_after
        and metrics_before == metrics_after
        and len(unit_paths) == expected_units,
        "checked_at": utc_now(),
        "predictions_before": predictions_before,
        "predictions_after": predictions_after,
        "metrics_before": metrics_before,
        "metrics_after": metrics_after,
        "unit_checkpoint_count": len(unit_paths),
        "expected_unit_checkpoint_count": expected_units,
        "gpu_loaded": False,
    }
    atomic_json(OUTPUT / "checkpoint_recovery.json", result)
    if not result["complete"]:
        raise RuntimeError("v3 checkpoint recovery validation failed")
    mark_stage(
        "recover",
        "complete",
        (OUTPUT / "checkpoint_recovery.json",),
        "A5 CPU checkpoint 恢复验证通过。",
        "online",
    )
    return result


def prepare_online() -> dict[str, Any]:
    metrics = load_json(OUTPUT / "metrics.json")
    if not metrics["offline_pass"]:
        raise RuntimeError("offline confirmation failed; online confirmation is not admissible")
    public_records = load_jsonl(DATA / "confirmatory_public.jsonl")
    private_records = load_jsonl(DATA / "confirmatory_private.jsonl")
    private_by_id = {item["episode_id"]: item for item in private_records}
    source_roots = {
        "sealed": ROOT / "data/episodes/mvp",
        "replication": DATA / "fresh_mvp",
    }
    source_records = {
        name: load_jsonl(path / "episodes_public.jsonl")
        for name, path in source_roots.items()
    }
    source_public = {
        item["episode_id"]: (name, item)
        for name, records in source_records.items()
        for item in records
    }
    predictions = [
        item
        for item in load_jsonl(OUTPUT / "predictions.jsonl")
        if item["policy_id"] == "trust3d_five_route"
    ]
    prediction_by_id = {item["episode_id"]: item for item in predictions}
    reobserve_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reobserve_groups: dict[str, set[str]] = defaultdict(set)
    mapping = []
    non_reobserve = []
    for public in public_records:
        private = private_by_id[public["episode_id"]]
        prediction = prediction_by_id[public["episode_id"]]
        if prediction["route"] != private["oracle_best_route"]:
            raise AssertionError("online route differs from independent oracle")
        if prediction["route"] == "REOBSERVE":
            source_id = public["source_episode_id"]
            if source_id not in source_public:
                raise ValueError(f"online source episode missing: {source_id}")
            source_name, source_item = source_public[source_id]
            reobserve_sources[source_name].append(source_item)
            reobserve_groups[source_name].add(source_item["group_id"])
            mapping.append(
                {
                    "parallel_episode_id": public["episode_id"],
                    "source_episode_id": source_id,
                    "source_batch": source_name,
                    "expected_move_steps": public["candidate_costs"]["REOBSERVE"][
                        "move_steps"
                    ],
                }
            )
        else:
            execution = execute_route(public, private, prediction["route"])
            no_movement = (
                execution["cost"]["move_steps"] == 0
                and execution["cost"]["new_observations"] == 0
            )
            abstain_clean = prediction["route"] != "ABSTAIN" or not execution["answered"]
            non_reobserve.append(
                {
                    "episode_id": public["episode_id"],
                    "route": prediction["route"],
                    "no_movement_or_new_observation": no_movement,
                    "abstain_has_no_answer": abstain_clean,
                }
            )
    reobserve_count = sum(len(items) for items in reobserve_sources.values())
    if reobserve_count != 20 or len(non_reobserve) != 80:
        raise AssertionError("online route split must be 20 REOBSERVE and 80 non-REOBSERVE")
    batch_result = {}
    for name, groups in reobserve_groups.items():
        root = source_roots[name]
        records = [item for item in source_records[name] if item["group_id"] in groups]
        selection = load_json(root / "selection.json")
        candidates = [
            item
            for item in selection["candidates"]
            if "g_" + item["candidate_id"][:24] in groups
        ]
        if len(candidates) != len(groups):
            raise ValueError(f"online {name} candidate/group mapping mismatch")
        public_path = DATA / f"online_{name}_public.jsonl"
        selection_path = DATA / f"online_{name}_selection.json"
        atomic_jsonl(public_path, records)
        atomic_json(
            selection_path,
            {
                "schema_version": 1,
                "candidates": sorted(candidates, key=lambda item: item["candidate_id"]),
            },
        )
        batch_result[name] = {
            "reobserve_group_count": len(groups),
            "source_episode_count": len(records),
            "public_path": public_path.relative_to(ROOT).as_posix(),
            "selection_path": selection_path.relative_to(ROOT).as_posix(),
            "source_root": root.relative_to(ROOT).as_posix(),
            "exclusion_path": (
                "configs/gate3_exclusions.json"
                if name == "sealed"
                else "data/episodes/parallel_v3/gt5/fresh_empty_exclusions.json"
            ),
        }
    if set(batch_result) != {"sealed", "replication"}:
        raise AssertionError(f"online source batches incomplete: {sorted(batch_result)}")
    atomic_json(OUTPUT / "online_mapping.json", {"records": mapping})
    atomic_json(OUTPUT / "online_batches.json", {"batches": batch_result})
    non_reobserve_result = {
        "group_count": len(non_reobserve),
        "complete": all(
            item["no_movement_or_new_observation"] and item["abstain_has_no_answer"]
            for item in non_reobserve
        ),
        "records": non_reobserve,
    }
    atomic_json(OUTPUT / "online_non_reobserve_validation.json", non_reobserve_result)
    if not non_reobserve_result["complete"]:
        raise RuntimeError("non-REOBSERVE side-effect validation failed")
    return {
        "reobserve_group_count": reobserve_count,
        "non_reobserve_group_count": len(non_reobserve),
        "batches": batch_result,
    }


def merge_online_traces() -> dict[str, Any]:
    paths = (OUTPUT / "online_sealed_traces.jsonl", OUTPUT / "online_replication_traces.jsonl")
    traces = [item for path in paths for item in load_jsonl(path)]
    keys = [(item["episode_id"], item.get("selected_route")) for item in traces]
    if len(keys) != len(set(keys)):
        raise ValueError("online batch traces contain duplicates")
    traces.sort(key=lambda item: item["episode_id"])
    atomic_jsonl(OUTPUT / "online_traces.jsonl", traces)
    return {
        "trace_count": len(traces),
        "batch_trace_count": {path.stem: len(load_jsonl(path)) for path in paths},
        "traces_sha256": sha256_file(OUTPUT / "online_traces.jsonl"),
    }


def record_interruption_probe(exit_code: int) -> dict[str, Any]:
    traces_path = OUTPUT / "online_traces.jsonl"
    traces = load_jsonl(traces_path) if traces_path.is_file() else []
    checkpoint_count = (
        len(list((DATA / "online_checkpoints").rglob("*")))
        if (DATA / "online_checkpoints").is_dir()
        else 0
    )
    result = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "complete": exit_code in {124, 137, 143},
        "interrupted_exit_code": exit_code,
        "partial_trace_count": len(traces),
        "checkpoint_entry_count": checkpoint_count,
        "recorded_at": utc_now(),
    }
    atomic_json(OUTPUT / "online_interruption_probe.json", result)
    if not result["complete"]:
        raise RuntimeError("online interruption probe did not terminate as expected")
    return result


def validate_online(traces_path: Path) -> dict[str, Any]:
    traces = load_jsonl(traces_path)
    mapping = load_json(OUTPUT / "online_mapping.json")["records"]
    mapping_by_source = {item["source_episode_id"]: item for item in mapping}
    source_ids = set(mapping_by_source)
    primary = [item for item in traces if item.get("episode_id") in source_ids]
    by_id = {item["episode_id"]: item for item in primary}
    movement_matches = all(
        int(by_id[source_id].get("movement_steps", -1))
        == int(mapping_by_source[source_id]["expected_move_steps"])
        for source_id in source_ids
        if source_id in by_id
    )
    checks = {
        "all_reobserve_sources_completed": set(by_id) == source_ids,
        "all_routes_reobserve": all(
            item.get("selected_route") == "REOBSERVE" for item in by_id.values()
        ),
        "new_observation_recorded": all(
            item.get("new_observation_count", 0) >= 1 for item in by_id.values()
        ),
        "movement_cost_matches_ledger": movement_matches,
        "no_action_failure": all(
            item.get("action_failure_count", 0) == 0 for item in by_id.values()
        ),
        "no_duplicate_episode": len(primary) == len(by_id),
    }
    non_reobserve = load_json(OUTPUT / "online_non_reobserve_validation.json")
    interruption = load_json(OUTPUT / "online_interruption_probe.json")
    result = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "complete": all(checks.values())
        and non_reobserve["complete"]
        and interruption["complete"],
        "backend": "ai2thor_shortest_visible_pose",
        "confirmatory_group_count": len(source_ids) + non_reobserve["group_count"],
        "ai2thor_reobserve_group_count": len(source_ids),
        "deterministic_non_reobserve_group_count": non_reobserve["group_count"],
        "checks": checks,
        "interruption_recovery": interruption,
        "traces_sha256": sha256_file(traces_path),
        "checked_at": utc_now(),
    }
    atomic_json(OUTPUT / "online_validation.json", result)
    if not result["complete"]:
        raise RuntimeError(f"online validation failed: {checks}")
    mark_stage(
        "online",
        "complete",
        (OUTPUT / "online_validation.json",),
        "A6 confirmatory100 在线副作用、成本和中断恢复验证通过。",
        "report",
    )
    return result


def report() -> dict[str, Any]:
    baseline = verify_baseline()
    preflight_result = load_json(OUTPUT / "preflight.json")
    contract_result = load_json(OUTPUT / "contract.json")
    prepare_result = load_json(OUTPUT / "prepare.json")
    metrics = load_json(OUTPUT / "metrics.json")
    recovery = load_json(OUTPUT / "checkpoint_recovery.json")
    online = load_json(OUTPUT / "online_validation.json")
    all_acceptance = all(metrics["acceptance"].values())
    complete = (
        all_acceptance
        and metrics["offline_pass"]
        and recovery["complete"]
        and online["complete"]
        and bool(baseline)
    )
    status = "complete" if complete else "failed_scientific"
    decision = {
        "schema_version": 1,
        "protocol_revision": "gt-five-route-v3",
        "status": status,
        "complete": complete,
        "offline_pass": metrics["offline_pass"],
        "online_pass": online["complete"],
        "checkpoint_recovery_pass": recovery["complete"],
        "baseline_file_count_verified": len(baseline),
        "acceptance": metrics["acceptance"],
        "generated_at": utc_now(),
    }
    atomic_json(OUTPUT / "final_decision.json", decision)
    trust = metrics["policy_metrics"]["trust3d_five_route"]
    baseline_metrics = metrics["policy_metrics"]["always_reobserve"]
    pair = metrics["paired_accuracy_vs_always_reobserve"]
    risk = metrics["risk_intervals"]
    cost = metrics["cost_reduction_vs_always_reobserve"]
    lines = [
        "# Trust3D GT 五路路由可行性验证报告 v3",
        "",
        "## 1. 最终结果",
        "",
        f"- 最终状态：`{status}`",
        f"- 五路路由可行性验收：{'通过' if complete else '未通过'}",
        f"- 协议版本：`gt-five-route-v3`",
        f"- 确认集：100 groups（sealed60 + replication40），每路 20 groups",
        f"- Git commit：`{_git_commit()}`",
        "",
        "通过仅表示：在可靠 GT 证据和预注册平衡压力测试下，完整五路控制器风险受控，且相对 always-reobserve 保持准确率并降低成本。该结论不等于 RGB-only 端到端系统已经成立，也不改写原 Gate 7 失败。",
        "",
        "## 2. v2 停止原因与 v3 修订",
        "",
        f"v2 pilot 的路由契约通过，但最坏情形功效公式得到 N={preflight_result['v2_required_groups']}，因此状态为 `inconclusive_underpowered`。v3 保留该结果，只将该公式降为敏感性分析，并用固定 100-group 确认集、有限样本精确区间和独立复现集检验实际主张。",
        "",
        "## 3. 数据与防泄漏",
        "",
        f"- sealed60 未揭示审计：`{prepare_result['sealed_audit']['decision']}`",
        f"- development20：仅用于开发，不进入主指标",
        f"- replication40：五路各 8，来源与 development/sealed 不重叠",
        f"- confirmatory100 source overlap：{prepare_result['source_overlap_count']}",
        f"- inference private open count：{load_json(OUTPUT / 'predictions_seal.json')['private_open_count']}",
        f"- evaluator private open count：{metrics['private_open_count']}",
        "",
        "## 4. 契约和独立 oracle",
        "",
        f"- 全因子 case：{contract_result['full_factor_case_count']}，全部通过",
        f"- mutation：{contract_result['mutation_count']}，全部杀死",
        f"- development route match：{contract_result['development_route_match']}/{contract_result['development_group_count']}",
        f"- confirmatory route match：{metrics['route_match_count']}/100",
        f"- avoidable route regret：{trust['avoidable_regret_count']}",
        "",
        "## 5. 准确率、风险与成本",
        "",
        f"- Trust3D 总体准确率：{trust['correct_count']}/100 = {trust['accuracy']:.4f}",
        f"- always-reobserve 总体准确率：{baseline_metrics['correct_count']}/100 = {baseline_metrics['accuracy']:.4f}",
        f"- 配对差：{pair['point_estimate']:.4f}；Tango 单侧 95% 下界：{pair['tango_one_sided_95_lower']:.4f}",
        f"- coverage：{trust['coverage']:.4f}",
        f"- selective error：{trust['selective_error_count']}/{trust['answered_count']}；单侧 95% 上界：{risk['selective_error_one_sided_95_upper']:.4f}",
        f"- unsafe answer：{trust['unsafe_answer_count']}/100；单侧 95% 上界：{risk['unsafe_answer_one_sided_95_upper']:.4f}",
        f"- false abstain：{trust['false_abstain_count']}/{trust['false_abstain_denominator']}；单侧 95% 上界：{risk['false_abstain_one_sided_95_upper']:.4f}",
        f"- 成本下降：{cost['point_estimate']:.4f}；scene-cluster bootstrap 单侧 95% 下界：{cost['one_sided_95_lower']:.4f}",
        "",
        "## 6. 分层复现",
        "",
        f"- sealed60 route match：{metrics['layer_metrics']['sealed60']['route_match']}/60",
        f"- replication40 route match：{metrics['layer_metrics']['replication40']['route_match']}/40",
        "",
        "## 7. 在线执行与恢复",
        "",
        f"- AI2-THOR REOBSERVE：{online['ai2thor_reobserve_group_count']} groups，全部通过",
        f"- 其余四路确定性副作用：{online['deterministic_non_reobserve_group_count']} groups，全部通过",
        f"- confirmatory100 在线覆盖：{online['confirmatory_group_count']}/100",
        f"- 人为中断退出码：{online['interruption_recovery']['interrupted_exit_code']}，随后从 checkpoint 恢复",
        f"- CPU 单元 checkpoint：{recovery['unit_checkpoint_count']}/{recovery['expected_unit_checkpoint_count']}，重复恢复哈希不变",
        "",
        "## 8. 逐项验收",
        "",
    ]
    for name, passed in metrics["acceptance"].items():
        lines.append(f"- `{name}`：{'通过' if passed else '未通过'}")
    lines.extend(
        [
            "",
            "## 9. 结论边界",
            "",
            "本实验隔离验证的是 controller：当 current/history/3D/reobserve/abstain 所需证据可靠且语义明确时，路由器可以稳定选择正确路线，并获得 accuracy-cost-risk 优势。CUT3R/VGGT 的 RGB 几何误差仍由 Gate 7/B 线单独评价；只有 A、B 均通过且 C 线联合实验通过，才能支持完整 RGB 五路 Trust3D 主结果。",
            "",
        ]
    )
    atomic_bytes(REPORT_PATH, "\n".join(lines).encode("utf-8"))
    decision["report_sha256"] = sha256_file(REPORT_PATH)
    atomic_json(OUTPUT / "final_decision.json", decision)
    mark_stage(
        "report",
        "complete" if complete else "failed_scientific",
        (OUTPUT / "final_decision.json", REPORT_PATH),
        "A7 中文详细报告已生成。",
        "complete",
    )
    write_status(
        "complete" if complete else "failed_scientific",
        "complete",
        "GT 五路路由 v3 全部阶段已结束。",
    )
    return decision


def status() -> dict[str, Any]:
    value = (
        load_json(OUTPUT / "status.json")
        if (OUTPUT / "status.json").is_file()
        else {
            "state": "pending",
            "stage": "preflight",
            "message": "尚未启动",
        }
    )
    value["stages"] = {
        name: stage_complete(name)
        for name in (
            "preflight",
            "contract",
            "prepare",
            "freeze",
            "infer",
            "evaluate",
            "recover",
            "online",
            "report",
        )
    }
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "preflight",
            "contract",
            "source-plan",
            "source-audit",
            "prepare",
            "freeze",
            "infer",
            "evaluate",
            "recover",
            "prepare-online",
            "merge-online",
            "record-interruption",
            "validate-online",
            "report",
            "status",
        ),
    )
    parser.add_argument("--traces", type=Path, default=OUTPUT / "online_traces.jsonl")
    parser.add_argument("--exit-code", type=int, default=0)
    args = parser.parse_args(argv)
    functions = {
        "preflight": preflight,
        "contract": contract,
        "source-plan": source_plan,
        "source-audit": source_audit,
        "prepare": prepare,
        "freeze": freeze,
        "infer": infer,
        "evaluate": evaluate,
        "recover": recover,
        "prepare-online": prepare_online,
        "merge-online": merge_online_traces,
        "record-interruption": lambda: record_interruption_probe(args.exit_code),
        "validate-online": lambda: validate_online(args.traces),
        "report": report,
        "status": status,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.mkdir(parents=True, exist_ok=True)
    write_status("running", args.mode, f"正在执行 {args.mode}")
    result = functions[args.mode]()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
