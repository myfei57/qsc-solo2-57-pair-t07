"""分区漏点定位：嫌疑排序、隔离/恢复时序、定压重算与审计一致性。"""

from __future__ import annotations

import pytest

from heatnet.config import ConfigError, config_to_mapping, default_config
from heatnet.console.api import Api
from heatnet.errors import InterlockError, NamespaceError
from heatnet.leak.rank import assess_leak_suspects
from heatnet.leak.state import ZoneObservation
from heatnet.ns.zone import Zone, ZoneRegistry


def _registry() -> ZoneRegistry:
    registry = ZoneRegistry()
    for name, stations in (
        ("north", ("st01", "st02", "st03")),
        ("south", ("st04", "st05")),
        ("east", ("st06",)),
    ):
        registry.add(Zone(name=name))
        for station in stations:
            registry.assign(station, name)
    return registry


def _assess(observations: list[ZoneObservation], window_s: float = 300.0) -> dict:
    return assess_leak_suspects(observations, default_config().leak, _registry(), window_s)


def test_ranking_fixed_criterion_is_drop_rate_then_makeup_then_name():
    ranking = _assess(
        [
            ZoneObservation(zone="south", drop_kpa=30.0, makeup_t=9.0),
            ZoneObservation(zone="north", drop_kpa=30.0, makeup_t=4.0),
            ZoneObservation(zone="east", drop_kpa=60.0, makeup_t=1.0),
        ]
    )["ranking"]
    # 压降速度是主判据：east 掉得最快排第一，哪怕补水增量最小。
    assert [item["zone"] for item in ranking] == ["east", "south", "north"]
    # 压降速度并列时补水增量定先后（south 9.0 > north 4.0）。
    assert ranking[1]["drop_rate_kpa_s"] == pytest.approx(ranking[2]["drop_rate_kpa_s"])

    tied = _assess(
        [
            ZoneObservation(zone="south", drop_kpa=30.0, makeup_t=5.0),
            ZoneObservation(zone="north", drop_kpa=30.0, makeup_t=5.0),
        ]
    )["ranking"]
    # 两项都并列时按分区名字典序，排序完全确定。
    assert [item["zone"] for item in tied] == ["north", "south"]


def test_missing_reading_is_partial_failure_and_suspect():
    report = _assess(
        [
            ZoneObservation(zone="north", drop_kpa=6.0, makeup_t=1.0),
            ZoneObservation(zone="south", drop_kpa=None, makeup_t=2.0),
            ZoneObservation(zone="east", drop_kpa=3.0, makeup_t=0.5),
        ]
    )
    assert report["ok"] is False
    assert report["partial"] is True
    assert report["violations"] == ["pressure_reading_missing"]
    # 读不到的区按可疑处理：进嫌疑名单，排在正常区之前。
    assert report["missing"] == ["south"]
    assert "south" in report["suspects"]
    verdict = {item["zone"]: item for item in report["ranking"]}["south"]
    assert verdict["suspect"] is True
    assert verdict["reading_missing"] is True
    east = {item["zone"]: item for item in report["ranking"]}["east"]
    assert verdict["rank"] < east["rank"]
    assert east["suspect"] is False


def test_clear_suspect_recommends_isolation():
    report = _assess(
        [
            ZoneObservation(zone="north", drop_kpa=3.0, makeup_t=0.5),
            ZoneObservation(zone="south", drop_kpa=4.0, makeup_t=0.5),
            ZoneObservation(zone="east", drop_kpa=45.0, makeup_t=2.0),
        ]
    )
    assert report["ok"] is True
    assert report["confidence"] == "clear"
    assert report["recommendation"]["action"] == "isolate"
    assert report["recommendation"]["zone"] == "east"
    assert report["recommendation"]["reason"] == "clear_suspect"


def test_unclear_suspects_topology_allows_isolate_first():
    # north 与 south 压降速度拉不开余量（比值 < clear_margin_ratio），嫌疑不明；
    # 拓扑允许（south 站数占比 2/6 ≤ 0.5，隔离后还剩 2 区）→ 先隔离最像的那个。
    report = _assess(
        [
            ZoneObservation(zone="north", drop_kpa=20.0, makeup_t=2.0),
            ZoneObservation(zone="south", drop_kpa=19.0, makeup_t=2.0),
            ZoneObservation(zone="east", drop_kpa=2.0, makeup_t=0.5),
        ]
    )
    assert report["confidence"] == "unclear"
    assert report["recommendation"]["action"] == "isolate"
    assert report["recommendation"]["zone"] == "north"
    assert report["recommendation"]["reason"] == "unclear_isolate_first"


