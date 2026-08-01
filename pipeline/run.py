"""전체 파이프라인 실행. GitHub Actions 와 UI 버튼이 이걸 호출한다.

    python -m pipeline.run                    # 최근 1주 (config 기본값)
    python -m pipeline.run --weeks 4
    python -m pipeline.run --skip crawl       # 이미 받아둔 데이터로 요약만
    python -m pipeline.run --vision-limit 40  # 무료 쿼터 아끼기

각 단계는 이어달리기다. 쿼터가 소진되면 거기까지 저장하고 정상 종료하며,
다음 실행이 남은 분량부터 이어서 처리한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from datetime import datetime

from . import bundle, classify, index, summarize, vision
from .config import DATA_DIR, KST, STATE_PATH, ConfigError, ensure_dirs, load_config
from .crawl import crawl

STEPS = ["crawl", "vision", "classify", "summarize", "index", "bundle"]


def _save_state(state: dict) -> None:
    ensure_dirs()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main() -> int:
    p = argparse.ArgumentParser(description="전체 파이프라인")
    p.add_argument("--weeks", type=int, default=None, help="크롤 기간 (1~4)")
    p.add_argument("--skip", nargs="*", default=[], choices=STEPS, help="건너뛸 단계")
    p.add_argument("--only", nargs="*", default=None, choices=STEPS, help="이 단계만 실행")
    p.add_argument("--vision-limit", type=int, default=None, help="비전 API 호출 상한")
    p.add_argument("--force-summary", action="store_true", help="기존 요약도 재생성")
    p.add_argument("--no-media", action="store_true", help="이미지 다운로드 건너뛰기")
    args = p.parse_args()

    cfg = load_config()
    weeks = args.weeks or int(cfg.get("crawl.default_weeks", 1))
    allowed = cfg.get("crawl.allowed_weeks", [1, 2, 3, 4])
    if weeks not in allowed:
        print(f"[X] --weeks 는 {allowed} 중 하나여야 합니다 (받은 값: {weeks})", file=sys.stderr)
        return 2

    steps = args.only if args.only else [s for s in STEPS if s not in args.skip]
    started = datetime.now(KST)
    state: dict = {
        "started_at": started.isoformat(),
        "weeks": weeks,
        "steps": {},
        "ok": True,
    }

    for step in STEPS:
        if step not in steps:
            continue
        print(f"\n{'=' * 60}\n  {step}\n{'=' * 60}")
        try:
            if step == "crawl":
                result = asyncio.run(
                    crawl(weeks=weeks, download_media=False if args.no_media else None, cfg=cfg)
                )
            elif step == "vision":
                result = vision.run(limit=args.vision_limit)
            elif step == "classify":
                result = classify.run()
            elif step == "summarize":
                result = summarize.run(force=args.force_summary)
            elif step == "index":
                result = index.run()
            elif step == "bundle":
                result = bundle.run()
            else:
                continue
            state["steps"][step] = result
        except ConfigError as e:
            # 설정 누락은 사용자 실수지 버그가 아니다. 스택트레이스로 덮지 않는다.
            print(f"\n[X] {step} 단계 실패: {e}", file=sys.stderr)
            state["steps"][step] = {"error": str(e)}
            state["ok"] = False
            break
        except Exception as e:
            print(f"\n[X] {step} 단계 실패: {e}", file=sys.stderr)
            traceback.print_exc()
            state["steps"][step] = {"error": str(e)}
            state["ok"] = False
            # 번들만은 남은 데이터로라도 만들어 둔다
            if step != "bundle" and "bundle" in steps:
                continue
            break

    finished = datetime.now(KST)
    state["finished_at"] = finished.isoformat()
    state["elapsed_sec"] = round((finished - started).total_seconds())
    _save_state(state)

    print(f"\n{'=' * 60}")
    print(f"  {'완료' if state['ok'] else '일부 실패'} · {state['elapsed_sec']}초")
    print(f"{'=' * 60}")
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
