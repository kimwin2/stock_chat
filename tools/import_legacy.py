"""stock_test 프로젝트에 남아 있는 예전 채널 덤프를 새 포맷으로 가져온다.

예전 채널(2026 상반기)은 6월 26일에 닫혔지만 3,400건 넘는 실제 글이 남아 있다.
가져오면 RAG 가 처음부터 과거 맥락을 갖게 된다. 이미지는 없으므로 텍스트만 들어간다.

    python -m tools.import_legacy
    python -m tools.import_legacy --src /path/to/dev --channel ch_2026h1
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.config import ensure_dirs  # noqa: E402
from pipeline.crawl import load_day, save_day  # noqa: E402

DEFAULT_SRC = os.path.expanduser("~/repo/stock_test/backend/telegram/dev")
MEDIA_MAP = {
    "MessageMediaPhoto": "photo",
    "MessageMediaDocument": "document",
    "MessageMediaWebPage": "webpage",
}


def main() -> None:
    p = argparse.ArgumentParser(description="예전 채널 덤프 가져오기")
    p.add_argument("--src", default=DEFAULT_SRC, help="예전 덤프 JSON 이 있는 디렉터리")
    # 같은 디렉터리에 다른 채널의 덤프가 섞여 있을 수 있으므로 기본값을 두지 않는다.
    # 엉뚱한 채널을 섞어 넣으면 요약이 통째로 오염된다.
    p.add_argument("--pattern", required=True, help="덤프 파일 glob (예: 'mydump_*.json')")
    p.add_argument("--channel", default="ch_2026h1", help="config.yaml 의 채널 id")
    args = p.parse_args()

    ensure_dirs()
    files = sorted(glob.glob(os.path.join(args.src, args.pattern)))
    if not files:
        raise SystemExit(f"[X] 덤프를 찾지 못했습니다: {args.src}/{args.pattern}")

    # 여러 덤프에 같은 메시지가 겹쳐 있다. id 기준으로 합치되 본문이 긴 쪽을 남긴다.
    merged: dict[int, dict] = {}
    titles = set()
    for path in files:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "messages" not in data:
            print(f"  건너뜀 (덤프 형식 아님): {os.path.basename(path)}")
            continue
        titles.add(data.get("channel_title") or "?")
        for m in data.get("messages", []):
            mid = m.get("id")
            if not mid or not m.get("date"):
                continue
            old = merged.get(mid)
            if old is None or len(m.get("text") or "") > len(old.get("text") or ""):
                merged[mid] = m
        print(f"  읽음: {os.path.basename(path)} ({len(data.get('messages', []))}건)")

    if len(titles) > 1:
        raise SystemExit(
            "[X] 서로 다른 채널의 덤프가 섞여 있습니다. --pattern 을 좁히세요:\n"
            + "\n".join(f"    - {t}" for t in sorted(titles))
        )

    by_day: dict[str, list[dict]] = defaultdict(list)
    for m in merged.values():
        rec = {
            "id": int(m["id"]),
            "channel": args.channel,
            "date": m["date"],
            "text": (m.get("text") or "").strip(),
            "media": MEDIA_MAP.get(m.get("media") or "", None),
            "file_name": None,
            "group_id": None,
            "reply_to": m.get("reply_to_msg_id"),
            "tg_views": int(m.get("views") or 0),
            "tg_forwards": int(m.get("forwards") or 0),
            # 예전 덤프에는 이미지 파일이 없다. 비전 단계가 계속 재시도하지 않도록
            # image 를 None 으로 두고 vision 을 빈 문자열로 못 박는다.
            "image": None,
            "vision": "",
        }
        by_day[rec["date"][:10]].append(rec)

    total_new = 0
    for day, recs in sorted(by_day.items()):
        existing = {x["id"]: x for x in load_day(day)}
        before = len(existing)
        for rec in recs:
            if rec["id"] not in existing:
                existing[rec["id"]] = rec
                total_new += 1
        save_day(day, list(existing.values()))
        print(f"  [{day}] {len(existing)}건 (신규 {len(existing) - before})")

    print(f"\n[완료] {len(by_day)}일치 · 신규 {total_new}건 저장")
    print("이미지가 없는 과거 데이터이므로 비전 단계는 건너뜁니다.")
    print("다음: python -m pipeline.run --only classify summarize index bundle")


if __name__ == "__main__":
    main()
