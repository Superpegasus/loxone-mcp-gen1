"""Loxone-Symbolbibliothek (IconLibrary) und Farben für Status-Bausteine.

Die Symbole stehen in der Loxone-Config-Installation unter
C:\\ProgramData\\Loxone\\Loxone Config <Version>\\IconLibrary.zip (IconLibrary_DEU.xml):
<Icon uuid="00000235-00FF-..." Id="sun.svg" Tags="klima,heiß,sonne,..."/>.
Im Projekt werden sie klein geschrieben verwendet, z. B. Icon="00000235-00ff-0000-0000000000000000".
"""
from __future__ import annotations

import re
import zipfile
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

from loxproject import ProjectError

NONE_ICON = "00000000-0000-0000-0000000000000000"


def _library_zips() -> list[Path]:
    import os
    direct = os.environ.get("LOXONE_ICON_LIBRARY")  # Pfad zu einer IconLibrary.zip
    if direct and Path(direct).is_file():
        return [Path(direct)]
    root = Path(os.environ.get("LOXONE_CONFIG_DATA", r"C:\ProgramData\Loxone"))
    if not root.is_dir():
        return []
    zips = [p / "IconLibrary.zip" for p in root.glob("Loxone Config *") if (p / "IconLibrary.zip").is_file()]

    def ver(p: Path):
        return tuple(int(x) for x in re.findall(r"\d+", p.parent.name))
    return sorted(zips, key=ver, reverse=True)


@lru_cache(maxsize=1)
def library() -> list[dict]:
    """Alle Symbole der neuesten installierten Config: uuid, name (ohne .svg), tags."""
    for z in _library_zips():
        with zipfile.ZipFile(z) as zf:
            name = next((n for n in zf.namelist() if n.endswith("IconLibrary_DEU.xml")), None) \
                or next((n for n in zf.namelist() if n.endswith("IconLibrary.xml")), None)
            if not name:
                continue
            root = ET.fromstring(zf.read(name))
            out = []
            for ic in root.iter("Icon"):
                out.append({"uuid": ic.get("uuid", "").lower(), "name": (ic.get("Id") or "").removesuffix(".svg"),
                            "tags": ic.get("Tags", "")})
            if out:
                return out
    return []


def find(query: str, limit: int = 20) -> list[dict]:
    q = query.lower().strip()

    def score(i):
        name = i["name"].lower()
        parts = re.split(r"[-_]", name)
        tags = [t.strip() for t in i["tags"].lower().split(",")]
        if name == q:
            return 0
        if q in parts or q in tags:      # ganzes Wort in Name oder Stichwort
            return 1
        if name.startswith(q) or any(t.startswith(q) for t in tags):
            return 2
        if q in name or any(q in t for t in tags):
            return 3
        return None
    hits = [(s, i) for i in library() for s in [score(i)] if s is not None]
    hits.sort(key=lambda x: (x[0], x[1]["name"]))
    return [i for _, i in hits[:limit]]


def resolve(spec: str | None) -> str:
    """Symbol aus Name ('sun'), Dateiname ('sun.svg') oder UUID; leer = kein Symbol."""
    if not spec:
        return NONE_ICON
    s = spec.strip().lower().removesuffix(".svg")
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}", s):
        return s
    lib = library()
    if not lib:
        raise ProjectError("Symbolbibliothek nicht gefunden (C:\\ProgramData\\Loxone\\Loxone Config *\\IconLibrary.zip). "
                           "Bitte die Symbol-UUID angeben.")
    exact = [i for i in lib if i["name"].lower() == s]
    if exact:
        return exact[0]["uuid"]
    hits = find(s, 8)
    raise ProjectError(f"Symbol '{spec}' nicht gefunden. Ähnlich: {', '.join(i['name'] for i in hits) or '–'}")


def color(spec: str | int | None, default: str = "#70ff8d") -> int:
    """Farbe '#RRGGBB' -> Loxone-IcC (Blau*65536 + Grün*256 + Rot)."""
    if spec is None or spec == "":
        spec = default
    if isinstance(spec, int):
        return spec
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(spec).strip())
    if not m:
        raise ProjectError(f"Farbe '{spec}' bitte als #RRGGBB angeben, z. B. #ffc800.")
    v = m.group(1)
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    return r + g * 256 + b * 65536
