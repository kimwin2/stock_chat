"""Gemini 키 없이 UI 를 검증하기 위한 목업 요약 생성기.

실제 메시지에서 정규식으로 현금비중·종목·섹터를 뽑아 요약 JSON 의 형태만 채운다.
문장 품질은 실제 파이프라인(pipeline.summarize)이 담당하고, 여기서는 화면이
제대로 그려지는지만 본다. 검증이 끝나면 data/daily/ 를 지우고 진짜 요약을 돌리면 된다.

    python -m tools.mock_summaries
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.config import DAILY_DIR, ensure_dirs  # noqa: E402
from pipeline.crawl import all_days, load_day  # noqa: E402
from pipeline.summarize import KR_VIEWS, US_VIEWS, COMMON_VIEWS, _weekday  # noqa: E402
from pipeline.views import VIEW_BY_ID  # noqa: E402

CASH_RE = re.compile(r"현금\s*(?:비중)?\s*(\d{1,3})\s*%")
SECTORS = ["반도체", "피지컬 AI", "전력", "방산", "바이오", "조선", "소비재", "우주", "금융", "로봇"]
TICKERS = ["SK하이닉스", "삼성전자", "SK스퀘어", "삼성물산", "기가비스", "브이엠", "SK",
           "마이크론", "엔비디아", "AMD", "ARM"]
INDICATORS = ["피어앤그리드 오실레이터", "소라티노", "업종쏠림지수", "RS", "임펄스", "10일선", "신고가"]


def cash_values(messages: list[dict], market: str) -> tuple[int | None, int | None]:
    vals: list[int] = []
    for m in messages:
        if (m.get("market") or "both") not in (market, "both"):
            continue
        for hit in CASH_RE.findall(m.get("text") or ""):
            v = int(hit)
            if 0 <= v <= 100:
                vals.append(v)
    if not vals:
        return None, None
    return vals[0], vals[-1]


def pick(messages: list[dict], words: list[str]) -> list[str]:
    found = Counter()
    for m in messages:
        text = (m.get("text") or "") + " " + (m.get("vision") or "")
        for w in words:
            if w in text:
                found[w] += 1
    return [w for w, _ in found.most_common()]


def section(messages: list[dict], view_ids: list[str], market: str) -> dict:
    out = {}
    pool = [m for m in messages if (m.get("text") or "").strip()]
    for i, vid in enumerate(view_ids):
        sample = pool[i * 7 : i * 7 + 3]
        if not sample:
            continue
        v = VIEW_BY_ID[vid]
        out[vid] = {
            "label": v["label"],
            "icon": v["icon"],
            "summary": f"[목업] {v['label']} 관점 요약 자리. 실제 파이프라인이 여기에 "
                       f"{market} 기준 2~4문장을 채운다.",
            "bullets": [(m.get("text") or "").split("\n")[0][:70] for m in sample],
            "refs": [m["id"] for m in sample],
        }
    return out


def main() -> None:
    ensure_dirs()
    days = all_days()
    prev_end = None
    made = 0

    for day in days:
        messages = [m for m in load_day(day) if (m.get("text") or "").strip()]
        if len(messages) < 5:
            continue

        kr_start, kr_end = cash_values(messages, "kr")
        us_start, us_end = cash_values(messages, "us")
        if kr_start is None:
            kr_start = kr_end = prev_end
        prev_end = kr_end if kr_end is not None else prev_end

        stance = "중립"
        if kr_end is not None:
            stance = "방어" if kr_end >= 35 else ("공격" if kr_end <= 12 else "중립")

        longest = max(messages, key=lambda m: len(m.get("text") or ""))
        quotes = sorted(messages, key=lambda m: -len(m.get("text") or ""))[:3]

        daily = {
            "date": day,
            "weekday": _weekday(day),
            "headline": f"[목업] {(longest.get('text') or '')[:38].strip()}",
            "stance": stance,
            "stance_reason": "목업 데이터라 실제 판단이 아니다. 현금비중 수치만 실제 원문에서 추출했다.",
            "cash": {
                "kr": {"start": kr_start, "end": kr_end,
                       "note": "원문에서 추출한 현금비중 언급" if kr_end is not None else ""},
                "us": {"start": us_start, "end": us_end, "note": ""},
            },
            "changes": ["[목업] 전일 대비 변화 자리"],
            "markets": {
                "kr": section(messages, KR_VIEWS, "국장"),
                "us": section(messages, US_VIEWS, "미장"),
                "common": section(messages, COMMON_VIEWS, "공통"),
            },
            "quotes": [{"id": q["id"], "time": q["date"][11:16],
                        "text": (q.get("text") or "")[:160]} for q in quotes],
            "tickers": pick(messages, TICKERS)[:15],
            "sectors": pick(messages, SECTORS)[:10],
            "message_count": len(load_day(day)),
            "image_count": sum(1 for m in load_day(day) if m.get("media") == "photo"),
        }

        with open(DAILY_DIR / f"{day}.json", "w", encoding="utf-8") as f:
            json.dump(daily, f, ensure_ascii=False, indent=1)
        made += 1

    print(f"[목업] 요약 {made}일치 생성 → {DAILY_DIR}")
    print("실제 요약을 만들려면 data/daily/ 를 비우고 `python -m pipeline.summarize` 를 실행하세요.")


if __name__ == "__main__":
    main()
