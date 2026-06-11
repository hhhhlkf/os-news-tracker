from types import SimpleNamespace


def test_entry_exposes_app():
    from app.entry import app
    assert app is not None


def test_start_background_task_starts_daemon_thread(monkeypatch):
    from app.entry import _start_background_task

    captured = SimpleNamespace(started=False, daemon=None, name=None, target=None)

    class _FakeThread:
        def __init__(self, *, target, name, daemon):
            captured.target = target
            captured.name = name
            captured.daemon = daemon

        def start(self):
            captured.started = True

    monkeypatch.setattr("app.entry.threading.Thread", _FakeThread)

    def _task():
        return None

    _start_background_task(_task, name="startup-backfill")

    assert captured.target is _task
    assert captured.name == "startup-backfill"
    assert captured.daemon is True
    assert captured.started is True
