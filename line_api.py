"""LINE Messaging API 薄封裝（只用標準庫）＋給老闆的決策卡片。"""
import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.request

API = "https://api.line.me/v2/bot"


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """確認 webhook 真的是 LINE 送來的。沒過就丟掉。"""
    if not secret or not signature:
        return False
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode("utf-8"), signature)


def text_messages(text: str):
    text = text or " "
    return [{"type": "text", "text": text[i:i + 4900]} for i in range(0, len(text), 4900)][:5]


class LineSender:
    """真正送 LINE 的實作。測試用 FakeSender 換掉它。"""

    def __init__(self, access_token: str, timeout: int = 10):
        self.token = access_token
        self.timeout = timeout

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # 網路問題不該讓主流程掛掉
            return 0, str(e)

    def reply(self, reply_token, messages):
        return self._call("POST", "/message/reply", {"replyToken": reply_token, "messages": messages[:5]})

    def push(self, user_id, messages):
        return self._call("POST", "/message/push", {"to": user_id, "messages": messages[:5]})

    def display_name(self, user_id):
        status, body = self._call("GET", f"/profile/{user_id}")
        if status == 200:
            try:
                return json.loads(body).get("displayName")
            except ValueError:
                return None
        return None


def decision_card(decision_id: int, title: str, question: str, context: str, options: list) -> dict:
    """Flex 卡片：上面是客人問題，下面是老闆點選的按鈕（postback: decide:<id>:<value>）。"""
    buttons = [{
        "type": "button", "style": "primary" if i == 0 else "secondary", "height": "sm", "margin": "sm",
        "action": {"type": "postback", "label": opt["label"][:20],
                   "data": f"decide:{decision_id}:{opt['value']}", "displayText": f"我選：{opt['label']}"},
    } for i, opt in enumerate(options[:4])]
    return {
        "type": "flex",
        "altText": f"【要你決定】{question[:40]}",
        "contents": {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1F6F5C", "paddingAll": "12px",
                       "contents": [{"type": "text", "text": title[:40], "color": "#FFFFFF",
                                     "weight": "bold", "size": "sm"}]},
            "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
                {"type": "text", "text": question[:200], "wrap": True, "weight": "bold", "size": "md"},
                {"type": "separator"},
                {"type": "text", "text": (context or "（無補充說明）")[:400], "wrap": True,
                 "size": "xs", "color": "#666666"}]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "xs", "contents": buttons + [
                {"type": "text", "text": "也可以直接打字，我會照你的話回客人", "size": "xxs",
                 "color": "#999999", "wrap": True, "margin": "md"}]},
        },
    }
