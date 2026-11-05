"""LLM 客户端：OpenAI 兼容的 chat.completions + function calling。

为什么不深度绑定某一家：工具面全部用标准 function calling 暴露，而
DeepSeek / 通义 / vLLM / Ollama 都提供 OpenAI 兼容端点，只要 base_url +
api_key + model 三个值即可切换。真需要换协议时，只需再写一个同接口的类。

没有 API key 的场景（校园网离线、评审环境）由 `MockClient` 兜底：它按固定
剧本调用工具，保证「编排循环 + MCP 工具」这条链路可被完整演示与测试。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from asc_agent import config


@dataclass
class ToolCall:
    """模型请求发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass
class AssistantTurn:
    """模型一轮输出：可能只有文本，也可能带工具调用。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ChatClient(Protocol):
    """编排循环只需要这一个方法。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> AssistantTurn: ...


_API_LIKE = re.compile(r"\b([A-Z][A-Za-z0-9]+|[A-Z]{2,}[A-Za-z0-9]*)\b")
_API_STOP = {
    "Ascend", "Atlas", "CANN", "API", "UT", "GM", "UB", "AI", "CPU", "SIMD",
    "SIMT", "NPU", "SoC", "SOC",
}


def _api_like_token(text: str) -> str:
    """从问句里挑一个像 API 名的英文词（首字母大写），用于精确查卡片。"""
    for m in _API_LIKE.finditer(text):
        tok = m.group(1)
        if tok not in _API_STOP and len(tok) >= 2:
            return tok
    return ""


