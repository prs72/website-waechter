"""Tests fuer waechter.py (nur Standardbibliothek): python3 -m unittest discover -s tests -v"""
import contextlib
import email.message
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import waechter  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    antwort = b"[]"
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.antwort)))
        self.end_headers()
        self.wfile.write(self.antwort)

    def log_message(self, *a):
        pass


@contextlib.contextmanager
def server(antwort, status=200):
    handler = type("H", (_Handler,), {"antwort": antwort, "status": status})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/api/bea-health"
    finally:
        httpd.shutdown()
        httpd.server_close()


def ziel(url, **kw):
    z = dict(name="Test", url=url, status=200, json_pflicht={"ok": True}, schwere="rot")
    z.update(kw)
    return z


@mock.patch.object(waechter, "zertifikat", return_value=(True, 90, "gueltig, noch 90 Tage"))
class JsonPflicht(unittest.TestCase):
    def befunde(self, antwort):
        with server(antwort) as url:
            return waechter.pruefe(ziel(url))[1]

    def test_gutes_objekt(self, _):
        self.assertEqual(self.befunde(b'{"ok": true}'), [])

    def test_falscher_wert(self, _):
        self.assertEqual(self.befunde(b'{"ok": false}'), ["ok = False statt True"])

    def test_kein_json(self, _):
        self.assertEqual(self.befunde(b"<html>"), ["Antwort ist kein JSON"])

    def test_json_ohne_objekt_stuerzt_nicht_ab(self, _):
        for roh, typ in [(b"[]", "list"), (b"null", "NoneType"), (b"42", "int"), (b'"text"', "str"),
                         (b"true", "bool")]:
            with self.subTest(roh=roh):
                self.assertEqual(self.befunde(roh), [f"Antwort ist kein JSON-Objekt (sondern {typ})"])

    def test_tief_verschachteltes_json(self, _):
        self.assertEqual(self.befunde(b"[" * 200000 + b"]" * 200000), ["Antwort ist kein JSON"])


@mock.patch.object(waechter, "zertifikat", return_value=(True, 90, "gueltig, noch 90 Tage"))
class FehlerAntwort(unittest.TestCase):
    def test_fehlercode_mit_normalem_body(self, _):
        with server(b"Wartung", status=503) as url:
            befunde = waechter.pruefe(ziel(url, json_pflicht=None))[1]
        self.assertEqual(befunde, ["HTTP 503 statt 200"])

    def test_body_lesen_bricht_ab(self, _):
        class Kaputt:
            def read(self, *a):
                raise ConnectionResetError("Verbindung weg")

            def close(self):
                pass

        fehler = urllib.error.HTTPError("http://x/", 502, "Bad Gateway", email.message.Message(), Kaputt())
        opener = mock.Mock()
        opener.open.side_effect = fehler
        with mock.patch.object(waechter.urllib.request, "build_opener", return_value=opener):
            befunde = waechter.pruefe(ziel("http://x/", json_pflicht=None))[1]
        self.assertEqual(befunde, ["HTTP 502 statt 200"])


