from assistant.core.plugin_manager import PluginManager
from assistant.core.search_service import SearchService
from tests.helpers import context_factory, make_plugin


class Recorder:
    def __init__(self, svc):
        self.started, self.finished, self.cleared = [], [], 0
        self.results = []   # (generation, plugin_id, [titles])
        svc.search_started.connect(self.started.append)
        svc.search_finished.connect(self.finished.append)
        svc.cleared.connect(self._cleared)
        svc.results_ready.connect(lambda g, p, r: self.results.append((g, p, [x.title for x in r])))

    def _cleared(self):
        self.cleared += 1


def build(tmp_path, specs):
    root = tmp_path / "plugins"
    root.mkdir()
    for pid, kwargs in specs.items():
        make_plugin(root, pid, **kwargs)
    mgr = PluginManager([root], context_factory(tmp_path))
    mgr.load_all()
    svc = SearchService(mgr, debounce_ms=10)
    return mgr, svc, Recorder(svc)


def test_global_search_fans_out_and_stamps_plugin_id(qapp, tmp_path, wait):
    mgr, svc, rec = build(tmp_path, {"a": {}, "b": {}})
    svc.query("hello")
    assert wait(lambda: rec.finished)
    assert {p for _, p, _ in rec.results} == {"a", "b"}
    assert sorted(t for _, _, ts in rec.results for t in ts) == ["a:hello", "b:hello"]
    svc.shutdown()


def test_keyword_routes_to_one_plugin(qapp, tmp_path, wait):
    mgr, svc, rec = build(tmp_path, {"a": {"keyword": "aa"}, "b": {}})
    svc.query("aa report")
    assert wait(lambda: rec.finished)
    assert rec.results == [(rec.started[0], "a", ["a:report"])]
    svc.shutdown()


def test_plugin_exception_does_not_break_others(qapp, tmp_path, wait):
    mgr, svc, rec = build(tmp_path, {"good": {}, "bad": {"boom": True}})
    svc.query("x")
    assert wait(lambda: rec.finished)       # finishes even though one plugin raised
    assert [p for _, p, _ in rec.results] == ["good"]
    svc.shutdown()


def test_stale_results_are_dropped(qapp, tmp_path, wait):
    mgr, svc, rec = build(tmp_path, {"slow": {"delay": 0.3}})
    svc.query("first")
    assert wait(lambda: rec.started)
    svc.query("second")
    assert wait(lambda: len(rec.started) == 2)
    assert wait(lambda: rec.finished)
    assert rec.finished == [rec.started[1]]
    assert [t for _, _, ts in rec.results for t in ts] == ["slow:second"]
    svc.shutdown()


def test_empty_query_clears(qapp, tmp_path, wait):
    mgr, svc, rec = build(tmp_path, {"a": {}})
    svc.query("   ")
    assert wait(lambda: rec.cleared == 1)
    assert not rec.started
    svc.shutdown()


def test_no_plugins_finishes_immediately(qapp, tmp_path, wait):
    mgr, svc, rec = build(tmp_path, {})
    svc.query("anything")
    assert wait(lambda: rec.finished)
    assert rec.results == []
    svc.shutdown()
