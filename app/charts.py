"""Geometry for the charts the pages draw on the server: scales, ticks, and SVG points.

A plot is an SVG with viewBox 0 0 1000 300 stretched to its box, so lines and areas are drawn in those units. Dots
and labels are HTML laid over it at percentages of the box, which keeps them round and readable at any size.
"""

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


def compact_money(value: float) -> str:
    """Returns a short dollar amount for an axis or a chart label, e.g. "$0", "$750", "$1.5K", "$12K", "-$2.5M"."""
    sign, value = ("-" if value < 0 else ""), abs(value)
    for size, suffix in ((1_000_000, "M"), (1_000, "K")):
        if value >= size:
            number = value / size
            text = f"{number:.1f}".removesuffix(".0") if number < 10 else f"{number:.0f}"
            return f"{sign}${text}{suffix}"
    return f"{sign}${value:,.0f}"


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
            end = {"left": x_percent(len(values) - 1, count), "top": y_percent(values[-1], low, high), "value": values[-1]}
        drawn.append({**s, "points": points, "area_path": area, "end": end})
    return {
        "count": count, "low": low, "high": high,
        "ticks": [{"label": compact_money(t), "top": y_percent(t, low, high)} for t in axis["ticks"]],
        "series": drawn,
        "markers": [{**m, "top": y_percent(m["value"], low, high)} for m in markers],
        "hover": {"labels": labels, "low": low, "high": high,
                  "series": [{"name": s["name"], "role": s["role"], "values": s["values"]} for s in series]},
    }


def bar_heights(values: list[float]) -> dict:
    """Returns {"high", "ticks", "heights"} for a column chart of `values`: a round top for the axis, its ticks with
    their positions, and each column's height as a percentage of the plot (0 for anything not above zero)."""
    axis = scale([max(v, 0.0) for v in values])
    high = axis["high"]
    return {
        "high": high,
        "ticks": [{"label": compact_money(t), "top": y_percent(t, 0.0, high)} for t in axis["ticks"]],
        "heights": [max(0.0, v) / high * 100 for v in values],
    }
