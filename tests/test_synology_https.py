import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from beetsplug._synology import AudioStation, SynologyError


class AudioStationHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.path, self.headers["Host"], parse_qs(body.decode())))
        if self.server.redirect:
            self.send_response(307)
            self.send_header("Location", "https://other.example.test/")
            self.end_headers()
            return
        if self.path.endswith("query.cgi"):
            data = {"SYNO.API.Auth": {"path": "auth.cgi", "minVersion": 1, "maxVersion": 7}}
        elif parse_qs(body.decode()).get("method") == ["login"]:
            data = {"sid": "test-session"}
        else:
            data = {}
        payload = json.dumps({"success": True, "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required for local TLS tests")
class LocalHTTPSTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.ca = os.path.join(cls.temp.name, "ca.pem")
        ca_key = os.path.join(cls.temp.name, "ca.key")
        cls.key = os.path.join(cls.temp.name, "server.key")
        csr = os.path.join(cls.temp.name, "server.csr")

        def openssl(*args):
            subprocess.run(
                ["openssl", *args], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )

        openssl(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=Synologysync Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-keyout", ca_key, "-out", cls.ca,
        )
        openssl(
            "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-subj", "/CN=nas.example.test", "-keyout", cls.key, "-out", csr,
        )
        for name, san in (("dns", "DNS:nas.example.test"), ("ip", "IP:127.0.0.1")):
            extensions = os.path.join(cls.temp.name, name + ".conf")
            cert = os.path.join(cls.temp.name, name + ".pem")
            with open(extensions, "w") as stream:
                stream.write(
                    "basicConstraints=critical,CA:FALSE\n"
                    "keyUsage=critical,digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage=serverAuth\n"
                    f"subjectAltName={san}\n"
                )
            openssl(
                "x509", "-req", "-in", csr, "-CA", cls.ca, "-CAkey", ca_key,
                "-set_serial", "1" if name == "dns" else "2",
                "-days", "2", "-extfile", extensions, "-out", cert,
            )
            setattr(cls, name + "_cert", cert)

    def start_server(self, cert=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), AudioStationHandler)
        server.requests = []
        server.redirect = False
        server.sni = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert or self.dns_cert, self.key)
        context.set_servername_callback(
            lambda sock, name, ctx: server.sni.append(name)
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            thread.join()
            server.server_close()

        self.addCleanup(stop_server)
        return server, f"https://127.0.0.1:{server.server_port}"

    def test_public_hostname_validates_certificate_over_local_ip(self):
        server, url = self.start_server()
        client = AudioStation(
            url, "test-user", "test-password", ca_file=self.ca,
            public_url="https://nas.example.test:5001", timeout=2,
        )
        client.close()
        self.assertTrue(server.sni)
        self.assertTrue(all(name == "nas.example.test" for name in server.sni))
        self.assertEqual([request[0] for request in server.requests],
                         ["/webapi/query.cgi", "/webapi/auth.cgi", "/webapi/auth.cgi"])
        self.assertTrue(all(request[1] == "nas.example.test:5001"
                            for request in server.requests))
        self.assertEqual(server.requests[1][2]["passwd"], ["test-password"])
        self.assertEqual(server.requests[2][2]["method"], ["logout"])
        self.assertEqual(server.requests[2][2]["_sid"], ["test-session"])

    def test_wrong_public_hostname_is_rejected_before_http(self):
        server, url = self.start_server()
        with self.assertRaises(SynologyError):
            AudioStation(url, "test-user", "test-password", ca_file=self.ca,
                         public_url="https://wrong.example.test", timeout=2)
        self.assertIn("wrong.example.test", server.sni)
        self.assertEqual(server.requests, [])

    def test_untrusted_ca_is_rejected_before_http(self):
        server, url = self.start_server()
        with self.assertRaises(SynologyError):
            AudioStation(url, "test-user", "test-password",
                         public_url="https://nas.example.test", timeout=2)
        self.assertEqual(server.requests, [])

    def test_without_public_url_ip_mismatch_is_still_rejected(self):
        server, url = self.start_server()
        with self.assertRaises(SynologyError):
            AudioStation(url, "test-user", "test-password", ca_file=self.ca, timeout=2)
        self.assertEqual(server.requests, [])

    def test_without_public_url_matching_ip_certificate_works(self):
        server, url = self.start_server(self.ip_cert)
        client = AudioStation(url, "test-user", "test-password",
                              ca_file=self.ca, timeout=2)
        client.close()
        self.assertTrue(all(request[1] == f"127.0.0.1:{server.server_port}"
                            for request in server.requests))

    def test_https_redirect_is_not_followed(self):
        server, url = self.start_server()
        server.redirect = True
        with self.assertRaisesRegex(SynologyError, "redirects are not allowed"):
            AudioStation(url, "test-user", "test-password", ca_file=self.ca,
                         public_url="https://nas.example.test", timeout=2)
        self.assertEqual(len(server.requests), 1)
        self.assertNotIn("passwd", server.requests[0][2])
