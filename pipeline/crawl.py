"""채널 크롤. 지정 기간(주 단위)의 메시지와 사진을 가져와 일자별 JSON 으로 저장한다.

    python -m pipeline.crawl --weeks 1
    python -m pipeline.crawl --weeks 4 --no-media

증분 동작:
  - 이미 저장된 메시지는 텍스트가 바뀐 경우(수정)에만 갱신한다.
  - 이미 내려받은 이미지는 다시 받지 않는다.
  - vision / view / market 등 후속 단계가 채운 필드는 보존한다.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import shutil
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)

from .config import KST, MEDIA_DIR, RAW_DIR, Config, ensure_dirs, load_config, require
from .tg_client import create_client, resolve_channel

# 후속 단계가 만들어낸 필드. 재크롤 시 덮어쓰지 않는다.
DERIVED_FIELDS = ("vision", "view", "market", "vision_model")


def _media_kind(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaWebPage):
        return "webpage"
    if isinstance(media, MessageMediaDocument):
        return "document"
    return type(media).__name__


def _doc_name(message: Any) -> str | None:
    media = getattr(message, "media", None)
    if not isinstance(media, MessageMediaDocument):
        return None
    doc = getattr(media, "document", None)
    for attr in getattr(doc, "attributes", []) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return None


def _serialize(message: Any, channel_id: str) -> dict:
    posted = getattr(message, "date", None)
    if posted is None:
        return {}
    posted_kst = posted.astimezone(KST)

    return {
        "id": int(message.id),
        "channel": channel_id,
        "date": posted_kst.isoformat(),
        "text": (getattr(message, "message", "") or "").strip(),
        "media": _media_kind(message),
        "file_name": _doc_name(message),
        "group_id": int(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        "reply_to": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "tg_views": int(getattr(message, "views", 0) or 0),
        "tg_forwards": int(getattr(message, "forwards", 0) or 0),
        "image": None,
    }


def _day_path(day: str) -> Path:
    return RAW_DIR / f"{day}.json"


def load_day(day: str) -> list[dict]:
    path = _day_path(day)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_day(day: str, messages: list[dict]) -> None:
    messages.sort(key=lambda m: (m["date"], m["id"]))
    path = _day_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def all_days() -> list[str]:
    return sorted(p.stem for p in RAW_DIR.glob("*.json"))


def load_range(start_day: str | None = None, end_day: str | None = None) -> list[dict]:
    """일자 범위의 메시지를 시간순으로 모아 준다."""
    out: list[dict] = []
    for day in all_days():
        if start_day and day < start_day:
            continue
        if end_day and day > end_day:
            continue
        out.extend(load_day(day))
    out.sort(key=lambda m: (m["date"], m["id"]))
    return out


def _merge_into_day(day: str, incoming: list[dict]) -> tuple[int, int]:
    """반환: (신규, 갱신)"""
    existing = {m["id"]: m for m in load_day(day)}
    added = updated = 0
    for msg in incoming:
        old = existing.get(msg["id"])
        if old is None:
            existing[msg["id"]] = msg
            added += 1
            continue
        # 본문이 바뀌었으면(수정) 갱신하되, 후속 단계 산출물은 보존
        changed = old.get("text") != msg["text"]
        for field in DERIVED_FIELDS:
            if field in old:
                msg[field] = old[field]
        if old.get("image") and not msg.get("image"):
            msg["image"] = old["image"]
        if changed:
            # 본문이 바뀌면 비전/분류 결과는 무효
            for field in DERIVED_FIELDS:
                msg.pop(field, None)
            updated += 1
        existing[msg["id"]] = msg
    save_day(day, list(existing.values()))
    return added, updated


async def _download_photo(client, message: Any, day: str, max_width: int) -> str | None:
    """사진을 webp 로 리사이즈해 저장하고 MEDIA_DIR 기준 상대경로를 반환."""
    rel = f"{day}/{message.id}.webp"
    dest = MEDIA_DIR / rel
    if dest.exists():
        return rel

    buf = io.BytesIO()
    try:
        await client.download_media(message, file=buf)
    except Exception as e:  # 이미지 하나 실패가 전체를 막지 않게
        print(f"    [!] 이미지 다운로드 실패 #{message.id}: {e}")
        return None

    buf.seek(0)
    try:
        img = Image.open(buf)
        img.load()
    except Exception as e:
        print(f"    [!] 이미지 디코드 실패 #{message.id}: {e}")
        return None

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, round(img.height * ratio))), Image.LANCZOS)

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "WEBP", quality=80, method=4)
    return rel


def prune_media(retention_days: int) -> int:
    """오래된 이미지 디렉터리 삭제. 텍스트 추출본은 raw JSON 에 남으므로 정보 손실 없음."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(KST) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    removed = 0
    for d in MEDIA_DIR.glob("*"):
        if d.is_dir() and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


