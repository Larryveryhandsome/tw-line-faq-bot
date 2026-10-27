"""#11：老闆名單／移除老闆指令、客人訊息保留期限。跑法：python -m unittest discover -s tests"""
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot import Bot  # noqa: E402
from config import Config, from_env  # noqa: E402
from db import Store  # noqa: E402
from faq import FAQ  # noqa: E402
from test_bot import CUSTOMER, NOW, OWNER, FakeSender, msg  # noqa: E402

OWNER2 = "U" + "7" * 32


class OwnerAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(owner_claim_code="abcd1234", data_dir=Path(self.tmp.name))
        self.sender = FakeSender()
        self.store = Store(self.cfg.db_path)
        self.bot = Bot(self.cfg, self.store, FAQ(ROOT / "faq.json"), self.sender, clock=lambda: NOW)
        for o in (OWNER, OWNER2):
            self.bot.handle_event(msg(o, "我是老闆 abcd1234"))
        self.sender.sent.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def last(self, who):
        return self.sender.texts_to(who)[-1]

    def test_list_shows_owners_and_marks_self(self):
        self.assertEqual(self.bot.handle_event(msg(OWNER, "老闆名單")), "owner_list")
        t = self.last(OWNER)
        self.assertIn("1.", t)
        self.assertIn("2.", t)
        self.assertIn("（你）", t)
        self.assertNotIn(OWNER, t)  # 不印完整 userId

    def test_remove_owner_demotes_and_is_no_longer_owner(self):
        idx = self.bot.owners().index(OWNER2) + 1
        self.assertEqual(self.bot.handle_event(msg(OWNER, f"移除老闆 {idx}")), "owner_remove")
        self.assertIn("已移除", self.last(OWNER))
        self.assertFalse(self.bot.is_owner(OWNER2))
        # 被移除的人傳老闆指令，現在只是一般客人：不會看到老闆名單
        self.sender.sent.clear()
        self.assertNotEqual(self.bot.handle_event(msg(OWNER2, "老闆名單")), "owner_list")
        self.assertNotEqual(self.bot.handle_event(msg(OWNER2, "移除老闆 1")), "owner_remove")
        self.assertTrue(self.bot.is_owner(OWNER))

    def test_cannot_remove_last_owner(self):
        idx = self.bot.owners().index(OWNER2) + 1
        self.bot.handle_event(msg(OWNER, f"移除老闆 {idx}"))
        self.bot.handle_event(msg(OWNER, "移除老闆 1"))
        self.assertIn("最後一位", self.last(OWNER))
        self.assertTrue(self.bot.is_owner(OWNER))

    def test_bad_argument(self):
        for arg in ("", "abc", "0", "99", "-1"):
            self.bot.handle_event(msg(OWNER, f"移除老闆 {arg}"))
            self.assertIn("編號", self.last(OWNER))
        self.assertEqual(len(self.bot.owners()), 2)

    def test_customer_cannot_use_owner_commands(self):
        for cmd in ("老闆名單", "移除老闆 1", "移除老闆 2"):
            r = self.bot.handle_event(msg(CUSTOMER, cmd))
            self.assertNotIn(r, ("owner_list", "owner_remove"))
        self.assertEqual(len(self.bot.owners()), 2)
        self.assertNotIn("目前的老闆", " ".join(self.sender.texts_to(CUSTOMER)))

    def test_env_fixed_owner_cannot_be_removed_by_command(self):
        self.cfg.owner_user_id = OWNER2
        idx = self.bot.owners().index(OWNER2) + 1
        self.bot.handle_event(msg(OWNER, f"移除老闆 {idx}"))
        self.assertIn(".env", self.last(OWNER))
        self.assertTrue(self.bot.is_owner(OWNER2))

    def test_remove_self_allowed_when_others_remain(self):
        idx = self.bot.owners().index(OWNER) + 1
        self.bot.handle_event(msg(OWNER, f"移除老闆 {idx}"))
        self.assertFalse(self.bot.is_owner(OWNER))
        self.assertTrue(self.bot.is_owner(OWNER2))


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bot.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _age(self, table, rowid, days):
        old = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self.store.conn() as c:
            c.execute(f"UPDATE {table} SET created_at=? WHERE id=?", (old, rowid))

    def test_default_is_30_days(self):
        self.assertEqual(Config().retention_days, 30)

    def test_env_override(self):
        import os
        os.environ["BOT_RETENTION_DAYS"] = "7"
        try:
            self.assertEqual(from_env().retention_days, 7)
            os.environ["BOT_RETENTION_DAYS"] = "0"
            self.assertEqual(from_env().retention_days, 0)
        finally:
            del os.environ["BOT_RETENTION_DAYS"]

    def test_purge_removes_old_keeps_recent_and_open_decisions(self):
        self.store.log(CUSTOMER, "in", "舊訊息", "x", 1)
        self.store.log(CUSTOMER, "in", "新訊息", "x", 1)
        old_id, new_id = [r["id"] for r in self.store.messages()]
        self._age("messages", old_id, 31)
        self._age("messages", new_id, 29)
        d_old_res = self.store.create_decision(CUSTOMER, "舊已結案", "ctx", [])
        self.store.set_decision(d_old_res, "resolved")
        d_old_open = self.store.create_decision(CUSTOMER, "舊未結案", "ctx", [])
        d_new_res = self.store.create_decision(CUSTOMER, "新已結案", "ctx", [])
        self.store.set_decision(d_new_res, "resolved")
        for d in (d_old_res, d_old_open):
            self._age("decisions", d, 40)
        m, d = self.store.purge_old(30)
        self.assertEqual((m, d), (1, 1))
        self.assertEqual([r["text"] for r in self.store.messages()], ["新訊息"])
        self.assertIsNone(self.store.get_decision(d_old_res))
        self.assertIsNotNone(self.store.get_decision(d_old_open))
        self.assertIsNotNone(self.store.get_decision(d_new_res))

    def test_zero_keeps_forever(self):
        self.store.log(CUSTOMER, "in", "很舊", "x", 1)
        self._age("messages", self.store.messages()[0]["id"], 3650)
        self.assertEqual(self.store.purge_old(0), (0, 0))
        self.assertEqual(len(self.store.messages()), 1)

    def test_purge_runs_at_startup(self):
        import app
        self.store.log(CUSTOMER, "in", "舊", "x", 1)
        self._age("messages", self.store.messages()[0]["id"], 100)
        cfg = Config(data_dir=Path(self.tmp.name))
        app.start_purge_loop(cfg, self.store, interval=3600)
        self.assertEqual(self.store.messages(), [])


if __name__ == "__main__":
    unittest.main()
