// 공유 암호 → 키 유도 → 번들 복호화.
// 파이프라인의 pipeline/bundle.py 와 짝을 이룬다.
//   파일 형식: iv(12바이트) || AES-GCM 암호문+태그
//   평문: gzip(JSON)

const DATA_BASE = 'data/';

let _key = null;      // CryptoKey
let _manifest = null;
const _cache = new Map();   // 파일명 → 복호화된 객체 (한 번만 받아온다)

export function isUnlocked() {
  return _key !== null;
}

export function manifest() {
  return _manifest;
}

export async function loadManifest() {
  if (_manifest) return _manifest;
  const res = await fetch(DATA_BASE + 'manifest.json', { cache: 'no-cache' });
  if (!res.ok) {
    throw new Error(`데이터가 아직 배포되지 않았습니다 (manifest.json ${res.status}).`);
  }
  _manifest = await res.json();
  return _manifest;
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function deriveKey(passphrase, kdf) {
  const base = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(passphrase), 'PBKDF2', false, ['deriveKey'],
  );
  return crypto.subtle.deriveKey(
    {
      name: 'PBKDF2',
      salt: b64ToBytes(kdf.salt),
      iterations: kdf.iterations,
      hash: kdf.hash,
    },
    base,
    { name: 'AES-GCM', length: 256 },
    false,
    ['decrypt'],
  );
}

async function gunzip(bytes) {
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('이 브라우저는 gzip 해제를 지원하지 않습니다. 최신 브라우저를 사용하세요.');
  }
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).text();
}

async function decryptFile(name) {
  const res = await fetch(DATA_BASE + name, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${name} 을(를) 불러오지 못했습니다 (${res.status}).`);
  const buf = new Uint8Array(await res.arrayBuffer());
  const iv = buf.slice(0, 12);
  const body = buf.slice(12);

  let plainBytes;
  try {
    plainBytes = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, _key, body);
  } catch {
    throw new Error('BAD_PASSPHRASE');
  }
  return JSON.parse(await gunzip(new Uint8Array(plainBytes)));
}

/** 암호를 검증하고 core.enc 를 복호화해서 반환. 틀리면 BAD_PASSPHRASE 를 던진다. */
export async function unlock(passphrase) {
  const mf = await loadManifest();
  _key = await deriveKey(passphrase, mf.kdf);
  try {
    const core = await decryptFile('core.enc');
    _cache.set('core.enc', core);
    return core;
  } catch (e) {
    _key = null;
    throw e;
  }
}

/** 지연 로딩. 없는 파일이면 null. */
export async function loadPart(name) {
  if (!_key) throw new Error('아직 잠금 해제되지 않았습니다.');
  if (_cache.has(name)) return _cache.get(name);
  if (_manifest && !_manifest.files.includes(name)) {
    _cache.set(name, null);
    return null;
  }
  try {
    const data = await decryptFile(name);
    _cache.set(name, data);
    return data;
  } catch (e) {
    if (e.message === 'BAD_PASSPHRASE') throw e;
    console.warn(name, e);
    _cache.set(name, null);
    return null;
  }
}

export function lock() {
  _key = null;
  _cache.clear();
}
