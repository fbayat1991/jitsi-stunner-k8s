#!/usr/bin/env python3
"""Send a STUN Binding Request and report whether STUNner answered.

No credentials needed. Answers the first question when debugging:
does UDP reach STUNner at all (edge NAT, firewall, LoadBalancer)?

Usage:
    python3 debug/stun-ping.py <PUBLIC_IP> [port]
"""
import os
import socket
import sys

if len(sys.argv) < 2:
    sys.exit(f"usage: {sys.argv[0]} <host> [port]")

host = sys.argv[1]
port = int(sys.argv[2]) if len(sys.argv) > 2 else 3478

# Binding Request: type 0x0001, length 0, magic cookie 0x2112A442, 12-byte transaction ID
request = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + os.urandom(12)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(request, (host, port))
try:
    data, addr = s.recvfrom(2048)
    print(f"OK: got {len(data)} bytes from {addr[0]}:{addr[1]} - UDP reaches STUNner")
except socket.timeout:
    print("TIMEOUT: no answer - UDP does not reach STUNner (NAT, firewall, LoadBalancer or ISP)")
    sys.exit(1)
