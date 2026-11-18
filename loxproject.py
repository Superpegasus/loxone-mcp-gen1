"""Liest eine Loxone-Config-Projektdatei (.Loxone) und stellt sie als Graph bereit.

Nur lesend: Diese Datei schreibt niemals in ein Projekt.

Aufbau der Datei (soweit hier genutzt):
  <C Type=... U=uuid Title=...>     ein Objekt (Baustein, Seite, Gerät, Raum ...)
    <Co K=name U=uuid Def=wert Inv=...>   ein Anschluss (Ein- oder Ausgang)
      <In Input=uuid-des-Quellanschlusses/>   eine Verbindung zu diesem Eingang
    <IoData Cr=kategorie-uuid Pr=raum-uuid/>
  Objekte mit Attribut Ref (InputRef, OutputRef ...) sind Platzhalter auf einer
  Seite, die auf das eigentliche Objekt (Eingang, Aktor, Merker ...) verweisen.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}", re.I)
SECRET_RE = re.compile(r"(?i)pass|pwd|hash|key|token|secret|appid|^s$|^sc$|^alg$|^user$")
# Attribute, die nur Darstellung oder interne Verwaltung sind
HIDDEN_ATTRS = {
    "Type", "U", "Title", "Desc", "V", "Px", "Py", "Px2", "Py2", "Cl", "Nio",
    "LtE", "WF", "Ref", "LinkRefType", "COName", "Icon", "Ix",
}
SHORT_LEN = 18  # "xxxxxxxx-xxxx-xxxx"
MAX_VALUE_LEN = 160


@dataclass
class Connector:
    key: str
    uuid: str
    default: str | None
    inverted: bool
    sources: list[tuple[str, str | None]] = field(default_factory=list)  # (Quell-UUID, FLG)


@dataclass
class Block:
    elem: ET.Element
    uuid: str
    type: str
    title: str
    parent: "Block | None"
    page: "Block | None" = None
    connectors: list[Connector] = field(default_factory=list)

    @property
    def desc(self) -> str:
        return self.elem.get("Desc", "")

    def connector(self, key: str) -> Connector | None:
        for c in self.connectors:
            if c.key.lower() == key.lower():
                return c
        return None


@dataclass
class Edge:
    src: Block
    src_key: str
    dst: Block
    dst_key: str
    inverted: bool


class ProjectError(Exception):
    pass


class Project:
    def __init__(self, path: str | Path, data: bytes | None = None):
        """data: Inhalt statt der Datei lesen (für den Arbeitsstand beim Bearbeiten)."""
        self.path = Path(path)
        if not self.path.is_file():
            raise ProjectError(f"Datei nicht gefunden: {self.path}")
        self.mtime = self.path.stat().st_mtime
        raw = data if data is not None else self.path.read_bytes()
        # Neuere Config-Versionen schreiben Attribute, die mit einer Ziffer beginnen (z. B. 12hTF="false").
        # Das ist kein gültiges XML; nur zum Einlesen umbenennen (der Schreibweg arbeitet am Rohtext).
        raw = re.sub(rb'(\s)(\d[\w.-]*)(=")', rb"\1_\2\3", raw)
        try:
            self.root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise ProjectError(f"Keine gültige Loxone-Projektdatei (XML-Fehler): {e}") from e
        if self.root.tag != "ControlList":
            raise ProjectError("Keine Loxone-Projektdatei: Wurzelelement ist nicht 'ControlList'.")

        self.blocks: dict[str, Block] = {}
        self.co_index: dict[str, tuple[Block, Connector]] = {}
        self.pages: list[Block] = []
        self.places: dict[str, str] = {}
        self.categories: dict[str, str] = {}
        self.dangling = 0  # Verbindungen, deren Quelle nicht existiert
        self.source_cos: set[str] = set()  # Anschlüsse, die irgendwo als Quelle dienen (also Ausgänge)
        doc = self.root.find("C[@Type='Document']")
        try:
            self.config_version = int(doc.get("ConfigVersion", "0")) if doc is not None else 0
        except ValueError:
            self.config_version = 0
        self._walk(self.root, None, None)
        self._short = self._build_short_ids()
        self._build_edges()

    # ---------- Einlesen ----------
    def _walk(self, elem: ET.Element, parent: Block | None, page: Block | None) -> None:
        for child in elem.findall("C"):
            uuid = child.get("U", "")
            block = Block(child, uuid, child.get("Type", "?"), child.get("Title", ""), parent, page)
            for co in child.findall("Co"):
                con = Connector(
                    key=co.get("K", "?"),
                    uuid=co.get("U", ""),
                    default=co.get("Def"),
                    inverted=co.get("Inv") not in (None, "", "0", "false"),
                    sources=[(i.get("Input", ""), i.get("FLG")) for i in co.findall("In")],
                )
                block.connectors.append(con)
                if con.uuid:
                    self.co_index[con.uuid] = (block, con)
            if uuid:
                self.blocks[uuid] = block
            if block.type == "Page":
                self.pages.append(block)
            elif block.type == "Place":
                self.places[uuid] = block.title
            elif block.type == "Category":
                self.categories[uuid] = block.title
            self._walk(child, block, block if block.type == "Page" else page)

    def _build_short_ids(self) -> dict[str, str]:
        counts = Counter(u[:SHORT_LEN] for u in self.blocks)
        return {u: (u[:SHORT_LEN] if counts[u[:SHORT_LEN]] == 1 else u) for u in self.blocks}

    def _build_edges(self) -> None:
        self.edges: list[Edge] = []
        self.out_edges: dict[str, list[Edge]] = defaultdict(list)   # physisch, je Baustein
        self.in_edges: dict[str, list[Edge]] = defaultdict(list)
        self.log_out: dict[str, list[Edge]] = defaultdict(list)     # logisch, Referenzen aufgelöst
        self.log_in: dict[str, list[Edge]] = defaultdict(list)
        for dst in self.blocks.values():
            for con in dst.connectors:
                for src_uuid, _flg in con.sources:
                    self.source_cos.add(src_uuid)
                    hit = self.co_index.get(src_uuid)
                    if hit is None:
                        self.dangling += 1
                        continue
                    src, src_con = hit
                    if self._is_plumbing(src, dst):
                        continue
                    e = Edge(src, src_con.key, dst, con.key, con.inverted)
                    self.edges.append(e)
                    self.out_edges[src.uuid].append(e)
                    self.in_edges[dst.uuid].append(e)
                    self.log_out[self.logical(src).uuid].append(e)
                    self.log_in[self.logical(dst).uuid].append(e)

    # ---------- Referenzen ----------
    def is_ref(self, b: Block) -> bool:
        ref = b.elem.get("Ref")
        return bool(ref) and ref in self.blocks and ref != b.uuid

    def target(self, b: Block) -> Block | None:
        return self.blocks.get(b.elem.get("Ref", "")) if self.is_ref(b) else None

    def logical(self, b: Block) -> Block:
        """Das eigentliche Objekt: bei Platzhaltern das Ziel, sonst der Baustein selbst."""
        return self.target(b) or b

    @staticmethod
    def _related(a: Block, b: Block) -> bool:
        x: Block | None = a
        while x is not None:
            if x is b:
                return True
            x = x.parent
        x = b
        while x is not None:
            if x is a:
                return True
            x = x.parent
        return False

    def _is_plumbing(self, src: Block, dst: Block) -> bool:
        """Interne Verbindung zwischen einem Platzhalter und seinem eigenen Zielobjekt."""
        t = self.target(dst)
        if t is not None and self._related(src, t):
            return True
        t = self.target(src)
        return t is not None and self._related(dst, t)

    # ---------- Eigenschaften ----------
    def short(self, b: Block) -> str:
        return self._short.get(b.uuid, b.uuid)

    def _iodata(self, b: Block, attr: str) -> str | None:
        io = b.elem.find("IoData")
        return io.get(attr) if io is not None else None

    def room(self, b: Block) -> str | None:
        for cand in (b, self.target(b), b.page):
            if cand is not None:
                name = self.places.get(self._iodata(cand, "Pr") or "")
                if name:
                    return name
        return None

    def category(self, b: Block) -> str | None:
        for cand in (b, self.target(b), b.page):
            if cand is not None:
                name = self.categories.get(self._iodata(cand, "Cr") or "")
                if name:
                    return name
        return None

    def location(self, b: Block) -> str:
        """Wo das Objekt liegt: Seite oder Pfad im Peripheriebaum."""
        if b.page is not None:
            return f"Seite {b.page.title}"
        parts = []
        x = b.parent
        while x is not None and x.type != "Document":
            if x.title:
                parts.append(x.title)
            x = x.parent
        return " / ".join(reversed(parts)) or "Projekt"

    def params(self, b: Block) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in b.elem.attrib.items():
            if k in HIDDEN_ATTRS or SECRET_RE.search(k) or k.lower().startswith("uuid"):
                continue
            if UUID_RE.search(v):
                continue
            out[k] = v if len(v) <= MAX_VALUE_LEN else v[:MAX_VALUE_LEN] + "…"
        return out

    def name(self, b: Block) -> str:
        t = self.target(b)
        if t is not None:
            return f"{t.title or b.title} [{t.type}] #{self.short(t)}"
        return f"{b.title} [{b.type}] #{self.short(b)}"

    def label(self, b: Block, key: str, ctx_page: Block | None = None) -> str:
        s = f"{self.name(b)}.{key}"
        if b.page is not None and b.page is not ctx_page:
            s += f" @Seite {b.page.title}"
        return s

    # ---------- Suche ----------
    def resolve(self, spec: str, page: str | None = None) -> Block:
        spec = spec.strip().lstrip("#")
        if not spec:
            raise ProjectError("Kein Baustein angegeben.")
        if spec in self.blocks:
            return self.blocks[spec]
        if re.fullmatch(r"[0-9a-f]{8}[0-9a-f-]*", spec, re.I):
            hits = [b for u, b in self.blocks.items() if u.startswith(spec.lower())]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ProjectError(f"Kennung '{spec}' ist nicht eindeutig ({len(hits)} Treffer).")
        low = spec.lower()
        hits = [b for b in self.blocks.values() if b.title.lower() == low]
        if page:
            pg = self.find_page(page)
            hits = [b for b in hits if b.page is pg]
        # Platzhalter und ihr Ziel zählen als ein Objekt
        uniq = {self.logical(b).uuid: self.logical(b) for b in hits}
        if len(uniq) > 1:
            # Räume, Seiten und Berechtigungsgruppen heißen oft wie ein Baustein; Bausteine haben Vorrang
            real = {u: b for u, b in uniq.items() if b.connectors}
            on_page = {u: b for u, b in real.items() if b.page is not None}
            for narrowed in (real, on_page):
                if len(narrowed) == 1:
                    uniq = narrowed
                    break
        if len(uniq) == 1:
            return next(iter(uniq.values()))
        if not uniq:
            raise ProjectError(f"Kein Baustein mit Kennung oder Titel '{spec}' gefunden. Nutze find_blocks zum Suchen.")
        cands = "; ".join(f"{self.name(b)} ({self.location(b)})" for b in list(uniq.values())[:12])
        raise ProjectError(f"Titel '{spec}' ist mehrdeutig ({len(uniq)} Treffer): {cands}. Bitte die #Kennung verwenden.")

    def find_page(self, spec: str) -> Block:
        low = spec.strip().lower()
        exact = [p for p in self.pages if p.title.lower() == low or p.uuid == spec]
        if len(exact) == 1:
            return exact[0]
        part = [p for p in self.pages if low in p.title.lower()]
        if len(part) == 1:
            return part[0]
        names = ", ".join(p.title for p in self.pages)
        raise ProjectError(f"Seite '{spec}' nicht eindeutig gefunden. Vorhandene Seiten: {names}")

    # ---------- Ausgaben ----------
    def summary(self) -> dict:
        doc = self.root.find("C[@Type='Document']")
        types = Counter(b.type for b in self.blocks.values() if b.page is not None)
        return {
            "datei": str(self.path),
            "titel": doc.get("Title") if doc is not None else None,
            "config_version": doc.get("ConfigVersion") if doc is not None else None,
            "gespeichert": doc.get("Date") if doc is not None else None,
            "objekte": len(self.blocks),
            "verbindungen": len(self.edges),
            "verbindungen_ohne_quelle": self.dangling,
            "raeume": len(self.places),
            "kategorien": len(self.categories),
            "seiten": [{"titel": p.title, "bausteine": len(p.elem.findall("C"))} for p in self.pages],
            "haeufigste_typen_auf_seiten": dict(types.most_common(25)),
        }

    def describe(self, b: Block, ctx_page: Block | None, include_unconnected: bool) -> dict:
        d: dict = {"id": self.short(b), "typ": b.type, "titel": b.title}
        t = self.target(b)
        if t is not None:
            d["verweist_auf"] = f"{t.title} [{t.type}] #{self.short(t)} ({self.location(t)})"
        if b.desc:
            d["beschreibung"] = b.desc
        room, cat = self.room(b), self.category(b)
        if room:
            d["raum"] = room
        if cat:
            d["kategorie"] = cat
        p = self.params(b)
        if p:
            d["parameter"] = p
        ins: dict[str, object] = {}
        outs: dict[str, list[str]] = {}
        free: list[str] = []
        by_in: dict[str, list[Edge]] = defaultdict(list)
        by_out: dict[str, list[Edge]] = defaultdict(list)
        for e in self.in_edges.get(b.uuid, []):
            by_in[e.dst_key].append(e)
        for e in self.out_edges.get(b.uuid, []):
            by_out[e.src_key].append(e)
        for con in b.connectors:
            used = False
            if con.key in by_in:
                srcs = [self.label(e.src, e.src_key, ctx_page) for e in by_in[con.key]]
                entry: object = srcs if not con.inverted else {"invertiert": True, "von": srcs}
                ins[con.key] = entry
                used = True
            elif con.default not in (None, ""):
                ins[con.key] = f"= {con.default}"
                used = True
            if con.key in by_out:
                outs[con.key] = [self.label(e.dst, e.dst_key, ctx_page) for e in by_out[con.key]]
                used = True
            if not used:
                free.append(con.key)
        if ins:
            d["eingaenge"] = ins
        if outs:
            d["ausgaenge"] = outs
        if include_unconnected and free:
            d["unbelegt"] = free
        return d

    def page_view(self, page: Block, include_unconnected: bool = False) -> dict:
        blocks = [self.blocks[c.get("U")] for c in page.elem.findall("C") if c.get("U") in self.blocks]
        blocks.sort(key=lambda b: (int(b.elem.get("Px", "0") or 0), int(b.elem.get("Py", "0") or 0)))
        return {
            "seite": page.title,
            "raum": self.room(page),
            "kategorie": self.category(page),
            "anzahl": len(blocks),
            "bausteine": [self.describe(b, page, include_unconnected) for b in blocks],
        }

    def block_view(self, b: Block) -> dict:
        b = self.logical(b)
        d = self.describe(b, None, True)
        d["uuid"] = b.uuid
        d["ort"] = self.location(b)
        refs = [r for r in self.blocks.values() if self.target(r) is b]
        if refs:
            d["verwendet_auf"] = sorted({self.location(r) for r in refs})
            uses_in = [f"{self.label(e.src, e.src_key)} -> {e.dst_key}" for r in refs for e in self.in_edges.get(r.uuid, [])]
            uses_out = [f"{e.src_key} -> {self.label(e.dst, e.dst_key)}" for r in refs for e in self.out_edges.get(r.uuid, [])]
            if uses_in:
                d["gesteuert_von"] = uses_in
            if uses_out:
                d["wirkt_auf"] = uses_out
        subs = [self.blocks[c.get("U")] for c in b.elem.findall("C") if c.get("U") in self.blocks]
        if subs:
            d["unterobjekte"] = [self.name(s) for s in subs[:60]]
        return d

    def find(self, query: str | None, type_: str | None, room: str | None,
             category: str | None, page: str | None, limit: int) -> dict:
        pg = self.find_page(page) if page else None
        q = (query or "").lower()
        seen: set[str] = set()
        hits: list[dict] = []
        total = 0
        for b in self.blocks.values():
            if self.is_ref(b) and pg is None:
                continue  # Platzhalter nur bei Seitensuche einzeln zeigen
            if pg is not None and b.page is not pg:
                continue
            if type_ and type_.lower() not in b.type.lower() and not (
                self.target(b) and type_.lower() in self.target(b).type.lower()):
                continue
            if q and q not in b.title.lower() and q not in b.desc.lower() and q not in b.uuid:
                continue
            if room and room.lower() not in (self.room(b) or "").lower():
                continue
            if category and category.lower() not in (self.category(b) or "").lower():
                continue
            if b.uuid in seen:
                continue
            seen.add(b.uuid)
            total += 1
            if len(hits) < limit:
                h = {"id": self.short(b), "typ": b.type, "titel": b.title, "ort": self.location(b)}
                if b.desc:
                    h["beschreibung"] = b.desc
                r = self.room(b)
                if r:
                    h["raum"] = r
                hits.append(h)
        return {"treffer_gesamt": total, "angezeigt": len(hits), "treffer": hits}

    def trace(self, start: Block, connector: str | None, direction: str,
              max_depth: int, max_edges: int = 150) -> dict:
        start = self.logical(start)
        forward = direction != "backward"
        table = self.log_out if forward else self.log_in
        visited = {start.uuid}
        queue: deque[tuple[Block, int, str | None]] = deque([(start, 1, connector)])
        steps: list[dict] = []
        truncated = False
        while queue:
            node, depth, only_key = queue.popleft()
            for e in table.get(node.uuid, []):
                own, own_key = (e.src, e.src_key) if forward else (e.dst, e.dst_key)
                # Anschlussfilter nur anwenden, wenn die Verbindung direkt am Objekt hängt
                if only_key and own is node and own_key.lower() != only_key.lower():
                    continue
                if len(steps) >= max_edges:
                    truncated = True
                    break
                pg = e.dst.page or e.src.page
                steps.append({
                    "tiefe": depth,
                    "von": self.label(e.src, e.src_key, pg),
                    "nach": self.label(e.dst, e.dst_key, pg),
                    "seite": pg.title if pg else None,
                    **({"invertiert": True} if e.inverted else {}),
                })
                nxt = self.logical(e.dst if forward else e.src)
                if nxt.uuid not in visited and depth < max_depth:
                    visited.add(nxt.uuid)
                    queue.append((nxt, depth + 1, None))
            if truncated:
                break
        return {
            "start": f"{self.name(start)} ({self.location(start)})",
            "richtung": "vorwärts (wohin wirkt das Signal)" if forward else "rückwärts (woher kommt das Signal)",
            "hinweis": "Innerhalb eines Bausteins wird angenommen, dass jeder Eingang jeden Ausgang beeinflussen kann.",
            "schritte": steps,
            "abgeschnitten": truncated,
        }
