"""隔离后定压重算。

判定、隔离执行和审计使用同一个重算结果：先按仍在线分区的最低定压要求取包络，再由
调用方交给既有定压基线联锁校验。任何在线分区都不允许因为另一个区被隔离而低于其
要求，也不能借隔离之名整网抬高补水压力。
"""

from __future__ import annotations

from ..errors import InterlockError
from .topology import LeakTopology


def recalculate_setpoint(
    topology: LeakTopology,
    *,
    isolated: set[str] | None = None,
    restored: set[str] | None = None,
    margin_kpa: float = 0.0,
) -> dict:
    """按当前在线分区重算共用定压目标。"""

    if margin_kpa < 0:
        raise InterlockError("定压裕量不能为负", component="leak")
    isolated_set = set(isolated or ())
    restored_set = set(restored or ())
    active = [
        topology.get(name)
        for name in topology.names()
        if name not in isolated_set or name in restored_set
    ]
    if not active:
        raise InterlockError("至少保留一个在线分区才能重算定压", component="leak")

    governing = max(active, key=lambda zone: zone.required_setpoint_kpa)
    target = governing.required_setpoint_kpa + float(margin_kpa)
    return {
        "target_kpa": round(target, 2),
        "governing_zone": governing.name,
        "active_zones": sorted(zone.name for zone in active),
        "isolated_zones": sorted(name for name in isolated_set if name not in restored_set),
        "restored_zones": sorted(restored_set),
        "requirements": {
            zone.name: round(zone.required_setpoint_kpa, 2) for zone in sorted(active, key=lambda item: item.name)
        },
        "margin_kpa": round(float(margin_kpa), 2),
    }
