"""用 Pillow 生成不依赖额外绘图库的 Gate 4 位图。"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1200
HEIGHT = 800
MARGIN = {"left": 110, "right": 50, "top": 70, "bottom": 100}
COLORS = {
    "always_trust": "#C4473A",
    "always_reobserve": "#167D8D",
    "global_ttl": "#D18B21",
    "fact_freshness": "#52796F",
    "clairvoyant_oracle": "#333333",
    "trust3d": "#2455A4",
}


def _font(size):
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _policy_kind(policy_id):
    return "trust3d" if policy_id.startswith("trust3d_lambda_") else policy_id


def _new_canvas(title):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#F7F8F5")
    draw = ImageDraw.Draw(image)
    draw.text((MARGIN["left"], 24), title, fill="#1D2428", font=_font(28))
    return image, draw


def _axes(draw, x_label, y_label, x_max):
    x0 = MARGIN["left"]
    x1 = WIDTH - MARGIN["right"]
    y0 = HEIGHT - MARGIN["bottom"]
    y1 = MARGIN["top"]
    draw.line((x0, y0, x1, y0), fill="#30373B", width=3)
    draw.line((x0, y0, x0, y1), fill="#30373B", width=3)
    font = _font(18)
    for tick in range(6):
        fraction = tick / 5.0
        x = x0 + fraction * (x1 - x0)
        y = y0 - fraction * (y0 - y1)
        draw.line((x, y0, x, y0 + 8), fill="#30373B", width=2)
        draw.line((x0 - 8, y, x0, y), fill="#30373B", width=2)
        draw.text(
            (x - 18, y0 + 14),
            "{:.1f}".format(fraction * x_max),
            fill="#30373B",
            font=font,
        )
        draw.text(
            (x0 - 64, y - 10),
            "{:.1f}".format(fraction),
            fill="#30373B",
            font=font,
        )
        if tick:
            draw.line((x0, y, x1, y), fill="#D8DCDA", width=1)
    draw.text(
        ((x0 + x1) / 2 - 100, HEIGHT - 44),
        x_label,
        fill="#1D2428",
        font=_font(20),
    )
    draw.text((18, y1 - 8), y_label, fill="#1D2428", font=_font(20))
    return x0, x1, y0, y1


def _save(image, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".tmp" + path.suffix)
    image.save(str(temporary), format="PNG")
    temporary.replace(path)


def _pareto_plot(report, output):
    points = report["pareto_points"]
    x_max = max(item["average_verification_cost"] for item in points) or 1
    x_max *= 1.08
    image, draw = _new_canvas("Gate 4: Accuracy vs. verification cost")
    x0, x1, y0, y1 = _axes(
        draw, "Average verification cost", "Accuracy", x_max
    )

    trust_points = []
    for point in points:
        x = x0 + point["average_verification_cost"] / x_max * (x1 - x0)
        y = y0 - point["answer_accuracy"] * (y0 - y1)
        if point["policy_id"].startswith("trust3d_lambda_"):
            trust_points.append((x, y))
    for start, end in zip(sorted(trust_points), sorted(trust_points)[1:]):
        draw.line(start + end, fill=COLORS["trust3d"], width=4)

    legend_y = MARGIN["top"] + 10
    seen = set()
    for point in points:
        policy_id = point["policy_id"]
        kind = _policy_kind(policy_id)
        x = x0 + point["average_verification_cost"] / x_max * (x1 - x0)
        y = y0 - point["answer_accuracy"] * (y0 - y1)
        color = COLORS.get(kind, "#666666")
        radius = 8 if kind == "trust3d" else 10
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=color,
            outline="white",
            width=2,
        )
        if kind not in seen:
            seen.add(kind)
            draw.rectangle(
                (WIDTH - 315, legend_y, WIDTH - 293, legend_y + 16), fill=color
            )
            draw.text(
                (WIDTH - 284, legend_y - 4),
                kind,
                fill="#1D2428",
                font=_font(16),
            )
            legend_y += 27
    _save(image, Path(output) / "accuracy_cost_pareto.png")


def _risk_plot(report, output):
    metrics = report["policy_metrics"]
    selected = [
        "always_trust",
        "always_reobserve",
        "global_ttl",
        "fact_freshness",
        report["primary_policy"],
        "clairvoyant_oracle",
    ]
    selected = [item for item in selected if item in metrics]
    image, draw = _new_canvas("Gate 4: Stale error and unnecessary reobserve")
    x0, x1, y0, y1 = _axes(draw, "Stale-memory error rate", "URR", 1.0)
    for policy_id in selected:
        values = metrics[policy_id]
        x = x0 + values["stale_memory_error_rate"] * (x1 - x0)
        y = y0 - values["unnecessary_reobserve_rate"] * (y0 - y1)
        kind = _policy_kind(policy_id)
        color = COLORS.get(kind, "#666666")
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=color)
        label = "Trust3D primary" if policy_id == report["primary_policy"] else kind
        draw.text((x + 14, y - 10), label, fill="#1D2428", font=_font(16))
    _save(image, Path(output) / "risk_tradeoff.png")


def plot(metrics_path, output):
    report = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
    _pareto_plot(report, output)
    _risk_plot(report, output)
    manifest = {
        "plots": ["accuracy_cost_pareto.png", "risk_tradeoff.png"],
        "primary_policy": report["primary_policy"],
    }
    path = Path(output) / "manifest.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return manifest


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    print(json.dumps(plot(args.metrics, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
