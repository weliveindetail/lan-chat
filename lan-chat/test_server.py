import http.client
import json
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server as lan_chat

def sse_line(delta, finish_reason=None):
    chunk = {"id": "x", "model": "m", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        owner = self.server.owner
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        owner.requests.append((self.path, body))
        if owner.status != 200:
            self.send_response(owner.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not body["stream"]:
            data = json.dumps(owner.full_response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for item in owner.stream:
                self.wfile.write(item)
                self.wfile.flush()
                time.sleep(owner.delay)
                if owner.gate is not None:
                    owner.gate.wait(5)
        except OSError:
            owner.client_gone.set()
            return
        if owner.hold_open:
            # Keep writing until the proxy closes, proving it cancels upstream
            try:
                for _ in range(500):
                    self.wfile.write(sse_line({"content": "", "reasoning": "x"}))
                    self.wfile.flush()
                    time.sleep(0.01)
            except OSError:
                owner.client_gone.set()


class ProxyTestCase(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.delay = 0
        self.gate = None
        self.hold_open = False
        self.client_gone = threading.Event()
        self.stream = [
            sse_line({"role": "assistant", "content": "", "reasoning": "SECRET-THOUGHT"}),
            sse_line({"content": "", "reasoning_content": "SECRET-OTHER"}),
            sse_line({"content": "Hel"}),
            sse_line({"content": "lo", "reasoning": "SECRET-MIXED"}),
            sse_line({}, "stop"),
            b"data: [DONE]\n\n",
        ]
        self.full_response = {"id": "x", "choices": [{
            "message": {"role": "assistant", "content": "Hi", "reasoning": "SECRET-THOUGHT"},
            "finish_reason": "stop"}]}
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.upstream.daemon_threads = True
        self.upstream.owner = self
        self.start(self.upstream)
        self.addCleanup(self.stop, self.upstream)
        self.static = tempfile.TemporaryDirectory()
        self.addCleanup(self.static.cleanup)
        root = Path(self.static.name)
        (root / "index.html").write_text("<p>page</p>")
        (root / "assets").mkdir()
        (root / "assets" / "a.js").write_text("1")
        self.proxy = self.make_proxy()

    def start(self, httpd):
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop(self, httpd):
        httpd.shutdown()
        httpd.server_close()

    def make_proxy(self, **overrides):
        settings = dict(upstream_port=self.upstream.server_port,
                        static_dir=Path(self.static.name), client_timeout=5)
        settings.update(overrides)
        proxy = lan_chat.ChatServer(("127.0.0.1", 0), lan_chat.Config(**settings))
        self.start(proxy)
        self.addCleanup(self.stop, proxy)
        return proxy

    def call(self, method, path, body=None, headers=None, proxy=None):
        conn = http.client.HTTPConnection("127.0.0.1", (proxy or self.proxy).server_port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response, response.read()

    def chat(self, payload=None, headers=None, proxy=None):
        payload = {"messages": [{"role": "user", "content": "hi"}]} if payload is None else payload
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        merged = {"Content-Type": "application/json", **(headers or {})}
        return self.call("POST", "/api/chat", data, merged, proxy)

    def read_until(self, sock, marker):
        seen = b""
        while marker not in seen:
            part = sock.recv(4096)
            if not part:
                break
            seen += part
        return seen


class RouteTests(ProxyTestCase):
    def test_static_files_and_headers(self):
        response, body = self.call("GET", "/")
        self.assertEqual((response.status, body), (200, b"<p>page</p>"))
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        response, _ = self.call("GET", "/assets/a.js")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.getheader("Content-Type"))

    def test_unknown_routes_404(self):
        cases = [("GET", "/nope"), ("GET", "/../server.py"), ("GET", "/assets/../../etc/passwd"),
                 ("GET", "/%2e%2e/server.py"), ("GET", "/v1/models"), ("GET", "/api/tags"),
                 ("GET", "/api/chat"), ("POST", "/"), ("POST", "/api/delete"),
                 ("POST", "/v1/chat/completions"), ("DELETE", "/api/delete"),
                 ("PUT", "/api/chat"), ("OPTIONS", "/api/chat"), ("HEAD", "/")]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                response, _ = self.call(method, path)
                self.assertEqual(response.status, 404)
        self.assertEqual(self.requests, [])

    def test_symlink_not_served(self):
        link = Path(self.static.name) / "link.txt"
        link.symlink_to("/etc/hostname")
        proxy = self.make_proxy()
        response, _ = self.call("GET", "/link.txt", proxy=proxy)
        self.assertEqual(response.status, 404)


class RequestShapeTests(ProxyTestCase):
    def test_model_forced_and_fields_allowlisted(self):
        response, _ = self.chat({
            "model": "evil:latest", "max_tokens": 999999, "temperature": 9, "tools": [{"x": 1}],
            "options": {"num_ctx": 1}, "stream": True, "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi", "name": "n", "images": ["x"]}],
        })
        self.assertEqual(response.status, 200)
        path, body = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body, {"model": "qwen3.6:35b", "stream": True, "max_tokens": 8192,
                                "messages": [{"role": "user", "content": "hi"}]})

    def test_no_think_sets_reasoning_effort(self):
        proxy = self.make_proxy(think=False)
        self.chat(proxy=proxy)
        self.assertEqual(self.requests[0][1]["reasoning_effort"], "none")

    def test_invalid_payloads(self):
        message = {"role": "user", "content": "x"}
        cases = [b"not json", [], {}, {"messages": []}, {"messages": "hi"}, {"messages": ["x"]},
                 {"messages": [{"role": "system", "content": "x"}]},
                 {"messages": [{"role": "tool", "content": "x"}]},
                 {"messages": [{"role": "user", "content": [{"type": "text"}]}]},
                 {"messages": [message], "stream": "yes"}]
        for payload in cases:
            with self.subTest(payload=payload):
                response, _ = self.chat(payload)
                self.assertEqual(response.status, 400)
        self.assertEqual(self.requests, [])

    def test_deeply_nested_json_rejected(self):
        response, _ = self.chat(b"[" * 200000)
        self.assertEqual(response.status, 400)

    def test_body_cap(self):
        proxy = self.make_proxy(max_body_bytes=100)
        big = {"messages": [{"role": "user", "content": "x" * 200}]}
        response, _ = self.chat(big, proxy=proxy)
        self.assertEqual(response.status, 413)
        self.assertEqual(self.requests, [])

    def test_message_cap(self):
        proxy = self.make_proxy(max_messages=2)
        message = {"role": "user", "content": "x"}
        response, _ = self.chat({"messages": [message] * 2}, proxy=proxy)
        self.assertEqual(response.status, 200)
        response, _ = self.chat({"messages": [message] * 3}, proxy=proxy)
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.requests), 1)

    def test_content_type_and_length_required(self):
        response, _ = self.chat(headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/api/chat")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)

    def test_cross_origin_rejected(self):
        response, _ = self.chat(headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.requests, [])
        host = f"127.0.0.1:{self.proxy.server_port}"
        response, _ = self.chat(headers={"Origin": f"http://{host}"})
        self.assertEqual(response.status, 200)

    def test_busy_when_no_slots(self):
        self.proxy.chat_slots.acquire()
        self.proxy.chat_slots.acquire()
        response, _ = self.chat()
        self.assertEqual(response.status, 429)
        self.assertEqual(self.requests, [])
        self.proxy.chat_slots.release()
        self.proxy.chat_slots.release()


class StreamTests(ProxyTestCase):
    def test_stream_passthrough_strips_reasoning(self):
        response, body = self.chat()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        text = body.decode()
        self.assertNotIn("SECRET", text)
        self.assertNotIn("reasoning", text)
        events = [e[6:] for e in text.split("\n\n") if e.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(e) for e in events[:-1]]
        joined = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        self.assertEqual(joined, "Hello")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertIn(": thinking", text)
        for chunk in chunks:
            self.assertEqual(set(chunk), {"choices"})

    def test_chunks_arrive_incrementally(self):
        self.gate = threading.Event()
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        self.addCleanup(conn.close)
        self.stream = [sse_line({"content": "first"}), sse_line({"content": "second"}),
                       b"data: [DONE]\n\n"]
        conn.request("POST", "/api/chat", json.dumps({"messages": [{"role": "user", "content": "x"}]}),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        # Upstream is held at the gate, so this only passes if nothing is buffered
        self.assertIn(b"first", response.readline())
        self.gate.set()
        self.assertIn(b"second", response.read())

    def test_non_stream_strips_reasoning(self):
        response, body = self.chat({"messages": [{"role": "user", "content": "x"}], "stream": False})
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"SECRET", body)
        self.assertEqual(json.loads(body), {"choices": [{
            "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}]})

    def test_upstream_error_status_hidden(self):
        self.status = 500
        response, body = self.chat()
        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(body), {"error": "Model server returned an error"})

    def test_upstream_down(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        proxy = self.make_proxy(upstream_port=free_port)
        response, body = self.chat(proxy=proxy)
        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(body), {"error": "Model server unavailable"})
        self.assertTrue(proxy.chat_slots.acquire(blocking=False), "slot must be released")

    def test_stream_error_chunk_hides_details(self):
        self.stream = [b'data: {"error":{"message":"/private/path failed"}}\n\n']
        _, body = self.chat()
        self.assertNotIn(b"/private/path", body)
        self.assertIn(b"Model server returned an error", body)

    def test_stream_ending_without_done_reports_error(self):
        self.stream = [sse_line({"content": "partial"})]
        _, body = self.chat()
        self.assertIn(b"partial", body)
        self.assertIn(b"ended early", body)

    def test_upstream_timeout_reports_error(self):
        proxy = self.make_proxy(upstream_timeout=0.2)
        self.stream = [sse_line({"content": "partial"})]
        self.delay = 1
        _, body = self.chat(proxy=proxy)
        self.assertIn(b"stopped responding", body)
        self.assertTrue(proxy.chat_slots.acquire(blocking=False), "slot must be released")

    def test_client_disconnect_cancels_upstream_and_frees_slot(self):
        self.hold_open = True
        self.stream = [sse_line({"content": "first"})]
        with mock.patch.object(lan_chat, "THINKING_PING_SECONDS", 0):
            sock = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=5)
            data = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
            headers = {"Content-Type": "application/json", "Content-Length": str(len(data)),
                       "Host": "x"}
            head = "POST /api/chat HTTP/1.1\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
            sock.sendall(head.encode() + data)
            self.assertIn(b"first", self.read_until(sock, b"first"))
            sock.close()
            self.assertTrue(self.client_gone.wait(5), "upstream was not cancelled")
        for _ in range(100):
            if self.proxy.chat_slots.acquire(blocking=False):
                self.proxy.chat_slots.release()
                return
            time.sleep(0.05)
        self.fail("slot was not released")


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class TlsTests(ProxyTestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.cert = Path(cls.folder.name) / "cert.pem"
        cls.key = Path(cls.folder.name) / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-days", "2", "-keyout", cls.key, "-out", cls.cert, "-subj", "/CN=lan-chat",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True)
        cls.key.chmod(0o600)

    def setUp(self):
        super().setUp()
        self.proxy = self.make_proxy(tls_context=lan_chat.load_tls_context(self.cert, self.key))
        self.client_tls = ssl.create_default_context(cafile=self.cert)

    def https(self, method="GET", path="/", body=None, headers=None):
        conn = http.client.HTTPSConnection("127.0.0.1", self.proxy.server_port, timeout=5,
                                           context=self.client_tls)
        self.addCleanup(conn.close)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response, response.read()

    def test_plain_listener_shares_chat_slots(self):
        settings = lan_chat.Config(upstream_port=self.upstream.server_port,
                                   static_dir=Path(self.static.name), client_timeout=5)
        plain = lan_chat.ChatServer(("127.0.0.1", 0), settings, chat_slots=self.proxy.chat_slots)
        self.start(plain)
        self.addCleanup(self.stop, plain)
        self.assertEqual(self.call("GET", "/", proxy=plain)[0].status, 200)
        data = json.dumps({"messages": [{"role": "user", "content": "Hi"}]})
        headers = {"Content-Type": "application/json"}
        for _ in range(2):
            self.proxy.chat_slots.acquire()
            self.addCleanup(self.proxy.chat_slots.release)
        self.assertEqual(self.call("POST", "/api/chat", data, headers, proxy=plain)[0].status, 429)

    def test_page_served_over_tls(self):
        response, body = self.https()
        self.assertEqual((response.status, body), (200, b"<p>page</p>"))

    def test_stream_over_tls(self):
        data = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
        response, body = self.https("POST", "/api/chat", data, {"Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        self.assertIn(b"data: [DONE]", body)
        self.assertNotIn(b"SECRET", body)

    def test_plain_http_is_refused_and_server_survives(self):
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.call("GET", "/")
        self.assertEqual(self.https()[0].status, 200)

    def test_untrusted_client_rejected_and_server_survives(self):
        with socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=5) as sock:
            with self.assertRaises(ssl.SSLCertVerificationError):
                ssl.create_default_context().wrap_socket(sock, server_hostname="127.0.0.1")
        self.assertEqual(self.https()[0].status, 200)

    def test_stalled_handshake_does_not_block_others(self):
        with socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=5):
            self.assertEqual(self.https()[0].status, 200)

    def test_client_disconnect_cancels_upstream_and_frees_slot(self):
        self.hold_open = True
        self.stream = [sse_line({"content": "first"})]
        with mock.patch.object(lan_chat, "THINKING_PING_SECONDS", 0):
            raw = socket.create_connection(("127.0.0.1", self.proxy.server_port), timeout=5)
            sock = self.client_tls.wrap_socket(raw, server_hostname="127.0.0.1")
            data = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
            head = ("POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n")
            sock.sendall(head.encode() + data)
            self.assertIn(b"first", self.read_until(sock, b"first"))
            sock.close()
            self.assertTrue(self.client_gone.wait(5), "upstream was not cancelled")
        for _ in range(100):
            if self.proxy.chat_slots.acquire(blocking=False):
                self.proxy.chat_slots.release()
                return
            time.sleep(0.05)
        self.fail("slot was not released")

    def test_readable_key_refused(self):
        self.key.chmod(0o640)
        self.addCleanup(self.key.chmod, 0o600)
        with self.assertRaises(SystemExit):
            lan_chat.load_tls_context(self.cert, self.key)


class ArgumentTests(unittest.TestCase):
    def test_default_bind_is_loopback(self):
        self.assertEqual(lan_chat.parse_args([]).host, "127.0.0.1")

    def test_http_port_needs_tls(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            lan_chat.parse_args(["--http-port", "8081"])
        args = lan_chat.parse_args(["--tls-cert", "c", "--tls-key", "k", "--http-port", "8081"])
        self.assertEqual(args.http_port, 8081)

    def test_cert_and_key_needed_together(self):
        for argv in (["--tls-cert", "c"], ["--tls-key", "k"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    lan_chat.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
