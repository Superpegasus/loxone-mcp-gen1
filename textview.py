"""Lesbare Textansicht eines Loxone-Programms (nur lesen).

Je Programmseite ein Abschnitt; je Baustein eine Zeile mit Typ und eingestellten Werten, darunter
seine Eingänge („<- Quelle.Ausgang“). Platzhalter werden aufgelöst, Positionen und Kennungen
weggelassen – so zeigt ein Git-Vergleich zweier Sicherungen nur inhaltliche Änderungen.
"""
from __future__ import annotations

import re
from collections import defaultdict

from loxproject import Block, Project

SHOW_ATTRS = ("Formula", "Analog", "MinVal", "MaxVal", "Tp", "CmdOn", "CmdOff", "Check")
RADIO_RE = re.compile(r"T\d+$")


def _name(p: Project, b: Block) -> str:
    t = b.title or b.type
    return f"{t} [{b.type}]"


def _src(p: Project, b: Block, key: str) -> str:
    b = p.logical(b)
    where = "" if b.page is not None else " (Peripherie)"
    return f"{b.title or b.type}.{key}{where}"


def _settings(b: Block) -> str:
    parts = []
    for c in b.connectors:
        if c.default not in (None, "") and not c.sources:
            parts.append(f"{c.key}={c.default}")
        if c.inverted and not c.sources:
            parts.append(f"{c.key}=negiert(dauerhaft Ein)")
    for k in SHOW_ATTRS:
        v = b.elem.get(k)
        if v not in (None, ""):
            parts.append(f"{k}={v}")
    radio = [f"{k}={v}" for k, v in b.elem.attrib.items() if RADIO_RE.fullmatch(k) and v]
    if radio:
        parts.append("Auswahl: " + ", ".join(radio))
    st = b.elem.find("StateTexts")
    if st is not None:
        rows = []
        for s in st.findall("StateText"):
            cond = ""
            if s.get("Input0"):
                op = ">=" if s.get("Cond0") == "1" else ("=" if not s.get("Cond0") else f"cond{s.get('Cond0')}")
                cond = f"I{s.get('Input0')}{op}{s.get('CondV0', '0')}: "
            rows.append(cond + (s.get("Text") or ""))
        parts.append("Zustände: " + " | ".join(rows))
    return ("  {" + "; ".join(parts) + "}") if parts else ""


def render(p: Project, page: str | None = None) -> str:
    L = p.logical
    incoming: dict[str, list] = defaultdict(list)
    for e in p.edges:
        incoming[L(e.dst).uuid].append(e)
    pages = [b for b in p.blocks.values() if b.type == "Page"]
    if page:
        pg = p.find_page(page)
        pages = [pg]
    lines: list[str] = []
    for pg in sorted(pages, key=lambda x: x.title.lower()):
        blocks = [b for b in p.blocks.values() if b.page is not None and b.page.uuid == pg.uuid
                  and not p.is_ref(b)]
        outs = [r for r in p.blocks.values() if r.page is not None and r.page.uuid == pg.uuid
                and p.is_ref(r) and r.type.startswith("OutputRef") and p.target(r) is not None]
        if not blocks and not outs:
            continue
        lines.append(f"=== Seite {pg.title} ===")

        def key(b):
            try:
                return (int(b.elem.get("Py", 0)), int(b.elem.get("Px", 0)), b.title)
            except ValueError:
                return (0, 0, b.title)
        for b in sorted(blocks, key=key):
            lines.append(f"{_name(p, b)}{_settings(b)}")
            for e in sorted(incoming.get(b.uuid, []), key=lambda e: (e.dst_key, e.src.title)):
                inv = " (negiert)" if e.inverted else ""
                lines.append(f"    {e.dst_key} <- {_src(p, e.src, e.src_key)}{inv}")
        # Ausgänge der Seite in die Peripherie (Aktoren, Befehle, Merker anderer Seiten …)
        seen = set()
        for r in sorted(outs, key=key):
            t = p.target(r)
            for e in incoming.get(L(r).uuid, []):
                if e.dst is not r or (t.uuid, e.src.uuid) in seen:
                    continue
                seen.add((t.uuid, e.src.uuid))
                lines.append(f"-> {t.title} [{t.type}] <- {_src(p, e.src, e.src_key)}"
                             + (" (negiert)" if e.inverted else ""))
        lines.append("")
    return "\n".join(lines)
