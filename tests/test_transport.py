import http.server
import json
import pathlib
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
import transport  # noqa: E402


class Handler(http.server.BaseHTTPRequestHandler):
    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/missing":
            self._reply(404, {"error": "missing"})
            return
        self._reply(200, {"host": self.headers.get("Host"), "path": self.path,
                          "auth": self.headers.get("Authorization")})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode()
        self._reply(201, {"form": data, "type": self.headers.get("Content-Type")})

    def log_message(self, *arguments) -> None:
        pass


def openssl(*arguments: str) -> None:
    subprocess.run(["openssl", *arguments], check=True, capture_output=True)


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(cls.temporary.name)
        cls.ca = base / "ca.crt"
        openssl("genrsa", "-out", str(base / "ca.key"), "2048")
        openssl("req", "-x509", "-new", "-key", str(base / "ca.key"), "-subj", "/CN=Test CA",
                "-days", "2", "-out", str(cls.ca))
        (base / "ext").write_text("subjectAltName=DNS:jenkins.example.test\n")
        openssl("genrsa", "-out", str(base / "server.key"), "2048")
        openssl("req", "-new", "-key", str(base / "server.key"), "-subj", "/CN=jenkins.example.test",
                "-out", str(base / "server.csr"))
        openssl("x509", "-req", "-in", str(base / "server.csr"), "-CA", str(cls.ca), "-CAkey",
                str(base / "ca.key"), "-CAcreateserial", "-days", "2", "-extfile", str(base / "ext"),
                "-out", str(base / "server.crt"))
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(base / "server.crt"), str(base / "server.key"))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.temporary.cleanup()

    def client(self, host: str = "jenkins.example.test", **kwargs) -> transport.UrlTransport:
        return transport.UrlTransport(f"https://{host}:{self.port}", str(self.ca), resolve="127.0.0.1", **kwargs)

    def test_resolve_connects_to_address_but_keeps_canonical_host(self) -> None:
        status, payload, headers = self.client().request("GET", "/crumbIssuer/api/json")
        self.assertEqual(200, status)
        self.assertEqual(f"jenkins.example.test:{self.port}", payload["host"])
        self.assertEqual("/crumbIssuer/api/json", payload["path"])
        self.assertIn("content-type", {key.lower() for key in headers})

    def test_certificate_is_validated_against_the_canonical_name(self) -> None:
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.client(host="other.example.test").request("GET", "/")

    def test_unexpected_status_raises_with_method_and_path(self) -> None:
        with self.assertRaisesRegex(RuntimeError, r"GET /missing returned HTTP 404"):
            self.client().request("GET", "/missing")
        status, payload, _ = self.client().request("GET", "/missing", expected=(404,))
        self.assertEqual((404, "missing"), (status, payload["error"]))

    def test_form_fields_and_raw_bodies(self) -> None:
        _, payload, _ = self.client().request("POST", "/x", {"a": "1", "b[]": ["2", "3"]}, expected=(201,))
        self.assertEqual("a=1&b%5B%5D=2&b%5B%5D=3", payload["form"])
        self.assertEqual("application/x-www-form-urlencoded", payload["type"])
        _, payload, _ = self.client().request("POST", "/x", body="<a/>", expected=(201,))
        self.assertEqual(("<a/>", "application/xml"), (payload["form"], payload["type"]))

    def test_prefix_path_and_headers_are_applied(self) -> None:
        client = transport.UrlTransport(
            f"https://jenkins.example.test:{self.port}/api/v4/", str(self.ca),
            headers=transport.basic_auth_headers("admin", "pw"), resolve="127.0.0.1",
        )
        _, payload, _ = client.request("GET", "/user")
        self.assertEqual("/api/v4/user", payload["path"])
        self.assertEqual("Basic YWRtaW46cHc=", payload["auth"])


class OriginTests(unittest.TestCase):
    def test_validate_https_origin(self) -> None:
        transport.validate_https_origin("https://gitlab.devops.test", "gitlab.devops.test")
        transport.validate_https_origin("https://gitlab.devops.test:8443", "gitlab.devops.test")
        for bad in ("http://gitlab.devops.test", "https://evil.test", "https://gitlab.devops.test:0",
                    "https://gitlab.devops.test/path", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                transport.validate_https_origin(bad, "gitlab.devops.test")


if __name__ == "__main__":
    unittest.main()
