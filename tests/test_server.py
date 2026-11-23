"""Tests for the long line.  Standard library only:  python3 -m unittest -v"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from longline.server import Store, make_handler
from http.server import ThreadingHTTPServer


class Server:
    def __init__(self, db_path):
        self.store = Store(db_path)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def append(self, number, handle, line, key):
        return self.request("POST", "/line", {
            "line_number": number, "handle": handle, "line": line,
            "idempotency_key": key,
        })


class LongLineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.dir.name, "test.db")
        self.srv = Server(self.db_path)

    def tearDown(self):
        self.srv.close()
        self.dir.cleanup()

    def test_first_write(self):
        status, row = self.srv.append(1, "first", "The line begins.", "k1")
        self.assertEqual(status, 201)
        self.assertEqual(row["line_number"], 1)
        self.assertFalse(row["duplicate"])
        status, latest = self.srv.request("GET", "/line")
        self.assertEqual(status, 200)
        self.assertEqual(latest["line"], "The line begins.")
        self.assertEqual(latest["handle"], "first")
        self.assertFalse(latest["continued"])

    def test_empty_line_reads_as_unstarted(self):
        status, latest = self.srv.request("GET", "/line")
        self.assertEqual(status, 200)
        self.assertIsNone(latest["line"])
        self.assertTrue(latest["empty"])

    def test_reader_sees_only_the_last_line(self):
        self.srv.append(1, "a", "one", "k1")
        self.srv.append(2, "b", "two", "k2")
        status, latest = self.srv.request("GET", "/line")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(latest), {"line_number", "handle", "line", "at", "continued"}
        )
        self.assertEqual(latest["line_number"], 2)
        self.assertNotIn("lines", latest)

    def test_wrong_line_number_conflicts_and_returns_latest(self):
        self.srv.append(1, "a", "one", "k1")
        status, body = self.srv.append(3, "b", "three", "k2")
        self.assertEqual(status, 409)
        self.assertEqual(body["latest"]["line_number"], 1)
        status, body = self.srv.append(1, "b", "again one", "k3")
        self.assertEqual(status, 409)

    def test_idempotent_retry_appends_nothing(self):
        status, first = self.srv.append(1, "a", "one", "same-key")
        self.assertEqual(status, 201)
        status, again = self.srv.append(1, "a", "one", "same-key")
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["line_number"], first["line_number"])
        self.assertEqual(self.srv.store.count(), 1)

    def test_concurrent_appends_exactly_one_wins(self):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def writer(i):
            barrier.wait()
            status, body = self.srv.append(1, "w%d" % i, "line %d" % i, "key-%d" % i)
            with lock:
                results.append(status)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201] + [409] * 7)
        self.assertEqual(self.srv.store.count(), 1)

    def test_bounds(self):
        cases = [
            (1, "x" * 33, "line", "k"),          # handle too long
            (1, "", "line", "k"),                # handle empty
            (1, "a", "", "k"),                   # line empty
            (1, "a", "   ", "k"),                # line blank
            (1, "a", "x" * 281, "k"),            # line too long
            (1, "a", "two\nlines", "k"),         # newline
            (1, "a", "line", ""),                # key empty
            (1, "a", "line", "k" * 65),          # key too long
        ]
        for case in cases:
            with self.subTest(case=case):
                status, _ = self.srv.append(*case)
                self.assertEqual(status, 400)

    def test_restart_persistence(self):
        self.srv.append(1, "a", "one", "k1")
        self.srv.append(2, "b", "two", "k2")
        self.srv.close()
        self.srv = Server(self.db_path)
        status, latest = self.srv.request("GET", "/line")
        self.assertEqual(status, 200)
        self.assertEqual(latest["line_number"], 2)
        self.assertEqual(latest["line"], "two")

    def test_continuation_is_visible_in_one_call(self):
        self.srv.append(1, "a", "one", "k1")
        status, row = self.srv.request("GET", "/line/1")
        self.assertEqual(status, 200)
        self.assertFalse(row["continued"])
        self.assertEqual(row["lines_after"], 0)
        self.srv.append(2, "b", "two", "k2")
        status, row = self.srv.request("GET", "/line/1")
        self.assertEqual(status, 200)
        self.assertTrue(row["continued"])
        self.assertEqual(row["lines_after"], 1)
        status, row = self.srv.request("GET", "/line/2")
        self.assertFalse(row["continued"])

    def test_unknown_line_is_404(self):
        status, body = self.srv.request("GET", "/line/7")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_html_page_shows_the_chain(self):
        self.srv.append(1, "a", "<one>", "k1")
        req = urllib.request.Request(self.srv.base + "/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            page = resp.read().decode()
        self.assertIn("&lt;one&gt;", page)
        self.assertIn("The long line", page)

    def test_bad_json_is_400(self):
        req = urllib.request.Request(self.srv.base + "/line", data=b"not json", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("accepted bad JSON")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
