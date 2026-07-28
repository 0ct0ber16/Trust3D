"""Deterministically select a diverse Gate 2 event pilot."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path):
    records = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(
                    "invalid JSON at {}:{}: {}".format(path, line_number, exc)
                )
    return records


def _seed_rank(seed, candidate_id):
    value = "{}\0{}".format(seed, candidate_id).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _greedy_pick(pool, count, seed, selected, counters):
    used_trajectories = {
        (item["split"], item["trajectory_id"]) for item in selected
    }
    used_scenes = {item["scene"] for item in selected}
    remaining = list(pool)

    while len(selected) < count:
        available = [
            item
            for item in remaining
            if (item["split"], item["trajectory_id"]) not in used_trajectories
        ]
        if not available:
            raise ValueError("not enough distinct trajectories for requested pilot")

        def score(item):
            exact_unique = (
                item.get("same_type_object_count") == 1
                and not item.get("same_type_count_is_lower_bound", True)
            )
            return (
                item["scene"] in used_scenes,
                counters["split"][item["split"]],
                counters["action"][item["action"]],
                counters["type"][item["target_object_type"]],
                not exact_unique,
                item.get("prefix_length", 10 ** 9),
                _seed_rank(seed, item["candidate_id"]),
            )

        chosen = min(available, key=score)
        selected.append(chosen)
        used_trajectories.add((chosen["split"], chosen["trajectory_id"]))
        used_scenes.add(chosen["scene"])
        for key, field in (
            ("split", "split"),
            ("action", "action"),
            ("type", "target_object_type"),
        ):
            counters[key][chosen[field]] += 1
        remaining.remove(chosen)


def select_candidates(candidates, limit, seed=17):
    if limit < 1:
        raise ValueError("limit must be positive")
    eligible = [
        item
        for item in candidates
        if item.get("mvp_whitelist")
        and item.get("action") in {"OpenObject", "CloseObject"}
    ]
    unseen = [item for item in eligible if item.get("split") == "valid_unseen"]
    others = [item for item in eligible if item.get("split") != "valid_unseen"]
    minimum_unseen = (limit + 1) // 2
    if len(unseen) < minimum_unseen:
        raise ValueError("not enough valid_unseen candidates")

    selected = []
    counters = {
        "split": Counter(),
        "action": Counter(),
        "type": Counter(),
    }
    _greedy_pick(unseen, minimum_unseen, seed, selected, counters)
    _greedy_pick(others, limit, seed, selected, counters)
    return selected


def selection_summary(selected, seed):
    return {
        "seed": seed,
        "selected_count": len(selected),
        "split_counts": dict(sorted(Counter(x["split"] for x in selected).items())),
        "action_counts": dict(
            sorted(Counter(x["action"] for x in selected).items())
        ),
        "object_type_counts": dict(
            sorted(Counter(x["target_object_type"] for x in selected).items())
        ),
        "distinct_scenes": len({x["scene"] for x in selected}),
        "scenes": sorted({x["scene"] for x in selected}),
        "distinct_trajectories": len(
            {(x["split"], x["trajectory_id"]) for x in selected}
        ),
        "valid_unseen_selected_scenes": len(
            {x["scene"] for x in selected if x["split"] == "valid_unseen"}
        ),
        "all_candidate_ids": [x["candidate_id"] for x in selected],
    }


def write_selection(selected, summary, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            {"summary": summary, "candidates": selected},
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")
    temporary.replace(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, type=Path)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--seed", default=17, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    selected = select_candidates(read_jsonl(args.candidates), args.limit, args.seed)
    summary = selection_summary(selected, args.seed)
    write_selection(selected, summary, args.output)
    print(
        "[gate2] selected {} events from {} scenes".format(
            len(selected), summary["distinct_scenes"]
        )
    )


if __name__ == "__main__":
    main()
