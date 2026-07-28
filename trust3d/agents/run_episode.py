"""仅使用公开 episode 输入执行 Gate 4 离线证据路由。"""

import argparse
import json
from pathlib import Path

from trust3d.data.select_events import read_jsonl


ROUTES = {"trust_memory", "reobserve"}
SUPPORTED_METHODS = {
    "always_trust",
    "always_reobserve",
    "global_ttl",
    "fact_freshness",
    "trust3d",
}
FORBIDDEN_INPUT_KEYS = {
    "branch",
    "current_answer",
    "hidden_intervention",
    "historical_answer",
    "memory_is_stale",
    "shortest_verification_cost",
}


def _atomic_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            for nested in _walk_keys(child):
                yield nested
    elif isinstance(value, list):
        for child in value:
            for nested in _walk_keys(child):
                yield nested


def _load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "global_ttl_steps",
        "fact_ttl_steps",
        "trust3d",
        "utility_cost_weights",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ValueError("配置缺少字段: {}".format(", ".join(missing)))
    return config


def _validate_public_episode(episode):
    leaked = sorted(set(_walk_keys(episode)) & FORBIDDEN_INPUT_KEYS)
    if leaked:
        raise ValueError(
            "公开 episode 包含禁止字段: {}".format(", ".join(leaked))
        )
    cost = episode.get("verification_cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise ValueError("公开 episode 缺少有效 verification_cost")


def _base_route(episode, method, route, reason):
    if route not in ROUTES:
        raise ValueError("不支持的 route: {}".format(route))
    return {
        "episode_id": episode["episode_id"],
        "group_id": episode["group_id"],
        "split": episode["split"],
        "method": method,
        "policy_id": method,
        "route": route,
        "route_reason": reason,
        "public_verification_cost": episode["verification_cost"],
    }


def _route_baseline(episode, method, config):
    if method == "always_trust":
        return _base_route(episode, method, "trust_memory", "固定信任历史事实")
    if method == "always_reobserve":
        return _base_route(episode, method, "reobserve", "固定重新观察")
    if method == "global_ttl":
        expired = episode["elapsed_steps"] > config["global_ttl_steps"]
        return _base_route(
            episode,
            method,
            "reobserve" if expired else "trust_memory",
            "全局 TTL 已过期" if expired else "全局 TTL 仍有效",
        )
    if method == "fact_freshness":
        ttl = config["fact_ttl_steps"]["articulated_state"]
        at_risk = episode["public_context"]["intervention_window"]
        expired = episode["elapsed_steps"] > ttl
        reobserve = at_risk and expired
        return _base_route(
            episode,
            method,
            "reobserve" if reobserve else "trust_memory",
            "高可变事实存在公开风险且已过期"
            if reobserve
            else "事实时效仍满足阈值",
        )
    raise ValueError("不支持的 baseline: {}".format(method))


def _route_trust3d(episode, config, cost_weight):
    settings = config["trust3d"]
    at_risk = episode["public_context"]["intervention_window"]
    error_probability = (
        settings["risk_error_probability"]
        if at_risk
        else settings["fresh_error_probability"]
    )
    trust_loss = error_probability * settings["error_penalty"]
    reobserve_loss = cost_weight * episode["verification_cost"]
    exceeds_risk = error_probability > settings["max_error_probability"]
    reobserve = exceeds_risk and reobserve_loss < trust_loss
    route = _base_route(
        episode,
        "trust3d",
        "reobserve" if reobserve else "trust_memory",
        "预期重新观察损失更低"
        if reobserve
        else "历史事实风险在阈值内或重新观察代价更高",
    )
    route.update(
        {
            "policy_id": "trust3d_lambda_{:g}".format(cost_weight),
            "cost_weight": cost_weight,
            "estimated_error_probability": error_probability,
            "trust_loss": trust_loss,
            "reobserve_loss": reobserve_loss,
        }
    )
    return route


def run(episodes_path, methods, config_path, output_path):
    config = _load_config(config_path)
    unsupported = sorted(set(methods) - SUPPORTED_METHODS)
    if unsupported:
        raise ValueError("不支持的方法: {}".format(", ".join(unsupported)))

    episodes = read_jsonl(episodes_path)
    if len({item["episode_id"] for item in episodes}) != len(episodes):
        raise ValueError("公开 episode_id 必须唯一")
    routes = []
    for episode in episodes:
        _validate_public_episode(episode)
        for method in methods:
            if method == "trust3d":
                for weight in config["trust3d"]["route_cost_weights"]:
                    routes.append(_route_trust3d(episode, config, weight))
            else:
                routes.append(_route_baseline(episode, method, config))

    routes.sort(key=lambda item: (item["policy_id"], item["episode_id"]))
    _atomic_jsonl(output_path, routes)
    return {
        "episode_count": len(episodes),
        "route_count": len(routes),
        "policy_ids": sorted({item["policy_id"] for item in routes}),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", required=True, type=Path)
    parser.add_argument("--methods", nargs="+", required=True)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    report = run(args.episodes, args.methods, args.config, args.output)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
