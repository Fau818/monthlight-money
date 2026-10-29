"""Geometry for the charts the pages draw on the server: scales, ticks, and SVG points.

A plot is an SVG with viewBox 0 0 1000 300 stretched to its box, so lines and areas are drawn in those units. Dots
and labels are HTML laid over it at percentages of the box, which keeps them round and readable at any size.
"""

import functools
import itertools
import math

WIDTH, HEIGHT = 1000, 300


def nice_step(span: float, parts: int = 3) -> float:
    """Returns a round step (1, 2, 2.5 or 5 times a power of ten) that divides `span` into about `parts` parts."""
    if span <= 0:
        return 1.0
    raw = span / parts
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 2.5, 5, 10) if raw <= m * power * 1.0000001)


def scale(values: list[float], zero: bool = True, parts: int = 3) -> dict:
    """Returns {"low", "high", "ticks"}: an axis with round ticks that covers `values`, and 0 too when `zero`."""
    low, high = min(values, default=0.0), max(values, default=0.0)
    if zero:
        low, high = min(low, 0.0), max(high, 0.0)
    if high == low:
        high = low + (abs(low) or 100.0)
    step = nice_step(high - low, parts)
    low, high = math.floor(low / step + 1e-9) * step, math.ceil(high / step - 1e-9) * step
    ticks = [round(low + i * step, 2) for i in range(round((high - low) / step) + 1)]
    return {"low": low, "high": high, "ticks": ticks}


def compact_money(value: float, digits: int | None = None) -> str:
    """Returns a short dollar amount for an axis or a chart label, e.g. "$0", "$750", "$1.5K", "$12K", "-$2.5M"; with
    `digits`, thousands and millions get that many decimals, like "$16.25K"."""
    sign, value = ("-" if value < 0 else ""), abs(value)
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if value >= size:
            number = value / size
            if digits is not None:
                text = f"{number:.{digits}f}"
            else:
                text = f"{number:.1f}".removesuffix(".0") if number < 10 else f"{number:.0f}"
            return f"{sign}${text}{suffix}"
    return f"{sign}${value:,.0f}"


def label_digits(ticks: list[float]) -> int | None:
    """Returns how many decimals the labels of an axis's evenly spaced `ticks` need: None when compact_money's own
    rounding tells them apart, else as many as the step between them takes (2 for ticks $250 apart above $10K)."""
    if len({compact_money(t) for t in ticks}) == len(ticks):
        return None
    unit = 1_000_000 if max(abs(t) for t in ticks) >= 1_000_000 else 1_000
    step = f"{(ticks[1] - ticks[0]) / unit:.6f}".rstrip("0")
    return len(step.split(".")[1])


def x_percent(index: int, count: int) -> float:
    """Returns how far across the plot point `index` of `count` sits, from 0 to 100."""
    return 0.0 if count <= 1 else index / (count - 1) * 100


def y_percent(value: float, low: float, high: float) -> float:
    """Returns how far down the plot `value` sits on a low..high axis, from 0 (top) to 100 (bottom)."""
    return (high - value) / (high - low) * 100


def _point(index: int, value: float, count: int, low: float, high: float) -> str:
    return f"{x_percent(index, count) * WIDTH / 100:.1f},{y_percent(value, low, high) * HEIGHT / 100:.1f}"


def line_chart(labels: list[str], series: list[dict], zero: bool = True, markers: list[dict] = ()) -> dict:
    """Returns what the line chart macro draws for `series` over x positions named by `labels`.

    Each series is {"name", "values", "role"} and may be shorter than `labels` (a month so far). Markers are
    {"label", "value"} levels drawn across the plot, such as a budget. The result has the axis (low, high, ticks
    with their positions), each series with its SVG points, an area under it when it has "area": True, and its
    last point, the markers with their positions, and the data the hover readout needs."""
    count = len(labels)
    axis = scale([v for s in series for v in s["values"]] + [m["value"] for m in markers], zero=zero)
    low, high = axis["low"], axis["high"]
    digits = label_digits(axis["ticks"])  # NOTE: the last point's label too, so it reads like the axis beside it
    drawn = []
    for s in series:
        values = s["values"]
        points = " ".join(_point(i, v, count, low, high) for i, v in enumerate(values))
        area = None
        if s.get("area") and len(values) > 1:
            base = y_percent(max(low, min(high, 0.0)), low, high) * HEIGHT / 100
            area = f"M0,{base:.1f} L{points.replace(' ', ' L')} L{x_percent(len(values) - 1, count) * WIDTH / 100:.1f},{base:.1f} Z"
        end = None
        if values:
            end = {"left": x_percent(len(values) - 1, count), "top": y_percent(values[-1], low, high), "value": values[-1],
                   "label": compact_money(values[-1], digits)}
        drawn.append({**s, "points": points, "area_path": area, "end": end})
    return {
        "count": count, "low": low, "high": high,
        "ticks": [{"label": compact_money(t, digits), "top": y_percent(t, low, high)} for t in axis["ticks"]],
        "series": drawn,
        "markers": [{**m, "top": y_percent(m["value"], low, high)} for m in markers],
        "hover": {"labels": labels, "low": low, "high": high,
                  "series": [{"name": s["name"], "role": s["role"], "values": s["values"]} for s in series]},
    }


