"""漏点定位的观测与判定数据结构。

观测来自各分区在一个公共窗口内的压降与补水增量：``drop_kpa`` 为 ``None``
表示这一区的压降读不到——属于部分失败，判定时必须按可疑处理，不能当正常。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ZoneObservation:
    """一个分区在一个观测窗口内的压降与补水增量。"""

    zone: str
    drop_kpa: float | None
    makeup_t: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ZoneObservation":
        """从控制台/调度上送的映射解析一条观测，取值非法即拒绝。"""

        if not isinstance(raw, Mapping):
            raise ValueError("观测必须是映射")
        zone = raw.get("zone")
        if not isinstance(zone, str) or not zone:
            raise ValueError("观测缺少分区名")
        drop = raw.get("drop_kpa")
        if drop is not None:
            if isinstance(drop, bool) or not isinstance(drop, (int, float)):
                raise ValueError(f"分区 {zone} 的压降必须是数值或空")
            drop = float(drop)
            if drop < 0:
                raise ValueError(f"分区 {zone} 的压降不得为负")
        makeup = raw.get("makeup_t", 0.0)
        if isinstance(makeup, bool) or not isinstance(makeup, (int, float)):
            raise ValueError(f"分区 {zone} 的补水增量必须是数值")
        makeup = float(makeup)
        if makeup < 0:
            raise ValueError(f"分区 {zone} 的补水增量不得为负")
        return cls(zone=zone, drop_kpa=drop, makeup_t=makeup)

    def as_dict(self) -> dict:
        return {
            "zone": self.zone,
            "drop_kpa": None if self.drop_kpa is None else round(self.drop_kpa, 3),
            "makeup_t": round(self.makeup_t, 3),
        }


@dataclass(frozen=True)
class ZoneVerdict:
    """一个分区的判定结果：名次、压降速度、补水增量与嫌疑标记。"""

    rank: int
    zone: str
    drop_rate_kpa_s: float | None
    makeup_t: float
    suspect: bool
    reading_missing: bool

    def as_dict(self) -> dict:
        return {
            "rank": self.rank,
            "zone": self.zone,
            "drop_rate_kpa_s": (
                None if self.drop_rate_kpa_s is None else round(self.drop_rate_kpa_s, 6)
            ),
            "makeup_t": round(self.makeup_t, 3),
            "suspect": self.suspect,
            "reading_missing": self.reading_missing,
        }
