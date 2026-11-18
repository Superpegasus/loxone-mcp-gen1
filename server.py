"""MCP-Server: Loxone-Config-Projektdateien lesen, verstehen und in Kopien bearbeiten.

Lesen (project_*), Baustein-Katalog (catalog_*), Bearbeiten (block_*, ref_add).
Bearbeitet wird ein Arbeitsstand im Speicher; gespeichert wird nur in eine neue Datei.

Start:  uv run server.py
Ordner mit den Projektdateien: Umgebungsvariable LOXONE_PROJECTS_DIR,
sonst Dokumente\\Loxone\\Loxone Config\\Projects.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from analyze import analyze, summarize
from catalog import Catalog
from editor import Editor
from live import Miniserver
from loxproject import Project, ProjectError
from recipes import RECIPES, Recipes
from sync import autosave_report, sync_report

mcp = MCPServer("loxone-projekt")

_project: Project | None = None
_catalog: Catalog | None = None
_edit: Editor | None = None  # Arbeitsstand mit ungespeicherten Änderungen


def _editor() -> Editor:
    """Arbeitsstand; wird beim ersten Schreibzugriff aus dem geöffneten Projekt angelegt."""
    global _edit
    if _edit is None:
        if not _cat().types:
            raise ProjectError("Der Katalog ist leer. Bitte zuerst catalog_learn aufrufen.")
        _edit = Editor(_current().path, _cat())
    return _edit


def _cat() -> Catalog:
    global _catalog
    if _catalog is None:
        path = os.environ.get("LOXONE_CATALOG") or Path(__file__).parent / "catalog.json"
        _catalog = Catalog(path)
    return _catalog


def _projects_dir() -> Path:
    env = os.environ.get("LOXONE_PROJECTS_DIR")
    if env:
        return Path(env)
    return Path.home() / "Documents" / "Loxone" / "Loxone Config" / "Projects"


def _project_files() -> list[Path]:
    d = _projects_dir()
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".loxone"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


SETTINGS = Path(__file__).parent / "settings.json"


def _settings() -> dict:
    import json
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _main_files() -> list[Path]:
    """Dateien des Hauptprojekts (Muster aus settings.json, z. B. 'my_home_v*.Loxone'),
    neueste zuerst; ohne Muster alle Projektdateien."""
    import fnmatch
    pat = _settings().get("hauptprojekt_muster")
    files = _project_files()
    if pat:
        files = [p for p in files if fnmatch.fnmatch(p.name.lower(), pat.lower())]
    return files


def _current() -> Project:
    """Geöffnetes Projekt; lädt neu, wenn die Datei inzwischen gespeichert wurde."""
    global _project
    if _edit is not None:
        return _edit.project  # Lesewerkzeuge zeigen den Arbeitsstand samt Änderungen
    if _project is None:
        files = _main_files()
        if not files:
            raise ProjectError(
                f"Kein Projekt geöffnet und keine .Loxone-Datei in {_projects_dir()} gefunden. "
                "Bitte project_open mit einem Pfad aufrufen."
            )
        _project = Project(files[0])
    elif _project.path.is_file() and _project.path.stat().st_mtime != _project.mtime:
        _project = Project(_project.path)
    return _project


def _safe(fn):
    try:
        return fn()
    except ProjectError as e:
        return {"fehler": str(e)}


@mcp.tool()
def project_list_files() -> dict:
    """Listet die Loxone-Projektdateien (.Loxone) im Projektordner, neueste zuerst."""
    files = _project_files()
    return {
        "ordner": str(_projects_dir()),
        "geoeffnet": str(_project.path) if _project else None,
        "dateien": [
            {
                "name": p.name,
                "pfad": str(p),
                "kb": round(p.stat().st_size / 1024),
                "geaendert": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
            for p in files
        ],
    }


@mcp.tool()
def project_set_main(pattern: str | None = None) -> dict:
    """Legt fest, welche Dateien zum Hauptprojekt gehören (Muster mit *, z. B.
    'my_home_v*.Loxone'). project_open ohne Pfad nimmt dann die neueste passende Datei,
    nicht irgendeine zuletzt geänderte. Ohne pattern: aktuelle Einstellung anzeigen."""
    import json
    def run():
        s = _settings()
        if pattern:
            s["hauptprojekt_muster"] = pattern
            SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        files = _main_files()
        return {"hauptprojekt_muster": s.get("hauptprojekt_muster"),
                "neueste_datei": files[0].name if files else None, "anzahl_dateien": len(files)}
    return _safe(run)


@mcp.tool()
def project_open(path: str | None = None) -> dict:
    """Öffnet eine Loxone-Projektdatei zum Lesen und gibt eine Übersicht zurück
    (Titel, Version, Seiten, häufigste Bausteintypen). Ohne Pfad wird die neueste Datei
    des Hauptprojekts genommen (Muster siehe project_set_main). Ein Dateiname ohne Ordner wird im
    Projektordner gesucht. Die Datei wird nie verändert."""
    def run():
        global _project, _edit
        if _edit is not None and _edit.log:
            raise ProjectError(f"Es gibt {len(_edit.log)} ungespeicherte Änderungen. "
                               "Bitte zuerst project_save oder project_discard aufrufen.")
        _edit = None
        if path:
            p = Path(path)
            if not p.is_absolute():
                p = _projects_dir() / path
            _project = Project(p)
        else:
            _project = None
        pr = _current()
        out = pr.summary()
        import loadcheck
        out["abgleich"] = loadcheck.open_check(pr)  # Miniserver-Sicherung + automatische Sicherung
        return out
    return _safe(run)


@mcp.tool()
def verify_load(file: str | None = None, cleanup: bool = False) -> dict:
    """Nach dem Laden in Config aufrufen: wartet, bis der Miniserver wieder antwortet (bis 3 min),
    sichert das laufende Programm (Git) und vergleicht es mit der erwarteten Projektdatei (Standard:
    neueste Datei des Hauptprojekts). cleanup=True legt bei Erfolg ältere Versionen des Hauptprojekts
    in den Papierkorb."""
    import loadcheck
    def run():
        files = _main_files()
        if file:
            p = Path(file)
            exp = p if p.is_absolute() else _projects_dir() / file
        elif files:
            exp = files[0]
        else:
            raise ProjectError("Keine Datei des Hauptprojekts gefunden.")
        return loadcheck.verify_load(exp, files, cleanup)
    return _safe(run)


@mcp.tool()
def project_list_pages() -> dict:
    """Listet alle Programmseiten des geöffneten Projekts mit Raum, Kategorie und Bausteinanzahl."""
    def run():
        pr = _current()
        return {
            "datei": pr.path.name,
            "seiten": [
                {
                    "titel": p.title,
                    "bausteine": len(p.elem.findall("C")),
                    "raum": pr.room(p),
                    "kategorie": pr.category(p),
                }
                for p in pr.pages
            ],
        }
    return _safe(run)


@mcp.tool()
def project_get_page(page: str, include_unconnected: bool = False) -> dict:
    """Zeigt eine Programmseite: alle Bausteine mit Parametern und Verbindungen.
    'eingaenge' nennt je Eingang die Quelle(n) oder einen festen Wert ('= 900'),
    'ausgaenge' nennt je Ausgang die Ziele. Schreibweise eines Anschlusses:
    'Titel [Typ] #Kennung.Anschluss'. Mit include_unconnected werden auch
    unbelegte Anschlüsse aufgeführt."""
    def run():
        pr = _current()
        return pr.page_view(pr.find_page(page), include_unconnected)
    return _safe(run)


@mcp.tool()
def project_find(query: str | None = None, type: str | None = None, room: str | None = None,
                 category: str | None = None, page: str | None = None, limit: int = 50) -> dict:
    """Sucht Bausteine und Geräte. Alle Filter sind optional und werden kombiniert:
    query (Teil von Titel, Beschreibung oder UUID), type (Bausteintyp, z. B.
    LightController2, OnDelay, Memory), room, category, page."""
    def run():
        return _current().find(query, type, room, category, page, max(1, min(limit, 300)))
    return _safe(run)


@mcp.tool()
def project_get_block(block: str, page: str | None = None) -> dict:
    """Zeigt einen Baustein oder ein Gerät im Detail: Parameter, alle Anschlüsse,
    Verbindungen und auf welchen Seiten er verwendet wird. 'block' ist eine
    #Kennung, eine UUID oder ein eindeutiger Titel (bei Mehrdeutigkeit 'page' angeben)."""
    def run():
        pr = _current()
        b = pr.logical(pr.resolve(block, page))
        view = pr.block_view(b)
        dev = _cat().deviations(b)
        if dev:
            view["abweichend_vom_ueblichen_wert"] = dev
        return view
    return _safe(run)


