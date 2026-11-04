"""端到端测试：真的起 MCP server、真的走 streamable-http、真的跑编排循环。

这组测试是作业要求「MCP 打通 agent 与本地环境」的可执行证明：
断言的每一条都要求一次真实的 HTTP 往返，不是 mock。
"""

from __future__ import annotations

import json

import pytest

from asc_agent import config
from asc_agent.agent.bridge import LocalServer, McpBridge, pick_port
from asc_agent.agent.llm import AssistantTurn, MockClient, ToolCall
from asc_agent.agent.orchestrator import Agent
from asc_agent.agent.prompts import SYSTEM_PROMPT, build_task_prompt, list_workflows, resolve_workflow

pytestmark = pytest.mark.skipif(
    not config.INDEX_DB.exists(),
    reason="索引未构建，先跑 python -m asc_agent.indexing.indexer",
)


# ---------------------------------------------------------------- 提示词


def test_system_prompt_has_no_hardcoded_facts():
    """提示词里不许出现具体架构号/dtype 位宽/资料路径——那些必须由工具现取。

    提示词一旦写死事实，资料更新后就会腐化，而且模型会拿记忆覆盖工具结果。
    """
    for forbidden in ("__NPU_ARCH__=2201", "npu_arch", "ASCEND910B", "docs/zh/api",
                      "fp8_e4m3fn_t", "ascendc_case_"):
        assert forbidden not in SYSTEM_PROMPT, f"提示词里不该硬编码 {forbidden}"


def test_system_prompt_states_discipline():
    for keyword in ("先取证", "chip_facts", "冲突", "原型", "出处"):
        assert keyword in SYSTEM_PROMPT, keyword


def test_workflows_are_resolvable():
    keys = {w["key"] for w in list_workflows()}
    assert "lookup_api" in keys
    assert resolve_workflow("api") == "lookup_api"
    assert resolve_workflow("不支持的类型") == ""


def test_build_task_prompt_injects_steps():
    plain = build_task_prompt("Exp 怎么用")
    assert plain == "Exp 怎么用"
    with_wf = build_task_prompt("Exp 怎么用", "lookup_api")
    assert "api_card" in with_wf
    assert "查 API 用法" in with_wf


# ---------------------------------------------------------------- MCP 往返


@pytest.fixture(scope="module")
def bridge():
    server = LocalServer(port=pick_port(8960)).start()
    b = McpBridge(server.url).__enter__()
    yield b
    b.__exit__(None, None, None)
    server.stop()


def test_bridge_exposes_all_tools(bridge: McpBridge):
    names = set(bridge.names())
    expected = {
        "search_docs", "api_card", "api_support", "chip_apis", "chip_facts",
        "read_doc", "read_skill", "fetch_url", "check_markdown",
        "find_symbol", "find_examples", "gen_ut", "repo_stats",
    }
    assert expected <= names


