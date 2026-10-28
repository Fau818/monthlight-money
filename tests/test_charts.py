from app import charts


def test_axes_get_round_ticks():
    assert charts.nice_step(4170) == 2000
    assert charts.scale([0, 2910.79, 4170]) == {"low": 0, "high": 6000, "ticks": [0, 2000, 4000, 6000]}
    assert charts.scale([20500, 23400], zero=False)["low"] == 20000
    assert charts.scale([])["high"] > 0  # an empty axis still has a size


def test_compact_money():
    values = [0, 750, 1000, 1500, 12000, 2_500_000, -2500]
    assert [charts.compact_money(v) for v in values] == ["$0", "$750", "$1K", "$1.5K", "$12K", "$2.5M", "-$2.5K"]


def test_line_chart_geometry():
    chart = charts.line_chart(["Oct 1", "Oct 2", "Oct 3"], [{"name": "October", "values": [0, 50], "role": "main", "area": True}],
                              markers=[{"label": "Budget", "value": 100}])
    assert (chart["low"], chart["high"]) == (0, 100)
    assert [t["label"] for t in chart["ticks"]] == ["$0", "$50", "$100"]
    [line] = chart["series"]
    assert line["points"] == "0.0,300.0 500.0,150.0"  # a series shorter than the labels stops partway
    assert line["area_path"] == "M0,300.0 L0.0,300.0 L500.0,150.0 L500.0,300.0 Z"
    assert line["end"] == {"left": 50.0, "top": 50.0, "value": 50}
    assert chart["markers"][0]["top"] == 0.0
    assert chart["hover"]["labels"] == ["Oct 1", "Oct 2", "Oct 3"]


def test_bar_heights():
    bars = charts.bar_heights([1000, 2500, 0, -20])
    assert bars["high"] == 3000 and bars["heights"] == [1000 / 3000 * 100, 2500 / 3000 * 100, 0.0, 0.0]
