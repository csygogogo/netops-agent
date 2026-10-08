"""技能按元数据、SKILL.md、参考文件和可信脚本渐进加载。"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import yaml

from .budget import clip


class Skills:
    """按元数据、正文、资源三个层级加载技能。"""
    def __init__(self, config):
        self.config = config
        self.root = config.directory.resolve()
        self.catalog: dict[str, dict] = {}  # 仅保存技能名称、描述和目录
        self.errors: list[dict] = []
        self.refresh()

    def refresh(self):
        """扫描技能的 YAML 头部，不提前读取正文和参考资料。"""
        self.catalog.clear()
        self.errors.clear()
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir() or not (folder / "SKILL.md").is_file():
                continue
            try:
                path = (folder / "SKILL.md").resolve()
                if not path.is_relative_to(self.root):
                    raise ValueError("Skill 路径越界")
                # 发现技能时只读 YAML 头部，正文和参考资料暂不加载。
                with path.open(encoding="utf-8-sig") as stream:
                    if stream.readline().strip() != "---":
                        raise ValueError("缺少 YAML frontmatter")
                    header = ""
                    for line in stream:
                        if line.strip() == "---":
                            break
                        header += line
                        if len(header) > 16384:
                            raise ValueError("frontmatter 过长")
                    else:
                        raise ValueError("frontmatter 未结束")
                meta = yaml.safe_load(header)
                name, description = meta["name"], meta["description"]
                if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64 or name != folder.name:
                    raise ValueError("name 应与目录同名，使用小写字母/数字/单连字符，最多 64 字符")
                if not isinstance(description, str) or not 1 <= len(description) <= 1024:
                    raise ValueError("description 长度需要 1..1024 字符")
                self.catalog[name] = {"name": name, "description": description, "directory": folder.resolve()}
            except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
                self.errors.append({"name": folder.name, "error": str(exc)})

    def list(self, query: str = "", offset: int = 0, limit: int = 10):
        """筛选技能元数据并分页返回。"""
        items = [{"name": m["name"], "description": m["description"]} for m in self.catalog.values()
                 if query.lower() in (m["name"] + m["description"]).lower()]
        return {"skills": items[offset:offset + limit], "total": len(items), "next_offset": min(offset + limit, len(items))}

    def path(self, name: str, relative: str) -> Path:
        """解析技能相对路径，拒绝目录外的文件。"""
        if name not in self.catalog:
            raise ValueError(f"未知 skill: {name}")
        root = self.catalog[name]["directory"]
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("文件不存在或超出 skill 目录")
        return path

    def load(self, name: str) -> dict:
        """读取 SKILL.md 和资源文件名，正文之外的内容仍按需加载。"""
        path = self.path(name, "SKILL.md")
        text = path.read_text(encoding="utf-8-sig")
        root = path.parent
        files = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))
                 if p.is_file() and p.resolve().is_relative_to(root) and "__pycache__" not in p.parts][:100]
        return {"name": name, "text": text, "files": files}

    def read(self, name: str, path: str, offset: int = 0, limit: int = 3000) -> dict:
        """按字符分页读取技能资源。"""
        target = self.path(name, path)
        if offset < 0 or not 1 <= limit <= 16000:
            raise ValueError("offset >= 0；limit=1..16000")
        with target.open(encoding="utf-8-sig") as stream:
            remaining = offset
            while remaining > 0:
                chunk = stream.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            content = stream.read(limit)
            eof = not stream.read(1)
        return {"name": name, "path": path, "offset": offset, "text": content,
                "next_offset": offset + len(content), "eof": eof}

    def instructions(self, loaded: list[str]) -> str:
        """将当前激活技能的有限指令片段注入上下文。"""
        parts = []
        for name in loaded[-self.config.max_active:]:
            if name in self.catalog:
                page = self.read(name, "SKILL.md", limit=self.config.instruction_tokens)
                text = clip(page["text"], self.config.instruction_tokens)
                note = ""
                if not page["eof"] or text != page["text"]:
                    note = "\n[这里只是指令摘录；完整正文见 skill.load 结果，必要时使用 skill.read 读取 SKILL.md。]"
                parts.append(f"[active skill: {name}]\n" + text + note)
        return "\n\n".join(parts)

    async def run(self, name: str, script: str, args: list[str], output_path: Path) -> dict:
        """直接将输出写入会话文件，不限制输出长度；只限制执行时间。"""
        if not self.config.allow_scripts:
            raise ValueError("Skill 脚本执行未启用；管理员可设置 skills.allow_scripts=true")
        target = self.path(name, script)
        script_root = (self.catalog[name]["directory"] / "scripts").resolve()
        if not target.is_relative_to(script_root) or target.suffix != ".py":
            raise ValueError("仅允许执行该 skill 的 scripts/ 下的 Python 文件")
        if len(args) > 50 or any(len(arg) > 4096 for arg in args):
            raise ValueError("脚本参数过长")
        # 继承后端进程的本机环境，不通过 shell；空配置沿用当前解释器。
        python = self.config.python_path or sys.executable
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 让操作系统把 stdout/stderr 直接写入文件，避免整份输出进入内存。
        with output_path.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                python, "-u", str(target), *args, cwd=str(self.catalog[name]["directory"]),
                env=env, stdout=output, stderr=asyncio.subprocess.STDOUT,
            )
            status = "completed"
            try:
                async with asyncio.timeout(self.config.script_timeout_seconds):
                    await process.wait()
            except TimeoutError:
                status = "timeout"
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        return {"ok": status == "completed" and process.returncode == 0, "status": status,
                "exit_code": process.returncode}
