#!/usr/bin/env python3
import argparse
import http.client
import json
import mimetypes
import ssl
import stat
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MODEL = "qwen3.6:35b"
UPSTREAM_PATH = "/v1/chat/completions"
ALLOWED_ROLES = {"user", "assistant"}
MAX_LINE_BYTES = 1024 * 1024
THINKING_PING_SECONDS = 1.0

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}


@dataclass
class Config:
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 11434
    static_dir: Path = Path(__file__).resolve().parent / "static"
    max_body_bytes: int = 1024 * 1024
    max_messages: int = 200
    max_tokens: int = 8192
    # Per-read limit, since a cold model load or long reasoning can pause output
    upstream_timeout: float = 300.0
    client_timeout: float = 30.0
    max_active_chats: int = 2
    think: bool = True
    tls_context: ssl.SSLContext | None = None


class BadRequest(Exception):
    pass


def static_routes(static_dir):
    # Fixed at startup so request paths never touch the filesystem
    routes = {}
    for path in sorted(Path(static_dir).rglob("*")):
        if path.is_file() and not path.is_symlink():
            routes["/" + path.relative_to(static_dir).as_posix()] = path
    if "/index.html" in routes:
        routes["/"] = routes["/index.html"]
    return routes


def content_type(path):
    known = CONTENT_TYPES.get(path.suffix.lower())
    return known or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def build_upstream_body(payload, config):
    if not isinstance(payload, dict):
        raise BadRequest("Body must be a JSON object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BadRequest("messages must be a non-empty list")
    if len(messages) > config.max_messages:
        raise BadRequest(f"Too many messages (limit {config.max_messages})")
    clean = []
    for message in messages:
        if not isinstance(message, dict):
            raise BadRequest("Each message must be an object")
        role, text = message.get("role"), message.get("content")
        if role not in ALLOWED_ROLES:
            raise BadRequest("Message role must be user or assistant")
        if not isinstance(text, str):
            raise BadRequest("Message content must be a string")
        clean.append({"role": role, "content": text})
    stream = payload.get("stream", True)
    if not isinstance(stream, bool):
        raise BadRequest("stream must be true or false")
    body = {
        "model": MODEL,
        "messages": clean,
        "stream": stream,
        "max_tokens": config.max_tokens,
    }
    if not config.think:
        body["reasoning_effort"] = "none"
    return body


def public_chunk(content, finish_reason):
    # Rebuilt field by field so reasoning can't leak through any field name
    delta = {"content": content} if content else {}
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


def sse(obj):
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config):
        super().__init__(address, Handler)
        self.config = config
        if config.tls_context:
            # Handshake later, in the connection's thread, so a stalled client can't block accept
            self.socket = config.tls_context.wrap_socket(
                self.socket, server_side=True, do_handshake_on_connect=False)
        self.routes = static_routes(config.static_dir)
        self.chat_slots = threading.BoundedSemaphore(config.max_active_chats)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lan-chat"
    sys_version = ""
    disable_nagle_algorithm = True

    def setup(self):
        self.timeout = self.server.config.client_timeout
        super().setup()

    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, ConnectionError) as error:
            # Routine with a self-signed cert: browsers abort the handshake until it's accepted
            self.close_connection = True
            self.log_message("connection failed: %s", type(error).__name__)

    def log_message(self, format, *args):
        # Request line and status only, so prompts stay out of logs
        sys.stderr.write(f"{self.client_address[0]} {format % args}\n")

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def send_json(self, status, obj, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def fail(self, status, message, headers=None):
        # Any unread body would be parsed as the next request, so never reuse the connection
        self.close_connection = True
        self.send_json(status, {"error": message}, {"Connection": "close", **(headers or {})})

    def request_path(self):
        return urlsplit(self.path).path

    def do_GET(self):
        path = self.server.routes.get(self.request_path())
        if path is None:
            self.fail(404, "Not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type(path))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.request_path() != "/api/chat":
            self.fail(404, "Not found")
            return
        self.handle_chat()

    def reject_method(self):
        self.fail(404, "Not found")

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = reject_method

    def same_origin(self):
        # Stops other sites using the model through a visitor's browser
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return urlsplit(origin).netloc == self.headers.get("Host")

    def read_payload(self):
        media_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            self.fail(415, "Content-Type must be application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.fail(411, "Content-Length required")
            return None
        if length < 0 or length > self.server.config.max_body_bytes:
            self.fail(413, f"Body too large (limit {self.server.config.max_body_bytes} bytes)")
            return None
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            self.close_connection = True
            return None
        if len(raw) < length:
            self.close_connection = True
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            self.fail(400, "Body is not valid JSON")
            return None

    def handle_chat(self):
        if not self.same_origin():
            self.fail(403, "Cross-origin request rejected")
            return
        payload = self.read_payload()
        if payload is None:
            return
        try:
            body = build_upstream_body(payload, self.server.config)
        except BadRequest as error:
            self.fail(400, str(error))
            return
        if not self.server.chat_slots.acquire(blocking=False):
            self.fail(429, "Server is busy, try again shortly")
            return
        config = self.server.config
        upstream = http.client.HTTPConnection(
            config.upstream_host, config.upstream_port, timeout=config.upstream_timeout)
        try:
            self.proxy(upstream, body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError,
                ssl.SSLError):
            # Client left or stalled. Closing upstream below makes Ollama stop generating.
            self.close_connection = True
            self.log_message("client disconnected during chat")
        finally:
            upstream.close()
            self.server.chat_slots.release()

    def proxy(self, upstream, body):
        try:
            upstream.request("POST", UPSTREAM_PATH, json.dumps(body),
                             {"Content-Type": "application/json"})
            response = upstream.getresponse()
        except (OSError, http.client.HTTPException) as error:
            self.log_message("upstream unavailable: %s", type(error).__name__)
            self.fail(502, "Model server unavailable")
            return
        if response.status != 200:
            self.log_message("upstream status %d", response.status)
            self.fail(502, "Model server returned an error")
            return
        if body["stream"]:
            self.relay_stream(response)
        else:
            self.relay_full(response)

    def relay_full(self, response):
        try:
            data = json.loads(response.read())
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError,
                TypeError, AttributeError):
            self.fail(502, "Bad response from model server")
            return
        self.send_json(200, {"choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }]})

    def relay_stream(self, response):
        # No Content-Length, so the end of the stream is marked by closing the connection
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_ping = 0.0
        try:
            while True:
                line = response.readline(MAX_LINE_BYTES)
                if not line:
                    self.wfile.write(sse({"error": "Model stream ended early"}))
                    return
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    self.log_message("upstream stream error")
                    self.wfile.write(sse({"error": "Model server returned an error"}))
                    return
                content, finish_reason, thinking = parse_chunk(chunk)
                if content or finish_reason:
                    self.wfile.write(sse(public_chunk(content, finish_reason)))
                elif thinking and time.monotonic() - last_ping >= THINKING_PING_SECONDS:
                    # Text-free ping: shows progress and reveals a client that has left
                    last_ping = time.monotonic()
                    self.wfile.write(b": thinking\n\n")
        except (TimeoutError, http.client.HTTPException) as error:
            self.log_message("upstream read failed: %s", type(error).__name__)
            self.wfile.write(sse({"error": "Model server stopped responding"}))


def parse_chunk(chunk):
    try:
        choice = chunk["choices"][0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        thinking = bool(delta.get("reasoning") or delta.get("reasoning_content"))
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return "", None, False
    return content if isinstance(content, str) else "", finish_reason, thinking


def load_tls_context(cert_file, key_file):
    if stat.S_IMODE(Path(key_file).stat().st_mode) & 0o077:
        raise SystemExit(f"{key_file} must not be accessible by group or others (use chmod 600)")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Chat proxy for Ollama")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tls-cert", help="certificate file, turns on HTTPS")
    parser.add_argument("--tls-key", help="private key file, required with --tls-cert")
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=11434)
    parser.add_argument("--static-dir", type=Path, default=Config.static_dir)
    parser.add_argument("--max-tokens", type=int, default=Config.max_tokens)
    parser.add_argument("--upstream-timeout", type=float, default=Config.upstream_timeout)
    parser.add_argument("--max-active-chats", type=int, default=Config.max_active_chats)
    parser.add_argument("--no-think", action="store_true", help="turn off model reasoning")
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be used together")
    return args


def main():
    args = parse_args()
    config = Config(
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
        static_dir=args.static_dir,
        max_tokens=args.max_tokens,
        upstream_timeout=args.upstream_timeout,
        max_active_chats=args.max_active_chats,
        think=not args.no_think,
        tls_context=load_tls_context(args.tls_cert, args.tls_key) if args.tls_cert else None,
    )
    server = ChatServer((args.host, args.port), config)
    scheme = "https" if args.tls_cert else "http"
    print(f"lan-chat on {scheme}://{args.host}:{server.server_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
