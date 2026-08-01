import { loadManifest, unlock, loadPart, lock } from './crypto.js';
import { ask, ensureIndex } from './rag.js';

const $ = (id) => document.getElementById(id);
const PASS_KEY = 'sc.pass';
const THEME_KEY = 'sc.theme';

let core = null;          // 복호화된 core.enc
let days = [];            // 요약 목록 (최신이 뒤)
let current = null;       // 현재 보고 있는 요약
let market = 'kr';        // kr | us | common
const history = [];       // 대화 기록

// ── 공통 ───────────────────────────────────────────────
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, isError = false, ms = 4000) {
  document.querySelector('.toast')?.remove();
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' err' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  if (ms) setTimeout(() => el.remove(), ms);
  return el;
}

const STANCE = {
  '공격': { cls: 'good', glyph: '▲' },
  '중립': { cls: 'warning', glyph: '■' },
  '방어': { cls: 'critical', glyph: '▼' },
};
const stanceOf = (s) => STANCE[s] || { cls: '', glyph: '·' };

// ── 잠금 해제 ───────────────────────────────────────────
async function doUnlock(pass, remember) {
  const err = $('lock-err');
  err.textContent = '';
  const btn = $('unlock-btn');
  btn.disabled = true;
  btn.textContent = '여는 중...';
  try {
    core = await unlock(pass);
    if (remember) localStorage.setItem(PASS_KEY, pass);
    else localStorage.removeItem(PASS_KEY);
    start();
  } catch (e) {
    localStorage.removeItem(PASS_KEY);
    err.textContent = e.message === 'BAD_PASSPHRASE' ? '암호가 맞지 않습니다.' : e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '열기';
  }
}

function start() {
  $('lock').style.display = 'none';
  $('app').classList.add('ready');

  days = (core.days || []).slice().sort((a, b) => a.date.localeCompare(b.date));
  const updated = core.updated_at ? core.updated_at.slice(0, 16).replace('T', ' ') : '';
  $('updated').textContent = updated ? `업데이트 ${updated}` : '';

  const weeks = $('weeks');
  weeks.innerHTML = (core.settings.allowed_weeks || [1, 2, 3, 4])
    .map((w) => `<option value="${w}"${w === core.settings.default_weeks ? ' selected' : ''}>최근 ${w}주</option>`)
    .join('');

  // 토큰이 없어도 GitHub Actions 페이지로 보내주면 거기서 한 번 더 눌러 실행할 수 있다.
  // 버튼을 죽여두는 것보다 낫다.
  if (!core.secrets?.gh_token && core.secrets?.gh_repo) {
    $('refresh').title = '토큰이 없어 GitHub Actions 페이지로 이동합니다. 거기서 Run workflow 를 누르세요.';
  } else if (!core.secrets?.gh_repo) {
    $('refresh').disabled = true;
    $('refresh').title = 'GH_REPO 가 설정되지 않았습니다 (자정 자동 실행은 동작).';
  }

  renderDaystrip();
  select(days.at(-1)?.date);
  renderSuggestions();
}

// ── 날짜 스트립 ─────────────────────────────────────────
function renderDaystrip() {
  const strip = $('daystrip');
  if (!days.length) {
    strip.innerHTML = '<span class="muted">요약이 아직 없습니다. 상단 “크롤링 + 요약”을 눌러보세요.</span>';
    return;
  }
  strip.innerHTML = days.map((d) => {
    const s = stanceOf(d.stance);
    const [, mm, dd] = d.date.split('-');
    return `<button class="day-chip" data-date="${d.date}" aria-current="false" title="${esc(d.headline)}">
      <b>${mm}.${dd}</b><span class="wd">${esc(d.weekday || '')}</span>
      <span class="dot" style="background:var(--${s.cls || 'text-muted'})"></span>
    </button>`;
  }).join('');
  strip.querySelectorAll('.day-chip').forEach((el) =>
    el.addEventListener('click', () => select(el.dataset.date)));
}

