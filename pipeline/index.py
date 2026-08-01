"""RAG 인덱스. 메시지를 시간 블록으로 묶고 임베딩한다.

메시지 하나하나는 "아직 기다려야 합니다." 처럼 너무 짧아서 단독으로는 검색이 안 된다.
그래서 연속된 메시지를 시간·분량 기준으로 묶어 하나의 청크로 만든다.
청크 수가 줄어드는 만큼 번들 용량도 줄고 검색 품질도 올라간다.

    python -m pipeline.index
    python -m pipeline.index --force
"""

from __future__ import annotations

import argparse
import base64
import json
from collections import Counter
from datetime import datetime, timedelta


import numpy as np

from .config import INDEX_DIR, KST, ensure_dirs, load_config, require
from .crawl import all_days, load_day
from .llm import QuotaExceeded, embed_batch
from .views import VIEW_BY_ID

CHUNKS_PATH = INDEX_DIR / "chunks.json"
VECTORS_PATH = INDEX_DIR / "vectors.npy"
MAX_GAP_MINUTES = 30


def _msg_text(msg: dict) -> str:
    text = (msg.get("text") or "").strip()
    vision = (msg.get("vision") or "").strip()
    if text and vision:
        return f"{text}\n(이미지) {vision}"
    return text or (f"(이미지) {vision}" if vision else "")


def _split_long(text: str, max_chars: int) -> list[str]:
    """장문 메시지를 문단 경계 우선으로 자른다."""
    pieces: list[str] = []
    buf = ""
    for para in text.split("\n"):
        if buf and len(buf) + len(para) + 1 > max_chars:
            pieces.append(buf)
            buf = ""
        while len(para) > max_chars:
            pieces.append(para[:max_chars])
            para = para[max_chars:]
        buf = f"{buf}\n{para}" if buf else para
    if buf.strip():
        pieces.append(buf)
    return pieces or [text[:max_chars]]


def build_chunks(day: str, max_chars: int) -> list[dict]:
    messages = [m for m in load_day(day) if _msg_text(m)]
    if not messages:
        return []

    chunks: list[dict] = []
    buf: list[dict] = []
    buf_chars = 0

    def flush() -> None:
        nonlocal buf, buf_chars
        if not buf:
            return
        markets = Counter(m.get("market") or "both" for m in buf)
        views = [v for v, _ in Counter(m.get("view") or "etc" for m in buf).most_common(3)]
        lines = [f"{m['date'][11:16]} {_msg_text(m)}" for m in buf]
        chunks.append({
            "id": f"{day}#{len(chunks)}",
            "date": day,
            "t0": buf[0]["date"][11:16],
            "t1": buf[-1]["date"][11:16],
            "market": markets.most_common(1)[0][0],
            "views": views,
            "view_labels": [VIEW_BY_ID[v]["label"] for v in views if v in VIEW_BY_ID],
            "msg_ids": [m["id"] for m in buf],
            "text": "\n".join(lines),
        })
        buf, buf_chars = [], 0

    prev_dt: datetime | None = None
    for msg in messages:
        dt = datetime.fromisoformat(msg["date"])
        body = _msg_text(msg)
        gap_break = prev_dt is not None and (dt - prev_dt) > timedelta(minutes=MAX_GAP_MINUTES)
        if buf and (gap_break or buf_chars + len(body) > max_chars):
            flush()

        # 메시지 하나가 이미 한도를 넘으면 (섹터별 종목 나열 같은 장문) 쪼갠다.
        # 한 청크가 비대해지면 검색 결과를 혼자 차지해 버린다.
        if len(body) > max_chars:
            for piece in _split_long(body, max_chars):
                buf = [dict(msg, text=piece, vision="")]
                buf_chars = len(piece)
                flush()
            prev_dt = dt
            continue

        buf.append(msg)
        buf_chars += len(body) + 8
        prev_dt = dt

    flush()
    return chunks


def _embed_text(chunk: dict) -> str:
    """임베딩에 넣을 텍스트. 날짜·관점 메타를 앞에 붙여 시점 질문에도 걸리게 한다."""
    head = f"{chunk['date']} {chunk['t0']}~{chunk['t1']} · {chunk['market']} · {'/'.join(chunk['view_labels'])}"
    return f"{head}\n{chunk['text']}"


