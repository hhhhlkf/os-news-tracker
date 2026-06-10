def test_entry_exposes_app():
    from app.entry import app
    assert app is not None
