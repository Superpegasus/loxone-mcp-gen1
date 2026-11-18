"""Funktionsvorlagen (Stufe „4“): ganze Funktionen in einem geprüften Schritt.

Jede Vorlage arbeitet auf dem Arbeitsstand des Editors und nutzt nur dessen geprüfte
Operationen. Sie baut entweder alles oder nichts: Scheitert ein Schritt, werden alle
Schritte dieser Vorlage zurückgenommen.
"""
from __future__ import annotations

from editor import Editor
from loxproject import Block, ProjectError

# Reihenfolge = Vorrang bei mehrdeutigen Titeln (echter Eingang vor Merker/Anzeige gleichen Namens)
INPUTS = ["VirtualUdpInCmd", "VirtualIn", "DigitalIn", "LoxAIRsensor", "VirtualHttpInCmd", "Daylight", "Mode", "Memory"]
OUTPUTS = ["VirtualOutCmd", "Actor", "TreeActor", "LoxAIRactor", "Lox232actor", "ApiActor"]

RECIPES = {
    "fensterkontakt_rollladen": {
        "beschreibung": "Schließt einen Tür- oder Fensterkontakt an den Eingang 'Window' eines Rollladens an. Laut Bausteinverlauf "
                        "fährt der Rollladen beim Öffnen in eine Position und wird gesperrt. Sinnvoll vor allem an "
                        "Terrassen- und Balkontüren (Aussperrschutz), an normalen Fenstern meist nicht.",
        "parameter": {"rollladen": "Rollladen-Baustein (#Kennung oder eindeutiger Titel)",
                      "kontakt": "Fensterkontakt (#Kennung oder Titel des Eingangs)"},
    },
    "licht_aus_bei_abwesenheit": {
        "beschreibung": "Legt ein Abwesenheitssignal an den Eingang 'Reset' (aus) von Lichtsteuerungen.",
        "parameter": {"signal": "Signal, Standard 'Abwesen'",
                      "lichter": "Liste von Lichtsteuerungen (Titel/#Kennung); leer = alle Lichtsteuerungen"},
    },
    "licht_aus_nach_zeit": {
        "beschreibung": "Schaltet eine Lichtsteuerung nach einer Zeit aus (Einschaltverzögerung zwischen Lichtkreis und 'Reset'), "
                        "optional nur, wenn zusätzlich eine Bedingung erfüllt ist (z. B. Tag-Schwelle).",
        "parameter": {"licht": "Lichtsteuerung", "minuten": "Zeit bis zum Ausschalten", "kreis": "Lichtkreis, Standard 1",
                      "nur_wenn": "optional: digitales Signal, das zusätzlich anstehen muss (Titel/#Kennung)"},
    },
    "signal_ersetzen": {
        "beschreibung": "Hängt alle Verwendungen eines Eingangs auf allen Seiten auf einen anderen Eingang um "
                        "(z. B. weatherapi-Wert -> DWD-Wert aus Home Assistant). Alte Platzhalter werden entfernt; "
                        "der alte Eingang selbst bleibt und kann danach mit periphery_remove gelöscht werden.",
        "parameter": {"alt": "bisheriger Eingang (Titel/#Kennung)", "neu": "neuer Eingang (Titel/#Kennung)"},
    },
    "meldung_wenn_offen": {
        "beschreibung": "Löst einen vorhandenen Ausgang (z. B. Push über Home Assistant) aus, wenn ein Kontakt länger als X Minuten offen ist.",
        "parameter": {"kontakt": "Kontakt (Titel/#Kennung)", "minuten": "Dauer", "ausgang": "vorhandener Ausgang, z. B. virtueller Ausgangsbefehl",
                      "seite": "Programmseite, auf der gebaut wird", "kontakt_invertiert": "true, wenn der Kontakt bei offenem Fenster 0 liefert"},
    },
}


