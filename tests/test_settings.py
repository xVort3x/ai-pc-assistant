from assistant.config import Settings


def test_defaults_and_persistence(tmp_path):
    path = tmp_path / "config.json"
    s = Settings(path)
    assert s.get("hotkey") == "F9"
    s.set("hotkey", "F8")
    assert Settings(path).get("hotkey") == "F8"


def test_corrupt_file_recovers(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    s = Settings(path)
    assert s.get("hotkey") == "F9"
    assert (tmp_path / "config.json.bad").exists()


def test_plugin_scoped_settings(tmp_path):
    s = Settings(tmp_path / "config.json")
    a, b = s.plugin("a"), s.plugin("b")
    a.set("x", 1)
    assert a.get("x") == 1
    assert b.get("x") is None
    assert Settings(tmp_path / "config.json").plugin("a").get("x") == 1
