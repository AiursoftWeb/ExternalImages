#!/usr/bin/env python3
"""Observe local HTTP mocks; never connects to production."""
import argparse
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from test_monitor import Mock

p = argparse.ArgumentParser()
p.add_argument('--interval', type=float, default=300)
p.add_argument('--cycles', type=int, default=3)
a = p.parse_args()
if a.interval < 0 or a.cycles < 1:
    p.error('interval must be nonnegative and cycles positive')
server = ThreadingHTTPServer(('127.0.0.1', 0), Mock)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
url = 'http://127.0.0.1:' + str(server.server_port)
try:
    with tempfile.TemporaryDirectory() as directory:
        store = main.Store(directory, 'mock-node', 'AspNetCoreLogs')
        monitor = main.Monitor(main.ClickHouse(url, 'AspNetCoreLogs', 'default', ''), main.AlertCenter(url, 'mock-key'), store, 'mock-node', 'AspNetCoreLogs')
        try:
            for cycle in range(1, a.cycles + 1):
                start = time.monotonic()
                failed = monitor.scan()
                elapsed = time.monotonic() - start
                print(f'MOCK cycle={cycle} tables={len(Mock.tables)} failed={failed} seconds={elapsed:.3f}', flush=True)
                if failed:
                    raise SystemExit(1)
                if cycle < a.cycles:
                    time.sleep(max(0, a.interval - elapsed))
        finally:
            store.close()
finally:
    server.shutdown()
    server.server_close()
    thread.join()
