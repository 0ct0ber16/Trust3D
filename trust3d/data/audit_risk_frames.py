"""审计 Risk-Stable/Risk-Stale 查询观测是否泄漏隐藏干预。"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from trust3d.data.select_events import read_jsonl


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _difference(left, right):
    if left.shape != right.shape:
        return {
            "shape_equal": False,
            "changed_count": None,
            "changed_fraction": 1.0,
            "max_absolute_difference": None,
            "mean_absolute_difference": None,
        }
    difference = np.abs(left.astype(np.float64) - right.astype(np.float64))
    changed = difference != 0
    return {
        "shape_equal": True,
        "changed_count": int(changed.sum()),
        "changed_fraction": float(changed.mean()),
        "max_absolute_difference": float(difference.max()),
        "mean_absolute_difference": float(difference.mean()),
    }


def _load(path, modality):
    if modality == "depth":
        return np.load(str(path), allow_pickle=False)
    with Image.open(str(path)) as image:
        return np.asarray(image).copy()


def audit(
    public_path,
    private_path,
    output_path,
    rgb_changed_fraction_limit=0.001,
    rgb_max_difference_limit=2.0,
    depth_changed_fraction_limit=0.0,
    depth_max_difference_limit=0.0,
):
    public_path = Path(public_path)
    public = read_jsonl(public_path)
    private = read_jsonl(private_path)
    public_by_id = {item["episode_id"]: item for item in public}
    grouped = defaultdict(lambda: defaultdict(dict))
    for item in private:
        grouped[item["group_id"]][int(item.get("question_index", 0))][
            item["branch"]
        ] = item

    pair_reports = []
    errors = []
    maxima = {
        "rgb_changed_fraction": 0.0,
        "rgb_max_absolute_difference": 0.0,
        "depth_changed_fraction": 0.0,
        "depth_max_absolute_difference": 0.0,
        "instance_changed_fraction": 0.0,
    }
    for group_id, questions in sorted(grouped.items()):
        for question_index, branches in sorted(questions.items()):
            if not {"risk_stable", "risk_stale"}.issubset(branches):
                errors.append("{}:q{} 缺少风险分支".format(group_id, question_index))
                continue
            stable_private = branches["risk_stable"]
            stale_private = branches["risk_stale"]
            stable = public_by_id[stable_private["episode_id"]]
            stale = public_by_id[stale_private["episode_id"]]
            observations = {}
            pair_failed = False
            for modality in ("rgb", "depth", "instance"):
                left_path = public_path.parent / stable["query_observation"][modality]
                right_path = public_path.parent / stale["query_observation"][modality]
                difference = _difference(
                    _load(left_path, modality), _load(right_path, modality)
                )
                observations[modality] = difference
                if modality == "rgb":
                    maxima["rgb_changed_fraction"] = max(
                        maxima["rgb_changed_fraction"],
                        difference["changed_fraction"],
                    )
                    maxima["rgb_max_absolute_difference"] = max(
                        maxima["rgb_max_absolute_difference"],
                        difference["max_absolute_difference"] or 0,
                    )
                    pair_failed = pair_failed or (
                        not difference["shape_equal"]
                        or difference["changed_fraction"]
                        > rgb_changed_fraction_limit
                        or (difference["max_absolute_difference"] or 0)
                        > rgb_max_difference_limit
                    )
                elif modality == "depth":
                    maxima["depth_changed_fraction"] = max(
                        maxima["depth_changed_fraction"],
                        difference["changed_fraction"],
                    )
                    maxima["depth_max_absolute_difference"] = max(
                        maxima["depth_max_absolute_difference"],
                        difference["max_absolute_difference"] or 0,
                    )
                    pair_failed = pair_failed or (
                        not difference["shape_equal"]
                        or difference["changed_fraction"]
                        > depth_changed_fraction_limit
                        or (difference["max_absolute_difference"] or 0)
                        > depth_max_difference_limit
                    )
                else:
                    maxima["instance_changed_fraction"] = max(
                        maxima["instance_changed_fraction"],
                        difference["changed_fraction"],
                    )
                    pair_failed = pair_failed or (
                        not difference["shape_equal"]
                        or difference["changed_fraction"] > 0
                    )
            if stable_private.get("target_visible_from_query", True) or stale_private.get(
                "target_visible_from_query", True
            ):
                pair_failed = True
                errors.append("{}:q{} 目标在查询帧可见".format(group_id, question_index))
            pair = {
                "group_id": group_id,
                "question_index": question_index,
                "observations": observations,
                "pair_pass": not pair_failed,
            }
            pair_reports.append(pair)
            if pair_failed:
                errors.append("{}:q{} 像素差异超限".format(group_id, question_index))

    report = {
        "schema_version": 1,
        "pair_count": len(pair_reports),
        "failed_pair_count": sum(not item["pair_pass"] for item in pair_reports),
        "limits": {
            "rgb_changed_fraction": rgb_changed_fraction_limit,
            "rgb_max_absolute_difference": rgb_max_difference_limit,
            "depth_changed_fraction": depth_changed_fraction_limit,
            "depth_max_absolute_difference": depth_max_difference_limit,
            "instance_changed_fraction": 0.0,
        },
        "maxima": maxima,
        "errors": errors[:20],
        "failed_pairs": [
            item for item in pair_reports if not item["pair_pass"]
        ][:20],
        "risk_frame_audit_pass": bool(pair_reports) and not errors,
    }
    _atomic_json(output_path, report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", required=True, type=Path)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rgb-changed-fraction-limit", type=float, default=0.001)
    parser.add_argument("--rgb-max-difference-limit", type=float, default=2.0)
    parser.add_argument("--depth-changed-fraction-limit", type=float, default=0.0)
    parser.add_argument("--depth-max-difference-limit", type=float, default=0.0)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = audit(
        args.public,
        args.private,
        args.output,
        rgb_changed_fraction_limit=args.rgb_changed_fraction_limit,
        rgb_max_difference_limit=args.rgb_max_difference_limit,
        depth_changed_fraction_limit=args.depth_changed_fraction_limit,
        depth_max_difference_limit=args.depth_max_difference_limit,
    )
    print(json.dumps(report, sort_keys=True))
    if not report["risk_frame_audit_pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
