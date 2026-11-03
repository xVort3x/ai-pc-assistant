import json

from assistant.core.plugin_manager import PluginManager
from tests.helpers import context_factory, make_plugin


def manager(tmp_path, plugins_dir, disabled=()):
    return PluginManager([plugins_dir], context_factory(tmp_path), disabled)


def test_loads_valid_plugin(qapp, tmp_path):
    root = tmp_path / "plugins"
    make_plugin(root, "alpha", keyword="a")
    m = manager(tmp_path, root)
    m.load_all()
    assert [p.id for p in m.active_plugins()] == ["alpha"]
    assert m.get("alpha").keyword == "a"
    assert not m.errors


def test_missing_directory_is_fine(qapp, tmp_path):
    m = manager(tmp_path, tmp_path / "nope")
    m.load_all()
    assert m.active_plugins() == []


def test_broken_plugins_are_isolated(qapp, tmp_path):
    root = tmp_path / "plugins"
    make_plugin(root, "good")
    make_plugin(root, "syntax", source="def create_plugin(ctx) <<<")
    make_plugin(root, "nofactory", source="x = 1")
    make_plugin(root, "wrongtype", source="def create_plugin(ctx):\n    return 42")
    make_plugin(root, "future", api_version=99)
    make_plugin(root, "escape", manifest_extra={"entry": "../x.py"})
    bad_json = root / "badjson"
    bad_json.mkdir()
    (bad_json / "plugin.json").write_text("{nope", encoding="utf-8")
    m = manager(tmp_path, root)
    m.load_all()
    assert [p.id for p in m.active_plugins()] == ["good"]
    assert set(m.errors) == {"syntax", "nofactory", "wrongtype", "future", "escape", "badjson"}


def test_disabled_and_duplicate(qapp, tmp_path):
    root, other = tmp_path / "a", tmp_path / "b"
    make_plugin(root, "one")
    make_plugin(root, "off")
    make_plugin(other, "one")
    m = PluginManager([root, other], context_factory(tmp_path), disabled=["off"])
    m.load_all()
    assert [p.id for p in m.active_plugins()] == ["one"]
    assert "one" in m.errors          # second copy rejected


def test_reload_picks_up_new_plugin(qapp, tmp_path):
    root = tmp_path / "plugins"
    root.mkdir()
    m = manager(tmp_path, root)
    m.load_all()
    assert m.active_plugins() == []
    make_plugin(root, "late")
    m.reload()
    assert [p.id for p in m.active_plugins()] == ["late"]


def test_invalid_manifest_id(qapp, tmp_path):
    root = tmp_path / "plugins"
    make_plugin(root, "ok")
    folder = root / "weird"
    folder.mkdir()
    (folder / "plugin.json").write_text(json.dumps({"id": "bad id!"}), encoding="utf-8")
    m = manager(tmp_path, root)
    m.load_all()
    assert "weird" in m.errors
