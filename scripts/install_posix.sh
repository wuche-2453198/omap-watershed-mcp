#!/usr/bin/env bash
# OMAP Watershed MCP — POSIX（macOS/Linux）安装/检查脚本
#
# 用途：
#   - 用 conda-forge 创建/更新项目环境（environment.yml）
#   - 校验 Python 依赖与本仓库可导入性
#   - 生成服务配置样例副本、受限权限的密钥目录、以及按实际路径替换后的 DSH patch
#   - 打印 DSH 启动与验收步骤
#
# 安全：
#   - 本脚本不接收、不写入真实 PSK；密钥文件留空由用户在受限目录内自行填写。
#   - 不覆盖已有配置/patch；生成文件只写 .local 副本。
#
# 用法：
#   bash scripts/install_posix.sh --check-only                 # 只检查依赖，不写任何文件
#   bash scripts/install_posix.sh                             # 创建 conda 环境 + 生成配置
#   bash scripts/install_posix.sh --python .venv/bin/python --skip-env
#                                                              # 使用已有 venv（开发机）
#   bash scripts/install_posix.sh --help
#
# 退出码：0 成功；1 环境/依赖错误；2 参数错误。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_NAME="omap-mcp"
GRASS_ENV="omap-grass"
CONDA_EXE=""
PYTHON=""
SECRETS_DIR="${HOME}/.omap-mcp-secrets"
CHECK_ONLY=0
SKIP_ENV=0

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log() { printf '[install] %s\n' "$*"; }
warn() { printf '[install] 警告: %s\n' "$*" >&2; }
die() { printf '[install] 错误: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:?缺少参数值}"; shift 2 ;;
    --env-name) ENV_NAME="${2:?缺少参数值}"; shift 2 ;;
    --grass-env) GRASS_ENV="${2:?缺少参数值}"; shift 2 ;;
    --conda) CONDA_EXE="${2:?缺少参数值}"; shift 2 ;;
    --python) PYTHON="${2:?缺少参数值}"; shift 2 ;;
    --secrets-dir) SECRETS_DIR="${2:?缺少参数值}"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
    --skip-env) SKIP_ENV=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "未知参数：$1" ;;
  esac
done

REPO_ROOT="$(cd "${REPO_ROOT}" && pwd)" || die "仓库目录不存在：${REPO_ROOT}"

# ---------------------------------------------------------------------------
# 0. 基本文件检查
# ---------------------------------------------------------------------------
log "仓库目录：${REPO_ROOT}"
for required in environment.yml pyproject.toml \
  ovital_mcp/dsh/ovital.cordis.yml ovital_mcp/dsh/config.example.yaml; do
  [[ -f "${REPO_ROOT}/${required}" ]] || die "缺少文件：${REPO_ROOT}/${required}"
done
case "$(uname -s)" in
  Darwin) : ;;
  Linux) : ;;
  *) warn "非 macOS/Linux（$(uname -s)）；本脚本主要面向 POSIX，Windows 请用 scripts/install_windows.ps1" ;;
esac

# ---------------------------------------------------------------------------
# 1. 定位/创建 Python 环境
# ---------------------------------------------------------------------------
find_conda() {
  if [[ -n "${CONDA_EXE}" ]]; then
    [[ -x "${CONDA_EXE}" || -f "${CONDA_EXE}" ]] || die "指定的 conda 不存在：${CONDA_EXE}"
    return
  fi
  for candidate in conda micromamba mamba; do
    if command -v "${candidate}" >/dev/null 2>&1; then CONDA_EXE="$(command -v "${candidate}")"; return; fi
  done
  for candidate in "${HOME}/miniconda3/bin/conda" "${HOME}/miniforge3/bin/conda" \
    "${HOME}/mambaforge/bin/conda" "/opt/homebrew/Caskroom/miniconda/base/bin/conda"; do
    if [[ -x "${candidate}" ]]; then CONDA_EXE="${candidate}"; return; fi
  done
  CONDA_EXE=""
}

environment_exists() {
  "${CONDA_EXE}" env list 2>/dev/null | awk '{print $1}' | grep -qx "${ENV_NAME}"
}

