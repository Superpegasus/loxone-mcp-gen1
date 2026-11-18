"""Schreibkern (Stufe 3): ändert den Rohtext einer Projektdatei gezielt an einzelnen Stellen.

Arbeitet bewusst auf dem Text statt auf einem XML-Baum, damit jede nicht betroffene
Stelle der Datei Byte für Byte erhalten bleibt (Zeilenenden, Tabs, Schreibweise).

Beobachtete Regeln von Loxone Config:
  UUID        = hex(Sekunden seit 1.1.2009 UTC) - Millisekunden - Zähler - Endung
  Baustein    Endung ffff112233445566; seine Anschlüsse haben die Zähler davor
  Anschluss   Endung = laufende Nummer (2 hex) + "ff" + 12 Zufallsziffern je Baustein
  Co          Attributreihenfolge K, Def, Nc, U;  Nc = Anzahl der <In> darunter
  Nio         = Anzahl Anschlüsse des Bausteins
  NumO        = Anzahl aller Objekte (am Document), NextObj = fortlaufender Namenszähler
"""
from __future__ import annotations

import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

EPOCH = datetime(2009, 1, 1, tzinfo=timezone.utc)
BLOCK_TAIL = "ffff112233445566"
TAG = r'(?:[^>"]|"[^"]*")*?'
C_TAG_RE = re.compile(r"<C\b" + TAG + r"(/?)>|</C>")
UUID_ANY_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}")
COUNTERS = {
    "NumO": r'<C Type="Document"' + TAG + r'\sNumO="(\d+)"',
    "NextObj": r'<ControlList\b[^>]*\sNextObj="(\d+)"',
    "NextMem": r'<ControlList\b[^>]*\sNextMem="(\d+)"',
}


class WriteError(Exception):
    pass


def esc(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
             .replace("\r", "&#xD;").replace("\n", "&#xA;").replace("\t", "&#x9;"))


def render(e: ET.Element, indent: str, nl: str) -> list[str]:
    """XML in der Schreibweise von Loxone Config: Tabs, leere Elemente als <x/>."""
    attrs = "".join(f' {k}="{esc(v)}"' for k, v in e.attrib.items())
    text = e.text if e.text and e.text.strip() else ""
    kids = list(e)
    if not kids and not text:
        return [f"{indent}<{e.tag}{attrs}/>"]
    if not kids:
        return [f"{indent}<{e.tag}{attrs}>{esc(text)}</{e.tag}>"]
    lines = [f"{indent}<{e.tag}{attrs}>"]
    for k in kids:
        lines += render(k, indent + "\t", nl)
    lines.append(f"{indent}</{e.tag}>")
    return lines


