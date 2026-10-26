"""Feiertag-Melder: meldet gesetzliche Feiertage ausgewählter Bundesländer
mit einstellbarem Vorlauf (Standard 6 Wochen) per SMTP und/oder Pushover.
Konfiguration über die Weboberfläche, gespeichert in DATA_DIR/config.json."""

import argparse
import html
import json
import os
import smtplib
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import holidays

LAENDER = {
    "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin",
    "BB": "Brandenburg", "HB": "Bremen", "HH": "Hamburg", "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland",
    "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "status.json"
PORT = int(os.environ.get("PORT", "8080"))
ICON_FILE = Path(__file__).with_name("icon.png")

STANDARD = {
    "laender": [], "vorlauf_tage": 42, "pruef_uhrzeit": "08:00",
    "zeitzone": "Europe/Berlin", "kanaele": [],
    "pushover_token": "", "pushover_user": "",
    "smtp_host": "", "smtp_port": 587, "smtp_sicherheit": "starttls",
    "smtp_user": "", "smtp_passwort": "", "smtp_from": "", "smtp_to": "",
}
GEHEIM = ("pushover_token", "smtp_passwort")

sperre = threading.Lock()


# ---------- Speicher ----------

def lade_json(pfad, standard):
    try:
        return {**standard, **json.loads(pfad.read_text())}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(standard)


def speichere_json(pfad, daten):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2))
    tmp.replace(pfad)


def lade_config():
    return lade_json(CONFIG_FILE, STANDARD)


def lade_status():
    return lade_json(STATE_FILE, {"gemeldet": [], "letzte_pruefung": None, "letztes_ergebnis": ""})


def jetzt(cfg):
    try:
        return datetime.now(ZoneInfo(cfg["zeitzone"]))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(ZoneInfo("Europe/Berlin"))


# ---------- Feiertage ----------

def anstehende_feiertage(laender, heute, vorlauf):
    """Liefert {(datum, name): [länder]} für alle Feiertage im Zeitraum heute..heute+vorlauf."""
    ende = heute + timedelta(days=vorlauf)
    jahre = sorted({heute.year, ende.year})
    treffer = {}
    for land in laender:
        kalender = holidays.country_holidays("DE", subdiv=land, years=jahre, language="de")
        for tag, name in kalender.items():
            if heute <= tag <= ende:
                treffer.setdefault((tag, name), []).append(land)
    return dict(sorted(treffer.items()))


def schluessel(tag, name, land):
    return f"{tag.isoformat()}|{land}|{name}"


def wann(tag, heute):
    tage = (tag - heute).days
    return "heute" if tage == 0 else "morgen" if tage == 1 else f"in {tage} Tagen"


def wo(laender, alle_laender):
    if set(laender) == set(alle_laender) and len(alle_laender) > 1:
        return "alle gewählten Bundesländer"
    return ", ".join(LAENDER[l] for l in laender)


def formatiere(eintraege, alle_laender, heute):
    zeilen = [
        f"• {WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.%Y} – {name} ({wann(tag, heute)})\n  {wo(laender, alle_laender)}"
        for (tag, name), laender in eintraege.items()
    ]
    titel = "Anstehender Feiertag" if len(eintraege) == 1 else f"{len(eintraege)} anstehende Feiertage"
    return titel, "\n".join(zeilen)


# ---------- Versand ----------

def sende_pushover(cfg, titel, text):
    if not (cfg["pushover_token"] and cfg["pushover_user"]):
        raise RuntimeError("Pushover-Token oder User-Key fehlt")
    daten = urllib.parse.urlencode({
        "token": cfg["pushover_token"], "user": cfg["pushover_user"],
        "title": titel, "message": text,
    }).encode()
    with urllib.request.urlopen("https://api.pushover.net/1/messages.json", data=daten, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"Pushover antwortete mit HTTP {r.status}")


def sende_smtp(cfg, titel, text):
    if not (cfg["smtp_host"] and cfg["smtp_to"]):
        raise RuntimeError("SMTP-Server oder Empfänger fehlt")
    msg = EmailMessage()
    msg["Subject"] = titel
    msg["From"] = cfg["smtp_from"] or cfg["smtp_user"]
    msg["To"] = cfg["smtp_to"]
    msg.set_content(text)

    port = int(cfg["smtp_port"])
    if cfg["smtp_sicherheit"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=30)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], port, timeout=30)
    with server:
        if cfg["smtp_sicherheit"] == "starttls":
            server.starttls()
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], cfg["smtp_passwort"])
        server.send_message(msg)


