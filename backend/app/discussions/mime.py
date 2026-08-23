"""Safe, bounded MIME normalization for public mailing-list messages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape


PARSER_VERSION = "discussion-mime-v1"
MAX_BODY_BYTES = 1_500_000
MAX_TEXT_ATTACHMENT_BYTES = 300_000
_MESSAGE_ID = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class ParsedDiscussionMessage:
    message_id: str
    in_reply_to: str | None
    references: list[str]
    subject: str
    author_name: str | None
    author_email: str | None
    sent_at: datetime | None
    body_text: str
    authored_text: str
    inline_text: str
    attachments: list[dict]
    headers: dict[str, str]
    content_hash: str
    parse_status: str


def normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _MESSAGE_ID.search(str(value))
    if match:
        return match.group(0).lower()
    compact = str(value).strip().strip("<>").lower()
    return f"<{compact}>" if compact and "@" in compact else None


def message_id_references(value: str | None) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(match.group(0).lower() for match in _MESSAGE_ID.finditer(value)))


def decoded_header(message: Message, name: str) -> str | None:
    value = message.get(name)
    if not value:
        return None
    try:
        return str(make_header(decode_header(value))).strip() or None
    except Exception:
        return str(value).strip() or None


def normalize_email(value: str | None) -> str | None:
    _, address = parseaddr(value or "")
    return address.lower().strip() or None


def parse_message(message: Message) -> ParsedDiscussionMessage:
    message_id = normalize_message_id(decoded_header(message, "Message-ID"))
    if not message_id:
        raise ValueError("mailing-list message has no valid Message-ID")
    sender = decoded_header(message, "From")
    author_name, _ = parseaddr(sender or "")
    body_text, inline_text, attachments, over_limit = _extract_content(message)
    authored = _extract_authored_text(body_text)
    headers = {
        key: value
        for key in ("List-Id", "List-Post", "Delivered-To", "To", "Cc", "From", "Subject")
        if (value := decoded_header(message, key))
    }
    status = "partial" if over_limit else "parsed"
    return ParsedDiscussionMessage(
        message_id=message_id,
        in_reply_to=normalize_message_id(decoded_header(message, "In-Reply-To")),
        references=message_id_references(decoded_header(message, "References")),
        subject=decoded_header(message, "Subject") or "(无主题邮件)",
        author_name=author_name.strip() or None,
        author_email=normalize_email(sender),
        sent_at=_parse_date(decoded_header(message, "Date")),
        body_text=body_text,
        authored_text=authored,
        inline_text=inline_text,
        attachments=attachments,
        headers=headers,
        content_hash=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        parse_status=status,
    )


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_content(message: Message) -> tuple[str, str, list[dict], bool]:
    plain: list[str] = []
    html: list[str] = []
    inline: list[str] = []
    attachments: list[dict] = []
    total = 0
    over_limit = False
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        raw = part.get_payload(decode=True) or b""
        filename = decoded_header(part, "Content-Disposition") or part.get_filename()
        content_type = part.get_content_type().lower()
        is_attachment = part.get_content_disposition() == "attachment" or bool(part.get_filename())
        if is_attachment:
            attachments.append({
                "name": str(part.get_filename() or "attachment")[:500],
                "content_type": content_type,
                "size": len(raw),
                "stored": content_type in {"text/plain", "text/x-patch", "text/x-diff"} and len(raw) <= MAX_TEXT_ATTACHMENT_BYTES,
            })
            if content_type in {"text/plain", "text/x-patch", "text/x-diff"} and len(raw) <= MAX_TEXT_ATTACHMENT_BYTES:
                decoded = _decode(raw, part.get_content_charset())
                inline.append(decoded)
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        remaining = MAX_BODY_BYTES - total
        if remaining <= 0:
            over_limit = True
            continue
        if len(raw) > remaining:
            raw = raw[:remaining]
            over_limit = True
        total += len(raw)
        decoded = _decode(raw, part.get_content_charset())
        (plain if content_type == "text/plain" else html).append(decoded)
    content = "\n\n".join(plain) if plain else _html_to_text("\n\n".join(html))
    return content.strip(), "\n\n".join(inline).strip(), attachments, over_limit


def _decode(raw: bytes, charset: str | None) -> str:
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    return re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", value))).strip()


def _extract_authored_text(body: str) -> str:
    """Remove only conventional quote blocks; uncertain content remains intact."""
    lines = body.splitlines()
    kept: list[str] = []
    for line in lines:
        if line.lstrip().startswith(">"):
            continue
        if re.match(r"^On .+wrote:$", line.strip()):
            break
        kept.append(line)
    result = "\n".join(kept).strip()
    return result or body.strip()
