"""일일 요약 생성. 국장/미장으로 나누고 관점별로 채운다.

하루치 메시지 전체(이미지 판독 텍스트 포함)를 한 번에 넣고 구조화된 JSON 을 받는다.
전날 요약을 같이 넣어서 "어제 대비 무엇이 바뀌었는지"를 뽑게 한다.

    python -m pipeline.summarize                # 요약 없는 날짜 전부
    python -m pipeline.summarize --days 3 --force
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import DAILY_DIR, ensure_dirs, load_config, require
from .crawl import all_days, load_day
from .llm import QuotaExceeded, generate_json
from .views import GLOSSARY, SUMMARY_VIEW_IDS, VIEW_BY_ID, views_for_prompt

# 시장별로 채울 관점
KR_VIEWS = ["cash", "regime", "leaders", "flow", "trades", "signals"]
US_VIEWS = ["cash", "regime", "leaders", "trades", "signals"]
COMMON_VIEWS = ["macro", "principles", "news"]

SYSTEM = f"""\
당신은 한 개인 투자자가 운영하는 한국 주식 텔레그램 채널의 하루치 글을 정리하는 애널리스트다.

운영자는 펀드매니저 출신 개인 투자자다. 그는 하루 200건가량의 짧은 코멘트를 실시간으로 올리며,
모든 판단이 결국 "포트폴리오 현금 비중 몇 %"로 수렴한다.

{GLOSSARY}

정리 원칙:
1. **숫자를 절대 뭉개지 마라.** 현금비중 %, 종목 비중 %, 지수 등락률, 이평선 일수, MDD % 는 원문 그대로 옮긴다.
2. **그의 판단과 사실을 섞지 마라.** "그는 ~라고 본다" 처럼 주체를 분명히 한다.
3. 원문에 없는 것을 추론해서 채우지 마라. 해당 관점에 내용이 없으면 summary 를 빈 문자열로 두고 bullets 를 비운다.
4. 시간 흐름이 중요한 관점(현금비중, 매매)은 **시간 순서대로** 적는다. 하루 중 입장이 바뀌면 그 변화가 핵심이다.
5. 종목 별칭은 정식 명칭으로 풀어 쓰되 괄호로 원문 표현을 남긴다. 예: SK하이닉스(닉스).
6. 문체는 담백한 평서문. 과장·감탄사·마케팅 표현 금지.
7. **refs 는 필수다.** 관점 하나를 채울 때마다 그 근거가 된 메시지 id 를 2~6개 넣어라.
   입력의 각 메시지는 `[12345] 09:42 <kr/cash> 본문` 형태이고 대괄호 안 숫자가 id 다.
   refs 가 빈 배열이면 그 관점은 검증 불가능한 요약이 되므로 절대 비워두지 마라.
   본문에는 id 를 쓰지 말고 refs 배열에만 넣어라.
"""

PROMPT_TMPL = """\
{date} ({weekday}) 하루치 메시지다. 아래 JSON 스키마에 맞춰 정리하라.

{prev_block}

=== 관점 정의 ===
{views}

=== 출력 스키마 ===
{{
  "headline": "오늘 하루를 한 문장으로. 현금비중이나 스탠스 변화가 있으면 그것을 우선한다. 40자 내외",
  "stance": "공격" | "중립" | "방어",
  "stance_reason": "그렇게 판단한 근거 한 문장",
  "cash": {{
    "kr": {{"start": 숫자 또는 null, "end": 숫자 또는 null, "note": "변화 이유 한 문장"}},
    "us": {{"start": 숫자 또는 null, "end": 숫자 또는 null, "note": "..."}}
  }},
  "changes": ["어제 대비 달라진 것 (전날 요약이 있을 때만). 최대 3개"],
  "markets": {{
    "kr": {{
      "<관점id>": {{
        "refs": [12345, 12350, 12361],   // 필수. 근거 메시지 id 2~6개. 빈 배열 금지
        "summary": "2~4문장",
        "bullets": ["핵심 항목"]
      }}
    }},
    "us": {{ ... }},
    "common": {{ ... }}
  }},
  "quotes": [{{"id": 메시지id, "time": "HH:MM", "text": "그날 가장 중요한 그의 발언 원문 그대로. 3개"}}],
  "tickers": ["오늘 언급된 종목 정식명칭. 최대 15개"],
  "sectors": ["오늘 언급된 섹터. 최대 10개"]
}}