@mcp.tool()
def project_trace(block: str, connector: str | None = None, direction: str = "forward",
                  max_depth: int = 4, page: str | None = None) -> dict:
    """Verfolgt ein Signal durch die Verbindungen, auch über Seiten hinweg.
    direction 'forward' zeigt, worauf der Baustein wirkt; 'backward' zeigt, wovon
    er abhängt. 'connector' beschränkt den Start auf einen Anschluss (z. B. Q oder I1)."""
    def run():
        pr = _current()
        if direction not in ("forward", "backward"):
            raise ProjectError("direction muss 'forward' oder 'backward' sein.")
        return pr.trace(pr.resolve(block, page), connector, direction, max(1, min(max_depth, 12)))
    return _safe(run)


@mcp.tool()
def catalog_learn(paths: list[str] | None = None, reset: bool = False) -> dict:
    """Lernt Bausteintypen aus Projektdateien und speichert sie im Katalog.
    Ohne 'paths' werden alle .Loxone-Dateien im Projektordner gelernt. Bereits
    gelernte Dateien bleiben Quelle; reset=true beginnt von vorn. Fehlt ein
    Bausteintyp, einmal in Loxone Config anlegen, speichern und erneut lernen.
    Projektdateien werden nur gelesen."""
    def run():
        if paths:
            files = [Path(p) if Path(p).is_absolute() else _projects_dir() / p for p in paths]
        else:
            files = _project_files()
        return _cat().learn(files, reset)
    return _safe(run)


