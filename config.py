from __future__ import annotations

import logging
import os
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger("telbot.config")


@dataclass(frozen=True)
class Settings:
    tg_api_id: int
    tg_api_hash: str
    tg_session_name: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    db_path: str
    rules_path: str
    pairs_path: str
    stop_relay_rules_path: str
    polish_prompt_path: str
    app_conf_path: str
    archive_dir: str
    log_level: str


def load_settings() -> Settings:
    load_dotenv()
    missing = [k for k in ("TG_API_ID", "TG_API_HASH", "DEEPSEEK_API_KEY") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}. See .env.example.")
    return Settings(
        tg_api_id=int(os.environ["TG_API_ID"]),
        tg_api_hash=os.environ["TG_API_HASH"],
        tg_session_name=os.getenv("TG_SESSION_NAME", "telbot"),
        deepseek_api_key=os.environ["DEEPSEEK_API_KEY"],
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        db_path=os.getenv("DB_PATH", "./telbot.db"),
        rules_path=os.getenv("RULES_PATH", "./rules.yaml"),
        pairs_path=os.getenv("PAIRS_PATH", "./pair-accounts.txt"),
        stop_relay_rules_path=os.getenv("STOP_RELAY_RULES_PATH", "./stop-relay-rules.txt"),
        polish_prompt_path=os.getenv("POLISH_PROMPT_PATH", "./polish-prompt.txt"),
        app_conf_path=os.getenv("APP_CONF_PATH", "./app.conf"),
        archive_dir=os.getenv("ARCHIVE_DIR", "./msg"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


DEFAULT_RULES: dict = {
    "defaults": {
        "save": True,
        "analyze": True,
        "analyze_system_prompt": (
            "You are a message-analysis assistant. Read the user's incoming Telegram "
            "message and respond with a compact JSON object with keys: "
            "language, sentiment (positive|neutral|negative), intent (question|request|"
            "statement|smalltalk|spam|other), priority (low|medium|high), summary "
            "(<=15 words). Respond with JSON only, no prose."
        ),
        "auto_reply": {"enabled": False, "system_prompt": "", "temperature": 0.4, "max_tokens": 300},
        "relay_to": None,
        "relay_template": "From {sender_name} (@{sender_username}) in {chat_title}: {text}",
    },
    "rules": [],
}


def load_rules(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return DEFAULT_RULES
    data = yaml.safe_load(p.read_text()) or {}
    merged = {**DEFAULT_RULES, **data}
    merged["defaults"] = {**DEFAULT_RULES["defaults"], **(data.get("defaults") or {})}
    merged["defaults"]["auto_reply"] = {
        **DEFAULT_RULES["defaults"]["auto_reply"],
        **(merged["defaults"].get("auto_reply") or {}),
    }
    merged["rules"] = data.get("rules") or []
    return merged


@dataclass(frozen=True)
class Delays:
    relay_min: float
    relay_max: float
    mark_read_min: float
    mark_read_max: float


DEFAULT_DELAYS = Delays(relay_min=1.0, relay_max=10.0,
                        mark_read_min=1.0, mark_read_max=3.0)


def load_delays(path: str) -> Delays:
    """Read [delays] section from app.conf. Missing file or keys fall back
    to DEFAULT_DELAYS. Read once at startup; restart to apply changes."""
    if not Path(path).exists():
        log.warning("no app conf at %s — using built-in default delays", path)
        return DEFAULT_DELAYS

    cp = ConfigParser()
    cp.read(path, encoding="utf-8")
    section = cp["delays"] if cp.has_section("delays") else {}

    def _get(key: str, default: float) -> float:
        try:
            return float(section.get(key, default))
        except (TypeError, ValueError):
            log.warning("app.conf [delays] %s is not a number — using default %s", key, default)
            return default

    d = Delays(
        relay_min=_get("relay_min", DEFAULT_DELAYS.relay_min),
        relay_max=_get("relay_max", DEFAULT_DELAYS.relay_max),
        mark_read_min=_get("mark_read_min", DEFAULT_DELAYS.mark_read_min),
        mark_read_max=_get("mark_read_max", DEFAULT_DELAYS.mark_read_max),
    )
    if d.relay_max < d.relay_min:
        log.warning("app.conf: relay_max < relay_min (%s < %s)", d.relay_max, d.relay_min)
    if d.mark_read_max < d.mark_read_min:
        log.warning("app.conf: mark_read_max < mark_read_min (%s < %s)",
                    d.mark_read_max, d.mark_read_min)
    return d
