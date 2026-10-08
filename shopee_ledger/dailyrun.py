"""每日任务到底跑没跑：从 daily.log 里读，而不是问任务计划。

为什么需要这一层
----------------
2026-10-08 实测：任务计划里 `shopee-ledger daily check` 显示
`LastRunTime = 10:00:01`、`LastTaskResult = 0`（看起来"跑完了、没事"），
但 `data/daily.log` 里那天**一条记录都没有**。

任务计划自己的那两个字段分不清"跑完没事"和"触发了但根本没启动"——
`LastTaskResult` 会沿用上一次的值，`LastRunTime` 只说明触发时刻。
而唯一能独立作证的记录（`Microsoft-Windows-TaskScheduler/Operational`）
当时是**关闭**的，所以那天到底跑没跑，事后无法证实。

于是这里做一次可复查的判断：**只认日志里的 `=== begin ===`**。
它守的是"监测机制自己死了却没人知道"——那比某一次监测失败更危险。

判不出来不报警
--------------
没有日志文件（新克隆的仓库、还没跑过）算"还没跑过"的信息，不算告警。
只有**日志在、但最近一次运行已经超过 STALE_AFTER** 才报——
"判不出来 ≠ 出事了"，跟 `is_tracked()` 返回 None 同一个道理。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "data" / "daily.log"

# 日志行形如：[2026-10-08 13:20:52] === begin ===
BEGIN_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*=== begin ===$")

# 每天 10:00 跑一次。26 小时＝给任务计划留一点抖动，超过就是漏了一天。
STALE_AFTER = timedelta(hours=26)


def last_run(log_path: Path | str = DEFAULT_LOG) -> datetime | None:
    """日志里最后一次 ``=== begin ===`` 的时刻。没有/读不到返回 None。"""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    latest: datetime | None = None
    for line in text.splitlines():
        match = BEGIN_RE.match(line.strip())
        if not match:
            continue
        try:
            moment = datetime.fromisoformat(match.group(1))
        except ValueError:      # 格式对不上就当这行不存在，不猜
            continue
        if latest is None or moment > latest:
            latest = moment
    return latest


def describe(log_path: Path | str = DEFAULT_LOG,
             now: datetime | None = None) -> tuple[str, bool]:
    """返回 (一句人话, 是否该报警)。"""
    path = Path(log_path)
    moment = last_run(path)
    if moment is None:
        if not path.exists():
            # 新克隆的仓库本来就没有日志（data/*.log 不进版本库）。不是故障。
            return ("每日任务：还没有运行记录（%s 不存在）" % path.name, False)
        return ("每日任务：日志 %s 里没有任何一次运行记录" % path.name, True)

    delta = (now or datetime.now()) - moment
    hours = max(delta.total_seconds() / 3600.0, 0.0)
    when = moment.strftime("%Y-%m-%d %H:%M")
    if delta > STALE_AFTER:
        return ("每日任务：最近一次运行 %s（%.1f 天前）——超过 %.0f 小时没跑，"
                "说明某次 10:00 的计划任务没起来；任务计划的 LastRunTime/LastTaskResult "
                "不能当跑了没跑的证据（实测会显示 10:00:01 / 0）"
                % (when, hours / 24.0, STALE_AFTER.total_seconds() / 3600.0), True)
    return ("每日任务：最近一次运行 %s（%.1f 小时前）" % (when, hours), False)
