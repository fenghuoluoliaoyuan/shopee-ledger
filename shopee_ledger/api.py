"""自己店铺的只读接口。没有发货、上架、采集方法。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import urlopen

HOST = "https://partner.shopeemobile.com"
READ_PATHS = {
    "order_list": "/api/v2/order/get_order_list",
    "escrow": "/api/v2/payment/get_escrow_detail",
    "shipping_parameter": "/api/v2/logistics/get_shipping_parameter",
}


def sign(partner_id: int, partner_key: str, path: str, timestamp: int, access_token: str, shop_id: int) -> str:
    base = f"{partner_id}{path}{timestamp}{access_token}{shop_id}"
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


@dataclass
class ReadConfig:
    partner_id: int
    partner_key: str
    access_token: str
    shop_id: int
    host: str = HOST

    @classmethod
    def from_env(cls) -> "ReadConfig":
        missing = [
            name
            for name in (
                "SHOPEE_PARTNER_ID",
                "SHOPEE_PARTNER_KEY",
                "SHOPEE_ACCESS_TOKEN",
                "SHOPEE_SHOP_ID",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise ValueError("缺少环境变量: " + ", ".join(missing))
        return cls(
            partner_id=int(os.environ["SHOPEE_PARTNER_ID"]),
            partner_key=os.environ["SHOPEE_PARTNER_KEY"],
            access_token=os.environ["SHOPEE_ACCESS_TOKEN"],
            shop_id=int(os.environ["SHOPEE_SHOP_ID"]),
            host=os.environ.get("SHOPEE_HOST", HOST),
        )


class ReadClient:
    def __init__(self, config: ReadConfig):
        self.config = config

    def get_order_list(self, time_from: int, time_to: int, cursor: str = "") -> dict:
        return self._get(
            READ_PATHS["order_list"],
            {
                "time_range_field": "create_time",
                "time_from": time_from,
                "time_to": time_to,
                "page_size": 50,
                "cursor": cursor,
            },
        )

    def get_escrow_detail(self, order_sn: str) -> dict:
        return self._get(READ_PATHS["escrow"], {"order_sn": order_sn})

    def get_shipping_parameter(self, order_sn: str) -> dict:
        return self._get(READ_PATHS["shipping_parameter"], {"order_sn": order_sn})

    def _get(self, path: str, query: dict) -> dict:
        timestamp = int(time.time())
        digest = sign(
            self.config.partner_id,
            self.config.partner_key,
            path,
            timestamp,
            self.config.access_token,
            self.config.shop_id,
        )
        params = {
            "partner_id": self.config.partner_id,
            "timestamp": timestamp,
            "sign": digest,
            "access_token": self.config.access_token,
            "shop_id": self.config.shop_id,
            **query,
        }
        url = self.config.host + path + "?" + urlencode(params)
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode())