SENDER = {"pushover": sende_pushover, "smtp": sende_smtp}


def sende(cfg, titel, text):
    """Sendet über alle aktiven Kanäle. Liefert (erfolg, fehlerliste)."""
    if not cfg["kanaele"]:
        return False, ["Kein Kanal aktiviert"]
    erfolg, fehler = False, []
    for kanal in cfg["kanaele"]:
        try:
            SENDER[kanal](cfg, titel, text)
            log(f"Gesendet via {kanal}: {titel}")
            erfolg = True
        except Exception as e:  # ein Kanal-Fehler soll die anderen nicht blockieren
            fehler.append(f"{kanal}: {e}")
            log(f"Fehler beim Senden via {kanal}: {e}")
    return erfolg, fehler


# ---------- Prüfung ----------

def log(text):
    print(f"{jetzt(lade_config()):%Y-%m-%d %H:%M:%S} {text}", flush=True)


def pruefe(trocken=False):
    with sperre:
        cfg, status = lade_config(), lade_status()
        heute = jetzt(cfg).date()
        gemeldet = set(status["gemeldet"])

        neu = {}
        for (tag, name), betroffen in anstehende_feiertage(cfg["laender"], heute, int(cfg["vorlauf_tage"])).items():
            offen = [l for l in betroffen if schluessel(tag, name, l) not in gemeldet]
            if offen:
                neu[(tag, name)] = offen

        if not neu:
            ergebnis = "Keine neuen Feiertage."
        else:
            titel, text = formatiere(neu, cfg["laender"], heute)
            if trocken:
                print(f"[Testlauf] {titel}\n{text}")
                return
            erfolg, fehler = sende(cfg, titel, text)
            if erfolg:
                for (tag, name), betroffen in neu.items():
                    gemeldet.update(schluessel(tag, name, l) for l in betroffen)
            ergebnis = f"{titel} gemeldet." if erfolg else "Senden fehlgeschlagen"
            if fehler:
                ergebnis += " Fehler: " + "; ".join(fehler)

        log(ergebnis)
        if trocken:
            return
        status.update(
            gemeldet=sorted(k for k in gemeldet if k[:10] >= heute.isoformat()),
            letzte_pruefung=jetzt(cfg).strftime("%Y-%m-%d %H:%M"),
            letztes_ergebnis=ergebnis,
        )
        speichere_json(STATE_FILE, status)


def zeitplaner():
    while True:
        try:
            cfg, status = lade_config(), lade_status()
            n = jetzt(cfg)
            faellig = n.strftime("%H:%M") >= cfg["pruef_uhrzeit"]
            heute_geprueft = (status["letzte_pruefung"] or "").startswith(n.date().isoformat())
            if cfg["laender"] and cfg["kanaele"] and faellig and not heute_geprueft:
                pruefe()
        except Exception as e:
            log(f"Fehler im Zeitplaner: {e}")
        time.sleep(30)


# ---------- Weboberfläche ----------