async def crawl(
    weeks: int = 1,
    download_media: bool | None = None,
    cfg: Config | None = None,
    days: int | None = None,
) -> dict:
    """days 를 주면 weeks 대신 최근 N일만 가져온다 (시간별 증분 실행용).

    1주치를 매시간 훑으면 매번 800건 가까이 되짚게 된다. 당일 갱신에는
    2일이면 충분하고, 자정 전후에 걸친 메시지도 놓치지 않는다.
    """
    cfg = cfg or load_config()
    ensure_dirs()
    require("TG_API_ID", "TG_API_HASH", "TG_STRING_SESSION")

    channels = cfg.active_channels
    if not channels:
        raise RuntimeError(
            "config.yaml 에 활성 채널이 없습니다.\n"
            "telegram.channels 에서 active: true 인 항목의 link 에 invite 링크를 넣으세요."
        )

    if download_media is None:
        download_media = bool(cfg.get("crawl.download_media", True))
    max_width = int(cfg.get("crawl.media_max_width", 800))
    max_messages = int(cfg.get("crawl.max_messages", 4000))

    cutoff = (
        datetime.now(KST) - timedelta(days=days)
        if days
        else datetime.now(KST) - timedelta(weeks=weeks)
    )
    window = f"최근 {days}일" if days else f"최근 {weeks}주"
    stats: dict[str, Any] = {
        "weeks": weeks,
        "days": days,
        "cutoff": cutoff.isoformat(),
        "channels": [],
        "added": 0,
        "updated": 0,
        "images": 0,
        "started_at": datetime.now(KST).isoformat(),
    }

    client = create_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "텔레그램 세션이 만료됐습니다. `python -m pipeline.tg_auth` 로 재발급하세요."
            )

        for ch in channels:
            entity = await resolve_channel(client, ch["link"])
            title = getattr(entity, "title", None) or ch["id"]
            print(f"[채널] {title}  (기간: {window})")

            by_day: dict[str, list[dict]] = defaultdict(list)
            photo_msgs: list[tuple[str, Any]] = []
            count = 0

            async for message in client.iter_messages(entity, limit=max_messages):
                if message is None or getattr(message, "date", None) is None:
                    continue
                if message.date.astimezone(KST) < cutoff:
                    break
                rec = _serialize(message, ch["id"])
                if not rec:
                    continue
                day = rec["date"][:10]
                by_day[day].append(rec)
                if download_media and rec["media"] == "photo":
                    photo_msgs.append((day, message))
                count += 1

            print(f"  메시지 {count}건 / {len(by_day)}일치")

            if photo_msgs:
                print(f"  이미지 {len(photo_msgs)}건 확인 중...")
                index = {(d, m.id): None for d, m in photo_msgs}
                for i, (day, message) in enumerate(photo_msgs, 1):
                    rel = await _download_photo(client, message, day, max_width)
                    index[(day, message.id)] = rel
                    if rel:
                        stats["images"] += 1
                    if i % 50 == 0:
                        print(f"    {i}/{len(photo_msgs)}")
                for day, recs in by_day.items():
                    for rec in recs:
                        rel = index.get((day, rec["id"]))
                        if rel:
                            rec["image"] = rel

            for day, recs in sorted(by_day.items()):
                added, updated = _merge_into_day(day, recs)
                stats["added"] += added
                stats["updated"] += updated

            stats["channels"].append({"id": ch["id"], "title": title, "messages": count})
    finally:
        await client.disconnect()

    pruned = prune_media(int(cfg.get("crawl.media_retention_days", 28)))
    stats["pruned_media_days"] = pruned
    stats["finished_at"] = datetime.now(KST).isoformat()
    print(
        f"[완료] 신규 {stats['added']}건 · 갱신 {stats['updated']}건 · "
        f"이미지 {stats['images']}장 · 오래된 이미지 {pruned}일치 정리"
    )
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="대상 채널 크롤")
    p.add_argument("--weeks", type=int, default=None, help="최근 N주 (기본: config.yaml)")
    p.add_argument("--days", type=int, default=None, help="최근 N일 (weeks 보다 우선)")
    p.add_argument("--no-media", action="store_true", help="이미지 다운로드 건너뛰기")
    args = p.parse_args()

    cfg = load_config()
    weeks = args.weeks or int(cfg.get("crawl.default_weeks", 1))
    allowed = cfg.get("crawl.allowed_weeks", [1, 2, 3, 4])
    if not args.days and weeks not in allowed:
        raise SystemExit(f"--weeks 는 {allowed} 중 하나여야 합니다 (받은 값: {weeks})")

    asyncio.run(crawl(
        weeks=weeks, days=args.days,
        download_media=False if args.no_media else None, cfg=cfg,
    ))


if __name__ == "__main__":
    main()
