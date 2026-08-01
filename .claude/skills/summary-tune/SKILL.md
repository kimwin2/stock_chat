---
name: summary-tune
description: 요약이나 질의응답의 품질을 손본다. 관점(뷰) 추가·삭제·이름 변경, 새 지표 용어 등록, 요약이 뭉뚱그려지거나 숫자를 놓칠 때, 국장/미장 분류가 틀릴 때, RAG 검색이 엉뚱한 걸 물어올 때 사용. "요약이 별로", "관점 바꿔줘", "이 용어 모르네", "검색이 이상해" 같은 말이 나오면 이 스킬.
---

# 요약·질의응답 품질 튜닝

## 어디를 고쳐야 하는지 먼저 판단한다

| 증상 | 고칠 파일 |
|---|---|
| 관점을 추가/삭제/개명 | `pipeline/views.py` 의 `VIEWS` |
| 그가 쓰는 새 지표 용어를 모른다 | `pipeline/views.py` 의 `GLOSSARY` |
| 요약 문장이 뭉뚱그려진다 / 숫자를 놓친다 | `pipeline/summarize.py` 의 `SYSTEM` |
| 요약 JSON 필드를 바꾸고 싶다 | `pipeline/summarize.py` 의 `PROMPT_TMPL` + `_clean()` + `web/app.js` |
| 국장/미장 분류가 틀린다 | `pipeline/classify.py` 의 `PROMPT_TMPL` |
| 이미지 표를 대충 읽는다 | `pipeline/vision.py` 의 `SYSTEM` |
| 검색이 엉뚱한 걸 물어온다 | `pipeline/index.py` 청킹 · `web/rag.js` 검색 |
| 답변 말투·인용 형식 | `web/rag.js` 의 `buildPrompt` |

**`views.py` 가 중심이다.** 여기 정의가 분류·요약·질의응답 프롬프트에 전부 주입되므로
한 곳만 고치면 파이프라인 전체가 따라간다.

## 관점을 바꿨을 때 재생성 순서

```bash
python -m pipeline.classify --force     # 라벨 다시
python -m pipeline.summarize --force    # 요약 다시
python -m pipeline.bundle
```

`--force` 없이 돌리면 기존 결과를 건너뛰므로 아무것도 안 바뀐다.
비용이 걱정되면 `--days 3` 으로 최근 며칠만 먼저 돌려 결과를 보고 판단한다.

## 관점 하나 추가하는 법

`pipeline/views.py` 의 `VIEWS` 에 항목을 넣는다. `hint` 가 분류 정확도를 좌우하므로
**실제 메시지에 등장하는 표현을 그대로 인용**해서 쓴다.

```python
{
    "id": "risk",
    "order": 6.5,
    "label": "리스크 관리",
    "icon": "⚠",
    "primary": True,
    "desc": "MDD 단계별 대응과 손실 통제.",
    "hint": "'고점대비 -10% 이내에서만 리스크 관리 가능', '-15%쯤에서는 분할로 현금 소진' 같은 문장.",
},
```

그 다음 `summarize.py` 의 `KR_VIEWS` / `US_VIEWS` / `COMMON_VIEWS` 중 맞는 배열에 id 를 넣는다.
넣지 않으면 분류는 되지만 요약 카드에 안 나온다.

## 품질을 볼 때 보는 것

실제 데이터로 확인한다. 좋은 검증일: **2026-06-23** — 코스피가 10일선을 이탈하며
현금비중을 15% → 56% 로 올린 날이라 관점 8개가 전부 채워진다.

```bash
python -m pipeline.summarize --days 1 --force
python -c "
import json; d=json.load(open('data/daily/2026-06-23.json'))
print(d['headline']); print(d['stance'], d['cash'])
for m, sec in d['markets'].items():
    print(f'--- {m}')
    for k, v in sec.items(): print(' ', k, ':', v['summary'][:90])
"
```

체크리스트:

- [ ] `cash.kr.start/end` 에 실제 숫자가 들어갔는가 (06-23은 15 → 56)
- [ ] `headline` 이 40자 내외이고 그날의 핵심 변화를 짚는가
- [ ] `trades` 에 종목명 + 기존비중 + 변경비중 + 사유가 세트로 들어갔는가
- [ ] `signals` 가 "규칙을 말한 것"과 "오늘 발동된 것"을 구분하는가
- [ ] `refs` 의 id 가 실제 존재하는 메시지인가 (`_clean()` 이 걸러주지만 개수가 0이면 의심)
- [ ] 별칭이 풀렸는가 — 닉스→SK하이닉스, 닉전→SK하이닉스+삼성전자, 스퀘어→SK스퀘어

## 검색이 엉뚱할 때

1. **청킹을 먼저 본다.** 청크가 너무 크면 검색 결과를 혼자 차지한다.

```bash
python -c "
from pipeline.index import build_chunks
cs = build_chunks('2026-06-23', 900)
print(len(cs), 'chunks, max', max(len(c['text']) for c in cs))
for c in cs[:3]: print(c['t0'], '~', c['t1'], len(c['text']), c['view_labels'])
"
```

- 하루 15~25개, 각 900자 이하가 정상
- `config.yaml` 의 `rag.chunk_max_chars`, `pipeline/index.py` 의 `MAX_GAP_MINUTES` 로 조정

2. **날짜 질문이 안 먹으면** `web/rag.js` 의 `DATE_RE` 와 날짜 가산점(0.35)을 본다.
3. **종목명이 안 걸리면** `lexicalBoost` 의 상한(0.12)을 올린다.
4. 청킹을 바꿨으면 `python -m pipeline.index --force` 로 전부 재임베딩해야 한다.

## 하지 말 것

- 요약 프롬프트에 "추측해도 된다"는 여지를 주지 말 것. 이 채널은 숫자가 전부이고,
  지어낸 현금비중은 요약을 쓸모없게 만든다.
- 관점 id 를 바꾸면 **과거 요약 JSON 과 호환이 깨진다.** 이름만 바꾸고 싶으면
  `label` 만 바꾸고 `id` 는 유지할 것.
- `_clean()` 의 ref 검증을 빼지 말 것. 모델은 존재하지 않는 메시지 id 를 자주 지어낸다.