def bar_heights(values: list[float]) -> dict:
    """Returns {"high", "ticks", "heights"} for a column chart of `values`: a round top for the axis, its ticks with
    their positions, and each column's height as a percentage of the plot (0 for anything not above zero)."""
    axis = scale([max(v, 0.0) for v in values])
    high, digits = axis["high"], label_digits(axis["ticks"])
    return {
        "high": high,
        "ticks": [{"label": compact_money(t, digits), "top": y_percent(t, 0.0, high)} for t in axis["ticks"]],
        "heights": [max(0.0, v) / high * 100 for v in values],
    }


def stacked_bars(columns: list[str], series: list[dict]) -> dict:
    """Returns what the stacked bars macro draws for `series` ({"key", "values": {column: amount}}, the bottom one
    first) over `columns`: an axis for the columns' tops, and for each column its "top" (what's above zero adds up to),
    its "height" as a percentage of the plot, and its "segments", each series' "share" of the column's height. An
    amount below zero (refunds that outweigh what was spent) isn't drawn; the readout and the table show it."""
    tops = [sum(max(s["values"].get(c, 0.0), 0.0) for s in series) for c in columns]
    axis = scale(tops)
    high, digits = axis["high"], label_digits(axis["ticks"])
    drawn = []
    for column, top in zip(columns, tops):
        segments = [{"key": s["key"], "value": s["values"].get(column, 0.0),
                     "share": max(s["values"].get(column, 0.0), 0.0) / top * 100 if top else 0.0} for s in series]
        drawn.append({"column": column, "top": round(top, 2), "height": top / high * 100, "segments": segments})
    return {"high": high, "digits": digits, "columns": drawn,
            "ticks": [{"label": compact_money(t, digits), "top": y_percent(t, 0.0, high)} for t in axis["ticks"]]}


# The data-viz palette's colors as style.css has them (--c-*), in light and dark mode, for telling apart the colors a
# chart puts side by side. Red is kept for "over budget"; gray is "Other".
HUES = {
    "light": {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100", "magenta": "#e87ba4",
              "green": "#008300", "violet": "#6250d6", "gray": "#7d8ca0"},
    "dark": {"blue": "#3987e5", "orange": "#d95926", "aqua": "#199e70", "yellow": "#c98500", "magenta": "#d55181",
             "green": "#008300", "violet": "#9085e9", "gray": "#7d8ca0"},
}
# Machado, Oliveira and Fernandes (2009): how red-blind (protan) and green-blind (deutan) eyes see linear RGB.
_CVD = {"protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
        "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))}
NORMAL_FLOOR, CVD_TARGET = 15.0, 8.0  # the OKLab differences (×100) two neighbors need, as the data-viz checks have them


def _linear(color: str) -> tuple[float, ...]:
    channels = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)


def _oklab(r: float, g: float, b: float) -> tuple[float, float, float]:
    l, m, s = (math.cbrt(x) for x in (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
                                      0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
                                      0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b))
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s, 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _seen(color: str, kind: str | None) -> tuple[float, float, float]:
    rgb = _linear(color)
    if kind:
        rgb = tuple(max(0.0, min(1.0, sum(w * c for w, c in zip(row, rgb)))) for row in _CVD[kind])
    return _oklab(*rgb)


@functools.cache
def separation(first: str, second: str) -> float:
    """Returns how well two of the palette's colors (HUES names) tell apart side by side, in both modes: the worst of
    their OKLab difference over NORMAL_FLOOR and, as color-blind eyes see them, over CVD_TARGET; 1 or more is enough."""
    worst = math.inf
    for hues in HUES.values():
        for kind, floor in ((None, NORMAL_FLOOR), ("protan", CVD_TARGET), ("deutan", CVD_TARGET)):
            a, b = _seen(hues[first], kind), _seen(hues[second], kind)
            worst = min(worst, 100 * math.dist(a, b) / floor)
    return worst


def best_order(colors: list[str]) -> list[int]:
    """Returns the order to stack series of `colors` (HUES names) in, bottom first, so the worst pair of neighbors tells
    apart best; of orders as good, the first in the given order. A last "gray" (Other) stays on top."""
    top = [len(colors) - 1] if colors and colors[-1] == "gray" else []
    pair = {(i, j): separation(colors[i], colors[j]) for i in range(len(colors)) for j in range(len(colors)) if i != j}
    worst = lambda order: min((pair[a, b] for a, b in itertools.pairwise(order)), default=math.inf)
    best = {"order": list(range(len(colors))), "score": worst(range(len(colors)))}

    def extend(order: list[int], score: float, left: list[int]) -> None:
        # NOTE: an order whose worst neighbors so far are no better than the best one's can't end up better
        if score <= best["score"] + 1e-9:
            return
        if not left:
            score = min(score, pair[order[-1], top[0]]) if top and order else score
            if score > best["score"] + 1e-9:
                best.update(order=order + top, score=score)
            return
        for i in left:
            extend(order + [i], min(score, pair[order[-1], i]) if order else score, [j for j in left if j != i])

    extend([], math.inf, [i for i in range(len(colors)) if i not in top])
    return best["order"]
