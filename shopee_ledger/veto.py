"""第 3.1 节一票否决。重量不在此列。"""

from __future__ import annotations

VETO_FLAGS = {
    "brand_ip": "含品牌 / IP / 卡通形象",
    "liquid_powder_battery": "液体、粉末、电池",
    "fragile": "易碎品",
    "apparel": "服装、鞋帽",
    "needs_cert": "需认证品类",
}


def veto_reasons(flags: list[str]) -> list[str]:
    unknown = [flag for flag in flags if flag not in VETO_FLAGS and flag != ""]
    if unknown:
        raise ValueError("未知否决项: " + ",".join(unknown))
    return [VETO_FLAGS[flag] for flag in flags if flag in VETO_FLAGS]