if [[ "${SKIP_ENV}" -eq 0 ]]; then
  find_conda
  [[ -n "${CONDA_EXE}" ]] || die "未找到 conda/micromamba/mamba；可安装 Miniforge，或用 --python 指定已有环境并加 --skip-env"
  log "conda：${CONDA_EXE}"
  if [[ "${CHECK_ONLY}" -eq 0 ]]; then
    if environment_exists; then
      log "更新已有环境：${ENV_NAME}"
      "${CONDA_EXE}" env update -f "${REPO_ROOT}/environment.yml" -n "${ENV_NAME}" --prune
    else
      log "创建环境：${ENV_NAME}（首次解析可能较慢）"
      "${CONDA_EXE}" env create -f "${REPO_ROOT}/environment.yml" -n "${ENV_NAME}"
    fi
    PYTHON="$("${CONDA_EXE}" run -n "${ENV_NAME}" python -c 'import sys; print(sys.executable)')"
  fi
elif [[ -z "${PYTHON}" ]]; then
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON="${REPO_ROOT}/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || true)"
  fi
fi

if [[ -n "${PYTHON}" ]]; then
  [[ -x "${PYTHON}" ]] || die "Python 不可执行：${PYTHON}"
  log "Python：${PYTHON}（$("${PYTHON}" --version 2>&1)）"
elif [[ "${CHECK_ONLY}" -eq 1 ]]; then
  warn "未解析到 Python；仅做文件检查"
else
  die "未能确定 Python 解释器"
fi

# ---------------------------------------------------------------------------
# 2. 依赖与本仓库可导入性
# ---------------------------------------------------------------------------
if [[ -n "${PYTHON}" ]]; then
  log "检查依赖（rasterio/shapely/pyproj/mcp/websockets/matplotlib/pillow/yaml/pydantic）..."
  PYTHONPATH="${REPO_ROOT}" "${PYTHON}" - <<'PY' || die "依赖检查失败；请按 environment.yml 重建环境"
import importlib

missing = []
for name in ("mcp", "rasterio", "shapely", "pyproj", "numpy", "scipy",
             "matplotlib", "PIL", "websockets", "yaml", "pydantic"):
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - 报告中要如实列出
        missing.append(f"{name}: {type(exc).__name__}: {exc}")
if missing:
    raise SystemExit("缺少依赖：\n  " + "\n  ".join(missing))

import ovital_mcp
from ovital_mcp.config import load_config

print("deps ok; ovital_mcp", getattr(ovital_mcp, "__version__", "?"))
cfg = load_config()
print("config ok; ovital.configured =", cfg.safe_dict()["ovital"]["psk_configured"])
PY
fi

# ---------------------------------------------------------------------------
# 3. 生成配置 / 密钥目录 / 本地 patch（--check-only 不落盘）
# ---------------------------------------------------------------------------
CONFIG_DIR="${REPO_ROOT}/config"
CONFIG_FILE="${CONFIG_DIR}/ovital.yaml"
PATCH_SRC="${REPO_ROOT}/ovital_mcp/dsh/ovital.cordis.yml"
PATCH_LOCAL="${REPO_ROOT}/ovital_mcp/dsh/ovital.cordis.local.yml"
PSK_FILE="${SECRETS_DIR}/ovital_psk.txt"

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  log "check-only：将生成（未写入）"
  log "  配置：${CONFIG_FILE}（源：ovital_mcp/dsh/config.example.yaml）"
  log "  密钥目录：${SECRETS_DIR}（权限 700；PSK 文件：${PSK_FILE}，权限 600）"
  log "  本地 patch：${PATCH_LOCAL}（把 C:/work/omap-mcp 替换为 ${REPO_ROOT}）"
else
  mkdir -p "${CONFIG_DIR}"
  if [[ -f "${CONFIG_FILE}" ]]; then
    log "配置文件已存在，不覆盖：${CONFIG_FILE}"
  else
    cp "${REPO_ROOT}/ovital_mcp/dsh/config.example.yaml" "${CONFIG_FILE}"
    log "已生成配置：${CONFIG_FILE}（请修改 endpoint/psk_file/data_root/gis.grass_cmd）"
  fi

  umask 077
  mkdir -p "${SECRETS_DIR}"
  chmod 700 "${SECRETS_DIR}"
  if [[ -f "${PSK_FILE}" ]]; then
    log "密钥文件已存在，不覆盖：${PSK_FILE}"
  else
    : > "${PSK_FILE}"
    chmod 600 "${PSK_FILE}"
    log "已创建空密钥文件：${PSK_FILE}"
    log "  -> 请把奥维预共享密钥写入该文件首行（UTF-8 无 BOM，不要多余空行），再执行 chmod 600"
  fi

  if [[ -f "${PATCH_LOCAL}" ]]; then
    log "本地 patch 已存在，不覆盖：${PATCH_LOCAL}"
  else
    # 生成 POSIX 可用的本地 patch：替换 Python 入口与仓库/配置文件路径。
    PYTHON_ABS="$("${PYTHON}" -c 'import sys; print(sys.executable)')"
    "${PYTHON}" - "${PATCH_SRC}" "${REPO_ROOT}" "${PYTHON_ABS}" "${PATCH_LOCAL}" <<'PY'
