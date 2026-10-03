"""v4.0 存储层：**元数据驱动**建表 + 只增不改表 + 配置指纹。

三条设计决定
------------
1. **表结构由 entities.json 生成**，不手写 DDL。
   对应 ERPNext 的 DocType 思路（每类单据一份 JSON 元数据），只是这里用标准库实现。
   改 spec 就改表结构，代码不动。
2. **只增不改靠数据库触发器**，不靠应用层自觉。
   ``CostSnapshot`` / ``SupplierQuote`` / ``AuditLog`` 三张表上加
   ``BEFORE UPDATE/DELETE → RAISE(ABORT)``：ORM 钩子拦不住裸连接，
   触发器在数据库层，谁绕过都要报错（INV-007 / INV-008）。
3. **配置指纹（params_version）**：参数值/等级/规则条件的哈希。
   每张成本快照都记下当时指纹，改配置后旧快照仍可复现（INV-009）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from shopee_ledger.spec import Spec, default_spec

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "ledger.sqlite"

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 实体字段类型 → SQLite 类型
TYPE_MAP = {"number": "REAL", "boolean": "INTEGER"}


def snake(name: str) -> str:
    """OnboardingCase → onboarding_case"""
    out = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return out


def table_for(entity_id: str) -> str:
    name = snake(entity_id)
    if not IDENT_RE.match(name):
        raise ValueError("非法实体名，不能作为表名: %r" % entity_id)
    return name


def column_for(field_name: str) -> str:
    name = snake(field_name)
    if not IDENT_RE.match(name):
        raise ValueError("非法字段名，不能作为列名: %r" % field_name)
    return name


def sql_type(field: dict[str, Any]) -> str:
    raw = str(field.get("type") or "string")
    if raw.startswith("array") or raw.startswith("ref<") or raw in ("map", "object", "any", "json"):
        return "TEXT"
    return TYPE_MAP.get(raw, "TEXT")


JSON_TYPES = ("any", "object", "map", "json")


def is_json_field(field: dict[str, Any] | None) -> bool:
    """any / object / map / array 一律按 JSON 存。

    不这么做会踩 SQLite 的列亲和性：TEXT 列插入数字会被悄悄转成字符串，
    读回来 `"0.15" != 0.15`，于是"值没变"被判成"变了"。
    """
    if not field:
        return False
    raw = str(field.get("type") or "string")
    return raw in JSON_TYPES or raw.startswith("array")


def _encode(value: Any, field: dict[str, Any] | None = None) -> Any:
    if field is not None and is_json_field(field):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _decode(raw: Any, field: dict[str, Any]) -> Any:
    if raw is None:
        return None
    if is_json_field(field):
        if not isinstance(raw, str):
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            return raw  # 旧数据可能是裸标量，读得回来就行
    if sql_type(field) == "TEXT" and isinstance(raw, str) and raw[:1] in "[{":
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


class Storage:
    def __init__(self, path: Path | str = DEFAULT_DB, spec: Spec | None = None):
        self.path = Path(path)
        self.spec = spec or default_spec()
        self.conn: sqlite3.Connection | None = None

    # ---- 连接与建表 -----------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            # 触发器 RAISE(ABORT) 后可能留有未结束的事务；Windows 上不回收会锁住文件
            try:
                self.conn.rollback()
            except sqlite3.Error:
                pass
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Storage":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def entity(self, entity_id: str) -> dict[str, Any]:
        try:
            return self.spec.entities[entity_id]
        except KeyError as exc:
            raise KeyError("entities.json 里没有实体 %s" % entity_id) from exc

    def is_immutable(self, entity_id: str) -> bool:
        """只增不改的判定来源：entities.json 顶层的 immutable_entities 列表（或实体自带标记）。"""
        if entity_id in getattr(self.spec, "immutable_entities", set()):
            return True
        return bool(self.entity(entity_id).get("immutable"))

    def ddl(self, entity_id: str) -> str:
        entity = self.entity(entity_id)
        table = table_for(entity_id)
        columns = ['"id" INTEGER PRIMARY KEY AUTOINCREMENT']
        for field in entity.get("fields") or []:
            name = column_for(field["name"])
            columns.append('"%s" %s' % (name, sql_type(field)))
        # 有些实体自己就声明了 created_at（如 CostSnapshot），不要重复加列
        if "created_at" not in self._field_columns(entity_id):
            columns.append('"created_at" TEXT NOT NULL')
        return 'CREATE TABLE IF NOT EXISTS "%s" (\n  %s\n);' % (table, ",\n  ".join(columns))

    def _field_columns(self, entity_id: str) -> list[str]:
        return [column_for(f["name"]) for f in self.entity(entity_id).get("fields") or []]

    def init(self) -> list[str]:
        conn = self.connect()
        created: list[str] = []
        for entity_id in self.spec.entities:
            conn.execute(self.ddl(entity_id))
            created.append(table_for(entity_id))
        for entity_id in self.spec.entities:
            self._migrate(entity_id)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config_version ("
            "  fingerprint TEXT PRIMARY KEY,"
            "  params INTEGER NOT NULL,"
            "  rules INTEGER NOT NULL,"
            "  recorded_at TEXT NOT NULL);"
        )
        for entity_id in self.spec.entities:
            if self.is_immutable(entity_id):
                self._install_append_only_triggers(table_for(entity_id))
        conn.commit()
        return created

    def _existing_columns(self, table: str) -> set[str]:
        rows = self.connect().execute('PRAGMA table_info("%s")' % table).fetchall()
        return {row["name"] for row in rows}

    def _migrate(self, entity_id: str) -> list[str]:
        """元数据里加了字段，给**已经存在**的表补列。

        ``CREATE TABLE IF NOT EXISTS`` 不会改已存在的表——加了字段不补列，
        写进去就报 "no such column"（实测踩过：给 WatchEntry 加 content_ref）。
        SQLite 的 ADD COLUMN 只能加可空列，正好符合这里的用法。
        """
        table = table_for(entity_id)
        existing = self._existing_columns(table)
        added: list[str] = []
        for field in self.entity(entity_id).get("fields") or []:
            name = column_for(field["name"])
            if name not in existing:
                self.connect().execute('ALTER TABLE "%s" ADD COLUMN "%s" %s'
                                       % (table, name, sql_type(field)))
                added.append(name)
        # insert() 会自动写 created_at（除非实体自己声明了同名列），迁移也要补上
        if "created_at" not in existing and "created_at" not in self._field_columns(entity_id):
            self.connect().execute('ALTER TABLE "%s" ADD COLUMN "created_at" TEXT' % table)
            added.append("created_at")
        return added

    def _install_append_only_triggers(self, table: str) -> None:
        """只增不改：数据库层兜底，绕过应用层也拦得住（INV-007）。"""
        conn = self.connect()
        for verb in ("UPDATE", "DELETE"):
            conn.execute(
                'CREATE TRIGGER IF NOT EXISTS "%s_no_%s" BEFORE %s ON "%s"\n'
                "BEGIN\n  SELECT RAISE(ABORT, '%s is append-only');\nEND;"
                % (table, verb.lower(), verb, table, table)
            )

    # ---- 配置指纹 -------------------------------------------------------
    def fingerprint(self) -> str:
        """参数（值/等级/状态）+ 规则（等级/条件）的稳定哈希。"""
        params = [
            [pid, p.value, p.evidence_level, p.state, p.unverified_claim]
            for pid, p in sorted(self.spec.params.items())
        ]
        rules = [[rid, r.level, r.condition] for rid, r in sorted(self.spec.rules.items())]
        payload = json.dumps({"params": params, "rules": rules}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def record_config_version(self) -> str:
        conn = self.connect()
        digest = self.fingerprint()
        conn.execute(
            "INSERT OR IGNORE INTO config_version (fingerprint, params, rules, recorded_at)"
            " VALUES (?,?,?,?)",
            (digest, len(self.spec.params), len(self.spec.rules), _now()),
        )
        conn.commit()
        return digest

    # ---- 通用读写 -------------------------------------------------------
    def _columns(self, entity_id: str) -> list[str]:
        return self._field_columns(entity_id)

    def _row_to_dict(self, entity_id: str, row: sqlite3.Row) -> dict[str, Any]:
        entity = self.entity(entity_id)
        keys = set(row.keys())
        out: dict[str, Any] = {"id": row["id"]}
        if "created_at" in keys:
            out["created_at"] = row["created_at"]
        for field in entity.get("fields") or []:
            name = column_for(field["name"])
            out[name] = _decode(row[name], field) if name in keys else None
        return out

    def insert(self, entity_id: str, data: dict[str, Any]) -> int:
        entity = self.entity(entity_id)
        table = table_for(entity_id)
        fields = entity.get("fields") or []
        field_cols = [column_for(field["name"]) for field in fields]
        pairs = []
        for field in fields:
            name = column_for(field["name"])
            if name in data:
                pairs.append((name, _encode(data[name], field)))
        cols = [p[0] for p in pairs]
        values = [p[1] for p in pairs]
        if "created_at" not in field_cols:
            cols.append("created_at")
            values.append(_now())
        marks = ["?"] * len(cols)
        sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
            table, ", ".join('"%s"' % c for c in cols), ", ".join(marks))
        cur = self.connect().execute(sql, values)
        self.connect().commit()
        return int(cur.lastrowid)

    def get(self, entity_id: str, row_id: int) -> dict[str, Any] | None:
        row = self.connect().execute(
            'SELECT * FROM "%s" WHERE id = ?' % table_for(entity_id), (row_id,)).fetchone()
        return self._row_to_dict(entity_id, row) if row else None

    def update(self, entity_id: str, row_id: int, data: dict[str, Any]) -> None:
        if self.is_immutable(entity_id):
            raise ValueError("%s 是只增不改表，禁止 UPDATE（INV-007）" % entity_id)
        by_column = {column_for(field["name"]): field for field in self.entity(entity_id).get("fields") or []}
        pairs = [(key, _encode(value, by_column.get(key)))
                 for key, value in data.items() if key in by_column]
        if not pairs:
            return
        sql = 'UPDATE "%s" SET %s WHERE id = ?' % (
            table_for(entity_id), ", ".join('"%s" = ?' % k for k, _ in pairs))
        self.connect().execute(sql, [v for _, v in pairs] + [row_id])
        self.connect().commit()

    def list(self, entity_id: str, limit: int = 200, where: str | None = None,
             args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        sql = 'SELECT * FROM "%s"' % table_for(entity_id)
        if where:
            sql += " WHERE " + where
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self.connect().execute(sql, list(args) + [limit]).fetchall()
        return [self._row_to_dict(entity_id, row) for row in rows]

    def count(self, entity_id: str) -> int:
        row = self.connect().execute('SELECT COUNT(*) AS n FROM "%s"' % table_for(entity_id)).fetchone()
        return int(row["n"])

    # ---- 审计与快照 -----------------------------------------------------
    def record_audit(self, action: str, object_type: str, object_id: Any, *,
                     rule_id: str | None = None, params_version: str | None = None,
                     result: str | None = None, operator: str = "cli",
                     detail: dict[str, Any] | None = None, at: str | None = None) -> int:
        """at 用于补录历史事件。审计表只增不改，所以只能追加一条带旧时间戳的记录。"""
        return self.insert("AuditLog", {
            "action": action,
            "object_type": object_type,
            "object_id": str(object_id),
            "rule_id": rule_id,
            "params_version": params_version or self.fingerprint(),
            "result": result,
            "operator": operator,
            "at": at or _now(),
            "detail": detail or {},
        })

    def save_cost_snapshot(self, result: Any, *, subject_type: str, subject_id: Any,
                           platform: str = "shopee", market: str = "TW",
                           mode: str = "dropship", snapshot_type: str = "estimate") -> int:
        """把一次核算完整留档：输入、输出、证据链、配置指纹（只增不改）。"""
        version = self.record_config_version()
        return self.insert("CostSnapshot", {
            "subject_type": subject_type,
            "subject_id": str(subject_id),
            "platform": platform,
            "market": market,
            "mode": mode,
            "type": snapshot_type,
            "inputs": result.context or {},
            "params_version": version,
            "outputs": {
                "status": result.status, "net_profit": result.net, "net_margin": result.rate,
                "missing_fields": result.missing, "buyer_price_uplift": result.buyer_price_uplift,
                "components": result.components,
            },
            "trace": [
                {"label": e.label, "value": e.value, "source": e.source,
                 "level": e.level, "note": e.note}
                for e in result.trace
            ],
            "created_at": _now(),
        })


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
