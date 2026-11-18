"""Nur lesender Zugriff auf den Miniserver per HTTP (Stufe 3 und Abgleich).

Zugangsdaten nur aus den Umgebungsvariablen LOXONE_HOST/LOXONE_USER/LOXONE_PASS
(z. B. im "env"-Block des MCP-Eintrags). Das Passwort wird nie ausgegeben.
Empfehlung: einen eigenen Loxone-Benutzer mit möglichst wenig Rechten anlegen.

Bewusst keine Schaltbefehle: Es werden nur Pfade aus einer festen Liste von
Lesebefehlen erlaubt.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from loxproject import ProjectError

READ_SUFFIXES = ("/state", "/astate", "/all", "/gethistory")
READ_PATHS = {"dev/sys/cpu", "dev/sys/heap", "dev/sps/state", "dev/sps/status", "dev/cfg/version",
              "dev/sps/LoxAPPversion3", "dev/sys/time", "dev/sys/date"}
MAX_REQUESTS_PER_CALL = 40


def _credentials() -> tuple[str, str, str, str]:
    """(host, user, password, quelle)"""
    h, u, p = (os.environ.get(k) for k in ("LOXONE_HOST", "LOXONE_USER", "LOXONE_PASS"))
    if h and u and p:
        return h, u, p, "eigene Umgebungsvariablen"
    raise ProjectError("Keine Zugangsdaten für den Miniserver: bitte LOXONE_HOST, LOXONE_USER und "
                       "LOXONE_PASS als Umgebungsvariablen setzen.")


class Miniserver:
    def __init__(self):
        host, user, pw, self.source = _credentials()
        host = host.strip().rstrip("/")
        if not re.match(r"https?://", host):
            host = "http://" + host
        self.base = host
        self._auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        self.requests = 0

    def _allowed(self, path: str) -> bool:
        p = path.strip("/")
        if p.startswith("jdev/"):
            p = p[1:]
        return p in READ_PATHS or (p.startswith("dev/sps/io/") and p.endswith(READ_SUFFIXES))

    def get(self, path: str):
        if not self._allowed(path):
            raise ProjectError(f"Pfad {path} ist kein erlaubter Lesebefehl.")
        self.requests += 1
        if self.requests > MAX_REQUESTS_PER_CALL:
            raise ProjectError("Zu viele Anfragen in einem Aufruf; der Miniserver wird geschont.")
        url = f"{self.base}/{path.strip('/')}"
        req = urllib.request.Request(url, headers={"Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ProjectError(f"Miniserver antwortet mit HTTP {e.code} auf {path.split('/')[-1]}.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProjectError(f"Miniserver nicht erreichbar: {e}") from None
        if path.strip("/").startswith("jdev/"):
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body
        m = re.search(r'value="([^"]*)"', body)
        return m.group(1) if m else body

    def ll(self, path: str) -> dict:
        """jdev-Abfrage, gibt den LL-Block zurück."""
        data = self.get("jdev/" + path.strip("/"))
        if isinstance(data, dict) and "LL" in data:
            return data["LL"]
        if isinstance(data, list):
            return {"value": data}
        raise ProjectError("Unerwartete Antwort des Miniservers.")

    # ---------- Bausteine ----------
    def value(self, uuid: str, output: bool = True) -> str:
        return str(self.ll(f"sps/io/{uuid}/{'astate' if output else 'state'}").get("value"))

    def outputs(self, uuid: str) -> dict:
        ll = self.ll(f"sps/io/{uuid}/all")
        out = {"wert": ll.get("value")}
        for k, v in ll.items():
            if k.startswith("output") and isinstance(v, dict):
                out[v.get("name") or f"Ausgang {v.get('nr')}"] = v.get("value")
        return out

    def history(self, uuid: str) -> list:
        data = self.get(f"jdev/sps/io/{uuid}/gethistory")
        if isinstance(data, dict) and "LL" in data:
            data = data["LL"].get("value")
        if not isinstance(data, list):
            raise ProjectError("Dieser Baustein führt keinen Verlauf.")
        rows = []
        for e in data:
            ts = e.get("ts")
            row = {"zeit": datetime.fromtimestamp(ts).strftime("%d.%m. %H:%M") if ts else None,
                   "was": e.get("what")}
            if e.get("trigger"):
                row["ausloeser"] = e["trigger"]
            if e.get("impacts"):
                row["folgen"] = e["impacts"]
            rows.append(row)
        return rows

    def program_changed(self) -> str:
        return str(self.ll("sps/LoxAPPversion3").get("value"))

    def status(self) -> dict:
        states = {"0": "kein Programm", "1": "startet", "2": "geladen", "3": "gestartet", "4": "LoxLink gestartet",
                  "5": "läuft", "6": "wird geändert", "7": "Fehler", "8": "Update"}
        sps = str(self.get("dev/sps/state"))
        return {
            "programm": states.get(sps, sps),
            "takt": self.get("dev/sps/status"),
            "cpu": self.get("dev/sys/cpu"),
            "speicher": self.get("dev/sys/heap"),
            "firmware": self.get("dev/cfg/version"),
            "letzte_programmaenderung": self.program_changed(),
            "hinweis": "Die CPU-Angabe ist ein Augenblickswert; jede Abfrage erzeugt selbst Last.",
            "zugangsdaten_aus": self.source,
        }
