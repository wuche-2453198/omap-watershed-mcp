#!/usr/bin/env python3
"""阶段 1 验证：DSH Cordis patch 组合 + 真实插件 schema 校验（隔离 DSH_HOME）。

验证对象：``ovital_mcp/dsh/ovital.cordis.yml``（Teammate C 交付）。

本脚本回答的问题：
1. 本机 DSH（0.1.5-rc.1）能否接受该 patch，把它作为**最后一层**叠加进 headless
   profile 组合树（``--dump-config`` 成功、行内容正确）；
2. 对照实验：不带 ``--patch`` 时组合树里**没有** ``mcp-ovital`` 行 —— 证明是 patch
   加进去的，而不是 DSH 自带；
3. 该行 ``config`` 是否通过**已安装的** ``@deepseek-ai/dsh-mcp-client`` 0.1.5-rc.1
   的 schemastery schema 校验（并用负向控制证明校验器真的会拒绝坏配置）；
4. patch 中**没有**明文 PSK / 凭据类 env 名 / 16 位 md5 派生值；
5. 全程使用隔离 ``DSH_HOME``（默认 ``<repo>/.dsh-home-verify``），不写用户真实
   ``~/.dsh``。

诚实边界（写入证据 limitations，不得省略）：
- ``--dump-config`` 只证明 **loader 组合**通过，不证明插件启动成功、不断言工具被发现；
- patch 里启用的是 Windows 命令占位路径（``C:/work/...``），在 macOS 上**不可能
  实际启动**；真实 Windows 工具发现仍属 ``real_device_pending``；
- schema 校验只证明字段/类型被插件接受，不证明子进程可启动。

用法::

    .venv/bin/python scripts/verify_dsh_patch.py
    .venv/bin/python scripts/verify_dsh_patch.py --dsh /opt/homebrew/bin/dsh

退出码：0 = 全部通过 / 有不影响结论的 SKIP；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_PATH = REPO_ROOT / "ovital_mcp" / "dsh" / "ovital.cordis.yml"
EVIDENCE_DIR = REPO_ROOT / ".cache" / "verify"
DEFAULT_DSH_HOME = REPO_ROOT / ".dsh-home-verify"
MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"
SECRET_ENV_NAME_RE = re.compile(r"(KEY|PASSWORD|SECRET|TOKEN|PSK)", re.IGNORECASE)
HEX16_RE = re.compile(r"\b[0-9A-Fa-f]{16}\b")


class Checklist:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def check(self, name: str, passed: bool, detail: Any = None, *, skip: bool = False) -> bool:
        if skip:
            self.items.append({"name": name, "passed": True, "skipped": True, "detail": detail})
            print(f"[SKIP] {name}  [{detail}]")
            return True
        self.items.append({"name": name, "passed": bool(passed), "detail": detail})
        mark = "PASS" if passed else "FAIL"
        suffix = "" if detail is None else f"  [{_short(detail)}]"
        print(f"[{mark}] {name}{suffix}")
        return bool(passed)

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [i for i in self.items if not i["passed"]]

    @property
    def skipped(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i.get("skipped")]


def _short(value: Any, limit: int = 360) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def load_patch_rows() -> list[dict[str, Any]]:
    data = yaml.safe_load(PATCH_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list), "patch 顶层必须是 YAML 数组"
    rows: list[dict[str, Any]] = []
    for entry in data:
        assert isinstance(entry, dict), f"patch 条目必须是映射：{entry!r}"
        for row in entry.get("insert") or []:
            assert isinstance(row, dict)
            rows.append(row)
    return rows


def locate_dsh_package(dsh_bin: Path) -> Path | None:
    """从 dsh CLI 真实路径推导其安装包目录（用于找内置 mcp-client 与 yaml 模块）。"""

    try:
        real = Path(os.path.realpath(dsh_bin))
    except OSError:
        return None
    # .../node_modules/@deepseek-ai/dsh/lib/bin.js -> .../node_modules/@deepseek-ai/dsh
    for parent in [real.parent, *real.parents]:
        if parent.name == "dsh" and parent.parent.name == "@deepseek-ai":
            return parent
    return None


def node_schema_check(checklist: Checklist, dsh_pkg: Path | None, config: dict[str, Any]) -> dict[str, Any]:
    """用已经安装的 dsh-mcp-client 的真实 schema 校验 patch 配置。"""

    facts: dict[str, Any] = {"performed": False}
    if shutil.which("node") is None:
        checklist.check("C1 用已安装 dsh-mcp-client 的真实 schema 校验 config", True,
                        "本机无 node，跳过（SKIP 不代表通过）", skip=True)
        return facts
    if dsh_pkg is None:
        checklist.check("C1 用已安装 dsh-mcp-client 的真实 schema 校验 config", True,
                        "未能定位 dsh 安装包目录，跳过", skip=True)
        return facts
    mcp_client = dsh_pkg / "node_modules" / "@deepseek-ai" / "dsh-mcp-client"
    yaml_mod = dsh_pkg / "node_modules" / "yaml"
    if not (mcp_client / "lib" / "index.js").is_file():
        checklist.check("C1 用已安装 dsh-mcp-client 的真实 schema 校验 config", True,
                        f"未找到 {mcp_client}，跳过", skip=True)
        return facts
    script = Path(os.environ.get("TMPDIR", "/tmp")) / f"dsh_schema_check_{os.getpid()}.mjs"
    script.write_text(
        """