class ProjectText:
    def __init__(self, raw: bytes):
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        self.text = raw.decode("utf-8-sig")
        self.nl = "\r\n" if "\r\n" in self.text else "\n"
        self._counter = random.randint(0x1000, 0xEFFF)

    def data(self) -> bytes:
        return (b"\xef\xbb\xbf" if self.bom else b"") + self.text.encode("utf-8")

    # ---------- UUIDs ----------
    def new_block_uuids(self, n_connectors: int) -> tuple[str, list[str]]:
        used = set(UUID_ANY_RE.findall(self.text))
        now = datetime.now(timezone.utc)
        secs = int((now - EPOCH).total_seconds())
        while True:
            head = f"{secs:08x}-{now.microsecond // 1000:04x}"
            rnd = f"{random.getrandbits(48):012x}"
            cos = [f"{head}-{(self._counter + i) & 0xFFFF:04x}-{i:02x}ff{rnd}" for i in range(n_connectors)]
            block = f"{head}-{(self._counter + n_connectors) & 0xFFFF:04x}-{BLOCK_TAIL}"
            self._counter = (self._counter + n_connectors + 1) & 0xFFFF
            if block not in used and not any(c in used for c in cos):
                return block, cos

    # ---------- Suchen ----------
    def _line_start(self, pos: int) -> int:
        return self.text.rfind("\n", 0, pos) + 1

    def _indent(self, pos: int) -> str:
        ind = self.text[self._line_start(pos):pos]
        if ind.strip():
            raise WriteError("Unerwartete Formatierung: Element beginnt nicht am Zeilenanfang.")
        return ind

    def _c_open(self, uuid: str) -> re.Match:
        m = re.search(r"<C\b" + TAG + r'\sU="%s"' % re.escape(uuid) + TAG + r"(/?)>", self.text)
        if not m:
            raise WriteError(f"Objekt {uuid} nicht in der Datei gefunden.")
        return m

    def _c_close(self, open_m: re.Match) -> int:
        if open_m.group(1) == "/":
            return -1
        depth = 1
        for m in C_TAG_RE.finditer(self.text, open_m.end()):
            if m.group(0) == "</C>":
                depth -= 1
                if depth == 0:
                    return m.start()
            elif m.group(1) != "/":
                depth += 1
        raise WriteError("Schließendes Tag nicht gefunden; die Datei ist unvollständig.")

    def _co(self, uuid: str) -> tuple[re.Match, int]:
        """Öffnendes Co-Tag und Position von </Co> (-1 bei leerem Element)."""
        m = re.search(r"<Co\b" + TAG + r'\sU="%s"' % re.escape(uuid) + TAG + r"(/?)>", self.text)
        if not m:
            raise WriteError(f"Anschluss {uuid} nicht in der Datei gefunden.")
        if m.group(1) == "/":
            return m, -1
        end = self.text.find("</Co>", m.end())
        if end < 0:
            raise WriteError("Schließendes </Co> nicht gefunden.")
        return m, end

    def _replace(self, start: int, end: int, new: str) -> None:
        self.text = self.text[:start] + new + self.text[end:]

    # ---------- Zähler ----------
    def bump(self, counter: str, delta: int) -> None:
        m = re.search(COUNTERS[counter], self.text)
        if not m:
            raise WriteError(f"Zähler {counter} nicht gefunden.")
        self._replace(m.start(1), m.end(1), str(int(m.group(1)) + delta))

    # ---------- Bausteine ----------
    def insert_block(self, page_uuid: str, elem: ET.Element) -> None:
        open_m = self._c_open(page_uuid)
        close = self._c_close(open_m)
        if close < 0:
            raise WriteError("Die Seite ist ein leeres Element; Einfügen wird dafür nicht unterstützt.")
        ls = self._line_start(close)
        ind = self._indent(close) + "\t"
        block = self.nl.join(render(elem, ind, self.nl)) + self.nl
        self._replace(ls, ls, block)
        self.bump("NumO", sum(1 for e in elem.iter("C") if e.get("U")))

    def remove_block(self, uuid: str) -> None:
        open_m = self._c_open(uuid)
        close = self._c_close(open_m)
        end = open_m.end() if close < 0 else close + len("</C>")
        inner = self.text[open_m.start():end]
        n = len(re.findall(r"<C\b", inner))
        ls = self._line_start(open_m.start())
        self._indent(open_m.start())
        nl_end = self.text.find("\n", end)
        self._replace(ls, len(self.text) if nl_end < 0 else nl_end + 1, "")
        self.bump("NumO", -n)

    def remove_line_element(self, pattern: str) -> int:
        """Entfernt alle einzeiligen, leeren Elemente (<x .../>), auf die das Muster passt.
        Gibt die Anzahl zurück."""
        n = 0
        while True:
            m = re.search(r"[ \t]*" + pattern + r"\r?\n", self.text)
            if not m:
                return n
            if not m.group(0).rstrip().endswith("/>"):
                raise WriteError("Unerwarteter Aufbau: Element ist nicht leer.")
            ls = self._line_start(m.start())
            self._indent(ls + len(m.group(0)) - len(m.group(0).lstrip()))
            self._replace(ls, m.end(), "")
            n += 1

    def remove_permission_refs(self, group_uuid: str) -> int:
        """Entfernt <PRM REF="gruppe" .../> aus allen Berechtigungslisten und zählt NUM des PH herunter."""
        n = 0
        while True:
            m = re.search(r'[ \t]*<PRM REF="%s"[^>]*/>\r?\n' % re.escape(group_uuid), self.text)
            if not m:
                return n
            ph = self.text.rfind("<PH ", 0, m.start())
            ph_close = self.text.find("</PH>", ph)
            if ph < 0 or ph_close < m.start():
                raise WriteError("Berechtigungseintrag liegt nicht in einer Berechtigungsliste.")
            self._replace(m.start(), m.end(), "")
            tag_end = self.text.find(">", ph)
            tag = self.text[ph:tag_end + 1]
            num = re.search(r'\sNUM="(\d+)"', tag)
            if not num:
                raise WriteError("Berechtigungsliste ohne Zähler NUM.")
            self._replace(ph + num.start(1), ph + num.end(1), str(int(num.group(1)) - 1))
            n += 1

    def replace_attr_value(self, attr: str, old: str, new: str) -> int:
        """Ersetzt den Wert eines Attributs überall, wo er genau 'old' ist."""
        pat = re.compile(r'(\s%s=")%s(")' % (re.escape(attr), re.escape(old)))
        self.text, n = pat.subn(lambda m: m.group(1) + new + m.group(2), self.text)
        return n

    def set_child_attr(self, uuid: str, child: str, name: str, value: str | None) -> None:
        """Setzt/entfernt ein Attribut am ersten direkten Kindelement 'child' (z. B. IoData) eines Objekts."""
        open_m = self._c_open(uuid)
        close = self._c_close(open_m)
        if close < 0:
            raise WriteError("Das Objekt hat keine Kindelemente.")
        body = self.text[open_m.end():close]
        cm = re.search(r"<%s\b[^>]*>" % re.escape(child), body)
        if not cm:
            raise WriteError(f"Kindelement {child} nicht gefunden.")
        tag = cm.group(0)
        am = re.search(r'\s%s="[^"]*"' % re.escape(name), tag)
        if am:
            new = tag[:am.start()] + ("" if value is None else f' {name}="{esc(value)}"') + tag[am.end():]
        elif value is None:
            return
        else:
            end = len(tag) - (2 if tag.endswith("/>") else 1)
            new = tag[:end].rstrip() + f' {name}="{esc(value)}"' + tag[end:]
        s = open_m.end() + cm.start()
        self._replace(s, s + len(tag), new)

    def set_block_attr(self, uuid: str, name: str, value: str | None) -> None:
        """Setzt eine vorhandene Eigenschaft; value=None entfernt sie."""
        m = self._c_open(uuid)
        tag = m.group(0)
        am = re.search(r'\s%s="[^"]*"' % re.escape(name), tag)
        if not am:
            raise WriteError(f"Eigenschaft {name} ist an diesem Baustein nicht vorhanden.")
        new = tag[:am.start()] + ("" if value is None else f' {name}="{esc(value)}"') + tag[am.end():]
        self._replace(m.start(), m.end(), new)

    def add_block_attr(self, uuid: str, name: str, value: str) -> None:
        """Fügt eine Eigenschaft hinzu (direkt nach Type, wie Loxone Config z. B. Desc schreibt)."""
        m = self._c_open(uuid)
        tag = m.group(0)
        if re.search(r'\s%s="' % re.escape(name), tag):
            raise WriteError(f"Eigenschaft {name} ist schon vorhanden.")
        tm = re.search(r'\sType="[^"]*"', tag)
        pos = tm.end() if tm else len("<C")
        new = tag[:pos] + f' {name}="{esc(value)}"' + tag[pos:]
        self._replace(m.start(), m.end(), new)

    # ---------- Anschlüsse ----------
    @staticmethod
    def _set_tag_attr(tag: str, name: str, value: str | None, before: tuple[str, ...]) -> str:
        """Setzt, ersetzt oder entfernt ein Attribut im Co-Tag; neue Attribute vor 'before'."""
        am = re.search(r'\s%s="[^"]*"' % name, tag)
        if am:
            return tag[:am.start()] + ("" if value is None else f' {name}="{esc(value)}"') + tag[am.end():]
        if value is None:
            return tag
        for b in before:
            bm = re.search(r'\s%s="' % b, tag)
            if bm:
                return tag[:bm.start()] + f' {name}="{esc(value)}"' + tag[bm.start():]
        raise WriteError("Unerwarteter Aufbau eines Anschlusses.")

    def set_co_default(self, co_uuid: str, value: str | None) -> None:
        m, _ = self._co(co_uuid)
        self._replace(m.start(), m.end(), self._set_tag_attr(m.group(0), "Def", value, ("Nc", "U")))

    def set_co_inverted(self, co_uuid: str, inverted: bool) -> None:
        """Negiert einen Eingang. Ein freier, negierter Eingang ist dauerhaft Ein.
        Schreibweise in Loxone-Dateien: <Co K="..." Inv="true" U="..."/>"""
        m, _ = self._co(co_uuid)
        new = self._set_tag_attr(m.group(0), "Inv", "true" if inverted else None, ("Def", "Nc", "U"))
        self._replace(m.start(), m.end(), new)

    def add_in(self, dst_co: str, src_co: str) -> None:
        m, end = self._co(dst_co)
        ind = self._indent(m.start())
        tag = m.group(0)
        line = f'{ind}\t<In Input="{src_co}"/>{self.nl}'
        if end < 0:
            opened = self._set_tag_attr(tag[:-2] + ">", "Nc", "1", ("U",))
            self._replace(m.start(), m.end(), f"{opened}{self.nl}{line}{ind}</Co>")
            return
        ls = self._line_start(end)
        self._indent(end)
        self._replace(ls, ls, line)  # hinter die vorhandenen Verbindungen, Tag-Position bleibt gültig
        nc = len(re.findall(r"<In\b", self.text[m.end():end + len(line)]))
        self._replace(m.start(), m.end(), self._set_tag_attr(tag, "Nc", str(nc), ("U",)))

    def remove_in(self, dst_co: str, src_co: str) -> None:
        m, end = self._co(dst_co)
        if end < 0:
            raise WriteError("An diesem Anschluss gibt es keine Verbindung.")
        body = self.text[m.end():end]
        im = re.search(r'[ \t]*<In Input="%s"[^>]*/>\r?\n' % re.escape(src_co), body)
        if not im:
            raise WriteError("Diese Verbindung existiert nicht.")
        rest = body[:im.start()] + body[im.end():]
        nc = len(re.findall(r"<In\b", rest))
        tag = m.group(0)
        if nc == 0:
            if rest.strip():
                raise WriteError("Unerwarteter Inhalt im Anschluss; Verbindung wird nicht gelöst.")
            closed = self._set_tag_attr(tag, "Nc", None, ())[:-1] + "/>"
            self._replace(m.start(), end + len("</Co>"), closed)
        else:
            self._replace(m.start(), end, self._set_tag_attr(tag, "Nc", str(nc), ("U",)) + rest)
