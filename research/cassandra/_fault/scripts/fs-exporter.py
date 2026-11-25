#!/usr/bin/env python3
"""Tiny Prometheus exporter for the Cassandra data filesystem.

cAdvisor cannot attribute Docker Desktop tmpfs/volume usage to a container, so
the measurement must be made from inside the Cassandra mount namespace.
"""

import argparse
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def prometheus_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class MetricsHandler(BaseHTTPRequestHandler):
    path_to_measure = "/var/lib/cassandra"
    node = socket.gethostname()

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path not in ("/", "/metrics"):
            self.send_error(404)
            return

        try:
            stat = os.statvfs(self.path_to_measure)
            size = stat.f_blocks * stat.f_frsize
            free = stat.f_bfree * stat.f_frsize
            available = stat.f_bavail * stat.f_frsize
            used = size - free
            ratio = used / size if size else 0.0
            label = (
                f'node="{prometheus_escape(self.node)}",'
                f'path="{prometheus_escape(self.path_to_measure)}"'
            )
            body = (
                "# HELP cassandra_data_filesystem_size_bytes Total size of the Cassandra data filesystem.\n"
                "# TYPE cassandra_data_filesystem_size_bytes gauge\n"
                f"cassandra_data_filesystem_size_bytes{{{label}}} {size}\n"
                "# HELP cassandra_data_filesystem_used_bytes Used bytes on the Cassandra data filesystem.\n"
                "# TYPE cassandra_data_filesystem_used_bytes gauge\n"
                f"cassandra_data_filesystem_used_bytes{{{label}}} {used}\n"
                "# HELP cassandra_data_filesystem_available_bytes Bytes available on the Cassandra data filesystem.\n"
                "# TYPE cassandra_data_filesystem_available_bytes gauge\n"
                f"cassandra_data_filesystem_available_bytes{{{label}}} {available}\n"
                "# HELP cassandra_data_filesystem_used_ratio Used fraction of the Cassandra data filesystem.\n"
                "# TYPE cassandra_data_filesystem_used_ratio gauge\n"
                f"cassandra_data_filesystem_used_ratio{{{label}}} {ratio:.9f}\n"
            ).encode("utf-8")
        except OSError as exc:
            body = f"# filesystem exporter error: {exc}\n".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="/var/lib/cassandra")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--node", default=socket.gethostname())
    args = parser.parse_args()

    MetricsHandler.path_to_measure = args.path
    MetricsHandler.node = args.node
    ThreadingHTTPServer(("0.0.0.0", args.port), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