@mcp.tool()
def catalog_list_types(query: str | None = None, include_peripherals: bool = False) -> dict:
    """Listet die Bausteintypen im Katalog mit Anzahl der Beispiele sowie Ein- und
    Ausgängen. Standardmäßig nur Typen, die auf Programmseiten vorkommen;
    include_peripherals=true zeigt auch Geräte, Räume und Verwaltungsobjekte."""
    return _safe(lambda: _cat().list_types(query, include_peripherals))


@mcp.tool()
def catalog_get_type(type: str, full_template: bool = False) -> dict:
    """Zeigt einen Bausteintyp aus dem Katalog: Anschlüsse mit Richtung und
    üblichem Wert (häufigster Wert in den gelernten Dateien, nicht der Werkswert), Eigenschaften, Größe und XML-Vorlage mit Platzhaltern
    ({uuid}, {uuid:Anschluss}, {titel}, {px} ...). 'richtung_unsicher' heißt:
    in keinem Beispiel war der Anschluss verbunden, die Richtung ist geschätzt."""
    return _safe(lambda: _cat().get_type(type, full_template))


@mcp.tool()
def block_add(page: str, type: str, title: str, x: int | None = None, y: int | None = None,
              params: dict | None = None) -> dict:
    """Legt einen neuen Baustein auf einer Seite an (im Arbeitsstand, nicht in der Datei).
    'type' ist ein Typ aus dem Katalog, z. B. And, Or, Not, OnDelay, Memory, Monoflop.
    'params' setzt Eingangswerte, z. B. {"Time": 300}, oder Eigenschaften des Typs
    (bei Memory vermutlich Tp: 0 = digital, 1 = analog). Ohne x/y wird ein freier Platz
    unter den vorhandenen Bausteinen gewählt. Typen mit Verweisen auf andere Objekte
    (Lichtsteuerung, Raumregler, Jalousie ...) werden abgelehnt; die legt man in
    Loxone Config an."""
    return _safe(lambda: _editor().add_block(page, type, title, x, y, params))


@mcp.tool()
def ref_add(page: str, target: str, kind: str | None = None, x: int | None = None, y: int | None = None) -> dict:
    """Holt ein vorhandenes Objekt (Eingang, Sensor, Merker, Konstante, Aktor, virtueller
    Ausgang) als Platzhalter auf eine Seite, damit es dort verdrahtet werden kann.
    kind 'input' liefert das Signal auf die Seite, 'output' nimmt ein Signal entgegen;
    ohne Angabe wird es aus dem Typ abgeleitet. 'target' ist #Kennung oder eindeutiger Titel."""
    return _safe(lambda: _editor().add_ref(page, target, kind, x, y))


@mcp.tool()
def block_connect(from_block: str, to_block: str, to_connector: str | None = None,
                  from_connector: str | None = None, page: str | None = None) -> dict:
    """Verbindet einen Ausgang mit einem Eingang auf derselben Seite. Bausteine per
    #Kennung oder eindeutigem Titel; für Geräte und Merker anderer Seiten 'page'
    angeben, dann wird der Platzhalter auf dieser Seite benutzt. from_connector darf
    fehlen, wenn die Quelle nur einen Ausgang hat oder ein Platzhalter ist;
    to_connector darf fehlen, wenn das Ziel ein Ausgangs-Platzhalter ist."""
    return _safe(lambda: _editor().connect(from_block, from_connector, to_block, to_connector, page))