import { readFileSync } from 'node:fs';
const [mcpClientPath, yamlPath, configJson] = process.argv.slice(2);
const { Config } = await import(mcpClientPath);
const cfg = JSON.parse(configJson);
let result;
try {
  const out = Config(cfg);
  result = { ok: true, normalized: { serverName: out.serverName, transport: out.transport,
             command: out.command, args: out.args, cwd: out.cwd,
             toolCallTimeoutMs: out.toolCallTimeoutMs, failOnStartupError: out.failOnStartupError,
             reconnect: out.reconnect } };
} catch (e) {
  result = { ok: false, error: String(e.message).split('\\n')[0] };
}
console.log(JSON.stringify(result));
""",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            ["node", str(script), str(mcp_client / "lib" / "index.js"), str(yaml_mod),
             json.dumps(config, ensure_ascii=False)],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        script.unlink(missing_ok=True)
    facts["performed"] = True
    facts["exit_code"] = proc.returncode
    if proc.returncode != 0:
        facts["stderr"] = proc.stderr[-800:]
        checklist.check("C1 用已安装 dsh-mcp-client 的真实 schema 校验 config", False, facts)
        return facts
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        facts["stdout"] = proc.stdout[-800:]
        checklist.check("C1 用已安装 dsh-mcp-client 的真实 schema 校验 config", False, facts)
        return facts
    facts["normalized"] = payload.get("normalized")
    checklist.check(
        "C1 patch config 通过已安装 dsh-mcp-client 0.1.5-rc.1 的真实 schema 校验",
        payload.get("ok") is True,
        {"ok": payload.get("ok"), "error": payload.get("error"), "normalized": payload.get("normalized")},
    )

    # 负向控制：证明该校验器确实会拒绝坏配置，而不是"什么都通过"。
    bad_config = dict(config)
    bad_config["transport"] = "carrier-pigeon"
    script.write_text(
        """
