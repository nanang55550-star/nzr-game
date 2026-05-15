#!/usr/bin/env python3
"""
Tetris Dark Mode - Local Server
Cara pakai: python3 server.py
"""
import http.server
import socketserver
import os

PORT = 8080
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

os.chdir(DIRECTORY)

print("=" * 45)
print("🎮  TETRIS DARK MODE SERVER")
print("=" * 45)
print(f"📁  Folder : {DIRECTORY}")
print(f"🌐  Port   : {PORT}")
print("-" * 45)
print("✅  Server berjalan!")
print(f"👉  Buka di browser: http://localhost:{PORT}/")
print("-" * 45)
print("⚠️  Tekan Ctrl+C untuk menghentikan server")
print("=" * 45)

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("

🛑 Server dihentikan.")
