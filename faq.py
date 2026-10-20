"""FAQ 比對：純關鍵字加權，不靠 AI，行為可預測。"""
import json
import re
from pathlib import Path


def normalise(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


DEFAULT_OPTIONS = [
    {"label": "我來回，等我打字", "value": "owner_will_type", "reply": None},
    {"label": "回：稍後聯繫", "value": "later",
     "reply": "不好意思，這個我需要確認一下，稍後回覆你 🙏"},
    {"label": "回：可以，沒問題", "value": "yes", "reply": "可以的，沒問題 🙌"},
    {"label": "回：這個沒辦法", "value": "no", "reply": "不好意思，這個目前沒有辦法，真的很抱歉 🙇"},
]


class FAQ:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {}
        self.reload()

    def reload(self):
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self

    @property
    def shop_name(self) -> str:
        return self.data.get("shop_name", "本店")

    @property
    def welcome(self) -> str:
        return self.data.get("welcome", f"你好，這裡是{self.shop_name}，有什麼可以幫你的嗎？")

    @property
    def wait_message(self) -> str:
        return self.data.get("wait_message", "這個我幫你確認一下，馬上回覆你 🙏")

    @property
    def handoff_message(self) -> str:
        return self.data.get("handoff_message", "這個問題我請專人為你處理，營業時間內會盡快回覆你 🙏")

    def match(self, text: str):
        """回傳 (intent, answer, confidence)。"""
        norm = normalise(text)
        if not norm:
            return None, None, 0.0
        best = (None, None, 0.0)
        for entry in self.data.get("entries", []):
            hits = [k for k in entry["keywords"] if normalise(k) and normalise(k) in norm]
            if not hits:
                continue
            longest = max(len(normalise(k)) for k in hits)
            # 命中越多、命中詞越長分數越高；整句就是關鍵字時幾乎確定
            score = min(1.0, 0.42 + 0.16 * len(hits) + 0.06 * longest)
            if any(normalise(k) == norm for k in hits):
                score = 0.97
            if score > best[2]:
                best = (entry["intent"], entry["answer"], score)
        return best[0], best[1], round(best[2], 2)

    def sensitive_hits(self, text: str):
        """命中敏感詞（客訴、退款、法律…）就一定轉真人，不讓機器人回。"""
        norm = normalise(text)
        return [k for k in self.data.get("handoff_keywords", []) if normalise(k) in norm]

    def escalation_template(self, text: str):
        """已知「要老闆拍板」的情境（談價、月結…）回傳預設按鈕。"""
        norm = normalise(text)
        for tpl in self.data.get("escalation_templates", []):
            if any(normalise(t) in norm for t in tpl["trigger"]):
                return tpl
        return None

    def knowledge_text(self) -> str:
        """給 AI 補答用的知識庫全文（順序固定，prompt caching 才會命中）。"""
        lines = [f"# {self.shop_name} 客服知識庫"]
        for e in self.data.get("entries", []):
            lines.append(f"\n## {e['intent']}\n{e['answer']}")
        extra = self.data.get("ai_notes")
        if extra:
            lines.append("\n## 其他說明\n" + extra)
        return "\n".join(lines)