function select(date) {
  current = days.find((d) => d.date === date) || days.at(-1) || null;
  document.querySelectorAll('.day-chip').forEach((el) =>
    el.setAttribute('aria-current', String(el.dataset.date === current?.date)));
  document.querySelector('.day-chip[aria-current="true"]')
    ?.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
  renderSummary();
}

// ── 스파크라인 (단일 계열, 범례 없음, 마지막 점만 직접 라벨) ──
function sparkline(points, label) {
  const vals = points.filter((p) => p.v !== null && p.v !== undefined);
  if (vals.length < 2) return '';

  const W = 260, H = 44, PAD_L = 4, PAD_R = 30, PAD_T = 8, PAD_B = 12;
  const lo = 0;
  const hi = Math.max(100, ...vals.map((p) => p.v));
  const x = (i) => PAD_L + (i / (points.length - 1)) * (W - PAD_L - PAD_R);
  const y = (v) => PAD_T + (1 - (v - lo) / (hi - lo)) * (H - PAD_T - PAD_B);

  const seg = [];
  let d = '';
  points.forEach((p, i) => {
    if (p.v === null || p.v === undefined) { d += ''; return; }
    d += (d ? ' L' : 'M') + `${x(i).toFixed(1)},${y(p.v).toFixed(1)}`;
    seg.push(i);
  });
  if (!d) return '';

  const first = seg[0], last = seg.at(-1);
  const area = `${d} L${x(last).toFixed(1)},${y(lo).toFixed(1)} L${x(first).toFixed(1)},${y(lo).toFixed(1)} Z`;
  const lastVal = points[last].v;

  const hits = points.map((p, i) => p.v === null || p.v === undefined ? '' :
    `<rect class="hit" x="${(x(i) - 6).toFixed(1)}" y="0" width="12" height="${H}"><title>${p.d} · ${label} 현금 ${p.v}%</title></rect>`).join('');

  return `<figure class="spark-wrap" style="margin:10px 0 0">
    <figcaption><span>현금비중 추이</span><span>${points.length}일</span></figcaption>
    <svg class="spark" viewBox="0 0 ${W} ${H}" role="img"
         aria-label="${label} 현금비중 추이, 최근 ${lastVal}퍼센트">
      <line class="grid" x1="${PAD_L}" y1="${y(lo).toFixed(1)}" x2="${(W - PAD_R).toFixed(1)}" y2="${y(lo).toFixed(1)}"/>
      <path class="area" d="${area}"/>
      <path class="line" d="${d}"/>
      <circle class="pt" cx="${x(last).toFixed(1)}" cy="${y(lastVal).toFixed(1)}" r="3.5"/>
      <text class="lbl" x="${(x(last) + 7).toFixed(1)}" y="${(y(lastVal) + 3.5).toFixed(1)}">${lastVal}%</text>
      ${hits}
    </svg>
  </figure>`;
}

function cashBlock(side, label) {
  const c = current.cash?.[side] || {};
  const end = c.end ?? c.start;
  const hasVal = end !== null && end !== undefined;
  const moved = c.start !== null && c.start !== undefined && c.end !== null && c.end !== undefined && c.start !== c.end;

  // 선택한 날짜에서 끝나야 카드에 적힌 값과 선 끝점이 일치한다.
  const upto = days.findIndex((d) => d.date === current.date);
  const recent = days.slice(Math.max(0, upto - 13), upto + 1).map((d) => ({
    d: d.date.slice(5),
    v: d.cash?.[side]?.end ?? d.cash?.[side]?.start ?? null,
  }));

  return `<div class="cash-item">
    <div class="cash-head">
      <span class="cash-label">${label} 현금비중</span>
      <span class="cash-value">${
        hasVal
          ? (moved ? `<span class="from">${c.start}%</span><span class="arrow">→</span>${end}%` : `${end}%`)
          : '<span class="from">언급 없음</span>'
      }</span>
    </div>
    <div class="cash-bar"><i style="width:${hasVal ? Math.min(100, Math.max(0, end)) : 0}%"></i></div>
    ${c.note ? `<div class="cash-note">${esc(c.note)}</div>` : ''}
    ${sparkline(recent, label)}
  </div>`;
}

