#!/usr/bin/env python3
"""Dependency-free SVG figures for the logical-cluster performance report."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path
import statistics
from typing import Any
from xml.etree import ElementTree as ET


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

COLORS = {
    "maze": "#c2412d",
    "ray": "#2563eb",
    "text": "#26734d",
    "vision": "#7c3aed",
    "grid": "#d7dce2",
    "axis": "#40464f",
    "muted": "#69717d",
    "background": "#ffffff",
}


class SvgCanvas:
    def __init__(self, width: int, height: int, title: str) -> None:
        self.width = width
        self.height = height
        self.root = ET.Element(
            f"{{{SVG_NS}}}svg",
            {
                "width": str(width),
                "height": str(height),
                "viewBox": f"0 0 {width} {height}",
                "role": "img",
                "aria-label": title,
            },
        )
        ET.SubElement(self.root, f"{{{SVG_NS}}}title").text = title
        self.rect(0, 0, width, height, fill=COLORS["background"])

    def element(self, name: str, **attrs: object) -> ET.Element:
        return ET.SubElement(
            self.root,
            f"{{{SVG_NS}}}{name}",
            {key.replace("_", "-"): str(value) for key, value in attrs.items()},
        )

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        fill: str,
        stroke: str | None = None,
        stroke_width: float = 1,
        rx: float = 0,
    ) -> None:
        attrs: dict[str, object] = {
            "x": round(x, 3),
            "y": round(y, 3),
            "width": max(0, round(width, 3)),
            "height": max(0, round(height, 3)),
            "fill": fill,
            "rx": rx,
        }
        if stroke is not None:
            attrs.update(stroke=stroke, stroke_width=stroke_width)
        self.element("rect", **attrs)

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        stroke: str,
        stroke_width: float = 1,
        dash: str | None = None,
    ) -> None:
        attrs: dict[str, object] = {
            "x1": round(x1, 3),
            "y1": round(y1, 3),
            "x2": round(x2, 3),
            "y2": round(y2, 3),
            "stroke": stroke,
            "stroke_width": stroke_width,
        }
        if dash is not None:
            attrs["stroke_dasharray"] = dash
        self.element("line", **attrs)

    def circle(self, x: float, y: float, radius: float, *, fill: str) -> None:
        self.element(
            "circle",
            cx=round(x, 3),
            cy=round(y, 3),
            r=radius,
            fill=fill,
        )

    def polyline(
        self,
        points: Sequence[tuple[float, float]],
        *,
        stroke: str,
        stroke_width: float = 2,
        dash: str | None = None,
    ) -> None:
        if len(points) < 2:
            return
        attrs: dict[str, object] = {
            "points": " ".join(f"{x:.2f},{y:.2f}" for x, y in points),
            "fill": "none",
            "stroke": stroke,
            "stroke_width": stroke_width,
            "stroke_linejoin": "round",
            "stroke_linecap": "round",
        }
        if dash is not None:
            attrs["stroke_dasharray"] = dash
        self.element("polyline", **attrs)

    def text(
        self,
        x: float,
        y: float,
        value: object,
        *,
        size: int = 14,
        fill: str = "#20242a",
        anchor: str = "start",
        weight: int = 400,
    ) -> None:
        node = self.element(
            "text",
            x=round(x, 3),
            y=round(y, 3),
            fill=fill,
            font_size=size,
            font_family="DejaVu Sans, Arial, sans-serif",
            text_anchor=anchor,
            font_weight=weight,
        )
        node.text = str(value)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(self.root).write(
            path,
            encoding="utf-8",
            xml_declaration=True,
        )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _results(summary: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw = summary.get("results")
    if not isinstance(raw, list):
        return {}
    return {
        str(item.get("executor")): item
        for item in raw
        if isinstance(item, Mapping) and item.get("executor") in {"maze", "ray"}
    }


def _records(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = result.get("records")
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _request_stats(
    result: Mapping[str, object],
    *,
    family: str | None = None,
    workflow: str | None = None,
) -> Mapping[str, object]:
    breakdowns = _mapping(result.get("breakdowns"))
    if workflow is not None:
        group = _mapping(_mapping(breakdowns.get("workflows")).get(workflow))
    elif family is not None:
        group = _mapping(_mapping(breakdowns.get("families")).get(family))
    else:
        group = _mapping(breakdowns.get("overall"))
    return _mapping(group.get("requests"))


def _figure_title(canvas: SvgCanvas, title: str, subtitle: str) -> None:
    canvas.text(40, 38, title, size=22, weight=700)
    canvas.text(40, 62, subtitle, size=13, fill=COLORS["muted"])


def _legend(canvas: SvgCanvas, x: float, y: float) -> None:
    for offset, executor in enumerate(("maze", "ray")):
        left = x + offset * 105
        canvas.rect(left, y - 11, 18, 10, fill=COLORS[executor], rx=2)
        canvas.text(left + 25, y - 2, executor.capitalize(), size=13)


def _bar_panel(
    canvas: SvgCanvas,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    values: Mapping[str, float],
    formatter: Any,
) -> None:
    canvas.text(x, y, title, size=16, weight=600)
    plot_top = y + 28
    plot_height = height - 56
    maximum = max(values.values(), default=1.0)
    maximum = maximum if maximum > 0 else 1.0
    for tick in range(5):
        tick_value = maximum * tick / 4
        tick_y = plot_top + plot_height - plot_height * tick / 4
        canvas.line(x, tick_y, x + width, tick_y, stroke=COLORS["grid"])
        canvas.text(x - 7, tick_y + 5, formatter(tick_value), size=11, anchor="end", fill=COLORS["muted"])
    bar_width = min(82.0, width / 4)
    centers = (x + width * 0.34, x + width * 0.68)
    for center, executor in zip(centers, ("maze", "ray"), strict=True):
        value = values.get(executor, 0.0)
        bar_height = plot_height * value / maximum
        canvas.rect(
            center - bar_width / 2,
            plot_top + plot_height - bar_height,
            bar_width,
            bar_height,
            fill=COLORS[executor],
            rx=3,
        )
        canvas.text(center, plot_top + plot_height + 21, executor.capitalize(), size=12, anchor="middle")
        canvas.text(
            center,
            max(plot_top + 15, plot_top + plot_height - bar_height - 8),
            formatter(value),
            size=12,
            anchor="middle",
            weight=600,
        )


def _overall_figure(results: Mapping[str, Mapping[str, object]], path: Path) -> None:
    canvas = SvgCanvas(1280, 540, "Overall performance comparison")
    _figure_title(
        canvas,
        "Overall performance",
        "Fixed Batch=20; lower latency and makespan are better, higher throughput is better",
    )
    panels = (
        (
            "E2E P95 (s)",
            {
                name: (_number(_request_stats(result).get("p95_e2e_ms")) or 0) / 1000
                for name, result in results.items()
            },
            lambda value: f"{value:.1f}",
        ),
        (
            "Makespan (s)",
            {
                name: (_number(_request_stats(result).get("makespan_ms")) or 0) / 1000
                for name, result in results.items()
            },
            lambda value: f"{value:.0f}",
        ),
        (
            "Throughput (req/s)",
            {
                name: _number(
                    _request_stats(result).get("throughput_requests_per_second")
                )
                or 0
                for name, result in results.items()
            },
            lambda value: f"{value:.3f}",
        ),
    )
    for index, (title, values, formatter) in enumerate(panels):
        _bar_panel(
            canvas,
            x=80 + index * 410,
            y=105,
            width=330,
            height=365,
            title=title,
            values=values,
            formatter=formatter,
        )
    canvas.save(path)


def _family_figure(results: Mapping[str, Mapping[str, object]], path: Path) -> None:
    canvas = SvgCanvas(1280, 560, "Text and vision performance comparison")
    _figure_title(
        canvas,
        "Text versus vision",
        "The same 14 text and 6 vision requests are paired across executors",
    )
    _legend(canvas, 1010, 50)
    panels = (
        ("E2E P95 (s)", "p95_e2e_ms", 0.001, lambda value: f"{value:.0f}"),
        (
            "Throughput (req/s)",
            "throughput_requests_per_second",
            1.0,
            lambda value: f"{value:.3f}",
        ),
    )
    for panel_index, (title, key, multiplier, formatter) in enumerate(panels):
        left = 75 + panel_index * 620
        top = 110
        width = 520
        height = 350
        canvas.text(left, top, title, size=16, weight=600)
        values = {
            (executor, family): (
                _number(_request_stats(result, family=family).get(key)) or 0
            )
            * multiplier
            for executor, result in results.items()
            for family in ("text", "vision")
        }
        maximum = max(values.values(), default=1.0) or 1.0
        plot_top = top + 28
        plot_height = height - 45
        for tick in range(5):
            tick_y = plot_top + plot_height - plot_height * tick / 4
            canvas.line(left, tick_y, left + width, tick_y, stroke=COLORS["grid"])
            canvas.text(
                left - 7,
                tick_y + 5,
                formatter(maximum * tick / 4),
                size=11,
                anchor="end",
                fill=COLORS["muted"],
            )
        for family_index, family in enumerate(("text", "vision")):
            group_center = left + width * (0.28 + family_index * 0.48)
            for executor_index, executor in enumerate(("maze", "ray")):
                value = values.get((executor, family), 0.0)
                bar_height = plot_height * value / maximum
                bar_left = group_center + (executor_index - 0.5) * 62 - 24
                canvas.rect(
                    bar_left,
                    plot_top + plot_height - bar_height,
                    48,
                    bar_height,
                    fill=COLORS[executor],
                    rx=3,
                )
                canvas.text(
                    bar_left + 24,
                    max(plot_top + 14, plot_top + plot_height - bar_height - 7),
                    formatter(value),
                    size=11,
                    anchor="middle",
                )
            canvas.text(
                group_center,
                plot_top + plot_height + 24,
                family.capitalize(),
                size=13,
                anchor="middle",
                weight=600,
            )
    canvas.save(path)


def _paired_records(
    results: Mapping[str, Mapping[str, object]],
) -> list[tuple[Mapping[str, object], Mapping[str, object]]]:
    maze = {str(item.get("sample_id")): item for item in _records(results["maze"])}
    ray = {str(item.get("sample_id")): item for item in _records(results["ray"])}
    return [(maze[sample_id], ray[sample_id]) for sample_id in sorted(maze.keys() & ray.keys())]


def _paired_speedup_figure(
    results: Mapping[str, Mapping[str, object]], path: Path
) -> None:
    pairs = []
    for maze, ray in _paired_records(results):
        maze_ms = _number(maze.get("client_e2e_ms"))
        ray_ms = _number(ray.get("client_e2e_ms"))
        if maze_ms and ray_ms and maze_ms > 0 and ray_ms > 0:
            pairs.append((math.log2(ray_ms / maze_ms), maze, ray))
    pairs.sort(key=lambda item: item[0])
    canvas = SvgCanvas(1360, 860, "Paired request speedup")
    _figure_title(
        canvas,
        "Paired request speedup",
        "log2(Ray E2E / Maze E2E); right of 1x means Maze completed the same sample faster",
    )
    left = 360
    right = 1280
    top = 105
    row_height = 34
    bound = max((abs(item[0]) for item in pairs), default=1.0)
    bound = max(1.0, math.ceil(bound))

    def x_for(score: float) -> float:
        return left + (score + bound) / (2 * bound) * (right - left)

    center = x_for(0)
    for exponent in range(-int(bound), int(bound) + 1):
        x = x_for(float(exponent))
        canvas.line(
            x,
            top - 18,
            x,
            top + row_height * len(pairs),
            stroke=COLORS["axis"] if exponent == 0 else COLORS["grid"],
            stroke_width=2 if exponent == 0 else 1,
        )
        canvas.text(x, top - 26, f"{2 ** exponent:.2g}x", size=11, anchor="middle")
    for index, (score, maze, ray) in enumerate(pairs):
        y = top + index * row_height + 12
        label = f"{maze.get('dataset')}.{maze.get('workflow')}  #{maze.get('request_index')}"
        canvas.text(left - 12, y + 5, label, size=11, anchor="end")
        x = x_for(score)
        family = str(maze.get("family"))
        canvas.rect(
            min(center, x),
            y - 7,
            abs(x - center),
            14,
            fill=COLORS.get(family, COLORS["muted"]),
            rx=2,
        )
        ratio = 2**score
        canvas.text(
            x + (7 if score >= 0 else -7),
            y + 5,
            f"{ratio:.2f}x",
            size=10,
            anchor="start" if score >= 0 else "end",
            weight=600,
        )
    canvas.rect(1030, 805, 14, 10, fill=COLORS["text"])
    canvas.text(1051, 814, "Text", size=12)
    canvas.rect(1120, 805, 14, 10, fill=COLORS["vision"])
    canvas.text(1141, 814, "Vision", size=12)
    canvas.save(path)


def _workflow_figure(results: Mapping[str, Mapping[str, object]], path: Path) -> None:
    workflow_values: dict[str, dict[str, float]] = {}
    for executor, result in results.items():
        grouped: dict[str, list[float]] = {}
        for record in _records(result):
            value = _number(record.get("client_e2e_ms"))
            if value is not None:
                key = f"{record.get('dataset')}.{record.get('workflow')}"
                grouped.setdefault(key, []).append(value / 1000)
        for workflow, values in grouped.items():
            workflow_values.setdefault(workflow, {})[executor] = statistics.fmean(values)
    workflows = sorted(workflow_values)
    all_values = [value for values in workflow_values.values() for value in values.values()]
    minimum = max(1.0, min(all_values, default=1.0) * 0.8)
    maximum = max(minimum * 2, max(all_values, default=2.0) * 1.2)
    log_min = math.log10(minimum)
    log_max = math.log10(maximum)
    canvas = SvgCanvas(1320, 720, "Per-workflow mean E2E")
    _figure_title(
        canvas,
        "Per-workflow mean E2E",
        "Dumbbell plot on a logarithmic axis; workflows contain one or two fixed requests",
    )
    _legend(canvas, 1050, 50)
    left = 330
    right = 1240
    top = 115
    row_height = 39

    def x_for(value: float) -> float:
        return left + (math.log10(value) - log_min) / (log_max - log_min) * (right - left)

    start_power = math.floor(log_min)
    end_power = math.ceil(log_max)
    for power in range(start_power, end_power + 1):
        value = 10**power
        if minimum <= value <= maximum:
            x = x_for(value)
            canvas.line(x, top - 18, x, top + row_height * len(workflows), stroke=COLORS["grid"])
            canvas.text(x, top - 25, f"{value:g}s", size=11, anchor="middle")
    for index, workflow in enumerate(workflows):
        y = top + index * row_height + 11
        canvas.text(left - 14, y + 5, workflow, size=11, anchor="end")
        values = workflow_values[workflow]
        if "maze" in values and "ray" in values:
            canvas.line(
                x_for(values["maze"]),
                y,
                x_for(values["ray"]),
                y,
                stroke=COLORS["muted"],
                stroke_width=2,
            )
        for executor in ("maze", "ray"):
            if executor in values:
                x = x_for(values[executor])
                canvas.circle(x, y, 6, fill=COLORS[executor])
                canvas.text(
                    x + (9 if executor == "ray" else -9),
                    y + 4,
                    f"{values[executor]:.0f}",
                    size=9,
                    anchor="start" if executor == "ray" else "end",
                    fill=COLORS[executor],
                )
    canvas.save(path)


def _ecdf_figure(results: Mapping[str, Mapping[str, object]], path: Path) -> None:
    series: dict[tuple[str, str], list[float]] = {}
    for executor, result in results.items():
        for family in ("text", "vision"):
            series[(executor, family)] = sorted(
                value / 1000
                for record in _records(result)
                if record.get("family") == family
                for value in [_number(record.get("client_e2e_ms"))]
                if value is not None and value > 0
            )
    all_values = [value for values in series.values() for value in values]
    minimum = max(1.0, min(all_values, default=1.0) * 0.8)
    maximum = max(minimum * 2, max(all_values, default=2.0) * 1.2)
    log_min = math.log10(minimum)
    log_max = math.log10(maximum)
    canvas = SvgCanvas(1240, 650, "E2E empirical cumulative distribution")
    _figure_title(
        canvas,
        "E2E latency ECDF",
        "Batch-level request distribution; this is not cross-run statistical uncertainty",
    )
    left, right, top, bottom = 100.0, 1170.0, 105.0, 565.0
    for tick in range(6):
        fraction = tick / 5
        y = bottom - fraction * (bottom - top)
        canvas.line(left, y, right, y, stroke=COLORS["grid"])
        canvas.text(left - 10, y + 5, f"{fraction:.1f}", size=11, anchor="end")
    for power in range(math.floor(log_min), math.ceil(log_max) + 1):
        value = 10**power
        if minimum <= value <= maximum:
            x = left + (math.log10(value) - log_min) / (log_max - log_min) * (right - left)
            canvas.line(x, top, x, bottom, stroke=COLORS["grid"])
            canvas.text(x, bottom + 24, f"{value:g}s", size=11, anchor="middle")
    for executor in ("maze", "ray"):
        for family in ("text", "vision"):
            values = series.get((executor, family), [])
            points: list[tuple[float, float]] = []
            for index, value in enumerate(values, start=1):
                x = left + (math.log10(value) - log_min) / (log_max - log_min) * (right - left)
                y = bottom - index / len(values) * (bottom - top)
                points.append((x, y))
            canvas.polyline(
                points,
                stroke=COLORS[executor],
                stroke_width=3 if family == "text" else 2,
                dash=None if family == "text" else "8 5",
            )
    legends = (
        ("Maze text", COLORS["maze"], None),
        ("Maze vision", COLORS["maze"], "8 5"),
        ("Ray text", COLORS["ray"], None),
        ("Ray vision", COLORS["ray"], "8 5"),
    )
    for index, (label, color, dash) in enumerate(legends):
        x = 720 + index * 125
        canvas.line(x, 76, x + 28, 76, stroke=color, stroke_width=3, dash=dash)
        canvas.text(x + 34, 81, label, size=11)
    canvas.text(27, 340, "ECDF", size=13, weight=600)
    canvas.text((left + right) / 2, 620, "Client E2E latency (log scale)", size=13, anchor="middle")
    canvas.save(path)


def _timeline_figure(results: Mapping[str, Mapping[str, object]], path: Path) -> None:
    canvas = SvgCanvas(1320, 960, "Request completion timeline")
    _figure_title(
        canvas,
        "Request completion timeline",
        "All 20 requests are admitted at offset zero; rows are ordered by completion time",
    )
    for panel_index, executor in enumerate(("maze", "ray")):
        records = sorted(
            _records(results[executor]),
            key=lambda item: _number(item.get("client_e2e_finished_at_ms")) or 0,
        )
        start = min(
            (_number(item.get("client_e2e_started_at_ms")) or 0 for item in records),
            default=0,
        )
        end = max(
            (_number(item.get("client_e2e_finished_at_ms")) or start for item in records),
            default=start + 1,
        )
        duration = max(1.0, (end - start) / 1000)
        left, right = 330.0, 1240.0
        top = 115.0 + panel_index * 415
        row_height = 18.0
        canvas.text(55, top + 10, executor.capitalize(), size=16, weight=700, fill=COLORS[executor])
        for tick in range(6):
            value = duration * tick / 5
            x = left + (right - left) * tick / 5
            canvas.line(x, top - 12, x, top + row_height * len(records), stroke=COLORS["grid"])
            canvas.text(x, top - 20, f"{value / 60:.1f}m", size=10, anchor="middle")
        for index, record in enumerate(records):
            y = top + index * row_height + 8
            sample_start = ((_number(record.get("client_e2e_started_at_ms")) or start) - start) / 1000
            sample_end = ((_number(record.get("client_e2e_finished_at_ms")) or start) - start) / 1000
            x1 = left + sample_start / duration * (right - left)
            x2 = left + sample_end / duration * (right - left)
            label = f"{record.get('dataset')}.{record.get('workflow')} #{record.get('request_index')}"
            canvas.text(left - 12, y + 4, label, size=9, anchor="end")
            canvas.line(
                x1,
                y,
                x2,
                y,
                stroke=COLORS.get(str(record.get("family")), COLORS["muted"]),
                stroke_width=7,
            )
            canvas.circle(x2, y, 4, fill=COLORS[executor])
    canvas.rect(1040, 925, 14, 10, fill=COLORS["text"])
    canvas.text(1061, 934, "Text", size=12)
    canvas.rect(1130, 925, 14, 10, fill=COLORS["vision"])
    canvas.text(1151, 934, "Vision", size=12)
    canvas.save(path)


def _read_resource_samples(result: Mapping[str, object]) -> list[Mapping[str, object]]:
    value = result.get("resource_samples_path")
    if not isinstance(value, str):
        return []
    path = Path(value)
    if not path.is_file():
        return []
    samples: list[Mapping[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, Mapping):
                samples.append(item)
    resources = _mapping(result.get("resources"))
    start = _number(resources.get("window_started_at_ms"))
    finish = _number(resources.get("window_finished_at_ms"))
    if start is None or finish is None:
        return samples
    return [
        item
        for item in samples
        if (timestamp := _number(item.get("timestamp_ms"))) is not None
        and start <= timestamp <= finish
    ]


def _downsample(values: Sequence[Any], maximum: int = 700) -> list[Any]:
    if len(values) <= maximum:
        return list(values)
    return [values[round(index * (len(values) - 1) / (maximum - 1))] for index in range(maximum)]


def _resource_figure(results: Mapping[str, Mapping[str, object]], path: Path) -> bool:
    samples = {executor: _read_resource_samples(result) for executor, result in results.items()}
    available = [executor for executor in ("maze", "ray") if samples.get(executor)]
    if not available:
        return False
    canvas = SvgCanvas(1320, 980, "CPU, NPU and HBM resource timeline")
    _figure_title(
        canvas,
        "Cluster resource timeline",
        "Eight logical nodes; HBM is incremental over each executor's pre-workload baseline",
    )
    _legend(canvas, 1070, 50)
    missing = [executor.capitalize() for executor in ("maze", "ray") if executor not in available]
    if missing:
        canvas.text(
            760,
            81,
            f"Host samples unavailable: {', '.join(missing)}",
            size=12,
            fill=COLORS["muted"],
        )
    metrics = (
        ("Container CPU mean (%)", "cluster_cpu_utilization_pct"),
        ("Eight-NPU mean utilization (%)", "cluster_npu_utilization_pct"),
        ("Incremental HBM (GiB)", "cluster_hbm_used_mb"),
    )
    for panel_index, (title, key) in enumerate(metrics):
        left, right = 105.0, 1240.0
        top = 105.0 + panel_index * 285
        bottom = top + 225
        series: dict[str, list[tuple[float, float]]] = {}
        maximum_x = 1.0
        maximum_y = 1.0
        for executor, result in results.items():
            if not samples.get(executor):
                continue
            raw = _downsample(samples[executor])
            first = _number(raw[0].get("timestamp_ms")) or 0
            baseline_hbm = _number(_mapping(result.get("resources")).get("baseline_hbm_mb")) or 0
            points: list[tuple[float, float]] = []
            for sample in raw:
                timestamp = _number(sample.get("timestamp_ms"))
                value = _number(sample.get(key))
                if timestamp is None or value is None:
                    continue
                if key == "cluster_hbm_used_mb":
                    value = max(0.0, value - baseline_hbm) / 1024
                x_value = (timestamp - first) / 1000 / 60
                points.append((x_value, value))
                maximum_x = max(maximum_x, x_value)
                maximum_y = max(maximum_y, value)
            series[executor] = points
        canvas.text(left, top - 12, title, size=15, weight=600)
        for tick in range(6):
            x = left + (right - left) * tick / 5
            y = bottom - (bottom - top) * tick / 5
            canvas.line(x, top, x, bottom, stroke=COLORS["grid"])
            canvas.line(left, y, right, y, stroke=COLORS["grid"])
            canvas.text(x, bottom + 20, f"{maximum_x * tick / 5:.1f}m", size=10, anchor="middle")
            canvas.text(left - 10, y + 4, f"{maximum_y * tick / 5:.1f}", size=10, anchor="end")
        for executor, points in series.items():
            canvas.polyline(
                [
                    (
                        left + x_value / maximum_x * (right - left),
                        bottom - y_value / maximum_y * (bottom - top),
                    )
                    for x_value, y_value in points
                ],
                stroke=COLORS[executor],
                stroke_width=2,
            )
    canvas.save(path)
    return True


def _concurrency_figure(
    results: Mapping[str, Mapping[str, object]], path: Path
) -> bool:
    samples = {executor: _read_resource_samples(result) for executor, result in results.items()}
    if not any(samples.get(executor) for executor in ("maze", "ray")):
        return False
    canvas = SvgCanvas(1320, 650, "Per-device NPU process concurrency")
    _figure_title(
        canvas,
        "Per-card model process concurrency",
        "Each cell is the maximum observed NPU process count in a normalized time bin",
    )
    colors = ("#edf2f7", "#f2c14e", "#e76f51", "#9b2226")
    columns = 180
    for panel_index, executor in enumerate(("maze", "ray")):
        left, right = 160.0, 1240.0
        top = 115.0 + panel_index * 245
        row_height = 24.0
        raw = samples[executor]
        if not raw:
            canvas.text(
                48,
                top + 12,
                executor.capitalize(),
                size=16,
                weight=700,
                fill=COLORS[executor],
            )
            canvas.text(
                left,
                top + 80,
                "Host resource samples unavailable",
                size=14,
                fill=COLORS["muted"],
            )
            continue
        bins = [[0 for _ in range(columns)] for _ in range(8)]
        for sample_index, sample in enumerate(raw):
            column = min(columns - 1, int(sample_index / max(1, len(raw)) * columns))
            npus = sample.get("npus")
            if not isinstance(npus, list):
                continue
            for npu in npus:
                if not isinstance(npu, Mapping):
                    continue
                try:
                    device = int(str(npu.get("physical_device_id")))
                except ValueError:
                    continue
                processes = npu.get("processes")
                if 0 <= device < 8 and isinstance(processes, list):
                    bins[device][column] = max(bins[device][column], len(processes))
        canvas.text(48, top + 12, executor.capitalize(), size=16, weight=700, fill=COLORS[executor])
        cell_width = (right - left) / columns
        for device in range(8):
            y = top + device * row_height
            canvas.text(left - 12, y + 16, f"NPU {device}", size=10, anchor="end")
            for column, count in enumerate(bins[device]):
                canvas.rect(
                    left + column * cell_width,
                    y,
                    cell_width + 0.2,
                    row_height - 2,
                    fill=colors[min(3, count)],
                )
        canvas.text(left, top + 8 * row_height + 20, "0%", size=10)
        canvas.text(right, top + 8 * row_height + 20, "100%", size=10, anchor="end")
    for count, color in enumerate(colors):
        x = 930 + count * 85
        canvas.rect(x, 615, 16, 10, fill=color)
        canvas.text(x + 22, 624, f"{count if count < 3 else '3+'}", size=11)
    canvas.save(path)
    return True


def write_figures(
    summary: Mapping[str, object], output_dir: Path
) -> list[dict[str, str]]:
    """Write all figures supported by the available evidence."""

    results = _results(summary)
    if set(results) != {"maze", "ray"}:
        return []
    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    figures: list[dict[str, str]] = []

    def render(
        figure_id: str,
        title: str,
        description: str,
        function: Any,
    ) -> None:
        path = figure_dir / f"{figure_id}.svg"
        function(results, path)
        figures.append(
            {
                "id": figure_id,
                "title": title,
                "path": str(path.relative_to(output_dir)),
                "description": description,
            }
        )

    render(
        "overall",
        "整体性能",
        "E2E P95、Makespan 和吞吐量",
        _overall_figure,
    )
    render(
        "family",
        "文本与视觉",
        "文本/VL 的 P95 和吞吐量",
        _family_figure,
    )
    render(
        "paired_speedup",
        "逐样本配对加速比",
        "同一样本的 Ray E2E 与 Maze E2E 比值",
        _paired_speedup_figure,
    )
    render(
        "workflow_latency",
        "逐 Workflow 延迟",
        "14 种 Workflow 的配对平均 E2E",
        _workflow_figure,
    )
    render(
        "e2e_ecdf",
        "E2E ECDF",
        "文本和视觉请求的批内延迟分布",
        _ecdf_figure,
    )
    render(
        "request_timeline",
        "请求完成时间线",
        "Batch=20 的并发执行和长尾",
        _timeline_figure,
    )
    resource_path = figure_dir / "resource_timeline.svg"
    if _resource_figure(results, resource_path):
        resource_available = [
            executor
            for executor, result in results.items()
            if _read_resource_samples(result)
        ]
        figures.append(
            {
                "id": "resource_timeline",
                "title": "CPU/NPU/HBM 时间线",
                "path": str(resource_path.relative_to(output_dir)),
                "description": (
                    "宿主侧每秒资源采样；当前可用执行器："
                    + ", ".join(sorted(resource_available))
                ),
            }
        )
    concurrency_path = figure_dir / "device_concurrency.svg"
    if _concurrency_figure(results, concurrency_path):
        figures.append(
            {
                "id": "device_concurrency",
                "title": "每卡模型进程并发",
                "path": str(concurrency_path.relative_to(output_dir)),
                "description": (
                    "8 张物理 NPU 的并发进程热力图；缺少采样的执行器明确留空"
                ),
            }
        )
    (figure_dir / "manifest.json").write_text(
        json.dumps(figures, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return figures