const [mcpClientPath, configJson] = process.argv.slice(2);
const { Config } = await import(mcpClientPath);
try { Config(JSON.parse(configJson)); console.log(JSON.stringify({ ok: true })); }
catch (e) { console.log(JSON.stringify({ ok: false, error: String(e.message).split('\\n')[0] })); }
""",
        encoding="utf-8",
    )
    try:
        proc_bad = subprocess.run(
            ["node", str(script), str(mcp_client / "lib" / "index.js"), json.dumps(bad_config)],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        script.unlink(missing_ok=True)
    bad_ok = False
    if proc_bad.returncode == 0:
        try:
            bad_ok = json.loads(proc_bad.stdout.strip().splitlines()[-1]).get("ok") is True
        except Exception:  # noqa: BLE001
            bad_ok = True
    facts["negative_control_rejected"] = not bad_ok
    checklist.check(
        "C2 负向控制：真实 schema 会拒绝非法 transport（证明 C1 不是空校验）",
        not bad_ok,
        {"bad_config_accepted": bad_ok},
    )
    return facts


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


def extract_block(dump: str, block_id: str) -> str | None:
    """从 dump-config 输出里截取一个插件行（顶格 `- id:` 到下一个顶格条目）。"""

    lines = dump.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(rf"^- id: {re.escape(block_id)}\s*$", line):
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("- ") or lines[index].startswith("# =="):
            end = index
            break
    return "\n".join(lines[start:end])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DSH Cordis patch 组合 + schema 校验")
    parser.add_argument("--dsh", default=os.environ.get("DSH_BIN", "dsh"))
    parser.add_argument("--dsh-home", default=str(DEFAULT_DSH_HOME))
    parser.add_argument("--evidence-dir", default=str(EVIDENCE_DIR))
    args = parser.parse_args(argv)

    checklist = Checklist()
    evidence_dir = Path(args.evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    dsh_home = Path(args.dsh_home).resolve()
    started = time.monotonic()
    evidence: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_root": str(REPO_ROOT),
        "patch_path": str(PATCH_PATH),
        "dsh_home_isolated": str(dsh_home),
        "real_user_dsh_home": os.path.expanduser("~/.dsh"),
        "limitations": [
            "--dump-config 只证明 loader 组合通过，不证明插件启动/工具发现成功",
            "patch 启用的 command 是 Windows 占位路径，macOS 上无法实际启动 MCP 子进程",
            "schema 校验只证明字段/类型被插件接受，不证明子进程可启动",
            "Windows 实机安装/工具发现仍为 real_device_pending",
        ],
    }

    dsh_bin = shutil.which(args.dsh) or (str(Path(args.dsh)) if Path(args.dsh).is_file() else None)
    if dsh_bin is None:
        print(f"[FAIL] 找不到 dsh 可执行文件：{args.dsh}", file=__import__("sys").stderr)
        return 1
    dsh_pkg = locate_dsh_package(Path(dsh_bin))
    evidence["dsh_bin"] = dsh_bin
    evidence["dsh_package"] = str(dsh_pkg) if dsh_pkg else None
    version = subprocess.run([dsh_bin, "--version"], capture_output=True, text=True, timeout=60)
    evidence["dsh_version"] = version.stdout.strip()
    checklist.check(
        "A1 本机 dsh CLI 可用且版本符合交付声明（0.1.5-rc.1）",
        version.returncode == 0 and version.stdout.strip() == "0.1.5-rc.1",
        {"version": version.stdout.strip(), "exit": version.returncode},
    )

    # --- patch 结构（不依赖 DSH）----------------------------------------
    try:
        rows = load_patch_rows()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] patch YAML 无法解析：{exc}")
        return 1
    mcp_rows = [r for r in rows if r.get("name") == MCP_CLIENT_PLUGIN]
    config = (mcp_rows[0].get("config") if mcp_rows else None) or {}
    evidence["patch_rows"] = len(rows)
    evidence["patch_mcp_rows"] = len(mcp_rows)
    checklist.check(
        "A2 patch 顶层为 YAML 数组，并 insert 了且仅插入一行 dsh-mcp-client",
        len(mcp_rows) == 1 and bool(config),
        {"rows": len(rows), "mcp_rows": len(mcp_rows)},
    )
    checklist.check(
        "A3 config 关键字段正确（serverName=ovital / transport=stdio / args / cwd / 超时 / failOnStartupError）",
        config.get("serverName") == "ovital"
        and config.get("transport") == "stdio"
        and config.get("args") == ["-m", "ovital_mcp"]
        and isinstance(config.get("cwd"), str)
        and config.get("toolCallTimeoutMs") == 60000
        and config.get("failOnStartupError") is False,
        {k: config.get(k) for k in ("serverName", "transport", "args", "cwd",
                                     "toolCallTimeoutMs", "failOnStartupError")},
    )
    patch_tree = yaml.safe_load(PATCH_PATH.read_text(encoding="utf-8"))
    tree_keys = _iter_keys(patch_tree)
    checklist.check(
        "A4 patch 结构中不含 mcpServers 键（DSH 用 Cordis patch，不是他客户端格式；注释文字不算）",
        "mcpServers" not in tree_keys,
        None,
    )
    env = config.get("env") or {}
    secret_env = [key for key in env if SECRET_ENV_NAME_RE.search(str(key))]
    dumped_config = json.dumps(config, ensure_ascii=False)
    hex_hits = HEX16_RE.findall(dumped_config)
    checklist.check(
        "A5 patch 无明文 PSK / 凭据类 env 名 / 16 位 md5 派生值",
        "psk" not in dumped_config.lower() and not secret_env and not hex_hits,
        {"secret_env_names": secret_env, "hex16_hits": hex_hits},
    )

    # --- 真实 dsh --dump-config 组合（隔离 DSH_HOME）---------------------
    dsh_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)
    # 不继承外部可能存在的 DSH 覆盖变量，保证验证可复现
    for key in ("DSH_PROFILE", "DSH_PATCH"):
        env.pop(key, None)
    real_home_state_before = _dir_state(Path(evidence["real_user_dsh_home"]))
    run_started = time.monotonic()
    proc = subprocess.run(
        [dsh_bin, "--profile", "headless", "--patch", str(PATCH_PATH), "--dump-config"],
        env=env, capture_output=True, text=True, timeout=180,
    )
    dump_elapsed = round(time.monotonic() - run_started, 2)
    real_home_state_after = _dir_state(Path(evidence["real_user_dsh_home"]))
    dump_path = evidence_dir / "dsh-dump-config.txt"
    dump_path.write_text(proc.stdout, encoding="utf-8")
    evidence["dump_config"] = {
        "exit_code": proc.returncode,
        "stderr_chars": len(proc.stderr),
        "stdout_lines": len(proc.stdout.splitlines()),
        "elapsed_s": dump_elapsed,
        "output_path": str(dump_path),
        "sha256": __import__("hashlib").sha256(proc.stdout.encode("utf-8")).hexdigest(),
    }
    checklist.check(
        "B1 隔离 DSH_HOME 下 `dsh --profile headless --patch <patch> --dump-config` 成功组合",
        proc.returncode == 0 and not proc.stderr.strip(),
        {"exit": proc.returncode, "stderr_tail": proc.stderr[-300:], "elapsed_s": dump_elapsed},
    )
    block = extract_block(proc.stdout, "mcp-ovital") or ""
    block_lines = block.splitlines()
    evidence["mcp_ovital_block"] = block
    checklist.check(
        "B2 dump 组合树中出现 mcp-ovital 行，且字段与 patch 一致",
        bool(block)
        and "name: '@deepseek-ai/dsh-mcp-client'" in block
        and "serverName: ovital" in block
        and "transport: stdio" in block
        and "- '-m'" in block
        and "- ovital_mcp" in block
        and "toolCallTimeoutMs: 60000" in block
        and "failOnStartupError: false" in block,
        {"block_lines": len(block_lines), "first": block_lines[:3]},
    )
    checklist.check(
        "B3 DSH_HOME 隔离生效：组合运行未修改用户真实 ~/.dsh",
        real_home_state_before == real_home_state_after,
        {"before": real_home_state_before, "after": real_home_state_after},
    )

    # --- 对照实验：不带 --patch 时不应出现该行 ---------------------------
    control = subprocess.run(
        [dsh_bin, "--profile", "headless", "--dump-config"],
        env=env, capture_output=True, text=True, timeout=180,
    )
    evidence["control"] = {
        "exit_code": control.returncode,
        "mcp_ovital_present": "- id: mcp-ovital" in control.stdout,
    }
    checklist.check(
        "B4 对照实验：不带 --patch 的组合树没有 mcp-ovital（证明由 patch 引入）",
        control.returncode == 0 and "- id: mcp-ovital" not in control.stdout,
        evidence["control"],
    )

    # --- 真实插件 schema 校验 -------------------------------------------
    evidence["plugin_schema_check"] = node_schema_check(checklist, dsh_pkg, config)

    # --- 证据落盘 --------------------------------------------------------
    evidence["elapsed_s"] = round(time.monotonic() - started, 2)
    report = {
        "evidence": evidence,
        "checks_total": len(checklist.items),
        "checks_passed": len(checklist.items) - len(checklist.failed),
        "checks_failed": len(checklist.failed),
        "checks_skipped": len(checklist.skipped),
        "checks": checklist.items,
        "passed": not checklist.failed,
    }
    stamp = time.strftime("%Y%m%dT%H%M%S")
    evidence_path = evidence_dir / f"dsh-patch-evidence-{stamp}.json"
    evidence_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (evidence_dir / "dsh-patch-evidence-latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"证据：{evidence_path}")
    print(f"dump：{dump_path}")
    print(
        f"结果：{report['checks_passed']}/{report['checks_total']} 通过，"
        f"{report['checks_failed']} 失败，{report['checks_skipped']} 跳过，用时 {evidence['elapsed_s']}s"
    )
    if checklist.failed:
        print("失败项：")
        for item in checklist.failed:
            print(f"  - {item['name']}  实际={_short(item['detail'])}")
        return 1
    print("阶段 1 DSH patch 验证：通过（组合 + schema；插件实际启动仍属 Windows 待验项）。")
    return 0


def _dir_state(path: Path) -> dict[str, Any]:
    """用户真实 ~/.dsh 的轻量状态快照（只看目录 mtime/大小，不读内容）。"""

    try:
        stat = path.stat()
    except OSError:
        return {"exists": False}
    return {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


if __name__ == "__main__":
    raise SystemExit(main())
