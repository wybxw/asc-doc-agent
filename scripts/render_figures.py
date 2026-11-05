"""把 data/figures/src/*.mmd 渲染成文档插图用的 PNG。

依赖本机的 Node + @mermaid-js/mermaid-cli（装在 tools/node_modules 下，已被 .gitignore 排除）
和本机 Chrome（puppeteer 复用系统 Chrome，不下载 chromium）。

用法：
    python scripts/render_figures.py            # 渲染全部
    python scripts/render_figures.py fig1 fig2  # 只渲染名字里含这些关键字的图
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "figures" / "src"
OUT = ROOT / "data" / "figures"

CLI = ROOT / "tools" / "node_modules" / "@mermaid-js" / "mermaid-cli" / "src" / "cli.js"
PPTR = ROOT / "tools" / "pptr.json"
CONF = SRC / "mermaid.json"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# 每张图的渲染宽度（px）：等于该图在 mermaid 里的自然宽度，不做二次缩放，
# 这样 24px 的字号原样保留，插到 Word 里按 16~17 cm 宽度放置时约 6~9 pt。
WIDTH = {
    "fig1_architecture": 1630,
    "fig2_context": 1140,
    "fig3_runtime": 880,
    "fig4_trace": 1170,
    "fig5_index": 905,
    "fig6_cards": 1980,
}


def ensure_pptr_config() -> None:
    if PPTR.exists():
        return
    exe = next((p for p in CHROME_CANDIDATES if Path(p).exists()), "")
    if not exe:
        raise SystemExit("找不到 Chrome / Edge，无法渲染。请先装 Chrome。")
    PPTR.parent.mkdir(parents=True, exist_ok=True)
    PPTR.write_text(
        json.dumps(
            {"executablePath": exe, "headless": True, "args": ["--no-sandbox"]},
            indent=2,
        ),
        encoding="utf-8",
    )


def render(src: Path, w: int) -> Path:
    out = OUT / (src.stem + ".png")
    cmd = [
        "node", str(CLI),
        "-p", str(PPTR),
        "-c", str(CONF),
        "-i", str(src),
        "-o", str(out),
        "--size", str(w),
        "-b", "white",
        "-s", "2",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise SystemExit(f"渲染失败 {src.name}:\n{r.stdout}\n{r.stderr}")
    return out


def main(argv: list[str]) -> int:
    if not CLI.exists():
        raise SystemExit(
            f"没有找到 mermaid-cli：{CLI}\n"
            "先在 tools/ 下执行：PUPPETEER_SKIP_DOWNLOAD=1 npm i @mermaid-js/mermaid-cli"
        )
    ensure_pptr_config()
    picks = [s for s in argv if not s.startswith("-")]
    srcs = sorted(SRC.glob("*.mmd"))
    if picks:
        srcs = [p for p in srcs if any(k in p.stem for k in picks)]
    if not srcs:
        raise SystemExit("没有匹配到 .mmd 文件")
    for src in srcs:
        out = render(src, WIDTH.get(src.stem, 1500))
        print(f"{src.name} -> {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