// ── 요약 렌더 ───────────────────────────────────────────
function renderSummary() {
  const root = $('summary');
  if (!current) {
    root.innerHTML = '<div class="card"><div class="empty">표시할 요약이 없습니다.</div></div>';
    return;
  }
  const s = stanceOf(current.stance);
  const sections = current.markets || {};
  const counts = {
    kr: Object.keys(sections.kr || {}).length,
    us: Object.keys(sections.us || {}).length,
    common: Object.keys(sections.common || {}).length,
  };
  if (!counts[market]) market = ['kr', 'us', 'common'].find((m) => counts[m]) || 'kr';

  root.innerHTML = `
    <div class="card">
      <h2>${current.date} (${esc(current.weekday || '')}) · 메시지 ${current.message_count || 0}건${current.image_count ? ` · 이미지 ${current.image_count}장` : ''}</h2>
      <p class="headline">${esc(current.headline)}</p>
      <div class="meta-row">
        ${current.stance ? `<span class="badge ${s.cls}"><span class="glyph">${s.glyph}</span>${esc(current.stance)}</span>` : ''}
        ${current.stance_reason ? `<span class="muted">${esc(current.stance_reason)}</span>` : ''}
      </div>
      <div class="cash-grid">${cashBlock('kr', '국장')}${cashBlock('us', '미장')}</div>
      ${current.changes?.length
        ? `<ul class="changes">${current.changes.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
    </div>

    ${current.quotes?.length ? `<div class="card"><h2>그날의 발언</h2>
      ${current.quotes.map((q) => `<blockquote class="quote"><span class="t">${esc(q.time || '')}</span>${esc(q.text)}</blockquote>`).join('')}
    </div>` : ''}

    <div class="tabs" id="market-tabs">
      ${[['kr', '국장'], ['us', '미장'], ['common', '공통']].map(([k, l]) =>
        `<button data-m="${k}" aria-selected="${k === market}"${counts[k] ? '' : ' disabled'}>${l}<span class="n">${counts[k]}</span></button>`).join('')}
    </div>
    <div id="views"></div>

    ${(current.tickers?.length || current.sectors?.length) ? `<div class="card">
      <h2>언급된 종목 · 섹터</h2>
      <div class="chips">${[...(current.sectors || []), ...(current.tickers || [])]
        .map((t) => `<span class="chip">${esc(t)}</span>`).join('')}</div>
    </div>` : ''}
  `;

  root.querySelectorAll('#market-tabs button').forEach((b) =>
    b.addEventListener('click', () => { market = b.dataset.m; renderSummary(); }));
  renderViews();
}

function renderViews() {
  const order = Object.fromEntries((core.views || []).map((v) => [v.id, v.order ?? 99]));
  const section = (current.markets || {})[market] || {};
  const entries = Object.entries(section).sort((a, b) => (order[a[0]] ?? 99) - (order[b[0]] ?? 99));

  const box = $('views');
  if (!entries.length) {
    box.innerHTML = '<div class="card"><div class="empty">이 시장에 대한 내용이 없습니다.</div></div>';
    return;
  }

  box.innerHTML = entries.map(([id, v]) => `
    <article class="view-card">
      <div class="view-head"><span class="icon">${esc(v.icon || '·')}</span>${esc(v.label || id)}</div>
      <div class="view-body">
        ${v.summary ? `<p>${esc(v.summary)}</p>` : ''}
        ${v.bullets?.length ? `<ul>${v.bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
        ${v.refs?.length ? `<details class="refs" data-refs="${v.refs.join(',')}">
          <summary>원문 ${v.refs.length}건 보기</summary><div class="refs-body"></div>
        </details>` : ''}
      </div>
    </article>`).join('');

  box.querySelectorAll('details.refs').forEach((d) =>
    d.addEventListener('toggle', () => { if (d.open) fillRefs(d); }, { once: false }));
}

// ── 원문 펼치기 ─────────────────────────────────────────
async function fillRefs(details) {
  const body = details.querySelector('.refs-body');
  if (body.dataset.loaded) return;
  body.dataset.loaded = '1';
  body.innerHTML = '<div class="muted">불러오는 중<span class="dots"></span></div>';

  const ids = details.dataset.refs.split(',').map(Number);
  const day = current.date;
  const [pack, imgs] = await Promise.all([
    loadPart(`msgs-${day.slice(0, 7)}.enc`),
    loadPart(`img-${day}.enc`),
  ]);

  const list = pack?.days?.[day] || [];
  const byId = new Map(list.map((m) => [m.id, m]));
  const images = imgs?.images || {};

  const html = ids.map((id) => {
    const m = byId.get(id);
    if (!m) return '';
    const img = images[String(id)];
    return `<div class="msg">
      <span class="t">${esc(m.t)}</span><span class="body">${esc(m.text) || '<i>(사진)</i>'}</span>
      ${m.vision ? `<span class="vision">${esc(m.vision)}</span>` : ''}
      ${img ? `<img loading="lazy" alt="${esc(m.t)} 첨부 이미지" src="data:image/webp;base64,${img}">` : ''}
    </div>`;
  }).join('');

  body.innerHTML = html || '<div class="muted">해당 원문을 찾지 못했습니다.</div>';
}

// ── Q&A ────────────────────────────────────────────────
const SUGGESTIONS = [
  '지금 현금비중을 얼마로 보고 있어?',
  '최근에 주도업종을 뭐라고 봤어?',
  '연금·투신 수급이 들어온 섹터는?',
  '최근에 판 종목과 이유는?',
];

function renderSuggestions() {
  $('suggest').innerHTML = SUGGESTIONS.map((s) => `<button>${esc(s)}</button>`).join('');
  $('suggest').querySelectorAll('button').forEach((b) =>
    b.addEventListener('click', () => { $('q').value = b.textContent; send(); }));
}

function renderAnswer(text, hits) {
  // 아주 가벼운 서식 처리 — **굵게**, 줄머리 불릿, [n] 인용
  let html = esc(text)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/^[-*]\s+/gm, '· ');
  html = html.replace(/\[(\d{1,2})\]/g, (m, n) => {
    const i = Number(n) - 1;
    return hits?.[i] ? `<span class="cite" data-src="${i}" title="${esc(hits[i].date)} ${esc(hits[i].t0)}">[${n}]</span>` : m;
  });
  return html;
}

