"""Webhook 伺服器：收到 LINE 訊息先驗簽、丟進佇列、立刻回 200；背景執行緒慢慢處理。

啟動：python app.py（Windows 背景執行請雙擊「背景啟動.vbs」，不會跳黑色視窗）
"""
import json
import os
import queue
import sys
import threading
import traceback

from flask import Flask, abort, request

import config
from bot import Bot
from db import Store
from faq import FAQ
from line_api import LineSender, verify_signature

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def build_bot(cfg):
    ai = None
    if cfg.ai_enabled:
        from llm import ClaudeResponder
        ai = ClaudeResponder(cfg.anthropic_api_key, cfg.ai_model)
    return Bot(cfg, Store(cfg.db_path), FAQ(cfg.faq_path), LineSender(cfg.access_token), ai=ai)


def create_app(cfg, bot):
    app = Flask(__name__)
    jobs: "queue.Queue[dict]" = queue.Queue()

    def worker():
        while True:
            ev = jobs.get()
            try:
                bot.handle_event(ev)
            except Exception:  # 單一事件出錯不影響其他人
                traceback.print_exc()
            finally:
                jobs.task_done()

    threading.Thread(target=worker, daemon=True, name="line-worker").start()
    app.config["JOBS"] = jobs

    @app.post("/callback")
    def callback():
        body = request.get_data()
        if not verify_signature(cfg.channel_secret, body, request.headers.get("X-Line-Signature", "")):
            abort(400)
        for ev in json.loads(body.decode("utf-8") or "{}").get("events", []):
            jobs.put(ev)
        return "OK"

    @app.get("/health")
    def health():
        return {"ok": True, "queue": jobs.qsize(), "ai": cfg.ai_enabled}

    return app


def main():
    cfg = config.from_env()
    try:
        cfg.require_line()
    except config.ConfigError as e:
        print(f"\n❌ {e}\n")
        sys.exit(1)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:  # pythonw 背景執行沒有主控台，改寫到 log 檔
        log = open(cfg.data_dir / "bot.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    (cfg.data_dir / "bot.pid").write_text(str(os.getpid()), encoding="utf-8")
    app = create_app(cfg, build_bot(cfg))
    print(f"LINE 客服 bot 啟動：http://{cfg.host}:{cfg.port}/callback（AI 補答：{'開' if cfg.ai_enabled else '關'}）")
    app.run(host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
