# 채널 요약 + 질의응답

비공개 텔레그램 채널 **"주식 채널"** 을 매일 수집해서

- **관점별 일일 요약** — 국장 / 미장으로 나누고 그가 실제로 쓰는 사고 틀 8가지로 분해
- **RAG 질의응답** — 수집한 원문 전체에 대해 자연어로 질문

을 보여주는 정적 웹앱. 친한 몇 명만 **공유 암호**로 접근한다.

설계 배경과 관점 8종의 근거는 [DESIGN.md](DESIGN.md) 참고.

---

## 1. 처음 한 번만 하는 설정

### 1-1. 의존성

```bash
cd ~/repo/stock_chat
pip install -r requirements.txt
```

### 1-2. `.env` 채우기

```bash
cp .env.example .env
```

| 값 | 어디서 |
|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — **무료 등급**으로 발급 |
| `TG_API_ID` / `TG_API_HASH` | [my.telegram.org](https://my.telegram.org) |
| `TG_STRING_SESSION` | `python -m pipeline.tg_auth` 로 발급 (기존 stock_test 세션 재사용 가능) |
| `SHARE_PASSPHRASE` | 친구들에게 알려줄 암호. 직접 정한다 |
| `GH_DISPATCH_TOKEN` | (선택) UI의 "크롤링 + 요약" 버튼용. 아래 1-5 |

### 1-3. 채널 링크 넣기

운영자는 **6개월마다 채널을 새로 연다**. `config.yaml` 에서:

```yaml
telegram:
  channels:
    - id: ch_2026h2
      link: "https://t.me/+새로운초대링크"   # ← 여기
      active: true
```

### 1-4. GitHub 레포 + Pages

1. `kimwin2/stock_chat` 레포를 **public** 으로 생성 (데이터는 전부 암호화되므로 안전)
2. 코드 푸시
3. Settings → Pages → Source 를 **GitHub Actions** 로 변경
4. Settings → Secrets and variables → Actions 에 시크릿 등록:
   `GEMINI_API_KEY`, `TG_API_ID`, `TG_API_HASH`, `TG_STRING_SESSION`, `SHARE_PASSPHRASE`, (선택) `GH_DISPATCH_TOKEN`

### 1-5. (선택) UI 버튼 활성화

UI의 "크롤링 + 요약" 버튼은 GitHub Actions 를 원격 실행한다. 필요한 토큰:

- [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens) → Fine-grained token
- Repository access: `kimwin2/stock_chat` **하나만**
- Permissions: **Actions → Read and write** 하나만
- 발급된 토큰을 `.env` 의 `GH_DISPATCH_TOKEN` 과 레포 시크릿 양쪽에 넣는다

토큰이 없으면 버튼만 비활성화되고 매일 자정 자동 실행은 그대로 동작한다.

---

## 2. 평소 운영

**아무것도 안 해도 된다.** GitHub Actions 가 매일 **00:10 KST** 에 돌면서
크롤 → 이미지 판독 → 분류 → 요약 → 임베딩 → 배포까지 마친다.

즉시 갱신하고 싶으면 웹 화면 상단에서 기간(1~4주)을 고르고 **"크롤링 + 요약"** 을 누른다.
5~15분 뒤 새로고침하면 반영된다.

### 로컬에서 직접 돌리기

```bash
python -m pipeline.run                # 최근 1주 전체 파이프라인
python -m pipeline.run --weeks 4      # 최근 4주
python -m pipeline.run --only summarize --force-summary
python -m pipeline.run --vision-limit 40   # 무료 쿼터 아끼기

# 단계별
python -m pipeline.crawl --weeks 2
python -m pipeline.vision --days 3
python -m pipeline.classify
python -m pipeline.summarize
python -m pipeline.index
python -m pipeline.bundle
```

### 로컬에서 화면 확인

```bash
python -m pipeline.bundle
cd web && python -m http.server 8811 --bind 127.0.0.1
# http://127.0.0.1:8811 → SHARE_PASSPHRASE 입력
```

---

## 3. 구조

```
config.yaml            채널 링크, 모델, 기간 설정 (비밀값 없음)
.env                   비밀값 (커밋 안 됨)

pipeline/
  views.py             ★ 관점 8종 정의 + 그의 고유 용어 사전. 요약 품질의 핵심
  config.py            설정 로딩
  tg_client.py         Telethon 클라이언트, invite 링크 resolve
  tg_auth.py           최초 1회 세션 발급
  crawl.py             기간 지정 크롤 + 이미지 다운로드/리사이즈
  llm.py               Gemini REST 래퍼 (속도 제한, 백오프, 쿼터 감지)
  vision.py            이미지 → 텍스트 (앨범 단위 배치)
  classify.py          메시지 → (시장, 관점) 라벨
  summarize.py         하루치 → 구조화 요약 JSON
  index.py             시간 블록 청킹 + 임베딩 (256차원 int8)
  bundle.py            AES-256-GCM 암호화 배포 번들
  run.py               전체 오케스트레이션

web/                   정적 프론트 (빌드 도구 없음, vanilla JS)
  index.html  style.css  app.js  crypto.js  rag.js

tools/
  import_legacy.py     예전 채널 덤프 가져오기
  mock_summaries.py    키 없이 UI 확인용 목업 요약

data/                  로컬 산출물 (전부 .gitignore)
  raw/YYYY-MM-DD.json  원문 + 이미지 판독 + 라벨
  media/YYYY-MM-DD/    리사이즈된 원본 이미지 (28일 보관)
  daily/YYYY-MM-DD.json 요약
  index/               청크 + 임베딩
```

---

## 4. 자주 하는 일

### 채널이 새로 열렸을 때 (6개월마다)

`config.yaml` 의 `telegram.channels` 에서 기존 항목을 `active: false` 로 바꾸고
새 항목을 추가한다. 기존 항목은 **지우지 말 것** — 이미 수집한 데이터의 출처 표기에 쓰인다.

```yaml
    - id: ch_2026h2
      label: "... (26년 하반기)"
      link: "CLOSED"
      active: false
    - id: ch_2027h1
      label: "... (27년 상반기)"
      link: "https://t.me/+새링크"
      active: true
```

### 관점을 바꾸고 싶을 때

`pipeline/views.py` 의 `VIEWS` 를 수정하고 요약을 재생성한다:

```bash
python -m pipeline.classify --force
python -m pipeline.summarize --force
python -m pipeline.bundle
```

`GLOSSARY` (그의 고유 용어 사전)도 같은 파일에 있다. 새 지표 용어가 생기면 여기 추가하면
비전·분류·요약·질의응답 프롬프트에 한꺼번에 반영된다.

### 공유 암호를 바꿀 때

`.env` 와 레포 시크릿의 `SHARE_PASSPHRASE` 를 바꾸고 워크플로를 한 번 돌린다.
번들이 새 암호로 다시 암호화된다. 친구들은 새 암호를 입력해야 한다.

---

## 5. 알아둘 것

- **무료 Gemini 쿼터.** 매일 증분(하루 200건, 사진 130장)은 무료 등급으로 충분하다.
  다만 **처음 4주치를 한 번에 받으면 일일 한도에 걸린다.** 걸리면 그 지점까지 저장하고
  정상 종료하며, 다음 실행이 남은 분량부터 이어서 처리한다. 며칠에 나눠 채워진다.
- **텔레그램 세션.** GitHub Actions 는 데이터센터 IP 에서 접속한다. 기존 세션을 재사용하는
  것이라 새 로그인은 아니지만, 텔레그램이 세션을 끊으면 `python -m pipeline.tg_auth` 로
  재발급하고 레포 시크릿을 갱신해야 한다.
- **레포는 public 이지만 평문 콘텐츠가 0.** `web/data/` 는 커밋하지 않고 Actions 가
  매번 새로 만들어 Pages 아티팩트로만 올린다. 검색엔진에 채널 내용이 노출되지 않는다.
- **이미지 원본**은 28일만 보관하고 자동 삭제된다. 판독한 텍스트는 영구 보관되므로
  요약과 질의응답에서는 계속 쓸 수 있다.
- 이건 **투자 권유가 아니다.** 한 사람이 채널에 쓴 글을 정리해 보여줄 뿐이고,
  요약은 LLM 이 만든 것이라 틀릴 수 있다. 판단이 중요하면 항상 원문을 확인할 것.
