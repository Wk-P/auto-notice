from __future__ import annotations

import argparse
import getpass
import sys
import json
import time
from pathlib import Path
from wsgiref.simple_server import make_server

from .accounts import AccountService
from .ai import AIWorker, OpenAIAnalyzer
from .backup import create_backup
from .config import Settings
from .crawler import Crawler
from .db import Database
from .delivery import DeliveryPlanner, DeliveryWorker
from .emailer import ResendMailer
from .monitoring import AlertWorker
from .sources import SOURCES
from .subscriptions import SubscriptionService
from .timeutil import iso_utc
from .web import WebApp


def services():
    settings = Settings.from_env()
    db = Database(settings.database_path)
    db.initialize()
    now = iso_utc()
    with db.transaction() as conn:
        for source in SOURCES:
            conn.execute("""INSERT INTO sources(source_key,name,page_url,rss_url,created_at) VALUES(?,?,?,?,?)
                            ON CONFLICT(source_key) DO UPDATE SET name=excluded.name,page_url=excluded.page_url,rss_url=excluded.rss_url""",
                         (source.key, source.name, source.page_url, source.rss_url, now))
    mailer = ResendMailer(settings)
    planner = DeliveryPlanner(db, settings)
    return settings, db, Crawler(db, settings), AIWorker(db, OpenAIAnalyzer(settings), planner), planner, DeliveryWorker(db, settings, mailer), mailer


def run_initialization(db: Database, crawler: Crawler) -> dict:
    result = {}
    with db.connect() as conn:
        completed = {row["source_key"] for row in conn.execute("SELECT source_key FROM sources WHERE backfill_completed_at IS NOT NULL")}
    for source in SOURCES:
        if source.key not in completed:
            try:
                result[source.key] = crawler.run_backfill(source)
            except Exception as exc:  # recorded in crawl_runs/job_failures; retried on the next cycle
                result[source.key] = {"error": str(exc)}
    return result


def poll_sources(db: Database, crawler: Crawler) -> dict:
    """Backfill any source that has not finished it yet, then poll RSS for the rest."""
    result = run_initialization(db, crawler)
    for source in SOURCES:
        if source.key in result:
            continue
        try:
            result[source.key] = crawler.run_incremental(source)
        except Exception as exc:
            result[source.key] = {"error": str(exc)}
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pnu-notice")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    commands.add_parser("backfill")
    commands.add_parser("poll")
    ai_parser = commands.add_parser("process-ai")
    ai_parser.add_argument("--limit", type=int, default=25)
    delivery_parser = commands.add_parser("deliver")
    delivery_parser.add_argument("--limit", type=int, default=100)
    commands.add_parser("run-cycle")
    commands.add_parser("run-scheduler")
    backup = commands.add_parser("backup")
    backup.add_argument("--directory", type=Path, default=Path("backups"))
    backup.add_argument("--retention-days", type=int, default=14)
    admin = commands.add_parser("set-admin-password",
                                help="create an administrator or reset its password (read from stdin or prompt)")
    admin.add_argument("email")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    settings, db, crawler, ai_worker, planner, delivery_worker, mailer = services()
    if args.command == "init-db":
        print(f"Database initialized at {settings.database_path}")
    elif args.command == "backfill":
        print(json.dumps(run_initialization(db, crawler), ensure_ascii=False, indent=2))
    elif args.command == "poll":
        print(json.dumps(poll_sources(db, crawler), ensure_ascii=False, indent=2))
    elif args.command == "process-ai":
        print(json.dumps(ai_worker.process_pending(args.limit), ensure_ascii=False))
    elif args.command == "deliver":
        print(json.dumps(delivery_worker.process_due(args.limit), ensure_ascii=False))
    elif args.command == "run-cycle":
        result = {"crawl": poll_sources(db, crawler)}
        result["ai"] = ai_worker.process_pending(100)
        result["planning"] = planner.plan_pending(200)
        result["delivery"] = delivery_worker.process_due(500)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "run-scheduler":
        alerts = AlertWorker(db, settings, mailer)
        last_poll = None
        while True:
            if last_poll is None or time.monotonic() - last_poll >= settings.poll_interval_minutes * 60:
                last_poll = time.monotonic()
                print(f"{iso_utc()} crawl {json.dumps(poll_sources(db, crawler), ensure_ascii=False)}", flush=True)
            for name, step in (("ai", lambda: ai_worker.process_pending(50)),
                               ("planning", lambda: planner.plan_pending(100)),
                               ("delivery", lambda: delivery_worker.process_due(200)),
                               ("alerts", alerts.alert_repeated_failures)):
                try:
                    step()
                except Exception as exc:
                    print(f"{iso_utc()} {name} failed: {exc}", flush=True)
            time.sleep(60)
    elif args.command == "backup":
        target = create_backup(db, args.directory, args.retention_days)
        print(f"Backup created at {target}")
    elif args.command == "set-admin-password":
        password = getpass.getpass("新密码: ") if sys.stdin.isatty() else sys.stdin.read().strip()
        account = AccountService(db, settings, mailer).create_admin(args.email, password)
        print(f"Administrator {account['email']} saved; the password must be changed at next login.")
    elif args.command == "serve":
        subscriptions = SubscriptionService(db, settings, mailer)
        print(f"Serving on http://{args.host}:{args.port}")
        with make_server(args.host, args.port, WebApp(subscriptions)) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
