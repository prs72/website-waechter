# Website-Wächter

Prüft von außen, ob die Websites erreichbar sind, und meldet Zustandswechsel per Telegram. Nur Python-Standardbibliothek, keine Abhängigkeiten.

## Was geprüft wird

Je Ziel (alle Angaben optional außer Name, URL und Schwere):

- HTTP-Status
- Pflichttext (muss in der Seite stehen)
- verbotener Text (zum Beispiel Wartungsseite)
- Mindestgröße der Antwort in Bytes
- Antwortzeit (über 8 Sekunden gilt als langsam)
- Weiterleitungsziel (bei Ziel ohne Redirect-Folgen)
- Pflichtfelder in einer JSON-Antwort
- Zertifikat: gültig für den Host, Warnung bei weniger als 14 Tagen Restlaufzeit

Ist das Zertifikat ungültig oder der Host nicht erreichbar, entfällt die HTTP-Prüfung dieses Ziels. Ein Ziel ohne `status` wird nur auf Zertifikat und Erreichbarkeit geprüft.

## Wo die Ziele stehen

In der Konstante `ZIELE` oben in `waechter.py`. Dort stehen je Ziel `name`, `url`, `status`, `muss`, `darf_nicht`, `min_bytes`, `schwere` (`rot` oder `gelb`), `folge`, `ort_enthaelt` und `json_pflicht`. Die Schwellen sind `ERINNERUNG_STUNDEN` (6), `ZERT_WARN_TAGE` (14) und `LANGSAM_SEKUNDEN` (8).

## Alarmverhalten

- Eine Meldung geht nur bei einem Zustandswechsel raus: ok zu kaputt ("STOERUNG" bei Schwere rot, "Hinweis" bei gelb) und kaputt zu ok ("Wieder in Ordnung").
- Bleibt ein Ziel kaputt, kommt alle 6 Stunden eine Erinnerung.
- Den letzten Zustand hält `waechter-status.json` (steht in der `.gitignore`). In der GitHub Action wandert die Datei zwischen den Läufen über den Actions-Cache.
- Kanäle: Telegram (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) und optional ntfy (`NTFY_URL`). Ist kein Kanal gesetzt, steht der Alarm nur auf stdout (Trockenlauf).
- Lässt sich ein Alarm nicht zustellen, wird der Zustand nicht gespeichert, damit der nächste Lauf ihn erneut sendet.
- Exit-Code 0: alles in Ordnung oder nur Hinweise (gelb). 1: mindestens ein Ziel rot. 2: Alarm nicht zustellbar.

## Lokal ausführen

```
python3 waechter.py [--state status.json] [--json]
```

Trockenlauf, bei dem garantiert nichts gesendet wird:

```
env -u TELEGRAM_TOKEN -u TELEGRAM_CHAT_ID -u NTFY_URL python3 waechter.py --state /pfad/zu/test.json
```

## GitHub Action

`.github/workflows/website-waechter.yml` läuft alle 10 Minuten (`*/10 * * * *`) und auf Knopfdruck (`workflow_dispatch`). Exit 1 (rot) lässt den Lauf grün, damit GitHub keine Fehlermail pro Lauf schickt, gemeldet wird über Telegram. Nur Exit 2 lässt den Lauf scheitern.

Der Schritt "Keepalive" legt einen leeren Commit an, sobald der letzte Commit älter als 45 Tage ist. Dafür braucht der Workflow `contents: write`.

## Benötigte Repository-Secrets

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

Anlegen unter Settings, Secrets and variables, Actions. Beide Werte kommen ausschließlich aus Umgebungsvariablen, im Code steht keiner.
