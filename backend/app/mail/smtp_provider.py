from email.message import EmailMessage
import smtplib
from typing import TypedDict


class SMTPConfig(TypedDict):
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from_email: str
    smtp_from_name: str
    smtp_use_tls: bool
    smtp_use_ssl: bool


class SmtpMailProvider:
    def __init__(self, config: SMTPConfig) -> None:
        self._config = config

    def _build_client(self) -> smtplib.SMTP:
        host = self._config["smtp_host"]
        port = self._config["smtp_port"]
        if self._config["smtp_use_ssl"]:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
        if self._config["smtp_use_tls"] and not self._config["smtp_use_ssl"]:
            client.starttls()
        username = self._config["smtp_username"]
        password = self._config["smtp_password"]
        if username:
            client.login(username, password or "")
        return client

    def test_connection(self) -> None:
        client = self._build_client()
        try:
            client.noop()
        finally:
            client.quit()

    def send(
        self,
        *,
        subject: str,
        html: str,
        recipients: list[str],
        from_email: str,
        from_name: str | None = None,
    ) -> None:
        if not recipients:
            raise ValueError("at least one recipient is required")

        msg = EmailMessage()
        sender = f"{from_name} <{from_email}>" if from_name else from_email
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.set_content("This email contains HTML content. Please use an HTML-capable client.")
        msg.add_alternative(html, subtype="html")

        client = self._build_client()
        try:
            client.send_message(msg)
        finally:
            client.quit()