markets.kr 에 채울 관점: {kr_views}
markets.us 에 채울 관점: {us_views}
markets.common 에 채울 관점: {common_views}

cash.start 는 그날 처음 언급된 현금비중, cash.end 는 마지막으로 언급된 현금비중이다.
언급이 없으면 null. 추측하지 마라.

=== {date} 메시지 ({count}건) ===
{body}
"""


def _fmt_message(msg: dict) -> str:
    time = msg["date"][11:16]
    market = msg.get("market") or "?"
    view = msg.get("view") or "?"
    text = (msg.get("text") or "").strip()
    vision = (msg.get("vision") or "").strip()

    parts = [f"[{msg['id']}] {time} <{market}/{view}>"]
    if text:
        parts.append(text)
    if vision:
        parts.append(f"(이미지) {vision}")
    if msg.get("file_name"):
        parts.append(f"(첨부파일) {msg['file_name']}")
    return "\n".join(parts)


def _weekday(day: str) -> str:
    import datetime as dt

    names = ["월", "화", "수", "목", "금", "토", "일"]
    return names[dt.date.fromisoformat(day).weekday()]


def daily_path(day: str) -> Path:
    return DAILY_DIR / f"{day}.json"


def load_daily(day: str) -> dict | None:
    path = daily_path(day)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _prev_block(days: list[str], idx: int) -> str:
    if idx == 0:
        return ""
    prev = load_daily(days[idx - 1])
    if not prev:
        return ""
    brief = {
        "date": prev.get("date"),
        "headline": prev.get("headline"),
        "stance": prev.get("stance"),
        "cash": prev.get("cash"),
    }
    return (
        "=== 전날 요약 (변화 비교용) ===\n"
        + json.dumps(brief, ensure_ascii=False, indent=1)
        + "\n"
    )


def _clean(result: dict, day: str, messages: list[dict]) -> dict:
    """모델 출력 정리 — 스키마 보정과 존재하지 않는 ref 제거."""
    valid_ids = {m["id"] for m in messages}
    id_time = {m["id"]: m["date"][11:16] for m in messages}

    markets = result.get("markets") or {}
    cleaned_markets: dict[str, dict] = {}
    allowed = {"kr": KR_VIEWS, "us": US_VIEWS, "common": COMMON_VIEWS}
    for market, view_ids in allowed.items():
        section = markets.get(market) or {}
        out: dict[str, dict] = {}
        for vid in view_ids:
            item = section.get(vid) or {}
            summary = (item.get("summary") or "").strip()
            bullets = [b.strip() for b in (item.get("bullets") or []) if str(b).strip()]
            if not summary and not bullets:
                continue
            refs = [i for i in (item.get("refs") or []) if i in valid_ids][:6]
            out[vid] = {
                "label": VIEW_BY_ID[vid]["label"],
                "icon": VIEW_BY_ID[vid]["icon"],
                "summary": summary,
                "bullets": bullets,
                "refs": refs,
            }
        if out:
            cleaned_markets[market] = out

    quotes = []
    for q in (result.get("quotes") or [])[:3]:
        if not isinstance(q, dict):
            continue
        qid = q.get("id")
        text = (q.get("text") or "").strip()
        if not text:
            continue
        quotes.append({
            "id": qid if qid in valid_ids else None,
            "time": id_time.get(qid) or (q.get("time") or ""),
            "text": text,
        })

    def _cash(side: dict | None) -> dict:
        side = side or {}
        def num(v):
            try:
                return round(float(v)) if v is not None else None
            except (TypeError, ValueError):
                return None
        return {
            "start": num(side.get("start")),
            "end": num(side.get("end")),
            "note": (side.get("note") or "").strip(),
        }

    cash = result.get("cash") or {}
    return {
        "date": day,
        "weekday": _weekday(day),
        "headline": (result.get("headline") or "").strip(),
        "stance": (result.get("stance") or "").strip(),
        "stance_reason": (result.get("stance_reason") or "").strip(),
        "cash": {"kr": _cash(cash.get("kr")), "us": _cash(cash.get("us"))},
        "changes": [c.strip() for c in (result.get("changes") or []) if str(c).strip()][:3],
        "markets": cleaned_markets,
        "quotes": quotes,
        "tickers": [t.strip() for t in (result.get("tickers") or []) if str(t).strip()][:15],
        "sectors": [s.strip() for s in (result.get("sectors") or []) if str(s).strip()][:10],
        "message_count": len(messages),
        "image_count": sum(1 for m in messages if m.get("media") == "photo"),
    }


def run(days: int | None = None, force: bool = False, model: str | None = None) -> dict:
    cfg = load_config()
    ensure_dirs()
    require("GEMINI_API_KEY")
    model = model or cfg.get("models.summarize", "gemini-2.5-flash")

    day_list = all_days()
    if days:
        day_list = day_list[-days:]

    views_text = views_for_prompt(SUMMARY_VIEW_IDS)
    stats = {"summarized": 0, "skipped": 0, "failed": 0, "quota_hit": False}

    for idx, day in enumerate(day_list):
        if not force and daily_path(day).exists():
            stats["skipped"] += 1
            continue

        messages = load_day(day)
        # 요약 가치가 없는 것은 빼되, 분류 전이면 전부 넣는다
        useful = [
            m for m in messages
            if m.get("view") != "etc" and ((m.get("text") or "").strip() or (m.get("vision") or "").strip())
        ]
        if len(useful) < 5:
            print(f"[{day}] 메시지 {len(useful)}건 — 건너뜀")
            stats["skipped"] += 1
            continue

        body = "\n\n".join(_fmt_message(m) for m in useful)
        prompt = PROMPT_TMPL.format(
            date=day,
            weekday=_weekday(day),
            prev_block=_prev_block(day_list, idx),
            views=views_text,
            kr_views=", ".join(KR_VIEWS),
            us_views=", ".join(US_VIEWS),
            common_views=", ".join(COMMON_VIEWS),
            count=len(useful),
            body=body,
        )

        print(f"[{day}] 요약 생성 중... ({len(useful)}건, {len(body):,}자)")
        try:
            result = generate_json(
                model, prompt, system=SYSTEM, temperature=0.3, max_output_tokens=16384
            )
        except QuotaExceeded as e:
            print(f"  [쿼터] {e}")
            stats["quota_hit"] = True
            return stats
        except Exception as e:
            print(f"  [!] 요약 실패: {e}")
            stats["failed"] += 1
            continue

        if not isinstance(result, dict):
            print(f"  [!] 예상 밖 응답 형식: {type(result)}")
            stats["failed"] += 1
            continue

        daily = _clean(result, day, useful)
        with open(daily_path(day), "w", encoding="utf-8") as f:
            json.dump(daily, f, ensure_ascii=False, indent=1)
        stats["summarized"] += 1
        print(f"  → {daily['headline']}")

    print(f"[완료] 요약 {stats['summarized']}일치"
          + (f" · 건너뜀 {stats['skipped']}" if stats["skipped"] else "")
          + (f" · 실패 {stats['failed']}" if stats["failed"] else ""))
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="일일 요약 생성")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--force", action="store_true", help="이미 있는 요약도 다시")
    p.add_argument("--model", default=None)
    args = p.parse_args()
    run(days=args.days, force=args.force, model=args.model)


if __name__ == "__main__":
    main()
