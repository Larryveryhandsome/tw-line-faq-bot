"""設定：全部從環境變數或 .env 讀，原始碼不寫任何憑證。"""
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path = BASE_DIR / ".env"):
    """極簡 .env 讀取（不依賴 python-dotenv）。已存在的環境變數優先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


class ConfigError(RuntimeError):
    pass


def _env(name, default=""):
    return os.getenv(name, default).strip()


@dataclass
class Config:
    channel_secret: str = ""
    access_token: str = ""
    owner_user_id: str = ""          # 老闆的 LINE userId（可留空，用認證碼綁定）
    owner_claim_code: str = ""       # 老闆認證碼：傳「我是老闆 <認證碼>」綁定
    host: str = "127.0.0.1"
    port: int = 18800
    threshold: float = 0.55          # FAQ 信心門檻，低於就交給 AI 或老闆
    reply_ttl_sec: int = 50          # LINE reply token 有時效，超過改用 push
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    faq_path: Path = field(default_factory=lambda: BASE_DIR / "faq.json")
    # 選用：Claude 補答（沒設 key 就完全不呼叫 AI）
    ai_per_user_hour: int = 6        # 每位客人每小時最多讓 Claude 補答幾次（防洗版燒錢）
    ai_daily_cap: int = 300          # 全店每 24 小時最多呼叫 Claude 幾次
    claim_max_fails: int = 5         # 老闆認證碼同一人 15 分鐘內最多錯幾次
    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-5-5"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    def require_line(self):
        missing = [n for n, v in (("LINE_CHANNEL_SECRET", self.channel_secret),
                                  ("LINE_CHANNEL_ACCESS_TOKEN", self.access_token)) if not v]
        if missing:
            raise ConfigError(
                "缺少必要設定：" + "、".join(missing)
                + "\n請把 .env.example 複製成 .env 並填入（步驟見 README）。")
        if not self.owner_user_id and not self.owner_claim_code:
            raise ConfigError("請在 .env 設定 BOT_OWNER_CLAIM_CODE（老闆認證碼）或 BOT_OWNER_USER_ID。")
        if self.owner_claim_code and len(self.owner_claim_code) < 6:
            raise ConfigError("BOT_OWNER_CLAIM_CODE 太短，至少 6 個字（建議 8 位以上英數混合），不然別人可以猜到變成老闆。")


def from_env() -> Config:
    load_dotenv()
    data_dir = _env("BOT_DATA_DIR")
    faq_path = _env("BOT_FAQ_PATH")
    cfg = Config(
        channel_secret=_env("LINE_CHANNEL_SECRET"),
        access_token=_env("LINE_CHANNEL_ACCESS_TOKEN"),
        owner_user_id=_env("BOT_OWNER_USER_ID"),
        owner_claim_code=_env("BOT_OWNER_CLAIM_CODE"),
        host=_env("BOT_HOST", "127.0.0.1"),
        port=int(_env("BOT_PORT", "18800")),
        threshold=float(_env("BOT_CONFIDENCE_THRESHOLD", "0.55")),
        reply_ttl_sec=int(_env("BOT_REPLY_TTL_SEC", "50")),
        ai_per_user_hour=int(_env("BOT_AI_PER_USER_HOUR", "6")),
        ai_daily_cap=int(_env("BOT_AI_DAILY_CAP", "300")),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        ai_model=_env("BOT_AI_MODEL", "claude-sonnet-5-5"),
    )
    if data_dir:
        cfg.data_dir = Path(data_dir)
    if faq_path:
        cfg.faq_path = Path(faq_path)
    return cfg
