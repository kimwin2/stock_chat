"""설정 로딩. config.yaml + .env 를 합쳐 하나의 객체로 준다."""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv 없이도 환경변수만으로 동작하게
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # 채널 원문 (일자별)
MEDIA_DIR = DATA_DIR / "media"      # 리사이즈된 이미지 (일자별)
DAILY_DIR = DATA_DIR / "daily"      # 일일 요약
INDEX_DIR = DATA_DIR / "index"      # RAG 청크 + 임베딩
WEB_DIR = ROOT / "web"
WEB_DATA_DIR = WEB_DIR / "data"     # 배포용 암호화 번들
STATE_PATH = DATA_DIR / "state.json"


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")


class Config:
    def __init__(self, raw: dict[str, Any]):
        self._raw = raw

    def __getitem__(self, key: str) -> Any:
        return self._raw[key]

    def get(self, path: str, default: Any = None) -> Any:
        """점 표기로 중첩 접근. cfg.get('models.vision')"""
        node: Any = self._raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ── 채널 ────────────────────────────────────────────────
    @property
    def channels(self) -> list[dict]:
        return self.get("telegram.channels", []) or []

    @property
    def active_channels(self) -> list[dict]:
        out = []
        for ch in self.channels:
            if not ch.get("active"):
                continue
            link = (ch.get("link") or "").strip()
            if not link or link.upper() == "CLOSED":
                continue
            out.append(ch)
        return out

    def channel_label(self, channel_id: str) -> str:
        for ch in self.channels:
            if ch.get("id") == channel_id:
                return ch.get("label") or channel_id
        return channel_id

    # ── 비밀값 ──────────────────────────────────────────────
    @property
    def gemini_key(self) -> str:
        return (os.getenv("GEMINI_API_KEY") or "").strip()

    @property
    def share_passphrase(self) -> str:
        return os.getenv("SHARE_PASSPHRASE") or ""

    @property
    def gh_dispatch_token(self) -> str:
        return (os.getenv("GH_DISPATCH_TOKEN") or "").strip()

    @property
    def gh_repo(self) -> str:
        return (os.getenv("GH_REPO") or "").strip()


class ConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_config() -> Config:
    _load_env()
    path = ROOT / "config.yaml"
    if not path.exists():
        raise ConfigError(f"config.yaml 이 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config(raw)


def ensure_dirs() -> None:
    for d in (RAW_DIR, MEDIA_DIR, DAILY_DIR, INDEX_DIR, WEB_DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def require(*names: str) -> None:
    """필수 환경변수 확인. 없으면 무엇을 어디에 넣어야 하는지 알려준다."""
    _load_env()
    hints = {
        "GEMINI_API_KEY": "https://aistudio.google.com/apikey 에서 발급 (무료 등급)",
        "TG_API_ID": "https://my.telegram.org 에서 발급",
        "TG_API_HASH": "https://my.telegram.org 에서 발급",
        "TG_STRING_SESSION": "python -m pipeline.tg_auth 로 발급",
        "SHARE_PASSPHRASE": "친구들에게 공유할 암호를 직접 정하세요",
    }
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        lines = "\n".join(f"  - {n}: {hints.get(n, '')}" for n in missing)
        raise ConfigError(f".env 에 다음 값이 필요합니다 ({ROOT / '.env'}):\n{lines}")