def test_bridge_tool_schema_is_flat(bridge: McpBridge):
    """工具参数必须是扁平 properties，不能是嵌套 $ref——否则模型调不对。"""
    for tool in bridge.tools:
        params = tool["function"]["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params
        for name, spec in params["properties"].items():
            assert "$ref" not in spec, f"{tool['function']['name']}.{name} 是嵌套引用"


def test_bridge_calls_search_tool_over_http(bridge: McpBridge):
    raw = bridge.call("search_docs", {"query": "精度转换", "top_k": 3})
    payload = json.loads(raw)
    assert payload["count"] > 0
    assert payload["hits"][0]["path"]


def test_bridge_calls_chip_facts(bridge: McpBridge):
    payload = json.loads(bridge.call("chip_facts", {"chip": "ascend950"}))
    assert payload["chip"]["normalized"] == "ascend950pr_9599"
    assert payload["chip"]["npu_arch"] == "3510"


def test_bridge_reports_tool_errors_as_data(bridge: McpBridge):
    """参数非法时必须返回可读的错误内容，而不是把协议打挂。

    越界参数在 MCP 的 input_schema 校验层就被拦下，表现为 is_error=True +
    带字段名的文本；工具内部的业务性错误则返回 {"error": ...} 的 JSON。
    两种形态 agent 都要能读，所以这里断言「有可解析的、含字段名的错误」。
    """
    raw = bridge.call("search_docs", {"query": "x", "top_k": 999})
    if raw.startswith('{"tool_error"'):
        payload = json.loads(raw)
        assert "top_k" in payload["detail"]
    else:
        payload = json.loads(raw)
        assert payload["details"][0]["field"] == "top_k"


def test_bridge_surfaces_business_errors_as_json(bridge: McpBridge):
    """业务层面的「查不到」是正常 JSON，不该走 tool_error 通道。"""
    payload = json.loads(bridge.call("api_card", {"name": "NoSuchApiXyz"}))
    assert payload["found"] is False
    assert payload["hint"]


def test_bridge_reads_skill_resource(bridge: McpBridge):
    body = bridge.read_resource("skill://asc-npu-arch/SKILL.md")
    assert "统一芯片调用名" in body


def test_bridge_reads_chip_resource(bridge: McpBridge):
    payload = json.loads(bridge.read_resource("chip://facts/ascend910b1"))
    assert payload["npu_arch"] == "2201"


# ---------------------------------------------------------------- 编排循环


class ScriptedClient:
    """严格按脚本返回的假模型——用来精确验证编排循环的控制流。"""

    def __init__(self, script: list[AssistantTurn]):
        self.script = script
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    def chat(self, messages, tools=None, temperature=0.2) -> AssistantTurn:
        self.seen_messages.append(list(messages))
        turn = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return turn


def test_agent_executes_tool_calls_then_answers(bridge: McpBridge):
    client = ScriptedClient(
        [
            AssistantTurn(
                tool_calls=[
                    ToolCall(id="c1", name="search_docs",
                             arguments={"query": "精度转换", "top_k": 2},
                             raw_arguments='{"query":"精度转换","top_k":2}')
                ]
            ),
            AssistantTurn(content="精度转换见 precision_conversion.md。"),
        ]
    )
    agent = Agent(bridge, client)
    answer, trace = agent.run("精度转换怎么做")
    assert "precision_conversion" in answer
    assert [s.name for s in trace.steps] == ["search_docs"]
    assert trace.steps[0].ok is True
    assert trace.stop_reason == "answered"
    assert trace.rounds == 2


def test_agent_feeds_tool_result_back_to_model(bridge: McpBridge):
    client = ScriptedClient(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="chip_facts", arguments={"chip": "910b"})],
            ),
            AssistantTurn(content="done"),
        ]
    )
    Agent(bridge, client).run("910b 架构号")
    second_round = client.seen_messages[1]
    tool_msgs = [m for m in second_round if m.get("role") == "tool"]
    assert tool_msgs, "工具结果必须回灌给模型"
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["chip"]["npu_arch"] == "2201"


def test_agent_records_failed_tool_calls(bridge: McpBridge):
    client = ScriptedClient(
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="search_docs",
                                     arguments={"query": "x", "top_k": 999})]
            ),
            AssistantTurn(content="参数错了"),
        ]
    )
    _, trace = Agent(bridge, client).run("bad call")
    assert trace.steps[0].ok is False
    assert trace.summary()["failed_calls"] == 1


def test_agent_stops_at_max_rounds(bridge: McpBridge):
    """模型一直要工具时，必须在预算用尽后收敛，不能无限循环。"""
    loop_turn = AssistantTurn(
        tool_calls=[ToolCall(id="c", name="repo_stats", arguments={}, raw_arguments="{}")]
    )
    # 脚本里每一轮都是「继续要工具」，第 4 轮（收尾）才给答案
    client = ScriptedClient([loop_turn, loop_turn, loop_turn, AssistantTurn(content="收尾答案")])
    agent = Agent(bridge, client, max_rounds=3)
    answer, trace = agent.run("一直调工具")
    assert trace.stop_reason == "max_rounds"
    assert answer == "收尾答案"
    assert trace.rounds == 3
    # 三轮各调用一次，收尾轮不再给工具
    assert len(trace.steps) == 3


def test_agent_offline_mock_end_to_end(bridge: McpBridge):
    """离线演示路径也要真的走工具，不能空手回答。"""
    client = MockClient()
    answer, trace = agent_run(bridge, client, "Exp 在 910b 上支持吗")
    assert trace.steps, "离线模式也必须调用真实工具"
    assert trace.steps[0].name == "api_support"
    assert "离线演示" in answer


def agent_run(bridge, client, question):
    return Agent(bridge, client).run(question)


def test_mock_client_picks_relevant_tool(bridge: McpBridge):
    for question, expected in [
        ("Exp 在 910b 上支持吗", "api_support"),
        ("UT 怎么生成", "read_skill"),
        ("文档规范检查有哪些规则", "read_skill"),
        ("有没有 cast 的样例", "find_examples"),
        ("Ascend 950 的架构号是多少", "chip_facts"),
    ]:
        client = MockClient()
        _, trace = Agent(bridge, client).run(question)
        assert trace.steps and trace.steps[0].name == expected, question
