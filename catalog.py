"""Baustein-Katalog (Stufe 2): lernt aus Projektdateien, wie jeder Bausteintyp aussieht.

Je Typ: Anschlüsse mit Richtung und Standardwert, Eigenschaften und eine XML-Vorlage.
Der Katalog wird als JSON gespeichert; Projektdateien werden nur gelesen.
"""
from __future__ import annotations

import copy
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from loxproject import SECRET_RE, UUID_RE, Block, Project, ProjectError

LAYOUT_ATTRS = {"Px", "Py", "Px2", "Py2"}
INSTANCE_ATTRS = {"U", "Title", "LtE", "Ref"} | LAYOUT_ATTRS
OUT_GUESS_RE = re.compile(r"^(AQ|TQ|Q)")
MAX_TEMPLATE_SHOWN = 4000


def _int(s: str | None) -> int:
    try:
        return int(s or 0)
    except ValueError:
        return 0


class Catalog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"quellen": [], "typen": {}}
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                raise ProjectError(f"Katalogdatei {self.path} ist nicht lesbar: {e}") from e

    @property
    def types(self) -> dict:
        return self.data.get("typen", {})

    # ---------- Lernen ----------
    def learn(self, files: list[Path], reset: bool = False) -> dict:
        sources = [] if reset else [Path(p) for p in self.data.get("quellen", [])]
        for f in files:
            if f not in sources:
                sources.append(f)
        projects: list[Project] = []
        skipped: list[str] = []
        for f in sources:
            try:
                projects.append(Project(f))
            except ProjectError as e:
                skipped.append(f"{f.name}: {e}")
        if not projects:
            raise ProjectError("Keine lesbare Projektdatei zum Lernen gefunden.")
        before = set(self.types)
        self.data = {
            "erstellt": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "quellen": [str(p.path) for p in projects],
            "typen": self._build(projects),
        }
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        now = set(self.types)
        return {
            "katalog": str(self.path),
            "gelernte_dateien": [p.path.name for p in projects],
            "uebersprungen": skipped,
            "typen_gesamt": len(now),
            "typen_auf_seiten": sum(1 for t in self.types.values() if t["beispiele_auf_seiten"]),
            "neu_hinzugekommen": sorted(now - before) if before else "erster Lernlauf",
        }

    def _build(self, projects: list[Project]) -> dict:
        examples: dict[str, list[tuple[Project, Block]]] = defaultdict(list)
        for p in projects:
            for b in p.blocks.values():
                examples[b.type].append((p, b))
        return {t: self._build_type(t, ex) for t, ex in sorted(examples.items())}

    def _build_type(self, type_: str, ex: list[tuple[Project, Block]]) -> dict:
        # Maßgeblich ist die Datei mit der neuesten Config-Version, in der der Typ vorkommt
        newest = max((p for p, _ in ex), key=lambda p: (p.config_version, p.mtime))
        ex_new = [(p, b) for p, b in ex if p is newest]
        on_page = [(p, b) for p, b in ex_new if b.page is not None]
        pool = on_page or ex_new

        shapes = Counter(tuple(c.key for c in b.connectors) for _, b in pool)
        main = shapes.most_common(1)[0][0]
        same = [(p, b) for p, b in pool if tuple(c.key for c in b.connectors) == main]

        # Richtung: Belege aus allen Beispielen aller Dateien
        in_ev: Counter = Counter()
        out_ev: Counter = Counter()
        for p, b in ex:
            for c in b.connectors:
                if c.sources or c.default not in (None, ""):
                    in_ev[c.key] += 1
                if c.uuid in p.source_cos:
                    out_ev[c.key] += 1

        # Loxone speichert erst alle Eingänge, dann alle Ausgänge. Stimmt das mit den
        # belegten Anschlüssen überein, lässt sich die Richtung der übrigen daraus ableiten.
        sure_in = [i for i, k in enumerate(main) if in_ev[k] and not out_ev[k]]
        sure_out = [i for i, k in enumerate(main) if out_ev[k] and not in_ev[k]]
        ordered = not sure_in or not sure_out or max(sure_in) < min(sure_out)
        last_in = max(sure_in) if sure_in else -1
        first_out = min(sure_out) if sure_out else len(main)

        connectors = []
        for i, key in enumerate(main):
            defs = Counter(b.connector(key).default for _, b in same if b.connector(key) is not None)
            defs.pop(None, None)
            basis = "beleg"
            if in_ev[key] and not out_ev[key]:
                direction = "eingang"
            elif out_ev[key] and not in_ev[key]:
                direction = "ausgang"
            elif in_ev[key] and out_ev[key]:
                direction, basis = ("eingang" if in_ev[key] >= out_ev[key] else "ausgang"), "geschaetzt"
            elif ordered and i < last_in:
                direction, basis = "eingang", "reihenfolge"
            elif ordered and i > first_out:
                direction, basis = "ausgang", "reihenfolge"
            else:
                direction, basis = ("ausgang" if OUT_GUESS_RE.match(key) else "eingang"), "geschaetzt"
            c: dict = {"name": key, "richtung": direction}
            if basis == "geschaetzt":
                c["richtung_unsicher"] = True
            if defs:
                c["ueblicher_wert"] = defs.most_common(1)[0][0]
                if len(defs) > 1:
                    c["weitere_werte"] = [v for v, _ in defs.most_common(5)][1:]
            connectors.append(c)
        usual = {c["name"]: c.get("ueblicher_wert") for c in connectors}

        attrs: dict[str, dict] = {}
        presence: Counter = Counter()
        values: dict[str, Counter] = defaultdict(Counter)
        for _, b in pool:
            for k, v in b.elem.attrib.items():
                if k in INSTANCE_ATTRS or k == "Type" or SECRET_RE.search(k):
                    continue
                presence[k] += 1
                values[k]["<uuid>" if UUID_RE.search(v) else v[:80]] += 1
        for k in sorted(presence):
            common = values[k].most_common(1)[0][0]
            a: dict = {"haeufigster_wert": common, "immer_vorhanden": presence[k] == len(pool)}
            if len(values[k]) > 1:
                a["verschiedene_werte"] = len(values[k])
            attrs[k] = a

        template_block = min((b for _, b in same), key=lambda b: (
            sum(len(c.sources) for c in b.connectors), len(b.elem.findall("C"))))
        template, foreign = self._template(template_block, usual)
        sub = Counter(c.get("Type", "?") for _, b in pool for c in b.elem.findall("C"))
        px = [(_int(b.elem.get("Px2")) - _int(b.elem.get("Px")), _int(b.elem.get("Py2")) - _int(b.elem.get("Py")))
              for _, b in same if b.elem.get("Px2")]

        out: dict = {
            "beispiele": len(ex),
            "beispiele_auf_seiten": sum(1 for _, b in ex if b.page is not None),
            "dateien": len({p.path for p, _ in ex}),
            "gelernt_aus": newest.path.name,
            "config_version": newest.config_version,
            "anschluesse": connectors,
            "eigenschaften": attrs,
            "vorlage_xml": template,
        }
        if len(shapes) > 1:
            out["anschluss_varianten"] = [
                {"anzahl": n, "anschluesse": list(keys)} for keys, n in shapes.most_common(4)]
        if px:
            out["groesse"] = {"breite": Counter(px).most_common(1)[0][0][0], "hoehe": Counter(px).most_common(1)[0][0][1]}
        if sub:
            out["unterobjekte"] = dict(sub.most_common(8))
        if foreign:
            out["verweise_auf_andere_objekte"] = foreign
        kids = sorted({c.tag for _, b in pool for c in b.elem if c.tag not in ("C", "Co")})
        if kids:
            out["weitere_elemente"] = kids
        return out

    @staticmethod
    def _template(b: Block, usual: dict[str, str | None]) -> tuple[str, list[str]]:
        """XML-Muster: UUIDs, Titel und Position durch Platzhalter ersetzt, Verbindungen entfernt,
        Anschlusswerte auf den üblichen Wert gesetzt."""
        el = copy.deepcopy(b.elem)
        foreign: set[str] = set()

        def clean(e: ET.Element, depth: int) -> None:
            if not (e.text and e.text.strip()):
                e.text = None
            e.tail = None
            if e.tag == "Co" and depth == 1:
                e.attrib.pop("Nc", None)  # Anzahl der Verbindungen, in der Vorlage keine
                if usual.get(e.get("K", "")) is not None and "Def" in e.attrib:
                    e.set("Def", usual[e.get("K", "")])
            for k in list(e.attrib):
                v = e.attrib[k]
                if SECRET_RE.search(k):
                    e.set(k, "")
                elif k == "U":
                    e.set(k, "{uuid}" if e.tag == "C" else "{uuid:%s}" % e.get("K", "?"))
                elif k == "Title" and depth == 0:
                    e.set(k, "{titel}")
                elif k in LAYOUT_ATTRS and depth == 0:
                    e.set(k, "{%s}" % k.lower())
                elif k == "LtE":
                    del e.attrib[k]
                elif UUID_RE.search(v):
                    foreign.add(f"{e.tag}.{k}")
                    e.set(k, "{verweis:%s}" % k)
                elif len(v) > 400:
                    e.set(k, v[:60] + "…")
            for child in list(e):
                if child.tag == "In":
                    e.remove(child)
                else:
                    clean(child, depth + 1)

        clean(el, 0)
        return ET.tostring(el, encoding="unicode"), sorted(foreign)

    # ---------- Abfragen ----------
    def list_types(self, query: str | None, include_peripherals: bool) -> dict:
        if not self.types:
            raise ProjectError("Der Katalog ist leer. Bitte zuerst catalog_learn aufrufen.")
        q = (query or "").lower()
        rows = []
        for name, t in self.types.items():
            if not include_peripherals and not t["beispiele_auf_seiten"]:
                continue
            if q and q not in name.lower():
                continue
            ins = sum(1 for c in t["anschluesse"] if c["richtung"] == "eingang")
            row = {"typ": name, "beispiele": t["beispiele"], "eingaenge": ins,
                   "ausgaenge": len(t["anschluesse"]) - ins, "config_version": t["config_version"]}
            if "anschluss_varianten" in t:
                row["varianten"] = len(t["anschluss_varianten"])
            rows.append(row)
        rows.sort(key=lambda r: -r["beispiele"])
        return {"erstellt": self.data.get("erstellt"), "quellen": [Path(p).name for p in self.data.get("quellen", [])],
                "anzahl": len(rows), "typen": rows}

    def find_type(self, type_: str) -> tuple[str, dict]:
        if not self.types:
            raise ProjectError("Der Katalog ist leer. Bitte zuerst catalog_learn aufrufen.")
        low = type_.strip().lower()
        for name, t in self.types.items():
            if name.lower() == low:
                return name, t
        part = [n for n in self.types if low in n.lower()]
        if len(part) == 1:
            return part[0], self.types[part[0]]
        hint = ", ".join(part[:15]) if part else "keine ähnlichen Typen"
        raise ProjectError(f"Typ '{type_}' ist nicht im Katalog. Ähnlich: {hint}. "
                           "Fehlende Typen einmal in Loxone Config anlegen und mit catalog_learn lernen.")

    def get_type(self, type_: str, full_template: bool) -> dict:
        name, t = self.find_type(type_)
        out = {"typ": name, **t}
        if not full_template and len(out["vorlage_xml"]) > MAX_TEMPLATE_SHOWN:
            out["vorlage_xml"] = out["vorlage_xml"][:MAX_TEMPLATE_SHOWN] + "… (gekürzt, full_template=true zeigt alles)"
        return out

    def deviations(self, b: Block) -> dict:
        """Anschlusswerte eines Bausteins, die vom üblichen Wert in den gelernten Dateien abweichen."""
        t = self.types.get(b.type)
        if not t:
            return {}
        std = {c["name"]: c.get("ueblicher_wert") for c in t["anschluesse"]}
        return {
            c.key: {"wert": c.default, "ueblich": std[c.key]}
            for c in b.connectors
            if c.default not in (None, "") and std.get(c.key) not in (None, c.default)
        }
