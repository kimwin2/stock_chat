"""메시지 → (시장, 관점) 분류.

각 메시지에 market(kr/us/both) 과 view(관점 8+2종) 라벨을 붙인다.
요약 단계가 이 라벨로 메시지를 모아서 관점별 카드를 만든다.

    python -m pipeline.classify
    python -m pipeline.classify --days 3 --force
"""

from __future__ import annotations

import argparse

from .config import ensure_dirs, load_config, require
from .crawl import all_days, load_day, save_day
from .llm import QuotaExceeded, generate_json
from .views import GLOSSARY, MARKETS, VIEW_IDS, views_for_prompt

SYSTEM = f"""\
당신은 한국 주식 텔레그램 채널의 메시지 분류기다.
운영자는 펀드매니저 출신 개인 투자자이며, 하루 200건가량의 짧은 코멘트를 실시간으로 올린다.

{GLOSSARY}
"""

PROMPT_TMPL = """\
아래 메시지 각각에 대해 **시장**과 **관점** 라벨을 하나씩 붙여라.

[시장] 이 메시지가 어느 시장에 대한 이야기인가
- kr: 한국 증시(국장). 코스피/코스닥, 국내 종목, 국내 수급
- us: 미국 증시(미장). 나스닥/S&P, 미국 종목·ETF, 미국 지표
- both: 양쪽 다이거나 어느 쪽인지 구분이 무의미한 것 (거시, 원칙, 잡담 등)

판단 힌트: 게시 시각도 참고하라. 05~07시와 21~24시는 미장 이야기일 확률이 높고,
08~19시는 국장일 확률이 높다. 다만 내용이 우선이다.

[관점]
{views}

규칙:
- 입력의 모든 id 에 대해 정확히 한 줄씩 출력한다. 빠뜨리지 마라.
- 애매하면 가장 가까운 것 하나를 고른다. 새 라벨을 만들지 마라.
- 텍스트가 비어 있고 이미지 판독 내용만 있으면 그 내용으로 판단한다.

JSON 배열만 출력하라:
[{{"id": 12345, "market": "kr", "view": "cash"}}, ...]

=== 메시지 ===
{items}
"""


def _line(msg: dict, max_chars: int) -> str:
    time = msg["date"][11:16]
    text = (msg.get("text") or "").replace("\n", " ").strip()
    vision = (msg.get("vision") or "").replace("\n", " ").strip()

    body = text
    if vision:
        body = f"{body} [이미지: {vision}]" if body else f"[이미지: {vision}]"
    if not body:
        body = "(내용 없음)"
    return f"[{msg['id']}] {time} {body[:max_chars]}"


def _pending(messages: list[dict], force: bool) -> list[dict]:
    if force:
        return list(messages)
    return [m for m in messages if not m.get("view")]


def run(days: int | None = None, force: bool = False, model: str | None = None) -> dict:
    cfg = load_config()
    ensure_dirs()
    require("GEMINI_API_KEY")
    model = model or cfg.get("models.classify", "gemini-2.5-flash-lite")
    batch_size = int(cfg.get("models.classify_batch", 60))
    views_text = views_for_prompt()

    day_list = all_days()
    if days:
        day_list = day_list[-days:]

    stats = {"labeled": 0, "batches": 0, "failed": 0, "quota_hit": False}

    for day in day_list:
        messages = load_day(day)
        pending = _pending(messages, force)
        if not pending:
            continue

        by_id = {m["id"]: m for m in messages}
        print(f"[{day}] 분류 대상 {len(pending)}건")
        dirty = False

        for i in range(0, len(pending), batch_size):
            batch = pending[i : i + batch_size]
            prompt = PROMPT_TMPL.format(
                views=views_text,
                items="\n".join(_line(m, 400) for m in batch),
            )
            try:
                result = generate_json(model, prompt, system=SYSTEM, temperature=0.0)
            except QuotaExceeded as e:
                print(f"  [쿼터] {e}")
                stats["quota_hit"] = True
                if dirty:
                    save_day(day, messages)
                return stats
            except Exception as e:
                print(f"  [!] 분류 실패 (batch {i // batch_size + 1}): {e}")
                stats["failed"] += len(batch)
                continue

            stats["batches"] += 1
            if isinstance(result, dict):
                result = result.get("items") or result.get("results") or []

            got = 0
            for item in result if isinstance(result, list) else []:
                if not isinstance(item, dict):
                    continue
                msg = by_id.get(item.get("id"))
                if msg is None:
                    continue
                market = item.get("market")
                view = item.get("view")
                msg["market"] = market if market in MARKETS else "both"
                msg["view"] = view if view in VIEW_IDS else "etc"
                got += 1
                dirty = True

            stats["labeled"] += got
            missed = len(batch) - got
            if missed > 0:
                stats["failed"] += missed
                print(f"  [!] {missed}건 라벨 누락 — 다음 실행에서 재시도합니다.")

        if dirty:
            save_day(day, messages)

    print(f"[완료] {stats['labeled']}건 분류 ({stats['batches']}배치)"
          + (f" · 미처리 {stats['failed']}건" if stats["failed"] else ""))
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="메시지 시장/관점 분류")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--force", action="store_true", help="이미 분류된 것도 다시")
    p.add_argument("--model", default=None)
    args = p.parse_args()
    run(days=args.days, force=args.force, model=args.model)


if __name__ == "__main__":
    main()
