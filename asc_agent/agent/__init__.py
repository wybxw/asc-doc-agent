"""agent 层：提示词、编排循环、LLM 客户端、MCP 桥。

注意本文件不做 re-export：`prompts` / `orchestrator` / `bridge` 之间有引用关系，
在这里聚合会拉长导入链并引发循环导入。请直接 import 具体模块。
"""

from __future__ import annotations

__all__: list[str] = []