@mcp.tool()
def block_disconnect(from_block: str, to_block: str, to_connector: str | None = None,
                     from_connector: str | None = None, page: str | None = None) -> dict:
    """Löst eine bestehende Verbindung. Angaben wie bei block_connect."""
    return _safe(lambda: _editor().disconnect(from_block, from_connector, to_block, to_connector, page))


@mcp.tool()
def block_set_params(block: str, params: dict, page: str | None = None) -> dict:
    """Ändert Werte eines Bausteins: Eingangswerte per Anschlussname (z. B. {"Time": 600}),
    den Titel per {"Titel": "..."} oder vorhandene Eigenschaften per Attributname.
    Ein leerer Wert entfernt einen festen Eingangswert."""
    return _safe(lambda: _editor().set_params(block, params, page))


@mcp.tool()
def block_set_inverted(block: str, connector: str, inverted: bool = True, page: str | None = None) -> dict:
    """Negiert einen Eingang eines Bausteins oder hebt die Negierung auf. Ein freier,
    negierter Eingang ist dauerhaft Ein; so schaltet man z. B. eine Sperre fest ein."""
    return _safe(lambda: _editor().set_inverted(block, connector, inverted, page))


@mcp.tool()
def io_add_virtual_input(title: str, analog: bool = True, min_val: float | None = None, max_val: float | None = None,
                         room: str | None = None, category: str | None = None, visu: bool = False,
                         unit: str | None = None, text: bool = False, desc: str | None = None) -> dict:
    """Legt einen virtuellen Eingang unter 'Virtuelle Eingänge' an. Werte setzt man per HTTP
    (/dev/sps/io/<Titel>/<Wert>), z. B. aus Home Assistant. visu=True zeigt ihn in der App,
    unit ist die Anzeige, z. B. '<v.1>°' oder '<v>%'. text=True legt einen Text-Eingang an
    (Wert url-kodiert senden; ACHTUNG: /state auf einen Text-Eingang überschreibt ihn).
    desc ist der Name in der App. Systemvariablen speist man NICHT über 'source', sondern über
    einen Ausgangs-Platzhalter der Systemvariable (ref_add ... 'output')."""
    return _safe(lambda: _editor().add_virtual_input(title, analog, min_val, max_val, room, category, visu, unit,
                                                     text, desc))


@mcp.tool()
def build_plan(steps: list[dict], save_as: str | None = None) -> dict:
    """Führt einen ganzen Bauplan in einem Aufruf aus – alles oder nichts. Jeder Schritt ist ein dict
    mit 'op' und den Angaben, z. B.
      {"op":"baustein","typ":"And","titel":"Regen und offen","seite":"Zentral 2","als":"und"}
      {"op":"verbinden","von":"ha_regen","nach":"$und","nach_eingang":"I2","seite":"Zentral 2"}
      {"op":"befehl","titel":"Push X","ein":"/api/webhook/<id>?msg=X","quelle":"$und","seite":"Zentral 2"}
    Ops: baustein, kopie, status, eingang, befehl, rs232, platzhalter, verbinden, werte, negieren,
    anzeige, statistik, position, loeschen (Details: plan.py). 'als' benennt Neues, '$name' verweist
    darauf. Fehlende Platzhalter legt 'verbinden' selbst an. Danach wird geprüft; mit save_as
    (neuer Dateiname) wird bei fehlerfreier Prüfung gleich gespeichert."""
    import plan
    def run():
        ed = _editor()
        res = plan.run_plan(ed, steps)
        res["pruefung"] = ed.validate()
        if save_as:
            if not res["pruefung"]["in_ordnung"]:
                raise ProjectError("Nicht gespeichert, die Prüfung meldet Fehler: " + " | ".join(res["pruefung"]["fehler"]))
            res["gespeichert"] = project_save(save_as)
        if len(str(res)) > 20000:  # Antwort kurz halten
            res["ergebnisse"] = [{"schritt": e["schritt"], "op": e["op"]} for e in res["ergebnisse"]]
        return res
    return _safe(run)


@mcp.tool()
def project_text(page: str | None = None) -> dict:
    """Lesbare Textansicht des Programms (bzw. Arbeitsstands): je Seite die Bausteine mit ihren
    Einstellungen und Eingängen („I1 <- Quelle.Ausgang“) sowie die Ausgänge in die Peripherie.
    Ohne page: ganzes Projekt (lang!)."""
    import textview
    def run():
        t = textview.render(_current(), page)
        if len(t) > 60000:
            t = t[:60000] + "\n… (gekürzt – bitte eine Seite angeben)"
        return {"text": t}
    return _safe(run)


