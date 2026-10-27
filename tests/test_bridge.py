import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
import telegram_bridge as bridge


class TelegramBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.day = "2026-10-27"

    def tearDown(self):
        self.temp.cleanup()

    def write_report(self, value):
        (self.root / f"{self.day}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_report_preserves_links_and_places_rtl_marks_at_line_starts(self):
        self.write_report({"date": self.day, "posts": ["🚀 خبر تازه\n\nمنبع:\nhttps://example.org/"]})
        result = bridge.load_report(self.day, self.root)
        self.assertEqual(result, ["\u200f🚀 خبر تازه\n\n\u200fمنبع:\n\u200fhttps://example.org/"])

    def test_report_rejects_mismatched_date_empty_and_overlong_posts(self):
        for value in (
            {"date": "2026-10-28", "posts": ["ok"]},
            {"date": self.day, "posts": []},
            {"date": self.day, "posts": ["  "]},
            {"date": self.day, "posts": ["😀" * 2001]},
        ):
            with self.subTest(value=value):
                self.write_report(value)
                with self.assertRaises(ValueError):
                    bridge.load_report(self.day, self.root)

    def test_partial_failure_resumes_without_resending_completed_posts(self):
        path = self.root / "state.sqlite3"
        messages = ["\u200fخبر اول", "\u200fخبر دوم"]
        first = Mock(side_effect=[None, RuntimeError("network error")])
        store = app.Store(path)
        with self.assertRaises(RuntimeError):
            bridge.deliver(self.day, messages, store, first)
        store.db.close()
        self.assertEqual(first.call_count, 2)

        second = Mock()
        store = app.Store(path)
        bridge.deliver(self.day, messages, store, second)
        self.assertEqual([call.args[0] for call in second.call_args_list], [messages[1]])
        self.assertEqual(store.run_status(self.day), "complete")
        bridge.deliver(self.day, messages, store, second)
        self.assertEqual(second.call_count, 1)
        store.db.close()

    def test_changed_report_for_existing_day_is_rejected(self):
        store = app.Store(self.root / "state.sqlite3")
        sender = Mock()
        bridge.deliver(self.day, ["first"], store, sender)
        with self.assertRaises(ValueError):
            bridge.deliver(self.day, ["different"], store, sender)
        self.assertEqual(sender.call_count, 1)
        store.db.close()

    def test_push_only_processes_one_new_valid_report(self):
        before, after = "a" * 40, "b" * 40
        with patch.object(bridge.subprocess, "run") as run:
            run.return_value.stdout = "channel_reports/2026-10-27.json\n"
            self.assertEqual(bridge.resolve_event("push", "", "", before, after), ("publish", self.day))
            self.assertIn("--diff-filter=A", run.call_args.args[0])
            run.return_value.stdout = "README.md\n"
            self.assertEqual(bridge.resolve_event("push", "", "", before, after), ("skip", ""))
            run.return_value.stdout = "channel_reports/2026-10-27.json\nchannel_reports/2026-10-28.json\n"
            with self.assertRaises(ValueError):
                bridge.resolve_event("push", "", "", before, after)

    def test_manual_check_requires_no_report_date_and_sends_nothing(self):
        self.assertEqual(bridge.resolve_event("workflow_dispatch", "check", "", "", ""), ("check", ""))
        with patch.object(bridge, "channel_config", return_value=object()), patch.object(app, "check_telegram") as check, patch.object(app, "send_telegram") as send, patch.object(bridge.sys, "argv", ["telegram_bridge.py", "check"]):
            self.assertEqual(bridge.main(), 0)
            check.assert_called_once()
            send.assert_not_called()

    def test_formatting_bolds_title_and_escapes_untrusted_html(self):
        cfg = SimpleNamespace(bot_token="dummy", chat_id="@dummy", silent=False)
        with patch.object(app, "post_json", return_value={"ok": True}) as post:
            bridge.send_formatted(cfg, "\u200fخبر <تازه>\n\u200fجزئیات & منبع")
        payload = post.call_args.args[1]
        self.assertEqual(payload["text"], "<b>\u200fخبر &lt;تازه&gt;</b>\n\u200fجزئیات &amp; منبع")
        self.assertEqual(payload["parse_mode"], "HTML")

    def test_unsafe_dates_are_rejected(self):
        for value in ("../secret", "2026-99-99", "2026-1-1", "2026-10-27\nhello"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.valid_date(value)


if __name__ == "__main__":
    unittest.main()
