"""Shared atomic artifacts, protocol checks, and resource admission."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Union


ROOT = Path("/224010104/Jerry/trust3d")
JERRY = Path("/224010104/Jerry")
OUTPUT_ROOT = ROOT / "outputs/parallel_v2"
CHECKPOINT_ROOT = JERRY / "checkpoints/parallel_v2"
LOG_ROOT = JERRY / "logs/parallel_v2"
PROTOCOL_PATH = ROOT / "configs/parallel_v2_protocol.json"

FORBIDDEN_PUBLIC_KEYS = {
    "branch",
    "changed",
    "current_answer",
    "current_answer_gt",
    "historical_answer_gt",
    "memory_is_stale",
    "oracle_best_route",
    "private_answer",
    "route_losses",
    "simulator_oracle_answer",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Union[os.PathLike, str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_bytes(path: Union[os.PathLike, str], payload: bytes, mode: int = 0o644) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp.{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, destination)
    descriptor = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Union[os.PathLike, str], value: Any, mode: int = 0o644) -> None:
    atomic_bytes(path, canonical_bytes(value) + b"\n", mode=mode)


def atomic_jsonl(
    path: Union[os.PathLike, str], values: Iterable[dict[str, Any]], mode: int = 0o644
) -> None:
    payload = b"".join(canonical_bytes(value) + b"\n" for value in values)
    atomic_bytes(path, payload, mode=mode)


def load_json(path: Union[os.PathLike, str]) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_jsonl(path: Union[os.PathLike, str]) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def protocol() -> dict[str, Any]:
    value = load_json(PROTOCOL_PATH)
    if value.get("protocol_revision") != "parallel-v2":
        raise ValueError("protocol revision is not parallel-v2")
    return value


def walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def assert_public_record(value: dict[str, Any]) -> None:
    leaked = sorted(set(walk_keys(value)) & FORBIDDEN_PUBLIC_KEYS)
    if leaked:
        raise ValueError("public record contains private fields: " + ", ".join(leaked))


def repository_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def repository_dirty() -> bool:
    output = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    )
    return bool(output.strip())


def path_manifest(paths: Iterable[Union[os.PathLike, str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise FileNotFoundError(path)
        files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
        for item in files:
            records.append(
                {
                    "path": item.relative_to(ROOT).as_posix(),
                    "size": item.stat().st_size,
                    "sha256": sha256_file(item),
                }
            )
    records.sort(key=lambda item: item["path"])
    return records


def verify_path_manifest(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    checked = []
    for expected in records:
        path = ROOT / expected["path"]
        actual = sha256_file(path) if path.is_file() else None
        checked.append(
            {
                "path": expected["path"],
                "expected_sha256": expected["sha256"],
                "actual_sha256": actual,
                "match": actual == expected["sha256"],
            }
        )
    if not all(item["match"] for item in checked):
        raise RuntimeError("immutable baseline manifest changed")
    return checked


def baseline_manifest_path() -> Path:
    return OUTPUT_ROOT / "protocol/baseline_manifest.json"


def verify_baseline() -> list[dict[str, Any]]:
    value = load_json(baseline_manifest_path())
    return verify_path_manifest(value["files"])


def stage_path(stage: str) -> Path:
    return CHECKPOINT_ROOT / "stages" / f"{stage}.json"


def stage_complete(stage: str) -> bool:
    path = stage_path(stage)
    if not path.is_file():
        return False
    try:
        value = load_json(path)
        if value.get("status") != "complete":
            return False
        for output in value.get("outputs", []):
            if sha256_file(ROOT / output["path"]) != output["sha256"]:
                return False
        return True
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return False


def mark_stage(
    stage: str,
    status: str,
    outputs: Iterable[Union[os.PathLike, str]] = (),
    message: str = "",
    next_checkpoint: str | None = None,
) -> dict[str, Any]:
    output_records = []
    for raw in outputs:
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        output_records.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "stage": stage,
        "status": status,
        "message": message,
        "updated_at": utc_now(),
        "host": platform.node(),
        "git_commit": repository_commit(),
        "git_dirty": repository_dirty(),
        "outputs": output_records,
        "next_checkpoint": next_checkpoint,
    }
    atomic_json(stage_path(stage), value)
    return value


def resource_snapshot() -> dict[str, Any]:
    load1, load5, load15 = os.getloadavg()
    cpu_count = os.cpu_count() or 1
    memory = {}
    with Path("/proc/meminfo").open("r", encoding="ascii") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) >= 2:
                memory[fields[0].rstrip(":")] = int(fields[1])
    disk = shutil.disk_usage(JERRY)
    gpus = []
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
        for line in output.splitlines():
            index, name, total, used, free, utilization, temperature = [
                item.strip() for item in line.split(",")
            ]
            gpus.append(
                {
                    "index": int(index),
                    "name": name,
                    "memory_total_mib": int(total),
                    "memory_used_mib": int(used),
                    "memory_free_mib": int(free),
                    "utilization_percent": int(utilization),
                    "temperature_c": int(temperature),
                }
            )
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return {
        "captured_at": utc_now(),
        "host": platform.node(),
        "cpu_count": cpu_count,
        "load": [load1, load5, load15],
        "load1_fraction": load1 / cpu_count,
        "memory_available_gib": memory.get("MemAvailable", 0) / 1024 / 1024,
        "disk_available_gib": disk.free / 1024**3,
        "gpus": gpus,
    }


def cpu_admitted(snapshot: dict[str, Any] | None = None) -> bool:
    snapshot = snapshot or resource_snapshot()
    limits = protocol()["resources"]
    return (
        snapshot["load1_fraction"] <= limits["maximum_cpu_load_fraction"]
        and snapshot["memory_available_gib"] >= limits["minimum_available_memory_gib"]
        and snapshot["disk_available_gib"] >= limits["minimum_available_disk_gib"]
    )


def gpu_candidate(snapshot: dict[str, Any] | None = None) -> dict[str, Any] | None:
    snapshot = snapshot or resource_snapshot()
    limits = protocol()["resources"]
    candidates = [
        gpu
        for gpu in snapshot["gpus"]
        if gpu["memory_free_mib"] >= limits["minimum_free_gpu_mib"]
        and gpu["utilization_percent"] <= limits["maximum_gpu_utilization_percent"]
    ]
    return max(candidates, key=lambda gpu: gpu["memory_free_mib"], default=None)


def write_status(component: str, state: str, message: str, **extra: Any) -> None:
    value = {
        "schema_version": 1,
        "protocol_revision": "parallel-v2",
        "component": component,
        "state": state,
        "message": message,
        "updated_at": utc_now(),
        "host": platform.node(),
        **extra,
    }
    atomic_json(OUTPUT_ROOT / "status" / f"{component}.json", value)