async function send() {
  const input = $('q');
  const query = input.value.trim();
  if (!query) return;
  input.value = '';
  input.style.height = 'auto';

  const log = $('chat-log');
  log.insertAdjacentHTML('beforeend', `<div class="bubble user">${esc(query)}</div>`);

  const bot = document.createElement('div');
  bot.className = 'bubble bot';
  bot.innerHTML = '<div class="text"><span class="dots"></span></div>';
  log.appendChild(bot);
  log.scrollTop = log.scrollHeight;

  $('send').disabled = true;
  try {
    const { answer, hits } = await ask(query, {
      settings: core.settings,
      key: core.secrets.gemini_key,
      glossary: core.glossary || '',
      history,
      onToken: (_t, full) => {
        bot.querySelector('.text').innerHTML = renderAnswer(full, null);
        log.scrollTop = log.scrollHeight;
      },
    });

    bot.querySelector('.text').innerHTML = renderAnswer(answer, hits);
    bot.insertAdjacentHTML('beforeend', `<details class="sources"><summary>근거 ${hits.length}건</summary>
      ${hits.map((h, i) => `<div class="src" id="src-${i}">
        <div class="h">[${i + 1}] ${esc(h.date)} ${esc(h.t0)}~${esc(h.t1)} · ${esc((h.view_labels || []).join(', '))}</div>
        <div class="b">${esc(h.text)}</div></div>`).join('')}
    </details>`);

    bot.querySelectorAll('.cite').forEach((c) => c.addEventListener('click', () => {
      const det = bot.querySelector('.sources');
      det.open = true;
      bot.querySelector(`#src-${c.dataset.src}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }));

    history.push({ role: 'user', text: query }, { role: 'bot', text: answer });
  } catch (e) {
    bot.classList.add('err');
    bot.querySelector('.text').textContent = e.message;
  } finally {
    $('send').disabled = false;
    log.scrollTop = log.scrollHeight;
  }
}

// ── 크롤링 + 요약 실행 ───────────────────────────────────
async function refresh() {
  const weeks = Number($('weeks').value);
  const { gh_repo: repo, gh_token: token } = core.secrets || {};
  if (!repo) return;

  if (!token) {
    // 토큰이 없으면 직접 실행은 못 하지만, 실행 페이지까지는 데려다줄 수 있다.
    window.open(`https://github.com/${repo}/actions/workflows/daily.yml`, '_blank', 'noopener');
    toast('GitHub 에서 “Run workflow” 를 누르면 실행됩니다. 기간은 거기서 고르세요.', false, 8000);
    return;
  }

  const btn = $('refresh');
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = '요청 중...';
  try {
    const res = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/daily.yml/dispatches`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      body: JSON.stringify({ ref: 'main', inputs: { weeks: String(weeks) } }),
    });
    if (res.status === 204) {
      toast(`최근 ${weeks}주 크롤링 + 요약을 시작했습니다. 5~15분 뒤 새로고침하세요.`, false, 9000);
    } else {
      toast(`실행 요청 실패 (${res.status}): ${(await res.text()).slice(0, 160)}`, true, 9000);
    }
  } catch (e) {
    toast(`실행 요청 실패: ${e.message}`, true, 9000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
}

// ── 부팅 ───────────────────────────────────────────────
function applyTheme(t) {
  if (t) document.documentElement.setAttribute('data-theme', t);
  else document.documentElement.removeAttribute('data-theme');
}

function bind() {
  $('unlock-btn').addEventListener('click', () => doUnlock($('pass').value, $('remember').checked));
  $('pass').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doUnlock($('pass').value, $('remember').checked);
  });

  $('refresh').addEventListener('click', refresh);
  $('send').addEventListener('click', send);
  $('q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $('q').addEventListener('input', (e) => {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(140, e.target.scrollHeight) + 'px';
  });

  $('logout').addEventListener('click', () => {
    localStorage.removeItem(PASS_KEY);
    lock();
    location.reload();
  });

  $('theme').addEventListener('click', () => {
    const now = document.documentElement.getAttribute('data-theme');
    const next = now === 'dark' ? 'light' : now === 'light' ? '' : 'dark';
    applyTheme(next);
    next ? localStorage.setItem(THEME_KEY, next) : localStorage.removeItem(THEME_KEY);
  });

  const setPane = (name) => {
    $('pane-summary').classList.toggle('active', name === 'summary');
    $('pane-chat').classList.toggle('active', name === 'chat');
    $('tab-summary').setAttribute('aria-selected', String(name === 'summary'));
    $('tab-chat').setAttribute('aria-selected', String(name === 'chat'));
    if (name === 'chat') ensureIndex().catch(() => {});
  };
  $('tab-summary').addEventListener('click', () => setPane('summary'));
  $('tab-chat').addEventListener('click', () => setPane('chat'));

  document.addEventListener('keydown', (e) => {
    if (!current || document.activeElement?.tagName === 'TEXTAREA' || document.activeElement?.tagName === 'INPUT') return;
    const i = days.findIndex((d) => d.date === current.date);
    if (e.key === 'ArrowLeft' && i > 0) select(days[i - 1].date);
    if (e.key === 'ArrowRight' && i < days.length - 1) select(days[i + 1].date);
  });
}

(async function boot() {
  applyTheme(localStorage.getItem(THEME_KEY) || '');
  bind();
  try {
    await loadManifest();
  } catch (e) {
    $('lock-err').textContent = e.message;
    return;
  }
  const saved = localStorage.getItem(PASS_KEY);
  if (saved) await doUnlock(saved, true);
})();
