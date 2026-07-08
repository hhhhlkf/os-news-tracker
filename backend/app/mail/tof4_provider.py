import hashlib
import random
import time
from typing import TypedDict

import httpx


class Tof4Config(TypedDict):
    paasid: str
    token: str
    url: str
    from_email: str


def _build_signature(*, timestamp: str, token: str, nonce: str) -> str:
    raw = f"{timestamp}{token}{nonce}{timestamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()


class Tof4MailProvider:
    def __init__(self, config: Tof4Config) -> None:
        self._config = config

    def _build_headers(self) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = str(random.randint(1000, 9999))
        return {
            "x-rio-paasid": self._config["paasid"],
            "x-rio-nonce": nonce,
            "x-rio-timestamp": timestamp,
            "x-rio-signature": _build_signature(timestamp=timestamp, token=self._config["token"], nonce=nonce),
        }

    def _build_form_fields(
        self,
        *,
        subject: str,
        html: str,
        recipients: list[str],
        from_email: str,
    ) -> list[tuple[str, tuple[None, str]]]:
        return [
            ("EmailType", (None, "1")),
            ("From", (None, from_email)),
            ("To", (None, ";".join(recipients))),
            ("CC", (None, "")),
            ("Bcc", (None, "")),
            ("Title", (None, subject)),
            ("Content", (None, html)),
            ("BodyFormat", (None, "1")),
        ]

    def _post(
        self,
        *,
        subject: str,
        html: str,
        recipients: list[str],
        from_email: str,
    ) -> None:
        if not recipients:
            raise ValueError("at least one recipient is required")

        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.post(
                self._config["url"],
                headers=self._build_headers(),
                files=self._build_form_fields(
                    subject=subject,
                    html=html,
                    recipients=recipients,
                    from_email=from_email,
                ),
            )
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                payload = response.json()
                errcode = payload.get("errcode", payload.get("ErrCode"))
                if errcode not in (None, 0, "0"):
                    errmsg = (
                        payload.get("errmsg")
                        or payload.get("ErrMsg")
                        or payload.get("msg")
                        or "unknown TOF4 error"
                    )
                    raise RuntimeError(f"tof4 send failed: {errmsg}")

    def test_connection(self) -> None:
        self._post(
            subject="OS News Tracker connection test",
            html="<p>connection test</p>",
            recipients=[self._config["from_email"]],
            from_email=self._config["from_email"],
        )

    def send(
        self,
        *,
        subject: str,
        html: str,
        recipients: list[str],
        from_email: str,
        from_name: str | None = None,
    ) -> None:
        del from_name
        self._post(
            subject=subject,
            html=html,
            recipients=recipients,
            from_email=from_email,
        )