@mcp.tool()
def backup_miniserver() -> dict:
    """Sichert das aktuell im Miniserver laufende Programm (neueste Datei aus /prog per FTP, nur
    Download) als XML in das lokale Git-Repository Dokumente\\Loxone\\Miniserver-Backup und zeigt,
    was sich seit der letzten Sicherung geändert hat. Nach jedem Laden aufrufen; ist nichts neu,
    wird nichts gespeichert."""
    import backup
    return _safe(backup.backup)


@mcp.tool()
def project_lint() -> dict:
    """Prüfung vor dem Laden über das ganze Projekt (bzw. den Arbeitsstand): Fehler, die Config still
    verwirft (falsche Platzhalter-Ausgänge, Verbindungen ins Leere, Platzhalter ohne Ziel), sowie
    Bausteine außerhalb des Blattes, Überlappungen, Titel mit < > %, Platzhalter ohne Verbindung."""
    from analyze import analyze as _an, lint_layout
    def run():
        pr = _current()
        found = [f for f in _an(pr) if f["stufe"] == "fehler"] + lint_layout(pr)
        seen, uniq = set(), []
        for f in found:
            k = (f["art"], f["ort"], f["befund"])
            if k not in seen:
                seen.add(k)
                uniq.append(f)
        from collections import Counter as _C
        return {"anzahl": len(uniq), "je_art": dict(_C(f["art"] for f in uniq)), "befunde": uniq[:60],
                "hinweis": "Überlappungen in Bestandsseiten können Absicht sein; neue Bausteine sollten keine haben."}
    return _safe(run)


@mcp.tool()
def block_set_position(block: str, x: int, y: int, page: str | None = None) -> dict:
    """Verschiebt einen Baustein oder Platzhalter auf seiner Seite (x/y in Loxone-Einheiten, Raster 96;
    das Blatt reicht bis ca. 23 400 x 16 300)."""
    return _safe(lambda: _editor().set_position(block, x, y, page))


@mcp.tool()
def block_clone(block: str, page: str, title: str, source_page: str | None = None) -> dict:
    """Legt eine Kopie eines vorhandenen Bausteins mit allen Einstellungen und Unterobjekten auf einer
    Seite an, ohne seine Verbindungen – so entstehen auch große Bausteine wie Lichtsteuerung,
    Raumregler oder Automatikjalousie, die block_add nicht anlegen kann. Danach verbinden
    (block_connect) und Einstellungen prüfen."""
    return _safe(lambda: _editor().clone_block(block, page, title, source_page))


@mcp.tool()
def io_set_statistics(block: str, enabled: bool = True) -> dict:
    """Schaltet die Statistik eines analogen Eingangs oder Merkers ein (bei jeder Änderung,
    aufsummiert – wie Loxone Config es für Messwerte anlegt) oder aus."""
    return _safe(lambda: _editor().set_statistics(block, "jede_aenderung" if enabled else "aus"))


@mcp.tool()
def icon_find(query: str) -> dict:
    """Sucht Symbole der Loxone-Symbolbibliothek nach Name oder Stichwort (deutsch),
    z. B. 'sonne', 'regen', 'thermometer'. Ergebnis: Name und UUID für state_add."""
    import icons
    return _safe(lambda: {"treffer": icons.find(query)} if icons.library() else
                 {"fehler": "Symbolbibliothek nicht gefunden (ProgramData\\Loxone\\Loxone Config *\\IconLibrary.zip)."})


@mcp.tool()
def state_add(page: str, title: str, source: str, states: list[dict], compare: str = "gleich",
              default_text: str = "", default_icon: str | None = None, default_color: str | None = None,
              room: str | None = None, category: str | None = None, visu: bool = True) -> dict:
    """Legt einen Status-Baustein an, der in der App je Zustand eigenen Text, Symbol und Farbe zeigt,
    und schließt 'source' an I1 an (Platzhalter wird bei Bedarf angelegt).
    states: [{"wert": 1, "text": "Sonnig", "symbol": "sun", "farbe": "#ffc800"}, ...].
    compare='gleich': Zustand gilt, wenn I1 gleich wert. compare='ab': I1 >= wert, von oben nach unten
    geprüft (größte Schwelle zuerst). Text darf <v1> oder <v1.1> enthalten (Wert von I1).
    default_* gilt, wenn kein Zustand passt. Symbole mit icon_find suchen. Raum/Kategorie ohne Angabe
    wie bei 'source'."""
    return _safe(lambda: _editor().add_state(page, title, source, states, compare, default_text, default_icon,
                                             default_color, None, room, category, visu))


