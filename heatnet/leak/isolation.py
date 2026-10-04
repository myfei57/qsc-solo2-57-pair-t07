"""漏点隔离与恢复时序。

隔离固定按“先确认在线分区与目标定压 → 停分支泵 → 关供水隔离阀 → 关回水隔离阀 →
阀位确认 → 必要时开放泄水/排气 → 重算并重设定压”的顺序；恢复必须严格逆序：先
恢复定压到在线包络 → 关泄水/排气 → 开水 → 供回均到位后启分支泵。顺序颠倒可能把
泄漏段带压打开或抽空仍在线分区。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..errors import InterlockError
from .analysis import LeakDecision
from .pressure import recalculate_setpoint
from .topology import LeakTopology


@dataclass(frozen=True)
class IsolationRecord:
    """一次分区隔离/恢复操作记录。"""

    zone: str
    operation: str
    ok: bool
    target_kpa: float
    steps: list[dict] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "zone": self.zone,
            "operation": self.operation,
            "ok": self.ok,
            "target_kpa": round(self.target_kpa, 2),
            "steps": list(self.steps),
            "reason": self.reason,
        }


class LeakIsolationManager:
    """维护隔离状态，并产出不可乱序的操作步骤。"""

    def __init__(self, topology: LeakTopology) -> None:
        self._topology = topology
        self._isolated: set[str] = set()
        self._records: list[IsolationRecord] = []

    @property
    def topology(self) -> LeakTopology:
        return self._topology

    def isolated_zones(self) -> list[str]:
        """返回已隔离分区。"""

        return sorted(self._isolated)

    def is_isolated(self, zone: str) -> bool:
        """分区是否已隔离。"""

        return zone in self._isolated

    def records(self) -> list[dict]:
        """返回隔离操作记录。"""

        return [item.as_dict() for item in self._records]

    def _step(self, component: str, action: str, device: str, **detail: object) -> dict:
        return {"component": component, "action": action, "detail": {"device": device, **detail}}

    def isolation_steps(self, zone: str, target_kpa: float, reason: str) -> list[dict]:
        """生成隔离时序（不含定压应用，平台用同一目标调用压力基线）。"""

        hydraulics = self._topology.get(zone)
        steps: list[dict] = []
        if hydraulics.pump:
            steps.append(self._step("pump", "stop", hydraulics.pump, confirmed=True))
        steps.extend(
            [
                self._step("valve", "close", hydraulics.supply_valve, confirmed=True),
                self._step("valve", "close", hydraulics.return_valve, confirmed=True),
                self._step(
                    "secondary",
                    "confirm_boundary_closed",
                    f"{zone}.boundary",
                    supply_valve=hydraulics.supply_valve,
                    return_valve=hydraulics.return_valve,
                ),
                self._step("valve", "open", hydraulics.drain_valve, confirmed=True, leak_drain=True),
                {
                    "component": "pressure",
                    "action": "recalculate_after_isolation",
                    "detail": {"zone": zone, "target_kpa": round(target_kpa, 2), "reason": reason},
                },
            ]
        )
        return steps

    def restoration_steps(self, zone: str, target_kpa: float) -> list[dict]:
        """生成恢复时序，必须与隔离时序逆序且带定压前置确认。"""

        hydraulics = self._topology.get(zone)
        steps: list[dict] = [
            {
                "component": "pressure",
                "action": "recalculate_before_restoration",
                "detail": {"zone": zone, "target_kpa": round(target_kpa, 2)},
            },
            self._step("valve", "close", hydraulics.drain_valve, confirmed=True),
            self._step("valve", "open", hydraulics.return_valve, confirmed=True),
            self._step("valve", "open", hydraulics.supply_valve, confirmed=True),
            self._step(
                "secondary",
                "confirm_boundary_open",
                f"{zone}.boundary",
                supply_valve=hydraulics.supply_valve,
                return_valve=hydraulics.return_valve,
            ),
        ]
        if hydraulics.pump:
            steps.append(self._step("pump", "start", hydraulics.pump, confirmed=True))
        return steps

    def isolate(
        self,
        decision: LeakDecision,
        *,
        apply_setpoint: Callable[[float], float] | None = None,
        validate_setpoint: Callable[[float], bool] | None = None,
        force: bool = False,
        margin_kpa: float = 0.0,
        zone: str | None = None,
    ) -> dict:
        """按诊断结论执行隔离，并在边界关闭后重算定压。"""

        target_zone = zone or decision.candidate_zone
        if target_zone is None:
            raise InterlockError("没有可隔离的嫌疑分区", component="leak")
        self._topology.get(target_zone)
        if target_zone in self._isolated:
            raise InterlockError(f"分区已隔离: {target_zone}", component="leak")
        allowed = {decision.candidate_zone}
        if target_zone not in allowed:
            raise InterlockError("手动隔离分区与最新诊断候选不一致", component="leak")
        if not force and decision.action not in ("isolate_candidate", "diagnostic_isolation"):
            raise InterlockError(f"当前诊断不允许隔离: {decision.recommendation}", component="leak")
        ok, reason = self._topology.is_isolatable(target_zone, active=self._active_names(), force=force)
        if not ok:
            raise InterlockError(reason, component="leak")

        plan = recalculate_setpoint(
            self._topology, isolated={*self._isolated, target_zone}, margin_kpa=margin_kpa
        )
        target = float(plan["target_kpa"])
        if validate_setpoint is not None and not validate_setpoint(target):
            raise InterlockError(f"隔离后定压 {target:.1f} kPa 未通过基线联锁", component="leak")
        steps = self.isolation_steps(target_zone, target, reason)
        applied_target = target
        setpoint_error = ""
        for step in steps:
            if step["component"] != "pressure" or apply_setpoint is None:
                continue
            try:
                applied_target = float(apply_setpoint(target))
            except InterlockError as error:
                setpoint_error = error.message
        # 阀位步骤均已按序产出，定压步骤位于最后；模拟边界关闭后提交隔离状态。
        self._isolated.add(target_zone)
        record = IsolationRecord(
            zone=target_zone,
            operation="isolate",
            ok=not setpoint_error,
            target_kpa=applied_target,
            steps=steps,
            reason=decision.recommendation,
        )
        self._records.append(record)
        payload = record.as_dict()
        payload["setpoint_plan"] = plan
        payload["isolated_zones"] = self.isolated_zones()
        if setpoint_error:
            payload["violations"] = [setpoint_error]
        return payload

    def restore(
        self,
        zone: str,
        *,
        apply_setpoint: Callable[[float], float] | None = None,
        validate_setpoint: Callable[[float], bool] | None = None,
        margin_kpa: float = 0.0,
    ) -> dict:
        """按严格逆序恢复一个已隔离分区。"""

        self._topology.get(zone)
        if zone not in self._isolated:
            raise InterlockError(f"分区未隔离: {zone}", component="leak")
        # 若还有其下游处于隔离，先恢复上游会改变下游开口段状态。
        blocked = [name for name in self._topology.downstream(zone) if name in self._isolated]
        if blocked:
            raise InterlockError(f"必须先恢复下游分区: {', '.join(blocked)}", component="leak")

        plan = recalculate_setpoint(
            self._topology, isolated=set(self._isolated), restored={zone}, margin_kpa=margin_kpa
        )
        target = float(plan["target_kpa"])
        if validate_setpoint is not None and not validate_setpoint(target):
            raise InterlockError(f"恢复前定压 {target:.1f} kPa 未通过基线联锁", component="leak")
        steps = self.restoration_steps(zone, target)
        applied_target = target
        violation = ""
        if apply_setpoint is not None:
            try:
                applied_target = float(apply_setpoint(target))
            except InterlockError as error:
                violation = error.message
        if not violation:
            self._isolated.remove(zone)
        record = IsolationRecord(
            zone=zone,
            operation="restore",
            ok=not violation,
            target_kpa=applied_target,
            steps=steps,
            reason="漏点处置完成后按逆序恢复",
        )
        self._records.append(record)
        payload = record.as_dict()
        payload["setpoint_plan"] = plan
        payload["isolated_zones"] = self.isolated_zones()
        if violation:
            payload["violations"] = [violation]
        return payload

    def _active_names(self) -> set[str]:
        return {name for name in self._topology.names() if name not in self._isolated}

    def describe(self) -> dict:
        """汇总隔离状态。"""

        return {
            "isolated_zones": self.isolated_zones(),
            "active_zones": sorted(self._active_names()),
            "records": self.records(),
            "topology": self._topology.describe(active=self._active_names(), isolated=self._isolated),
        }
