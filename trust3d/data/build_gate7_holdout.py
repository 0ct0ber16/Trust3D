"""Build source- and scene-disjoint Gate 7 pilot and holdout datasets."""

from __future__ import annotations

import argparse
import hashlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

from trust3d.data.build_spatial import run_spatial
from trust3d.parallel_v2.common import (
    ROOT,
    atomic_json,
    load_json,
    load_jsonl,
    protocol,
    sha256_file,
    utc_now,
)


def _atomic_hardlink(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == sha256_file(source):
        return
    temporary = destination.with_name(f"{destination.name}.tmp.{os.getpid()}")
    if temporary.exists():
        temporary.unlink()
    os.link(source, temporary)
    os.replace(temporary, destination)


def write_rgb_sequence_manifest(split_root: Path):
    """Expose anonymous RGB-only counterfactual sequences to inference runners."""
    public = load_jsonl(split_root / "episodes_public.jsonl")
    public_by_group = {}
    for episode in public:
        public_by_group.setdefault(episode["group_id"], episode)
    contexts = {}
    for path in sorted((split_root / "checkpoints").glob("*/context.json")):
        value = load_json(path)
        if value.get("status") == "success":
            contexts[value["group_id"]] = value
    if set(public_by_group) != set(contexts):
        raise ValueError("RGB sequence manifest group/context mismatch")

    groups = []
    for group_id in sorted(public_by_group):
        episode = public_by_group[group_id]
        context = contexts[group_id]
        history = episode["history_observations"]
        common = [
            ROOT / history["target"]["rgb"],
            ROOT / history["donor"]["rgb"],
            ROOT / episode["query_observation"]["rgb"],
        ]
        scenarios = []
        for scenario_index, branch in enumerate(("risk_stable", "risk_stale")):
            observations = context["branches"][branch]["observations"]
            sources = common + [
                split_root / observations["target"]["rgb"],
                split_root / observations["donor"]["rgb"],
            ]
            frames = []
            for frame_index, source in enumerate(sources):
                if not source.is_file():
                    raise FileNotFoundError(source)
                destination = (
                    split_root
                    / "inference_rgb"
                    / group_id
                    / f"scenario_{scenario_index}"
                    / f"frame_{frame_index}.png"
                )
                _atomic_hardlink(source, destination)
                frames.append(
                    {
                        "path": destination.relative_to(ROOT).as_posix(),
                        "sha256": sha256_file(destination),
                    }
                )
            scenarios.append(
                {"scenario_id": f"scenario_{scenario_index}", "frames": frames}
            )
        groups.append({"group_id": group_id, "scenarios": scenarios})
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "input_mode": "rgb_only_counterfactual_sequences",
        "group_count": len(groups),
        "groups": groups,
    }
    path = split_root / "rgb_sequences.json"
    atomic_json(path, value)
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256_file(path),
        "group_count": len(groups),
    }


def _stable_key(seed: int, namespace: str, value: str):
    return hashlib.sha256(f"{seed}|{namespace}|{value}".encode("ascii")).hexdigest()


def _selection_plan(config):
    source = load_json(ROOT / config["source_selection"])
    legacy = load_json(ROOT / "data/episodes/spatial30/selection.json")
    by_id = {item["candidate_id"]: item for item in source["candidates"]}
    missing = sorted(set(legacy["candidate_ids"]) - set(by_id))
    if missing:
        raise ValueError("legacy selection is not a subset of the source selection")
    legacy_scenes = {by_id[candidate_id]["scene"] for candidate_id in legacy["candidate_ids"]}
    allowed = [
        item
        for item in source["candidates"]
        if item["candidate_id"] not in set(legacy["candidate_ids"])
        and item["scene"] not in legacy_scenes
    ]
    by_scene = defaultdict(list)
    for item in allowed:
        by_scene[item["scene"]].append(item)
    seed = protocol()["seed"]
    scenes = sorted(by_scene, key=lambda scene: _stable_key(seed, "gate7-scene", scene))
    pilot_target = protocol()["datasets"]["gate7_pilot_groups"]
    holdout_target = protocol()["datasets"]["gate7_holdout_groups"]
    pilot_scenes = []
    pilot_count = 0
    reserve = max(pilot_target + 5, pilot_target)
    for scene in scenes:
        if len(allowed) - pilot_count <= holdout_target:
            break
        pilot_scenes.append(scene)
        pilot_count += len(by_scene[scene])
        if pilot_count >= reserve:
            break
    holdout_scenes = [scene for scene in scenes if scene not in set(pilot_scenes)]
    pilot_candidates = [item for scene in pilot_scenes for item in by_scene[scene]]
    holdout_candidates = [item for scene in holdout_scenes for item in by_scene[scene]]
    if len(pilot_candidates) < pilot_target or len(holdout_candidates) < holdout_target:
        raise RuntimeError(
            "insufficient scene-disjoint candidates: "
            f"pilot={len(pilot_candidates)}/{pilot_target}, "
            f"holdout={len(holdout_candidates)}/{holdout_target}"
        )
    pilot_candidates.sort(key=lambda item: _stable_key(seed, "gate7-pilot", item["candidate_id"]))
    holdout_candidates.sort(key=lambda item: _stable_key(seed, "gate7-holdout", item["candidate_id"]))
    return {
        "legacy_candidate_ids": set(legacy["candidate_ids"]),
        "legacy_scenes": legacy_scenes,
        "pilot_scenes": set(pilot_scenes),
        "holdout_scenes": set(holdout_scenes),
        "pilot_candidates": pilot_candidates,
        "holdout_candidates": holdout_candidates,
    }


