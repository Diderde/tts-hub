# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""逻辑音色注册表：SQLite 四表（voices / voice_bindings / call_log / vendor_state），
样本按内容哈希归档，WAL 模式支持多线程并发。"""

from __future__ import annotations

import contextlib
import hashlib
import pathlib
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["SCHEMA", "TaeHanazono", "nakiri_ayame", "oozora_subaru", "yuzuki_choco"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voice_bindings (
    id              TEXT PRIMARY KEY,
    voice_id        TEXT NOT NULL REFERENCES voices(id) ON DELETE CASCADE,
    vendor          TEXT NOT NULL,
    vendor_voice_id TEXT NOT NULL,
    model           TEXT,
    status          TEXT NOT NULL DEFAULT 'ready',
    sample_hash     TEXT,
    sample_path     TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    expires_at      TEXT,
    task_id         TEXT,
    -- 该逻辑音色的"首选厂商"标记：同一个音色至多一条为 1（P5 调音台的"一键设默认"）
    preferred       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (voice_id, vendor)
);

CREATE TABLE IF NOT EXISTS call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    vendor        TEXT NOT NULL,
    model         TEXT,
    voice_id      TEXT,
    chars         INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    cost_estimate REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    error_code    TEXT
);

CREATE INDEX IF NOT EXISTS idx_call_log_ts ON call_log(ts);
CREATE INDEX IF NOT EXISTS idx_call_log_vendor ON call_log(vendor);
CREATE INDEX IF NOT EXISTS idx_bindings_voice ON voice_bindings(voice_id);

-- 厂商启停的**运行期**状态：providers.yaml 是默认值，这里是 P2 管理接口改过的值。
-- 单独一张表而不是回写 YAML —— YAML 往返会丢掉用户写在里面的注释。
CREATE TABLE IF NOT EXISTS vendor_state (
    vendor     TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def nakiri_ayame(prefix: str) -> str:
    """生成记录 ID（``uuid4`` 走 os.urandom，不含可预测序列）。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def yuzuki_choco(data: bytes) -> str:
    """样本内容哈希（sha256 全串）：同一样本在多家复用克隆时不重复归档。"""
    return hashlib.sha256(data).hexdigest()


def oozora_subaru(moment: datetime | None = None) -> str:
    """UTC ISO-8601 时间戳（秒精度，便于字符串比较与排序）。"""
    stamp = moment or datetime.now(UTC)
    return stamp.astimezone(UTC).replace(microsecond=0).isoformat()


