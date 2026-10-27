import datetime as dt
import http.server
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 27, 9, tzinfo=dt.timezone.utc)
        self.since = self.now - dt.timedelta(hours=48)

    def test_feed_filters_dates_and_non_official_links(self):
        feed = b'''<rss><channel>
          <item><title>New release</title><link>https://openai.com/news/new?utm_source=test</link><pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate></item>
          <item><title>Too old</title><link>https://openai.com/news/old</link><pubDate>Thu, 22 Oct 2026 12:00:00 GMT</pubDate></item>
          <item><title>Bad link</title><link>https://attacker.invalid/news</link><pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate></item>
          <item><title>No date</title><link>https://openai.com/news/unknown</link></item>
          </channel></rss>'''
        result = app.parse_feed(feed, 'OpenAI', ('openai.com',), self.since, self.now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, 'https://openai.com/news/new')

    def test_video_feed_requires_valid_id_and_publication_time(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
          <entry><yt:videoId>abcdefghijk</yt:videoId><title>Agent tutorial</title><published>2026-10-26T10:00:00Z</published></entry>
          <entry><yt:videoId>bad</yt:videoId><title>Invalid</title><published>2026-10-26T10:00:00Z</published></entry>
          </feed>'''
        result = app.parse_yt_feed(feed, 'Example', self.since, self.now)
        self.assertEqual([i.video_id for i in result], ['abcdefghijk'])
        self.assertEqual(result[0].url, 'https://www.youtube.com/watch?v=abcdefghijk')

    def test_untrusted_model_link_does_not_replace_source(self):
        item = app.Item('a', 'article', 'title', 'https://openai.com/news/real', 'OpenAI', self.now)
        data = app.parse_model_json('{"relevant":true,"title_fa":"تیتر https://fake.test", "points_fa":["خلاصه https://fake.test"], "why_fa":"مفید"}')
        text = app.render_entry(item, data, 'متن مقاله')
        self.assertIn('https://openai.com/news/real', text)
        self.assertNotIn('fake.test', text)
        self.assertFalse(app.parse_classification('{"relevant":false}'))
        self.assertTrue(app.parse_classification('{"relevant":true}'))

    def test_unavailable_transcript_is_not_summarized_from_title(self):
        item = app.Item('video:abcdefghijk', 'video', 'Claim in title', 'https://www.youtube.com/watch?v=abcdefghijk', 'Example', self.now, 'abcdefghijk')
        text = app.render_entry(item, None, 'زیرنویس در دسترس نبود؛ خلاصه‌ای تولید نشد')
        self.assertIn('خلاصه‌ای تولید نشد', text)
        self.assertIn(item.url, text)

    def test_chunking_preserves_all_text(self):
        text = ('a' * 130 + '\n') * 5 + 'z' * 400
        chunks = app.chunks_by_line(text, 150)
        self.assertTrue(all(len(c) <= 150 for c in chunks))
        self.assertEqual(''.join(chunks).replace('\n', ''), text.replace('\n', ''))

    def test_outbox_resume_never_marks_unsent_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = app.Store(Path(tmp) / 'db.sqlite')
            store.save_outbox('2026-10-27', [('part one', ['a']), ('part two', ['b'])])
            first = store.pending()[0]
            store.mark_sent(first[0], first[1], first[3])
            self.assertTrue(store.seen('a'))
            self.assertFalse(store.seen('b'))
            self.assertEqual(len(store.pending()), 1)
            self.assertEqual(store.run_status('2026-10-27'), 'pending')
            second = store.pending()[0]
            store.mark_sent(second[0], second[1], second[3])
            self.assertTrue(store.seen('b'))
            self.assertEqual(store.run_status('2026-10-27'), 'complete')

    def test_budget_is_persistent_across_retries_and_resets_next_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite'
            one = app.Store(path)
            one.reserve_call('2026-10-27', 2)
            two = app.Store(path)
            two.reserve_call('2026-10-27', 2)
            with self.assertRaises(RuntimeError):
                two.reserve_call('2026-10-27', 2)
            self.assertEqual(one.calls_used('2026-10-27'), 2)
            two.reserve_call('2026-10-28', 2)
            self.assertEqual(two.calls_used('2026-10-28'), 1)

    def test_packing_respects_telegram_limit_and_keeps_links(self):
        parts = app.pack_messages('2026-10-27', [(str(i), ('x' * 1300) + f' https://example.com/{i}') for i in range(6)], ['feed'], 3400)
        self.assertTrue(all(len(text) <= 3400 for text, _ in parts))
        self.assertEqual(sum(len(keys) for _, keys in parts), 6)
        for i in range(6):
            self.assertIn(f'https://example.com/{i}', '\n'.join(text for text, _ in parts))
        self.assertIn('پوشش امروز کامل نیست', parts[-1][0])

    def test_daily_pipeline_retries_only_unsent_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = app.Store(Path(tmp) / 'db.sqlite')
            store.save_outbox('2026-10-27', [('first', ['a']), ('second', ['b'])])
            sent = []
            def flaky(cfg, message):
                if message == 'second':
                    raise RuntimeError('network failure')
                sent.append(message)
            with patch('app.send_telegram', side_effect=flaky):
                with self.assertRaises(RuntimeError):
                    app.flush(store, object())
            with patch('app.send_telegram', side_effect=lambda cfg, text: sent.append(text)):
                app.flush(store, object())
            self.assertEqual(sent, ['first', 'second'])
            self.assertEqual(store.run_status('2026-10-27'), 'complete')

    def test_complete_daily_flow_publishes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = app.Config('token', '@channel', 'http://localhost:9999/v1', '', 'test-model', '', app.ZoneInfo('Asia/Tehran'), dt.time(9, 0), Path(tmp), 48, 2, 2, 2, 20, False, llm_min_interval=0)
            items = [
                app.Item('article:one', 'article', 'New LLM release', 'https://openai.com/news/one', 'OpenAI', self.now, allowed_hosts=('openai.com',)),
                app.Item('video:abcdefghijk', 'video', 'AI agent update', 'https://www.youtube.com/watch?v=abcdefghijk', 'Example', self.now, 'abcdefghijk')
            ]
            sent = []
            def model_response(url, payload, headers=None, timeout=90):
                system = payload['messages'][0]['content']
                if system == app.CLASSIFY_SYSTEM:
                    result = '{"relevant":true}'
                else:
                    result = '{"relevant":true,"title_fa":"خبر تازه","points_fa":["خلاصه واقعی"],"why_fa":"کاربرد مهندسی"}'
                return {'choices': [{'message': {'content': result}}]}
            with patch('app.utc_now', return_value=self.now), \
                 patch('app.collect', return_value=(items, [], 2)), \
                 patch('app.article_text', return_value=('Article body' * 30, False)), \
                 patch('app.transcript_text', return_value=('Transcript body' * 30, False, 'خودکار')), \
                 patch('app.post_json', side_effect=model_response), \
                 patch('app.send_telegram', side_effect=lambda cfg, message: sent.append(message)):
                app.run_once(cfg)
                app.run_once(cfg)
            self.assertEqual(len(sent), 1)
            self.assertIn('https://openai.com/news/one', sent[0])
            self.assertIn('https://www.youtube.com/watch?v=abcdefghijk', sent[0])
            store = app.Store(Path(tmp) / 'radar.sqlite3')
            self.assertEqual(store.run_status('2026-10-27'), 'complete')
            self.assertTrue(store.seen('article:one'))
            self.assertTrue(store.seen('video:abcdefghijk'))

    def test_free_only_prevents_paid_remote_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = app.Config('token', '@channel', 'https://openrouter.ai/api/v1', 'fake-key', 'example/paid-model', '', app.ZoneInfo('Asia/Tehran'), dt.time(9), Path(tmp), 48, 2, 2, 2, 20, False)
            with self.assertRaisesRegex(ValueError, 'FREE_ONLY'):
                cfg.validate()
            cfg.llm_model = 'openrouter/free'
            cfg.validate()
            cfg.llm_model = 'example/model:free'
            cfg.validate()
            cfg.llm_base = 'https://api.example.com/v1'
            with self.assertRaisesRegex(ValueError, 'FREE_ONLY'):
                cfg.validate()

    def test_fallback_uses_only_native_captions_and_respects_monthly_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = app.Config('token', '@channel', 'http://localhost:9999/v1', '', 'model', '', app.ZoneInfo('Asia/Tehran'), dt.time(9), Path(tmp), 48, 2, 2, 2, 20, False, supadata_key='fake', max_supadata_daily=2, max_supadata_monthly=2, llm_min_interval=0)
            store = app.Store(Path(tmp) / 'db.sqlite')
            response = {'content': [{'text': 'caption text ' * 20, 'offset': 8300}]}
            with patch('app.direct_transcript_text', side_effect=RuntimeError('blocked')), patch('app.supadata_get', return_value=response) as api:
                text, partial, origin = app.transcript_text('abcdefghijk', 5000, cfg, store, '2026-10-27')
                self.assertIn('[0:08]', text)
                self.assertFalse(partial)
                self.assertEqual(api.call_args.args[1]['mode'], 'native')
                app.transcript_text('abcdefghijk', 5000, cfg, store, '2026-10-27')
                with self.assertRaisesRegex(RuntimeError, 'limit'):
                    app.transcript_text('abcdefghijk', 5000, cfg, store, '2026-10-28')
                self.assertEqual(api.call_count, 2)

    def test_supadata_async_job_is_polled_without_additional_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = app.Config('token', '@channel', 'http://localhost:9999/v1', '', 'model', '', app.ZoneInfo('Asia/Tehran'), dt.time(9), Path(tmp), 48, 2, 2, 2, 20, False, supadata_key='fake', max_supadata_daily=1, max_supadata_monthly=1, llm_min_interval=0)
            store = app.Store(Path(tmp) / 'db.sqlite')
            responses = [{'jobId': 'job-1'}, {'status': 'active'}, {'status': 'completed', 'content': 'transcript ' * 30}]
            with patch('app.direct_transcript_text', side_effect=RuntimeError('blocked')), patch('app.supadata_get', side_effect=responses) as api, patch('app.time.sleep'):
                text, partial, _ = app.transcript_text('abcdefghijk', 5000, cfg, store, '2026-10-27')
            self.assertIn('transcript', text)
            self.assertEqual(api.call_count, 3)
            with self.assertRaisesRegex(RuntimeError, 'limit'):
                store.reserve_supadata('2026-10-27', 1, 1)

    def test_telegram_preflight_checks_post_permission_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = app.Config('private-token', '@channel', 'http://localhost:9999/v1', '', 'model', '', app.ZoneInfo('Asia/Tehran'), dt.time(9), Path(tmp), 48, 2, 2, 2, 20, False, llm_min_interval=0)
            results = [{'ok': True, 'result': {'id': 1}}, {'ok': True, 'result': {'type': 'channel'}}, {'ok': True, 'result': {'status': 'administrator', 'can_post_messages': True}}]
            with patch('app.post_json', side_effect=results) as api:
                app.check_telegram(cfg)
            self.assertEqual([c.args[0].rsplit('/', 1)[-1] for c in api.call_args_list], ['getMe', 'getChat', 'getChatMember'])

    def test_http_client_sends_json_and_rejects_redirects(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/leak')
                    self.end_headers()
                    return
                size = int(self.headers.get('Content-Length', 0))
                payload = json.loads(self.rfile.read(size))
                response = json.dumps({'received': payload}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(response)
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(app.post_json(root + '/ok', {'value': 'آزمایش'})['received']['value'], 'آزمایش')
            with self.assertRaises(app.RemoteError) as exc:
                app.post_json(root + '/redirect', {}, {'Authorization': 'Bearer test-secret'})
            self.assertEqual(exc.exception.status, 302)
            self.assertNotIn('/leak', seen)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_authentication_failure_stops_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = app.Config('token', '@channel', 'http://localhost:9999/v1', '', 'test-model', '', app.ZoneInfo('Asia/Tehran'), dt.time(9), Path(tmp), 48, 5, 2, 2, 20, False, llm_min_interval=0)
            store = app.Store(Path(tmp) / 'db.sqlite')
            items = [app.Item(f'article:{i}', 'article', 'AI model update', f'https://openai.com/news/{i}', 'OpenAI', self.now, allowed_hosts=('openai.com',)) for i in range(3)]
            with patch('app.collect', return_value=(items, [], 1)), patch('app.article_text', return_value=('article text' * 30, False)), patch('app.post_json', side_effect=app.RemoteError(401)) as model:
                with self.assertRaisesRegex(RuntimeError, 'authentication'):
                    app.make_report(cfg, store, self.now)
            self.assertEqual(model.call_count, 1)

    def test_redirect_validation_rejects_other_hosts_and_http(self):
        self.assertTrue(app.allowed_url('https://openai.com/news', ('openai.com',)))
        self.assertFalse(app.allowed_url('http://openai.com/news', ('openai.com',)))
        self.assertFalse(app.allowed_url('https://openai.com.attacker.test/', ('openai.com',)))
        self.assertFalse(app.allowed_url('https://openai.com@attacker.test/', ('openai.com',)))


if __name__ == '__main__':
    unittest.main()