def _write_selections(config, plan):
    root = ROOT / config["dataset_root"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    seed = protocol()["seed"]
    empty_exclusions = root / "empty_exclusions.json"
    atomic_json(empty_exclusions, {"schema_version": 1, "group_ids": []})
    for split in ("pilot", "holdout"):
        atomic_json(
            root / f"{split}_source_selection.json",
            {
                "schema_version": 1,
                "seed": seed,
                "candidates": plan[f"{split}_candidates"],
            },
        )
    return root, empty_exclusions


def _selected_metadata(split_root: Path, source_selection: Path):
    locked = load_json(split_root / "selection.json")["candidate_ids"]
    by_id = {
        item["candidate_id"]: item
        for item in load_json(source_selection)["candidates"]
    }
    return [by_id[candidate_id] for candidate_id in locked]


def build(config_path: Path, max_new_contexts: Optional[int] = None):
    config = load_json(config_path)
    plan = _selection_plan(config)
    root, exclusions = _write_selections(config, plan)
    counts = protocol()["datasets"]
    reports = {}
    for split, target in (
        ("pilot", counts["gate7_pilot_groups"]),
        ("holdout", counts["gate7_holdout_groups"]),
    ):
        split_root = root / split
        split_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        reports[split] = run_spatial(
            root / f"{split}_source_selection.json",
            ROOT / config["alfred_json"],
            exclusions,
            split_root,
            target_groups=target,
            seed=protocol()["seed"] + (1 if split == "pilot" else 2),
            max_new_contexts=max_new_contexts,
        )
        private_path = split_root / "oracle_private.jsonl"
        if private_path.is_file():
            os.chmod(private_path, 0o600)
    if not all(report["complete"] for report in reports.values()):
        result = {
            "schema_version": 1,
            "complete": False,
            "generated_at": utc_now(),
            "splits": reports,
            "message": "spatial generation checkpointed but not complete",
        }
        atomic_json(ROOT / config["output_root"] / "prepare.json", result)
        return result
    pilot_metadata = _selected_metadata(root / "pilot", root / "pilot_source_selection.json")
    holdout_metadata = _selected_metadata(root / "holdout", root / "holdout_source_selection.json")
    pilot_ids = {item["candidate_id"] for item in pilot_metadata}
    holdout_ids = {item["candidate_id"] for item in holdout_metadata}
    pilot_scenes = {item["scene"] for item in pilot_metadata}
    holdout_scenes = {item["scene"] for item in holdout_metadata}
    source_overlap = pilot_ids & holdout_ids
    scene_overlap = pilot_scenes & holdout_scenes
    legacy_source_overlap = (pilot_ids | holdout_ids) & plan["legacy_candidate_ids"]
    legacy_scene_overlap = (pilot_scenes | holdout_scenes) & plan["legacy_scenes"]
    complete = not (source_overlap or scene_overlap or legacy_source_overlap or legacy_scene_overlap)
    sequence_manifests = {
        split: write_rgb_sequence_manifest(root / split)
        for split in ("pilot", "holdout")
    }
    result = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "complete": complete,
        "generated_at": utc_now(),
        "config_sha256": sha256_file(config_path),
        "splits": reports,
        "rgb_sequence_manifests": sequence_manifests,
        "audit": {
            "pilot_holdout_source_overlap": sorted(source_overlap),
            "pilot_holdout_scene_overlap": sorted(scene_overlap),
            "legacy_source_overlap": sorted(legacy_source_overlap),
            "legacy_scene_overlap": sorted(legacy_scene_overlap),
            "pilot_scenes": sorted(pilot_scenes),
            "holdout_scenes": sorted(holdout_scenes),
        },
    }
    atomic_json(ROOT / config["output_root"] / "prepare.json", result)
    if not complete:
        raise RuntimeError("Gate 7 pilot/holdout disjointness audit failed")
    return result


def audit(config_path: Path):
    config = load_json(config_path)
    plan = _selection_plan(config)
    return {
        "legacy_scene_count": len(plan["legacy_scenes"]),
        "pilot_candidate_count": len(plan["pilot_candidates"]),
        "pilot_scene_count": len(plan["pilot_scenes"]),
        "holdout_candidate_count": len(plan["holdout_candidates"]),
        "holdout_scene_count": len(plan["holdout_scenes"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["audit", "build"])
    parser.add_argument("--config", type=Path, default=Path("configs/gate7_fix_v1.json"))
    parser.add_argument("--max-new-contexts", type=int)
    args = parser.parse_args(argv)
    config_path = ROOT / args.config if not args.config.is_absolute() else args.config
    result = audit(config_path) if args.mode == "audit" else build(config_path, args.max_new_contexts)
    print(result)
    return 0 if result.get("complete", True) else 3


if __name__ == "__main__":
    raise SystemExit(main())
