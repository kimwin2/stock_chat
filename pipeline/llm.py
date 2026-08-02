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

# 무료 등급 기준 분당 요청 수. 여기 없는 모델은 10 RPM 으로 취급한다.
# 참고 — 실측한 일일 한도: gemini-3.6-flash 는 20회/일뿐이라 파이프라인에 부적합.
DEFAULT_RPM = {
    "gemini-3.6-flash": 4,
    "gemini-3.5-flash": 10,
    "gemini-3.5-flash-lite": 15,
    "gemini-3.1-flash-lite": 15,
    "gemini-2.5-flash-lite": 15,
    # 임베딩만 단위가 다르다 — "분당 텍스트 수". 배치 1회가 HTTP 로는 한 번이지만
    # 쿼터는 배치 안의 텍스트 수만큼 깎이므로 embed_batch 가 weight 로 배치 크기를 넘긴다.
    "gemini-embedding-001": 90,
}


class QuotaExceeded(RuntimeError):
    """일일 쿼터 소진. 파이프라인은 여기까지 저장하고 정상 종료한다."""


class _RateLimiter:
    """모델별 최소 호출 간격 유지. 스레드 안전.

    weight 는 이 호출이 쿼터를 몇 건으로 계산되는지다. 임베딩 배치 요청은
    HTTP 로는 1회지만 쿼터는 배치 안의 텍스트 수만큼 깎이므로, 배치 크기를
    weight 로 넘겨야 실제 한도에 맞는 간격이 나온다.

    문서에 적힌 한도와 실제가 다르고 모델마다 제각각이라, 429 를 맞으면
    스스로 느려지고(throttled) 성공이 이어지면 서서히 원래 속도로 돌아온다(succeeded).
    """

    MAX_PENALTY = 8.0

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, model: str, weight: int = 1) -> None:
        rpm = DEFAULT_RPM.get(model, 10)
        with self._lock:
            penalty = self._penalty.get(model, 1.0)
            interval = 60.0 / max(1, rpm) * max(1, weight) * penalty
            last = self._last.get(model, 0.0)
            delay = interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
            self._last[model] = time.monotonic()

    def throttled(self, model: str) -> float:
        """429 를 맞았다. 이 모델의 호출 간격을 늘린다."""
        with self._lock:
            penalty = min(self._penalty.get(model, 1.0) * 1.6, self.MAX_PENALTY)
            self._penalty[model] = penalty
            return penalty

    def succeeded(self, model: str) -> None:
        """성공하면 아주 조금씩 원래 속도로 되돌린다."""
        with self._lock:
            penalty = self._penalty.get(model, 1.0)
            if penalty > 1.0:
                self._penalty[model] = max(1.0, penalty * 0.97)


_limiter = _RateLimiter()


def api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY 가 없습니다. .env 에 넣으세요.\n"
            "  https://aistudio.google.com/apikey (무료 등급)"
        )
    return key


def _quota_violation(response: requests.Response) -> tuple[bool, str | None]:
    """429 응답에서 일일 한도 위반인지와 그 한도값을 뽑는다.

    반환: (일일한도_위반인가, 한도값)
    """
    try:
        details = response.json().get("error", {}).get("details", []) or []
    except ValueError:
        return False, None
    for detail in details:
        for violation in detail.get("violations", []) or []:
            quota_id = violation.get("quotaId", "")
            if "PerDay" in quota_id:
                return True, violation.get("quotaValue")
    return False, None


def _post(
    url: str,
    payload: dict,
    model: str,
    timeout: int = 180,
    max_retries: int = 5,
    weight: int = 1,
) -> dict:
    last_err: Exception | None = None
    for attempt in range(max_retries):
        _limiter.wait(model, weight)
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
            _limiter.succeeded(model)
            return r.json()

        body = r.text[:500]
        if r.status_code == 429:
            # 일일 한도와 분당 한도는 대응이 다르다. 일일 한도면 기다려도 소용없으므로
            # 즉시 멈추고 다음 실행에 넘긴다.
            # 본문을 잘라서 문자열로 검사하면 quotaId 가 500자 뒤에 있을 때 놓친다.
            per_day, quota_value = _quota_violation(r)
            if per_day:
                raise QuotaExceeded(
                    f"Gemini 일일 무료 쿼터를 다 썼습니다 "
                    f"(model={model}, 한도={quota_value}회/일).\n"
                    f"다음 실행이 남은 분량부터 이어서 처리합니다."
                )
            penalty = _limiter.throttled(model)
            wait = min(2 ** attempt * 5 + random.random() * 3, 90)
            print(
                f"    [rate limit] {wait:.0f}초 대기 후 재시도 "
                f"({attempt + 1}/{max_retries}, 이후 간격 x{penalty:.1f})"
            )
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
    batch_size: int = 20,
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
        data = _post(
            f"{BASE}/models/{model}:batchEmbedContents", payload, model, weight=len(chunk)
        )
        for item in data.get("embeddings", []):
            out.append(_normalize(item.get("values", [])))
    return out


def _normalize(vec: list[float]) -> list[float]:
    """MRL 로 차원을 줄이면 정규화가 깨지므로 다시 단위벡터로 만든다."""
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        return vec
    return [v / norm for v in vec]
