"""漏点定位服务：评估、隔离、恢复与定压重算的编排。

服务持有三份状态——最近一次评估、隔离台账、当前定压计划。控制台展示与
审计落盘读的都是这三份，评估一旦完成就先存后报，隔离/恢复在重算定压
之后才返回，保证判断、定压与审计任何时候都一致。
"""

from __future__ import annotations

from ..clock import Clock
from ..config import LeakLimits
from ..errors import InterlockError
from ..ns.zone import ZoneRegistry
from ..pressure.baseline import PressureBaseline
from ..secondary.loop import SecondaryLoop
from ..secondary.makeup import makeup_then_pressurise
from .isolation import IsolationTracker
from .plan import recompute_plan
from .rank import CRITERION, assess_leak_suspects
from .state import ZoneObservation


class LeakService:
    """分区漏点定位：嫌疑评估、隔离/恢复时序与定压重算。"""

    def __init__(
        self,
        limits: LeakLimits,
        zones: ZoneRegistry,
        baseline: PressureBaseline,
        loop: SecondaryLoop,
        clock: Clock,
    ) -> None:
        self._limits = limits
        self._zones = zones
        self._baseline = baseline
        self._loop = loop
        self._clock = clock
        self._tracker = IsolationTracker(limits)
        self._last_assessment: dict | None = None
        self._plan = recompute_plan(self._baseline.value_kpa(), self._zones, frozenset())

    def assess(self, observations: list[dict], window_s: float) -> dict:
        """按各分区压降速度与补水增量评估漏点嫌疑，返回排名与隔离建议。"""

        parsed = [ZoneObservation.from_mapping(item) for item in observations]
        assessment = assess_leak_suspects(
            parsed,
            self._limits,
            self._zones,
            float(window_s),
            frozenset(self._tracker.isolated()),
        )
        self._last_assessment = assessment

        steps: list[dict] = [
            {
                "component": "leak",
                "action": "assess",
                "detail": {
                    "criterion": assessment["criterion"],
                    "window_s": assessment["window_s"],
                    "zones": len(parsed),
                    "partial": assessment["partial"],
                },
            }
        ]
        for verdict in assessment["ranking"]:
            if verdict["suspect"]:
                steps.append({"component": "leak", "action": "suspect", "detail": verdict})
        recommendation = assessment["recommendation"]
        steps.append(
            {
                "component": "leak",
                "action": "recommend",
                "detail": {
                    "decision": recommendation["action"],
                    "zone": recommendation["zone"],
                    "reason": recommendation["reason"],
                },
            }
        )
        return {
            "ok": assessment["ok"],
            "partial": assessment["partial"],
            "violations": list(assessment["violations"]),
            "steps": steps,
            "assessment": assessment,
            "suspects": list(assessment["suspects"]),
            "confidence": assessment["confidence"],
            "recommendation": recommendation,
        }

    def isolate(self, zone: str) -> dict:
        """隔离一个嫌疑分区，并重算其余在役分区的定压。"""

        if self._last_assessment is None:
            raise InterlockError("先完成漏点评估再隔离分区", component="leak")
        self._zones.get(zone)
        active = [name for name in self._zones.names() if not self._tracker.is_isolated(name)]
        record = self._tracker.isolate(zone, len(active))
        plan = self._replan()
        return {
            "ok": True,
            "violations": [],
            "asset": zone,
            "steps": [
                {
                    "component": "leak",
                    "action": "isolate",
                    "detail": {"zone": zone, "seq": record["seq"]},
                },
                {
                    "component": "pressure",
                    "action": "replan",
                    "detail": {
                        "baseline_kpa": plan["baseline_kpa"],
                        "active": len(plan["active"]),
                        "isolated": len(plan["isolated"]),
                    },
                },
            ],
            "zone": zone,
            "isolated": self._tracker.isolated(),
            "plan": plan,
        }

    def recover(self, zone: str) -> dict:
        """恢复一个已隔离分区：先补水充压、再并网、最后重算定压。"""

        self._zones.get(zone)
        if not self._tracker.is_isolated(zone):
            raise InterlockError(f"分区未隔离，不能恢复: {zone}", component="leak")

        steps: list[dict] = []
        makeup = makeup_then_pressurise(self._loop)
        steps.append(
            {
                "component": "secondary",
                "action": "makeup",
                "detail": {"zone": zone, "pressure_kpa": makeup["pressure_kpa"]},
            }
        )
        if not makeup["ok"]:
            return {
                "ok": False,
                "violations": list(makeup["violations"]),
                "asset": zone,
                "steps": steps,
                "zone": zone,
                "rejoined": False,
                "isolated": self._tracker.isolated(),
                "plan": self._plan,
            }

        record = self._tracker.recover(zone)
        steps.append(
            {
                "component": "leak",
                "action": "rejoin",
                "detail": {"zone": zone, "seq": record["seq"]},
            }
        )
        plan = self._replan()
        steps.append(
            {
                "component": "pressure",
                "action": "replan",
                "detail": {
                    "baseline_kpa": plan["baseline_kpa"],
                    "active": len(plan["active"]),
                    "isolated": len(plan["isolated"]),
                },
            }
        )
        return {
            "ok": True,
            "violations": [],
            "asset": zone,
            "steps": steps,
            "zone": zone,
            "rejoined": True,
            "isolated": self._tracker.isolated(),
            "plan": plan,
        }

    def refresh_plan(self) -> dict:
        """按当前定压基线重算定压计划（基线改造后调用）。"""

        return self._replan()

    def report(self) -> dict:
        """漏点定位当前状态，供控制台展示与审计核对。"""

        return {
            "criterion": CRITERION,
            "assessed": self._last_assessment is not None,
            "suspects": (
                list(self._last_assessment["suspects"]) if self._last_assessment else []
            ),
            "assessment": self._last_assessment,
            "isolated": self._tracker.isolated(),
            "isolations": self._tracker.isolations(),
            "recoveries": self._tracker.recoveries(),
            "plan": self._plan,
        }

    def _replan(self) -> dict:
        """重算并留存定压计划，控制台与审计读同一份。"""

        self._plan = recompute_plan(
            self._baseline.value_kpa(), self._zones, frozenset(self._tracker.isolated())
        )
        return self._plan
