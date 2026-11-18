"""Analyse (Stufe â€ž2â€œ): durchsucht ein Projekt nach Fehlern, AufrÃ¤umbedarf und Verbesserungen.

Jeder Fund hat eine Stufe (fehler, aufraeumen, verbesserung), einen Ort, eine Beschreibung
und einen Vorschlag â€“ wenn mÃ¶glich mit dem Werkzeug oder der Vorlage, die ihn behebt.
Verbesserungen sind VorschlÃ¤ge: Ob etwas Absicht ist, kann die Analyse nicht wissen.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from loxproject import Block, Project

LOGIC = {"And", "Or", "Not", "OnDelay", "OffDelay", "OnOffDelay", "OnPulseDelay", "Monoflop", "EdgeDetection",
         "Greater", "GreaterEqual", "Less", "LessEqual", "Equal", "NotEqual", "Formula", "AnalogComparator",
         "AnalogThresholdTrigger", "Minmax", "Int", "BinDecoder", "WipingRelay", "EdgeWipingRelay", "LongClick"}
NO_INPUT_OK = {"PulseGen"}
QUIET_BIG = {"Memory", "Constant", "CentralShade", "CentralLight", "DayTimer", "Text", "State", "StateV"}
SIGNALS = ("VirtualIn", "VirtualUdpInCmd", "VirtualHttpInCmd", "VirtualOutCmd", "Constant")
CONTACT_TYPES = {"VirtualUdpInCmd", "VirtualIn", "LoxAIRsensor", "DigitalIn"}
CONTACT_RE = re.compile(r"(?i)fenster|contact|kontakt|tuer|tÃ¼r")
PRESENCE_RE = re.compile(r"(?i)occupancy|presence|praesenz|prÃ¤senz|bwm|bewegungsmelder")
GENERIC_WORDS = {"fenster", "automatikjalousie", "jalousie", "rollladen", "rollo", "beschattung"}
# Kontakt am Rollladen lohnt sich nur an TÃ¼ren (Aussperrschutz), nicht an Fenstern (Wunsch des Nutzers)
DOOR_RE = re.compile(r"(?i)tÃ¼r|tuer|door|terrass|balkon")
FAULT_RE = re.compile(r"(?i)ventil steckt|kein ventil|hardware defect|batterie schwach")


def _f(stufe: str, art: str, ort: str, text: str, vorschlag: str) -> dict:
    return {"stufe": stufe, "art": art, "ort": ort, "befund": text, "vorschlag": vorschlag}


def analyze(p: Project) -> list[dict]:
    L = p.logical
    out: list[dict] = []
    page_blocks = [b for b in p.blocks.values() if b.page is not None and not p.is_ref(b)]
    ref_targets = {p.target(r).uuid for r in p.blocks.values() if p.is_ref(r)}
    used = {L(e.src).uuid for e in p.edges} | {L(e.dst).uuid for e in p.edges} | ref_targets

    def where(b: Block) -> str:
        return p.location(b)

    # ---------- Fehler ----------
    if p.dangling:
        out.append(_f("fehler", "verbindung_ins_leere", "Projekt",
                      f"{p.dangling} Verbindungen zeigen auf einen Anschluss, den es nicht gibt.",
                      "In Loxone Config Ã¶ffnen und die betroffenen Verbindungen neu ziehen."))
    for b in p.blocks.values():
        ref = b.elem.get("Ref")
        if ref and "Ref" in b.type and ref not in p.blocks:
            out.append(_f("fehler", "platzhalter_ohne_ziel", where(b), f"Platzhalter '{b.title}' zeigt auf ein gelÃ¶schtes Objekt.",
                          f"block_remove('#{p.short(b)}')"))
    for e in p.edges:
        if p.is_ref(e.src) and e.src.type == "InputRef":
            t = p.target(e.src)
            need = "AQ" if e.src.elem.get("Analog") == "true" or t.type != "Memory" else "Q"
            if e.src_key != need:
                out.append(_f("fehler", "ungueltiger_platzhalterausgang", where(e.src),
                              f"{p.label(e.src, e.src_key)} -> {p.label(e.dst, e.dst_key)}: Ausgang {e.src_key} statt {need}. "
                              "Loxone Config verwirft diese Verbindung beim Laden.",
                              "Verbindung lÃ¶sen und Ã¼ber den richtigen Ausgang neu setzen."))

    # ---------- AufrÃ¤umen ----------
    for b in page_blocks:
        if b.type in LOGIC and not p.out_edges.get(b.uuid) and b.uuid not in ref_targets:
            out.append(_f("aufraeumen", "logik_ohne_wirkung", where(b), f"'{b.title}' [{b.type}] gibt sein Ergebnis nirgends weiter.",
                          f"block_remove('#{p.short(b)}'); danach erneut analysieren, ob VorgÃ¤nger dadurch ebenfalls wirkungslos werden."))
        if b.type in LOGIC and b.type not in NO_INPUT_OK and not p.in_edges.get(b.uuid):
            out.append(_f("aufraeumen", "logik_ohne_eingang", where(b), f"'{b.title}' [{b.type}] bekommt kein Signal und kann nie auslÃ¶sen.",
                          f"block_remove('#{p.short(b)}') oder den fehlenden Eingang verbinden."))
        if b.type not in LOGIC and b.type not in QUIET_BIG and not p.in_edges.get(b.uuid) and not p.out_edges.get(b.uuid) \
                and b.connectors:
            out.append(_f("aufraeumen", "baustein_ohne_verbindung", where(b), f"'{b.title}' [{b.type}] hat keine einzige Verbindung.",
                          "In Loxone Config lÃ¶schen (kann in Zentral- und Alarmlisten stehen)."))

    src_of: dict[tuple, set] = defaultdict(set)
    for e in p.edges:
        src_of[(L(e.dst).uuid, e.dst_key)].add(L(e.src).uuid)
    ins_of: dict[str, set] = defaultdict(set)
    for (u, _k), s in src_of.items():
        ins_of[u] |= s
    seen = set()
    for (tu, tk), srcs in src_of.items():
        for mid in srcs:
            for s in srcs & ins_of.get(mid, set()):
                if s != mid and (s, tu, tk) not in seen:
                    seen.add((s, tu, tk))
                    t = p.blocks[tu]
                    out.append(_f("aufraeumen", "doppelter_weg", where(t),
                                  f"'{p.blocks[s].title}' wirkt direkt auf '{t.title}.{tk}' und zusÃ¤tzlich Ã¼ber '{p.blocks[mid].title}'.",
                                  "PrÃ¼fen, welcher Weg gewollt ist, und den anderen mit block_disconnect lÃ¶sen."))
    for b in p.blocks.values():
        for c in b.connectors:
            ls = [(L(p.co_index[s][0]).uuid, p.co_index[s][1].key) for s, _ in c.sources if s in p.co_index]
            for k, n in Counter(ls).items():
                if n > 1:
                    out.append(_f("aufraeumen", "doppelte_verbindung", where(b),
                                  f"'{p.blocks[k[0]].title}' hÃ¤ngt {n}-mal an '{b.title}.{c.key}'.",
                                  "ÃœberzÃ¤hlige Platzhalter mit block_remove lÃ¶schen."))

    for t in SIGNALS:
        unused = [b for b in p.blocks.values() if b.type == t and b.uuid not in used]
        if unused:
            out.append(_f("aufraeumen", "ungenutzte_signale", "Peripherie",
                          f"{len(unused)} Ã— {t} ohne Verwendung: " + ", ".join(b.title for b in unused[:15]),
                          "In Loxone Config lÃ¶schen, falls nicht von auÃŸen (z. B. Home Assistant) nur zum Anzeigen gesetzt."))
    for b in p.blocks.values():
        if b.elem.get("StatsType") not in (None, "0") and b.uuid not in used and b.page is None:
            out.append(_f("aufraeumen", "statistik_ungenutzt", where(b), f"Statistik fÃ¼r ungenutztes Objekt '{b.title}'.",
                          "Statistik in Loxone Config abschalten oder das Objekt lÃ¶schen."))
    onames = Counter(b.page.title for b in page_blocks if re.fullmatch(r"O\d+", b.title))
    if onames:
        out.append(_f("aufraeumen", "automatische_namen", "Seiten " + ", ".join(onames),
                      f"{sum(onames.values())} Bausteine heiÃŸen noch 'O' + Nummer.",
                      "Mit block_set_params({'Titel': ...}) nach ihrer Funktion benennen."))
    pr = Counter(e.get("Pr") for e in p.root.iter("IoData"))
    cr = Counter(e.get("Cr") for e in p.root.iter("IoData"))
    empty_r = [t for u, t in p.places.items() if not pr[u]]
    empty_c = [t for u, t in p.categories.items() if not cr[u]]
    if empty_r:
        out.append(_f("aufraeumen", "leere_raeume", "RÃ¤ume", f"{len(empty_r)} RÃ¤ume ohne Zuordnung: {', '.join(empty_r)}",
                      "structure_remove('raum', ...) je Raum."))
    if empty_c:
        out.append(_f("aufraeumen", "leere_kategorien", "Kategorien", f"{len(empty_c)} Kategorien ohne Zuordnung: {', '.join(empty_c)}",
                      "structure_remove('kategorie', ...) je Kategorie."))
    for kind, names in (("RÃ¤ume", list(p.places.values())), ("Kategorien", list(p.categories.values()))):
        dup = [k for k, n in Counter(x.lower() for x in names).items() if n > 1]
        if dup:
            out.append(_f("aufraeumen", "doppelte_namen", kind, f"Doppelt: {', '.join(dup)}",
                          "Mit structure_reassign zusammenlegen, danach den leeren Eintrag lÃ¶schen."))

    # ---------- Verbesserungen ----------
    contacts = [b for b in p.blocks.values() if b.type in CONTACT_TYPES and CONTACT_RE.search(b.title) and b.uuid in used]
    jals = [b for b in page_blocks if b.type == "AutoJalousie" and p.out_edges.get(b.uuid)]
    on_window = {L(p.co_index[s][0]).uuid for j in jals if j.connector("Window") for s, _ in j.connector("Window").sources
                 if s in p.co_index}
    per_room = Counter(p.room(j) for j in jals)
    for j in jals:
        if not (j.connector("Window") and not j.connector("Window").sources):
            continue
        on_page = [p.target(r) for r in p.blocks.values() if r.page is j.page and p.is_ref(r) and p.target(r) in contacts]
        same_room = [c for c in contacts if p.room(c) == p.room(j)]
        cand = [c for c in {c.uuid: c for c in on_page + same_room}.values() if c.uuid not in on_window]
        words = {w for w in re.findall(r"[a-zÃ¤Ã¶Ã¼ÃŸ]+", j.title.lower()) if w not in GENERIC_WORDS and len(w) > 2}
        matching = [c for c in cand if any(w in c.title.lower() for w in words)]
        if per_room[p.room(j)] > 1 or len(cand) > 1:
            cand = matching  # mehrere Fenster im Raum: nur eindeutige Namenstreffer zÃ¤hlen
        if not DOOR_RE.search(j.title) and not any(DOOR_RE.search(c.title) for c in cand):
            continue  # normale Fenster: kein Vorschlag
        if len(cand) == 1:
            out.append(_f("verbesserung", "fensterkontakt_fehlt_am_rollladen", where(j),
                          f"Rollladen '{j.title}' hat keinen Fensterkontakt; '{cand[0].title}' passt dazu.",
                          f"recipe_apply('fensterkontakt_rollladen', {{'rollladen': '#{p.short(j)}', 'kontakt': '#{p.short(cand[0])}'}})"))
        elif same_room or on_page:
            names = ", ".join(sorted({c.title for c in on_page + same_room}))
            out.append(_f("verbesserung", "fensterkontakt_unklar", where(j),
                          f"Rollladen '{j.title}' hat keinen Fensterkontakt; im Raum gibt es {names}, aber die Zuordnung ist nicht eindeutig.",
                          "KlÃ¤ren, welcher Kontakt zu diesem Fenster gehÃ¶rt, oder Kontakt nachrÃ¼sten."))
    for b in page_blocks:
        if b.type != "HeatIRoomController2":
            continue
        outs = {e.src_key for e in p.out_edges.get(b.uuid, [])}
        if not any(k.startswith("AQh") or k.startswith("AQc") for k in outs):
            out.append(_f("verbesserung", "raumregler_ohne_ventil", where(b),
                          f"Raumregler '{b.title}' steuert keinen Stellantrieb.",
                          "Absicht prÃ¼fen; falls ein Ventil vorhanden ist, AQh mit dem Stellantrieb verbinden."))
        w = b.connector("Window")
        if w is not None and not w.sources:
            cand = [c for c in contacts if p.room(c) == p.room(b)]
            out.append(_f("verbesserung", "raumregler_ohne_fensterkontakt", where(b),
                          f"Raumregler '{b.title}' kennt keinen Fensterkontakt"
                          + (f"; vorhanden: {', '.join(c.title for c in cand)}" if cand else " (im Raum ist keiner bekannt)."),
                          "block_connect(Kontakt, Raumregler, 'Window', page=Seite)" if cand else "Fensterkontakt nachrÃ¼sten."))
    lcs = [b for b in page_blocks if b.type == "LightController2"]
    absent = next((b for b in p.blocks.values() if b.type == "VirtualIn" and b.title.lower() == "abwesen"), None)
    if absent and lcs:
        missing = [b.title for b in lcs if absent.uuid not in {L(p.co_index[s][0]).uuid for s, _ in (b.connector("Reset").sources if b.connector("Reset") else [])}]
        if missing:
            out.append(_f("verbesserung", "licht_bleibt_bei_abwesenheit", "Lichtsteuerungen",
                          f"'Abwesen' schaltet {len(missing)} Lichtsteuerungen nicht aus: {', '.join(missing)}",
                          "recipe_apply('licht_aus_bei_abwesenheit', {}) â€“ vorher mit live_history klÃ¤ren, ob 'Abwesen' ein Impuls ist."))
    presence = [b for b in p.blocks.values() if b.type in ("VirtualIn", "DigitalIn", "LoxAIRsensor") and PRESENCE_RE.search(b.title)]
    unused_p = [b.title for b in presence if b.uuid not in used]
    if unused_p:
        out.append(_f("verbesserung", "praesenz_ungenutzt", "Peripherie", f"PrÃ¤senz-/Bewegungssignale ohne Verwendung: {', '.join(unused_p)}",
                      "An Lichtsteuerung (Presence/Move) oder Raumregler anschlieÃŸen."))
    faults = [b for b in p.blocks.values() if FAULT_RE.search(b.title) and b.uuid not in used]
    if faults:
        c = Counter(b.title for b in faults)
        out.append(_f("verbesserung", "stoermeldungen_ungenutzt", "Peripherie",
                      "StÃ¶rsignale werden nirgends gemeldet: " + ", ".join(f"{n} Ã— {t}" for t, n in c.items()),
                      "In Config eine Mitteilung anlegen; die VerknÃ¼pfung (Oder -> Mitteilung) kann danach gebaut werden."))
    out.extend(lint_layout(p))
    return out


# Sichtbares Blatt einer Seite (PaperFormat 2); siehe editor.SHEET_W/SHEET_H
SHEET_W, SHEET_H = 23400, 16300
# Im Miniserver bestÃ¤tigt: '>' bleibt erhalten, '<' und '%' (in Logik-Titeln) ersetzt Config durch '_'
BAD_TITLE = re.compile(r"[<%]")


def _rect(b: Block):
    try:
        return tuple(int(b.elem.get(k)) for k in ("Px", "Py", "Px2", "Py2"))
    except (TypeError, ValueError):
        return None


def lint_layout(p: Project, only: set[str] | None = None) -> list[dict]:
    """PrÃ¼fungen vor dem Laden, die Loxone Config nicht meldet: Bausteine auÃŸerhalb des Blattes,
    Ã¼berlappende Bausteine, Titel mit < oder % (Config ersetzt sie durch _), Platzhalter ohne Verbindung.
    only: nur diese UUIDs prÃ¼fen (z. B. neue Bausteine); None = ganzes Projekt."""
    out: list[dict] = []
    sel = lambda b: only is None or b.uuid in only
    by_page: dict[str, list] = defaultdict(list)
    for b in p.blocks.values():
        if b.page is not None and _rect(b):
            by_page[b.page.uuid].append(b)
    for blocks in by_page.values():
        for b in blocks:
            if not sel(b):
                continue
            x1, y1, x2, y2 = _rect(b)
            if x1 < 0 or y1 < 0 or x2 > SHEET_W or y2 > SHEET_H:
                out.append(_f("aufraeumen", "ausserhalb_des_blattes", p.location(b),
                              f"'{b.title}' [{b.type}] liegt auÃŸerhalb des Blattes ({x1},{y1}â€“{x2},{y2}).",
                              "Verschieben (block_set_position) oder in Config aufs Blatt ziehen."))
        rects = [(b, _rect(b)) for b in blocks]
        for i, (a, ra) in enumerate(rects):
            for c, rc in rects[i + 1:]:
                if not (sel(a) or sel(c)):
                    continue
                if ra[0] < rc[2] and rc[0] < ra[2] and ra[1] < rc[3] and rc[1] < ra[3]:
                    out.append(_f("aufraeumen", "ueberlappung", p.location(a),
                                  f"'{a.title}' und '{c.title}' liegen Ã¼bereinander.",
                                  "Einen der beiden verschieben (block_set_position)."))
    for b in p.blocks.values():
        if sel(b) and b.title and BAD_TITLE.search(b.title) and b.type not in ("Page",):
            out.append(_f("aufraeumen", "titel_sonderzeichen", p.location(b),
                          f"Titel '{b.title}' enthÃ¤lt < > oder %; Loxone Config ersetzt diese Zeichen durch _.",
                          "Titel ohne diese Zeichen setzen (block_set_params Title)."))
        if sel(b) and b.page is not None and p.is_ref(b) and not p.in_edges.get(b.uuid) and not p.out_edges.get(b.uuid):
            out.append(_f("aufraeumen", "platzhalter_ohne_verbindung", p.location(b),
                          f"Platzhalter '{b.title}' ist mit nichts verbunden.", f"block_remove('#{p.short(b)}')"))
    return out


def summarize(findings: list[dict], stufe: str | None = None) -> dict:
    sel = [f for f in findings if not stufe or f["stufe"] == stufe]
    return {"anzahl": len(sel), "je_stufe": dict(Counter(f["stufe"] for f in findings)),
            "befunde": sel,
            "hinweis": "Verbesserungen sind VorschlÃ¤ge; ob etwas Absicht ist, bitte vor dem Umsetzen klÃ¤ren."}
