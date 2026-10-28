"""Career Assistant command line entry point."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Career Assistant 本机求职助手")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("web", help="Flask 页面与单个后台 Worker")
    web.add_argument("--port", type=int, default=5000)
    web.add_argument("--schedule", action="store_true", help="按 Radar 配置定时创建搜索任务")
    sub.add_parser("migrate", help="备份并迁移同一个 Radar SQLite")
    sub.add_parser("worker", help="单独运行 Worker（通常 web 已内置）")
    sub.add_parser("doctor", help="运行维护诊断（会访问网络；不会由启动自动调用）")
    login = sub.add_parser("login", help="显式打开现有浏览器档案，供用户手动登录；不绕过访问限制")
    login.add_argument("source", choices=["jobsdb", "indeed", "linkedin"])
    args = parser.parse_args()
    if args.command == "login":
        from .config import load_config
        from .integrations import SourceBrowser
        browser = SourceBrowser(load_config(require_runnable=False))
        try:
            page = browser.launch(args.source, force_headless=False)
            page.goto({"jobsdb": "https://hk.jobsdb.com/", "indeed": "https://hk.indeed.com/",
                       "linkedin": "https://www.linkedin.com/login"}[args.source])
            input("请手动完成登录；若遇访问限制请停止。完成或取消后按回车关闭：")
        finally:
            browser.close()
        return 0
    if args.command == "doctor":
        from .doctor import run_doctor
        return run_doctor(llm_test=False)
    from career_assistant.config import DB_PATH
    from career_assistant.store import Store
    store = Store(os.environ.get("RADAR_DB_PATH", str(DB_PATH)))
    backup = store.migrate()
    if args.command == "migrate":
        print(f"迁移完成；备份：{backup or '无需新迁移'}")
        return 0

    from career_assistant.app import create_app
    from career_assistant.worker import Worker
    from career_assistant.service import handlers, start_schedule
    app = create_app(store=store)
    if args.command == "web":
        app.config["BASE_URL"] = f"http://127.0.0.1:{args.port}"
    worker = Worker(store, handlers(store, app.config["UPLOAD_DIR"], app.config["BASE_URL"]))
    scheduler = None
    try:
        worker.start()
        scheduler = start_schedule(store, monitoring=args.command == 'web' and args.schedule)
        app.extensions['scheduler'] = scheduler
        if args.command == "web":
            app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
        else:
            import time
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
