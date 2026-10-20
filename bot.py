"""客服大腦：一個 LINE event 進來，決定怎麼回。

客人：敏感詞 → 轉真人；FAQ 有把握 → 直接回；
     沒把握 → （有設 AI）Claude 依知識庫補答 → 還是不行就推按鈕卡片給老闆。
老闆：「我是老闆 <認證碼>」綁定；「待辦」「重載」「說明」；直接打字＝回給最近在等的客人。

所有外部相依（sender、AI）都從建構子注入，測試不打真實 API。
"""
import hmac
import json
import time
from datetime import datetime, timedelta

from faq import DEFAULT_OPTIONS
from line_api import decision_card, text_messages

CLAIM_WORD = "我是老闆"
CLAIM_WINDOW = 15 * 60       # 認證碼錯誤次數的計算區間
CLAIM_GLOBAL_MAX = 30        # 全店 1 小時內認證碼錯誤超過這個數，暫停所有人綁定（分散式猜碼）
ESCALATE_BURST = 3           # 同一位客人 1 小時內超過幾張決策卡，之後只記錄不推（省 push 額度、不洗版老闆）
CLAIM_TTL_SEC = 30 * 60        # 按「我自己回／我來回」後，這位老闆 30 分鐘內打的字才會轉給那位客人
AI_BREAKER_SEC = 60          # Claude 出錯後暫停呼叫幾秒（401/429/逾時時不要每則訊息都等一輪）
PUSH_FAIL_TEXT = ("⚠️ 沒送出去（可能是本月 LINE 推播額度用完，或對方已封鎖）。這件事仍在待辦，"
                  "請直接到 LINE 官方帳號管理後台回覆對方。")
OWNER_HELP = ("你可以用的指令：\n"
              "・待辦 — 把還沒決定的事再推一次\n"
              "・重載 — faq.json 改過後重新載入\n"
              "・直接打字 — 照你的話回給最近在等的客人")


