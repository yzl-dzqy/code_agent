# -*- coding: utf-8 -*-
"""
Skill 系统：从多个 skills 目录加载 SKILL.md，注入 System Prompt。

SKILL.md 格式：
---
name: xxx
description: xxx
---
正文内容...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import CFG
from .tools.registry import tool
from .utils.log import log


@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path


@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str


class SkillRegistry:
    def __init__(self, skills_dir: Path | None = None):
        if skills_dir is not None:
            self.skills_dirs = [skills_dir]
        else:
            # 同时加载项目目录和包内目录；项目目录同名 skill 可覆盖内置 skill。
            self.skills_dirs = [CFG.pkg_dir / "skills", CFG.workdir / "skills"]
        self.documents: dict[str, SkillDocument] = {}
        self._load_all()

    def _load_all(self):
        for base_dir in self.skills_dirs:
            if not base_dir.exists():
                continue
            for path in sorted(base_dir.rglob("SKILL.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                meta, body = self._parse_frontmatter(text)
                name = meta.get("name", path.parent.name)
                desc = meta.get("description", "No description")
                manifest = SkillManifest(name=name, description=desc, path=path)
                self.documents[name] = SkillDocument(
                    manifest=manifest,
                    body=body.strip(),
                )
                log("INFO", "skill_loaded", f"{name}: {desc} ({path})")

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not m:
            return {}, text
        meta = {}
        for line in m.group(1).strip().splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
        return meta, m.group(2)

    def describe_available(self) -> str:
        """返回可用 skill 列表摘要，供 System Prompt 注入。"""
        if not self.documents:
            return ""
        lines = []
        for name in sorted(self.documents):
            mf = self.documents[name].manifest
            lines.append(f"- {mf.name}: {mf.description}")
        return "\n".join(lines)

    def load_full_text(self, name: str) -> str:
        """加载指定 skill 完整内容。"""
        doc = self.documents.get(name)
        if not doc:
            known = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: 未知 skill '{name}'。可用: {known}"
        return f"<skill name=\"{doc.manifest.name}\">\n{doc.body}\n</skill>"

    def list_skills(self) -> list[SkillManifest]:
        """返回所有已加载 skill 的 manifest 列表。"""
        return [doc.manifest for doc in self.documents.values()]

    def inject_prompt(self) -> str:
        """生成注入到 System Prompt 的 skill 上下文。"""
        desc = self.describe_available()
        if not desc:
            return ""
        return (
            "\n\n## 可用 Skills\n"
            "以下 skill 可通过 load_skill 工具加载完整内容：\n"
            f"{desc}\n"
            "需要某个 skill 的详细指引时，调用 load_skill 工具。"
        )


# 全局单例
SKILLS = SkillRegistry()


# 注册为工具
@tool(name="load_skill", description="加载指定 skill 的完整内容，获取专业领域的详细指引")
def load_skill(name: str) -> str:
    return SKILLS.load_full_text(name)
