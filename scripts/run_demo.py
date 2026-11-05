"""生成 docs/demo.md 所需的演示输出（离线模式，不需要任何 API key）。

用法：PYTHONPATH=. python scripts/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from asc_agent.agent.bridge import LocalServer, McpBridge, pick_port  # noqa: E402
from asc_agent.agent.llm import MockClient  # noqa: E402
from asc_agent.agent.orchestrator import Agent  # noqa: E402
from asc_agent.logging_setup import quiet_logs  # noqa: E402

PAGE = """# 演示记录（离线模式）

下面 {n} 个问题由 `scripts/run_demo.py` 自动生成，**不需要任何 API key**。

说明：工具调用是真实的（真的走 MCP、真的查索引、真的读技能文件）；
只有「选哪个工具、怎么措辞」由 `MockClient` 的模板完成。
配置 `ASC_AGENT_API_KEY` 后同一套工具与提示词可直接换成真实模型。

```bash
python -m asc_agent index      # 建索引（约 3 秒）
python scripts/run_demo.py     # 重新生成本页
```

---

"""

QUESTIONS = [
    ("Exp 在 910b 上支持吗", "chip_support"),
    ("Cast 怎么用", "lookup_api"),
    ("Ascend 950 的架构号是多少", ""),
    ("UT 怎么生成", "gen_ut"),
    ("有没有 cast 的样例", "locate_code"),
]


def main() -> int:
    quiet_logs()
    server = LocalServer(port=pick_port(8970)).start()
    bridge = McpBridge(server.url).__enter__()
    out: list[str] = []
    try:
        print(f"MCP server: {server.url}")
        print(f"工具数: {len(bridge.tools)}")
        for question, wf in QUESTIONS:
            agent = Agent(bridge, MockClient())
            answer, trace = agent.run(question, workflow=wf)
            summary = trace.summary()
            out.append(f"### {question}\n")
            # 不写耗时：每次跑差几毫秒，会让 demo.md 反复产生无意义 diff
            out.append(
                f"- 工作流: `{wf or '自动'}` ｜ 轮数: {summary['rounds']} ｜ "
                f"工具: {', '.join(summary['tools_used'])}\n"
            )
            out.append("<details><summary>回答</summary>\n")
            out.append(answer[:2600])
            out.append("\n</details>\n")
            print(f"  ✓ {question} -> {summary['tools_used']}")
    finally:
        bridge.__exit__(None, None, None)
        server.stop()

    target = ROOT / "docs" / "demo.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        PAGE.format(n=len(QUESTIONS)) + "\n".join(out), encoding="utf-8"
    )
    print(f"已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
