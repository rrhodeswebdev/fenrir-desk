import re

_MONTHS = {"H": "03", "M": "06", "U": "09", "Z": "12"}
_SERIES = {"ES": "ES", "MES": "ES", "NQ": "NQ", "MNQ": "NQ"}
_MMM = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def nt_instrument(symbol: str, front_code: str) -> str:
    """NinjaTrader instrument name for a symbol in a front-month contract:
    ("MNQ", "NQZ26") -> "MNQ 12-26". The code must be of the symbol's own series."""
    if symbol not in _SERIES:
        raise ValueError(f"unknown contract symbol {symbol!r}")
    m = re.fullmatch(rf"{_SERIES[symbol]}([HMUZ])([0-9]{{2}})", front_code)
    if m is None:
        raise ValueError(f"bad {_SERIES[symbol]} contract code {front_code!r}")
    return f"{symbol} {_MONTHS[m[1]]}-{m[2]}"


def canonical(name: str) -> str:
    """The contract's form of a name the add-on reports: some NinjaTrader installs give FullName
    as "MNQ DEC26" (#265), read as "MNQ 12-26". Any other name is returned as it is."""
    m = re.fullmatch(r"(\S+) ([A-Z]{3})([0-9]{2})", name)
    if m is None or m[2] not in _MMM:
        return name
    return f"{m[1]} {_MMM.index(m[2]) + 1:02d}-{m[3]}"
