import pytest

from fenrir.bridge.names import nt_instrument


@pytest.mark.parametrize(
    ("symbol", "code", "name"),
    [
        ("MNQ", "NQZ26", "MNQ 12-26"),
        ("ES", "ESH27", "ES 03-27"),
        ("NQ", "NQM26", "NQ 06-26"),
        ("MES", "ESU30", "MES 09-30"),
    ],
)
def test_nt_instrument(symbol, code, name):
    assert nt_instrument(symbol, code) == name


@pytest.mark.parametrize("code", ["NQF26", "NQZ6", "NQZ2026", "nqz26", "ESZ26", "", "NQZ26 "])
def test_bad_code_raises(code):
    with pytest.raises(ValueError):
        nt_instrument("NQ", code)


@pytest.mark.parametrize("symbol", ["YM", "MYM", "nq", ""])
def test_unknown_symbol_raises(symbol):
    with pytest.raises(ValueError):
        nt_instrument(symbol, "NQZ26")