SEITE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feiertag-Melder</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
:root{{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--mut:#667085;--line:#e3e6eb;--acc:#2563eb;--ok:#15803d;--err:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b1f27;--fg:#e6e8ec;--mut:#98a1b3;--line:#2c323d;--acc:#60a5fa;--ok:#4ade80;--err:#f87171}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:0 0 12px}}
.mut{{color:var(--mut);font-size:13px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:16px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px 12px}}
label{{display:block}}.feld{{margin:10px 0}}.feld>span{{display:block;font-size:13px;color:var(--mut);margin-bottom:3px}}
input[type=text],input[type=number],input[type=time],input[type=password],select{{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}}
.zeile{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:0 12px}}
button{{padding:9px 16px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit;cursor:pointer}}
button.prim{{background:var(--acc);border-color:var(--acc);color:#fff}}.knoepfe{{display:flex;flex-wrap:wrap;gap:8px}}
.hinweis{{padding:10px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card)}}
.ok{{color:var(--ok)}}.err{{color:var(--err)}}ul{{margin:0;padding-left:18px}}li{{margin:4px 0}}
</style></head><body><main>
<h1>Feiertag-Melder</h1>
<div class="mut">Meldet gesetzliche Feiertage {vorlauf} Tage im Voraus. Letzte Prüfung: {letzte} – {ergebnis}</div>
{meldung}
<form method="post" action="/speichern">
<div class="card"><h2>Bundesländer</h2><div class="grid">{laender}</div></div>
<div class="card"><h2>Zeitplan</h2><div class="zeile">
<label class="feld"><span>Vorlauf (Tage)</span><input type="number" name="vorlauf_tage" min="1" max="365" value="{vorlauf}"></label>
<label class="feld"><span>Tägliche Prüfung um</span><input type="time" name="pruef_uhrzeit" value="{pruef_uhrzeit}"></label>
<label class="feld"><span>Zeitzone</span><input type="text" name="zeitzone" value="{zeitzone}"></label>
</div></div>
<div class="card"><h2>Pushover</h2>
<label><input type="checkbox" name="kanaele" value="pushover" {pushover_an}> Pushover aktiv</label>
<div class="zeile">
<label class="feld"><span>API-Token</span><input type="password" name="pushover_token" placeholder="{pushover_token_ph}" autocomplete="off"></label>
<label class="feld"><span>User-Key</span><input type="text" name="pushover_user" value="{pushover_user}"></label>
</div></div>
<div class="card"><h2>E-Mail (SMTP)</h2>
<label><input type="checkbox" name="kanaele" value="smtp" {smtp_an}> E-Mail aktiv</label>
<div class="zeile">
<label class="feld"><span>Server</span><input type="text" name="smtp_host" value="{smtp_host}"></label>
<label class="feld"><span>Port</span><input type="number" name="smtp_port" value="{smtp_port}"></label>
<label class="feld"><span>Verschlüsselung</span><select name="smtp_sicherheit">{sicherheit}</select></label>
</div><div class="zeile">
<label class="feld"><span>Benutzer</span><input type="text" name="smtp_user" value="{smtp_user}" autocomplete="off"></label>
<label class="feld"><span>Passwort</span><input type="password" name="smtp_passwort" placeholder="{smtp_passwort_ph}" autocomplete="new-password"></label>
</div><div class="zeile">
<label class="feld"><span>Absender</span><input type="text" name="smtp_from" value="{smtp_from}"></label>
<label class="feld"><span>Empfänger (kommagetrennt)</span><input type="text" name="smtp_to" value="{smtp_to}"></label>
</div></div>
<div class="knoepfe"><button class="prim" type="submit">Speichern</button>
<button type="submit" formaction="/test">Testnachricht senden</button>
<button type="submit" formaction="/jetzt">Jetzt prüfen &amp; melden</button>
<button type="submit" formaction="/zuruecksetzen">Gemeldete zurücksetzen</button></div>
</form>
<div class="card"><h2>Feiertage in den nächsten {vorlauf} Tagen</h2>{vorschau}</div>
</main></body></html>"""


def e(wert):
    return html.escape(str(wert), quote=True)


def rendere(meldung=""):
    cfg, status = lade_config(), lade_status()
    heute = jetzt(cfg).date()
    gemeldet = set(status["gemeldet"])

    laender = "".join(
        f'<label><input type="checkbox" name="laender" value="{k}" {"checked" if k in cfg["laender"] else ""}> {v}</label>'
        for k, v in LAENDER.items()
    )
    sicherheit = "".join(
        f'<option value="{w}" {"selected" if cfg["smtp_sicherheit"] == w else ""}>{t}</option>'
        for w, t in (("starttls", "STARTTLS (587)"), ("ssl", "SSL/TLS (465)"), ("none", "keine"))
    )

    if not cfg["laender"]:
        vorschau = '<div class="mut">Noch keine Bundesländer ausgewählt.</div>'
    else:
        eintraege = anstehende_feiertage(cfg["laender"], heute, int(cfg["vorlauf_tage"]))
        punkte = []
        for (tag, name), betr in eintraege.items():
            erledigt = all(schluessel(tag, name, l) in gemeldet for l in betr)
            marke = ' <span class="ok">✓ gemeldet</span>' if erledigt else ""
            punkte.append(
                f"<li><b>{WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.%Y}</b> – {e(name)} ({wann(tag, heute)}){marke}"
                f'<br><span class="mut">{e(wo(betr, cfg["laender"]))}</span></li>'
            )
        vorschau = f"<ul>{''.join(punkte)}</ul>" if punkte else '<div class="mut">Keine Feiertage im Zeitraum.</div>'

    if meldung:
        klasse = "err" if meldung.startswith("Fehler") else "ok"
        meldung = f'<div class="card hinweis {klasse}">{e(meldung)}</div>'

    return SEITE.format(
        meldung=meldung, laender=laender, sicherheit=sicherheit, vorschau=vorschau,
        letzte=e(status["letzte_pruefung"] or "noch nie"), ergebnis=e(status["letztes_ergebnis"] or "–"),
        vorlauf=e(cfg["vorlauf_tage"]), pruef_uhrzeit=e(cfg["pruef_uhrzeit"]), zeitzone=e(cfg["zeitzone"]),
        pushover_an="checked" if "pushover" in cfg["kanaele"] else "",
        smtp_an="checked" if "smtp" in cfg["kanaele"] else "",
        pushover_token_ph="gespeichert – leer lassen zum Behalten" if cfg["pushover_token"] else "",
        smtp_passwort_ph="gespeichert – leer lassen zum Behalten" if cfg["smtp_passwort"] else "",
        **{k: e(cfg[k]) for k in ("pushover_user", "smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_to")},
    )


def speichere_formular(form):
    cfg = lade_config()
    feld = lambda k: form.get(k, [""])[0].strip()

    uhrzeit = feld("pruef_uhrzeit") or "08:00"
    datetime.strptime(uhrzeit, "%H:%M")
    zeitzone = feld("zeitzone") or "Europe/Berlin"
    ZoneInfo(zeitzone)

    cfg.update(
        laender=[l for l in form.get("laender", []) if l in LAENDER],
        kanaele=[k for k in form.get("kanaele", []) if k in SENDER],
        vorlauf_tage=max(1, min(365, int(feld("vorlauf_tage") or 42))),
        pruef_uhrzeit=uhrzeit, zeitzone=zeitzone,
        smtp_port=int(feld("smtp_port") or 587),
        smtp_sicherheit=feld("smtp_sicherheit") if feld("smtp_sicherheit") in ("starttls", "ssl", "none") else "starttls",
        **{k: feld(k) for k in ("pushover_user", "smtp_host", "smtp_user", "smtp_from", "smtp_to")},
    )
    for k in GEHEIM:  # leeres Passwortfeld = gespeicherten Wert behalten
        if feld(k):
            cfg[k] = feld(k)
    with sperre:
        speichere_json(CONFIG_FILE, cfg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def antworte(self, inhalt, typ="text/html; charset=utf-8"):
        daten = inhalt.encode() if isinstance(inhalt, str) else inhalt
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def weiter(self, meldung):
        self.send_response(303)
        self.send_header("Location", "/?" + urllib.parse.urlencode({"m": meldung}))
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/icon.png" and ICON_FILE.exists():
            self.antworte(ICON_FILE.read_bytes(), "image/png")
            return
        if url.path != "/":
            self.send_error(404)
            return
        self.antworte(rendere(urllib.parse.parse_qs(url.query).get("m", [""])[0]))

    def do_POST(self):
        laenge = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(laenge).decode(), keep_blank_values=True)
        try:
            speichere_formular(form)
            if self.path == "/speichern":
                self.weiter("Gespeichert.")
            elif self.path == "/test":
                ok, fehler = sende(lade_config(), "Feiertag-Melder Test", "Wenn du das liest, funktioniert der Versand. 🎉")
                self.weiter("Testnachricht gesendet." if ok and not fehler else "Fehler: " + "; ".join(fehler))
            elif self.path == "/jetzt":
                pruefe()
                self.weiter(lade_status()["letztes_ergebnis"])
            elif self.path == "/zuruecksetzen":
                with sperre:
                    status = lade_status()
                    status["gemeldet"] = []
                    speichere_json(STATE_FILE, status)
                self.weiter("Gemeldete Feiertage zurückgesetzt – sie werden bei der nächsten Prüfung erneut gesendet.")
            else:
                self.send_error(404)
        except (ValueError, ZoneInfoNotFoundError) as fehler:
            self.weiter(f"Fehler: Ungültige Eingabe ({fehler})")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trocken", action="store_true", help="anstehende Meldung nur ausgeben, nichts senden")
    args = p.parse_args()

    if args.trocken:
        pruefe(trocken=True)
        return

    threading.Thread(target=zeitplaner, daemon=True).start()
    log(f"Feiertag-Melder läuft – Weboberfläche auf Port {PORT}, Daten in {DATA_DIR.resolve()}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
