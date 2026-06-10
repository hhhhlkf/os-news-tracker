from app.config import get_settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/db")
    monkeypatch.setenv("LLM_MODEL", "m1")
    get_settings.cache_clear()
    s = get_settings()
    assert s.database_url.endswith("/db")
    assert s.llm_model == "m1"
    assert s.llm_max_concurrency >= 1
