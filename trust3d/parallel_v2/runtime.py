"""Small CLI used by persistent shell runners for atomic runtime state."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from trust3d.parallel_v2.common import (
    ROOT,
    cpu_admitted,
    mark_stage,
    resource_snapshot,
    stage_complete,
    verify_baseline,
    write_status,
)


def wait_cpu(component, interval):
    while True:
        snapshot = resource_snapshot()
        if cpu_admitted(snapshot):
            write_status(component, "cpu_admitted", "CPU、内存和磁盘满足硬门槛。", resources=snapshot)
            return snapshot
        write_status(
            component,
            "waiting_for_resource",
            "CPU、内存或磁盘暂不满足硬门槛，持续监控。",
            resources=snapshot,
        )
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("component")
    status_parser.add_argument("state")
    status_parser.add_argument("message")
    status_parser.add_argument("--log", default="")
    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("stage")
    mark_parser.add_argument("status")
    mark_parser.add_argument("message")
    mark_parser.add_argument("--output", action="append", default=[])
    mark_parser.add_argument("--next-checkpoint")
    wait_parser = subparsers.add_parser("wait-cpu")
    wait_parser.add_argument("component")
    wait_parser.add_argument("--interval", type=float, default=1.0)
    stage_parser = subparsers.add_parser("stage-complete")
    stage_parser.add_argument("stage")
    subparsers.add_parser("verify-baseline")
    args = parser.parse_args(argv)
    if args.mode == "status":
        write_status(args.component, args.state, args.message, log=args.log)
        result = {"complete": True}
    elif args.mode == "mark":
        result = mark_stage(
            args.stage,
            args.status,
            [ROOT / value for value in args.output],
            args.message,
            args.next_checkpoint,
        )
    elif args.mode == "wait-cpu":
        result = wait_cpu(args.component, args.interval)
    elif args.mode == "stage-complete":
        complete = stage_complete(args.stage)
        result = {"stage": args.stage, "complete": complete}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if complete else 1
    else:
        result = {"files": len(verify_baseline())}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
