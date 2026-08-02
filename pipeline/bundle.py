"""배포 번들 생성 — 모든 데이터를 AES-256-GCM 으로 암호화해서 web/data/ 에 쓴다.

레포는 public 이지만 평문 콘텐츠가 하나도 없다. 공유 암호를 아는 사람만 읽을 수 있다.

파일 구성 (전부 같은 키로 암호화):
  manifest.json     암호화 안 함. KDF 파라미터와 파일 목록만. 콘텐츠 없음.
  core.enc          요약 + 설정 + 관점 정의 + 비밀키. 첫 화면에 필요한 것 전부.
  search.enc        RAG 청크와 임베딩. 처음 질문할 때 지연 로딩.
  msgs-YYYY-MM.enc  원문 메시지. 근거 펼칠 때 지연 로딩.
  img-YYYY-MM-DD.enc 요약이 인용한 이미지. 해당 날짜를 열 때 지연 로딩.

    python -m pipeline.bundle
"""

from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from PIL import Image

from .config import (
    DAILY_DIR,
    KST,
    MEDIA_DIR,
    WEB_DATA_DIR,
    ensure_dirs,
    load_config,
    require,
)
from .crawl import all_days, load_day
from .index import load_index, quantize
from .summarize import _weekday, load_daily
from .views import GLOSSARY, VIEWS

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12
THUMB_WIDTH = 480


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt(payload: dict, key: bytes) -> bytes:
    """gzip(JSON) → AES-GCM. 출력은 iv(12) || ciphertext+tag."""
    plain = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
    iv = os.urandom(IV_BYTES)
    ct = AESGCM(key).encrypt(iv, plain, None)
    return iv + ct


def _write(name: str, blob: bytes) -> int:
    path = WEB_DATA_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return len(blob)


