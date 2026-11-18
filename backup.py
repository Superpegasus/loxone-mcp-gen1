"""Sicherung des Miniserver-Programms in ein lokales Git-Repository (nur lesend).

Der Miniserver legt jedes geladene Programm im FTP-Ordner /prog ab (sps_<Version>_<Zeit>.LoxCC oder
.Loxone/.zip). Wir laden die neueste Datei herunter (nur Download, nichts wird am Miniserver
verändert), entpacken das LoxCC-Format (LZ4-Block mit 16-Byte-Kopf) zu XML und legen sie als
`Miniserver.Loxone` im Sicherungsordner ab. Jede Änderung wird ein Git-Commit.
"""
from __future__ import annotations

import ftplib
import io
import re
import shutil
import struct
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from live import _credentials
from loxproject import ProjectError

import os
DEFAULT_DIR = Path(os.environ.get("LOXONE_BACKUP_DIR") or Path.home() / "Documents" / "Loxone" / "Miniserver-Backup")


def _git() -> str:
    g = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    if not Path(g).is_file() and not shutil.which("git"):
        raise ProjectError("Git ist nicht installiert.")
    return g


def decompress_loxcc(data: bytes) -> bytes:
    """LoxCC = 16-Byte-Kopf (0xAABBCCEE, gepackte Größe, entpackte Größe, Prüfsumme) + LZ4-Block."""
    magic, csize, usize, _crc = struct.unpack("<LLLL", data[:16])
    if magic != 0xAABBCCEE:
        raise ProjectError("Keine LoxCC-Datei (falscher Kopf).")
    src = data[16:16 + csize]
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off <= 0 or start < 0:
            raise ProjectError("LoxCC-Daten beschädigt.")
        for k in range(ml):
            out.append(out[start + k])
    if usize and len(out) != usize:
        raise ProjectError(f"LoxCC entpackt {len(out)} statt {usize} Byte.")
    return bytes(out)


def _to_xml(name: str, data: bytes) -> bytes:
    low = name.lower()
    if data[:4] == b"\xee\xcc\xbb\xaa":
        return decompress_loxcc(data)
    if low.endswith(".zip") or data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            inner = next((n for n in z.namelist() if n.lower().endswith((".loxcc", ".loxone", ".xml"))), None)
            if not inner:
                raise ProjectError("Im ZIP ist keine Programmdatei.")
            return _to_xml(inner, z.read(inner))
    if data.lstrip()[:5] in (b"<?xml", b"<C Ty"):
        return data
    raise ProjectError(f"Unbekanntes Format: {name}")


def fetch_program() -> tuple[str, bytes]:
    """Neueste Programmdatei aus /prog per FTP (nur lesen). Gibt (Dateiname, XML) zurück."""
    host, user, pw, _src = _credentials()
    host = re.sub(r"^https?://", "", host.strip()).split("/")[0].split(":")[0]
    with ftplib.FTP(host, timeout=20) as ftp:
        ftp.login(user, pw)
        ftp.cwd("/prog")
        # Gen 1 liefert bei NLST ganze Listenzeilen; der Name ist das letzte Wort.
        # Programmdateien heißen sps_<Version>_<JJJJMMTThhmmss>.(zip|LoxCC); MDTM kennt der Miniserver nicht.
        names = []
        for line in ftp.nlst():
            n = line.split()[-1]
            m = re.fullmatch(r"(?i)sps_\d+_(\d{14})\.(loxcc|zip|loxone)", n)
            if m:
                names.append((m.group(1), n))
        if not names:
            raise ProjectError("Im Ordner /prog des Miniservers wurde keine Programmdatei gefunden.")
        newest = max(names)[1]
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {newest}", buf.write)
    return newest, _to_xml(newest, buf.getvalue())


def backup(target: Path | None = None) -> dict:
    target = Path(target or DEFAULT_DIR)
    target.mkdir(parents=True, exist_ok=True)
    git = _git()

    def run(*args: str) -> str:
        r = subprocess.run([git, *args], cwd=target, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise ProjectError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip()

    if not (target / ".git").is_dir():
        run("init", "-b", "main", "-q")
        run("config", "user.name", "loxone-projekt")
        run("config", "user.email", "loxone-mcp@localhost")
        run("config", "core.autocrlf", "false")
    name, xml = fetch_program()
    out = target / "Miniserver.Loxone"
    old = out.read_bytes() if out.is_file() else None
    if old == xml and (target / "Miniserver.txt").is_file():
        return {"geaendert": False, "datei": name, "ordner": str(target),
                "hinweis": "Programm im Miniserver ist seit der letzten Sicherung unverändert."}
    out.write_bytes(xml)
    # lesbare Textansicht daneben: Git-Vergleiche zeigen so Änderungen in Klartext
    try:
        from loxproject import Project as _P
        import textview
        (target / "Miniserver.txt").write_text(textview.render(_P(out)), encoding="utf-8")
        run("add", "Miniserver.txt")
    except Exception as e:  # Textansicht ist Zusatz
        (target / "Miniserver.txt").write_text(f"Textansicht nicht möglich: {e}\n", encoding="utf-8")
    run("add", "Miniserver.Loxone")
    msg = (f"Miniserver-Programm {name} (gesichert {datetime.now():%Y-%m-%d %H:%M})" if old != xml
           else "Textansicht ergänzt")
    old = None if old == xml else old
    run("commit", "-q", "-m", msg)
    res = {"geaendert": True, "datei": name, "ordner": str(target), "commit": run("log", "-1", "--format=%h %s"),
           "kb": round(len(xml) / 1024)}
    if old is not None:
        try:  # Klartext-Änderungen der Textansicht (gekürzt)
            diff = run("diff", "HEAD~1", "HEAD", "--unified=0", "--", "Miniserver.txt")
            res["textaenderungen"] = [l for l in diff.splitlines()
                                      if l[:1] in "+-" and not l.startswith(("+++", "---"))][:40]
        except ProjectError:
            pass
    if old is not None:
        from loxproject import Project
        from sync import diff_projects
        prev = target / ".vorher.Loxone"
        prev.write_bytes(old)
        try:
            d = diff_projects(Project(prev), Project(out))
            res["aenderungen"] = {k: (v[:15] if isinstance(v, list) else v) for k, v in d.items()}
        except Exception as e:  # Vergleich ist Zusatz, Sicherung zählt
            res["aenderungen"] = f"Vergleich nicht möglich: {e}"
        finally:
            prev.unlink(missing_ok=True)
    return res
