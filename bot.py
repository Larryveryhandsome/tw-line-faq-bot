"""客服大腦：一個 LINE event 進來，決定怎麼回。

客人：敏感詞 → 轉真人；FAQ 有把握 → 直接回；
     沒把握 → （有設 AI）Claude 依知識庫補答 → 還是不行就推按鈕卡片給老闆。
老闆：「我是老闆 <認證碼>」綁定；「待辦」「重載」「說明」；直接打字＝回給最近在等的客人。

所有外部相依（sender、AI）都從建構子注入，測試不打真實 API。
"""
import json
import time

from faq import DEFAULT_OPTIONS
from line_api import decision_card, text_messages

CLAIM_WORD = "我是老闆"
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

    # ── 送訊息 ────────────────────────────────────────────
    def _send(self, event, user_id, messages):
        """reply token 還新鮮就 reply（免費），否則改 push。回傳用了哪一種。"""
        token = event.get("replyToken")
        ts = event.get("timestamp")  # LINE 給的是毫秒
        age = self.clock() - (ts / 1000.0) if ts else 0
        if token and age < self.cfg.reply_ttl_sec:
            self.sender.reply(token, messages)
            return "reply"
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
        if code and rest == code:
            self.store.set_role(user_id, "owner")
            self._send(event, user_id, text_messages("認證完成 ✅ 客人問我答不出來的事，我會推卡片給你。\n\n" + OWNER_HELP))
            return True
        self._send(event, user_id, text_messages("認證碼不對。請輸入「我是老闆 <認證碼>」，認證碼在 .env 的 BOT_OWNER_CLAIM_CODE。"))
        return True

    def _handle_owner(self, event, user_id, text):
        t = text.strip()
        if t in ("待辦", "待決", "決策"):
            rows = self.store.open_decisions()
            if not rows:
                self._send(event, user_id, text_messages("沒有待決定的事 👍"))
                return "owner_todo"
            for r in rows[:5]:
                self.sender.push(user_id, [decision_card(r["id"], f"{self.faq.shop_name}・需要你決定",
                                                         r["question"], r["context"], json.loads(r["options"]))])
            return "owner_todo"
        if t in ("重載", "重新載入", "reload"):
            self.faq.reload()
            self._send(event, user_id, text_messages("客服話術已重新載入 ✅"))
            return "owner_reload"
        if t in ("說明", "help", "指令"):
            self._send(event, user_id, text_messages(OWNER_HELP))
            return "owner_help"
        target = self.store.text_target()
        if target:
            self.store.set_decision(target["id"], "resolved", free_text=t)
            self.sender.push(target["from_user_id"], text_messages(t))
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
        for owner in owners:
            self.sender.push(owner, [card])
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

        if self.ai is not None:
            try:
                reply, _info = self.ai.answer(text, self.faq.shop_name, self.faq.knowledge_text())
            except Exception as e:  # AI 掛掉就當作答不出來
                reply, _info = None, {"error": str(e)}
            if reply:
                self._say(event, user_id, reply, "ai", None)
                return "ai"

        self._say(event, user_id, self.faq.wait_message, "escalated", conf)
        self._escalate(user_id, text, display_name)
        return "escalated"

    def _handle_postback(self, event, user_id):
        data = event.get("postback", {}).get("data", "")
        if not data.startswith("decide:") or not self.is_owner(user_id):
            return "ignored"
        _, sid, value = data.split(":", 2)
        d = self.store.get_decision(int(sid))
        if not d:
            return "ignored"
        if d["status"] == "resolved":
            self._send(event, user_id, text_messages("這件事已經處理過了 👍"))
            return "already"
        options = json.loads(d["options"])
        chosen = next((o for o in options if o["value"] == value), None)
        if chosen and chosen.get("reply"):
            self.store.set_decision(d["id"], "resolved", chosen=value)
            self.sender.push(d["from_user_id"], text_messages(chosen["reply"]))
            self.store.log(d["from_user_id"], "out", chosen["reply"], "owner_decision", 1.0)
            self._send(event, user_id, text_messages(f"已回覆對方：\n「{chosen['reply']}」"))
            return "decided"
        self.store.set_decision(d["id"], "waiting_text", chosen=value)
        self._send(event, user_id, text_messages("好，你直接打字，我就照你的話回他。"))
        return "waiting_text"

    # ── 入口 ──────────────────────────────────────────────
    def handle_event(self, event: dict) -> str:
        user_id = event.get("source", {}).get("userId")
        if not user_id:
            return "ignored"
        etype = event.get("type")
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
        name = self.sender.display_name(user_id)
        self.store.touch_contact(user_id, name)
        if text.strip().startswith(CLAIM_WORD):
            self._try_claim(event, user_id, text)
            return "claim"
        if self.is_owner(user_id):
            return self._handle_owner(event, user_id, text)
        return self._handle_customer(event, user_id, text, name)
