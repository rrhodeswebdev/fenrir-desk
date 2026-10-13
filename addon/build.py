"""Build addon/dist/FenrirAddOn.zip for NinjaTrader 8's Tools > Import > NinjaScript Add-On.

Run from anywhere: python3 addon/build.py   (standard library only)

Layout copied from NinjaTrader's own export (unverified until imported on a clean NinjaTrader):
    AddOns/<file>.cs   every file in addon/Fenrir/
    Info.xml           the NinjaTrader version that "exported" it
No AdditionalReferences.txt: System.Web.Extensions, the one extra assembly the add-on uses, is
already in NinjaTrader's default NinjaScript references.
"""

import zipfile
from pathlib import Path

ADDON = Path(__file__).resolve().parent
OUT = ADDON / "dist" / "FenrirAddOn.zip"
# the NinjaTrader.Core.dll version the add-on is compile-checked against
NT_VERSION = "8.1.8.2"
INFO_XML = (
    '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
    "<NinjaTrader>\r\n"
    "  <Export>\r\n"
    f"    <Version>{NT_VERSION}</Version>\r\n"
    "  </Export>\r\n"
    "</NinjaTrader>"
)
STAMP = (2026, 1, 1, 0, 0, 0)  # fixed, so the same sources give the same zip


def add(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def main() -> None:
    sources = sorted((ADDON / "Fenrir").glob("*.cs"))
    if not sources:
        raise SystemExit("no .cs files in addon/Fenrir")
    OUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT, "w") as zf:
        for path in sources:
            add(zf, f"AddOns/{path.name}", path.read_bytes())
        add(zf, "Info.xml", INFO_XML.encode("utf-8"))
    print(f"wrote {OUT} ({len(sources)} files)")


if __name__ == "__main__":
    main()
