from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from assistant.core.plugin_api import SearchResult
from assistant.ui.main_window import SearchWindow


def test_results_sorted_capped_and_activation(qapp):
    w = SearchWindow()
    w.max_results = 3
    activated = []
    w.result_activated.connect(activated.append)
    w.begin_search(1)
    w.add_results(1, "p", [SearchResult(title=f"r{i}", score=i, plugin_id="p") for i in range(6)])
    assert [r.title for r in w._results] == ["r5", "r4", "r3"]
    assert w.list.count() == 3 and not w.list.isHidden()

    w.add_results(0, "p", [SearchResult(title="stale", score=99)])   # old generation ignored
    assert w._results[0].title == "r5"

    QTest.keyClick(w.input, Qt.Key.Key_Down)
    assert w.list.currentRow() == 1
    QTest.keyClick(w.input, Qt.Key.Key_Up)
    QTest.keyClick(w.input, Qt.Key.Key_Up)
    assert w.list.currentRow() == 2          # wraps around
    QTest.keyClick(w.input, Qt.Key.Key_Return)
    assert [r.title for r in activated] == ["r3"]


def test_clear_hides_list_and_shows_hint(qapp):
    w = SearchWindow()
    w.set_idle_hint("hint")
    w.begin_search(1)
    w.add_results(1, "p", [SearchResult(title="x")])
    w.clear_results()
    assert w.list.isHidden()
    assert w.status.text() == "hint"
