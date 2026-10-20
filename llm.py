"""選用：FAQ 答不出來時，請 Claude 依知識庫補答。

- 沒設 ANTHROPIC_API_KEY 就不會載入，整個 bot 照樣能跑（答不出來就推給老闆）。
- 知識庫放在 system 的最後一塊並掛 cache_control，同一份知識庫重複使用時走 prompt caching。
- 模型只依知識庫回答；沒把握就回 ESCALATE，由老闆決定，不讓 AI 亂答。
"""

ESCALATE = "ESCALATE"

PERSONA = (
    "你是「{shop}」的 LINE 客服助理。用繁體中文、台灣用語，簡潔親切，回覆 3 句以內。"
    "只能根據下面的知識庫回答。知識庫沒有寫、需要店家決定（折扣、例外、承諾），"
    "或涉及客訴、退款、法律、健康安全的問題，一律只回一個單字：ESCALATE。"
)


class ClaudeResponder:
    def __init__(self, api_key: str, model: str, client=None):
        if client is None:
            from anthropic import Anthropic  # 只有真的要用才 import
            client = Anthropic(api_key=api_key)
        self.client = client
        self.model = model

    def answer(self, question: str, shop_name: str, knowledge: str):
        """回傳 (回覆文字 或 None, 用量資訊 dict)。None 代表要交給老闆。"""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=[
                {"type": "text", "text": PERSONA.format(shop=shop_name)},
                {"type": "text", "text": knowledge, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": question}],
            output_config={"effort": "low"},
        )
        usage = getattr(resp, "usage", None)
        info = {
            "model": self.model,
            "stop_reason": getattr(resp, "stop_reason", None),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) if usage else 0,
        }
        if info["stop_reason"] == "refusal":
            return None, info
        text = "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", "") == "text").strip()
        if not text or ESCALATE in text.upper():
            return None, info
        return text, info
