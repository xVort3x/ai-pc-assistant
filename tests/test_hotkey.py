import pytest

from assistant.system.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, parse_hotkey


def test_f9():
    assert parse_hotkey("F9") == (0, 0x78)


def test_function_key_range():
    assert parse_hotkey("f1") == (0, 0x70)
    assert parse_hotkey("F24") == (0, 0x87)


def test_modifiers_and_letter():
    assert parse_hotkey("Ctrl+Alt+K") == (MOD_CONTROL | MOD_ALT, ord("K"))
    assert parse_hotkey("shift+space") == (MOD_SHIFT, 0x20)


@pytest.mark.parametrize("bad", ["", "F25", "Hyper+F9", "Ctrl+", "Ctrl+Ü"])
def test_invalid(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)
