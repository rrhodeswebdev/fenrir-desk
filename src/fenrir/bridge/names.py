import re

_MONTHS = {"H": "03", "M": "06", "U": "09", "Z": "12"}
_SERIES = {"ES": "ES", "MES": "ES", "NQ": "NQ", "MNQ": "NQ"}


def nt_instrument(symbol: str, front_code: str) -> str:
    """NinjaTrader instrument name for a symbol in a front-month contract:
    ("MNQ", "NQZ26") -> "MNQ 12-26". The code must be of the symbol's own series."""
    if symbol not in _SERIES:
        raise ValueError(f"unknown contract symbol {symbol!r}")
    m = re.fullmatch(rf"{_SERIES[symbol]}([HMUZ])([0-9]{{2}})", front_code)
    if m is None:
        raise ValueError(f"bad {_SERIES[symbol]} contract code {front_code!r}")
    return f"{symbol} {_MONTHS[m[1]]}-{m[2]}"
