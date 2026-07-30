"""生成 Plan 2 的条件 crop 诊断与固定最终决策。"""

import argparse
import json
import math
from pathlib import Path

import numpy as np

from trust3d.data.select_events import read_jsonl
from trust3d.eval.evaluate_cut3r import evaluate_vggt
from trust3d.geometry.run_vggt import _atomic_json


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _group_purity(diagnostics):
    values = {}
    for group_id, group in diagnostics["groups"].items():
        purities = []
        for stage in group["crop"].values():
            for role in stage.values():
                purity = role.get("crop_purity")
                if purity is not None and math.isfinite(purity):
                    purities.append(float(purity))
        values[group_id] = float(np.median(purities)) if purities else None
    return values


def _group_accuracy(predictions):
    grouped = {}
    for item in predictions:
        if item["method"] != "trust3d_vggt":
            continue
        grouped.setdefault(item["group_id"], []).append(float(item["correct"]))
    return {group_id: float(np.mean(items)) for group_id, items in grouped.items()}


def _quartile_gap(purity, accuracy):
    rows = sorted(
        (value, group_id, accuracy[group_id])
        for group_id, value in purity.items()
        if value is not None and group_id in accuracy
    )
    count = max(1, math.ceil(len(rows) * 0.25))
    low = rows[:count]
    high = rows[-count:]
    return {
        "groups_per_quartile": count,
        "low_purity_median": float(np.median([row[0] for row in low])),
        "high_purity_median": float(np.median([row[0] for row in high])),
        "low_accuracy": float(np.mean([row[2] for row in low])),
        "high_accuracy": float(np.mean([row[2] for row in high])),
        "accuracy_gap": float(
            np.mean([row[2] for row in high]) - np.mean([row[2] for row in low])
        ),
    }


def _variant_geometry(source_root, destination_root, fraction):
    source_root = Path(source_root)
    destination_root = Path(destination_root)
    source_manifest = _load(source_root / "manifest.json")
    groups = []
    key = str(fraction)
    for item in source_manifest["groups"]:
        source = _load(item["checkpoint"])
        candidate = source["diagnostic_candidates"][key]
        variant = dict(source)
        for stage in (
            "historical",
            "stale_sequence_historical",
            "stable_reobserve",
            "stale_reobserve",
        ):
            variant[stage] = candidate[stage]
        variant["adapter_version"] = f"{source['adapter_version']}-crop-{key}-posthoc"
        variant["center_crop_fraction"] = fraction
        checkpoint = destination_root / "checkpoints" / f"{item['group_id']}.json"
        _atomic_json(checkpoint, variant)
        groups.append(
            {
                "group_id": item["group_id"],
                "status": "success",
                "fingerprint": item["fingerprint"],
                "checkpoint": str(checkpoint),
            }
        )
    manifest = dict(source_manifest)
    manifest["adapter_version"] = (
        f"{source_manifest['adapter_version']}-crop-{key}-posthoc"
    )
    manifest["groups"] = groups
    _atomic_json(destination_root / "manifest.json", manifest)


