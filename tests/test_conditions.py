from __future__ import annotations

from typing import Any

from daylog.sources import fly, surf

PEAK = {
    "name": "Peak",
    "surf": {
        "swell": [170, 250],
        "best_swell": 220,
        "offshore": 100,
        "tide": ["mid", "high"],
        "size": [0.8, 3.0],
        "min_period": 10,
    },
}


def _marine(hours: int, h: float, t: float, d: float, levels: list[float]) -> dict[str, Any]:
    times = [f"2026-09-29T{i:02d}:00" for i in range(hours)]
    return {
        "hourly": {
            "time": times,
            "swell_wave_height": [h] * hours,
            "swell_wave_period": [t] * hours,
            "swell_wave_direction": [d] * hours,
            "sea_level_height_msl": levels,
        }
    }


def _wind(hours: int, speeds: list[float], dirs: list[float]) -> dict[str, Any]:
    times = [f"2026-09-29T{i:02d}:00" for i in range(hours)]
    return {"hourly": {"time": times, "wind_speed_10m": speeds, "wind_direction_10m": dirs}}


def test_profile_requires_swell_and_offshore() -> None:
    assert surf.SurfProfile.from_node(PEAK) is not None
    assert surf.SurfProfile.from_node({"surf": {"swell": [1, 2]}}) is None
    assert surf.SurfProfile.from_node({}) is None


def test_offshore_beats_onshore_and_direction_matters() -> None:
    p = surf.SurfProfile.from_node(PEAK)
    assert p is not None
    base: dict[str, Any] = dict(
        swell_h=1.5, swell_t=13, swell_dir=220, gust_kn=0, tide="mid", tide_trend="rising"
    )
    from datetime import datetime

    t = datetime(2026, 9, 29, 8)
    offshore = surf.rate(surf.Hour(time=t, wind_kn=10, wind_dir=100, **base), p)
    onshore = surf.rate(surf.Hour(time=t, wind_kn=10, wind_dir=280, **base), p)
    wrong_swell = surf.rate(
        surf.Hour(time=t, wind_kn=0, wind_dir=0, **{**base, "swell_dir": 60}),
        p,
    )
    assert offshore > 4 and onshore < 2 and wrong_swell == 0


def test_tide_states_scale_to_each_day() -> None:
    states = surf._tide_states([0.0, 0.5, 1.0, 0.5], ["d"] * 4)
    assert [s for s, _ in states] == ["low", "mid", "high", "mid"]
    assert states[0][1] == "rising" and states[2][1] == "falling"


def test_report_line_names_the_best_window() -> None:
    hours = 24
    levels = [((i % 12) - 6) / 6 for i in range(hours)]
    wind_dirs = [100.0 if i < 12 else 280.0 for i in range(hours)]  # offshore AM, onshore PM
    wind_speeds = [4.0 if i < 12 else 15.0 for i in range(hours)]
    parsed = surf.parse(_marine(hours, 1.5, 13, 220, levels), _wind(hours, wind_speeds, wind_dirs))
    p = surf.SurfProfile.from_node(PEAK)
    assert p is not None
    text = surf.spot_report("Peak", p, parsed, comfort_face=1.5)
    assert text.startswith("Peak:\n  Tue 29:")
    assert "SW" in text and "above your 1.5m comfort" in text
    assert "poor" in text.lower()  # the onshore afternoon is called out


def test_fly_rules_are_the_stricter_of_site_and_user() -> None:
    node = {"fly": {"launch": [135, 250], "elevation": 1600, "max_wind": 14, "max_gust": 20}}
    p = fly.FlyProfile.from_node(node, {"max_gust": 15, "max_wind": 10})
    assert p is not None and p.max_gust == 15 and p.max_wind == 10


def _air(**kw: Any) -> fly.AirHour:
    from datetime import datetime

    base: dict[str, Any] = dict(
        time=datetime(2026, 10, 12, 11),
        wind_launch=3,
        dir_launch=190,
        wind_10m=3,
        gust_10m=15,
        precip_prob=0,
        cape=500,
        spread_c=7,
        low_cloud=0,
    )
    base.update(kw)
    return fly.AirHour(**base)


def test_thermal_gusts_in_light_wind_do_not_ground_you() -> None:
    p = fly.FlyProfile.from_node(
        {"fly": {"launch": [135, 250], "elevation": 1600}}, {"max_gust": 15}
    )
    assert p is not None
    assert fly.reasons_not_flyable(_air(), p) == []
    keys = [k for k, _ in fly.reasons_not_flyable(_air(wind_launch=9, gust_10m=19, wind_10m=8), p)]
    assert "gusts" in keys and "gust-spread" in keys
    keys = [
        k for k, _ in fly.reasons_not_flyable(_air(dir_launch=20, wind_launch=8, gust_10m=10), p)
    ]
    assert keys == ["direction"]
    keys = [k for k, _ in fly.reasons_not_flyable(_air(cape=2200, precip_prob=20), p)]
    assert keys == ["overdevelopment"]


def test_fly_day_line() -> None:
    p = fly.FlyProfile.from_node({"fly": {"launch": [135, 250], "elevation": 1600}})
    assert p is not None
    good = [_air(time=_air().time.replace(hour=h)) for h in range(9, 13)]
    bad = [_air(time=_air().time.replace(hour=h), precip_prob=80) for h in range(13, 17)]
    line = fly.day_line(good + bad, p, site_ground=900)
    assert line.startswith("Mon 12: FLYABLE 09-13 (4h)")
    assert "base ~1,775m" in line
    assert fly.day_line(bad, p).endswith("NOT FLYABLE (rain)")