class Recipes:
    def __init__(self, editor: Editor):
        self.ed = editor

    # ---------- Hilfen ----------
    def _atomic(self, name: str, fn) -> dict:
        start = len(self.ed.log)
        try:
            result = fn()
        except Exception as e:
            while len(self.ed.log) > start:
                self.ed.undo()
            raise ProjectError(f"Vorlage '{name}' nicht angewendet, alles zurückgenommen: {e}") from e
        result["schritte"] = self.ed.log[start:]
        result["hinweis"] = "Im Arbeitsstand. Mit project_changes prüfen, mit project_save als neue Datei speichern."
        return result

    def _block(self, spec: str, typ: str | None = None, prefer: list | None = None) -> Block:
        """Löst eine Angabe auf. Bei mehrdeutigen Titeln entscheidet der erwartete Typ,
        bei mehreren erwarteten Typen deren Reihenfolge."""
        pr = self.ed.project
        try:
            b = pr.logical(pr.resolve(spec, None))
        except ProjectError:
            cands = {x.uuid: x for x in (pr.logical(y) for y in pr.blocks.values() if y.title == spec.strip())}
            b = None
            for t in ([typ] if typ else (prefer or [])):
                hits = [x for x in cands.values() if x.type == t]
                if len(hits) == 1:
                    b = hits[0]
                    break
                if len(hits) > 1:
                    raise
            if b is None:
                raise
        if typ and b.type != typ:
            raise ProjectError(f"'{pr.name(b)}' ist kein {typ}.")
        return b

    def _input_on_page(self, target: Block, page: Block) -> str:
        """Kennung eines Eingangs-Platzhalters für target auf page; legt ihn bei Bedarf an."""
        pr = self.ed.project
        if target.page is page:
            return "#" + pr.short(target)
        for r in pr.blocks.values():
            if r.page is page and pr.is_ref(r) and pr.target(r) is target and r.type == "InputRef":
                return "#" + pr.short(r)
        self.ed.add_ref(page.title, "#" + pr.short(target), "input", None, None)
        pr = self.ed.project
        refs = [r for r in pr.blocks.values() if r.page and r.page.uuid == page.uuid and pr.is_ref(r)
                and pr.target(r).uuid == target.uuid and r.type == "InputRef"]
        return "#" + pr.short(refs[-1])

    def _output_on_page(self, target: Block, page: Block) -> str:
        pr = self.ed.project
        for r in pr.blocks.values():
            if r.page is page and pr.is_ref(r) and pr.target(r) is target and r.type == "OutputRef":
                return "#" + pr.short(r)
        self.ed.add_ref(page.title, "#" + pr.short(target), "output", None, None)
        pr = self.ed.project
        refs = [r for r in pr.blocks.values() if r.page and r.page.uuid == page.uuid and pr.is_ref(r)
                and pr.target(r).uuid == target.uuid and r.type == "OutputRef"]
        return "#" + pr.short(refs[-1])

    # ---------- Vorlagen ----------
    def apply(self, name: str, params: dict) -> dict:
        if name not in RECIPES:
            raise ProjectError(f"Unbekannte Vorlage '{name}'. Vorhanden: {', '.join(RECIPES)}")
        return self._atomic(name, lambda: getattr(self, name)(**(params or {})))

    def fensterkontakt_rollladen(self, rollladen: str, kontakt: str) -> dict:
        j = self._block(rollladen, "AutoJalousie")
        k = self._block(kontakt, prefer=INPUTS)
        w = j.connector("Window")
        if w is None:
            raise ProjectError("Der Rollladen hat keinen Eingang 'Window'.")
        if w.sources:
            raise ProjectError(f"Der Eingang 'Window' von '{j.title}' ist bereits belegt.")
        src = self._input_on_page(k, j.page)
        self.ed.connect(src, None, "#" + self.ed.project.short(j), "Window", j.page.title)
        return {"rollladen": j.title, "seite": j.page.title, "kontakt": k.title}

    def licht_aus_bei_abwesenheit(self, signal: str = "Abwesen", lichter: list | None = None) -> dict:
        pr = self.ed.project
        sig = self._block(signal, prefer=INPUTS)
        if lichter:
            lcs = [self._block(s, "LightController2") for s in lichter]
        else:
            lcs = [b for b in pr.blocks.values() if b.type == "LightController2" and b.page is not None]
        done, skipped = [], []
        for lc in lcs:
            pr = self.ed.project
            lc = pr.blocks[lc.uuid]
            reset = lc.connector("Reset")
            if any(pr.logical(pr.co_index[s][0]).uuid == sig.uuid for s, _ in reset.sources if s in pr.co_index):
                skipped.append(lc.title)
                continue
            src = self._input_on_page(sig, lc.page)
            self.ed.connect(src, None, "#" + self.ed.project.short(lc), "Reset", lc.page.title)
            done.append(f"{lc.title} (Seite {lc.page.title})")
        if not done:
            raise ProjectError("Alle gewählten Lichtsteuerungen hängen schon an diesem Signal.")
        return {"signal": sig.title, "angeschlossen": done, "schon_vorhanden": skipped}

    def licht_aus_nach_zeit(self, licht: str, minuten: float, kreis: int = 1, nur_wenn: str | None = None) -> dict:
        lc = self._block(licht, "LightController2")
        if not lc.connector(f"AQ{int(kreis)}"):
            raise ProjectError(f"Lichtkreis {kreis} gibt es an '{lc.title}' nicht.")
        if float(minuten) <= 0:
            raise ProjectError("minuten muss größer als 0 sein.")
        page = lc.page.title
        lc_id = "#" + self.ed.project.short(lc)
        title = f"{lc.title} aus nach {int(minuten) if float(minuten).is_integer() else minuten} min"
        r = self.ed.add_block(page, "OnDelay", title, None, None, {"Time": int(float(minuten) * 60)})
        delay = r["angelegt"].split("#")[-1]
        if nur_wenn:
            cond = self._block(nur_wenn, prefer=INPUTS)
            g = self.ed.add_block(page, "And", f"{lc.title} an und Bedingung", None, None, None)
            gate = g["angelegt"].split("#")[-1]
            self.ed.connect(lc_id, f"AQ{int(kreis)}", "#" + gate, "I1", page)
            self.ed.connect(self._input_on_page(cond, lc.page), None, "#" + gate, "I2", page)
            self.ed.connect("#" + gate, "Q", "#" + delay, "InputTrigger", page)
        else:
            self.ed.connect(lc_id, f"AQ{int(kreis)}", "#" + delay, "InputTrigger", page)
        self.ed.connect("#" + delay, "Q", lc_id, "Reset", page)
        return {"licht": lc.title, "seite": page, "zeit_min": minuten, "bedingung": nur_wenn}

    def signal_ersetzen(self, alt: str, neu: str) -> dict:
        old = self._block(alt, prefer=INPUTS + ["VirtualHttpInCmd"])
        new = self._block(neu, prefer=INPUTS)
        if old.uuid == new.uuid:
            raise ProjectError("Alter und neuer Eingang sind identisch.")
        pr = self.ed.project
        refs = [r for r in pr.blocks.values() if pr.target(r) is old and r.type == "InputRef" and r.page is not None]
        if old.page is not None:
            raise ProjectError("Der alte Eingang liegt selbst auf einer Seite; das wird nicht unterstützt.")
        moved = []
        for r0 in refs:
            pr = self.ed.project
            r = pr.blocks[r0.uuid]
            page = r.page
            edges = list(pr.out_edges.get(r.uuid, []))
            if edges:
                src = self._input_on_page(new, page)
                for e in edges:
                    pr = self.ed.project
                    dst = pr.blocks[e.dst.uuid]
                    self.ed.disconnect("#" + pr.short(r), e.src_key, "#" + pr.short(dst), e.dst_key, page.title)
                    self.ed.connect(src, None, "#" + self.ed.project.short(dst), e.dst_key, page.title)
                    moved.append(f"{dst.title}.{e.dst_key} (Seite {page.title})")
            self.ed.remove_block("#" + self.ed.project.short(self.ed.project.blocks[r.uuid]), page.title)
        if not refs:
            raise ProjectError(f"'{old.title}' wird auf keiner Seite verwendet.")
        return {"alt": old.title, "neu": new.title, "umgehaengt": moved, "entfernte_platzhalter": len(refs)}

    def meldung_wenn_offen(self, kontakt: str, minuten: float, ausgang: str, seite: str,
                           kontakt_invertiert: bool = False) -> dict:
        pr = self.ed.project
        pg = pr.find_page(seite)
        k = self._block(kontakt, prefer=INPUTS)
        out = self._block(ausgang, prefer=OUTPUTS)
        if float(minuten) <= 0:
            raise ProjectError("minuten muss größer als 0 sein.")
        r = self.ed.add_block(pg.title, "OnDelay", f"{k.title} {int(float(minuten))} min offen", None, None,
                              {"Time": int(float(minuten) * 60)})
        delay = "#" + r["angelegt"].split("#")[-1]
        self.ed.connect(self._input_on_page(k, pg), None, delay, "InputTrigger", pg.title)
        if kontakt_invertiert:
            self.ed.set_inverted(delay, "InputTrigger", True, pg.title)
        self.ed.connect(delay, "Q", self._output_on_page(out, pg), None, pg.title)
        return {"kontakt": k.title, "ausgang": out.title, "seite": pg.title, "zeit_min": minuten}