@mcp.tool()
def io_add_command(title: str, cmd_on: str, cmd_off: str | None = None, parent: str = "Home Assistant",
                   source: str | None = None, page: str | None = None, source_key: str | None = None) -> dict:
    """Legt einen virtuellen Ausgangsbefehl unter einem vorhandenen virtuellen Ausgang an (Standard:
    'Home Assistant'), z. B. Push: cmd_on='/api/webhook/<your-webhook-id>?msg=Text%20hier'.
    Mit source und page wird er dort gleich angeschlossen (Ausgangs-Platzhalter)."""
    return _safe(lambda: _editor().add_command(title, cmd_on, cmd_off, parent, source, page, source_key))


@mcp.tool()
def rs232_add(kind: str, title: str, code: str, device: str | None = None, code_off: str | None = None,
              analog: bool | None = None) -> dict:
    """Legt unter einer RS232-Extension (z. B. 'Zehnder ComfoAir 350') einen Befehl (kind='befehl',
    code = Befehl wie '\\x07\\xF0\\x00\\xD3\\x01\\x52\\xD3\\x07\\x0F') oder einen Sensor (kind='sensor',
    code = Prüfmuster wie '\\x07\\xF0\\x00\\xDE\\x14\\s3\\1') nach dem Muster vorhandener Objekte an.
    Auf Seiten anschließen danach mit ref_add/block_connect."""
    return _safe(lambda: _editor().add_rs232(kind, title, code, device, code_off, analog))


@mcp.tool()
def io_set_display(block: str, visible: bool | None = None, unit: str | None = None) -> dict:
    """Zeigt einen Eingang, Merker oder eine Konstante in der App an oder blendet ihn aus und setzt
    die Anzeigeeinheit, z. B. '<v.1>°', '<v>%', '<v.1>'."""
    return _safe(lambda: _editor().set_display(block, visible, unit))


@mcp.tool()
def periphery_remove(block: str) -> dict:
    """Löscht einen ungenutzten virtuellen Ein-/Ausgang, eine Konstante oder den Wetterserver.
    Verweigert, solange das Objekt noch irgendwo verwendet wird; Berechtigungseinträge darauf
    werden mit entfernt."""
    return _safe(lambda: _editor().remove_periphery(block))


@mcp.tool()
def structure_list(kind: str) -> dict:
    """Listet alle Räume (kind='raum') oder Kategorien (kind='kategorie') mit der Anzahl
    ihrer Zuordnungen. Einträge mit 0 Zuordnungen sind leer."""
    return _safe(lambda: _editor().list_structure(kind))


@mcp.tool()
def structure_remove(kind: str, name: str) -> dict:
    """Löscht einen leeren Raum oder eine leere Kategorie samt Berechtigungsgruppe und
    Berechtigungseinträgen. Verweigert, wenn noch etwas zugeordnet ist oder die Kennung
    an unbekannter Stelle vorkommt. Bei doppelten Namen die #Kennung angeben."""
    return _safe(lambda: _editor().remove_structure(kind, name))


@mcp.tool()
def structure_reassign(kind: str, source: str, target: str) -> dict:
    """Hängt alle Zuordnungen eines Raums oder einer Kategorie auf einen anderen um,
    z. B. um zwei gleichartige Kategorien zusammenzulegen."""
    return _safe(lambda: _editor().reassign_structure(kind, source, target))


@mcp.tool()
def block_remove(block: str, page: str | None = None) -> dict:
    """Löscht einen Baustein oder Platzhalter von einer Seite samt seinen Verbindungen.
    Geräte, Räume und Bausteine mit Unterobjekten werden nicht gelöscht."""
    return _safe(lambda: _editor().remove_block(block, page))


@mcp.tool()
def block_addable_types() -> dict:
    """Listet die Bausteintypen, die block_add sicher neu anlegen kann."""
    def run():
        types = sorted(n for n, t in _cat().types.items() if Editor._addable(n, t) is None)
        return {"anzahl": len(types), "typen": types}
    return _safe(run)


@mcp.tool()
def project_changes() -> dict:
    """Zeigt alle ungespeicherten Änderungen gegenüber der Quelldatei in Klartext:
    Schritte, neue und gelöschte Bausteine, neue und gelöste Verbindungen, geänderte Werte."""
    def run():
        if _edit is None or not _edit.log:
            return {"aenderungen": 0, "hinweis": "Es gibt keine ungespeicherten Änderungen."}
        return _edit.changes()
    return _safe(run)