def test_unclear_suspects_topology_guard_keeps_whole_network_makeup():
    registry = ZoneRegistry()
    registry.add(Zone(name="bulk"))
    for station in ("st01", "st02", "st03"):
        registry.assign(station, "bulk")
    registry.add(Zone(name="rim"))
    registry.assign("st04", "rim")
    # bulk 占 3/4 = 0.75 > 0.5，嫌疑不明时拓扑守住：整网继续补水、加密观测。
    report = assess_leak_suspects(
        [
            ZoneObservation(zone="bulk", drop_kpa=20.0, makeup_t=2.0),
            ZoneObservation(zone="rim", drop_kpa=19.0, makeup_t=1.0),
        ],
        default_config().leak,
        registry,
        300.0,
    )
    assert report["confidence"] == "unclear"
    recommendation = report["recommendation"]
    assert recommendation["action"] == "observe"
    assert recommendation["reason"] == "unclear_observe"
    assert recommendation["detail"]["makeup"] == "whole_network"
    assert recommendation["detail"]["sampling"] == "dense"


def test_no_suspect_recommends_normal_observation():
    report = _assess(
        [
            ZoneObservation(zone="north", drop_kpa=3.0, makeup_t=0.5),
            ZoneObservation(zone="south", drop_kpa=2.0, makeup_t=0.4),
        ]
    )
    assert report["ok"] is True
    assert report["confidence"] == "none"
    assert report["suspects"] == []
    assert report["recommendation"]["action"] == "observe"
    assert report["recommendation"]["reason"] == "no_suspect"


def test_observation_validation_rejects_bad_input():
    with pytest.raises(ValueError):
        ZoneObservation.from_mapping({"zone": "", "drop_kpa": 1.0})
    with pytest.raises(ValueError):
        ZoneObservation.from_mapping({"zone": "north", "drop_kpa": -1.0})
    with pytest.raises(ValueError):
        ZoneObservation.from_mapping({"zone": "north", "drop_kpa": 1.0, "makeup_t": -0.5})
    with pytest.raises(ValueError):
        _assess([ZoneObservation(zone="north", drop_kpa=1.0, makeup_t=0.0)], window_s=0.0)
    with pytest.raises(ValueError):
        _assess(
            [
                ZoneObservation(zone="north", drop_kpa=1.0, makeup_t=0.0),
                ZoneObservation(zone="north", drop_kpa=2.0, makeup_t=0.0),
            ]
        )


def test_leak_config_section_roundtrip_and_validation():
    config = default_config()
    assert config.leak.suspect_rate_kpa_s > 0.0
    assert config.leak.min_remaining_zones >= 1
    broken = config_to_mapping(config)
    broken["limits"]["leak"]["max_isolate_station_share"] = 1.5
    with pytest.raises(ConfigError):
        type(config).from_mapping(broken)


# ---- 平台集成：时序、定压重算与审计一致性 ------------------------------


def _observations(**drops: float | None) -> list[dict]:
    return [
        {"zone": zone, "drop_kpa": drop, "makeup_t": 1.0}
        for zone, drop in drops.items()
    ]


def test_isolate_requires_assessment_first(platform):
    with pytest.raises(InterlockError):
        platform.isolate_zone("east")


def test_isolate_recomputes_remaining_zone_pressure(platform):
    platform.assess_leaks(_observations(north=3.0, south=4.0, east=45.0))
    report = platform.isolate_zone("east")
    assert report["ok"] is True
    assert [step["action"] for step in report["steps"]] == ["isolate", "replan"]
    plan = report["plan"]
    baseline = platform.pressure.baseline.value_kpa()
    assert plan["zones"]["east"]["state"] == "isolated"
    assert plan["zones"]["east"]["setpoint_kpa"] is None
    assert plan["zones"]["east"]["makeup_share"] == 0.0
    # 其余区定压跟着重算：目标取基线，补水配额按站数占比重新分摊。
    assert plan["zones"]["north"]["setpoint_kpa"] == pytest.approx(baseline)
    assert plan["zones"]["north"]["makeup_share"] == pytest.approx(3 / 5)
    assert plan["zones"]["south"]["makeup_share"] == pytest.approx(2 / 5)
    assert platform.leak_report()["plan"] == plan


def test_isolation_sequencing_interlocks(platform):
    platform.assess_leaks(_observations(north=3.0, south=4.0, east=45.0))
    platform.isolate_zone("east")
    with pytest.raises(InterlockError):
        platform.isolate_zone("east")
    with pytest.raises(NamespaceError):
        platform.isolate_zone("nope")
    platform.assess_leaks(_observations(north=30.0, south=4.0))
    platform.isolate_zone("south")
    # north 是最后一个在役分区，再隔离就越过下限。
    with pytest.raises(InterlockError):
        platform.isolate_zone("north")
    assert platform.leak_report()["isolated"] == ["east", "south"]


