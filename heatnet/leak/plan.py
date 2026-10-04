"""分区定压计划。

隔离一个分区后，其余在役分区的定压要跟着重算：定压目标取当前定压基线，
补水配额按各在役分区的站数占比重新分摊；被隔离的分区定压目标置空、配额
归零。判断、定压与审计读的是同一份计划——重算只发生在隔离、恢复与基线
改造三个时点，其余时候控制台看到的就是审计里落盘的那份。
"""

from __future__ import annotations

from ..ns.zone import ZoneRegistry


def recompute_plan(
    baseline_kpa: float,
    zones: ZoneRegistry,
    isolated: frozenset[str] | set[str],
) -> dict:
    """按当前定压基线与在役分区重算各分区的定压目标与补水配额。"""

    cut = set(isolated)
    active = [name for name in zones.names() if name not in cut]
    total_stations = sum(len(zones.stations(name)) for name in active)
    entries: dict[str, dict] = {}
    for name in zones.names():
        stations = len(zones.stations(name))
        if name in cut:
            entries[name] = {
                "state": "isolated",
                "setpoint_kpa": None,
                "makeup_share": 0.0,
                "stations": stations,
            }
            continue
        share = stations / total_stations if total_stations > 0 else 0.0
        entries[name] = {
            "state": "active",
            "setpoint_kpa": round(float(baseline_kpa), 2),
            "makeup_share": round(share, 4),
            "stations": stations,
        }
    return {
        "baseline_kpa": round(float(baseline_kpa), 2),
        "active": active,
        "isolated": sorted(cut),
        "zones": entries,
    }
