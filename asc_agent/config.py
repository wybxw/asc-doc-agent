"""全局路径与运行参数。

所有路径都可用环境变量覆盖，方便在不同机器/CI 上跑。
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根目录（本文件在 <root>/asc_agent/config.py）
REPO_ROOT = Path(__file__).resolve().parent.parent

# 资料库根目录：默认用仓库内的 asc-devkit
DEVKIT_ROOT = Path(
    os.environ.get("ASC_DEVKIT_ROOT", str(REPO_ROOT / "asc-devkit"))
).resolve()

# 索引产物
DATA_DIR = Path(os.environ.get("ASC_AGENT_DATA", str(REPO_ROOT / "data"))).resolve()
INDEX_DB = Path(os.environ.get("ASC_AGENT_INDEX", str(DATA_DIR / "asc_index.db")))

# 索引范围：只索引中文文档（英文文档体量与中文重合，按需再扩展）
DOCS_SUBDIR = "docs/zh"
SKILLS_SUBDIR = ".agent/skills"

# 个人维护的知识卡片（不是索引产物，要进版本库，所以放在 data/ 之外）
KNOWLEDGE_DIR = Path(
    os.environ.get("ASC_AGENT_KNOWLEDGE", str(REPO_ROOT / "knowledge"))
).resolve()
CARDS_DIR = KNOWLEDGE_DIR / "cards"

# MCP 服务器
MCP_HOST = os.environ.get("ASC_AGENT_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("ASC_AGENT_PORT", "8931"))
MCP_PATH = "/mcp"

# LLM（OpenAI 兼容接口：DeepSeek / 通义 / vLLM / OpenAI 均可）
LLM_BASE_URL = os.environ.get(
    "ASC_AGENT_BASE_URL",
    os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
)
LLM_API_KEY = os.environ.get("ASC_AGENT_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
LLM_MODEL = os.environ.get("ASC_AGENT_MODEL", "deepseek-chat")


def docs_root() -> Path:
    return DEVKIT_ROOT / DOCS_SUBDIR


def skills_root() -> Path:
    return DEVKIT_ROOT / SKILLS_SUBDIR


def cards_root() -> Path:
    return CARDS_DIR


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
