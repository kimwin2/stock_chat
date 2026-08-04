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

## 오늘 화면이 안 따라온다

시간별 워크플로(`hourly.yml`, KST 05~24시)가 도는지 본다.

- GitHub cron 은 best-effort 다. **예약의 절반 이상을 그냥 버리는 날도 있다**
  (2026-08-02~03 실측: 매시 1회 예약 중 5/15, 2/7 만 실행, 최대 2시간 공백).
  그래서 cron 을 30분 간격 2회/시간으로 걸어 실효 1회/시간을 노린다.
  누락이 더 심해지면 분(minute)을 바꾸거나 횟수를 늘린다 — 붐비는 :00, :05 는 피할 것
- 실행 기록 확인: Actions 탭에서 event=schedule 로 거르고 created_at 간격을 본다.
  전부 success 인데 간격이 벌어져 있으면 파이프라인 문제가 아니라 cron 누락이다
- `concurrency: pipeline` 을 `daily.yml` 과 공유하므로 자정 실행 중에는 한 번 걸러진다
- **60일간 레포에 커밋이 없으면 GitHub 이 예약 워크플로를 끈다.** 월요일 실행에
  `.github/heartbeat.txt` 를 커밋해 막고 있다. 그래도 꺼졌으면 Actions 탭에서 수동으로 다시 켠다
- 오늘 글이 5건 미만이면 요약은 건너뛰지만, 번들이 빈 껍데기 항목을 만들어
  화면에는 오늘 칩과 실시간 스트림이 그대로 뜬다 (`bundle.py` 의 `pending: True`)

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

로그에 `Gemini 일일 무료 쿼터를 다 썼습니다 (model=..., 한도=N회/일)` 가 뜨면 **정상 동작이다.**
파이프라인은 거기까지 저장하고 종료하며, 다음 실행이 남은 분량부터 이어서 한다.

**모델을 바꾸기 전에 반드시 실제 호출로 일일 한도를 확인할 것.** 문서에 적힌 값과 다르고,
모델 목록 API 에 나오는 모델이 실제로는 404 인 경우도 있다.

```bash
python - <<'EOF'
import os, requests
from pipeline.config import _load_env; _load_env()
key = os.environ["GEMINI_API_KEY"]
def probe(model, n=3):
    for i in range(n):
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json={"contents":[{"role":"user","parts":[{"text":"ok"}]}],
                  "generationConfig":{"maxOutputTokens":16}}, timeout=60)
        if r.status_code == 200: continue
        if r.status_code == 429:
            for d in r.json().get("error",{}).get("details",[]):
                for v in d.get("violations",[]) or []:
                    return f"{i}회 후 429 · {v.get('quotaId')} = {v.get('quotaValue')}"
        return f"{r.status_code}: {r.text[:100]}"
    return f"{n}/{n} 성공"
for m in ("gemini-3.5-flash","gemini-3.5-flash-lite","gemini-3.6-flash"):
    print(f"{m:26s} {probe(m)}")
EOF
```

2026-08 실측: `gemini-2.5-flash` = 404(신규 사용자 차단), `gemini-3.6-flash` = **20회/일**,
`gemini-3.5-flash` 와 `-flash-lite` 는 여유 있음.

- 급하면: `python -m pipeline.run --skip vision` — 이미지 없이 텍스트만으로 요약
- 모델별 분당 한도는 `pipeline/llm.py` 의 `DEFAULT_RPM` 에서 조정

### 이미지 판독이 안 돈다

2026-08-04 부터 켜져 있다 (`vision.enabled: true`). 안 돌면 순서대로 확인:

- **`vision.start_date` 이전 날짜는 의도적으로 건너뛴다.** 과거분 백필로 쿼터가
  터지는 것을 막는 장치다. 백필하려면 날짜를 당기고 `--limit 20` 으로 며칠에 나눈다
- 시간별 실행은 `--vision-limit 10` 상한이 걸려 있다. 사진이 몰린 시간대엔
  일부가 다음 실행으로 밀리는 게 정상이다
- 쿼터(flash-lite)에 걸리면 거기까지 저장하고 정상 종료한다. state.json 의
  vision 단계에서 quota_hit 를 확인

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

### 질의응답만 "검색 인덱스가 없습니다" 라고 나온다

요약은 보이는데 Q&A 만 안 되는 상태다. 임베딩이 일일 쿼터(1,000회/일)에 걸려
`search.enc` 가 번들에 안 들어간 것이다. 실제로 첫 Actions 실행에서 이 경로를 밟았다.

- 쿼터는 태평양시 자정(KST 16~17시)에 리셋된다. 그 뒤 워크플로를 한 번 돌리면 채워진다
- 매일 자정 자동 실행은 KST 00:10 = PDT 08:10 이라 리셋 이후이므로 정상적으로 채워진다
- 인덱스는 한 번 만들어지면 Actions 캐시에 남고, 이후에는 새로 생긴 청크만 임베딩한다
- 쿼터로 실패해도 기존 인덱스는 보존된다 (빈 인덱스로 덮어쓰지 않음)

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
