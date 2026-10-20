"""離線測試：假 sender、假 AI，不打 LINE、不打 Claude。跑法：python -m unittest discover -s tests"""
import base64
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import Bot  # noqa: E402
from config import Config  # noqa: E402
from db import Store  # noqa: E402
from faq import FAQ  # noqa: E402
import line_api  # noqa: E402
from llm import ClaudeResponder  # noqa: E402

OWNER = "U" + "0" * 32
CUSTOMER = "U" + "1" * 32
NOW = 1_800_000_000.0


class FakeSender:
    def __init__(self):
        self.sent = []

    def reply(self, token, messages):
        self.sent.append(("reply", token, messages))

    def push(self, to, messages):
        self.sent.append(("push", to, messages))

    def display_name(self, user_id):
        return "測試客人" if user_id == CUSTOMER else "老闆"

    def texts_to(self, who):
        # reply 的對象是 reply token，測試裡 token 就是「tok-<userId>」
        return [m["text"] for kind, to, ms in self.sent if to in (who, "tok-" + who)
                for m in ms if m["type"] == "text"]

    def cards_to(self, who):
        return [m for kind, to, ms in self.sent if kind == "push" and to == who for m in ms if m["type"] == "flex"]


class FakeAI:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def answer(self, q, shop, kb):
        self.calls += 1
        return self.reply, {}


def msg(user, text, ts=NOW, token=None):
    return {"type": "message", "replyToken": token or "tok-" + user, "timestamp": int(ts * 1000),
            "source": {"userId": user}, "message": {"type": "text", "text": text}}


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(owner_claim_code="1234", data_dir=Path(self.tmp.name))
        self.sender = FakeSender()
        self.store = Store(cfg.db_path)
        self.bot = Bot(cfg, self.store, FAQ(ROOT / "faq.json"), self.sender, clock=lambda: NOW)
        self.bot.handle_event(msg(OWNER, "我是老闆 1234"))
        self.sender.sent.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_faq_answer_by_reply(self):
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "請問營業時間？")), "faq")
        kind, token, ms = self.sender.sent[0]
        self.assertEqual((kind, token), ("reply", "tok-" + CUSTOMER))
        self.assertIn("10:00", ms[0]["text"])

    def test_stale_token_falls_back_to_push(self):
        self.bot.handle_event(msg(CUSTOMER, "營業時間", ts=NOW - 120))
        self.assertEqual(self.sender.sent[0][:2], ("push", CUSTOMER))

    def test_unknown_question_escalates_to_owner(self):
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "你們有賣咖啡豆嗎")), "escalated")
        self.assertIn("確認", self.sender.texts_to(CUSTOMER)[0])
        cards = self.sender.cards_to(OWNER)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["contents"]["footer"]["contents"][0]["action"]["data"].startswith("decide:"))

    def test_owner_clicks_button_and_customer_gets_reply(self):
        self.bot.handle_event(msg(CUSTOMER, "可以便宜一點嗎"))
        card = self.sender.cards_to(OWNER)[0]
        data = card["contents"]["footer"]["contents"][1]["action"]["data"]  # 「可以 95 折」
        self.sender.sent.clear()
        r = self.bot.handle_event({"type": "postback", "replyToken": "t2", "timestamp": int(NOW * 1000),
                                   "source": {"userId": OWNER}, "postback": {"data": data}})
        self.assertEqual(r, "decided")
        self.assertIn("95 折", self.sender.texts_to(CUSTOMER)[0])
        r2 = self.bot.handle_event({"type": "postback", "replyToken": "t3", "timestamp": int(NOW * 1000),
                                    "source": {"userId": OWNER}, "postback": {"data": data}})
        self.assertEqual(r2, "already")

    def test_owner_free_text_forwarded(self):
        self.bot.handle_event(msg(CUSTOMER, "你們有賣咖啡豆嗎"))
        self.sender.sent.clear()
        self.assertEqual(self.bot.handle_event(msg(OWNER, "目前沒有喔，謝謝")), "owner_forward")
        self.assertEqual(self.sender.texts_to(CUSTOMER), ["目前沒有喔，謝謝"])

    def test_wait_for_typing_option(self):
        self.bot.handle_event(msg(CUSTOMER, "可以便宜一點嗎"))
        data = self.sender.cards_to(OWNER)[0]["contents"]["footer"]["contents"][2]["action"]["data"]
        self.bot.handle_event({"type": "postback", "replyToken": "t", "timestamp": int(NOW * 1000),
                               "source": {"userId": OWNER}, "postback": {"data": data}})
        self.sender.sent.clear()
        self.bot.handle_event(msg(OWNER, "老客人，幫你打 9 折"))
        self.assertEqual(self.sender.texts_to(CUSTOMER), ["老客人，幫你打 9 折"])

    def test_sensitive_word_hands_off_without_ai(self):
        ai = FakeAI("不應該被呼叫")
        self.bot.ai = ai
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "我要退款")), "handoff")
        self.assertEqual(ai.calls, 0)
        self.assertEqual(len(self.sender.cards_to(OWNER)), 1)

    def test_ai_answers_when_faq_unsure(self):
        self.bot.ai = FakeAI("有的，週末也有開喔")
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "禮拜六可以去嗎")), "ai")
        self.assertEqual(self.sender.texts_to(CUSTOMER), ["有的，週末也有開喔"])

    def test_ai_unsure_still_escalates(self):
        self.bot.ai = FakeAI(None)
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "禮拜六可以去嗎")), "escalated")

    def test_wrong_claim_code_is_rejected(self):
        self.bot.handle_event(msg(CUSTOMER, "我是老闆"))
        self.bot.handle_event(msg(CUSTOMER, "我是老闆 0000"))
        self.assertFalse(self.bot.is_owner(CUSTOMER))
        self.assertEqual(self.bot.handle_event(msg(CUSTOMER, "營業時間")), "faq")

    def test_postback_from_customer_ignored(self):
        self.bot.handle_event(msg(CUSTOMER, "你們有賣咖啡豆嗎"))
        did = self.store.open_decisions()[0]["id"]
        r = self.bot.handle_event({"type": "postback", "source": {"userId": CUSTOMER},
                                   "postback": {"data": f"decide:{did}:yes"}})
        self.assertEqual(r, "ignored")

    def test_non_text_and_follow(self):
        ev = msg(CUSTOMER, "")
        ev["message"] = {"type": "image"}
        self.assertEqual(self.bot.handle_event(ev), "unsupported")
        self.assertEqual(self.bot.handle_event({"type": "follow", "replyToken": "f", "timestamp": int(NOW * 1000),
                                                "source": {"userId": CUSTOMER}}), "follow")

    def test_owner_todo_and_reload(self):
        self.bot.handle_event(msg(CUSTOMER, "你們有賣咖啡豆嗎"))
        self.sender.sent.clear()
        self.bot.handle_event(msg(OWNER, "待辦"))
        self.assertEqual(len(self.sender.cards_to(OWNER)), 1)
        self.assertEqual(self.bot.handle_event(msg(OWNER, "重載")), "owner_reload")


