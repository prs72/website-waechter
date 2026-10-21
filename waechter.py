#!/usr/bin/env python3
"""Website-Waechter: prueft GAP, BetterKInd und den Prototyp von aussen.

Nur Standardbibliothek. Laeuft lokal, auf dem Studio oder als GitHub Action.
Prueft je Ziel: HTTP-Status, Pflichttext, verbotenen Text (Wartungsseite),
Mindestgroesse, Antwortzeit und Zertifikat (gueltig fuer den Host, Restlaufzeit).

Alarm geht nur bei Zustandswechsel raus (kaputt -> ok, ok -> kaputt) und
als Erinnerung alle ERINNERUNG_STUNDEN, solange etwas kaputt bleibt.
Kanaele: Telegram (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID) und/oder ntfy (NTFY_URL).
Ohne Kanal schreibt das Skript nur auf stdout (Trockenlauf).

Aufruf:  python3 waechter.py [--state status.json] [--json]
Exit-Code 1, wenn mindestens ein Ziel rot ist (fuer CI).
"""
import argparse, datetime as dt, json, os, socket, ssl, sys, time
import urllib.error, urllib.parse, urllib.request

ERINNERUNG_STUNDEN = 6
ZERT_WARN_TAGE = 14
LANGSAM_SEKUNDEN = 8

# Jedes Ziel: name, url, status (erwartet), muss (Text), darf_nicht (Text),
# min_bytes, schwere ("rot" = Alarm, "gelb" = Hinweis), folge (Redirects folgen)
ZIELE = [
    dict(name="GAP Startseite", url="https://www.granny-aupair.com/de",
         status=200, muss="granny", darf_nicht=["Wartungsarbeiten", "Wartungarbeiten"],
         min_bytes=5000, schwere="rot"),
    dict(name="GAP Registrierung", url="https://www.granny-aupair.com/de/user/register",
         status=200, min_bytes=5000, schwere="rot"),
    dict(name="GAP ohne www", url="https://granny-aupair.com/",
         status=None, schwere="gelb"),  # nur Zertifikat und Erreichbarkeit
    dict(name="BetterKInd Website", url="https://www.betterkind.ai/",
         status=200, muss="BetterKInd", darf_nicht=["Wartung"], min_bytes=20000,
         schwere="rot"),
    dict(name="BetterKInd ohne www", url="https://betterkind.ai/",
         status=301, folge=False, ort_enthaelt="www.betterkind.ai", schwere="gelb"),
    dict(name="Prototyp", url="https://prototyp.betterkind.ai/",
         status=200, muss="BetterKInd", min_bytes=1000, schwere="rot"),
    dict(name="Prototyp Bea", url="https://pt4.proto.betterkind.ai/api/bea-health",
         status=200, json_pflicht={"ok": True, "keyKonfiguriert": True}, schwere="rot"),
]

UA = "BK-Website-Waechter/1.0 (+https://www.betterkind.ai)"


class KeinRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def zertifikat(host, port=443, timeout=15):
    """Gibt (ok, tage_rest, text) zurueck. Geht ueber HTTPS_PROXY per CONNECT, falls gesetzt."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    try:
        if proxy:
            p = urllib.parse.urlparse(proxy)
            sock = socket.create_connection((p.hostname, p.port or 80), timeout=timeout)
            sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            antwort = b""
            while b"\r\n\r\n" not in antwort:
                stueck = sock.recv(4096)
                if not stueck:
                    break
                antwort += stueck
            if b" 200" not in antwort.split(b"\r\n")[0]:
                return False, None, "Proxy-CONNECT abgelehnt"
        else:
            sock = socket.create_connection((host, port), timeout=timeout)
        ctx = ssl.create_default_context()
        cafile = os.environ.get("SSL_CERT_FILE")
        if cafile:
            ctx.load_verify_locations(cafile)
        with ctx.wrap_socket(sock, server_hostname=host) as s:
            ende = ssl.cert_time_to_seconds(s.getpeercert()["notAfter"])
        tage = int((ende - time.time()) // 86400)
        return True, tage, f"gueltig, noch {tage} Tage"
    except ssl.SSLCertVerificationError as e:
        return False, None, f"Zertifikat ungueltig: {e.verify_message}"
    except Exception as e:  # noqa: BLE001
        return False, None, f"TLS-Fehler: {e.__class__.__name__}: {e}"


def pruefe(z):
    befunde = []
    url = z["url"]
    host = urllib.parse.urlparse(url).hostname

    ok, tage, text = zertifikat(host)
    if not ok:
        befunde.append(text)
    elif tage is not None and tage < ZERT_WARN_TAGE:
        befunde.append(f"Zertifikat laeuft in {tage} Tagen ab")

    if z.get("status") is not None and ok:
        handler = [] if z.get("folge", True) else [KeinRedirect()]
        opener = urllib.request.build_opener(*handler)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        t0 = time.time()
        try:
            r = opener.open(req, timeout=25)
            code, body, ort = r.status, r.read(), r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            code, body, ort = e.code, e.read() or b"", e.headers.get("Location", "")
        except Exception as e:  # noqa: BLE001
            return z, [f"nicht erreichbar: {e.__class__.__name__}: {e}"], None
        dauer = time.time() - t0
        text_body = body.decode("utf-8", "replace")

        if code != z["status"]:
            befunde.append(f"HTTP {code} statt {z['status']}")
        if z.get("muss") and z["muss"].lower() not in text_body.lower():
            befunde.append(f"Pflichttext '{z['muss']}' fehlt")
        for verboten in z.get("darf_nicht", []):
            if verboten.lower() in text_body.lower():
                befunde.append(f"Wartungs-/Fehlerseite erkannt ('{verboten}')")
        if z.get("min_bytes") and len(body) < z["min_bytes"]:
            befunde.append(f"Seite nur {len(body)} Bytes (erwartet mind. {z['min_bytes']})")
        if z.get("ort_enthaelt") and z["ort_enthaelt"] not in ort:
            befunde.append(f"Weiterleitung geht nach '{ort or '-'}'")
        if z.get("json_pflicht"):
            try:
                daten = json.loads(text_body)
                for k, v in z["json_pflicht"].items():
                    if daten.get(k) != v:
                        befunde.append(f"{k} = {daten.get(k)!r} statt {v!r}")
            except ValueError:
                befunde.append("Antwort ist kein JSON")
        if dauer > LANGSAM_SEKUNDEN:
            befunde.append(f"langsam: {dauer:.1f} s")
        return z, befunde, round(dauer, 2)
    return z, befunde, None


def senden(text):
    gesendet = False
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        daten = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", daten, timeout=15)
        gesendet = True
    ntfy = os.environ.get("NTFY_URL")
    if ntfy:
        req = urllib.request.Request(ntfy, data=text.encode(), headers={"Title": "Website-Waechter"})
        urllib.request.urlopen(req, timeout=15)
        gesendet = True
    return gesendet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="waechter-status.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        alt = json.load(open(a.state))
    except (OSError, ValueError):
        alt = {}

    jetzt = dt.datetime.now(dt.timezone.utc)
    neu, meldungen, rot = {}, [], False
    for z in ZIELE:
        z, befunde, dauer = pruefe(z)
        kaputt = bool(befunde)
        if kaputt and z["schwere"] == "rot":
            rot = True
        vorher = alt.get(z["name"], {})
        eintrag = {"kaputt": kaputt, "befunde": befunde, "dauer": dauer,
                   "seit": vorher.get("seit") if vorher.get("kaputt") == kaputt else jetzt.isoformat(),
                   "gemeldet": vorher.get("gemeldet")}
        zeichen = ("ROT" if z["schwere"] == "rot" else "GELB") if kaputt else "OK"
        print(f"[{zeichen:4}] {z['name']:22} {dauer if dauer is not None else '-':>5}s  "
              + ("; ".join(befunde) if befunde else "in Ordnung"))

        faellig = False
        if kaputt != bool(vorher.get("kaputt")) and (kaputt or vorher):
            faellig = True
        elif kaputt and vorher.get("gemeldet"):
            letzte = dt.datetime.fromisoformat(vorher["gemeldet"])
            faellig = (jetzt - letzte).total_seconds() > ERINNERUNG_STUNDEN * 3600
        elif kaputt and not vorher:
            faellig = True
        if faellig:
            if kaputt:
                meldungen.append(f"{'STOERUNG' if z['schwere']=='rot' else 'Hinweis'}: {z['name']}\n"
                                 f"{z['url']}\n- " + "\n- ".join(befunde))
            else:
                meldungen.append(f"Wieder in Ordnung: {z['name']} ({z['url']})")
            eintrag["gemeldet"] = jetzt.isoformat()
        neu[z["name"]] = eintrag

    if meldungen:
        text = "Website-Waechter " + jetzt.astimezone().strftime("%d.%m. %H:%M") + "\n\n" + "\n\n".join(meldungen)
        try:
            if not senden(text):
                print("\n(kein Kanal gesetzt, Alarm nur hier)\n" + text)
        except Exception as e:  # noqa: BLE001
            # Zustand NICHT speichern, damit der naechste Lauf den Alarm erneut sendet
            print(f"\nALARM KONNTE NICHT GESENDET WERDEN: {e.__class__.__name__}: {e}\n" + text)
            sys.exit(2)
    json.dump(neu, open(a.state, "w"), indent=2, ensure_ascii=False)
    if a.json:
        print(json.dumps(neu, indent=2, ensure_ascii=False))
    # Exit 1 = mindestens ein Ziel rot (fuer lokale Nutzung), 2 = Alarm nicht zustellbar
    sys.exit(1 if rot else 0)


if __name__ == "__main__":
    main()