class Bot:
    def __init__(self, cfg, store, faq, sender, ai=None, clock=time.time):
        self.cfg = cfg
        self.store = store
        self.faq = faq
        self.sender = sender
        self.ai = ai
        self.clock = clock
        self._ai_down_until = 0.0

    @staticmethod
    def _ok(res):
        """sender 回 (status, body)；舊式假 sender 回 None 視為成功。"""
        return res is None or not isinstance(res, tuple) or 200 <= res[0] < 300

    # ── 送訊息 ────────────────────────────────────────────
    def _send(self, event, user_id, messages):
        """reply token 還新鮮就 reply（免費），否則改 push。回傳用了哪一種。"""
        token = event.get("replyToken")
        ts = event.get("timestamp")  # LINE 給的是毫秒
        age = self.clock() - (ts / 1000.0) if ts else 0
        if token and age < self.cfg.reply_ttl_sec:
            res = self.sender.reply(token, messages)
            if self._ok(res) or not (isinstance(res, tuple) and res[0] == 400):
                return "reply"
            # 400＝reply token 失效（過期或已用過），這則其實沒送出去，改用 push 補
        self.sender.push(user_id, messages)
        return "push"

    def _say(self, event, user_id, text, intent=None, conf=None):
        self.store.log(user_id, "out", text, intent, conf)
        return self._send(event, user_id, text_messages(text))

    # ── 老闆 ────────────────────────────────────────────
    def owners(self):
        ids = set(self.store.owner_ids())
        if self.cfg.owner_user_id:
            ids.add(self.cfg.owner_user_id)
        return sorted(ids)

    def is_owner(self, user_id):
        return user_id in self.owners()

    def _try_claim(self, event, user_id, text):
        """「我是老闆 1234」：認證碼對才綁定。避免任何人打四個字就變老闆。"""
        rest = text.strip()[len(CLAIM_WORD):].strip()
        code = self.cfg.owner_claim_code
        now = int(self.clock())
        if (self.store.claim_fail_count(now - CLAIM_WINDOW, user_id) >= self.cfg.claim_max_fails
                or self.store.claim_fail_count(now - 3600) >= CLAIM_GLOBAL_MAX):
            print(f"[安全] 老闆認證碼嘗試過多，已暫時拒絕綁定（user …{user_id[-4:]}）", flush=True)
            self._send(event, user_id, text_messages("嘗試次數太多，請 15 分鐘後再試。"))
            return True
        if code and hmac.compare_digest(rest.encode("utf-8"), code.encode("utf-8")):
            self.store.set_role(user_id, "owner")
            self._send(event, user_id, text_messages("認證完成 ✅ 客人問我答不出來的事，我會推卡片給你。\n\n" + OWNER_HELP))
            return True
        self.store.add_claim_fail(user_id, now)
        self._send(event, user_id, text_messages("認證碼不對。請輸入「我是老闆 <認證碼>」，認證碼在 .env 的 BOT_OWNER_CLAIM_CODE。"))
        return True

    def _handle_owner(self, event, user_id, text):
        t = text.strip()
        if t in ("待辦", "待決", "決策"):
            rows = self.store.open_decisions()
            if not rows:
                self._send(event, user_id, text_messages("沒有待決定的事 👍"))
                return "owner_todo"
            # 用 reply 送（免費、不吃每月 push 額度；額度用完時老闆還是看得到待辦）
            cards = [decision_card(r["id"], f"{self.faq.shop_name}・需要你決定",
                                   r["question"], r["context"], json.loads(r["options"])) for r in rows[:5]]
            self._send(event, user_id, cards)
            return "owner_todo"
        if t in ("重載", "重新載入", "reload"):
            try:
                self.faq.reload()
            except Exception as e:  # faq.json 改壞了：舊話術照常，告訴老闆哪裡錯
                self._send(event, user_id, text_messages(f"重新載入失敗，仍使用舊的話術 ⚠️\n{str(e)[:200]}"))
                return "owner_reload_failed"
            self._send(event, user_id, text_messages("客服話術已重新載入 ✅"))
            return "owner_reload"
        if t in ("說明", "help", "指令"):
            self._send(event, user_id, text_messages(OWNER_HELP))
            return "owner_help"
        now = self.clock()
        target = self.store.text_target(user_id, now, CLAIM_TTL_SEC)
        if not target and len(self.owners()) == 1:
            # 只有一位老闆、且只剩一張待決卡：不用再按「我自己回／我來回」，打字就回給那位客人（維持單人老闆的原本體驗）
            rows = self.store.open_decisions()
            if len(rows) == 1:
                target = rows[0]
        if not target:
            if self.store.has_stale_claim(user_id, now, CLAIM_TTL_SEC):
                self._send(event, user_id, text_messages("⏱ 你按「我自己回／我來回」已超過 30 分鐘，鎖定失效了。請傳「待辦」再按一次卡片上的「我自己回／我來回」。這句話沒有轉給任何客人。"))
                return "owner_claim_expired"
            if self.store.open_decisions():
                self._send(event, user_id, text_messages("這句話沒有轉給任何客人。要親自回哪位客人，請先在他的卡片按「我自己回／我來回」（找不到卡片就傳「待辦」）。\n" + OWNER_HELP))
                return "owner_need_claim"
        if target:
            if not self._ok(self.sender.push(target["from_user_id"], text_messages(t))):
                self._send(event, user_id, text_messages(PUSH_FAIL_TEXT))
                return "owner_forward_failed"
            self.store.set_decision(target["id"], "resolved", free_text=t)
            self.store.log(target["from_user_id"], "out", t, "owner_text", 1.0)
            self._send(event, user_id, text_messages(f"已照你的話回覆對方 ✅\n（{target['question'][:40]}）"))
            return "owner_forward"
        self._send(event, user_id, text_messages("收到。目前沒有待決定的事。\n" + OWNER_HELP))
        return "owner_idle"

    # ── 升級給老闆 ────────────────────────────────────────
    def _escalate(self, user_id, text, display_name, reason=""):
        tpl = self.faq.escalation_template(text)
        options = tpl["options"] if tpl else DEFAULT_OPTIONS
        prefix = tpl["question_prefix"] if tpl else "客人問了我答不出來的問題"
        who = display_name or "某位客人"
        context = f"對方：{who}\n原話：「{text}」" + (f"\n原因：{reason}" if reason else "") \
            + "\n\n點一個按鈕，我就照這個意思回他。"
        did = self.store.create_decision(user_id, f"{prefix}：{text[:80]}", context, options)
        card = decision_card(did, f"{self.faq.shop_name}・需要你決定", prefix, context, options)
        owners = self.owners()
        if not owners:
            print("[提醒] 目前還沒有綁定老闆，這張決策卡沒有人收到。請用老闆的 LINE 傳「我是老闆 <認證碼>」，綁定後打「待辦」就會補推。", flush=True)
        # 同一位客人短時間狂傳：決策照記（打「待辦」看得到），但不再一直推卡片
        since = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")  # 與 db 的 created_at 同一個時鐘
        if self.store.decisions_since(user_id, since) > ESCALATE_BURST:
            print(f"[節流] 客人 …{user_id[-4:]} 1 小時內決策卡過多，這張只記錄不推播。", flush=True)
            return did
        results = [self.sender.push(owner, [card]) for owner in owners]
        if owners and not any(self._ok(r) for r in results):
            print("[警告] 決策卡推播給老闆全部失敗（可能 token 失效或本月 push 額度用完）。"
                  "老闆傳「待辦」可用免費的 reply 補看。", flush=True)
        return did

    # ── 客人 ──────────────────────────────────────────────
    def _handle_customer(self, event, user_id, text, display_name):
        hits = self.faq.sensitive_hits(text)
        if hits:
            self.store.log(user_id, "in", text, "handoff", 1.0)
            self._say(event, user_id, self.faq.handoff_message, "handoff", 1.0)
            self._escalate(user_id, text, display_name, reason="敏感詞：" + "、".join(hits))
            return "handoff"

        intent, answer, conf = self.faq.match(text)
        self.store.log(user_id, "in", text, intent, conf)
        if answer and conf >= self.cfg.threshold:
            self._say(event, user_id, answer, intent, conf)
            return "faq"

        reply = None
        if self.ai is not None and self._ai_allowed(user_id):
            self.store.add_ai_call(user_id, self.clock())
            try:
                reply, _info = self.ai.answer(text, self.faq.shop_name, self.faq.knowledge_text())
            except Exception as e:  # AI 掛掉（401/429/逾時/格式變了）就當作答不出來，推給老闆
                self._ai_down_until = self.clock() + AI_BREAKER_SEC
                status = getattr(e, "status_code", "")
                print(f"[AI 失敗] {type(e).__name__} {status}，{AI_BREAKER_SEC} 秒內改直接交給老闆。"
                      f"401＝金鑰錯／已撤銷；429＝限流或額度不足；其他請看 Claude Console。", flush=True)
                reply = None
            if reply:
                self._say(event, user_id, reply, "ai", None)
                return "ai"

        self._say(event, user_id, self.faq.wait_message, "escalated", conf)
        self._escalate(user_id, text, display_name)
        return "escalated"

    def _ai_allowed(self, user_id) -> bool:
        now = self.clock()
        if now < self._ai_down_until:
            return False
        if self.store.ai_count(int(now) - 3600, user_id) >= self.cfg.ai_per_user_hour:
            return False
        return self.store.ai_count(int(now) - 86400) < self.cfg.ai_daily_cap

    def _handle_postback(self, event, user_id):
        data = event.get("postback", {}).get("data", "")
        if not data.startswith("decide:") or not self.is_owner(user_id):
            return "ignored"
        parts = data.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            return "ignored"
        _, sid, value = parts
        d = self.store.get_decision(int(sid))
        if not d:
            return "ignored"
        if d["status"] == "resolved":
            self._send(event, user_id, text_messages("這件事已經處理過了 👍"))
            return "already"
        options = json.loads(d["options"])
        chosen = next((o for o in options if o["value"] == value), None)
        if chosen and chosen.get("reply"):
            if not self._ok(self.sender.push(d["from_user_id"], text_messages(chosen["reply"]))):
                self._send(event, user_id, text_messages(PUSH_FAIL_TEXT))
                return "decide_failed"
            self.store.set_decision(d["id"], "resolved", chosen=value)
            self.store.log(d["from_user_id"], "out", chosen["reply"], "owner_decision", 1.0)
            self._send(event, user_id, text_messages(f"已回覆對方：\n「{chosen['reply']}」"))
            return "decided"
        now = self.clock()
        if (d["status"] == "waiting_text" and d["claimed_by"] and d["claimed_by"] != user_id
                and d["claimed_at"] and now - d["claimed_at"] < CLAIM_TTL_SEC and self.is_owner(d["claimed_by"])):
            self._send(event, user_id, text_messages("這位客人已經有另一位老闆按了「我自己回／我來回」，由他處理中 👍"))
            return "claimed_by_other"
        self.store.set_decision(d["id"], "waiting_text", chosen=value, claimed_by=user_id, claimed_at=int(now))
        self._send(event, user_id, text_messages("好，你直接打字，我就照你的話回他（30 分鐘內有效）。\n問題：" + d["question"][:40]))
        return "waiting_text"

    # ── 入口 ──────────────────────────────────────────────
    def handle_event(self, event: dict) -> str:
        user_id = event.get("source", {}).get("userId")
        if not user_id:
            return "ignored"
        etype = event.get("type")
        eid = event.get("webhookEventId")
        if eid and not self.store.claim_event(eid):  # LINE 重送（isRedelivery）：已處理過就直接略過
            return "duplicate"
        if etype == "follow":
            self.store.touch_contact(user_id, self.sender.display_name(user_id))
            self._send(event, user_id, text_messages(self.faq.welcome))
            return "follow"
        if etype == "postback":
            return self._handle_postback(event, user_id)
        msg = event.get("message", {})
        if etype != "message" or msg.get("type") != "text":
            if etype == "message":
                self._send(event, user_id, text_messages("目前只看得懂文字訊息，麻煩用打字告訴我 🙏"))
                return "unsupported"
            return "ignored"

        text = msg.get("text", "")
        known = self.store.get_contact(user_id)
        name = (known["display_name"] if known and known["display_name"] else None) or self.sender.display_name(user_id)
        self.store.touch_contact(user_id, name)
        if text.strip().startswith(CLAIM_WORD):
            self._try_claim(event, user_id, text)
            return "claim"
        if self.is_owner(user_id):
            return self._handle_owner(event, user_id, text)
        return self._handle_customer(event, user_id, text, name)