class PartsTest(unittest.TestCase):
    def test_signature(self):
        body = b'{"events":[]}'
        sig = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        self.assertTrue(line_api.verify_signature("secret", body, sig))
        self.assertFalse(line_api.verify_signature("secret", body, "bad"))
        self.assertFalse(line_api.verify_signature("", body, sig))

    def test_long_text_split(self):
        ms = line_api.text_messages("字" * 10000)
        self.assertEqual([len(m["text"]) for m in ms], [4900, 4900, 200])

    def test_faq_scores(self):
        faq = FAQ(ROOT / "faq.json")
        self.assertEqual(faq.match("營業時間")[2], 0.97)
        self.assertEqual(faq.match("今天天氣如何")[0], None)
        self.assertIn("示範茶飲", faq.knowledge_text())

    def test_claude_responder_with_stub_client(self):
        calls = []

        def create(**kw):
            calls.append(kw)
            text = "ESCALATE" if "退" in kw["messages"][0]["content"] else "可以喔"
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                                   usage=SimpleNamespace(cache_read_input_tokens=1200))

        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        r = ClaudeResponder("x", "claude-sonnet-5-5", client=client)
        text, info = r.answer("週末有開嗎", "示範茶飲", "KB")
        self.assertEqual(text, "可以喔")
        self.assertEqual(info["cache_read_input_tokens"], 1200)
        self.assertEqual(calls[0]["system"][1]["cache_control"], {"type": "ephemeral"})
        self.assertIsNone(r.answer("能退嗎", "示範茶飲", "KB")[0])

    def test_webhook_queues_and_rejects_bad_signature(self):
        try:
            import app as app_mod
        except ImportError:
            self.skipTest("沒裝 flask")
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(channel_secret="secret", owner_claim_code="1", data_dir=Path(d))
            handled = []
            fake_bot = SimpleNamespace(handle_event=lambda ev: handled.append(ev))
            flask_app = app_mod.create_app(cfg, fake_bot)
            c = flask_app.test_client()
            body = json.dumps({"events": [msg(CUSTOMER, "hi")]}).encode()
            self.assertEqual(c.post("/callback", data=body, headers={"X-Line-Signature": "bad"}).status_code, 400)
            sig = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
            self.assertEqual(c.post("/callback", data=body, headers={"X-Line-Signature": sig}).status_code, 200)
            flask_app.config["JOBS"].join()
            self.assertEqual(len(handled), 1)


if __name__ == "__main__":
    unittest.main()
