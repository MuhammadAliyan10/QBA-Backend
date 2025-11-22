#!/usr/bin/env python3
import http.server
import socketserver
import os

PORT = 8888
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

class MyHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

with socketserver.TCPServer(("", PORT), MyHTTPRequestHandler) as httpd:
    print(f"🌐 Test HTTP Server running at http://localhost:{PORT}/")
    print(f"📄 Serving: {DIRECTORY}")
    print(f"🔗 Test page: http://localhost:{PORT}/test_page.html")
    httpd.serve_forever()
