"""Read-only IMAP adapter used by the discussion mailbox connection."""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from email.message import Message


@dataclass(frozen=True)
class ImapMessage:
    uid: int
    headers: Message
    full_message: Message | None = None


class ReadOnlyImapMailbox:
    def __init__(self, *, host: str, port: int, username: str, password: str, folder: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._folder = folder
        self._client: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "ReadOnlyImapMailbox":
        client = imaplib.IMAP4_SSL(self._host, self._port)
        client.login(self._username, self._password.replace(" ", ""))
        status, data = client.select(self._folder, readonly=True)
        if status != "OK":
            client.logout()
            raise RuntimeError(f"cannot open IMAP folder {self._folder!r}")
        self._client = client
        self.uidvalidity = _uidvalidity(data)
        return self

    def __exit__(self, *_: object) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def new_uids(self, after_uid: int) -> list[int]:
        client = self._require_client()
        status, data = client.uid("search", None, f"UID {max(1, after_uid + 1)}:*")
        if status != "OK":
            raise RuntimeError("cannot search IMAP UIDs")
        return [int(raw) for raw in (data[0] or b"").split()]

    def fetch_headers(self, uid: int) -> Message:
        return self._fetch(uid, "(BODY.PEEK[HEADER])")

    def fetch_full(self, uid: int) -> Message:
        return self._fetch(uid, "(BODY.PEEK[])")

    def _fetch(self, uid: int, query: str) -> Message:
        client = self._require_client()
        status, data = client.uid("fetch", str(uid), query)
        if status != "OK" or not data or not isinstance(data[0], tuple):
            raise RuntimeError(f"cannot fetch IMAP UID {uid}")
        return email.message_from_bytes(data[0][1])

    def _require_client(self) -> imaplib.IMAP4_SSL:
        if self._client is None:
            raise RuntimeError("IMAP mailbox is not connected")
        return self._client


def _uidvalidity(select_data: list[bytes] | None) -> str | None:
    for raw in select_data or []:
        match = re.search(rb"UIDVALIDITY\s+(\d+)", raw)
        if match:
            return match.group(1).decode("ascii")
    return None