import sys
from pathlib import Path

src, repo_root, python_abs, out = map(Path, sys.argv[1:5])
text = src.read_text(encoding="utf-8")
text = text.replace("C:/work/omap-mcp/.venv/Scripts/python.exe", python_abs.as_posix())
text = text.replace("C:/work/omap-mcp", repo_root.as_posix())
out.write_text(text, encoding="utf-8")
print(f"[install] 已生成本地 patch：{out}")
PY
  fi

  # 配置样例是 Windows 占位路径；生成 POSIX 副本时替换为实际路径。
  if [[ -f "${CONFIG_FILE}" ]] && grep -q "C:/work/omap-mcp" "${CONFIG_FILE}" 2>/dev/null; then
    PYTHON_ABS="${PYTHON_ABS:-$("${PYTHON}" -c 'import sys; print(sys.executable)')}"
    "${PYTHON}" - "${CONFIG_FILE}" "${REPO_ROOT}" "${SECRETS_DIR}" "${PYTHON_ABS}" <<'PY'
import sys
from pathlib import Path

config_path, repo_root, secrets_dir, python_abs = map(Path, sys.argv[1:5])
text = config_path.read_text(encoding="utf-8")
text = text.replace("C:/work/omap-mcp/.venv/Scripts/python.exe", python_abs.as_posix())
text = text.replace("C:/work/omap-mcp", repo_root.as_posix())
text = text.replace("C:/work/omap-secrets", secrets_dir.as_posix())
config_path.write_text(text, encoding="utf-8")
print(f"[install] 已把配置中的 Windows 占位路径替换为本地路径：{config_path}")
PY
  fi
  log "本地 patch 使用（示例）：dsh web --patch \"${PATCH_LOCAL}\""
fi

# ---------------------------------------------------------------------------
# 4. GRASS（可选）提示
# ---------------------------------------------------------------------------
if [[ -n "${CONDA_EXE}" ]] && "${CONDA_EXE}" env list 2>/dev/null | awk '{print $1}' | grep -qx "${GRASS_ENV}"; then
  log "检测到 GRASS 环境：${GRASS_ENV}"
  "${CONDA_EXE}" run -n "${GRASS_ENV}" grass --version 2>/dev/null || warn "GRASS 启动失败（检查 HOME/配置目录权限）"
else
  log "未检测到 GRASS 环境（可选）：conda create -n ${GRASS_ENV} -c conda-forge grass=8.5 -y"
fi

# ---------------------------------------------------------------------------
# 5. 后续步骤
# ---------------------------------------------------------------------------
cat <<EOF

[install] 完成。后续步骤：
  1) 编辑配置：${CONFIG_FILE}
     - ovital.endpoint 必须与奥维“第三方接口”设置一致（端口/ws/wss）
     - ovital.psk_file 指向受限密钥文件；不要把 PSK 写进配置/patch/环境变量
     - storage.data_root / gis.grass_cmd 按实际部署修改
  2) 写入 PSK：${PSK_FILE}（首行；UTF-8 无 BOM；随后 chmod 600）
  3) 启动 DSH：
       dsh web --patch "${PATCH_LOCAL}"
  4) 在 DSH 会话中调用 get_capabilities，确认 18 个 mcp__ovital__* 工具、连接与权限
  5) 验收步骤见 docs/install-windows.md §6（POSIX 同理）与 docs/待核验清单.md

[install] 注意：本机没有 Windows 奥维时，真实联机与边界精度验收仍未完成；
          模拟服务器/合成 DEM 的结果不能替代真实设备验收。
EOF
