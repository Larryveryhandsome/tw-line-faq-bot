"""第二輪檢視補的測試：冪等、暴力破解、額度、AI 失敗、併發。跑法：python -m unittest discover -s tests"""
import base64
import contextlib
import hashlib
import hmac
import io
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import line_api  # noqa: E402
from bot import Bot  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from db import Store  # noqa: E402
from faq import FAQ  # noqa: E402
from test_bot import CUSTOMER, NOW, OWNER, FakeAI, FakeSender, msg  # noqa: E402


def ev(user, text, eid, redelivery=False):
    e = msg(user, text)
    e["webhookEventId"] = eid
    e["deliveryContext"] = {"isRedelivery": redelivery}
    return e


def postback(user, data):
    return {"type": "postback", "replyToken": "t", "timestamp": int(NOW * 1000),
            "source": {"userId": user}, "postback": {"data": data}}


class HardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(owner_claim_code="abcd1234", data_dir=Path(self.tmp.name))
        self.sender = FakeSender()
        self.store = Store(self.cfg.db_path)
        self.now = NOW
        self.bot = Bot(self.cfg, self.store, FAQ(ROOT / "faq.json"), self.sender, clock=lambda: self.now)
        self.bot.handle_event(msg(OWNER, "我是老闆 abcd1234"))
        self.sender.sent.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_redelivered_event_is_idempotent(self):
        """LINE 重送同一個 webhookEventId：不重複回覆、不重複推卡給老闆、不重複呼叫 Claude。"""
        ai = FakeAI(None)
        self.bot.ai = ai
        self.assertEqual(self.bot.handle_event(ev(CUSTOMER, "你們有賣咖啡豆嗎", "E1")), "escalated")
        n_sent, n_ai = len(self.sender.sent), ai.calls
        for _ in range(3):
            self.assertEqual(self.bot.handle_event(ev(CUSTOMER, "你們有賣咖啡豆嗎", "E1", True)), "duplicate")
        self.assertEqual(len(self.sender.sent), n_sent)
        self.assertEqual(ai.calls, n_ai)
        self.assertEqual(len(self.store.open_decisions()), 1)

    def test_concurrent_redelivery_only_processed_once(self):
        results, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            results.append(self.bot.handle_event(ev(CUSTOMER, "你們有賣咖啡豆嗎", "E2", True)))

        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("duplicate"), 7)
        self.assertEqual(len(self.store.open_decisions()), 1)

    def test_claim_code_bruteforce_locked(self):
        guess = "U" + "9" * 32
        for i in range(5):
            self.bot.handle_event(msg(guess, f"我是老闆 wrong{i}"))
        self.sender.sent.clear()
        self.bot.handle_event(msg(guess, "我是老闆 abcd1234"))  # 鎖住後連正確的也暫時不收
        self.assertFalse(self.bot.is_owner(guess))
        self.assertIn("次數太多", self.sender.texts_to(guess)[0])
        self.now += 16 * 60
        self.bot.handle_event(msg(guess, "我是老闆 abcd1234", ts=self.now))
        self.assertTrue(self.bot.is_owner(guess))

    def test_claim_global_limit_blocks_distributed_guessing(self):
        for i in range(30):
            self.bot.handle_event(msg("U" + f"{i:032d}", "我是老闆 nope"))
        late = "U" + "8" * 32
        self.bot.handle_event(msg(late, "我是老闆 abcd1234"))
        self.assertFalse(self.bot.is_owner(late))

    def test_short_claim_code_rejected_by_config(self):
        with self.assertRaises(ConfigError):
            Config(channel_secret="s", access_token="t", owner_claim_code="1234").require_line()
        Config(channel_secret="s", access_token="t", owner_claim_code="abcd1234").require_line()

    def test_push_quota_exhausted_does_not_pretend_sent(self):
        """push 回 429（本月額度用完）：不能告訴老闆「已回覆對方」，決策要留在待辦。"""
        self.bot.handle_event(msg(CUSTOMER, "可以便宜一點嗎"))
        data = self.sender.cards_to(OWNER)[0]["contents"]["footer"]["contents"][1]["action"]["data"]
        self.sender.push_status = 429
        self.sender.sent.clear()
        self.assertEqual(self.bot.handle_event(postback(OWNER, data)), "decide_failed")
        self.assertEqual(len(self.store.open_decisions()), 1)
        self.assertIn("沒送出去", self.sender.sent[-1][2][0]["text"])
        self.sender.push_status = 200  # 額度恢復後可以再按一次
        self.assertEqual(self.bot.handle_event(postback(OWNER, data)), "decided")

    def test_owner_todo_works_without_push_quota(self):
        self.bot.handle_event(msg(CUSTOMER, "你們有賣咖啡豆嗎"))
        self.sender.push_status = 429
        self.sender.sent.clear()
        self.bot.handle_event(msg(OWNER, "待辦"))
        self.assertEqual(self.sender.sent[0][0], "reply")
        self.assertEqual(len(self.sender.cards_to(OWNER)), 1)

    def test_owner_free_text_push_failure_keeps_decision(self):
        self.bot.handle_event(msg(CUSTOMER, "你們有賣咖啡豆嗎"))
        self.sender.push_status = 429
        self.assertEqual(self.bot.handle_event(msg(OWNER, "沒有喔")), "owner_forward_failed")
        self.assertEqual(len(self.store.open_decisions()), 1)

    def test_escalation_spam_is_throttled(self):
        for i in range(10):
            self.bot.handle_event(msg(CUSTOMER, f"奇怪的問題{i}號"))
        self.assertEqual(len(self.store.open_decisions()), 10)  # 都記錄，待辦看得到
        self.assertEqual(len(self.sender.cards_to(OWNER)), 3)   # 但只推前 3 張

    def test_ai_quota_per_user_and_daily(self):
        self.bot.cfg.ai_per_user_hour = 2
        ai = FakeAI(None)
        self.bot.ai = ai
        for i in range(5):
            self.bot.handle_event(msg(CUSTOMER, f"奇怪的問題{i}號"))
        self.assertEqual(ai.calls, 2)
        other = "U" + "5" * 32
        self.bot.cfg.ai_daily_cap = 3
        self.bot.handle_event(msg(other, "另一個奇怪問題"))
        self.bot.handle_event(msg(other, "另一個奇怪問題二"))
        self.assertEqual(ai.calls, 3)

    def test_ai_error_logged_and_circuit_breaker(self):
        class Boom:
            calls = 0

            def answer(self, *a):
                Boom.calls += 1
                e = RuntimeError("rate limited")
                e.status_code = 429
                raise e

        self.bot.ai = Boom()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "奇怪的問題甲")), "escalated")
            self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "奇怪的問題乙")), "escalated")
        self.assertEqual(Boom.calls, 1)  # 第二則不再等 Claude
        self.assertIn("AI 失敗", out.getvalue())
        self.assertIn("429", out.getvalue())
        self.now += 61
        with contextlib.redirect_stdout(io.StringIO()):
            self.bot.handle_event(msg(CUSTOMER, "奇怪的問題丙"))
        self.assertEqual(Boom.calls, 2)

    def test_reply_token_400_falls_back_to_push(self):
        class Flaky(FakeSender):
            def reply(self, token, messages):
                self.sent.append(("reply", token, messages))
                return 400, "Invalid reply token"

        self.bot.sender = Flaky()
        self.bot.handle_event(msg(CUSTOMER, "請問營業時間？"))
        self.assertEqual([k for k, *_ in self.bot.sender.sent], ["reply", "push"])

    def test_broken_faq_reload_keeps_old_data(self):
        faq_copy = Path(self.tmp.name) / "faq.json"
        faq_copy.write_text((ROOT / "faq.json").read_text(encoding="utf-8"), encoding="utf-8")
        self.bot.faq = FAQ(faq_copy)
        faq_copy.write_text('{"entries": [{"intent": "x"}]}', encoding="utf-8")
        self.assertEqual(self.bot.handle_event(msg(OWNER, "重載")), "owner_reload_failed")
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "請問營業時間？")), "faq")
        faq_copy.write_text("{壞掉", encoding="utf-8")
        self.assertEqual(self.bot.handle_event(msg(OWNER, "重載")), "owner_reload_failed")

    def test_empty_keyword_does_not_handoff_everyone(self):
        self.bot.faq.data["handoff_keywords"] = ["", "退款"]
        self.assertEqual(self.bot.faq.sensitive_hits("請問營業時間"), [])

    def test_malformed_postback_ignored(self):
        for data in ("decide:", "decide:abc:x", "decide:1"):
            self.assertEqual(self.bot.handle_event(postback(OWNER, data)), "ignored")

    def test_concurrent_customers_sqlite(self):
        """30 位客人同時傳訊：不丟事件、不出現 database is locked。"""
        errors, barrier = [], threading.Barrier(30)

        def go(i):
            try:
                barrier.wait()
                self.bot.handle_event(msg("U" + f"{i + 100:032d}", f"沒聽過的問題{i}"))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(30)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.open_decisions()), 30)

    def test_webhook_rejects_bad_json_and_oversize(self):
        import app as app_mod
        cfg = Config(channel_secret="secret", owner_claim_code="abcd1234", data_dir=Path(self.tmp.name))
        c = app_mod.create_app(cfg, SimpleNamespace(handle_event=lambda e: None)).test_client()

        def post(body):
            sig = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
            return c.post("/callback", data=body, headers={"X-Line-Signature": sig}).status_code

        self.assertEqual(post(b"not json"), 400)
        self.assertEqual(post(b"x" * (1024 * 1024 + 10)), 413)

    def test_push_retries_use_same_retry_key(self):
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                seen.append(self.headers.get("X-Line-Retry-Key"))
                self.send_response(503 if len(seen) == 1 else 200)
                self.send_header("Content-Length", "2")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"{}")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old, old_sleep = line_api.API, line_api.time.sleep
        line_api.API = f"http://127.0.0.1:{srv.server_port}/v2/bot"
        line_api.time.sleep = lambda s: None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                status, _ = line_api.LineSender("t").push(CUSTOMER, [{"type": "text", "text": "x"}])
        finally:
            line_api.API, line_api.time.sleep = old, old_sleep
            srv.shutdown()
        self.assertEqual(status, 200)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0] and seen[0] == seen[1])

    def test_llm_falls_back_when_sdk_lacks_output_config(self):
        from llm import ClaudeResponder

        def create(**kw):
            if "output_config" in kw:
                raise TypeError("unexpected keyword argument 'output_config'")
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="好的")],
                                   usage=None)

        r = ClaudeResponder("x", "m", client=SimpleNamespace(messages=SimpleNamespace(create=create)))
        self.assertEqual(r.answer("q", "店", "KB")[0], "好的")


if __name__ == "__main__":
    unittest.main()
