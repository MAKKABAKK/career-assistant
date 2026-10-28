"""Versioned course records in Radar's SQLite database; no second business DB."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid

from career_assistant.db import _SCHEMA as RADAR_SCHEMA
from career_assistant.models import now_hk, to_iso

UPSTREAM_COMMIT = "e2bcb2ba917644b8a88d9981d4e3eb514be98c65"
SCHEMA_VERSION = 4
TABLES = {
    name: f"course_{name}" for name in (
        "resumes", "resume_versions", "reports", "preferences", "job_snapshots",
        "matches", "materials", "application_events", "tasks", "email_attempts", "preparations",
    )
}
TABLES["applications"] = "applications"
TABLES["jobs"] = "jobs"
APPLICATION_STATUSES = {"saved", "preparing", "applied", "interview", "offer", "rejected", "withdrawn"}

SCHEMA = """
CREATE TABLE course_resumes (
 id INTEGER PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
 sha256 TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE course_reports (
 id INTEGER PRIMARY KEY, resume_id INTEGER NOT NULL REFERENCES course_resumes(id),
 data_json TEXT NOT NULL, task_id INTEGER UNIQUE REFERENCES course_tasks(id),
 created_at TEXT NOT NULL
);
CREATE TABLE course_resume_versions (
 id INTEGER PRIMARY KEY, resume_id INTEGER NOT NULL REFERENCES course_resumes(id),
 version INTEGER NOT NULL, facts_json TEXT NOT NULL,
 report_id INTEGER REFERENCES course_reports(id), confirmed_at TEXT NOT NULL,
 UNIQUE(resume_id,version)
);
CREATE TABLE course_preferences (
 id INTEGER PRIMARY KEY,
 resume_version_id INTEGER NOT NULL REFERENCES course_resume_versions(id),
 data_json TEXT NOT NULL, confirmed_at TEXT NOT NULL
);
CREATE TABLE course_job_snapshots (
 id INTEGER PRIMARY KEY, job_uid TEXT NOT NULL REFERENCES jobs(job_uid),
 title TEXT NOT NULL, company TEXT NOT NULL, source TEXT NOT NULL,
 external_id TEXT NOT NULL, url TEXT NOT NULL, jd TEXT NOT NULL,
 is_full INTEGER NOT NULL CHECK(is_full IN (0,1)), fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(job_uid,fingerprint)
);
CREATE TABLE course_matches (
 id INTEGER PRIMARY KEY,
 resume_version_id INTEGER NOT NULL REFERENCES course_resume_versions(id),
 snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id),
 preference_id INTEGER REFERENCES course_preferences(id),
 data_json TEXT NOT NULL, task_id INTEGER UNIQUE REFERENCES course_tasks(id),
 created_at TEXT NOT NULL
);
CREATE TABLE course_materials (
 id INTEGER PRIMARY KEY, match_id INTEGER NOT NULL REFERENCES course_matches(id),
 version INTEGER NOT NULL, data_json TEXT NOT NULL,
 parent_id INTEGER REFERENCES course_materials(id),
 task_id INTEGER UNIQUE REFERENCES course_tasks(id), created_at TEXT NOT NULL,
 UNIQUE(match_id,version)
);
CREATE TABLE course_application_events (
 id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE course_tasks (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'queued'
   CHECK(status IN ('queued','running','succeeded','failed','uncertain')),
 attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
 side_effect INTEGER NOT NULL DEFAULT 0 CHECK(side_effect IN (0,1)),
 owner TEXT, error TEXT NOT NULL DEFAULT '', result_json TEXT,
 available_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX course_tasks_pending ON course_tasks(status,available_at,id);
CREATE TABLE course_email_attempts (
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES course_tasks(id),
 status TEXT NOT NULL CHECK(status IN ('preview','failed','accepted','uncertain')),
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""


def stamp() -> str:
    return to_iso(now_hk())


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value is not None else None
    return result


class Store:
    """Short-lived per-operation connections also work with Flask and a worker thread."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser().resolve()
        self.db_path = str(self.path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_v1(self) -> str | None:
        """Back up an existing database before an explicit, atomic schema migration.

        sqlite3.backup includes committed WAL pages. The backup remains a complete
        readable pre-migration database. Already migrated databases are untouched.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        backup = None
        with self.connect() as conn:
            has_migrations = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='course_migrations'"
            ).fetchone()
            if has_migrations:
                current = conn.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0]
                if current and current > 1:
                    raise RuntimeError("数据库版本比本程序新；请使用相应版本的程序。")
                if current == 1:
                    return None
            if existed:
                backup = str(self.path.with_name(
                    f"{self.path.name}.before-course-v1-{uuid.uuid4().hex[:12]}.bak"
                ))
                with sqlite3.connect(backup) as destination:
                    conn.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("数据库备份校验失败，未执行迁移。")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS course_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, upstream_commit TEXT NOT NULL)"
            )
            # Another startup may have completed the migration before this lock.
            if conn.execute("SELECT 1 FROM course_migrations WHERE version=1").fetchone():
                return backup
            for statement in (RADAR_SCHEMA + SCHEMA).split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
            additions = {
                "course_resume_version_id": "INTEGER REFERENCES course_resume_versions(id)",
                "course_material_id": "INTEGER REFERENCES course_materials(id)",
                "course_snapshot_id": "INTEGER REFERENCES course_job_snapshots(id)",
                "created_at": "TEXT", "updated_at": "TEXT",
            }
            for column, definition in additions.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE applications ADD COLUMN {column} {definition}")
            # Immutable evidence must survive later edits and application changes.
            for table in (
                "reports", "resume_versions", "preferences",
                "job_snapshots", "matches", "materials", "application_events",
            ):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER course_{table}_no_{action.lower()} "
                        f"BEFORE {action} ON course_{table} BEGIN "
                        "SELECT RAISE(ABORT,'历史版本不可修改；请另存新版本'); END"
                    )
            conn.execute(
                "CREATE TRIGGER course_resumes_no_update BEFORE UPDATE ON course_resumes "
                "WHEN OLD.text != '' OR NEW.text = '' OR NEW.filename != OLD.filename "
                "OR NEW.path != OLD.path OR NEW.sha256 != OLD.sha256 "
                "OR NEW.created_at != OLD.created_at BEGIN "
                "SELECT RAISE(ABORT,'简历原文只能首次写入'); END"
            )
            conn.execute(
                "CREATE TRIGGER course_resumes_no_delete BEFORE DELETE ON course_resumes "
                "BEGIN SELECT RAISE(ABORT,'简历原件不可删除'); END"
            )
            conn.execute(
                "INSERT INTO course_migrations VALUES(?,?,?)",
                (1, stamp(), UPSTREAM_COMMIT),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"迁移发现外键错误：{len(violations)} 条；已回滚。")
        return backup

    def _migrate_v2(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = conn.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 2:
            raise RuntimeError("数据库版本比本程序新；请使用相应版本的程序。")
        if current == 2:
            return None
        first_backup = self._migrate_v1() if not current else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v2-{uuid.uuid4().hex[:12]}.bak")) if current else None
        with self.connect() as conn:
            if backup:
                with sqlite3.connect(backup) as destination:
                    conn.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("数据库备份校验失败")
            # Rebuild only the nullable legacy preference link. Preserve IDs and all FK targets.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM course_migrations WHERE version=2").fetchone():
                return backup
            conn.execute("CREATE TABLE course_preferences_v2 (id INTEGER PRIMARY KEY, "
                         "resume_version_id INTEGER REFERENCES course_resume_versions(id), "
                         "data_json TEXT NOT NULL, confirmed_at TEXT NOT NULL)")
            conn.execute("INSERT INTO course_preferences_v2 SELECT * FROM course_preferences")
            conn.execute("DROP TABLE course_preferences")
            conn.execute("ALTER TABLE course_preferences_v2 RENAME TO course_preferences")
            for action in ("UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER course_preferences_no_{action.lower()} BEFORE {action} "
                             "ON course_preferences BEGIN SELECT RAISE(ABORT,'历史版本不可修改'); END")
            conn.execute("ALTER TABLE course_resume_versions ADD COLUMN origin TEXT NOT NULL DEFAULT 'user_edited'")
            conn.execute("ALTER TABLE course_resume_versions ADD COLUMN extraction_task_id INTEGER REFERENCES course_tasks(id)")
            conn.execute("CREATE UNIQUE INDEX course_extraction_task ON course_resume_versions(extraction_task_id)")
            conn.execute("ALTER TABLE course_application_events ADD COLUMN basis_json TEXT NOT NULL DEFAULT '{}'")
            conn.execute("CREATE TABLE course_preparations (id INTEGER PRIMARY KEY, "
                         "original_snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id), "
                         "snapshot_id INTEGER NOT NULL REFERENCES course_job_snapshots(id), "
                         "resume_id INTEGER REFERENCES course_resumes(id), "
                         "resume_version_id INTEGER REFERENCES course_resume_versions(id), "
                         "preference_id INTEGER REFERENCES course_preferences(id), "
                         "match_id INTEGER REFERENCES course_matches(id), material_id INTEGER REFERENCES course_materials(id), "
                         "state TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '', "
                         "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO course_migrations VALUES(?,?,?)", (2, stamp(), UPSTREAM_COMMIT))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v2 迁移外键校验失败")
        return first_backup or backup

    def migrate(self) -> str | None:
        # A live worker must never race schema changes. A current schema is read-only.
        if self.path.exists():
            with self.connect() as c:
                exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
                if exists and (c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] or 0) >= SCHEMA_VERSION:
                    return self._migrate_v4()
        from .worker import FileLock
        lock = FileLock(Path(f"{self.path}.worker.lock"))
        lock.acquire()
        try:
            return self._migrate_v4()
        finally:
            lock.release()

    def _migrate_v3(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > 3:
            raise RuntimeError("数据库版本比本程序新")
        if current == 3:
            return None
        first = self._migrate_v2() if current != 2 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v3-{uuid.uuid4().hex[:12]}.bak")) if current == 2 else None
        with self.connect() as c:
            if backup:
                with sqlite3.connect(backup) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM course_migrations WHERE version=3").fetchone():
                return first or backup
            c.execute("ALTER TABLE course_preparations ADD COLUMN scope TEXT NOT NULL DEFAULT 'analysis'")
            c.execute("ALTER TABLE course_tasks ADD COLUMN checkpoint_json TEXT")
            c.execute("ALTER TABLE course_application_events ADD COLUMN event_date TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE applications ADD COLUMN course_planned_json TEXT NOT NULL DEFAULT '{}'")
            c.execute("ALTER TABLE applications ADD COLUMN course_actual_json TEXT NOT NULL DEFAULT '{}'")
            for row in c.execute("SELECT * FROM applications").fetchall():
                basis = {"snapshot_id": row["course_snapshot_id"], "resume_version_id": row["course_resume_version_id"],
                         "material_id": row["course_material_id"]}
                column = "course_actual_json" if row["applied_date"] else "course_planned_json"
                c.execute(f"UPDATE applications SET {column}=? WHERE id=?", (encode(basis), row["id"]))
            c.execute("UPDATE course_preparations SET state='matched',error='' WHERE match_id IS NOT NULL AND material_id IS NULL")
            c.execute("UPDATE course_tasks SET status='failed',error='v3 已停用自动填写',finished_at=?,updated_at=? "
                      "WHERE kind='fill' AND status IN ('queued','running','uncertain')", (stamp(), stamp()))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v3 迁移外键校验失败")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (3, stamp(), UPSTREAM_COMMIT))
        return first or backup

    def _migrate_v4(self) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='course_migrations'").fetchone()
            current = c.execute("SELECT MAX(version) FROM course_migrations").fetchone()[0] if exists else 0
        if current and current > SCHEMA_VERSION:
            raise RuntimeError("数据库版本比本程序新")
        if current == SCHEMA_VERSION:
            return None
        first = self._migrate_v3() if current != 3 else None
        backup = str(self.path.with_name(f"{self.path.name}.before-course-v4-{uuid.uuid4().hex[:12]}.bak")) if current == 3 else None
        with self.connect() as c:
            if backup:
                with sqlite3.connect(backup) as destination:
                    c.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份校验失败")
            c.execute("BEGIN IMMEDIATE")
            c.execute("ALTER TABLE jobs ADD COLUMN last_fetched_at TEXT")
            # Legacy snapshot timestamps include local edits, not just fetches.
            # Use the known first acquisition, never migration time or local edits.
            c.execute("UPDATE jobs SET last_fetched_at=first_seen_at")
            c.execute("INSERT OR REPLACE INTO kv_store(key,value) VALUES('course_snapshot_sequence', "
                      "(SELECT COALESCE(MAX(id),0) FROM course_job_snapshots))")
            c.execute("INSERT INTO course_migrations VALUES(?,?,?)", (4, stamp(), UPSTREAM_COMMIT))
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("v4 迁移外键校验失败")
        return first or backup

    def cleanup_job_cache(self, *, now=None) -> int:
        """Delete only expired, unreferenced source cache in one transaction.

        Failed tasks remain retryable, so conservatively protect their inputs too.
        All preparations and upstream feedback are retained as historical evidence.
        """
        cutoff = to_iso((now or now_hk()) - timedelta(days=30))
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            protected_uids, protected_snapshots = set(), set()
            def references(value, key=''):
                if isinstance(value, dict):
                    for k, v in value.items(): references(v, k)
                elif isinstance(value, list):
                    for v in value: references(v, key)
                elif key in {'snapshot_id', 'snapshot_ids', 'original_snapshot_id'}:
                    protected_snapshots.add(value)
                elif key in {'job_uid', 'job_uids'}:
                    protected_uids.add(value)
            for row in c.execute("SELECT payload_json,result_json,checkpoint_json FROM course_tasks WHERE status!='succeeded'"):
                for value in row:
                    if value: references(json.loads(value))
            candidates = c.execute("""
                SELECT job_uid FROM jobs j
                WHERE source!='manual' AND status NOT IN ('interested','saved')
                AND julianday(COALESCE(last_fetched_at,first_seen_at))<=julianday(?)
                AND NOT EXISTS(SELECT 1 FROM applications a WHERE a.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM user_feedback f WHERE f.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN applications a
                    ON a.course_snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN course_matches m
                    ON m.snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s JOIN course_preparations p
                    ON p.snapshot_id=s.id OR p.original_snapshot_id=s.id WHERE s.job_uid=j.job_uid)
                AND NOT EXISTS(SELECT 1 FROM course_job_snapshots s WHERE s.job_uid=j.job_uid AND s.source='manual')
            """, (cutoff,)).fetchall()
            deleted = 0
            # Remove the blanket guard only within this write-locked transaction.
            # Restore it before commit; any error rolls back both DDL and deletes.
            trigger = c.execute("SELECT sql FROM sqlite_master WHERE name='course_job_snapshots_no_delete'").fetchone()[0]
            c.execute("DROP TRIGGER course_job_snapshots_no_delete")
            for row in candidates:
                uid = row['job_uid']
                ids = {r[0] for r in c.execute("SELECT id FROM course_job_snapshots WHERE job_uid=?", (uid,))}
                if uid in protected_uids or ids & protected_snapshots:
                    continue
                c.execute("DELETE FROM course_job_snapshots WHERE job_uid=?", (uid,))
                c.execute("DELETE FROM jobs WHERE job_uid=?", (uid,))
                deleted += 1
            c.execute(trigger)
        return deleted

    def checkpoint(self, task_id, data):
        with self.connect() as c:
            c.execute("UPDATE course_tasks SET checkpoint_json=? WHERE id=?", (encode(data), task_id))

    def accept_material_inputs(self, task_id, submitted):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            task = decode(c.execute("SELECT * FROM course_tasks WHERE id=?", (task_id,)).fetchone())
            if not task or task["kind"] != "materials" or (task.get("result") or {}).get("status") != "waiting_input":
                raise ValueError("当前任务不等待材料补充")
            cp = task["checkpoint"] or {}
            if not cp.get("input_saved"):
                questions = cp["analysis"]["questions"]
                answers = {q: submitted.get(f"answer_{i}", "").strip() for i, q in enumerate(questions)}
                cp.update(answers=answers, skipped=[q for q in questions if not answers[q]], input_saved=True)
                c.execute("UPDATE course_tasks SET checkpoint_json=? WHERE id=?", (encode(cp), task_id))
            return cp

    def resume_waiting(self, resume_id):
        versions = self.list("resume_versions", resume_id=resume_id)
        if not versions:
            return
        for item in self.list("preparations", resume_id=resume_id):
            if item["resume_version_id"] is not None:
                continue
            self.update_preparation(item["id"], resume_version_id=versions[0]["id"])
            if item["state"] == "waiting_facts":
                self.update_preparation(item["id"], state="queued", error="")
                self.enqueue("prepare", {"preparation_id": item["id"]},
                             f"facts-ready:{item['id']}:{versions[0]['id']}", max_attempts=2)

    @staticmethod
    def _table(name: str) -> str:
        try:
            return TABLES[name]
        except KeyError:
            raise ValueError(f"未知数据表：{name}") from None

    def get(self, table: str, record_id: int | str) -> dict | None:
        key = "job_uid" if table == "jobs" else "id"
        with self.connect() as conn:
            return decode(conn.execute(
                f"SELECT * FROM {self._table(table)} WHERE {key}=?", (record_id,)
            ).fetchone())

    def list(self, table: str, **filters: Any) -> list[dict]:
        name = self._table(table)
        with self.connect() as conn:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}
            if not set(filters).issubset(columns):
                raise ValueError("未知筛选字段")
            where = " AND ".join(f"{key}=?" for key in filters) or "1"
            ordering = "first_seen_at DESC" if table == "jobs" else "id DESC"
            return [decode(row) for row in conn.execute(
                f"SELECT * FROM {name} WHERE {where} ORDER BY {ordering}", tuple(filters.values())
            )]

    def _insert(self, table: str, values: dict) -> int:
        with self.connect() as conn:
            return self._insert_on(conn, table, values)

    @staticmethod
    def _insert_on(conn: sqlite3.Connection, table: str, values: dict) -> int:
        # Called with internal column names only; callers never provide raw SQL.
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        cursor = conn.execute(
            f"INSERT INTO {Store._table(table)} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return int(cursor.lastrowid)

    def add_resume(self, filename: str, path: str | Path, sha256: str, text: str = "") -> int:
        return self._insert("resumes", dict(
            filename=filename, path=str(Path(path).resolve()), sha256=sha256,
            text=text, created_at=stamp(),
        ))

    def set_resume_text(self, resume_id: int, text: str) -> None:
        if not text.strip():
            raise ValueError("简历原文不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            resume = conn.execute("SELECT text FROM course_resumes WHERE id=?", (resume_id,)).fetchone()
            if resume is None:
                raise ValueError("简历不存在")
            if resume["text"] == text:
                return
            if resume["text"]:
                raise ValueError("不能覆盖已经提取的简历原文")
            conn.execute("UPDATE course_resumes SET text=? WHERE id=?", (text, resume_id))

    def save_report(self, resume_id: int, data: dict, task_id: int | None = None) -> int:
        if task_id is not None:
            existing = self.list("reports", task_id=task_id)
            if existing:
                return existing[0]["id"]
        return self._insert("reports", dict(
            resume_id=resume_id, data_json=encode(data), task_id=task_id, created_at=stamp(),
        ))

    def confirm_facts(self, resume_id: int, facts: dict, report_id: int | None = None, *, origin="user_edited", extraction_task_id=None) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if extraction_task_id is not None:
                existing = conn.execute("SELECT id FROM course_resume_versions WHERE extraction_task_id=?", (extraction_task_id,)).fetchone()
                if existing:
                    return existing["id"]
            resume = conn.execute("SELECT text FROM course_resumes WHERE id=?", (resume_id,)).fetchone()
            if resume is None or not resume["text"]:
                raise ValueError("简历原文未解析，暂不能确认事实")
            if report_id is not None:
                report = conn.execute("SELECT resume_id FROM course_reports WHERE id=?", (report_id,)).fetchone()
                if report is None or report["resume_id"] != resume_id:
                    raise ValueError("报告与简历不一致")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM course_resume_versions WHERE resume_id=?",
                (resume_id,),
            ).fetchone()[0]
            return self._insert_on(conn, "resume_versions", dict(
                resume_id=resume_id, version=version, facts_json=encode(facts),
                report_id=report_id, confirmed_at=stamp(), origin=origin, extraction_task_id=extraction_task_id,
            ))

    def confirm_preferences(self, resume_version_id: int, data: dict) -> int:
        return self._insert("preferences", dict(
            resume_version_id=resume_version_id, data_json=encode(data), confirmed_at=stamp(),
        ))

    def snapshot_job(self, job_uid: str, jd: str, is_full: bool = False, metadata: dict | None = None, *, fetched: bool = False) -> int:
        job = self.get("jobs", job_uid)
        if job is None:
            raise ValueError("岗位不存在")
        if is_full and not jd.strip():
            raise ValueError("完整岗位描述不能为空")
        values = {key: job[key] for key in ("job_uid", "title", "company", "source", "external_id", "url")}
        if metadata is not None:
            for key in ("title", "company", "source", "external_id", "url"):
                if not isinstance(metadata.get(key), str) or (key in {"title", "source"} and not metadata[key].strip()):
                    raise ValueError(f"岗位快照缺少 {key}")
                values[key] = metadata[key]
        if values["source"] == "manual":
            values["external_id"] = ""
        values.update(jd=jd, is_full=int(is_full))
        fingerprint = hashlib.sha256(encode(values).encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if fetched:
                conn.execute("UPDATE jobs SET last_fetched_at=? WHERE job_uid=?", (stamp(), job_uid))
            existing = conn.execute(
                "SELECT id FROM course_job_snapshots WHERE job_uid=? AND fingerprint=?",
                (job_uid, fingerprint),
            ).fetchone()
            if existing:
                return existing["id"]
            counter = conn.execute("SELECT value FROM kv_store WHERE key='course_snapshot_sequence'").fetchone()
            sequence = max(int(counter[0]) if counter else 0,
                           conn.execute("SELECT COALESCE(MAX(id),0) FROM course_job_snapshots").fetchone()[0]) + 1
            conn.execute("INSERT OR REPLACE INTO kv_store(key,value) VALUES('course_snapshot_sequence',?)", (str(sequence),))
            return self._insert_on(conn, "job_snapshots", dict(
                id=sequence, **values, fingerprint=fingerprint, created_at=stamp(),
            ))

    def save_match(self, resume_version_id: int, snapshot_id: int, data: dict,
                   preference_id: int | None = None, task_id: int | None = None) -> int:
        if task_id is not None:
            existing = self.list("matches", task_id=task_id)
            if existing:
                return existing[0]["id"]
        snapshot = self.get("job_snapshots", snapshot_id)
        if not snapshot or not snapshot["is_full"]:
            raise ValueError("需要完整 JD 才能保存详细匹配")
        if preference_id is not None:
            preference = self.get("preferences", preference_id)
            if preference is None:
                raise ValueError("求职偏好与简历确认版本不一致")
        return self._insert("matches", dict(
            resume_version_id=resume_version_id, snapshot_id=snapshot_id,
            preference_id=preference_id, data_json=encode(data), task_id=task_id,
            created_at=stamp(),
        ))

    def save_material(self, match_id: int, data: dict, parent_id: int | None = None,
                      task_id: int | None = None) -> int:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if task_id is not None:
                existing = conn.execute("SELECT id FROM course_materials WHERE task_id=?", (task_id,)).fetchone()
                if existing:
                    return existing["id"]
            if parent_id is not None:
                parent = conn.execute("SELECT match_id FROM course_materials WHERE id=?", (parent_id,)).fetchone()
                if parent is None or parent["match_id"] != match_id:
                    raise ValueError("材料父版本与匹配记录不一致")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM course_materials WHERE match_id=?",
                (match_id,),
            ).fetchone()[0]
            return self._insert_on(conn, "materials", dict(
                match_id=match_id, version=version, data_json=encode(data),
                parent_id=parent_id, task_id=task_id, created_at=stamp(),
            ))

    def create_application(self, material_id: int, status: str = "saved") -> int:
        material = self.get("materials", material_id)
        if material is None:
            raise ValueError("材料不存在")
        match = self.get("matches", material["match_id"])
        return self.application_for_job(match["snapshot_id"], status, material_id=material_id)

    def application_for_job(self, snapshot_id, status="saved", material_id=None, note="", event_date=""):
        if status not in APPLICATION_STATUSES:
            raise ValueError("未知申请状态")
        from datetime import date
        event_date = event_date or stamp()[:10]
        date.fromisoformat(event_date)
        snapshot = self.get("job_snapshots", snapshot_id)
        if snapshot is None:
            raise ValueError("岗位不存在")
        version_id = None
        if material_id:
            material = self.get("materials", material_id)
            match = self.get("matches", material["match_id"]) if material else None
            if not match or match["snapshot_id"] != snapshot_id:
                raise ValueError("材料与岗位依据不一致")
            version_id = match["resume_version_id"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id FROM applications WHERE job_uid=? ORDER BY id LIMIT 1", (snapshot["job_uid"],)).fetchone()
            if existing:
                return existing["id"]
            app_id = self._insert_on(conn, "applications", dict(
                job_uid=snapshot["job_uid"], title=snapshot["title"], company=snapshot["company"],
                applied_date=event_date if status == "applied" else "",
                channel=snapshot["source"], resume_version=str(version_id or ""), status=status,
                course_snapshot_id=snapshot_id, course_resume_version_id=version_id,
                course_material_id=material_id, created_at=stamp(), updated_at=stamp()))
            self._insert_on(conn, "application_events", dict(application_id=app_id, status=status,
                note=note or "用户建立申请记录", event_date=event_date, created_at=stamp(), basis_json=encode({
                    "snapshot_id": snapshot_id, "resume_version_id": version_id, "material_id": material_id,
                    "usage": "actual" if status in {"applied", "interview", "offer", "rejected"} else "planned"})))
            basis = {"snapshot_id": snapshot_id, "resume_version_id": version_id, "material_id": material_id}
            column = "course_actual_json" if status == "applied" else "course_planned_json"
            conn.execute(f"UPDATE applications SET {column}=? WHERE id=?", (encode(basis), app_id))
            return app_id

    def prepare(self, snapshot_id, resume_id, preference_id):
        versions = self.list("resume_versions", resume_id=resume_id) if resume_id else []
        version_id = versions[0]["id"] if versions else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            original = conn.execute("SELECT * FROM course_job_snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if original is None:
                raise ValueError("岗位不存在")
            cached = conn.execute("SELECT id FROM course_job_snapshots WHERE job_uid=? AND is_full=1 ORDER BY id DESC LIMIT 1",
                                  (original['job_uid'],)).fetchone() if not original['is_full'] else None
            selected_id = cached['id'] if cached else snapshot_id
            row = conn.execute("SELECT id FROM course_preparations WHERE original_snapshot_id=? AND snapshot_id=? "
                "AND resume_id IS ? AND resume_version_id IS ? AND preference_id IS ? ORDER BY id DESC LIMIT 1",
                (snapshot_id, selected_id, resume_id, version_id, preference_id)).fetchone()
            if row:
                return row["id"]
            return self._insert_on(conn, "preparations", dict(original_snapshot_id=snapshot_id,
                snapshot_id=selected_id, resume_id=resume_id, resume_version_id=version_id,
                preference_id=preference_id, created_at=stamp(), updated_at=stamp()))

    def update_preparation(self, preparation_id, **values):
        allowed = {"snapshot_id", "resume_id", "resume_version_id", "preference_id", "match_id", "material_id", "state", "error"}
        if not set(values) <= allowed:
            raise ValueError("未知准备字段")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM course_preparations WHERE id=?", (preparation_id,)).fetchone()
            for key in ("resume_version_id", "preference_id"):
                if key in values and values[key] != old[key] and (key == "preference_id" or old[key] is not None):
                    raise ValueError("已固定的依据不能替换，请重新分析")
            if "snapshot_id" in values and values["snapshot_id"] != old["snapshot_id"]:
                snapshot = conn.execute("SELECT * FROM course_job_snapshots WHERE id=?", (old["snapshot_id"],)).fetchone()
                new = conn.execute("SELECT * FROM course_job_snapshots WHERE id=?", (values["snapshot_id"],)).fetchone()
                if snapshot["is_full"] or not new or snapshot["job_uid"] != new["job_uid"]:
                    raise ValueError("已固定的 JD 不能替换")
            values["updated_at"] = stamp()
            conn.execute("UPDATE course_preparations SET " + ",".join(k+"=?" for k in values) + " WHERE id=?", (*values.values(), preparation_id))

    def transition_application(self, application_id: int, status: str, note: str = "",
                               confirmed: bool = False, event_date: str = "") -> None:
        from datetime import date
        if status not in APPLICATION_STATUSES:
            raise ValueError("未知申请状态")
        event_date = event_date or stamp()[:10]
        date.fromisoformat(event_date)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            application = conn.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
            if application is None:
                raise ValueError("申请不存在")
            last = conn.execute("SELECT status,note,event_date FROM course_application_events WHERE application_id=? ORDER BY id DESC LIMIT 1", (application_id,)).fetchone()
            if last and last["status"] == status and last["note"] == note and last["event_date"] == event_date:
                return
            applied_date = application["applied_date"]
            if status == "applied":
                applied_date = event_date
            conn.execute(
                "UPDATE applications SET status=?, applied_date=?, updated_at=? WHERE id=?",
                (status, applied_date, stamp(), application_id),
            )
            self._insert_on(conn, "application_events", dict(
                application_id=application_id, status=status, note=note, event_date=event_date, created_at=stamp(),
                basis_json=encode({**json.loads(application["course_actual_json"] if applied_date else application["course_planned_json"]),
                    "usage": "actual" if applied_date else "planned"}),
            ))

    def enqueue(self, kind: str, payload: dict, idempotency_key: str,
                max_attempts: int = 3, side_effect: bool = False, checkpoint=None, result=None, resume_preparation=None) -> int:
        if kind == "fill":
            raise ValueError("v2 已停用自动填写任务")
        if not kind or not idempotency_key or not 1 <= max_attempts <= 5:
            raise ValueError("任务类型、幂等键和 1–5 次尝试次数必须有效")
        payload_json = encode(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM course_tasks WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if (existing["kind"] != kind or existing["payload_json"] != payload_json
                        or existing["side_effect"] != int(side_effect)
                        or existing["max_attempts"] != max_attempts):
                    raise ValueError("同一幂等键不能对应不同任务")
                return existing["id"]
            if resume_preparation is not None:
                changed = conn.execute("UPDATE course_preparations SET state='queued',error='',updated_at=? "
                    "WHERE id=? AND state IN ('waiting_facts','waiting_jd','failed','problem_draft')",
                    (stamp(), resume_preparation)).rowcount
                if not changed:
                    raise ValueError("当前请求已在处理或完成，无需重复继续")
            return self._insert_on(conn, "tasks", dict(
                kind=kind, payload_json=payload_json, idempotency_key=idempotency_key,
                checkpoint_json=encode(checkpoint) if checkpoint is not None else None,
                result_json=encode(result) if result is not None else None,
                max_attempts=max_attempts, side_effect=int(side_effect),
                available_at=stamp(), created_at=stamp(), updated_at=stamp(),
            ))

    def record_email_attempt(self, task_id: int, status: str, detail: str = "") -> int:
        return self._insert("email_attempts", dict(
            task_id=task_id, status=status, detail=detail, created_at=stamp(),
        ))

    def retry(self, task_id: int) -> int:
        """An explicit user retry creates a new attempt without rewriting history."""
        task = self.get("tasks", task_id)
        if task and task['kind'] == 'email' and self.list('email_attempts', task_id=task_id, status='accepted'):
            raise ValueError('SMTP 已有服务器接受记录，不能重复重试。')
        preview = (task is not None and task["kind"] == "email"
                   and task["status"] == "succeeded"
                   and isinstance(task["result"], dict)
                   and task["result"].get("status") == "preview")
        partial = task is not None and task["kind"] == "search" and any(
            r.get("status") == "failed" for r in (task.get("result") or {}).get("sources", {}).values())
        if task is None or (task["status"] not in {"failed", "uncertain"} and not preview and not partial):
            raise ValueError("只有失败、结果不确定或未发送的邮件预览可手动重试")
        checkpoint = dict(task.get("checkpoint") or {})
        if task["kind"] == "materials" and (task.get("result") or {}).get("material_id"):
            checkpoint["parent_id"] = task["result"]["material_id"]
        return self.enqueue(task["kind"], task["payload"], f"retry:{task_id}",
            max_attempts=task["max_attempts"], side_effect=bool(task["side_effect"]),
            checkpoint=checkpoint or None,
            result=task.get("result") if task["kind"] == "search" else None)

    def link_application_basis(self, application_id, version_id, material_id, note, usage=None):
        app = self.get('applications', application_id)
        if not app: raise ValueError('申请不存在')
        snapshot_id = app['course_snapshot_id']
        if version_id and not self.get('resume_versions', version_id): raise ValueError('事实版本不存在')
        if material_id:
            material = self.get('materials', material_id)
            match = self.get('matches', material['match_id']) if material else None
            snapshot = self.get('job_snapshots', match['snapshot_id']) if match else None
            if not snapshot or snapshot['job_uid'] != app['job_uid']:
                raise ValueError('材料与申请岗位不一致')
            if version_id and match['resume_version_id'] != version_id:
                raise ValueError('材料与事实版本不一致')
            version_id = match['resume_version_id']
            snapshot_id = match['snapshot_id']
        usage = usage or ('actual' if app['applied_date'] else 'planned')
        if usage not in {'planned', 'actual'}: raise ValueError('未知资料用途')
        basis = {'resume_version_id': version_id, 'material_id': material_id,
                 'snapshot_id': snapshot_id, 'usage': usage}
        events = self.list('application_events', application_id=application_id)
        if events and events[0].get('basis') == basis and events[0]['note'] == note: return
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._insert_on(c, 'application_events', dict(application_id=application_id, status=app['status'],
                note='关联修改前的依据', created_at=stamp(), basis_json=encode({**app[f'course_{usage}'], 'usage': usage})))
            c.execute(f'UPDATE applications SET course_{usage}_json=? WHERE id=?', (encode(basis), application_id))
            if usage == 'actual' or not app['course_actual']:
                c.execute('UPDATE applications SET course_resume_version_id=?,course_material_id=?,course_snapshot_id=?,updated_at=? WHERE id=?',
                          (version_id,material_id,snapshot_id,stamp(),application_id))
            self._insert_on(c, 'application_events', dict(application_id=application_id,status=app['status'],note=note,
                created_at=stamp(),basis_json=encode(basis)))
