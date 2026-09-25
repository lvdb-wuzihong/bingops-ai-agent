"""运行时技能加载器单测（fail-open、白名单收窄上界、prompt 预算）。"""

from __future__ import annotations

from pathlib import Path

from inspection_agent.bot.skillregistry import load_skills
from inspection_agent.config import BotSkillsConfig

ALLOWED = {"list_apps", "get_detail", "prom__query_range"}


def _make_skill(
    root: Path, name: str, *, tools: list[str] | None = None, prompt: str = "方法论文档"
) -> None:
    d = root / name
    d.mkdir(parents=True)
    lines = [f"name: {name}", f"description: {name} 技能"]
    if tools is not None:
        lines.append("tools:")
        lines.extend(f"  - {t}" for t in tools)
    (d / "skill.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "SKILL.md").write_text(prompt, encoding="utf-8")


def _cfg(root: Path, **kw) -> BotSkillsConfig:
    return BotSkillsConfig(enabled=True, dir=str(root), **kw)


def test_load_skills_and_narrow(tmp_path):
    _make_skill(tmp_path, "a-skill", tools=["list_apps"])
    _make_skill(tmp_path, "b-skill")  # 不声明 tools：仅贡献 prompt，不参与收窄
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert [s.name for s in reg.skills] == ["a-skill", "b-skill"]
    assert reg.tool_filter == frozenset({"list_apps"})
    assert "## 技能指引" in reg.system_prompt
    assert "### a-skill" in reg.system_prompt
    assert "b-skill" in reg.system_prompt


def test_out_of_whitelist_tool_dropped(tmp_path):
    _make_skill(tmp_path, "s", tools=["list_apps", "drop_table"])
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert reg.tool_filter == frozenset({"list_apps"})  # 白名单外声明丢弃，永不扩写


def test_server_prefixed_tool_kept(tmp_path):
    _make_skill(tmp_path, "s", tools=["prom__query_range"])
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert reg.tool_filter == frozenset({"prom__query_range"})


def test_no_declaration_no_narrow(tmp_path):
    _make_skill(tmp_path, "s1")
    _make_skill(tmp_path, "s2")
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert reg.tool_filter is None  # 无人声明 → 全白名单（与无技能一致）


def test_empty_declaration_union_falls_back(tmp_path):
    # 声明全部被白名单丢弃 → 并集为空 → 回退全白名单（fail-open，保持可用性）
    _make_skill(tmp_path, "s", tools=["evil"])
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert reg.tool_filter is None


def test_missing_dir_fail_open(tmp_path):
    reg = load_skills(_cfg(tmp_path / "nope"), ALLOWED)
    assert reg.skills == ()
    assert reg.system_prompt == ""
    assert reg.tool_filter is None


def test_disabled_is_noop(tmp_path):
    _make_skill(tmp_path, "s")
    reg = load_skills(BotSkillsConfig(enabled=False, dir=str(tmp_path)), ALLOWED)
    assert reg.skills == () and reg.tool_filter is None


def test_per_skill_budget_truncates(tmp_path):
    _make_skill(tmp_path, "s", prompt="长" * 100)
    reg = load_skills(_cfg(tmp_path, per_skill_max_chars=50), ALLOWED)
    assert len(reg.skills) == 1
    assert reg.skills[0].prompt.startswith("长" * 50)
    assert "已按预算截断" in reg.skills[0].prompt


def test_total_budget_skips_remaining(tmp_path):
    _make_skill(tmp_path, "s1", prompt="甲" * 40)
    _make_skill(tmp_path, "s2", prompt="乙" * 40)
    reg = load_skills(_cfg(tmp_path, prompt_budget_chars=50), ALLOWED)
    assert [s.name for s in reg.skills] == ["s1"]


def test_duplicate_name_second_dropped(tmp_path):
    _make_skill(tmp_path, "dup", prompt="第一份")
    dup2 = tmp_path / "dup2"
    dup2.mkdir()
    (dup2 / "skill.yaml").write_text("name: dup\ndescription: x\n", encoding="utf-8")
    (dup2 / "SKILL.md").write_text("第二份", encoding="utf-8")
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert len(reg.skills) == 1 and reg.skills[0].prompt == "第一份"


def test_missing_prompt_skipped(tmp_path):
    d = tmp_path / "only-meta"
    d.mkdir()
    (d / "skill.yaml").write_text("name: only-meta\ndescription: x\n", encoding="utf-8")
    _make_skill(tmp_path, "good")
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert [s.name for s in reg.skills] == ["good"]


def test_broken_meta_skipped(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "skill.yaml").write_text("::: not yaml [", encoding="utf-8")
    _make_skill(tmp_path, "good")
    reg = load_skills(_cfg(tmp_path), ALLOWED)
    assert [s.name for s in reg.skills] == ["good"]
