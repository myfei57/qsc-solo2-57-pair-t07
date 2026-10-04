"""分区漏点嫌疑排序与隔离建议。

判据定死：主判据是**压降速度**（kPa/s，降序）——补水增量受分区容积影响，
只能作并列时的次判据（降序），再并列按分区名字典序。排序、控制台展示与
审计落盘读的是同一份判定，任何一处改判据都必须三处同步。

压降读不到的分区属于部分失败：按可疑处理，排在已量化嫌疑区之后、正常区
之前，绝不落入正常档；整份评估标记 ``partial`` 并带上
``pressure_reading_missing`` 违例。

嫌疑不明（头名与次名拉不开余量，或存在读不到的分区）时，是先隔离最像的
那个还是整网继续补水、同时加密观测，按管网拓扑定：隔离后在役分区数不得
低于下限，且被隔离区的站数占比不得越上限，否则只能继续观测。
"""

from __future__ import annotations

from ..config import LeakLimits
from ..ns.zone import ZoneRegistry
from .state import ZoneObservation, ZoneVerdict

CRITERION = "pressure_drop_rate"

ACTION_ISOLATE = "isolate"
ACTION_OBSERVE = "observe"


def _sort_key(verdict: ZoneVerdict) -> tuple:
    """定死的排序键：压降速度降序 → 补水增量降序 → 分区名字典序。"""

    rate = verdict.drop_rate_kpa_s if verdict.drop_rate_kpa_s is not None else 0.0
    return (-rate, -verdict.makeup_t, verdict.zone)


def _station_share(zones: ZoneRegistry, zone: str, active: list[str]) -> float:
    """某分区站数占当前在役分区总站数的比例。"""

    total = sum(len(zones.stations(name)) for name in active)
    if total <= 0:
        return 0.0
    return len(zones.stations(zone)) / total


def assess_leak_suspects(
    observations: list[ZoneObservation],
    limits: LeakLimits,
    zones: ZoneRegistry,
    window_s: float,
    isolated: frozenset[str] = frozenset(),
) -> dict:
    """按定死的判据排出嫌疑顺序，并按管网拓扑给出隔离/观测建议。"""

    if window_s <= 0:
        raise ValueError("观测窗口必须为正")
    seen: set[str] = set()
    for observation in observations:
        if observation.zone in seen:
            raise ValueError(f"观测分区重复: {observation.zone}")
        seen.add(observation.zone)

    active = [name for name in zones.names() if name not in isolated]
    skipped = [
        {"zone": item.zone, "reason": "isolated"}
        for item in observations
        if item.zone in isolated
    ]

    measured: list[ZoneVerdict] = []
    missing: list[ZoneVerdict] = []
    for item in observations:
        if item.zone in isolated:
            continue
        zones.get(item.zone)
        if item.drop_kpa is None:
            missing.append(
                ZoneVerdict(
                    rank=0,
                    zone=item.zone,
                    drop_rate_kpa_s=None,
                    makeup_t=item.makeup_t,
                    suspect=True,
                    reading_missing=True,
                )
            )
            continue
        rate = item.drop_kpa / window_s
        measured.append(
            ZoneVerdict(
                rank=0,
                zone=item.zone,
                drop_rate_kpa_s=rate,
                makeup_t=item.makeup_t,
                suspect=limits.suspect(rate),
                reading_missing=False,
            )
        )

    measured_suspects = sorted((v for v in measured if v.suspect), key=_sort_key)
    measured_normal = sorted((v for v in measured if not v.suspect), key=_sort_key)
    unranked = sorted(missing, key=lambda v: (-v.makeup_t, v.zone))
    ranking = measured_suspects + unranked + measured_normal
    verdicts = [
        ZoneVerdict(
            rank=index + 1,
            zone=item.zone,
            drop_rate_kpa_s=item.drop_rate_kpa_s,
            makeup_t=item.makeup_t,
            suspect=item.suspect,
            reading_missing=item.reading_missing,
        )
        for index, item in enumerate(ranking)
    ]

    suspects = [item.zone for item in verdicts if item.suspect]
    partial = bool(missing)
    violations = ["pressure_reading_missing"] if partial else []

    if not suspects:
        confidence = "none"
    elif partial:
        confidence = "unclear"
    elif len(measured_suspects) == 1:
        confidence = "clear"
    else:
        top = measured_suspects[0].drop_rate_kpa_s or 0.0
        runner_up = measured_suspects[1].drop_rate_kpa_s or 0.0
        confidence = "clear" if top >= runner_up * limits.clear_margin_ratio else "unclear"

    recommendation = _recommend(confidence, verdicts, limits, zones, active)

    return {
        "ok": not partial,
        "partial": partial,
        "criterion": CRITERION,
        "window_s": round(float(window_s), 3),
        "ranking": [item.as_dict() for item in verdicts],
        "suspects": suspects,
        "missing": [item.zone for item in unranked],
        "skipped": skipped,
        "confidence": confidence,
        "recommendation": recommendation,
        "topology": {
            "active_zones": len(active),
            "isolated": sorted(isolated),
            "min_remaining_zones": limits.min_remaining_zones,
            "max_isolate_station_share": limits.max_isolate_station_share,
        },
        "violations": violations,
    }


def _recommend(
    confidence: str,
    verdicts: list[ZoneVerdict],
    limits: LeakLimits,
    zones: ZoneRegistry,
    active: list[str],
) -> dict:
    """按嫌疑明确度与管网拓扑给出建议：隔离最像的那个，或整网续补加密观测。"""

    suspects = [item for item in verdicts if item.suspect]
    if not suspects:
        return {
            "action": ACTION_OBSERVE,
            "zone": None,
            "reason": "no_suspect",
            "detail": {"makeup": "as_needed", "sampling": "normal"},
        }

    top = suspects[0]
    share = _station_share(zones, top.zone, active)
    isolatable = (
        len(active) - 1 >= limits.min_remaining_zones
        and share <= limits.max_isolate_station_share
    )
    if confidence == "clear":
        if isolatable:
            return {
                "action": ACTION_ISOLATE,
                "zone": top.zone,
                "reason": "clear_suspect",
                "detail": {"station_share": round(share, 4)},
            }
        return {
            "action": ACTION_OBSERVE,
            "zone": top.zone,
            "reason": "topology_guard",
            "detail": {
                "makeup": "whole_network",
                "sampling": "dense",
                "guarded_zone": top.zone,
                "station_share": round(share, 4),
            },
        }
    if isolatable:
        return {
            "action": ACTION_ISOLATE,
            "zone": top.zone,
            "reason": "unclear_isolate_first",
            "detail": {"station_share": round(share, 4)},
        }
    return {
        "action": ACTION_OBSERVE,
        "zone": top.zone,
        "reason": "unclear_observe",
        "detail": {
            "makeup": "whole_network",
            "sampling": "dense",
            "guarded_zone": top.zone,
            "station_share": round(share, 4),
        },
    }
