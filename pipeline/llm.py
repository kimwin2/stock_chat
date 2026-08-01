"""Gemini REST 래퍼.

SDK 대신 REST 를 직접 쓴다 — 의존성이 requests 하나뿐이고 SDK 버전 변화에 안 흔들린다.
무료 등급 쿼터(분당 요청수)를 고려해 호출 간격 조절과 지수 백오프 재시도를 내장했다.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta"

# 무료 등급 기준 분당 요청 수. 여유를 두고 보수적으로 잡았다.
DEFAULT_RPM = {
    "gemini-2.5-flash": 8,
    "gemini-2.5-flash-lite": 12,
    "gemini-embedding-001": 90,
}


class QuotaExceeded(RuntimeError):
    """일일 쿼터 소진. 파이프라인은 여기까지 저장하고 정상 종료한다."""


class _RateLimiter:
    """모델별 최소 호출 간격 유지. 스레드 안전."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, model: str) -> None:
        rpm = DEFAULT_RPM.get(model, 10)
        interval = 60.0 / max(1, rpm)
        with self._lock:
            last = self._last.get(model, 0.0)
            delay = interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
            self._last[model] = time.monotonic()


_limiter = _RateLimiter()


def api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 가 없습니다. .env 에 넣으세요.\n"
            "  https://aistudio.google.com/apikey (무료 등급)"
        )
    return key


def _post(url: str, payload: dict, model: str, timeout: int = 180, max_retries: int = 5) -> dict:
    last_err: Exception | None = None
    for attempt in range(max_retries):
        _limiter.wait(model)
        try:
            r = requests.post(
                url,
                params={"key": api_key()},
                json=payload,
                timeout=timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            last_err = e
            time.sleep(min(2 ** attempt + random.random(), 30))
            continue

        if r.status_code == 200:
            return r.json()

        body = r.text[:500]
        if r.status_code == 429:
            # 일일 한도인지 분당 한도인지 구분
            if "PerDay" in body or "per day" in body.lower():
                raise QuotaExceeded(
                    f"Gemini 일일 무료 쿼터를 다 썼습니다 (model={model}).\n"
                    f"내일 이어서 실행하면 남은 분량만 처리합니다.\n{body}"
                )
            wait = min(2 ** attempt * 5 + random.random() * 3, 90)
            print(f"    [rate limit] {wait:.0f}초 대기 후 재시도 ({attempt + 1}/{max_retries})")
            time.sleep(wait)
            last_err = RuntimeError(body)
            continue
        if r.status_code in (500, 502, 503, 504):
            time.sleep(min(2 ** attempt + random.random(), 30))
            last_err = RuntimeError(f"{r.status_code}: {body}")
            continue

        raise RuntimeError(f"Gemini API 오류 {r.status_code} (model={model}): {body}")

    raise RuntimeError(f"Gemini 호출이 {max_retries}회 실패했습니다 (model={model}): {last_err}")


# ── 텍스트/멀티모달 생성 ─────────────────────────────────────

def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".webp": "image/webp",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
    }.get(ext, "image/webp")


def generate(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    images: list[Path] | None = None,
    json_out: bool = False,
    temperature: float = 0.2,
    max_output_tokens: int = 8192,
) -> str:
    parts: list[dict] = [{"text": prompt}]
    for img in images or []:
        data = base64.b64encode(img.read_bytes()).decode("ascii")
        parts.append({"inline_data": {"mime_type": _mime_for(img), "data": data}})

    gen_cfg: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if json_out:
        gen_cfg["responseMimeType"] = "application/json"

    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": gen_cfg,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    data = _post(f"{BASE}/models/{model}:generateContent", payload, model)

    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        raise RuntimeError(f"응답이 비었습니다 (model={model}): {feedback}")

    cand = candidates[0]
    out = "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or []))
    if not out.strip() and cand.get("finishReason") == "MAX_TOKENS":
        raise RuntimeError(f"출력이 토큰 한도에 걸렸습니다 (model={model}). 입력을 줄이세요.")
    return out


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def generate_json(model: str, prompt: str, **kw) -> Any:
    """JSON 응답을 파싱해서 반환. 모델이 코드펜스를 붙여도 벗겨낸다."""
    raw = generate(model, prompt, json_out=True, **kw)
    text = raw.strip()
    if not text:
        raise ValueError("빈 응답")
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 앞뒤에 설명이 붙은 경우 첫 { 또는 [ 부터 마지막 } 또는 ] 까지 시도
        for opener, closer in (("{", "}"), ("[", "]")):
            i, j = text.find(opener), text.rfind(closer)
            if i != -1 and j > i:
                try:
                    return json.loads(text[i : j + 1])
                except json.JSONDecodeError:
                    continue
        raise ValueError(f"JSON 파싱 실패: {text[:400]}")


# ── 임베딩 ──────────────────────────────────────────────────

def embed_batch(
    model: str,
    texts: list[str],
    *,
    dim: int = 256,
    task_type: str = "RETRIEVAL_DOCUMENT",
    batch_size: int = 50,
) -> list[list[float]]:
    """텍스트 목록 → 정규화된 임베딩 목록."""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        payload = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": t[:8000]}]},
                    "taskType": task_type,
                    "outputDimensionality": dim,
                }
                for t in chunk
            ]
        }
        data = _post(f"{BASE}/models/{model}:batchEmbedContents", payload, model)
        for item in data.get("embeddings", []):
            out.append(_normalize(item.get("values", [])))
    return out


def _normalize(vec: list[float]) -> list[float]:
    """MRL 로 차원을 줄이면 정규화가 깨지므로 다시 단위벡터로 만든다."""
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return vec
    return [v / norm for v in vec]
