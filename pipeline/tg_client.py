"""Telethon 클라이언트 + 채널 resolve.

공개 @username 과 비공개 invite 링크(https://t.me/+HASH) 를 모두 지원한다.
비공개 채널은 세션 계정이 이미 가입돼 있어야 하며, 아니면 자동 가입을 시도한다.
"""

from __future__ import annotations

import os
import re
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready

INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)")


def create_client(session: str | None = None) -> TelegramClient:
    api_id = (os.getenv("TG_API_ID") or "").strip()
    api_hash = (os.getenv("TG_API_HASH") or "").strip()
    session_str = session if session is not None else (os.getenv("TG_STRING_SESSION") or "").strip()

    if not api_id or not api_hash:
        raise RuntimeError("TG_API_ID / TG_API_HASH 가 .env 에 없습니다. https://my.telegram.org 에서 발급하세요.")

    return TelegramClient(StringSession(session_str), int(api_id), api_hash)


def parse_target(link: str) -> tuple[str, str]:
    """('invite', hash) 또는 ('username', name) 반환."""
    link = (link or "").strip()
    m = INVITE_RE.search(link)
    if m:
        return "invite", m.group(1)
    name = link.rsplit("/", 1)[-1].lstrip("@")
    if not name:
        raise ValueError(f"채널 식별자를 파싱하지 못했습니다: {link!r}")
    return "username", name


async def resolve_channel(client: TelegramClient, link: str) -> Any:
    kind, value = parse_target(link)

    if kind == "username":
        return await client.get_entity(value)

    try:
        result = await client(CheckChatInviteRequest(value))
    except (InviteHashExpiredError, InviteHashInvalidError) as e:
        raise RuntimeError(
            f"invite 링크가 만료됐거나 잘못됐습니다: {link}\n"
            f"운영자가 6개월마다 채널을 새로 열기 때문에 config.yaml 의 링크를 갱신해야 할 수 있습니다."
        ) from e

    if isinstance(result, ChatInviteAlready):
        return result.chat

    # 아직 미가입이면 가입 시도
    try:
        update = await client(ImportChatInviteRequest(value))
        return update.chats[0]
    except UserAlreadyParticipantError:
        return await client.get_entity(value)
