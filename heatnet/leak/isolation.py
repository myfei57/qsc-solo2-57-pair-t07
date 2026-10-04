"""分区隔离与恢复的时序联锁。

时序写死，不能乱：先评估、再隔离、隔离完成后立刻重算其余区定压；恢复则
先补水充压、压力进目标带才允许并网，并网后再重算全网定压。跳过评估直接
隔离、重复隔离同一分区、恢复一个没在隔离的分区、把在役分区隔离到低于
下限，都属于时序违例，直接抛出 :class:`InterlockError` 拒绝。
"""

from __future__ import annotations

from ..config import LeakLimits
from ..errors import InterlockError


class IsolationTracker:
    """记录分区隔离状态并守住隔离/恢复的先后顺序。"""

    def __init__(self, limits: LeakLimits) -> None:
        self._limits = limits
        self._isolated: dict[str, dict] = {}
        self._isolations = 0
        self._recoveries = 0

    def isolated(self) -> list[str]:
        """返回当前处于隔离状态的分区（排序后）。"""

        return sorted(self._isolated)

    def is_isolated(self, zone: str) -> bool:
        """某分区当前是否处于隔离状态。"""

        return zone in self._isolated

    def isolations(self) -> int:
        """返回累计隔离次数。"""

        return self._isolations

    def recoveries(self) -> int:
        """返回累计恢复次数。"""

        return self._recoveries

    def isolate(self, zone: str, active_count: int) -> dict:
        """把一个在役分区切入隔离，返回隔离记录。"""

        if zone in self._isolated:
            raise InterlockError(f"分区已隔离: {zone}", component="leak")
        if active_count - 1 < self._limits.min_remaining_zones:
            raise InterlockError(
                f"隔离 {zone} 后在役分区数低于下限 {self._limits.min_remaining_zones}",
                component="leak",
            )
        self._isolations += 1
        record = {"zone": zone, "seq": self._isolations}
        self._isolated[zone] = record
        return record

    def recover(self, zone: str) -> dict:
        """把一个已隔离分区切回在役，返回其隔离记录。"""

        try:
            record = self._isolated.pop(zone)
        except KeyError as error:
            raise InterlockError(f"分区未隔离，不能恢复: {zone}", component="leak") from error
        self._recoveries += 1
        return record
