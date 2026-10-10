"""Temporary HTTP/CONNECT proxy: exact approved domains, public addresses only."""
from __future__ import annotations

import ipaddress
import selectors
import socket
import socketserver
import threading
import time
from typing import cast
from urllib.parse import urlsplit

from .installers import DOWNLOAD_DOMAINS

REGISTRIES = {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
              "crates.io", "index.crates.io", "static.crates.io",
              "archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com",
              "deb.debian.org", "security.debian.org"}


def approved(host: str, port: int, domains: set[str]) -> list[tuple]:
    if host.lower() not in domains or port not in (80, 443):
        raise ValueError("destination denied")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("non-public destination denied")
    return addresses


class Proxy:
    def __init__(self, domains: set[str]):
        self.domains = domains | REGISTRIES | DOWNLOAD_DOMAINS
        self.slots = threading.BoundedSemaphore(16)
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                if not owner.slots.acquire(blocking=False):
                    return
                try:
                    self.connection.settimeout(15)
                    line = self.rfile.readline(8193)
                    if len(line) > 8192:
                        return
                    method, address, version = line.decode("ascii").strip().split(" ")
                    headers = []
                    size = 0
                    while True:
                        header = self.rfile.readline(8193)
                        size += len(header)
                        if size > 32768 or not header:
                            return
                        if header == b"\r\n":
                            break
                        headers.append(header)
                    if method == "CONNECT":
                        parsed = urlsplit("//" + address)
                    else:
                        parsed = urlsplit(address)
                        if parsed.scheme != "http" or method not in ("GET", "HEAD"):
                            raise ValueError("request denied")
                    if parsed.username or parsed.password:
                        raise ValueError("credentials denied")
                    host = parsed.hostname or ""
                    port = parsed.port or (443 if method == "CONNECT" else 80)
                    addresses = approved(host, port, owner.domains)
                    family, socktype, proto, _, endpoint = addresses[0]
                    with socket.socket(family, socktype, proto) as upstream:
                        upstream.settimeout(15)
                        upstream.connect(endpoint)
                        if method == "CONNECT":
                            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        else:
                            path = parsed.path or "/"
                            if parsed.query:
                                path += "?" + parsed.query
                            upstream.sendall(f"{method} {path} {version}\r\n".encode("ascii"))
                            upstream.sendall(f"Host: {host}\r\n".encode("ascii"))
                            for header in headers:
                                if not header.lower().startswith((b"proxy-", b"connection:", b"host:")):
                                    upstream.sendall(header)
                            upstream.sendall(b"Connection: close\r\n\r\n")
                        self.relay(self.connection, upstream)
                except (ValueError, OSError, UnicodeError):
                    try:
                        self.connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        pass
                finally:
                    owner.slots.release()

            @staticmethod
            def relay(left: socket.socket, right: socket.socket) -> None:
                # All callers use GET/HEAD or CONNECT. POST bodies are refused
                # rather than reading unbounded buffered input.
                started = time.monotonic()
                with selectors.DefaultSelector() as selector:
                    selector.register(left, selectors.EVENT_READ, right)
                    selector.register(right, selectors.EVENT_READ, left)
                    while time.monotonic() - started < 180:
                        events = selector.select(1)
                        for key, _ in events:
                            data = cast(socket.socket, key.fileobj).recv(65536)
                            if not data:
                                return
                            key.data.sendall(data)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
