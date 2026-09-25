"""bot 运行时技能加载器（轻量 skill registry）。

技能 = 方法论 prompt 段（skills/<name>/SKILL.md）+ 元数据（skills/<name>/skill.yaml：
name/description/可选 tools 子集），启动时一次性加载、进程生命周期内复用：

- 白名单上界语义不变（SKILL 红线 2）：技能 tools 与 DEFAULT_ALLOWLIST（经
  effective_allowlist 收窄后）取交集，越界条目丢弃并告警，永不扩写；
- 未启用/目录缺失/单技能解析失败一律降级为跳过（fail-open），不阻断 bot 启动，
  行为与无技能时完全一致；
- prompt 预算（token 控制，红线 6）：单技能与总量双上限，超出截断/跳过并告警；
- 任一技能声明 tools 时，工具暴露面 = 各技能声明（∩白名单）的并集；声明并集
  为空（配置错误）时自然回退全白名单，保持可用性。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import BotSkillsConfig

logger = logging.getLogger(__name__)

_SKILL_META = "skill.yaml"
_SKILL_PROMPT = "SKILL.md"
_TRUNCATE_MARK = "\n…（方法论超长，已按预算截断）"


@dataclass(frozen=True)
class Skill:
    """单个运行时技能：prompt 段 + 收窄后生效的工具声明（裸名或 server__name）。"""

    name: str
    description: str
    prompt: str
    tools: frozenset[str]


@dataclass(frozen=True)
class SkillRegistry:
    """技能集合装配结果；tool_filter=None 表示不收窄（暴露全白名单）。"""

    skills: tuple[Skill, ...]
    system_prompt: str
    tool_filter: frozenset[str] | None


_EMPTY = SkillRegistry(skills=(), system_prompt="", tool_filter=None)


def load_skills(cfg: BotSkillsConfig, allowed_names: set[str]) -> SkillRegistry:
    """加载技能目录；allowed_names = 白名单裸名 ∪ server__name 全集（收窄上界）。"""
    if not cfg.enabled:
        return _EMPTY
    root = Path(cfg.dir)
    if not root.is_dir():
        logger.warning("bot 技能目录不存在（跳过技能加载，行为等同无技能）: %s", root)
        return _EMPTY

    skills: list[Skill] = []
    seen: set[str] = set()
    used_chars = 0
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        skill = _load_one(skill_dir, allowed_names, seen, cfg.per_skill_max_chars)
        if skill is None:
            continue
        if used_chars + len(skill.prompt) > cfg.prompt_budget_chars:
            logger.warning(
                "bot 技能 %s 超出 prompt 总预算 %d 字符（已跳过，token 控制）",
                skill.name, cfg.prompt_budget_chars,
            )
            continue
        seen.add(skill.name)
        used_chars += len(skill.prompt)
        skills.append(skill)

    return SkillRegistry(
        skills=tuple(skills),
        system_prompt=_assemble(skills),
        tool_filter=_tool_filter(skills),
    )


def _load_one(
    skill_dir: Path, allowed_names: set[str], seen: set[str], max_chars: int
) -> Skill | None:
    """加载单个技能；任何异常形态（缺文件/缺 name/重名）一律跳过并告警。"""
    try:
        meta = yaml.safe_load((skill_dir / _SKILL_META).read_text(encoding="utf-8"))
        prompt = (skill_dir / _SKILL_PROMPT).read_text(encoding="utf-8").strip()
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("bot 技能 %s 读取失败（跳过）: %s", skill_dir.name, exc)
        return None
    if not isinstance(meta, dict) or not str(meta.get("name", "")).strip():
        logger.warning("bot 技能 %s 缺少 name 元数据（跳过）", skill_dir.name)
        return None
    name = str(meta["name"]).strip()
    if name in seen:
        logger.warning("bot 技能重名 %s（后者跳过）: %s", name, skill_dir.name)
        return None
    if not prompt:
        logger.warning("bot 技能 %s 缺少 SKILL.md 正文（跳过）", name)
        return None
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars].rstrip() + _TRUNCATE_MARK
        logger.warning("bot 技能 %s 正文超长，已截断至 %d 字符", name, max_chars)
    declared = meta.get("tools")
    tools: frozenset[str] = frozenset()
    if isinstance(declared, list):
        kept: list[str] = []
        for tool in declared:
            tool_name = str(tool).strip()
            if not tool_name:
                continue
            if tool_name in allowed_names:
                kept.append(tool_name)
            else:
                logger.warning(
                    "bot 技能 %s 声明的工具不在白名单（丢弃，白名单永不扩写）: %s",
                    name, tool_name,
                )
        tools = frozenset(kept)
    elif declared is not None:
        logger.warning("bot 技能 %s 的 tools 必须为列表（忽略该字段）", name)
    return Skill(
        name=name,
        description=str(meta.get("description") or "").strip(),
        prompt=prompt,
        tools=tools,
    )


def _tool_filter(skills: list[Skill]) -> frozenset[str] | None:
    """工具暴露面 = 声明了 tools 的技能的并集；无人声明则不收窄（全白名单）。"""
    union: set[str] = set()
    for skill in skills:
        union.update(skill.tools)
    return frozenset(union) if union else None


def _assemble(skills: list[Skill]) -> str:
    """技能 prompt 段组装：追加在 BOT_SYSTEM_PROMPT 之后的基础红线之外内容。"""
    if not skills:
        return ""
    parts = ["", "## 技能指引（按数据域，仅约束方法，不改变基础红线）", ""]
    for skill in skills:
        title = f"### {skill.name}：{skill.description}".rstrip("：")
        parts.extend((title, skill.prompt, ""))
    return "\n".join(parts)