class Zustandsdatei(unittest.TestCase):
    """main() mit beschaedigter Zustandsdatei: kein Absturz, neuer Zustand wird geschrieben."""

    def lauf(self, inhalt, ziele=None):
        with tempfile.TemporaryDirectory() as d:
            pfad = os.path.join(d, "status.json")
            if inhalt is not None:
                with open(pfad, "w") as f:
                    f.write(inhalt)
            ok = (ziele or [dict(name="A", url="https://a.example/", schwere="rot")])
            kaputt = lambda z: (z, ["HTTP 500 statt 200"], None)
            with mock.patch.object(waechter, "ZIELE", ok), \
                    mock.patch.object(waechter, "pruefe", side_effect=kaputt), \
                    mock.patch.object(waechter, "senden", return_value=True) as senden, \
                    mock.patch.object(sys, "argv", ["waechter.py", "--state", pfad]), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ex:
                    waechter.main()
            with open(pfad) as f:
                return ex.exception.code, json.load(f), senden

    def test_ohne_datei(self):
        code, neu, senden = self.lauf(None)
        self.assertEqual(code, 1)
        self.assertTrue(neu["A"]["kaputt"])
        senden.assert_called_once()

    def test_liste_statt_objekt(self):
        for inhalt in ["[]", "null", "42", '"x"', "[1, 2]"]:
            with self.subTest(inhalt=inhalt):
                code, neu, senden = self.lauf(inhalt)
                self.assertEqual(code, 1)
                self.assertTrue(neu["A"]["kaputt"])
                senden.assert_called_once()

    def test_eintrag_kein_objekt(self):
        for inhalt in ['{"A": "kaputt"}', '{"A": null}', '{"A": [1]}']:
            with self.subTest(inhalt=inhalt):
                code, neu, senden = self.lauf(inhalt)
                self.assertEqual(code, 1)
                self.assertTrue(neu["A"]["kaputt"])

    def test_unbrauchbarer_zeitstempel_meldet_erneut(self):
        for stempel in ['"gestern"', "12345", "null"]:
            with self.subTest(stempel=stempel):
                inhalt = '{"A": {"kaputt": true, "gemeldet": %s, "seit": "x"}}' % stempel
                code, neu, senden = self.lauf(inhalt)
                self.assertEqual(code, 1)
                if stempel != "null":  # null = nie gemeldet: Verhalten unveraendert (keine Erinnerung)
                    senden.assert_called_once()

    def test_zeitstempel_ohne_zeitzone(self):
        inhalt = '{"A": {"kaputt": true, "gemeldet": "2020-01-01T00:00:00", "seit": "x"}}'
        code, neu, senden = self.lauf(inhalt)
        senden.assert_called_once()

    def test_ziel_bricht_ab_andere_laufen_weiter(self):
        ziele = [dict(name="A", url="https://a.example/", schwere="rot"),
                 dict(name="B", url="https://b.example/", schwere="rot")]

        def pruefe(z):
            if z["name"] == "A":
                raise AttributeError("boom")
            return z, [], 0.1

        with tempfile.TemporaryDirectory() as d:
            pfad = os.path.join(d, "status.json")
            with mock.patch.object(waechter, "ZIELE", ziele), \
                    mock.patch.object(waechter, "pruefe", side_effect=pruefe), \
                    mock.patch.object(waechter, "senden", return_value=True) as senden, \
                    mock.patch.object(sys, "argv", ["waechter.py", "--state", pfad]), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ex:
                    waechter.main()
            with open(pfad) as f:
                neu = json.load(f)
        self.assertEqual(ex.exception.code, 1)
        self.assertEqual(neu["A"]["befunde"], ["Pruefung abgestuerzt: AttributeError: boom"])
        self.assertFalse(neu["B"]["kaputt"])
        senden.assert_called_once()


class Absturzcode(unittest.TestCase):
    def test_unbehandelter_fehler_ist_exit_3(self):
        import subprocess
        pfad = os.path.join(os.path.dirname(__file__), "..", "waechter.py")
        with open(pfad) as f:
            quelltext = f.read()
        # main() durch eine abstuerzende Variante ersetzen, den __main__-Block unveraendert lassen
        quelltext = quelltext.replace("def main():", "def main():\n    1 / 0\n\n\ndef _unbenutzt():", 1)
        r = subprocess.run([sys.executable, "-I", "-c", quelltext], capture_output=True, text=True,
                           cwd=tempfile.gettempdir())
        self.assertEqual(r.returncode, 3)
        self.assertIn("ZeroDivisionError", r.stderr)

    def test_ohne_absturz_bleibt_exit_1_bei_rot(self):
        # Gegenprobe: ein roter Befund bleibt Exit 1 (nur der Absturz ist 3)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(waechter, "ZIELE", [dict(name="A", url="https://a.example/", schwere="rot")]), \
                mock.patch.object(waechter, "pruefe", side_effect=lambda z: (z, ["rot"], None)), \
                mock.patch.object(waechter, "senden", return_value=True), \
                mock.patch.object(sys, "argv", ["waechter.py", "--state", os.path.join(d, "s.json")]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ex:
                waechter.main()
        self.assertEqual(ex.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
