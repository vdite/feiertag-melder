# Feiertag-Melder

Meldet gesetzliche Feiertage der gewählten Bundesländer mit Vorlauf (Standard: 6 Wochen) per **Pushover** und/oder **E-Mail (SMTP)**.
Alles wird über die Weboberfläche eingestellt (Port **8642**) – keine Umgebungsvariablen nötig.
Einstellungen und Status liegen in `/data` (`config.json`, `status.json`).

## ZimaOS

1. **App Store → Benutzerdefinierte App installieren → Import** und den Inhalt von
   [`zimaos-compose.yml`](zimaos-compose.yml) einfügen.
2. Weboberfläche öffnen: `http://<zima-ip>:8642` → Bundesländer & Kanal wählen → Speichern → „Testnachricht senden“.

Das Image `ghcr.io/vdite/feiertag-melder` wird per GitHub Actions für amd64 und arm64 gebaut.

## Normales Docker

```bash
docker compose up -d --build
```
Oder ohne Bauen: `docker run -d -p 8642:8080 -v ./data:/data ghcr.io/vdite/feiertag-melder:latest`. Dann `http://localhost:8642` öffnen.

## Funktionsweise

- Prüft täglich zur eingestellten Uhrzeit (und sofort, falls die heutige Prüfung verpasst wurde).
- Jeder Feiertag im Vorlauf-Fenster wird **einmal** gemeldet. War der Container aus, wird nachgeholt.
- Gilt ein Feiertag in mehreren gewählten Ländern, kommt er nur einmal mit Länderliste.
- Die Weboberfläche hat keinen Login – nur im Heimnetz betreiben, nicht ins Internet freigeben.