def _parse_arguments(raw: str) -> dict[str, Any]:
    """模型偶尔会给出空串或坏 JSON；解析失败就退回空参，由工具层报参数错。"""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class OpenAIChatClient:
    """OpenAI 兼容客户端（DeepSeek / OpenAI / vLLM / Ollama 均可）。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        from openai import OpenAI  # 延迟导入：离线模式下不需要这个依赖

        self.model = model or config.LLM_MODEL
        key = api_key or config.LLM_API_KEY
        if not key:
            raise RuntimeError(
                "缺少 API key：请设置 ASC_AGENT_API_KEY 或 OPENAI_API_KEY；"
                "离线演示请用 --offline。"
            )
        self._client = OpenAI(base_url=base_url or config.LLM_BASE_URL, api_key=key)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> AssistantTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        calls = [
            ToolCall(
                id=c.id,
                name=c.function.name,
                arguments=_parse_arguments(c.function.arguments),
                raw_arguments=c.function.arguments or "",
            )
            for c in (msg.tool_calls or [])
        ]
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return AssistantTurn(
            content=msg.content or "",
            tool_calls=calls,
            finish_reason=choice.finish_reason or "",
            usage=usage,
        )


class MockClient:
    """离线演示客户端：按剧本调用真实 MCP 工具，再由模板生成回答。

    它**不是**假装模型在思考——工具真的会被调用、真的会查索引；
    只有「选哪个工具、怎么措辞」是预设的。这样离线也能验证整条链路。
    """

    name = "mock"

    # 关键词 → 工具调用。顺序敏感：越具体的意图放越前面。
    SCRIPTS: list[tuple[tuple[str, ...], list[tuple[str, dict[str, Any]]]]] = [
        (
            ("知识库", "知识卡片", "卡片", "记下来", "经验"),
            [("card_search", {"query": "支持性 CSV 路径"})],
        ),
        (
            ("ut", "单元测试", "测试用例", "gtest"),
            [("read_skill", {"skill": "asc-api-ut-gen"})],
        ),
        (
            ("规范", "写作", "标点", "章节顺序"),
            [("read_skill", {"skill": "asc-doc-checker"})],
        ),
        (
            ("样例", "示例", "例子", "工程"),
            [("find_examples", {"keyword": "cast"})],
        ),
        (
            ("架构号", "npu_arch", "socversion", "架构是什么", "什么架构"),
            [("chip_facts", {"chip": "ascend910b1"})],
        ),
        (
            ("支持", "哪些芯片", "兼容", "能不能用"),
            [("api_support", {"api": "Exp"})],
        ),
    ]

    def __init__(self) -> None:
        self._turn = 0

    @staticmethod
    def _raw_question(text: str) -> str:
        """剥掉编排器注入的工作流说明，只按用户原问题选工具。

        否则工作流步骤里的 `__NPU_ARCH__`、`支持矩阵` 这类词会污染关键词匹配
        （例如「Exp 在 910b 上支持吗」配上 chip_support 工作流时，
        步骤里出现的 `__NPU_ARCH__` 会把它误路由到 chip_facts）。
        """
        marker = "\n\n---\n"
        return text.split(marker, 1)[0].strip()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> AssistantTurn:
        self._turn += 1
        # 第一轮：挑工具；之后：根据工具结果收尾
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if self._turn == 1:
            calls = self._plan(self._raw_question(last_user))
            if calls:
                return AssistantTurn(
                    content="",
                    tool_calls=[
                        ToolCall(id=f"mock_{i}", name=n, arguments=a)
                        for i, (n, a) in enumerate(calls)
                    ],
                    finish_reason="tool_calls",
                )
        summary = self._summarize(tool_msgs)
        return AssistantTurn(content=summary, finish_reason="stop")

    def _plan(self, question: str) -> list[tuple[str, dict[str, Any]]]:
        lower = question.lower()
        for keys, calls in self.SCRIPTS:
            if any(k.lower() in lower for k in keys):
                return calls
        # 「XX 怎么用 / 参数是什么」这类问题：如果句子里有类 API 名（首字母大写的
        # 英文单词），直接查 API 卡片比全文检索更准。
        if any(k in question for k in ("怎么用", "怎么调用", "参数", "原型", "头文件")):
            token = _api_like_token(question)
            if token:
                return [("api_card", {"name": token})]
        # 兜底：凡是想问「怎么用/在哪」的，都先检索
        return [("search_docs", {"query": question, "top_k": 4})]

    # ---- 结果渲染：把工具返回的 JSON 摘成人类可读的要点 ------------------
    @staticmethod
    def _render(name: str, data: dict) -> list[str]:
        if data.get("tool_error"):
            return [f"工具报错：{str(data.get('detail'))[:300]}"]

        if name == "search_docs":
            lines = [f"命中 {data.get('count', 0)} 条："]
            for h in data.get("hits", [])[:4]:
                lines.append(f"- `{h['path']}`（{h['heading']}）")
            return lines

        if name == "api_card":
            if not data.get("found"):
                return ["未找到该 API 的卡片。"]
            lines = [f"**{data['name']}**（`{data['path']}`）"]
            if data.get("headers"):
                lines.append(f"- 头文件：{'、'.join('`'+h+'`' for h in data['headers'])}")
            if data.get("sections", {}).get("函数原型"):
                proto = data["sections"]["函数原型"].split("```")[1:2]
                if proto:
                    lines.append("- 原型：\n```cpp\n" + proto[0].strip()[:400] + "\n```")
            if data.get("alternatives"):
                lines.append(
                    "- 同名文档还有：" + "、".join(f"`{a['path']}`" for a in data["alternatives"])
                )
            return lines

        if name == "api_support":
            lines = [f"**{data['api']}**：{data.get('verdict', '')}"]
            # 按文档看更准：同一个 API 在 regbase / membase / adv 三份文档里
            # 支持范围不同，只报「部分支持」没有可操作性。
            for doc, chips in list(data.get("by_doc", {}).items())[:4]:
                detail = "；".join(
                    f"{chip} {v['supported']}/{v['total']}"
                    for chip, v in list(chips.items())[:4]
                )
                lines.append(f"- `{doc}`：{detail}（支持数/原型数）")
            if not data.get("by_doc"):
                for chip, rows in list(data.get("by_chip", {}).items())[:6]:
                    ok = sum(1 for r in rows if r["supported"])
                    lines.append(f"- {chip}：{ok}/{len(rows)} 项原型支持")
            for row in data.get("csv", [])[:4]:
                lines.append(f"- CSV `{row['chip']}` = {row['status']}（`{row['path']}`）")
            if data.get("conflicts"):
                lines.append("- ⚠ 两来源冲突：" + "；".join(data["conflicts"]))
            return lines

        if name == "chip_facts":
            chip = data.get("chip")
            if chip:
                if not chip.get("found"):
                    return [f"资料库未收录该芯片；已知：{', '.join(chip.get('known', [])[:8])}"]
                return [
                    f"**{chip['normalized']}**：`__NPU_ARCH__` = {chip['npu_arch']}，"
                    f"SocVersion = {chip['soc_version']}，实现目录 `{chip['arch_dir']}`"
                ]
            dtype = data.get("dtype")
            if dtype and dtype.get("found"):
                spec = dtype["spec"]
                return [f"{dtype['dtype']}：位宽 {spec['bit_width']}bit，size={spec['size']}，对齐 {spec['align32_elements']}/32B"]
            return ["芯片/数据类型事实已返回，详见原始 JSON。"]

        if name == "read_skill":
            if not data.get("found"):
                return [f"未找到技能：{data.get('error')}"]
            head = [
                ln for ln in data["body"].splitlines()[:14] if ln.strip()
            ]
            return [f"技能 `{data['skill']}`（{data['chars']} 字符）开头：", "```markdown", *head, "```"]

        if name == "card_search":
            hits = data.get("hits", [])
            lines = [f"匹配 {data.get('count', 0)} 张个人知识卡片（**不是官方文档**）："]
            for h in hits[:4]:
                lines.append(
                    f"- `{h['id']}`［{h['type']}/{h['status']}/{h['confidence']}］"
                    f"{h['title']} —— {h.get('summary') or '（无 summary）'}"
                )
            if not hits:
                lines.append(f"- {data.get('hint', '没有匹配')}")
            return lines

        if name == "find_examples":
            lines = [f"找到 {data.get('count', 0)} 个样例工程："]
            for p in data.get("projects", [])[:4]:
                lines.append(f"- `{p['project']}`（{p['files']} 个文件）")
            return lines

        return ["工具 `%s` 已返回，原始内容见下方 JSON。" % name]

    def _summarize(self, tool_msgs: list[dict[str, Any]]) -> str:
        if not tool_msgs:
            return "（离线模式）我没有调用到任何工具，请把问题写得更具体一些。"
        parts = [
            "> 离线演示模式：工具调用是真的，模型措辞由模板拼装；"
            "配置 API key 后可换成真实模型生成。"
        ]
        for m in tool_msgs:
            name = m.get("name") or m.get("tool_name") or "tool"
            try:
                data = json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                data = {"raw": (m.get("content") or "")[:400]}
            parts.append(f"\n**来自工具 `{name}`**\n")
            parts.extend(self._render(name, data))
            parts.append("\n<details><summary>原始返回（截断）</summary>\n")
            parts.append("```json")
            parts.append(json.dumps(data, ensure_ascii=False, indent=2)[:1200])
            parts.append("```\n</details>")
        return "\n".join(parts)


def make_client(offline: bool = False) -> ChatClient:
    """按需构造客户端；离线或没 key 时退回 MockClient。"""
    if offline:
        return MockClient()
    if not (os.environ.get("ASC_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        return MockClient()
    return OpenAIChatClient()
