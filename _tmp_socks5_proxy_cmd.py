#!/usr/bin/env python3
"""Minimal SOCKS5 ProxyCommand for ssh: socks5_proxy_cmd.py HOST PORT"""
from __future__ import annotations

import socket
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: socks5_proxy_cmd.py HOST PORT", file=sys.stderr)
        return 2
    host, port_s = sys.argv[1], sys.argv[2]
    port = int(port_s)
    proxy = ("127.0.0.1", 1080)
    sock = socket.create_connection(proxy, timeout=30)
    # greeting: ver=5, nmethods=1, method=0 (no auth)
    sock.sendall(b"\x05\x01\x00")
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
        print(f"socks greeting failed: {resp!r}", file=sys.stderr)
        return 1
    req = bytearray(b"\x05\x01\x00\x03")
    hb = host.encode()
    req.append(len(hb))
    req.extend(hb)
    req.extend(port.to_bytes(2, "big"))
    sock.sendall(req)
    hdr = sock.recv(4)
    if len(hdr) < 4 or hdr[1] != 0:
        print(f"socks connect failed: {hdr!r}", file=sys.stderr)
        return 1
    atyp = hdr[3]
    if atyp == 1:
        sock.recv(4 + 2)
    elif atyp == 3:
        ln = sock.recv(1)[0]
        sock.recv(ln + 2)
    elif atyp == 4:
        sock.recv(16 + 2)
    else:
        print(f"socks bad atyp {atyp}", file=sys.stderr)
        return 1

    import selectors

    sock.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)
    sel.register(sys.stdin.buffer, selectors.EVENT_READ)
    while True:
        for key, _ in sel.select():
            if key.fileobj is sock:
                data = sock.recv(65536)
                if not data:
                    return 0
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            else:
                data = sys.stdin.buffer.read1(65536)
                if not data:
                    return 0
                sock.sendall(data)


if __name__ == "__main__":
    raise SystemExit(main())
