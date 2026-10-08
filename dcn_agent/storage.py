"""SQLite 保存会话状态；每个会话的执行记录同时追加到一个日志文件。"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    """返回 UTC 时间，保证跨时区日志可排序。"""
    return datetime.now(timezone.utc).isoformat()


def encode(value) -> str:
    """用紧凑的 UTF-8 JSON 表示消息和事件。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_session_id(session_id: str):
    """会话 ID 直接用于目录名，限制为 Windows/Linux 均可用的名称。"""
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", session_id) or session_id.endswith("."):
        raise ValueError("session_id 需为 1..128 个字母、数字或 _ . -，不能以点开头或结尾")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", session_id.split(".")[0], re.IGNORECASE):
        raise ValueError("session_id 不能使用 Windows 保留名称，如 CON、NUL、COM1")


class SessionBusy(Exception):
    """同一会话已有运行中的请求时抛出。"""
    pass


class Store:
    """管理消息、工作上下文、运行状态，以及按会话组织的文件。"""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "agent.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '[]',
                    loaded_skills TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_prompts (user_id TEXT PRIMARY KEY, prompt TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS session_id_portable ON sessions(id COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,id);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(session_id) WHERE status='running';
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id,seq)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    source TEXT NOT NULL, chars INTEGER NOT NULL, created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        """创建数据库事务，结束时提交或回滚并关闭连接。"""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def session_directory(self, session_id: str) -> Path:
        """直接使用原始会话 ID，便于人工定位日志和结果。"""
        validate_session_id(session_id)
        folder = self.path.parent / "sessions" / session_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def log(self, session_id: str, entry: dict):
        """每行写一条 JSON；同一会话的多轮执行用 run_id 区分。"""
        path = self.session_directory(session_id) / "session.log"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encode(entry) + "\n")

    def recover(self):
        # 单进程启动时，将上次未结束的运行标记为中断。
        """启动时恢复运行状态，不重放外部工具。"""
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))

    def session(self, session_id: str, user_id: str) -> dict:
        """按会话 ID 和用户归属读取持久化上下文。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if row is None:
            raise KeyError("会话不存在")
        result = dict(row)
        for key in ("context", "loaded_skills"):
            result[key] = json.loads(result[key])
        return result

    def ensure_session(self, session_id: str, user_id: str) -> dict:
        """首次使用调用方的 ID 时创建会话，后续请求读取同一会话。"""
        validate_session_id(session_id)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO sessions(id,user_id,created_at) VALUES(?,?,?) ON CONFLICT(id COLLATE BINARY) DO NOTHING",
                           (session_id, user_id, now()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("session_id 与已有会话仅大小写不同，请使用原来的完整 ID") from exc
        # 相同 ID 不允许被其他用户接管，也不修改已有提示词和历史。
        return self.session(session_id, user_id)

    def begin_run(self, session_id: str, user_id: str) -> str:
        """创建本轮 run_id，由数据库限制同会话并发。"""
        self.session(session_id, user_id)
        run_id = uuid4().hex
        try:
            with self.connect() as db:
                db.execute("INSERT INTO runs VALUES(?,?, 'running', ?,NULL)", (run_id, session_id, now()))
        except sqlite3.IntegrityError as exc:
            raise SessionBusy("该会话已有运行中的任务") from exc
        return run_id

    def finish_run(self, run_id: str, status: str):
        """更新尚未结束的运行状态。"""
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,finished_at=? WHERE id=? AND status='running'", (status, now(), run_id))

    def save_context(self, session_id: str, context: list, summary: str, loaded_skills: list):
        """保存下一轮使用的历史、摘要和已激活技能。"""
        with self.connect() as db:
            db.execute("UPDATE sessions SET context=?,summary=?,loaded_skills=? WHERE id=?",
                       (encode(context), summary, encode(loaded_skills), session_id))

    def message(self, session_id: str, run_id: str, role: str, content: str):
        """保存完整对话，并将同一条消息写入会话日志。"""
        timestamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO messages(session_id,run_id,role,content,created_at) VALUES(?,?,?,?,?)",
                       (session_id, run_id, role, content, timestamp))
        self.log(session_id, {"type": role + ".message", "session_id": session_id,
                             "run_id": run_id, "timestamp": timestamp, "data": {"text": content}})

    def history(self, session_id: str, user_id: str, after: int = 0, limit: int = 100):
        """按消息序号分页读取完整对话。"""
        self.session(session_id, user_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?", (session_id, after, limit))]

    def event(self, payload: dict):
        """保存与 SSE 输出相同的事件，方便查询和按日志排查。"""
        with self.connect() as db:
            db.execute("INSERT INTO events VALUES(?,?,?)", (payload["run_id"], payload["seq"], encode(payload)))
        self.log(payload["session_id"], payload)

    def run(self, run_id: str, user_id: str):
        """检查用户归属并读取运行状态。"""
        with self.connect() as db:
            row = db.execute("SELECT r.* FROM runs r JOIN sessions s ON r.session_id=s.id WHERE r.id=? AND s.user_id=?",
                             (run_id, user_id)).fetchone()
        if row is None:
            raise KeyError("运行记录不存在")
        return dict(row)

    def events(self, run_id: str, user_id: str, after: int, limit: int):
        """按 seq 分页读取本轮结构化事件。"""
        self.run(run_id, user_id)
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run_id, after, limit))]

    def user_prompt(self, user_id: str) -> str:
        """读取用户长期提示词，未设置时返回空字符串。"""
        with self.connect() as db:
            row = db.execute("SELECT prompt FROM user_prompts WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else ""

    def set_user_prompt(self, user_id: str, prompt: str):
        """新增或覆盖用户长期提示词。"""
        with self.connect() as db:
            db.execute("INSERT INTO user_prompts VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET prompt=excluded.prompt", (user_id, prompt))

    def set_session_prompt(self, session_id: str, user_id: str, prompt: str):
        """设置指定会话的提示词，下一轮生效。"""
        self.ensure_session(session_id, user_id)
        with self.connect() as db:
            db.execute("UPDATE sessions SET prompt=? WHERE id=?", (prompt, session_id))
