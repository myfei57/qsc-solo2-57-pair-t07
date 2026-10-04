"""二次网漏点嫌疑定位。

定位口径固定为**关补水观测窗内的压降速度**：
``pressure_decay_kpa_min = (起点压力 - 终点压力) / 观测分钟数``。补水量只做佐证和
现场安全处置依据，绝不参与排序，避免"哪个区被多补、哪个区显得漏"的循环误判。

压降缺失是部分失败：该分区不能按正常处理，也不能给零分，统一按可疑区进入排序并
要求补齐仪表。是否直接隔离由嫌疑等级和水力拓扑共同决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import InterlockError
from .topology import LeakTopology

LEAK_LOCATOR_METRIC = "pressure_decay_kpa_min"
NORMAL_WINDOW_S = 300
DENSE_WINDOW_S = 60

# 关补水观测窗内的固定分级门限（kPa/min）。
DECAY_TENTATIVE_KPA_MIN = 0.5
DECAY_SUSPECT_KPA_MIN = 2.0
DECAY_HIGH_KPA_MIN = 5.0
MAKEUP_CORROBORATION_T_H = 0.5

LEVELS = ("normal", "tentative", "suspect", "high")
LEVEL_LABELS = {
    "normal": "正常",
    "tentative": "待观察",
    "suspect": "可疑",
    "high": "高度可疑",
}


@dataclass(frozen=True)
class LeakObservation:
    """一个分区在一次关补水观测窗内的结果。"""

    zone: str
    elapsed_s: float
    start_pressure_kpa: float | None = None
    end_pressure_kpa: float | None = None
    makeup_t: float = 0.0
    pressure_available: bool = True
    makeup_available: bool = True

    @classmethod
    def from_mapping(cls, zone: str, raw: Mapping[str, Any]) -> "LeakObservation":
        """从接口字典构造观测；缺少压降字段即标成部分失败。"""

        try:
            elapsed = float(raw["elapsed_s"])
        except (KeyError, TypeError, ValueError) as error:
            raise InterlockError(f"分区 {zone} 的 elapsed_s 必须是正数", component="leak") from error
        if elapsed <= 0:
            raise InterlockError(f"分区 {zone} 的 elapsed_s 必须是正数", component="leak")

        available = raw.get("pressure_available", True)
        start = raw.get("start_pressure_kpa")
        end = raw.get("end_pressure_kpa")
        pressure_available = available is not False and start is not None and end is not None
        try:
            start_value = None if start is None else float(start)
            end_value = None if end is None else float(end)
        except (TypeError, ValueError) as error:
            raise InterlockError(f"分区 {zone} 的压力读数必须是数值", component="leak") from error

        makeup_raw = raw.get("makeup_t", 0.0)
        makeup_available = raw.get("makeup_available", True) is not False and makeup_raw is not None
        try:
            makeup = 0.0 if makeup_raw is None else float(makeup_raw)
        except (TypeError, ValueError) as error:
            raise InterlockError(f"分区 {zone} 的 makeup_t 必须是数值", component="leak") from error
        if makeup < 0:
            raise InterlockError(f"分区 {zone} 的 makeup_t 不能为负", component="leak")
        return cls(
            zone=str(zone),
            elapsed_s=elapsed,
            start_pressure_kpa=start_value,
            end_pressure_kpa=end_value,
            makeup_t=makeup,
            pressure_available=bool(pressure_available),
            makeup_available=bool(makeup_available),
        )

    def as_dict(self) -> dict:
        return {
            "zone": self.zone,
            "elapsed_s": self.elapsed_s,
            "start_pressure_kpa": self.start_pressure_kpa,
            "end_pressure_kpa": self.end_pressure_kpa,
            "pressure_decay_kpa_min": self.pressure_decay_kpa_min(),
            "makeup_t": round(self.makeup_t, 4),
            "pressure_available": self.pressure_available,
            "makeup_available": self.makeup_available,
        }

    def pressure_decay_kpa_min(self) -> float | None:
        """关补水窗内压降速度；读不到时返回 ``None``，压力不下降时为 0。"""

        if not self.pressure_available:
            return None
        if self.start_pressure_kpa is None or self.end_pressure_kpa is None:
            return None
        decay = (self.start_pressure_kpa - self.end_pressure_kpa) / (self.elapsed_s / 60.0)
        return max(0.0, decay)


@dataclass(frozen=True)
class SuspectRank:
    """嫌疑排序中的一个分区。"""

    rank: int
    zone: str
    level: str
    score_group: int
    pressure_decay_kpa_min: float | None
    makeup_t: float | None
    makeup_corroborates: bool
    pressure_available: bool
    makeup_available: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "zone": self.zone,
            "level": self.level,
            "level_label": LEVEL_LABELS[self.level],
            "score_group": self.score_group,
            "pressure_decay_kpa_min": (
                None
                if self.pressure_decay_kpa_min is None
                else round(self.pressure_decay_kpa_min, 4)
            ),
            "makeup_t": None if self.makeup_t is None else round(self.makeup_t, 4),
            "makeup_corroborates": self.makeup_corroborates,
            "pressure_available": self.pressure_available,
            "makeup_available": self.makeup_available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LeakDecision:
    """一次漏点诊断结论及拓扑联锁后的处置建议。"""

    metric: str
    observations: list[LeakObservation]
    suspects: list[SuspectRank]
    recommendation: str
    action: str
    candidate_zone: str | None
    isolatable: bool
    isolation_reason: str
    observe_window_s: int
    makeup_policy: str
    missing_pressure_zones: list[str]
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "recommendation": self.recommendation,
            "action": self.action,
            "candidate_zone": self.candidate_zone,
            "isolatable": self.isolatable,
            "isolation_reason": self.isolation_reason,
            "observe_window_s": self.observe_window_s,
            "makeup_policy": self.makeup_policy,
            "missing_pressure_zones": list(self.missing_pressure_zones),
            "warnings": list(self.warnings),
            "suspects": [item.as_dict() for item in self.suspects],
            "observations": [item.as_dict() for item in self.observations],
        }


def level_for_decay(decay: float | None) -> str:
    """按压降速度映射固定嫌疑等级。"""

    if decay is None:
        return "suspect"
    if decay >= DECAY_HIGH_KPA_MIN:
        return "high"
    if decay >= DECAY_SUSPECT_KPA_MIN:
        return "suspect"
    if decay >= DECAY_TENTATIVE_KPA_MIN:
        return "tentative"
    return "normal"


def rank_leak_suspects(observations: list[LeakObservation]) -> list[SuspectRank]:
    """只按压降速度排序；补水量仅生成佐证标记。"""

    rows: list[SuspectRank] = []
    for observation in observations:
        decay = observation.pressure_decay_kpa_min()
        level = level_for_decay(decay)
        makeup = observation.makeup_t if observation.makeup_available else None
        corroborates = makeup is not None and makeup >= MAKEUP_CORROBORATION_T_H
        if not observation.pressure_available:
            score_group = 1
            reason = "压降读数缺失，按部分失败列为可疑"
        elif decay is None or decay <= 0:
            score_group = 0
            reason = "压力未下降"
        else:
            score_group = 2
            if level == "high":
                reason = "压降速度达到高度可疑门限"
            elif level == "suspect":
                reason = "压降速度达到可疑门限"
            else:
                reason = "压降速度超过待观察门限" if level == "tentative" else "压降速度低于待观察门限"
            if corroborates:
                reason += "，补水量同向佐证"
        rows.append(
            SuspectRank(
                rank=0,
                zone=observation.zone,
                level=level,
                score_group=score_group,
                pressure_decay_kpa_min=decay,
                makeup_t=makeup,
                makeup_corroborates=corroborates,
                pressure_available=observation.pressure_available,
                makeup_available=observation.makeup_available,
                reason=reason,
            )
        )

    rows.sort(
        key=lambda item: (
            0 if (item.pressure_decay_kpa_min or 0.0) > 0 else (1 if item.pressure_decay_kpa_min is None else 2),
            -(item.pressure_decay_kpa_min or 0.0),
            item.zone,
        )
    )
    return [
        SuspectRank(
            rank=index,
            zone=row.zone,
            level=row.level,
            score_group=row.score_group,
            pressure_decay_kpa_min=row.pressure_decay_kpa_min,
            makeup_t=row.makeup_t,
            makeup_corroborates=row.makeup_corroborates,
            pressure_available=row.pressure_available,
            makeup_available=row.makeup_available,
            reason=row.reason,
        )
        for index, row in enumerate(rows, start=1)
    ]


def analyse_leaks(
    topology: LeakTopology,
    observations: list[LeakObservation],
    *,
    isolated: set[str] | None = None,
    force: bool = False,
) -> LeakDecision:
    """按固定压降速度口径和拓扑生成漏点处置决策。"""

    isolated_set = set(isolated or ())
    active = [item for item in observations if item.zone not in isolated_set]
    known = set(topology.names())
    observed_names: set[str] = set()
    for observation in active:
        if observation.zone not in known:
            raise InterlockError(f"观测分区不在拓扑中: {observation.zone}", component="leak")
        if observation.zone in observed_names:
            raise InterlockError(f"同一分区不能重复观测: {observation.zone}", component="leak")
        observed_names.add(observation.zone)

    # 未上报观测的活动区同样属于仪表部分失败，不能默认正常。
    for name in topology.names():
        if name not in isolated_set and name not in observed_names:
            active.append(
                LeakObservation(
                    zone=name,
                    elapsed_s=DENSE_WINDOW_S,
                    pressure_available=False,
                    makeup_available=False,
                )
            )

    suspects = rank_leak_suspects(active)
    missing = [item.zone for item in suspects if not item.pressure_available]
    warnings: list[str] = []
    candidate = suspects[0] if suspects else None

    if candidate is None:
        return LeakDecision(
            metric=LEAK_LOCATOR_METRIC,
            observations=list(active),
            suspects=[],
            recommendation="没有活动分区，无需补水或隔离",
            action="hold",
            candidate_zone=None,
            isolatable=False,
            isolation_reason="无活动分区",
            observe_window_s=DENSE_WINDOW_S,
            makeup_policy="停止非必要补水",
            missing_pressure_zones=[],
        )

    topology.get(candidate.zone)
    if candidate.level in ("high", "suspect"):
        base = "隔离高度可疑分区" if candidate.level == "high" else "隔离可疑分区"
        action = "isolate_candidate"
        window = DENSE_WINDOW_S
    else:
        base = "维持整网安全补水并加密观测"
        action = "continue_makeup_observe"
        window = NORMAL_WINDOW_S if candidate.level == "normal" else DENSE_WINDOW_S

    isolatable, isolation_reason = topology.is_isolatable(
        candidate.zone,
        active={name for name in topology.names() if name not in isolated_set},
        force=force,
    )
    if not isolatable and not force:
        action = "continue_makeup_observe"
        base = (
            f"{candidate.zone} 嫌疑最高但不能独立隔离；"
            "维持安全补水、按枝状拓扑加密观测并先排查其下游/共用段"
        )
        if candidate.pressure_available is False:
            base = f"{candidate.zone} 压降缺失且不能独立隔离；维持安全补水并立即修复仪表"
    elif candidate.pressure_available is False:
        base = f"对压降缺失的 {candidate.zone} 做诊断性隔离以验证漏点，同时修复仪表"
        action = "diagnostic_isolation" if topology.is_leaf(candidate.zone) else "continue_makeup_observe"
        if action == "continue_makeup_observe":
            base = f"{candidate.zone} 压降缺失且位于共用段；不能当正常，先加密观测并修复仪表"
            isolatable = False
            isolation_reason = "缺读数且非独立叶区，禁止直接隔离"

    if missing:
        warnings.append("partial_failure:pressure_missing")
    recommendation = base
    if candidate.makeup_corroborates:
        recommendation += "；补水量仅作同向佐证，不改变排序"

    return LeakDecision(
        metric=LEAK_LOCATOR_METRIC,
        observations=list(active),
        suspects=suspects,
        recommendation=recommendation,
        action=action,
        candidate_zone=candidate.zone,
        isolatable=isolatable if action.startswith("isolate") or action == "diagnostic_isolation" else False,
        isolation_reason=isolation_reason,
        observe_window_s=window,
        makeup_policy="只维持定压带内安全补水，禁止整网盲目加大补水",
        missing_pressure_zones=missing,
        warnings=warnings,
    )