class TaeHanazono:
    """注册表门面。

    **每线程一条连接**：FastAPI 把同步端点丢进线程池执行，主线程建的 sqlite3 连接
    在别的线程里用会直接抛 ``ProgrammingError``。用 ``threading.local()`` 按线程各开
    一条连接即可绕开——不需要全局锁，SQLite 自己的文件锁配合 ``timeout`` 处理写竞争。
    """

    def __init__(
        self,
        db_path: str | pathlib.Path,
        *,
        samples_dir: str | pathlib.Path | None = None,
    ) -> None:
        self.db_path = pathlib.Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.samples_dir = pathlib.Path(samples_dir) if samples_dir else self.db_path.parent / "samples"
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._conn.executescript(SCHEMA)
        self._migrate()

    #: 老库补列：列名 -> **完整字面量** ALTER 语句。SQLite 的 ALTER/PRAGMA 不接受
    _ADDED_COLUMNS: Mapping[str, str] = {
        "task_id": "ALTER TABLE voice_bindings ADD COLUMN task_id TEXT",
        "preferred": "ALTER TABLE voice_bindings ADD COLUMN preferred INTEGER NOT NULL DEFAULT 0",
    }

    def _migrate(self) -> None:
        """幂等补列；表名经 ``pragma_table_info(?)`` 的绑定参数查询。"""
        existing = {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM pragma_table_info(?)", ("voice_bindings",)
            )
        }
        for column, statement in self._ADDED_COLUMNS.items():
            if column not in existing:
                self._conn.execute(statement)
        self._migrate_unique_voice_name()
        self._conn.commit()

    def _migrate_unique_voice_name(self) -> None:
        """给 ``voices.name`` 上 UNIQUE 约束，堵住 create_voice 的"查后插"竞态。

        老库里可能已存在重名（历史版本允许）：建索引前先把后来的重名**改名**
        （追加 ``~<id 尾段>``）而不是删除——删除会级联清掉它的绑定，数据没了。
        """
        index_name = "idx_voices_name_unique"
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (index_name,)
        ).fetchone()
        if exists:
            return
        seen: dict[str, str] = {}
        rows = self._conn.execute(
            "SELECT id, name FROM voices ORDER BY created_at, rowid"
        ).fetchall()
        for row in rows:
            name = str(row["name"])
            if name not in seen:
                seen[name] = str(row["id"])
                continue
            suffix = str(row["id"]).rsplit("_", 1)[-1]
            renamed = f"{name}~{suffix}"
            while renamed in seen:  # 理论撞名兜底：再补一段
                renamed = f"{renamed}x"
            seen[renamed] = str(row["id"])
            self._conn.execute(
                "UPDATE voices SET name = ? WHERE id = ?", (renamed, str(row["id"]))
            )
        self._conn.execute("CREATE UNIQUE INDEX idx_voices_name_unique ON voices(name)")

    @property
    def _conn(self) -> sqlite3.Connection:
        """当前线程的连接，按需建立。

        **WAL 日志模式**：FastAPI 线程池会多线程并发写注册表，默认 DELETE journal
        下多写并发会以 ``OperationalError: database is locked`` 裸抛（实测 8 线程
        并发 ``create_voice`` 有 6 个在 busy timeout 后炸掉，且异常类型不在归一
        错误体系内）。WAL 下写不再阻塞读、写写冲突由 busy timeout 正常排队；
        单文件本地库是 WAL 的标准适用场景。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
            self._opened.append(conn)
        return conn


    def close(self) -> None:
        for conn in self._opened:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        self._opened.clear()
        self._local = threading.local()

    def __enter__(self) -> TaeHanazono:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


    def archive_sample(self, data: bytes, filename: str) -> tuple[str, str]:
        """按内容哈希归档样本，返回 ``(hash, 路径)``；重复内容直接复用。"""
        digest = yuzuki_choco(data)
        suffix = pathlib.Path(filename).suffix.lower() or ".bin"
        target = self.samples_dir / digest[:2] / f"{digest}{suffix}"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return digest, str(target)


    def create_voice(self, name: str, tags: str = "") -> str:
        """新建逻辑音色；同名已存在时直接复用（避免重复建）。

        ``voices.name`` 上有 UNIQUE 索引：并发同名创建时后来者会撞索引，
        撞了就回查复用先建的那个，而不是抛 ``IntegrityError`` 砸到调用方。
        """
        existing = self.find_voice(name)
        if existing:
            return existing
        voice_id = nakiri_ayame("voice")
        try:
            self._conn.execute(
                "INSERT INTO voices (id, name, tags, created_at) VALUES (?, ?, ?, ?)",
                (voice_id, name, tags, oozora_subaru()),
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            existing = self.find_voice(name)
            if existing:
                return existing
            raise
        self._conn.commit()
        return voice_id

    def find_voice(self, name_or_id: str) -> str | None:
        """按 ID 或名称找逻辑音色，返回 ID。"""
        row = self._conn.execute("SELECT id FROM voices WHERE id = ?", (name_or_id,)).fetchone()
        if row:
            return str(row["id"])
        row = self._conn.execute(
            "SELECT id FROM voices WHERE name = ? ORDER BY created_at LIMIT 1", (name_or_id,)
        ).fetchone()
        return str(row["id"]) if row else None

    def list_voices(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, name, tags, created_at FROM voices ORDER BY created_at, name"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["bindings"] = self.bindings(voice_id=row["id"])
            out.append(item)
        return out

    def delete_voice(self, voice_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def update_voice(
        self, voice_id: str, *, name: str | None = None, tags: str | None = None
    ) -> bool:
        """改逻辑音色的名字/标签（P5 音色库的编辑动作）。只改给了值的那一项。"""
        sets: list[str] = []
        args: list[Any] = []
        if name:
            sets.append("name = ?")
            args.append(name)
        if tags is not None:
            sets.append("tags = ?")
            args.append(tags)
        if not sets:
            return False
        args.append(voice_id)
        cur = self._conn.execute(f"UPDATE voices SET {', '.join(sets)} WHERE id = ?", args)
        self._conn.commit()
        return cur.rowcount > 0


    def bind(
        self,
        voice_id: str,
        vendor: str,
        vendor_voice_id: str,
        *,
        model: str | None = None,
        status: str = "ready",
        sample_hash: str | None = None,
        sample_path: str | None = None,
        ttl_hours: int | None = None,
        task_id: str | None = None,
    ) -> str:
        """写一条绑定；同一 (逻辑音色, 厂商) 已存在则原地更新，不产生第二条。

        轮询型厂商（P3 起）在训练期间 ``vendor_voice_id`` 还是空的，真正的厂商音色 ID
        要等轮询到终态后由 :meth:`finish_binding` 回填。
        """
        now = datetime.now(UTC)
        expires = oozora_subaru(now + timedelta(hours=ttl_hours)) if ttl_hours else None
        row = self._conn.execute(
            "SELECT id FROM voice_bindings WHERE voice_id = ? AND vendor = ?",
            (voice_id, vendor),
        ).fetchone()
        if row:
            self._conn.execute(
                """UPDATE voice_bindings
                      SET vendor_voice_id = ?, model = ?, status = ?,
                          sample_hash = ?, sample_path = ?, expires_at = ?, task_id = ?
                    WHERE id = ?""",
                (
                    vendor_voice_id,
                    model,
                    status,
                    sample_hash,
                    sample_path,
                    expires,
                    task_id,
                    row["id"],
                ),
            )
            self._conn.commit()
            return str(row["id"])
        binding_id = nakiri_ayame("bind")
        try:
            self._conn.execute(
                """INSERT INTO voice_bindings
                       (id, voice_id, vendor, vendor_voice_id, model, status,
                        sample_hash, sample_path, created_at, expires_at, task_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    binding_id,
                    voice_id,
                    vendor,
                    vendor_voice_id,
                    model,
                    status,
                    sample_hash,
                    sample_path,
                    oozora_subaru(now),
                    expires,
                    task_id,
                ),
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return self.bind(
                voice_id,
                vendor,
                vendor_voice_id,
                model=model,
                status=status,
                sample_hash=sample_hash,
                sample_path=sample_path,
                ttl_hours=ttl_hours,
                task_id=task_id,
            )
        self._conn.commit()
        return binding_id

    def finish_binding(
        self,
        binding_id: str,
        *,
        vendor_voice_id: str,
        status: str = "ready",
        ttl_hours: int | None = None,
    ) -> bool:
        """轮询到终态后回填：厂商音色 ID 落地、状态改写、按需记 TTL。"""
        expires = (
            oozora_subaru(datetime.now(UTC) + timedelta(hours=ttl_hours))
            if ttl_hours
            else None
        )
        cur = self._conn.execute(
            "UPDATE voice_bindings SET vendor_voice_id = ?, status = ?, "
            "expires_at = COALESCE(?, expires_at) WHERE id = ?",
            (vendor_voice_id, status, expires, binding_id),
        )
        self._conn.commit()
        return bool(cur.rowcount)

    def find_binding_by_task(self, vendor: str, task_id: str) -> dict[str, Any] | None:
        """按 (厂商, 任务号) 找回绑定，供轮询完成后回填。"""
        row = self._conn.execute(
            "SELECT * FROM voice_bindings WHERE vendor = ? AND task_id = ? ORDER BY created_at DESC "
            "LIMIT 1",
            (vendor, task_id),
        ).fetchone()
        return dict(row) if row else None

    def bindings(
        self, *, voice_id: str | None = None, vendor: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM voice_bindings"
        where: list[str] = []
        args: list[Any] = []
        if voice_id:
            where.append("voice_id = ?")
            args.append(voice_id)
        if vendor:
            where.append("vendor = ?")
            args.append(vendor)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY preferred DESC, created_at"
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def resolve(self, name_or_id: str, *, vendor: str | None = None) -> str | None:
        """把逻辑音色（ID 或名称）解析成某厂商的 ``vendor_voice_id``。

        只认 ``ready`` 的绑定：训练中的绑定还没有可用的厂商音色 ID，返回空串给调用方
        用，只会把一个空 ``voice`` 参数发给厂商并换来一个难懂的报错。
        找不到逻辑音色时返回 ``None``，由调用方决定是"当作厂商内 voice_id 直用"
        还是报错——注册表层不替调用方做这个判断。
        """
        voice_id = self.find_voice(name_or_id)
        if not voice_id:
            return None
        for row in self.bindings(voice_id=voice_id, vendor=vendor):
            if row["status"] == "ready" and row["vendor_voice_id"]:
                return str(row["vendor_voice_id"])
        return None

    def set_binding_status(self, binding_id: str, status: str) -> bool:
        cur = self._conn.execute(
            "UPDATE voice_bindings SET status = ? WHERE id = ?", (status, binding_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_binding(self, binding_id: str) -> bool:
        """删一条绑定（只动本地注册表，不去厂商侧删音色——多数厂商根本没有删除端点）。"""
        cur = self._conn.execute("DELETE FROM voice_bindings WHERE id = ?", (binding_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def set_preferred_binding(self, voice_id: str, vendor: str) -> bool:
        """把某厂商的绑定设为该逻辑音色的首选（同音色其它绑定一并清标记）。

        两条 UPDATE 放在一个事务里：中间态"零个首选"虽然无害，但没必要让并发读到。
        """
        with self._conn:
            self._conn.execute(
                "UPDATE voice_bindings SET preferred = 0 WHERE voice_id = ?", (voice_id,)
            )
            cur = self._conn.execute(
                "UPDATE voice_bindings SET preferred = 1 WHERE voice_id = ? AND vendor = ?",
                (voice_id, vendor),
            )
        return cur.rowcount > 0

    def preferred_vendor(self, voice_id: str) -> str | None:
        """该逻辑音色的首选厂商；没设过就是 ``None``。"""
        row = self._conn.execute(
            "SELECT vendor FROM voice_bindings WHERE voice_id = ? AND preferred = 1 LIMIT 1",
            (voice_id,),
        ).fetchone()
        return str(row["vendor"]) if row else None

    def mark_used(self, vendor: str, vendor_voice_id: str, *, ttl_hours: int | None = None) -> bool:
        """记录一次"正式调用"，并顺延 TTL（MiniMax 的 168 小时保活要用）。"""
        now = datetime.now(UTC)
        expires = oozora_subaru(now + timedelta(hours=ttl_hours)) if ttl_hours else None
        cur = self._conn.execute(
            "UPDATE voice_bindings SET last_used_at = ?, expires_at = COALESCE(?, expires_at) "
            "WHERE vendor = ? AND vendor_voice_id = ?",
            (oozora_subaru(now), expires, vendor, vendor_voice_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def expiring_bindings(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """列出已过期或即将过期的绑定（TTL 为空的视为长期有效）。"""
        stamp = oozora_subaru(now)
        rows = self._conn.execute(
            "SELECT * FROM voice_bindings WHERE expires_at IS NOT NULL AND expires_at <= ? "
            "ORDER BY expires_at",
            (stamp,),
        ).fetchall()
        return [dict(r) for r in rows]


    def set_vendor_enabled(self, vendor: str, enabled: bool) -> None:
        """记下运行期的启停状态（覆盖 providers.yaml 的默认值，重启后仍生效）。"""
        self._conn.execute(
            "INSERT INTO vendor_state (vendor, enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(vendor) DO UPDATE SET enabled = excluded.enabled, "
            "updated_at = excluded.updated_at",
            (vendor, 1 if enabled else 0, oozora_subaru()),
        )
        self._conn.commit()

    def vendor_states(self) -> dict[str, bool]:
        """全部被显式改过启停状态的厂商。"""
        rows = self._conn.execute("SELECT vendor, enabled FROM vendor_state").fetchall()
        return {str(r["vendor"]): bool(r["enabled"]) for r in rows}

    def is_vendor_enabled(self, vendor: str, *, default: bool = True) -> bool:
        """某厂商当前是否启用；没被改过就回落到配置里的默认值。"""
        row = self._conn.execute(
            "SELECT enabled FROM vendor_state WHERE vendor = ?", (vendor,)
        ).fetchone()
        return bool(row["enabled"]) if row else default


    def log_call(
        self,
        *,
        vendor: str,
        model: str | None,
        voice_id: str | None,
        chars: int,
        latency_ms: int,
        cost_estimate: float | None,
        status: str,
        error_code: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO call_log
                   (ts, vendor, model, voice_id, chars, latency_ms, cost_estimate, status, error_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                oozora_subaru(),
                vendor,
                model,
                voice_id,
                int(chars),
                int(latency_ms),
                float(cost_estimate or 0.0),
                status,
                error_code,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def recent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM call_log ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    def search_calls(
        self,
        *,
        limit: int = 50,
        vendor: str | None = None,
        status: str | None = None,
        query: str | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        """调用日志检索：厂商、成败、近 N 天，外加一个跨字段的子串关键字。

        ``query`` 匹配 model / voice_id / error_code —— 排查"是哪家、哪个音色、报了什么码"
        时，这三列就是唯一定位信息。**只做参数化 LIKE 子串匹配**，不解析表达式：
        日志检索不该变成一个 SQL 方言入口。
        """
        sql = "SELECT * FROM call_log"
        where: list[str] = []
        args: list[Any] = []
        if vendor:
            where.append("vendor = ?")
            args.append(vendor)
        if status:
            where.append("status = ?")
            args.append(status)
        if days is not None:
            where.append("ts >= ?")
            args.append(oozora_subaru(datetime.now(UTC) - timedelta(days=int(days))))
        if query:
            needle = f"%{query}%"
            where.append("(model LIKE ? OR voice_id LIKE ? OR error_code LIKE ?)")
            args.extend([needle, needle, needle])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def cost_summary(self, *, days: int = 7, by: str = "vendor") -> list[dict[str, Any]]:
        """按维度汇总近 N 天的调用。``by`` 支持 vendor / model / day。"""
        column = {"vendor": "vendor", "model": "model", "day": "substr(ts, 1, 10)"}.get(by)
        if column is None:
            raise ValueError(f"不支持的汇总维度：{by!r}（可选 vendor/model/day）")
        since = oozora_subaru(datetime.now(UTC) - timedelta(days=int(days)))
        rows = self._conn.execute(
            f"""SELECT {column} AS bucket,
                       COUNT(*)              AS calls,
                       SUM(chars)            AS chars,
                       SUM(cost_estimate)    AS cost,
                       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_calls,
                       AVG(latency_ms)       AS avg_latency_ms
                  FROM call_log
                 WHERE ts >= ?
              GROUP BY bucket
              ORDER BY cost DESC, calls DESC""",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    def total_cost(self, *, days: int = 7) -> float:
        since = oozora_subaru(datetime.now(UTC) - timedelta(days=int(days)))
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_estimate), 0) AS total FROM call_log WHERE ts >= ?", (since,)
        ).fetchone()
        return float(row["total"] if row else 0.0)


    def vacuum(self) -> None:
        self._conn.execute("VACUUM")
        self._conn.commit()

    def tables(self) -> Sequence[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return tuple(str(r["name"]) for r in rows)

    def prune_calls(self, *, keep_days: int = 90) -> int:
        """清理过老记账（默认保留 90 天），返回删除行数。"""
        cutoff = oozora_subaru(datetime.now(UTC) - timedelta(days=int(keep_days)))
        cur = self._conn.execute("DELETE FROM call_log WHERE ts < ?", (cutoff,))
        self._conn.commit()
        return int(cur.rowcount or 0)

    def import_bindings(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """批量导入绑定（迁移/手工补录用）。返回导入条数。"""
        count = 0
        for row in rows:
            self.bind(
                str(row["voice_id"]),
                str(row["vendor"]),
                str(row["vendor_voice_id"]),
                model=row.get("model"),
                status=str(row.get("status") or "ready"),
                sample_hash=row.get("sample_hash"),
                sample_path=row.get("sample_path"),
            )
            count += 1
        return count
