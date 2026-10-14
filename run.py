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
    ap.add_argument("-m", "--model",
                    help="指定模型 key（config.yaml 里的 key，或自动发现的模型名，如 gpt-oss:20b）")
    ap.add_argument("-t", "--task", help="执行单个任务后退出，不进入交互模式")
    ap.add_argument("-y", "--yes", action="store_true", help="自动批准写文件与执行命令")
    ap.add_argument("--web", action="store_true", help="启动 Web 界面")
    ap.add_argument("--port", type=int, default=8787, help="Web 界面端口，默认 8787")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Web 界面监听地址，默认 127.0.0.1；容器/云部署用 0.0.0.0")
    ap.add_argument("--no-open", action="store_true",
                    help="启动后不自动开浏览器（service.py 这类常驻托管用："
                         "它会在崩溃重启时反复拉起，每次都弹标签页会刷屏）")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    cfg = Config.load(args.config)
    if args.workspace:
        cfg.workspace_root = Path(args.workspace)

    if not cfg.workspace_root.exists():
        print(f"错误：工作区不存在 {cfg.workspace_root}")
        return 2

    if args.web:
        from webui import serve

        httpd, url = serve(cfg, args.yes, args.port, host=args.host)
        if not args.no_open:
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

    # 校验放在 CLI 之后：自动发现的本地模型要等 catalog() 才注册进 cfg.models，
    # 放在前面会把 `-m gpt-oss:20b` 这类有效的选择误判成未知模型。
    if args.model and args.model not in cfg.models:
        keys = [k for k, s in cfg.models.items() if s.kind != "paid"]
        print(f"错误：未知模型 key '{args.model}'，可选本地模型 {keys}")
        return 2

    # 连不上 Ollama 不再直接退出：在线模型不依赖它，本地模型一个没装时
    # 照样能靠在线模型开工。只提示一句，让路由去挑能跑的。
    ollama_up = cli.client.ping()
    if ollama_up:
        available = cli.client.list_models()
        # 只检查本地模型：付费模型本来就不在本机 Ollama 里，算进来会一直误报
        # 「尚未下载」，把真正缺的本地模型淹掉。
        missing = [s.name for s in cfg.models.values()
                   if s.kind != "paid" and s.name not in available]
        if missing:
            print("提示：以下本地模型尚未安装，用到时会报错：")
            for m in missing:
                print(f"  ollama pull {m}")

    # 一个能用的模型都没有——新电脑 clone 下来、没装 Ollama、又没把模板里的 api_key
    # 填上时就是这个状态。先在这里说清楚下一步，别让用户撞上 "model not found"。
    if not cli.store.fallback_pool():
        print("错误：这台机器上一个可用模型都没有，无法开工。二选一：")
        print("  用在线模型：cp models.example.json models.json  再把里面的 api_key 填上")
        print("  用本地模型：装好 Ollama 后 ollama pull 一个模型，下次启动会自动配好窗口")
        return 2

    if not ollama_up:
        print(f"提示：连不上 Ollama（{cfg.ollama_host}），本地模型暂不可用，将只用在线模型。")

    # 默认模型跑不起来时会自动改用能跑的（见 Router.fallback_key），先说清楚
    picked = args.model or cli.router.fallback_key()
    spec = cfg.models.get(picked)
    if spec is not None and picked != cfg.routing_default and not args.model:
        print(f"默认模型本机不可用，本次改用：{spec.display}")

    if args.task:
        cli.run_once(args.task)
        return 0

    cli.repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
