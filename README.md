# Shopee 起步台账

按手册 v1.4 在本机记账。费率留空，不写入网上查到的马来本地店数字。

## 命令

```text
python -m shopee_ledger init
python -m shopee_ledger param-list --site MY
python -m shopee_ledger checklist
```

网页端：`python -m shopee_ledger web`，打开 http://127.0.0.1:8765 。版式参考 shadcn 后台的侧栏和表格，以及 Tremor 的指标卡。数据库默认在 `data/ledger.sqlite`。

先给站点填汇率、佣金率、交易手续费率、免运归属、提现费率、汇损率，再给候选品填退货率，然后 `quote`。缺任何一项，结果是 `incomplete`，不会当成 0。

出单顺序：`order-stock` → 未预确认则 `order-confirm-address` → `order-arrange` → `order-copy-address` → `order-purchase` → `order-inbound`。

`fetch-escrow` 只读自己店铺的 `get_escrow_detail`。需要环境变量 `SHOPEE_PARTNER_ID`、`SHOPEE_PARTNER_KEY`、`SHOPEE_ACCESS_TOKEN`、`SHOPEE_SHOP_ID`。没有发货和上架命令。

## 测试

```text
python -m unittest tests.test_ledger
```
