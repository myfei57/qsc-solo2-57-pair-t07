"""漏点诊断应用服务。

它把分区水力拓扑、观测窗、嫌疑排序、隔离状态和定压重算串成同一份事实：控制台、
状态快照和审计读取的都是最近一次 :class:`~heatnet.leak.analysis.LeakDecision`。
分区拆分后默认把新分区作为独立叶区补进拓扑，避免新分区在漏点诊断里被默认正常。
"""

from __future__ import annotations

from ..errors import NamespaceError
from .analysis import LeakDecision, LeakObservation, analyse_leaks
from .isolation import LeakIsolationManager
from .topology import LeakTopology, ZoneHydraulics, build_default_leak_topology


class LeakMonitor:
    """保存漏点观测、诊断结论与隔离状态。"""

    def __init__(self, topology: LeakTopology, target_kpa: float) -> None:
        self._topology = topology
        self._default_target_kpa = float(target_kpa)
        self.isolation = LeakIsolationManager(topology)
        self._last_observations: list[LeakObservation] = []
        self._last_decision: LeakDecision | None = None
        self._analyses = 0

    @property
    def topology(self) -> LeakTopology:
        return self._topology

    def analyses(self) -> int:
        """返回诊断次数。"""

        return self._analyses

    def last_decision(self) -> LeakDecision | None:
        """返回最近一次诊断。"""

        return self._last_decision

    def last_observations(self) -> list[LeakObservation]:
        """返回最近一次观测输入。"""

        return list(self._last_observations)

    def sync_zone(self, zone: str, target_kpa: float | None = None) -> ZoneHydraulics:
        """新增独立叶区；已存在时保持其拓扑和边界不变。"""

        try:
            return self._topology.get(zone)
        except NamespaceError:
            pass
        target = self._default_target_kpa if target_kpa is None else float(target_kpa)
        hydraulics = ZoneHydraulics(
            name=zone,
            required_setpoint_kpa=target,
            supply_valve=f"{zone}.supply-isolation",
            return_valve=f"{zone}.return-isolation",
            drain_valve=f"{zone}.drain",
            pump=f"{zone}.branch-pump",
        )
        self._topology.add_zone(hydraulics)
        return hydraulics

    def analyse(self, observations: list[LeakObservation], *, force: bool = False) -> LeakDecision:
        """执行一次诊断并保存同一份结果。"""

        decision = analyse_leaks(
            self._topology,
            observations,
            isolated=set(self.isolation.isolated_zones()),
            force=force,
        )
        self._last_observations = list(observations)
        self._last_decision = decision
        self._analyses += 1
        return decision

    def report(self) -> dict:
        """输出控制台状态。"""

        payload = {
            "metric": "pressure_decay_kpa_min",
            "analyses": self._analyses,
            "last_observations": [item.as_dict() for item in self._last_observations],
            "last_decision": None if self._last_decision is None else self._last_decision.as_dict(),
            **self.isolation.describe(),
        }
        return payload
