---
name: pipeline-ops
description: 대상 채널 파이프라인을 돌리고 고친다. 크롤이 0건일 때, 요약이 안 생길 때, 이미지 판독이 비었을 때, Gemini 쿼터에 걸렸을 때, 텔레그램 세션이 끊겼을 때, 채널이 새로 열렸을 때, 배포가 안 될 때 사용. "크롤 안 돼", "요약이 이상해", "쿼터", "세션 만료", "채널 바뀜", "Actions 실패" 같은 말이 나오면 이 스킬.
---

# 파이프라인 운영

## 먼저 상태를 본다 — 추측 금지

```bash
cd ~/repo/stock_chat
python - <<'EOF'
import json, glob, os
from collections import Counter
from pipeline.crawl import all_days, load_day
days = all_days()
print(f"수집일수 {len(days)}  ({days[0] if days else '-'} ~ {days[-1] if days else '-'})")
tot = Counter()
for d in days[-7:]:
    ms = load_day(d)
    photo   = sum(1 for m in ms if m.get('media') == 'photo')
    havefile= sum(1 for m in ms if m.get('image'))
    vision  = sum(1 for m in ms if (m.get('vision') or '').strip())
    labeled = sum(1 for m in ms if m.get('view'))
    summ    = os.path.exists(f'data/daily/{d}.json')
    print(f"  {d}  메시지{len(ms):4d}  사진{photo:4d}  파일{havefile:4d}  판독{vision:4d}  분류{labeled:4d}  요약{'O' if summ else 'X'}")
print("요약 파일:", len(glob.glob('data/daily/*.json')))
print("인덱스:", os.path.exists('data/index/chunks.json'))
if os.path.exists('data/state.json'):
    print("마지막 실행:", json.dumps(json.load(open('data/state.json')), ensure_ascii=False)[:600])
EOF
```

이 표에서 **어느 열이 0인지**가 곧 어느 단계가 막혔는지다.

| 증상 | 원인과 조치 |
|---|---|
| 수집일수 0 | `config.yaml` 의 `active: true` 채널 `link` 가 비었거나 만료 |
| 사진은 있는데 파일 0 | `--no-media` 로 돌았거나 `crawl.download_media: false` |
| 파일은 있는데 판독 0 | `GEMINI_API_KEY` 없음, 또는 쿼터 소진 |
| 분류 0 | classify 단계가 안 돌았음 |
| 요약 X | 그날 유효 메시지가 5건 미만이거나 summarize 실패 |

## 단계별 재실행

각 단계는 이어달리기다. 이미 처리한 것은 건너뛰므로 그냥 다시 돌리면 된다.

```bash
python -m pipeline.crawl --weeks 1
python -m pipeline.vision --days 3 --limit 40   # limit 로 쿼터 아끼기
python -m pipeline.classify
python -m pipeline.summarize
python -m pipeline.index
python -m pipeline.bundle
```

강제로 다시 만들려면 `--force` (classify/summarize/index) 를 붙인다.

## 자주 나오는 문제

### Gemini 일일 쿼터 소진

로그에 `Gemini 일일 무료 쿼터를 다 썼습니다` 가 뜨면 **정상 동작이다.**
파이프라인은 거기까지 저장하고 종료하며, 다음 실행이 남은 분량부터 이어서 한다.

- 급하면: `python -m pipeline.run --skip vision` — 이미지 없이 텍스트만으로 요약
- 초기 4주 백필은 며칠에 나눠 채워지는 게 정상이다
- 모델별 분당 한도는 `pipeline/llm.py` 의 `DEFAULT_RPM` 에서 조정

### 텔레그램 세션 만료

`텔레그램 세션이 만료됐습니다` → 재발급 후 **.env 와 레포 시크릿 양쪽** 갱신:

```bash
python -m pipeline.tg_auth
```

### invite 링크 만료 / 채널 교체

운영자는 **6개월마다 채널을 새로 연다** (직전 채널은 2026-06-26 종료).
`config.yaml` 에서 기존 항목을 `active: false` + `link: "CLOSED"` 로 바꾸고 새 항목 추가.
**기존 항목을 지우지 말 것** — 수집한 데이터의 출처 표기에 쓰인다.

### 크롤은 되는데 메시지가 적다

`crawl.max_messages` 상한(기본 4000)에 걸렸는지 본다. 하루 200건 × 28일 = 5600건이므로
4주를 한 번에 받으려면 상한을 올려야 한다.

### GitHub Actions 실패

- Actions 탭 → 실행 → "실행 요약" 스텝에 `data/state.json` 이 찍힌다. 어느 단계에서 죽었는지 여기서 본다
- 시크릿 6개가 다 등록됐는지 확인: `GEMINI_API_KEY` `TG_API_ID` `TG_API_HASH` `TG_STRING_SESSION` `SHARE_PASSPHRASE` `GH_DISPATCH_TOKEN`(선택)
- Pages 가 안 뜨면 Settings → Pages → Source 가 **GitHub Actions** 인지 확인
- 캐시(`pipeline-data-`)가 날아가면 재수집할 뿐 깨지지 않는다. 대신 비전 판독을 다시 하므로 쿼터를 먹는다

### 화면이 안 열린다 / 암호가 틀리다고 나온다

번들을 만들 때 쓴 `SHARE_PASSPHRASE` 와 입력한 암호가 다른 것이다.
암호를 바꿨으면 워크플로를 한 번 돌려 번들을 다시 만들어야 한다.

## 키 없이 UI 만 확인하기

```bash
python -m tools.mock_summaries          # 실데이터에서 목업 요약 생성
SHARE_PASSPHRASE=test GEMINI_API_KEY=x python -m pipeline.bundle
cd web && python -m http.server 8811 --bind 127.0.0.1
```

**확인이 끝나면 `data/daily/` 를 반드시 비울 것.** 목업이 남아 있으면 진짜 요약이
생성되지 않는다 (summarize 는 이미 있는 날짜를 건너뛴다).

## 브라우저 검증

Playwright + Chromium 이 설치돼 있다. UI 를 고쳤으면 눈으로 확인한다 —
특히 **모바일 390px 에서 가로 스크롤이 생기지 않는지**:

```python
pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
```
