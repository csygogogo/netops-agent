"""完整结果按会话保存，通过字符分页和关键词检索处理超长单行日志。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from .budget import clip, tokens
from .storage import Store, encode, now


class Artifacts:
    """原始 JSON 用于复核，文本视图用于搜索；模型只接收预算内的片段。"""
    def __init__(self, store: Store):
        self.store = store

    def save(self, session_id: str, run_id: str, source: str, result) -> dict:
        """保存原文和检索文本，并在数据库登记会话归属。"""
        directory = self.store.session_directory(session_id) / "results"
        directory.mkdir(exist_ok=True)
        artifact_id = uuid4().hex
        raw = encode(result)
        text = result.get("text") if isinstance(result, dict) else None
        # 结构化字段也进入检索文本，JSON 文件始终保留完整对象。
        if isinstance(result, dict) and result.get("data") is not None:
            data = result["data"]
            duplicate = isinstance(data, dict) and data == {"result": text}
            if isinstance(text, str) and text.lstrip().startswith(("{", "[")):
                try:
                    duplicate = duplicate or json.loads(text) == data
                except ValueError:
                    pass
            if not duplicate:
                text = (text or "") + "\n[structuredContent]\n" + encode(data)
        if not isinstance(text, str) or not text:
            text = raw
        (directory / f"{artifact_id}.json").write_text(raw, encoding="utf-8")
        (directory / f"{artifact_id}.txt").write_text(text, encoding="utf-8", newline="")
        with self.store.connect() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                       (artifact_id, session_id, run_id, source, len(text), now()))
        return {"artifact_id": artifact_id, "chars": len(text)}

    def metadata(self, session_id: str, artifact_id: str) -> dict:
        """检查结果属于当前会话，并读取字符数等元数据。"""
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=? AND session_id=?", (artifact_id, session_id)).fetchone()
        if row is None:
            raise KeyError("结果文件不存在或不属于该会话")
        return dict(row)

    def finish_output(self, session_id: str, artifact_id: str, result: dict):
        """分块生成脚本原始结果 JSON，登记字符数，不把完整输出读入内存。"""
        path = self.raw_path(session_id, artifact_id)
        chars = 0
        with path.with_suffix(".txt").open(encoding="utf-8", errors="replace", newline="") as source:
            with path.open("w", encoding="utf-8", newline="") as target:
                target.write(encode(result)[:-1] + ',"text":"')
                while chunk := source.read(65536):
                    chars += len(chunk)
                    target.write(encode(chunk)[1:-1])
                target.write('"}')
        with self.store.connect() as db:
            db.execute("UPDATE artifacts SET chars=? WHERE id=? AND session_id=?", (chars, artifact_id, session_id))

    def raw_path(self, session_id: str, artifact_id: str) -> Path:
        """校验会话归属后返回原始 JSON 路径。"""
        self.metadata(session_id, artifact_id)
        return self.store.session_directory(session_id) / "results" / f"{artifact_id}.json"

    @staticmethod
    def _skip(stream, offset: int):
        """分块跳过字符，避免一次读取整个大文件。"""
        while offset > 0:
            chunk = stream.read(min(offset, 65536))
            if not chunk:
                break
            offset -= len(chunk)

    def read(self, session_id: str, artifact_id: str, offset: int = 0, limit: int = 2000) -> dict:
        """按字符位置读取有限片段，返回下一页位置。"""
        meta = self.metadata(session_id, artifact_id)
        if offset < 0 or not 1 <= limit <= 16000:
            raise ValueError("offset >= 0；limit 范围为 1..16000 个字符")
        offset = min(offset, meta["chars"])
        with (self.store.session_directory(session_id) / "results" / f"{artifact_id}.txt").open(encoding="utf-8", errors="replace", newline="") as stream:
            self._skip(stream, offset)
            text = stream.read(limit)
        return {"artifact_id": artifact_id, "offset": offset, "text": text,
                "next_offset": offset + len(text), "eof": offset + len(text) >= meta["chars"]}

    def search(self, session_id: str, artifact_id: str, terms: list[str], offset: int = 0,
               limit: int = 6, radius: int = 180) -> dict:
        """分块搜索关键词并保留边界重叠，单行日志也能命中。"""
        meta = self.metadata(session_id, artifact_id)
        if not terms or len(terms) > 20 or any(not isinstance(t, str) or not 1 <= len(t) <= 256 for t in terms):
            raise ValueError("terms 需要 1..20 个非空关键词，每个最多 256 字符")
        if offset < 0 or not 1 <= limit <= 20 or not 0 <= radius <= 1000:
            raise ValueError("offset >= 0；limit=1..20；radius=0..1000")
        # 对关键词做字面匹配，避免模型传入复杂正则。
        pattern = re.compile("|".join(re.escape(term) for term in sorted(set(terms), key=len, reverse=True)), re.IGNORECASE)
        overlap = 2 * radius + max(map(len, terms)) + 1  # 保留跨块关键词及其周围文字
        matches = []
        start = max(0, offset - radius)
        seen_until = offset
        with (self.store.session_directory(session_id) / "results" / f"{artifact_id}.txt").open(encoding="utf-8", errors="replace", newline="") as stream:
            self._skip(stream, start)
            carry = ""
            while True:
                chunk = stream.read(65536)
                text = carry + chunk
                eof = not chunk
                safe_end = len(text) if eof else max(0, len(text) - overlap)
                for match in pattern.finditer(text):
                    absolute = start + match.start()
                    if absolute < seen_until or match.start() >= safe_end:
                        continue
                    left = max(0, match.start() - radius)
                    right = min(len(text), match.end() + radius)
                    matches.append({"offset": start + left, "match_offset": absolute,
                                    "text": text[left:right]})
                    seen_until = start + match.end()
                    if len(matches) >= limit:
                        return {"artifact_id": artifact_id, "matches": matches,
                                "next_offset": seen_until, "eof": seen_until >= meta["chars"]}
                if eof:
                    return {"artifact_id": artifact_id, "matches": matches,
                            "next_offset": meta["chars"], "eof": True}
                carry = text[safe_end:]
                start += safe_end

    def pack(self, session_id: str, run_id: str, source: str, result, query: str, budget: int) -> dict:
        """保存完整结果，超出预算时只返回命中片段和原文引用。"""
        if source == "skill.run" and isinstance(result, dict) and result.get("artifact_id"):
            # 脚本已直接落盘；只有预算内的小文件才完整读取。
            saved = self.metadata(session_id, result["artifact_id"])
            meta = {"artifact_id": saved["id"], "chars": saved["chars"]}
            path = self.raw_path(session_id, saved["id"])
            if path.stat().st_size + 180 <= budget:
                return {**meta, "truncated": False, "result": json.loads(path.read_text(encoding="utf-8"))}
        else:
            meta = self.save(session_id, run_id, source, result)
            if tokens(encode(result)) + 180 <= budget:
                return {**meta, "truncated": False, "result": result}
        # 在整个文件中寻找相关片段，不依赖行数或只看开头。
        terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{2,80}", query)
        terms += ["ERROR", "CRITICAL", "failed", "timeout", "down", "异常", "丢包"]
        terms = list(dict.fromkeys(terms))[:20]
        found = self.search(session_id, meta["artifact_id"], terms, limit=4, radius=100)
        pieces = [f"[offset={m['offset']}] {m['text']}" for m in found["matches"]]
        head = self.read(session_id, meta["artifact_id"], 0, 160)["text"]
        tail_start = max(0, meta["chars"] - 160)
        tail = self.read(session_id, meta["artifact_id"], tail_start, 160)["text"]
        preview = "\n".join(pieces + [f"[head offset=0] {head}", f"[tail offset={tail_start}] {tail}"])
        packed = {**meta, "truncated": True, "ok": result.get("ok", True) if isinstance(result, dict) else True,
                  "preview": "", "hint": "artifact.search/read"}
        if source == "skill.run" and isinstance(result, dict):
            packed.update({key: result[key] for key in ("status", "exit_code") if key in result})
        packed["preview"] = clip(preview, max(0, budget - tokens(encode(packed)) - 64))
        # JSON 转义可能增加体积，必要时继续缩短预览。
        while tokens(encode(packed)) > budget and packed["preview"]:
            packed["preview"] = packed["preview"][:len(packed["preview"]) // 2]
        return packed
