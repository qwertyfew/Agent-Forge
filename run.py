#!/usr/bin/env python
"""AgentForge 入口。

交互模式:   python run.py
单任务模式: python run.py -t "在工作区里新建一个 hello.py 并运行它"
Web 界面:   python run.py --web
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentforge.cli import CLI  # noqa: E402
from agentforge.config import Config  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agentforge",
        description="AgentForge - 基于本机 Ollama 的智能体运行层",
    )
    ap.add_argument("-c", "--config", help="配置文件路径，默认 ./config.yaml")
    ap.add_argument("-w", "--workspace", help="覆盖工作区根目录")
    ap.add_argument("-m", "--model", help="指定模型 key：general / code / fast")
    ap.add_argument("-t", "--task", help="执行单个任务后退出，不进入交互模式")
    ap.add_argument("-y", "--yes", action="store_true", help="自动批准写文件与执行命令")
    ap.add_argument("--web", action="store_true", help="启动 Web 界面")
    ap.add_argument("--port", type=int, default=8787, help="Web 界面端口，默认 8787")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    cfg = Config.load(args.config)
    if args.workspace:
        cfg.workspace_root = Path(args.workspace)
    if args.model and args.model not in cfg.models:
        print(f"错误：未知模型 key '{args.model}'，可选 {list(cfg.models)}")
        return 2

    if not cfg.workspace_root.exists():
        print(f"错误：工作区不存在 {cfg.workspace_root}")
        return 2

    if args.web:
        from webui import serve

        httpd, url = serve(cfg, args.yes, args.port)
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已退出")
        return 0

    cli = CLI(cfg, auto_approve=args.yes, model_key=args.model)

    if not cli.client.ping():
        print(f"错误：连不上 Ollama（{cfg.ollama_host}）。请先启动 Ollama。")
        return 1

    available = cli.client.list_models()
    missing = [s.name for s in cfg.models.values() if s.name not in available]
    if missing:
        print("警告：以下模型尚未下载，用到时会报错：")
        for m in missing:
            print(f"  ollama pull {m}")

    if args.task:
        cli.run_once(args.task)
        return 0

    cli.repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
