"""二次网漏点诊断、隔离时序、定压重算与控制台审计。"""

from __future__ import annotations

import pytest

from heatnet.console.api import Api
from heatnet.errors import InterlockError
from heatnet.leak.analysis import (
    LEAK_LOCATOR_METRIC,
    LeakObservation,
    analyse_leaks,
    rank_leak_suspects,
)
from heatnet.leak.isolation import LeakIsolationManager
from heatnet.leak.pressure import recalculate_setpoint
from heatnet.leak.topology import LeakTopology, ZoneHydraulics, build_default_leak_topology


def _observation(zone: str, start: float | None, end: float | None, makeup: float = 0.0) -> LeakObservation:
    if start is None or end is None:
        return LeakObservation.from_mapping(zone, {"elapsed_s": 300, "makeup_t": makeup})
    return LeakObservation.from_mapping(
        zone,
        {
            "elapsed_s": 300,
            "start_pressure_kpa": start,
            "end_pressure_kpa": end,
            "makeup_t": makeup,
        },
    )


def _topology(names=("north", "south", "east")) -> LeakTopology:
    zones = {
        name: ZoneHydraulics(
            name=name,
            required_setpoint_kpa=320.0 if name != "east" else 300.0,
            supply_valve=f"{name}.supply",
            return_valve=f"{name}.return",
            drain_valve=f"{name}.drain",
            pump=f"{name}.pump",
        )
        for name in names
    }
    return LeakTopology(zones, {("secondary_header", name) for name in names})


def _shared_topology() -> LeakTopology:
    zones = {
        name: ZoneHydraulics(
            name,
            320.0,
            f"{name}.supply",
            f"{name}.return",
            f"{name}.drain",
            pump=f"{name}.pump",
        )
        for name in ("trunk", "branch")
    }
    return LeakTopology(zones, {("secondary_header", "trunk"), ("trunk", "branch")})


def test_ranking_uses_only_pressure_decay_speed():
    decision = analyse_leaks(
        _topology(),
        [
            _observation("north", 320.0, 290.0, makeup=0.1),
            _observation("south", 320.0, 260.0, makeup=20.0),
            _observation("east", 320.0, 319.0, makeup=0.0),
        ],
    )
    assert decision.metric == LEAK_LOCATOR_METRIC == "pressure_decay_kpa_min"
    assert [item.zone for item in decision.suspects] == ["south", "north", "east"]
    assert decision.candidate_zone == "south"
    north = next(item for item in decision.suspects if item.zone == "north")
    assert north.makeup_corroborates is False
    south = next(item for item in decision.suspects if item.zone == "south")
    assert south.makeup_corroborates is True
    assert "补水量" in south.reason


def test_missing_pressure_is_partial_failure_and_suspicious_not_normal():
    ranked = rank_leak_suspects([_observation("north", 320.0, 290.0), _observation("south", None, None)])
    assert [item.zone for item in ranked] == ["north", "south"]
    missing = ranked[1]
    assert missing.pressure_available is False
    assert missing.pressure_decay_kpa_min is None
    assert missing.level == "suspect"
    assert "部分失败" in missing.reason


def test_unreported_zone_is_treated_as_missing():
    decision = analyse_leaks(_topology(), [_observation("north", 320.0, 290.0)])
    assert set(decision.missing_pressure_zones) == {"south", "east"}
    assert {item.zone for item in decision.suspects if not item.pressure_available} == {"south", "east"}
    assert "partial_failure:pressure_missing" in decision.warnings


def test_shared_upstream_keeps_makeup_and_requires_dense_observation():
    decision = analyse_leaks(
        _shared_topology(), [_observation("trunk", 320.0, 280.0), _observation("branch", 320.0, 319.0)]
    )
    assert decision.candidate_zone == "trunk"
    assert decision.action == "continue_makeup_observe"
    assert decision.isolatable is False
    assert "branch" in decision.isolation_reason
    assert decision.observe_window_s == 60
    assert "禁止整网盲目加大补水" in decision.makeup_policy


def test_independent_high_decay_zone_can_be_isolated():
    topology = _topology()
    decision = analyse_leaks(
        topology,
        [_observation("north", 320.0, 290.0), _observation("south", 320.0, 319.0)],
    )
    assert decision.action == "isolate_candidate"
    assert decision.candidate_zone == "north"
    assert decision.isolatable is True


def test_recalculate_setpoint_governed_by_remaining_zones_and_restore():
    topology = _topology()
    plan = recalculate_setpoint(topology, isolated={"north"})
    assert plan["target_kpa"] == 320.0
    assert plan["governing_zone"] == "south"
    assert plan["active_zones"] == ["east", "south"]
    restored = recalculate_setpoint(topology, isolated={"north"}, restored={"north"})
    assert restored["active_zones"] == ["east", "north", "south"]
    with pytest.raises(InterlockError):
        recalculate_setpoint(topology, isolated={"north", "south", "east"})


