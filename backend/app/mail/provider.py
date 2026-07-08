from typing import Protocol


class MailProvider(Protocol):
    def send(
        self,
        *,
        subject: str,
        html: str,
        recipients: list[str],
        from_email: str,
        from_name: str | None = None,
    ) -> None:
        ...

    def test_connection(self) -> None:
        ...
