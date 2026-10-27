"""Webhook 伺服器：收到 LINE 訊息先驗簽、丟進佇列、立刻回 200；背景執行緒慢慢處理。

啟動：python app.py（Windows 背景執行請雙擊「背景啟動.vbs」，不會跳黑色視窗）
"""
import json
import os
import queue
import sys
import threading
import time
import traceback

from flask import Flask, abort, request

import config
from bot import CUSTOM_AD, Bot
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


def purge_once(cfg, store):
    try:
        m, d = store.purge_old(cfg.retention_days)
        if m or d:
            print(f"[保留期限] 已清除 {cfg.retention_days} 天前的訊息 {m} 則、已結案待辦 {d} 筆", flush=True)
    except Exception:
        traceback.print_exc()


def start_purge_loop(cfg, store, interval=6 * 3600):
    """啟動時清一次，之後每 6 小時清一次。"""
    purge_once(cfg, store)

    def loop():
        while True:
            time.sleep(interval)
            purge_once(cfg, store)
    threading.Thread(target=loop, daemon=True, name="purge").start()


def create_app(cfg, bot):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # webhook 本文不會大於 1MB
    jobs: "queue.Queue[dict]" = queue.Queue(maxsize=2000)

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
        try:
            events = json.loads(body.decode("utf-8") or "{}").get("events", [])
        except ValueError:
            abort(400)
        for ev in events:
            try:
                jobs.put_nowait(ev)
            except queue.Full:  # 佇列滿了回 503，不要默默吞掉
                abort(503)
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
    bot = build_bot(cfg)
    start_purge_loop(cfg, bot.store)
    app = create_app(cfg, bot)
    print(f"LINE 客服 bot 啟動：http://{cfg.host}:{cfg.port}/callback（AI 補答：{'開' if cfg.ai_enabled else '關'}）")
    print(CUSTOM_AD, flush=True)
    app.run(host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