@mcp.tool()
def project_validate() -> dict:
    """Prüft den Arbeitsstand: doppelte UUIDs, Zähler, Verbindungen ins Leere, falsche
    Richtung, ungültige Platzhalter-Ausgänge, Verbindungen über Seitengrenzen.
    Läuft vor jedem Speichern automatisch."""
    def run():
        if _edit is None:
            return {"in_ordnung": True, "fehler": [], "hinweis": "Es gibt keine ungespeicherten Änderungen."}
        return _edit.validate()
    return _safe(run)


@mcp.tool()
def project_undo() -> dict:
    """Nimmt die letzte Änderung am Arbeitsstand zurück."""
    def run():
        if _edit is None:
            raise ProjectError("Es gibt nichts rückgängig zu machen.")
        return _edit.undo()
    return _safe(run)


@mcp.tool()
def project_discard() -> dict:
    """Verwirft alle ungespeicherten Änderungen. Die Quelldatei war nie betroffen."""
    def run():
        global _edit
        n = len(_edit.log) if _edit else 0
        _edit = None
        return {"verworfen": n}
    return _safe(run)


@mcp.tool()
def project_save(filename: str) -> dict:
    """Speichert den Arbeitsstand als NEUE Datei im Projektordner. Die Quelldatei und
    vorhandene Dateien werden nie überschrieben. Vorher läuft die Prüfung; bei Fehlern
    wird nicht gespeichert. Die neue Datei muss der Nutzer in Loxone Config öffnen,
    prüfen und selbst in den Miniserver laden."""
    def run():
        global _edit, _project
        if _edit is None or not _edit.log:
            raise ProjectError("Es gibt keine Änderungen zu speichern.")
        name = Path(filename).name
        if not name.lower().endswith(".loxone"):
            name += ".Loxone"
        result = _edit.save(_projects_dir() / name)
        result["aenderungsliste"] = _edit.changes()
        _project = Project(_projects_dir() / name)
        _edit = None
        a = autosave_report(_project)
        if a:
            result["achtung"] = ("Es gibt eine automatische Sicherung von Loxone Config für dieses Projekt. Beim Öffnen "
                                 "der neuen Datei die Rückfrage zur automatischen Sicherung ablehnen, sonst lädt Config "
                                 "den alten Stand. Am sichersten: Config vorher ohne Speichern schließen.")
        return result
    return _safe(run)


# ---------- Abgleich ----------
@mcp.tool()
def sync_check(miniserver_xml_path: str | None = None, ask_miniserver: bool = True) -> dict:
    """Vergleicht die Projektdatei mit der automatischen Sicherung von Loxone Config und mit dem
    Miniserver (Zeitpunkt der letzten Programmänderung). Mit miniserver_xml_path (Datei mit dem
    Ergebnis von get_loxone_xml des Loxone-MCP) zusätzlich ein inhaltlicher Vergleich.
    Vor jeder Änderung und nach dem Laden in den Miniserver verwenden."""
    def run():
        ms = None
        if ask_miniserver:
            try:
                ms = Miniserver()
            except ProjectError:
                ms = None
        return sync_report(_current(), ms, miniserver_xml_path)
    return _safe(run)


# ---------- Analyse ----------
@mcp.tool()
def project_analyze(stufe: str | None = None) -> dict:
    """Durchsucht das Projekt nach Fehlern (stufe='fehler'), Aufräumbedarf ('aufraeumen') und
    Verbesserungen ('verbesserung'). Ohne stufe alle. Jeder Befund hat einen Vorschlag, oft als
    fertiger Werkzeug- oder Vorlagenaufruf."""
    return _safe(lambda: summarize(analyze(_current()), stufe))


# ---------- Livewerte (nur lesend) ----------
INPUT_TYPES = {"VirtualIn", "DigitalIn", "VirtualUdpInCmd", "VirtualHttpInCmd", "LoxAIRsensor", "LoxAIRAsensor",
               "TreeSensor", "TreeAsensor", "Lox1wireAsensor", "Lox232sensor", "AnalogIn", "VoltageIn", "Constant"}
MULTI_OUT = {"LightController2", "AutoJalousie", "HeatIRoomController2", "CentralShade"}