def diagnose(args):
    validation = _load(args.validation)
    diagnostics = _load(args.cut3r_diagnostics)
    purity = _group_purity(diagnostics)
    accuracy = _group_accuracy(read_jsonl(args.predictions))
    finite_purity = [value for value in purity.values() if value is not None]
    purity_median = float(np.median(finite_purity))
    quartiles = _quartile_gap(purity, accuracy)
    conditions = {
        "main_qa_drop_above_10pp": validation["gt_to_vggt_qa_drop"] > 0.10,
        "crop_purity_median_below_0_90": purity_median < 0.90,
        "purity_quartiles_separated": quartiles["high_purity_median"]
        > quartiles["low_purity_median"],
        "accuracy_gap_at_least_20pp": quartiles["accuracy_gap"] >= 0.20,
    }
    triggered = all(conditions.values())
    variants = {}
    if triggered:
        for fraction in (0.08, 0.18):
            root = args.output.parent / "diagnostics" / f"crop_{fraction}"
            _variant_geometry(args.geometry, root, fraction)
            predictions = root / "predictions.jsonl"
            report_path = root / "validation.json"
            report = evaluate_vggt(
                args.public,
                args.private,
                args.routes,
                root,
                args.source_checkpoints,
                predictions,
                report_path,
                args.reference_predictions,
            )
            variants[str(fraction)] = {
                "accuracy": report["vggt_accuracy"],
                "qa_drop": report["gt_to_vggt_qa_drop"],
                "result_status": "posthoc_candidate"
                if report["gt_to_vggt_qa_drop"] <= 0.10
                else "posthoc_diagnostic",
                "validation": str(report_path),
            }
    report = {
        "schema_version": 1,
        "diagnostic_only": True,
        "triggered": triggered,
        "trigger_conditions": conditions,
        "crop_purity_median": purity_median,
        "purity_accuracy_quartiles": quartiles,
        "main_crop_fraction": 0.12,
        "variant_results": variants,
        "main_result_overwritten": False,
    }
    _atomic_json(args.output, report)
    return report


def decide(args):
    validation = _load(args.validation)
    recovery = _load(args.recovery)
    diagnostics = _load(args.conditional_diagnostics)
    metrics = validation["metrics"]
    vggt = metrics["trust3d_vggt"]
    cut3r = _load(args.cut3r_validation)["metrics"]["trust3d_cut3r"]
    engineering = {
        "all_evaluator_criteria_except_quality": all(
            value
            for key, value in validation["criteria"].items()
            if key != "qa_drop_not_above_10pp"
        ),
        "checkpoint_recovery_pass": recovery["checkpoint_recovery_pass"],
        "new_observation_count_unchanged": vggt["new_observation_count"]
        == cut3r["new_observation_count"],
        "movement_steps_unchanged": vggt["movement_steps"]
        == cut3r["movement_steps"],
    }
    drop = validation["gt_to_vggt_qa_drop"]
    if not all(engineering.values()):
        status = "engineering_failure"
        passed = False
    elif drop <= 0.10:
        status = "main_result"
        passed = True
    elif drop <= 0.20:
        status = "realistic_setting"
        passed = False
    else:
        status = "failure_analysis"
        passed = False
    report = {
        "schema_version": 1,
        "result_status": status,
        "gate7_vggt_pass": passed,
        "original_gate7_cut3r_pass": False,
        "original_gate7_preserved": True,
        "gt_to_vggt_qa_drop": drop,
        "vggt_accuracy": validation["vggt_accuracy"],
        "cut3r_accuracy": args.cut3r_accuracy,
        "paired_cut3r_comparison": validation["paired_cut3r_comparison"],
        "engineering_checks": engineering,
        "conditional_diagnostics_triggered": diagnostics["triggered"],
        "posthoc_changes_main_result": False,
        "gate8_may_be_planned": passed,
    }
    _atomic_json(args.output, report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("diagnose", "decide"))
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--cut3r-diagnostics", type=Path)
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--public", type=Path)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--routes", type=Path)
    parser.add_argument("--source-checkpoints", type=Path)
    parser.add_argument("--reference-predictions", type=Path)
    parser.add_argument("--recovery", type=Path)
    parser.add_argument("--conditional-diagnostics", type=Path)
    parser.add_argument("--cut3r-validation", type=Path)
    parser.add_argument("--cut3r-accuracy", type=float, default=0.5611111111111111)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.mode == "diagnose":
        required = (
            "predictions",
            "cut3r_diagnostics",
            "geometry",
            "public",
            "private",
            "routes",
            "source_checkpoints",
            "reference_predictions",
        )
        if any(getattr(args, name) is None for name in required):
            raise SystemExit("diagnose 缺少必需参数")
        report = diagnose(args)
    else:
        required = (
            "recovery",
            "conditional_diagnostics",
            "cut3r_validation",
        )
        if any(getattr(args, name) is None for name in required):
            raise SystemExit("decide 缺少必需参数")
        report = decide(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
