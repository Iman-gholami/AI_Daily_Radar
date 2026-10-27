#!/usr/bin/env python3
"""Relay a ChatGPT-authored, public report to one Telegram channel."""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

import app

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "channel_reports"
REPORT_PATH = re.compile(r"channel_reports/(\d{4}-\d{2}-\d{2})\.json\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
RLM = "\u200f"
LOG = logging.getLogger("telegram_bridge")


def valid_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("report date must use YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid report date") from None
    return value


def rtl_message(text: str) -> str:
    # Every nonempty paragraph begins with an RTL mark, including emoji lines.
    return "\n".join((line if line.startswith(RLM) else RLM + line) if line.strip() else "" for line in text.strip().splitlines())


def load_report(day: str, root: Path = REPORTS) -> list[str]:
    valid_date(day)
    path = root / f"{day}.json"
    data = path.read_bytes()
    if len(data) > 100_000:
        raise ValueError("report file is too large")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict) or value.get("date") != day:
        raise ValueError("report date does not match its filename")
    posts = value.get("posts")
    if not isinstance(posts, list) or not 1 <= len(posts) <= 10:
        raise ValueError("a report must contain 1 to 10 posts")
    messages = []
    for post in posts:
        if not isinstance(post, str) or not post.strip():
            raise ValueError("each post must be nonempty text")
        text = rtl_message(post)
        if len(text.encode("utf-16-le")) // 2 > 4000:
            raise ValueError("post exceeds the Telegram-safe length")
        messages.append(text)
    return messages


def resolve_event(event: str, operation: str, date_input: str, before: str, after: str) -> tuple[str, str]:
    if event == "workflow_dispatch":
        if operation == "check":
            return "check", ""
        if operation == "publish":
            return "publish", valid_date(date_input)
        raise ValueError("unsupported operation")
    if event != "push" or not SHA.fullmatch(before) or not SHA.fullmatch(after):
        raise ValueError("unsupported or invalid push event")
    if before == "0" * 40:
        command = ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "--diff-filter=A", "-r", after, "--", "channel_reports/"]
    else:
        command = ["git", "diff", "--name-only", "--diff-filter=A", before, after, "--", "channel_reports/"]
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    days = [m.group(1) for line in result.stdout.splitlines() if (m := REPORT_PATH.fullmatch(line))]
    if not days:
        return "skip", ""
    if len(days) != 1:
        raise ValueError("publish exactly one new report per push")
    return "publish", valid_date(days[0])


def channel_config() -> app.Config:
    cfg = app.Config.load()
    if not cfg.bot_token or not cfg.chat_id or cfg.chat_id == "@your_channel":
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured as GitHub secrets")
    return cfg


def send_formatted(cfg: app.Config, message: str) -> None:
    # Only the first line is styled; source text is escaped as data, not HTML.
    first, sep, rest = message.partition("\n")
    text = "<b>" + html.escape(first) + "</b>" + (sep + html.escape(rest) if sep else "")
    result = app.post_json(
        f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage",
        {"chat_id": cfg.chat_id, "text": text, "parse_mode": "HTML", "disable_notification": cfg.silent, "link_preview_options": {"is_disabled": True}},
    )
    if result.get("ok") is not True:
        raise RuntimeError("Telegram did not accept the message")


def deliver(day: str, messages: list[str], store: app.Store, sender) -> None:
    rows = store.db.execute("SELECT seq,text FROM outbox WHERE day=? ORDER BY seq", (day,)).fetchall()
    if rows:
        if [text for _, text in rows] != messages:
            raise ValueError("this date already has a different report; refusing to resend")
    else:
        store.save_outbox(day, [(text, []) for text in messages])
    for pending_day, seq, text, keys in store.pending():
        if pending_day != day:
            continue
        sender(text)
        store.mark_sent(day, seq, keys)
        LOG.info("Delivered post %s for %s", seq + 1, day)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "resolve":
            operation, day = resolve_event(os.getenv("GITHUB_EVENT_NAME", ""), os.getenv("INPUT_OPERATION", ""), os.getenv("INPUT_REPORT_DATE", ""), os.getenv("PUSH_BEFORE", ""), os.getenv("GITHUB_SHA", ""))
            print(f"operation={operation}\nreport_date={day}")
            return 0
        if len(sys.argv) == 2 and sys.argv[1] == "check":
            app.check_telegram(channel_config())
            LOG.info("Channel connection and posting permission verified; no message was sent")
            return 0
        if len(sys.argv) == 3 and sys.argv[1] == "publish":
            day = valid_date(sys.argv[2])
            messages = load_report(day)
            cfg = channel_config()
            app.check_telegram(cfg)
            with app.exclusive_lock(cfg.data_dir):
                store = app.Store(cfg.data_dir / "telegram_bridge.sqlite3")
                try:
                    deliver(day, messages, store, lambda message: send_formatted(cfg, message))
                    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    store.db.close()
            return 0
        raise ValueError("usage: telegram_bridge.py resolve | check | publish YYYY-MM-DD")
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # app.post_json hides the Telegram token in network error messages.
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