def test_isolation_and_restoration_step_order_is_fixed():
    topology = _topology(("north", "south"))
    decision = analyse_leaks(topology, [_observation("north", 320.0, 290.0), _observation("south", 320, 319)])
    manager = LeakIsolationManager(topology)
    report = manager.isolate(decision)
    assert report["ok"] is True
    assert report["zone"] == "north"
    assert [step["action"] for step in report["steps"]] == [
        "stop",
        "close",
        "close",
        "confirm_boundary_closed",
        "open",
        "recalculate_after_isolation",
    ]
    assert report["steps"][1]["detail"]["device"] == "north.supply"
    assert report["steps"][2]["detail"]["device"] == "north.return"
    assert report["setpoint_plan"]["target_kpa"] == 320.0

    restored = manager.restore("north")
    assert [step["action"] for step in restored["steps"]] == [
        "recalculate_before_restoration",
        "close",
        "open",
        "open",
        "confirm_boundary_open",
        "start",
    ]
    assert manager.isolated_zones() == []


def test_downstream_must_be_restored_before_upstream():
    topology = _shared_topology()
    manager = LeakIsolationManager(topology)
    manager._isolated.update({"trunk", "branch"})
    with pytest.raises(InterlockError, match="下游"):
        manager.restore("trunk")


def test_default_topology_tracks_leaf_zones():
    topology = build_default_leak_topology(["north", "south"], 320.0)
    assert topology.is_leaf("north") is True
    assert topology.describe()["zones"][0]["supply_valve"] == "north.supply-isolation"


def test_platform_analysis_console_and_audit_are_consistent(platform):
    api = Api(platform)
    observations = [
        {
            "zone": "north",
            "elapsed_s": 300,
            "start_pressure_kpa": 320,
            "end_pressure_kpa": 290,
            "makeup_t": 0.2,
        },
        {
            "zone": "south",
            "elapsed_s": 300,
            "start_pressure_kpa": 320,
            "end_pressure_kpa": 318,
            "makeup_t": 3.0,
        },
    ]
    status, decision = api.handle("POST", "/api/leaks/analyse", {}, {"observations": observations})
    assert status == 200
    assert decision["metric"] == "pressure_decay_kpa_min"
    assert decision["candidate_zone"] == "north"
    assert decision["missing_pressure_zones"] == ["east"]

    status, leaks = api.handle("GET", "/api/leaks", {}, {})
    assert status == 200
    assert leaks["last_decision"]["suspects"] == decision["suspects"]
    assert platform.status()["leaks"]["last_decision"]["suspects"] == decision["suspects"]
    assert platform.audit.count("leak_analysis") == 1
    audit_entry = platform.audit.entries(limit=1)[0]
    assert audit_entry["component"] == "leak"
    assert audit_entry["payload"]["metric"] == "pressure_decay_kpa_min"
    assert audit_entry["payload"]["missing_pressure_zones"] == ["east"]


def test_platform_isolation_recalculates_pressure_and_audits_sequence(platform):
    decision = platform.analyse_leaks(
        [
            {"zone": "north", "elapsed_s": 300, "start_pressure_kpa": 320, "end_pressure_kpa": 290},
            {"zone": "south", "elapsed_s": 300, "start_pressure_kpa": 320, "end_pressure_kpa": 319},
            {"zone": "east", "elapsed_s": 300, "start_pressure_kpa": 320, "end_pressure_kpa": 319},
        ]
    )
    assert decision["action"] == "isolate_candidate"
    isolation = platform.isolate_leak_candidate()
    assert isolation["ok"] is True
    assert isolation["setpoint_plan"]["active_zones"] == ["east", "south"]
    assert platform.pressure.baseline.value_kpa() == pytest.approx(320.0)
    assert platform.leaks.isolation.is_isolated("north") is True

    restoration = platform.restore_leak_zone("north")
    assert restoration["ok"] is True
    assert platform.leaks.isolation.is_isolated("north") is False
    assert platform.audit.count("leak_isolation") == 1
    assert platform.audit.count("leak_restoration") == 1


def test_console_blocks_isolation_when_decision_requires_observation(platform):
    build_shared_topology_for_platform(platform)
    decision = platform.analyse_leaks(
        [
            {"zone": "trunk", "elapsed_s": 300, "start_pressure_kpa": 320, "end_pressure_kpa": 280},
            {"zone": "branch", "elapsed_s": 300, "start_pressure_kpa": 320, "end_pressure_kpa": 319},
        ]
    )
    assert decision["action"] == "continue_makeup_observe"
    with pytest.raises(InterlockError):
        platform.isolate_leak_candidate()


def build_shared_topology_for_platform(platform) -> bool:
    zones = {
        name: ZoneHydraulics(name, 320.0, f"{name}.s", f"{name}.r", f"{name}.d")
        for name in ("trunk", "branch")
    }
    topology = LeakTopology(zones, {("secondary_header", "trunk"), ("trunk", "branch")})
    platform.leaks._topology = topology
    platform.leaks.isolation = LeakIsolationManager(topology)
    return True