def _thumb(path: Path, width: int = THUMB_WIDTH) -> str | None:
    try:
        img = Image.open(path)
        img.load()
    except Exception:
        return None
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > width:
        ratio = width / img.width
        img = img.resize((width, max(1, round(img.height * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=72, method=4)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _referenced_ids(daily: dict) -> set[int]:
    ids: set[int] = set()
    for section in (daily.get("markets") or {}).values():
        for item in section.values():
            ids.update(i for i in (item.get("refs") or []) if isinstance(i, int))
    for q in daily.get("quotes") or []:
        if isinstance(q.get("id"), int):
            ids.add(q["id"])
    return ids


def run(passphrase: str | None = None) -> dict:
    cfg = load_config()
    ensure_dirs()
    require("SHARE_PASSPHRASE", "GEMINI_API_KEY")
    passphrase = passphrase or cfg.share_passphrase

    salt = os.urandom(SALT_BYTES)
    key = derive_key(passphrase, salt)
    now = datetime.now(KST)

    files: list[str] = []
    sizes: dict[str, int] = {}

    # ── core.enc ──────────────────────────────────────────
    days = sorted(p.stem for p in DAILY_DIR.glob("*.json"))
    archive_days = int(cfg.get("summary.archive_days", 400))
    cutoff = (now - timedelta(days=archive_days)).strftime("%Y-%m-%d")
    days = [d for d in days if d >= cutoff]

    summaries = [s for s in (load_daily(d) for d in days) if s]

    # 오늘 글은 올라왔는데 아직 요약이 없을 수 있다 (이른 아침엔 몇 건뿐이라
    # summarize 가 건너뛴다). 그래도 화면에는 오늘 칩과 실시간 스트림이 떠야 하므로
    # 빈 껍데기 항목을 만들어 준다. 요약이 생기면 자연히 대체된다.
    today = now.strftime("%Y-%m-%d")
    if not any(s["date"] == today for s in summaries):
        todays = [
            m for m in load_day(today)
            if (m.get("text") or "").strip() or (m.get("vision") or "").strip()
        ]
        if todays:
            summaries.append({
                "date": today,
                "weekday": _weekday(today),
                "headline": "오늘은 아직 요약할 만큼 글이 쌓이지 않았습니다.",
                "stance": "",
                "stance_reason": "",
                "cash": {
                    "kr": {"start": None, "end": None, "basis": "", "note": ""},
                    "us": {"start": None, "end": None, "basis": "", "note": ""},
                },
                "changes": [],
                "markets": {},
                "quotes": [],
                "tickers": [],
                "sectors": [],
                "message_count": len(load_day(today)),
                "image_count": sum(1 for m in load_day(today) if m.get("media") == "photo"),
                "pending": True,
            })
            summaries.sort(key=lambda s: s["date"])

    core = {
        "version": 1,
        "updated_at": now.isoformat(),
        "channel_labels": {c["id"]: c.get("label", c["id"]) for c in cfg.channels},
        "settings": {
            "answer_model": cfg.get("models.answer", "gemini-2.5-flash"),
            "embed_model": cfg.get("models.embed", "gemini-embedding-001"),
            "embed_dim": int(cfg.get("models.embed_dim", 256)),
            "top_k": int(cfg.get("rag.top_k", 24)),
        },
        "secrets": {
            "gemini_key": cfg.gemini_key,
            "gh_repo": cfg.gh_repo,
            "gh_token": cfg.gh_dispatch_token,
        },
        "views": [
            {k: v[k] for k in ("id", "order", "label", "icon", "desc", "primary")} for v in VIEWS
        ],
        "glossary": GLOSSARY,
        "days": summaries,
    }
    sizes["core.enc"] = _write("core.enc", encrypt(core, key))
    files.append("core.enc")

    # ── search.enc ────────────────────────────────────────
    chunks, vectors = load_index()
    if chunks and vectors is not None and len(vectors):
        b64, rows, dim = quantize(np.asarray(vectors, dtype=np.float32))
        search = {
            "chunks": [
                {k: c[k] for k in ("id", "date", "t0", "t1", "market", "view_labels", "msg_ids", "text")}
                for c in chunks
            ],
            "vectors": {"b64": b64, "rows": rows, "dim": dim, "scale": 127.0},
        }
        sizes["search.enc"] = _write("search.enc", encrypt(search, key))
        files.append("search.enc")
    else:
        print("[!] RAG 인덱스가 없습니다. `python -m pipeline.index` 를 먼저 실행하세요.")

    # ── msgs-YYYY-MM.enc ──────────────────────────────────
    by_month: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for day in all_days():
        if day < cutoff:
            continue
        slim = []
        for m in load_day(day):
            if not ((m.get("text") or "").strip() or (m.get("vision") or "").strip()):
                continue
            slim.append({
                "id": m["id"],
                "t": m["date"][11:16],
                "text": m.get("text") or "",
                "vision": m.get("vision") or "",
                "market": m.get("market") or "both",
                "view": m.get("view") or "etc",
                "img": bool(m.get("image")),
            })
        if slim:
            by_month[day[:7]][day] = slim

    for month, payload in sorted(by_month.items()):
        name = f"msgs-{month}.enc"
        sizes[name] = _write(name, encrypt({"month": month, "days": payload}, key))
        files.append(name)

    # ── img-YYYY-MM-DD.enc ────────────────────────────────
    image_days = int(cfg.get("crawl.image_pack_days", 7))
    if image_days > 0:
        img_cutoff = (now - timedelta(days=image_days)).strftime("%Y-%m-%d")
        for day in [d for d in days if d >= img_cutoff]:
            daily = load_daily(day)
            if not daily:
                continue
            wanted = _referenced_ids(daily)
            if not wanted:
                continue
            pack: dict[str, str] = {}
            for m in load_day(day):
                if m["id"] not in wanted or not m.get("image"):
                    continue
                src = MEDIA_DIR / m["image"]
                if not src.exists():
                    continue
                data = _thumb(src)
                if data:
                    pack[str(m["id"])] = data
            if pack:
                name = f"img-{day}.enc"
                sizes[name] = _write(name, encrypt({"day": day, "images": pack}, key))
                files.append(name)

    # 보관 기간이 지난 이미지 팩 삭제
    for old in WEB_DATA_DIR.glob("img-*.enc"):
        day = old.stem[4:]
        if image_days > 0 and day < (now - timedelta(days=image_days)).strftime("%Y-%m-%d"):
            old.unlink()

    # ── manifest.json (평문, 콘텐츠 없음) ──────────────────
    manifest = {
        "version": 1,
        "updated_at": now.isoformat(),
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "cipher": "AES-GCM",
        "files": files,
    }
    with open(WEB_DATA_DIR / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)

    total = sum(sizes.values())
    print(f"[번들] {len(files)}개 파일 · 합계 {total / 1024 / 1024:.2f} MB")
    for name in sorted(sizes, key=lambda n: -sizes[n])[:8]:
        print(f"   {name:24s} {sizes[name] / 1024:8.0f} KB")
    print(f"   요약 {len(summaries)}일 · 청크 {len(chunks)}개")
    return {"files": files, "bytes": total, "days": len(summaries)}


def main() -> None:
    p = argparse.ArgumentParser(description="암호화 배포 번들 생성")
    p.add_argument("--passphrase", default=None, help="기본값은 .env 의 SHARE_PASSPHRASE")
    args = p.parse_args()
    run(passphrase=args.passphrase)


if __name__ == "__main__":
    main()
