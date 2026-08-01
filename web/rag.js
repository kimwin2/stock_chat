// 브라우저에서 도는 RAG. 임베딩은 미리 계산돼 번들에 들어 있고,
// 질문 임베딩 1회 + 답변 생성 1회만 Gemini 를 호출한다.

import { loadPart } from './crypto.js';

const API = 'https://generativelanguage.googleapis.com/v1beta';

let _index = null;   // { chunks, vecs: Int8Array, rows, dim }

export async function ensureIndex() {
  if (_index) return _index;
  const search = await loadPart('search.enc');
  if (!search) return null;

  const { b64, rows, dim } = search.vectors;
  const bin = atob(b64);
  const vecs = new Int8Array(bin.length);
  for (let i = 0; i < bin.length; i++) vecs[i] = (bin.charCodeAt(i) << 24) >> 24;

  _index = { chunks: search.chunks, vecs, rows, dim };
  return _index;
}

export function indexSize() {
  return _index ? _index.chunks.length : 0;
}

async function embedQuery(query, settings, key) {
  const res = await fetch(`${API}/models/${settings.embed_model}:embedContent?key=${encodeURIComponent(key)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: `models/${settings.embed_model}`,
      content: { parts: [{ text: query }] },
      taskType: 'RETRIEVAL_QUERY',
      outputDimensionality: settings.embed_dim,
    }),
  });
  if (!res.ok) throw new Error(`임베딩 실패 (${res.status}): ${(await res.text()).slice(0, 200)}`);
  const data = await res.json();
  const vec = data.embedding?.values || [];
  const norm = Math.hypot(...vec) || 1;
  return vec.map((v) => v / norm);
}

const DATE_RE = /(\d{4})[-.\s/]?(\d{1,2})[-.\s/]?(\d{1,2})|(\d{1,2})월\s*(\d{1,2})일/g;

function datesIn(query, fallbackYear) {
  const out = new Set();
  let m;
  while ((m = DATE_RE.exec(query)) !== null) {
    if (m[1]) out.add(`${m[1]}-${String(m[2]).padStart(2, '0')}-${String(m[3]).padStart(2, '0')}`);
    else out.add(`${fallbackYear}-${String(m[4]).padStart(2, '0')}-${String(m[5]).padStart(2, '0')}`);
  }
  return out;
}

function lexicalBoost(query, text) {
  // 종목명·지표명처럼 그대로 등장하는 고유어를 잡기 위한 가벼운 보정.
  const terms = query.match(/[가-힣A-Za-z]{2,}/g) || [];
  if (!terms.length) return 0;
  let hits = 0;
  for (const t of new Set(terms)) if (text.includes(t)) hits++;
  return Math.min(0.12, (hits / new Set(terms).size) * 0.12);
}

export async function search(query, settings, key, topK) {
  const index = await ensureIndex();
  if (!index) throw new Error('검색 인덱스가 없습니다. 파이프라인에서 index 단계를 실행하세요.');

  const q = await embedQuery(query, settings, key);
  const { chunks, vecs, dim } = index;
  const wantDates = datesIn(query, (chunks.at(-1)?.date || '2026-01-01').slice(0, 4));

  const scored = new Array(chunks.length);
  for (let i = 0; i < chunks.length; i++) {
    let dot = 0;
    const off = i * dim;
    for (let d = 0; d < dim; d++) dot += q[d] * vecs[off + d];
    let score = dot / 127;
    score += lexicalBoost(query, chunks[i].text);
    if (wantDates.size && wantDates.has(chunks[i].date)) score += 0.35;
    scored[i] = { i, score };
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, topK).map(({ i, score }) => ({ ...chunks[i], score }));
}

function buildPrompt(query, hits, glossary, history) {
  const context = hits
    .map((h, n) => `[${n + 1}] ${h.date} ${h.t0}~${h.t1} (${h.market === 'us' ? '미장' : h.market === 'kr' ? '국장' : '공통'})\n${h.text}`)
    .join('\n\n---\n\n');

  const past = history.length
    ? `\n=== 앞선 대화 ===\n${history.map((t) => `${t.role === 'user' ? '질문' : '답변'}: ${t.text}`).join('\n')}\n`
    : '';

  return `당신은 한 주식 텔레그램 채널의 글만 근거로 답하는 도우미다.
채널 운영자는 펀드매니저 출신 개인 투자자다.

${glossary}

규칙:
- 아래 발췌문에 있는 내용만으로 답한다. 없으면 "그 내용은 수집된 글에 없습니다"라고 말한다.
- 숫자(현금비중 %, 종목 비중, 지수 등락률)는 원문 그대로 옮긴다.
- 문장 끝에 근거 번호를 [1] [3] 처럼 붙인다.
- 그의 판단과 사실을 구분한다. "그는 ~라고 본다" 형태로 쓴다.
- 투자 권유가 아니라 "그가 무엇을 말했는지"를 전달하는 것이 목적임을 잊지 마라.
- 답변은 한국어 평서문. 필요하면 불릿을 쓰되 5개를 넘기지 마라.
${past}
=== 채널 발췌문 ===
${context}

=== 질문 ===
${query}`;
}

export async function ask(query, { settings, key, glossary, history = [], onToken }) {
  const hits = await search(query, settings, key, settings.top_k || 24);
  const prompt = buildPrompt(query, hits, glossary, history.slice(-4));

  const url = `${API}/models/${settings.answer_model}:streamGenerateContent?alt=sse&key=${encodeURIComponent(key)}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      contents: [{ role: 'user', parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.3, maxOutputTokens: 2048 },
    }),
  });

  if (!res.ok) {
    const body = (await res.text()).slice(0, 300);
    if (res.status === 429) throw new Error('Gemini 무료 쿼터를 초과했습니다. 잠시 후 다시 시도하세요.');
    throw new Error(`답변 생성 실패 (${res.status}): ${body}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let answer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      try {
        const chunk = JSON.parse(payload);
        const text = chunk.candidates?.[0]?.content?.parts?.map((p) => p.text || '').join('') || '';
        if (text) {
          answer += text;
          onToken?.(text, answer);
        }
      } catch { /* 부분 청크는 무시 */ }
    }
  }

  return { answer, hits };
}