def test_recover_sequence_pressurises_then_rejoins_then_replans(platform):
    with pytest.raises(InterlockError):
        platform.recover_zone("east")
    platform.assess_leaks(_observations(north=3.0, south=4.0, east=45.0))
    platform.isolate_zone("east")
    report = platform.recover_zone("east")
    assert report["ok"] is True
    assert report["rejoined"] is True
    # 恢复时序：先补水充压，再并网，最后重算定压。
    assert [step["action"] for step in report["steps"]] == ["makeup", "rejoin", "replan"]
    plan = report["plan"]
    assert plan["zones"]["east"]["state"] == "active"
    assert plan["zones"]["east"]["setpoint_kpa"] == pytest.approx(
        platform.pressure.baseline.value_kpa()
    )
    assert plan["zones"]["north"]["makeup_share"] == pytest.approx(3 / 6)
    assert platform.leak_report()["isolated"] == []
    assert platform.leak_report()["recoveries"] == 1


def test_retrofit_refreshes_leak_plan(platform):
    platform.assess_leaks(_observations(north=3.0, south=4.0, east=45.0))
    platform.isolate_zone("east")
    target = platform.pressure.baseline.limits.baseline_max_kpa - 10.0
    report = platform.retrofit_baseline(target)
    assert report["ok"] is True
    plan = platform.leak_report()["plan"]
    assert plan["baseline_kpa"] == pytest.approx(target)
    assert plan["zones"]["north"]["setpoint_kpa"] == pytest.approx(target)
    assert plan["zones"]["east"]["setpoint_kpa"] is None


def test_audit_and_console_share_one_judgement(platform):
    api = Api(platform)
    status, assessed = api.handle(
        "POST",
        "/api/leak/assess",
        {},
        {"observations": _observations(north=3.0, south=4.0, east=45.0), "window_s": 300.0},
    )
    assert status == 200
    assert assessed["suspects"] == ["east"]
    status, isolated = api.handle("POST", "/api/zone/isolate", {}, {"zone": "east"})
    assert status == 200

    # 审计里落的是同一份判断：嫌疑区、隔离与定压重算都能查到。
    actions = [entry["action"] for entry in platform.audit.entries(limit=50)]
    assert "suspect" in actions
    assert "isolate" in actions
    assert "replan" in actions
    suspect_entries = [
        entry
        for entry in platform.audit.entries(limit=50)
        if entry["action"] == "suspect" and entry["payload"].get("zone") == "east"
    ]
    assert suspect_entries

    # 控制台展示的嫌疑分区与定压计划同审计落盘的一致。
    status, leaks = api.handle("GET", "/api/leaks", {}, {})
    assert status == 200
    assert leaks["suspects"] == ["east"]
    assert leaks["assessment"]["criterion"] == "pressure_drop_rate"
    assert leaks["isolated"] == ["east"]
    assert leaks["plan"] == isolated["plan"]


def test_partial_assessment_lands_in_audit_as_not_ok(platform):
    report = platform.assess_leaks(_observations(north=3.0, south=None, east=4.0))
    assert report["ok"] is False
    assert report["partial"] is True
    assert "south" in report["suspects"]
    alarms = platform.audit.alarms()
    assert any(
        entry["payload"].get("violation") == "pressure_reading_missing" for entry in alarms
    )
    summary = [entry for entry in platform.audit.entries(limit=10) if entry["action"] == "leak_assess_incomplete"]
    assert summary
    # 部分失败不能当正常：控制台读到的仍是带 violations 的同一份评估。
    leaks = platform.leak_report()
    assert leaks["assessment"]["ok"] is False
    assert leaks["assessment"]["violations"] == ["pressure_reading_missing"]


def test_api_leak_endpoints_reject_bad_input(platform):
    api = Api(platform)
    status, payload = api.handle(
        "POST", "/api/leak/assess", {}, {"observations": [{"zone": "north", "drop_kpa": -1.0}]}
    )
    assert status == 400
    assert payload["error"] == "bad_request"
    status, payload = api.handle(
        "POST",
        "/api/leak/assess",
        {},
        {"observations": [{"zone": "nope", "drop_kpa": 1.0}]},
    )
    assert status == 409
    assert payload["component"] == "ns"
    status, payload = api.handle("POST", "/api/zone/isolate", {}, {"zone": "east"})
    assert status == 409
    assert payload["component"] == "leak"
    status, payload = api.handle("POST", "/api/zone/recover", {}, {"zone": "east"})
    assert status == 409
    assert payload["component"] == "leak"
