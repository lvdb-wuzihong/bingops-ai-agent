"""CLI 入口：python -m inspection_agent report [--date YYYY-MM-DD] [--config PATH] [--dry-run]。

调度形态：外部 cron/schtasks 每日 10:00 触发一次进程（一次日报 = 一次运行）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台中文输出兜底
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="inspection-agent", description="bingops 巡检日报 Agent（P0 零 LLM 规则版）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    report_parser = sub.add_parser("report", help="生成并推送巡检日报（覆盖前一自然日）")
    report_parser.add_argument("--date", help="统计日期 YYYY-MM-DD，默认前一自然日（按配置时区）")
    report_parser.add_argument(
        "--config", default="config/inspection.yaml",
        help="配置文件路径（样例见 config/inspection.example.yaml）",
    )
    report_parser.add_argument("--dry-run", action="store_true", help="只生成落盘并打印，不推送飞书")
    report_parser.add_argument("--verbose", action="store_true", help="调试日志")
    bot_parser = sub.add_parser("bot", help="启动飞书聊天助手（HTTP 服务，常驻；需 bot/llm.enabled=true）")
    bot_parser.add_argument(
        "--config", default="config/inspection.yaml",
        help="配置文件路径（样例见 config/inspection.example.yaml）",
    )
    bot_parser.add_argument("--verbose", action="store_true", help="调试日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.command == "report":
        report_date: date | None = None
        if args.date:
            try:
                report_date = date.fromisoformat(args.date)
            except ValueError:
                print("错误：--date 需为 YYYY-MM-DD", file=sys.stderr)
                return 2
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"错误：配置文件不存在 {config_path}", file=sys.stderr)
            return 2
        from .runner import run_report  # 延迟导入便于 --help 快速响应

        return asyncio.run(run_report(config_path, report_date=report_date, dry_run=args.dry_run))
    if args.command == "bot":
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"错误：配置文件不存在 {config_path}", file=sys.stderr)
            return 2
        from .bot.app import serve_bot  # 延迟导入便于 --help 快速响应

        return serve_bot(config_path)
    return 2


if __name__ == "__main__":
    sys.exit(main())
