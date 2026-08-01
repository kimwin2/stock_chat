"""이미지 → 텍스트. Gemini 비전으로 차트·수급표 캡처의 내용을 뽑아낸다.

이 채널은 메시지의 64% 가 사진이고, 캡션이 아예 없는 사진도 많다.
사진을 버리면 요약의 절반이 날아가므로 여기가 파이프라인의 핵심이다.

    python -m pipeline.vision                 # 아직 처리 안 된 것 전부
    python -m pipeline.vision --days 3        # 최근 3일치만
    python -m pipeline.vision --limit 20      # 호출 20회까지만 (쿼터 아끼기)

앨범(grouped_id) 단위로 묶어 한 번에 보낸다 — 호출 수가 1/4 로 줄고,
같이 올라온 이미지끼리 맥락이 이어져 판독 품질도 올라간다.
"""

from __future__ import annotations

import argparse

from .config import MEDIA_DIR, ensure_dirs, load_config, require
from .crawl import all_days, load_day, save_day
from .llm import QuotaExceeded, generate_json
from .views import GLOSSARY

MAX_IMAGES_PER_CALL = 8

SYSTEM = f"""\
당신은 한국 주식 텔레그램 채널의 이미지 판독기다.
운영자는 펀드매니저 출신이며, 자기가 만든 지표 화면과 수급 표를 캡처해서 올린다.

{GLOSSARY}

판독 원칙:
- 표가 보이면 **열 이름과 상위 20행**을 숫자까지 그대로 옮긴다. 순위표는 순서가 곧 정보다.
  20행이 넘으면 전체 행수를 밝히고 나머지는 업종 분포로 요약한다.
- **숫자 부호를 절대 빠뜨리지 마라.** 한국 증시 표는 색으로 부호를 나타낸다 —
  파란색 셀 또는 값 앞의 '-' 는 **음수(하락)**, 빨간색은 양수(상승)다.
  파란 셀의 수익률은 반드시 마이너스를 붙여서 적는다. 이걸 틀리면 판독 전체가 무의미하다.
- 차트가 보이면 무슨 지표인지, 축이 무엇인지, 현재 값이 어느 구간인지(고점/저점/중립), 최근 방향을 적는다.
- 종목명·섹터명·티커는 빠짐없이 나열한다.
- 증권사 리포트나 뉴스 캡처면 제목과 핵심 수치를 옮긴다.
- 읽을 수 없거나 의미 없는 이미지(이모티콘, 사진 등)는 content 를 빈 문자열로 두고 kind 를 "기타"로 한다.
- 추측해서 지어내지 말 것. 안 보이면 안 보인다고 할 것.
"""

PROMPT_TMPL = """\
아래는 같은 시각에 함께 올라온 이미지 {n}장이다.

[함께 올라온 캡션]
{caption}

각 이미지를 순서대로 판독해서 JSON 배열로 반환하라. 배열 길이는 정확히 {n}이어야 한다.

[
  {{
    "kind": "수급표" | "지표차트" | "종목차트" | "포트폴리오" | "뉴스캡처" | "리포트" | "기타",
    "title": "이미지 제목이나 지표명 (없으면 빈 문자열)",
    "content": "판독한 내용. 표는 숫자까지 그대로, 차트는 지표명·현재 위치·방향."
  }}
]
"""