@mcp.tool()
def live_status() -> dict:
    """Zustand des Miniservers: Programm, Takt, CPU, Speicher, Firmware, letzte Programmänderung.
    Nur lesend."""
    return _safe(lambda: Miniserver().status())


@mcp.tool()
def live_page(page: str, max_values: int = 30) -> dict:
    """Zeigt die aktuellen Werte der Bausteine einer Seite (Platzhalter: Wert des Eingangs bzw.
    Ausgangs; Logik: Ausgangswert; große Bausteine: alle Ausgänge). Nur lesend. Schont den
    Miniserver: höchstens max_values Abfragen (Standard 30)."""
    def run():
        pr = _current()
        pg = pr.find_page(page)
        ms = Miniserver()
        blocks = [pr.blocks[c.get("U")] for c in pg.elem.findall("C") if c.get("U") in pr.blocks]
        blocks.sort(key=lambda b: (int(b.elem.get("Px", "0") or 0), int(b.elem.get("Py", "0") or 0)))
        cache: dict[str, object] = {}
        rows, skipped = [], []
        limit = max(1, min(int(max_values), 38))
        for b in blocks:
            t = pr.target(b) or b
            if t.uuid not in cache:
                if len(cache) >= limit:
                    skipped.append(b.title)
                    continue
                try:
                    if t.type in TEXT_INPUTS:
                        cache[t.uuid] = TEXT_INPUT_NOTE
                    elif t.type in MULTI_OUT:
                        cache[t.uuid] = ms.outputs(t.uuid)
                    else:
                        cache[t.uuid] = ms.value(t.uuid, output=t.type not in INPUT_TYPES)
                except ProjectError as e:
                    cache[t.uuid] = f"nicht lesbar ({e})"
            rows.append({"baustein": ("Platzhalter für " if t is not b else "") + pr.name(t), "wert": cache[t.uuid]})
        out = {"seite": pg.title, "werte": rows, "abfragen": ms.requests}
        if skipped:
            out["nicht_abgefragt"] = skipped
            out["hinweis"] = "Zum Schutz des Miniservers abgebrochen; einzelne Bausteine mit live_block lesen."
        return out
    return _safe(run)


@mcp.tool()
def live_block(block: str, page: str | None = None) -> dict:
    """Aktueller Wert eines Bausteins oder Geräts; bei großen Bausteinen alle Ausgänge. Nur lesend."""
    def run():
        pr = _current()
        b = pr.logical(pr.resolve(block, page))
        if b.type in TEXT_INPUTS:
            return {"baustein": pr.name(b), "ort": pr.location(b), "wert": TEXT_INPUT_NOTE}
        ms = Miniserver()
        val = ms.outputs(b.uuid) if b.type in MULTI_OUT else ms.value(b.uuid, output=b.type not in INPUT_TYPES)
        return {"baustein": pr.name(b), "ort": pr.location(b), "wert": val}
    return _safe(run)


# Bei virtuellen Texteingängen SETZT /jdev/sps/io/<name>/state (bzw. astate/all) den Text auf
# dieses Wort, statt ihn zu lesen (am 2026-11-17 am Miniserver beobachtet). Nie abfragen.
TEXT_INPUTS = {"VirtualTextIn"}
TEXT_INPUT_NOTE = ("nicht abgefragt: Texteingang – eine Abfrage per /state würde den Text überschreiben; "
                   "Wert in der App ansehen")


@mcp.tool()
def live_history(block: str, page: str | None = None) -> dict:
    """Verlauf eines Bausteins aus dem Miniserver (wer hat wann was ausgelöst), in Ortszeit.
    Nur Bausteine mit Verlauf (z. B. Rollladen, Licht, Raumregler). Der Verlauf wird bei jedem
    Laden eines Programms geleert. Nur lesend."""
    def run():
        pr = _current()
        b = pr.logical(pr.resolve(block, page))
        return {"baustein": pr.name(b), "ort": pr.location(b), "verlauf": Miniserver().history(b.uuid)}
    return _safe(run)


# ---------- Funktionsvorlagen ----------
@mcp.tool()
def recipe_list() -> dict:
    """Listet die Funktionsvorlagen mit Beschreibung und Parametern."""
    return {"vorlagen": RECIPES}


@mcp.tool()
def recipe_apply(name: str, params: dict | None = None) -> dict:
    """Wendet eine Funktionsvorlage auf den Arbeitsstand an (alles oder nichts). Danach wie
    gewohnt project_changes und project_save. Parameter siehe recipe_list."""
    return _safe(lambda: Recipes(_editor()).apply(name, params or {}))


if __name__ == "__main__":
    mcp.run()
