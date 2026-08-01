"""최초 1회용 — 텔레그램 StringSession 발급.

    python -m pipeline.tg_auth

전화번호 → 인증코드 → (2FA 비밀번호) 순서로 물어보고,
발급된 문자열을 .env 의 TG_STRING_SESSION 에 넣으라고 안내한다.

이미 stock_test 프로젝트에서 발급받은 세션이 있으면 그걸 재사용해도 된다.
"""

from __future__ import annotations

import asyncio
import os

from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .config import ROOT, _load_env
from .tg_client import create_client


async def main() -> None:
    _load_env()
    existing = (os.getenv("TG_STRING_SESSION") or "").strip()
    if existing:
        client = create_client(existing)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"[OK] 기존 세션이 아직 유효합니다: {me.first_name} (@{me.username or '-'})")
                print("     새로 발급할 필요 없습니다.")
                return
            print("[!] 기존 TG_STRING_SESSION 이 만료됐습니다. 새로 발급합니다.\n")
        finally:
            await client.disconnect()

    client = create_client(session="")
    await client.connect()
    try:
        phone = (os.getenv("TG_PHONE_NUMBER") or "").strip()
        if not phone:
            phone = input("전화번호 (예: +821012345678): ").strip()

        await client.send_code_request(phone)
        code = input("텔레그램으로 받은 인증코드: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pw = input("2단계 인증 비밀번호: ").strip()
            await client.sign_in(password=pw)

        me = await client.get_me()
        session_str = StringSession.save(client.session)
        print(f"\n[OK] 로그인 성공: {me.first_name} (@{me.username or '-'})")
        print(f"\n아래 한 줄을 {ROOT / '.env'} 에 넣으세요:\n")
        print(f"TG_STRING_SESSION={session_str}\n")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