def _pending(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if not m.get("image"):
            continue
        if m.get("vision") is not None:
            continue
        if not (MEDIA_DIR / m["image"]).exists():
            # 보관 기간이 지나 이미지가 삭제된 경우 — 다시 시도하지 않도록 표시
            m["vision"] = ""
            continue
        out.append(m)
    return out


def _group(messages: list[dict]) -> list[list[dict]]:
    """앨범(grouped_id) 단위로 묶는다. 단일 이미지는 자기 혼자 한 묶음."""
    groups: dict[object, list[dict]] = {}
    for m in messages:
        key = m.get("group_id") or f"solo-{m['id']}"
        groups.setdefault(key, []).append(m)

    out: list[list[dict]] = []
    for key in sorted(groups, key=lambda k: min(m["id"] for m in groups[k])):
        members = sorted(groups[key], key=lambda m: m["id"])
        for i in range(0, len(members), MAX_IMAGES_PER_CALL):
            out.append(members[i : i + MAX_IMAGES_PER_CALL])
    return out


def _caption_for(group: list[dict], day_messages: list[dict]) -> str:
    """앨범에 붙은 캡션. 앨범 멤버 중 텍스트가 있는 것, 없으면 앞뒤 메시지에서 찾는다."""
    texts = [m["text"] for m in group if m.get("text")]
    if texts:
        return "\n".join(texts)

    gid = group[0].get("group_id")
    if gid:
        for m in day_messages:
            if m.get("group_id") == gid and m.get("text"):
                return m["text"]

    # 앨범 캡션이 없으면 바로 다음/이전 메시지 텍스트를 맥락으로
    ids = {m["id"] for m in group}
    lo, hi = min(ids), max(ids)
    near = [
        m["text"]
        for m in day_messages
        if m.get("text") and (lo - 2 <= m["id"] <= hi + 2) and m["id"] not in ids
    ]
    return "\n".join(near[:2]) or "(캡션 없음)"


def _render(item: dict) -> str:
    kind = (item.get("kind") or "").strip()
    title = (item.get("title") or "").strip()
    content = (item.get("content") or "").strip()
    if not content:
        return ""
    head = " · ".join(x for x in (kind, title) if x)
    return f"[{head}] {content}" if head else content


def run(days: int | None = None, limit: int | None = None, model: str | None = None) -> dict:
    cfg = load_config()
    ensure_dirs()
    require("GEMINI_API_KEY")
    model = model or cfg.get("models.vision", "gemini-2.5-flash")

    day_list = all_days()
    if days:
        day_list = day_list[-days:]

    stats = {"calls": 0, "images": 0, "days": 0, "failed": 0, "quota_hit": False}

    for day in day_list:
        messages = load_day(day)
        pending = _pending(messages)
        if not pending:
            save_day(day, messages)  # 삭제된 이미지 표시가 있을 수 있으니 저장
            continue

        groups = _group(pending)
        print(f"[{day}] 이미지 {len(pending)}장 / 호출 {len(groups)}회")
        stats["days"] += 1
        dirty = False

        for group in groups:
            if limit is not None and stats["calls"] >= limit:
                print(f"  [중단] 호출 한도 {limit}회 도달. 나머지는 다음 실행에서 이어집니다.")
                if dirty:
                    save_day(day, messages)
                return stats

            paths = [MEDIA_DIR / m["image"] for m in group]
            prompt = PROMPT_TMPL.format(n=len(group), caption=_caption_for(group, messages))

            try:
                result = generate_json(
                    model, prompt, system=SYSTEM, images=paths, temperature=0.1
                )
            except QuotaExceeded as e:
                print(f"  [쿼터] {e}")
                stats["quota_hit"] = True
                if dirty:
                    save_day(day, messages)
                return stats
            except Exception as e:
                print(f"  [!] 판독 실패 (#{group[0]['id']}~): {e}")
                stats["failed"] += len(group)
                continue

            stats["calls"] += 1
            if isinstance(result, dict):
                result = result.get("images") or result.get("results") or [result]
            if not isinstance(result, list):
                print(f"  [!] 예상 밖 응답 형식: {type(result)}")
                stats["failed"] += len(group)
                continue

            for msg, item in zip(group, result):
                msg["vision"] = _render(item) if isinstance(item, dict) else ""
                msg["vision_model"] = model
                stats["images"] += 1
                dirty = True
            # 응답이 짧게 오면 남은 것은 다음 실행에서 재시도
            if len(result) < len(group):
                stats["failed"] += len(group) - len(result)

        if dirty:
            save_day(day, messages)

    print(
        f"[완료] 호출 {stats['calls']}회 · 이미지 {stats['images']}장 판독"
        + (f" · 실패 {stats['failed']}장" if stats["failed"] else "")
    )
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="이미지 → 텍스트 (Gemini 비전)")
    p.add_argument("--days", type=int, default=None, help="최근 N일치만 처리")
    p.add_argument("--limit", type=int, default=None, help="API 호출 횟수 상한")
    p.add_argument("--model", default=None)
    args = p.parse_args()
    run(days=args.days, limit=args.limit, model=args.model)


if __name__ == "__main__":
    main()
