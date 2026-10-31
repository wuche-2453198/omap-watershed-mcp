"""DSH 接入交付物的结构化校验。

覆盖（任务 task-3 要求）：
- ``ovital_mcp/dsh/ovital.cordis.yml``：DSH Cordis patch 格式（顶层 YAML 数组 +
  ``insert`` 列表 + ``@deepseek-ai/dsh-mcp-client``），serverName/transport/超时/
  ``failOnStartupError``，以及禁止把 PSK 明文放进 ``env``/``config``。
- ``ovital_mcp/dsh/config.example.yaml``：能被 ``ovital_mcp.config.AppConfig`` 校验，
  凭据走 ``psk_file`` 占位而不写明文。
- ``ovital_mcp/dsh/README.md``：说明 ``--patch`` 启动方式与 ``mcp__ovital__`` 工具名。
- ``.dsh/skills/ovital-watershed/SKILL.md``：frontmatter ``name``/``description``，
  ``name`` 与目录名一致，且包含流程、红线与 ``docs/architecture.md`` §6 的全部工具名。

运行：``.venv/bin/python -m pytest tests/test_dsh_patch.py -q``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DSH_DIR = ROOT / "ovital_mcp" / "dsh"
PATCH_PATH = DSH_DIR / "ovital.cordis.yml"
CONFIG_PATH = DSH_DIR / "config.example.yaml"
DSH_README_PATH = DSH_DIR / "README.md"
SKILL_DIR = ROOT / ".dsh" / "skills" / "ovital-watershed"
SKILL_PATH = SKILL_DIR / "SKILL.md"
ARCHITECTURE_PATH = ROOT / "docs" / "architecture.md"

MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"

SECRET_ENV_NAME_RE = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN|PSK)", re.IGNORECASE)
# 手册中登录用 md5(psk).upper() 前 16 位；出现 16 位十六进制串要警惕把派生值写进配置。
HEX16_RE = re.compile(r"\b[0-9A-Fa-f]{16}\b")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(_read_text(path))


def _insert_configs() -> list[dict[str, Any]]:
    """取出 patch 中所有 ``insert:`` 条目的插件行（dict 列表）。"""

    patch = _load_yaml(PATCH_PATH)
    assert isinstance(patch, list), "Cordis patch 顶层必须是 YAML 数组"
    rows: list[dict[str, Any]] = []
    for entry in patch:
        assert isinstance(entry, dict), f"patch 顶层元素必须是映射：{entry!r}"
        insert = entry.get("insert")
        if insert is None:
            continue
        assert isinstance(insert, list), "insert 必须是列表"
        for row in insert:
            assert isinstance(row, dict), f"insert 行必须是映射：{row!r}"
            rows.append(row)
    return rows


def _ovital_rows() -> list[dict[str, Any]]:
    return [
        row
        for row in _insert_configs()
        if row.get("name") == MCP_CLIENT_PLUGIN
        and isinstance(row.get("config"), dict)
        and row["config"].get("serverName") == "ovital"
    ]


# ---------------------------------------------------------------------------
# 文件存在性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [PATCH_PATH, CONFIG_PATH, DSH_README_PATH, SKILL_PATH, ARCHITECTURE_PATH],
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_deliverable_exists_and_is_utf8(path: Path) -> None:
    assert path.is_file(), f"缺少交付文件：{path}"
    assert path.stat().st_size > 0
    _read_text(path)  # 必须能按 UTF-8 解码


# ---------------------------------------------------------------------------
# Cordis patch 结构
# ---------------------------------------------------------------------------


def test_patch_top_level_is_list_with_insert() -> None:
    patch = _load_yaml(PATCH_PATH)
    assert isinstance(patch, list) and patch, "顶层必须是非空 YAML 数组"
    assert any(isinstance(entry, dict) and "insert" in entry for entry in patch), (
        "patch 必须通过 insert 列表新增插件行"
    )


def test_patch_has_exactly_one_ovital_mcp_client_row() -> None:
    rows = _ovital_rows()
    assert len(rows) == 1, f"应有且仅有一行 serverName=ovital 的 MCP 客户端，实际 {len(rows)}"


def _iter_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_iter_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_iter_keys(child))
    return keys


def test_patch_row_uses_official_mcp_client_plugin() -> None:
    row = _ovital_rows()[0]
    assert row.get("name") == MCP_CLIENT_PLUGIN
    assert isinstance(row.get("id"), str) and row["id"].strip(), "插件行必须有非空 id"
    patch = _load_yaml(PATCH_PATH)
    assert "mcpServers" not in _iter_keys(patch), (
        "不得套用其他客户端的 mcpServers 结构；DSH 使用 Cordis patch + dsh-mcp-client"
    )


def test_patch_config_transport_and_stdio_command() -> None:
    config = _ovital_rows()[0]["config"]
    assert config["serverName"] == "ovital"
    assert config["transport"] == "stdio"
    assert isinstance(config.get("command"), str) and config["command"].strip()
    assert config["command"].lower().endswith("python.exe") or config["command"].replace(
        "\\", "/"
    ).endswith("/python"), "command 必须指向部署环境的 Python 解释器（示例为 Windows .venv）"
    assert config.get("args") == ["-m", "ovital_mcp"], "args 必须为 ['-m', 'ovital_mcp']"
    assert isinstance(config.get("cwd"), str) and config["cwd"].strip(), "stdio 必须显式给 cwd"
    assert isinstance(config.get("env"), dict), "env 必须是映射（可为空，但不许缺省成非映射）"


def test_patch_config_timeout_and_fail_policy() -> None:
    config = _ovital_rows()[0]["config"]
    timeout = config.get("toolCallTimeoutMs")
    assert isinstance(timeout, int) and not isinstance(timeout, bool), "toolCallTimeoutMs 必须是整数"
    assert 5_000 <= timeout <= 120_000, (
        "toolCallTimeoutMs 应在合理范围（单次调用 < 5s 设计，超时只是安全上限）："
        f"{timeout}"
    )
    assert config.get("failOnStartupError") is False, (
        "failOnStartupError 必须显式为 false：无奥维/无凭据时不得阻断 DSH 启动"
    )
    reconnect = config.get("reconnect")
    if reconnect is not None:
        assert isinstance(reconnect, dict)
        assert reconnect.get("enabled") is True, "reconnect 若显式配置，应启用官方默认重连"


def test_patch_config_has_no_plaintext_psk() -> None:
    config = _ovital_rows()[0]["config"]
    dumped = json.dumps(config, ensure_ascii=False).lower()
    assert "psk" not in dumped, "PSK 不得出现在 DSH patch（含 env / config）"
    for key, value in (config.get("env") or {}).items():
        assert not SECRET_ENV_NAME_RE.search(str(key)), f"env 不得携带凭据变量名：{key}"
        assert "psk" not in str(value).lower(), f"env 值不得包含 PSK：{key}"
        assert not HEX16_RE.search(str(value)), f"env 值疑似含 md5(PSK) 派生值：{key}"
    # 允许显式传入非秘密的配置文件路径，避免依赖 cwd 猜测，也避免依赖环境继承。
    assert any(
        str(value).lower().endswith((".yaml", ".yml"))
        for value in (config.get("env") or {}).values()
    ), "应通过非秘密 env（如 OVITAL_MCP_CONFIG）指向配置文件"


def test_patch_contains_posix_alternative_commented() -> None:
    text = _read_text(PATCH_PATH)
    # 交付要求同时给 Windows 与 POSIX 两种写法；当前启用 Windows，POSIX 以注释保留。
    assert "Windows" in text
    assert "POSIX" in text
    assert re.search(r"#\s*command:\s*'?/\S*/bin/python", text), (
        "应保留 POSIX 注释示例（.venv/bin/python）"
    )


# ---------------------------------------------------------------------------
# 服务端配置样例
# ---------------------------------------------------------------------------


def test_config_example_sections_and_keys() -> None:
    config = _load_yaml(CONFIG_PATH)
    assert isinstance(config, dict)
    for section in ("ovital", "gis", "storage", "logging"):
        assert isinstance(config.get(section), dict), f"缺少配置节：{section}"
    ovital = config["ovital"]
    endpoint = ovital.get("endpoint")
    assert isinstance(endpoint, str) and endpoint.startswith(("ws://", "wss://"))
    psk_file = ovital.get("psk_file")
    assert isinstance(psk_file, str) and psk_file.strip(), "示例必须使用 psk_file 而不是明文 psk"
    assert ovital.get("psk") in (None, ""), "示例不得填写明文 psk"
    assert config["gis"].get("engine") in {"auto", "native", "grass"}
    assert isinstance(config["storage"].get("data_root"), str) and config["storage"]["data_root"]


def test_config_example_matches_implementation_schema() -> None:
    """示例必须能被真实配置模型接受（字段名/类型/枚举一致）。"""

    config_mod = pytest.importorskip(
        "ovital_mcp.config", reason="ovital_mcp/config.py 尚不可导入时跳过实现一致性校验"
    )
    raw = _load_yaml(CONFIG_PATH)
    settings = config_mod.AppConfig.model_validate(raw)
    assert settings.ovital.endpoint.startswith(("ws://", "wss://"))
    assert settings.ovital.psk_file, "实现层必须能从示例读到 psk_file 占位"
    assert settings.gis.engine in {"auto", "native", "grass"}


def test_config_example_has_no_secret_literals() -> None:
    text = _read_text(CONFIG_PATH)
    assert not HEX16_RE.search(text), "示例中不得出现 16 位十六进制密钥/派生值"
    assert re.search(r"psk_file:\s*['\"]?[A-Za-z0-9_./:\\ -]+['\"]?", text), (
        "应以占位路径演示 psk_file 用法"
    )


# ---------------------------------------------------------------------------
# DSH 接入说明
# ---------------------------------------------------------------------------


def test_dsh_readme_documents_patch_and_tool_names() -> None:
    text = _read_text(DSH_README_PATH)
    for needle in ("--patch", "ovital.cordis.yml", "mcp__ovital__", "@deepseek-ai/dsh-mcp-client", "--dump-config"):
        assert needle in text, f"README 缺少关键说明：{needle}"
    assert at_least_tools(text, ["get_capabilities", "start_watershed", "write_draft", "read_back"])


def at_least_tools(text: str, tools: list[str]) -> bool:
    return all(tool in text for tool in tools)


# ---------------------------------------------------------------------------
# Skill frontmatter 与内容
# ---------------------------------------------------------------------------


def _frontmatter(text: str) -> dict[str, Any]:
    assert text.startswith("---\n"), "SKILL.md 必须以 YAML frontmatter 开始"
    end = text.find("\n---", 4)
    assert end != -1, "frontmatter 未闭合"
    data = yaml.safe_load(text[4:end])
    assert isinstance(data, dict), "frontmatter 必须是 YAML 映射"
    return data


def test_skill_frontmatter_name_matches_directory() -> None:
    meta = _frontmatter(_read_text(SKILL_PATH))
    assert meta.get("name") == SKILL_DIR.name == "ovital-watershed"
    description = meta.get("description")
    assert isinstance(description, str) and len(description.strip()) >= 20, "description 必须有实质内容"
    for trigger in ("奥维", "集水区", "汇水区", "流域", "分水岭", "坝址", "取水口"):
        assert trigger in description, f"description 缺少触发词：{trigger}"


def test_skill_documents_required_workflow() -> None:
    text = _read_text(SKILL_PATH)
    required = [
        "读能力",
        "读出口",
        "检查 DEM",
        "起任务",
        "分段复核",
        "实际看图",
        "疑点",
        "修订",
        "写草稿",
        "回读",
        "汇报",
        "定稿",
        "回滚",
    ]
    for keyword in required:
        assert keyword in text, f"Skill 流程缺少关键环节：{keyword}"


def test_skill_contains_red_lines() -> None:
    text = _read_text(SKILL_PATH)
    red_line_checks = {
        "不得为凑目标面积修改边界": ("凑", "面积"),
        "不得把影像河谷当山脊": ("河谷", "山脊"),
        "不得跨鞍部纳入邻近流域": ("鞍部",),
        "无 DEM 降级策略": ("无 DEM", "降级"),
        "模拟与真实必须区分": ("模拟", "真实"),
        "不得伪造精度": ("伪造", "精度"),
        "看图必须真实发生": ("看图", "图片"),
    }
    for label, needles in red_line_checks.items():
        for needle in needles:
            assert needle in text, f"红线「{label}」缺少关键词：{needle}"
    assert "simulated" in text and "verified_against_real_device" in text
    assert "r.watershed" in text or "分水岭" in text


def _architecture_tool_names() -> list[str]:
    """从 docs/architecture.md §6 的工具契约表解析工具名。"""

    text = _read_text(ARCHITECTURE_PATH)
    section = text.split("## 6.", 1)[1].split("## 7.", 1)[0]
    names = re.findall(r"^\|\s*\d+\s*\|\s*`([a-z][a-z0-9_]*)`\s*\|", section, re.MULTILINE)
    return names


#: 最终工具清单（Lead 2026-10-30 确认）：architecture §6 契约 17 个 + list_jobs 辅助工具 = 18 个。
#: 若 Lead 后续增删工具，这里与 dsh README / SKILL.md 必须同步更新。
FINAL_TOOL_NAMES = [
    "get_capabilities",
    "get_selection",
    "read_object",
    "get_scene",
    "inspect_dem",
    "start_watershed",
    "get_job",
    "cancel_job",
    "list_jobs",
    "review_segment",
    "revise_candidate",
    "validate_geometry",
    "measure_geometry",
    "write_draft",
    "read_back",
    "finalize_draft",
    "rollback_change",
    "export_result",
]


def test_final_tool_count_is_18() -> None:
    assert len(FINAL_TOOL_NAMES) == 18
    assert len(set(FINAL_TOOL_NAMES)) == 18


def test_dsh_readme_lists_all_final_tools() -> None:
    text = _read_text(DSH_README_PATH)
    missing = [tool for tool in FINAL_TOOL_NAMES if f"mcp__ovital__{tool}" not in text]
    assert not missing, f"DSH README 未列出工具：{missing}"


def test_skill_covers_every_final_tool() -> None:
    text = _read_text(SKILL_PATH)
    missing = [tool for tool in FINAL_TOOL_NAMES if f"mcp__ovital__{tool}" not in text]
    assert not missing, f"Skill 未覆盖最终工具清单：{missing}"


def test_skill_covers_every_contract_tool() -> None:
    tools = _architecture_tool_names()
    assert len(tools) >= 10, f"architecture §6 工具表解析异常：{tools}"
    skill_text = _read_text(SKILL_PATH)
    missing = [name for name in tools if f"mcp__ovital__{name}" not in skill_text]
    assert not missing, f"Skill 未覆盖架构契约中的工具：{missing}"


def test_skill_tool_prefix_matches_server_name() -> None:
    config = _ovital_rows()[0]["config"]
    skill_text = _read_text(SKILL_PATH)
    assert f"mcp__{config['serverName']}__" in skill_text


def test_skill_mentions_mcp_image_requirement() -> None:
    text = _read_text(SKILL_PATH)
    # 任务书 D 节：仅返回文件路径不能代表模型已看到图片。
    assert "路径" in text and "MCP Image" in text
    assert "deepseek-flash" in text or "视觉模型" in text
