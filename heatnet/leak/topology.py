"""二次网分区水力拓扑。

漏点处置不仅看压降数值，还必须知道某个分区是否位于共用干线上。关闭一个独立叶区
通常不影响其他区；关闭向其他区转供的上游区会连带切断下游，因此只能加密观测而不
能直接隔离。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import InterlockError, NamespaceError

ROOT = "secondary_header"


@dataclass(frozen=True)
class ZoneHydraulics:
    """分区隔离边界与最低定压要求。"""

    name: str
    required_setpoint_kpa: float
    supply_valve: str
    return_valve: str
    drain_valve: str
    critical: bool = False
    pump: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "required_setpoint_kpa": round(self.required_setpoint_kpa, 2),
            "supply_valve": self.supply_valve,
            "return_valve": self.return_valve,
            "drain_valve": self.drain_valve,
            "critical": self.critical,
            "pump": self.pump,
        }


class LeakTopology:
    """以分区为节点、供水路径为有向边的水力拓扑。"""

    def __init__(self, zones: dict[str, ZoneHydraulics], edges: set[tuple[str, str]]) -> None:
        self._zones = dict(zones)
        self._edges = set(edges)
        self._validate()

    def _validate(self) -> None:
        nodes = {ROOT, *self._zones}
        for upstream, downstream in self._edges:
            if upstream not in nodes:
                raise NamespaceError(f"拓扑节点不存在: {upstream}", component="leak")
            if downstream not in nodes:
                raise NamespaceError(f"拓扑节点不存在: {downstream}", component="leak")
            if upstream == downstream:
                raise InterlockError("分区不能连接自身", component="leak")
        for zone in self._zones:
            if zone == ROOT:
                raise NamespaceError(f"分区名不能保留给总回水节点: {ROOT}", component="leak")
            if not self._reaches(ROOT, zone):
                raise InterlockError(f"分区 {zone} 未连接到二次网总管", component="leak")

    def _reaches(self, source: str, target: str, *, strict: bool = False) -> bool:
        seen: set[str] = set()
        frontier = [(source, 0)]
        while frontier:
            current, distance = frontier.pop(0)
            if current in seen:
                continue
            if current == target and (not strict or distance > 0):
                return True
            seen.add(current)
            frontier.extend(
                (downstream, distance + 1)
                for upstream, downstream in self._edges
                if upstream == current and downstream not in seen
            )
        return False

    def names(self) -> list[str]:
        """返回全部分区名。"""

        return sorted(self._zones)

    def get(self, zone: str) -> ZoneHydraulics:
        """读取分区水力信息。"""

        try:
            return self._zones[zone]
        except KeyError as error:
            raise NamespaceError(f"未登记漏水分区: {zone}", component="leak") from error

    def add_zone(self, hydraulics: ZoneHydraulics, upstream: str = ROOT) -> ZoneHydraulics:
        """把一个新区作为独立叶区挂到指定上游。"""

        if hydraulics.name in self._zones:
            raise NamespaceError(f"漏水分区已存在: {hydraulics.name}", component="leak")
        if upstream not in {ROOT, *self._zones}:
            raise NamespaceError(f"拓扑节点不存在: {upstream}", component="leak")
        self._zones[hydraulics.name] = hydraulics
        self._edges.add((upstream, hydraulics.name))
        self._validate()
        return hydraulics

    def has_edge(self, upstream: str, downstream: str) -> bool:
        """是否存在指定供水连接。"""

        return (upstream, downstream) in self._edges

    def upstream(self, zone: str) -> list[str]:
        """返回从总管到该分区路径上的上游分区（不含该分区）。"""

        self.get(zone)
        result: list[str] = []
        for candidate in self._zones:
            if candidate != zone and self._reaches(candidate, zone, strict=True):
                result.append(candidate)
        return sorted(result)

    def downstream(self, zone: str) -> list[str]:
        """返回该分区转供的下游分区。"""

        self.get(zone)
        return sorted(
            candidate
            for candidate in self._zones
            if candidate != zone and self._reaches(zone, candidate, strict=True)
        )

    def is_leaf(self, zone: str) -> bool:
        """该分区是否没有向其他分区转供。"""

        return not self.downstream(zone)

    def is_isolatable(
        self, zone: str, active: set[str] | None = None, *, force: bool = False
    ) -> tuple[bool, str]:
        """按当前活动分区判断能否独立隔离。"""

        self.get(zone)
        active_set = set(active if active is not None else self._zones)
        affected = [name for name in self.downstream(zone) if name in active_set]
        if affected and not force:
            return False, f"分区 {zone} 仍向 {', '.join(affected)} 转供"
        if self.get(zone).critical and not force:
            return False, f"分区 {zone} 是保供关键区"
        return True, "可独立隔离"

    def describe(self, active: set[str] | None = None, isolated: set[str] | None = None) -> dict:
        """输出控制台与审计共用的拓扑口径。"""

        active_set = set(active if active is not None else self._zones)
        isolated_set = set(isolated or ())
        return {
            "root": ROOT,
            "edges": [list(edge) for edge in sorted(self._edges)],
            "zones": [
                {
                    **self._zones[name].as_dict(),
                    "active": name in active_set,
                    "isolated": name in isolated_set,
                    "leaf": self.is_leaf(name),
                    "downstream": self.downstream(name),
                }
                for name in self.names()
            ],
        }


def build_default_leak_topology(zone_names: list[str], target_kpa: float = 320.0) -> LeakTopology:
    """按现有环网/枝状网假设构造默认拓扑：各分区独立挂在总管下。"""

    zones = {
        name: ZoneHydraulics(
            name=name,
            required_setpoint_kpa=float(target_kpa),
            supply_valve=f"{name}.supply-isolation",
            return_valve=f"{name}.return-isolation",
            drain_valve=f"{name}.drain",
            pump=f"{name}.branch-pump",
        )
        for name in zone_names
    }
    edges = {(ROOT, name) for name in zone_names}
    return LeakTopology(zones, edges)
