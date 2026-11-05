"""编排循环：system prompt + 工具循环 + 取证轨迹。

循环很朴素，但每条纪律都对应一次真实的失败教训：

1. **先取证再回答**——根提示词强制「没有工具返回就不要下结论」。
2. **工具结果全量回灌**——不做摘要压缩，避免模型丢掉 chip 名/路径这类硬事实。
3. **轮数上限**——防止模型在 found=false 时无限换词重试；到限就让它带着已有证据收尾。
4. **轨迹可导出**——每次工具调用（名、参数、耗时、结果长度）都记进 trace，
   既是「编排过程可复现、可解释」的依据，也是调 prompt 的证据。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from asc_agent.agent.llm import ChatClient
from asc_agent.agent.prompts import SYSTEM_PROMPT, build_task_prompt

MAX_ROUNDS = 8


@dataclass
class Step:
    """一次工具调用记录。"""

    round: int
    call_id: str
    name: str
    arguments: dict[str, Any]
    ok: bool
    elapsed_ms: int
    chars: int
    preview: str


@dataclass
class Trace:
    """一次问答的完整轨迹，用于 --trace 输出与调试。"""

    question: str
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    stop_reason: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "rounds": self.rounds,
            "tools_used": [s.name for s in self.steps],
            "tool_calls": len(self.steps),
            "failed_calls": sum(0 if s.ok else 1 for s in self.steps),
            "elapsed_ms": self.elapsed_ms,
            "usage": self.usage,
            "stop_reason": self.stop_reason,
        }


class Agent:
    """把 LLM、MCP 工具、提示词粘在一起的那层胶水。"""

    def __init__(self, bridge, client: ChatClient, max_rounds: int = MAX_ROUNDS):
        self.bridge = bridge
        self.client = client
        self.max_rounds = max_rounds
        self.tools = bridge.tools

    # ------------------------------------------------------------------
    def run(self, question: str, workflow: str = "", history: list[dict] | None = None) -> tuple[str, Trace]:
        """跑一轮问答，返回（最终回答, 轨迹）。"""
        trace = Trace(question=question)
        started = time.time()

        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": build_task_prompt(question, workflow)})

        answer = ""
        for round_no in range(1, self.max_rounds + 1):
            trace.rounds = round_no
            try:
                turn = self.client.chat(messages, tools=self.tools)
            except Exception as exc:  # noqa: BLE001
                # 网络/鉴权/限流失败不该把整条对话炸成 traceback：保留已取到的证据，
                # 让调用方拿到一句能读懂的失败原因。
                trace.stop_reason = "llm_error"
                trace.elapsed_ms = int((time.time() - started) * 1000)
                detail = f"{type(exc).__name__}: {exc}"
                tip = "请检查 ASC_AGENT_API_KEY / ASC_AGENT_BASE_URL / ASC_AGENT_MODEL，或稍后重试。"
                if trace.steps:
                    return (
                        f"调用模型失败（{detail}）。已取到 {len(trace.steps)} 条工具结果"
                        f"（可用 --trace 查看），但没能生成最终回答。{tip}",
                        trace,
                    )
                return f"调用模型失败（{detail}）。{tip}", trace
            if turn.usage:
                for k, v in turn.usage.items():
                    trace.usage[k] = trace.usage.get(k, 0) + v

            if not turn.wants_tools:
                answer = turn.content.strip()
                if not answer:
                    # 模型偶尔返回空内容（内容过滤、或只给了 finish_reason）。
                    # 不要静默交出空白，至少说明发生了什么。
                    answer = (
                        "（模型没有返回内容"
                        f"；finish_reason={turn.finish_reason or 'unknown'}。"
                        "可以换个问法，或加 --trace 看工具调用情况。）"
                    )
                trace.stop_reason = "answered"
                break

            # 记录 assistant 的工具调用意图，OpenAI 协议要求原样回灌
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.content or "",
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": c.raw_arguments or json.dumps(c.arguments, ensure_ascii=False),
                            },
                        }
                        for c in turn.tool_calls
                    ],
                }
            )

            for call in turn.tool_calls:
                t0 = time.time()
                try:
                    result = self.bridge.call(call.name, call.arguments)
                    ok = not result.startswith('{"tool_error": true')
                except Exception as exc:  # noqa: BLE001 - 工具崩溃不能打断整轮对话
                    result = json.dumps(
                        {"tool_error": True, "detail": f"{type(exc).__name__}: {exc}"},
                        ensure_ascii=False,
                    )
                    ok = False
                elapsed = int((time.time() - t0) * 1000)
                trace.steps.append(
                    Step(
                        round=round_no,
                        call_id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                        ok=ok,
                        elapsed_ms=elapsed,
                        chars=len(result),
                        preview=result[:280].replace("\n", " "),
                    )
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result}
                )
        else:
            # 轮数用尽：让模型基于现有证据收尾，而不是把半成品丢给用户
            trace.stop_reason = "max_rounds"
            messages.append(
                {
                    "role": "user",
                    "content": "工具调用预算已用完。请只根据上面已经取到的证据作答，"
                    "并在开头用一句话说明还有哪些信息没查到。",
                }
            )
            try:
                turn = self.client.chat(messages, tools=None)
            except Exception as exc:  # noqa: BLE001
                trace.stop_reason = "llm_error"
                trace.elapsed_ms = int((time.time() - started) * 1000)
                return (
                    f"工具预算已用完，且收尾时调用模型失败"
                    f"（{type(exc).__name__}: {exc}）。已取到 {len(trace.steps)} 条工具结果，"
                    "可用 --trace 查看。",
                    trace,
                )
            answer = turn.content.strip()
            if not answer:
                answer = (
                    f"工具调用预算已用完，模型没有给出收尾回答。"
                    f"已取到 {len(trace.steps)} 条工具结果（可用 --trace 查看）。"
                )

        trace.elapsed_ms = int((time.time() - started) * 1000)
        return answer, trace