def quantize(vectors: np.ndarray) -> tuple[str, int, int]:
    """정규화된 float 벡터 → int8. 번들 용량을 4분의 1로 줄인다."""
    q = np.clip(np.round(vectors * 127.0), -127, 127).astype(np.int8)
    return base64.b64encode(q.tobytes()).decode("ascii"), q.shape[0], q.shape[1]


def load_index() -> tuple[list[dict], np.ndarray | None]:
    if not CHUNKS_PATH.exists():
        return [], None
    with open(CHUNKS_PATH, encoding="utf-8") as f:
        chunks = json.load(f)
    vectors = np.load(VECTORS_PATH) if VECTORS_PATH.exists() else None
    return chunks, vectors


def run(force: bool = False, model: str | None = None) -> dict:
    cfg = load_config()
    ensure_dirs()
    require("GEMINI_API_KEY")
    model = model or cfg.get("models.embed", "gemini-embedding-001")
    dim = int(cfg.get("models.embed_dim", 256))
    max_chars = int(cfg.get("rag.chunk_max_chars", 900))
    index_days = int(cfg.get("rag.index_days", 120))

    cutoff = (datetime.now(KST) - timedelta(days=index_days)).strftime("%Y-%m-%d")
    day_list = [d for d in all_days() if d >= cutoff]

    old_chunks, old_vectors = load_index()
    cache: dict[str, np.ndarray] = {}
    if old_vectors is not None and len(old_chunks) == len(old_vectors) and not force:
        for chunk, vec in zip(old_chunks, old_vectors):
            cache[chunk["id"] + "|" + str(hash(chunk["text"]))] = vec

    chunks: list[dict] = []
    for day in day_list:
        chunks.extend(build_chunks(day, max_chars))

    if not chunks:
        print("[!] 인덱싱할 청크가 없습니다. 먼저 크롤을 실행하세요.")
        return {"chunks": 0, "embedded": 0}

    vectors = np.zeros((len(chunks), dim), dtype=np.float32)
    todo_idx: list[int] = []
    reused = 0
    for i, chunk in enumerate(chunks):
        key = chunk["id"] + "|" + str(hash(chunk["text"]))
        hit = cache.get(key)
        if hit is not None and len(hit) == dim:
            vectors[i] = hit
            reused += 1
        else:
            todo_idx.append(i)

    print(f"[인덱스] 청크 {len(chunks)}개 · 재사용 {reused} · 신규 임베딩 {len(todo_idx)}")

    quota_hit = False
    if todo_idx:
        step = 50
        for s in range(0, len(todo_idx), step):
            part = todo_idx[s : s + step]
            texts = [_embed_text(chunks[i]) for i in part]
            try:
                embs = embed_batch(model, texts, dim=dim, task_type="RETRIEVAL_DOCUMENT")
            except QuotaExceeded as e:
                print(f"  [쿼터] {e}")
                quota_hit = True
                break
            except Exception as e:
                print(f"  [!] 임베딩 실패: {e}")
                break
            for i, emb in zip(part, embs):
                vectors[i] = np.asarray(emb, dtype=np.float32)
            print(f"  {min(s + step, len(todo_idx))}/{len(todo_idx)}")

    # 임베딩이 비어 있는 청크(쿼터/실패)는 인덱스에서 제외
    keep = [i for i in range(len(chunks)) if np.any(vectors[i])]
    chunks = [chunks[i] for i in keep]
    vectors = vectors[keep]

    with open(CHUNKS_PATH, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False)
    np.save(VECTORS_PATH, vectors)

    print(f"[완료] 인덱스 {len(chunks)}청크 · {dim}차원")
    return {"chunks": len(chunks), "embedded": len(todo_idx), "quota_hit": quota_hit}


def main() -> None:
    p = argparse.ArgumentParser(description="RAG 인덱스 구축")
    p.add_argument("--force", action="store_true", help="캐시 무시하고 전부 재임베딩")
    p.add_argument("--model", default=None)
    args = p.parse_args()
    run(force=args.force, model=args.model)


if __name__ == "__main__":
    main()
