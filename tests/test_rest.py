"""Офлайн-тесты REST-клиента: URL-кодирование, маппинг методов, ошибки HTTP."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_mod
from main import DeviceError, RouterOsRest
from tests.util import make_device


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        if text:
            self.text = text
            self.content = text.encode("utf-8")
        elif json_data is not None:
            self.text = json.dumps(json_data)
            self.content = self.text.encode("utf-8")
        else:
            self.text = ""
            self.content = b""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    def __init__(self):
        self.responses: list = []
        self.calls: list[dict] = []
        self.auth = None
        self.verify = None
        self.headers: dict = {}

    def request(self, method, url, json=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "json": json, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"неожиданный запрос: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeExceptions:
    class RequestException(Exception):
        pass


class _FakeRequests:
    exceptions = _FakeExceptions()
    Session = _FakeSession


class RouterOsRestTest(unittest.TestCase):
    def setUp(self):
        self._real_requests = main_mod.requests
        main_mod.requests = _FakeRequests()

    def tearDown(self):
        main_mod.requests = self._real_requests

    def _rest(self, **kw) -> RouterOsRest:
        return RouterOsRest(make_device("gw", **kw))

    # -- конструктор --

    def test_base_url_and_auth(self):
        rest = self._rest(host="10.0.0.1", scheme="https", port=8443)
        self.assertEqual(rest.base, "https://10.0.0.1:8443/rest")
        self.assertEqual(rest.session.auth, ("user", "pass"))
        self.assertFalse(rest.session.verify)
        self.assertEqual(rest.timeout, 15.0)
        self.assertEqual(rest.session.headers["Content-Type"], "application/json")

    def test_verify_ca_path(self):
        rest = self._rest(verify="rest-ca.crt")
        self.assertEqual(rest.session.verify, "rest-ca.crt")

    # -- кодирование пути записи --

    def test_record_path(self):
        self.assertEqual(RouterOsRest._record_path("*5"), "/ip/dns/static/*5")
        self.assertEqual(RouterOsRest._record_path("a b"), "/ip/dns/static/a%20b")
        # '*' не кодируется: RouterOS не декодирует %2A
        self.assertEqual(RouterOsRest._record_path("a*b"), "/ip/dns/static/a*b")

    # -- маппинг операций на HTTP-методы --

    def test_get_dns_static(self):
        rest = self._rest()
        rows = [{".id": "*1", "name": "x.com"}]
        rest.session.responses = [_FakeResponse(200, rows)]
        self.assertEqual(rest.get_dns_static(), rows)
        call = rest.session.calls[0]
        self.assertEqual((call["method"], call["url"], call["timeout"]),
                         ("GET", rest.base + "/ip/dns/static", 15.0))

    def test_get_non_list_answer_returns_empty(self):
        rest = self._rest()
        rest.session.responses = [_FakeResponse(200, {"unexpected": "dict"})]
        self.assertEqual(rest.get_dns_static(), [])

    def test_create_entry_uses_put(self):
        rest = self._rest()
        rest.session.responses = [_FakeResponse(201, {".id": "*9"})]
        rest.create_entry({"name": "x.com"})
        call = rest.session.calls[0]
        self.assertEqual((call["method"], call["url"]),
                         ("PUT", rest.base + "/ip/dns/static"))
        self.assertEqual(call["json"], {"name": "x.com"})

    def test_patch_entry_targets_record_url(self):
        rest = self._rest()
        rest.session.responses = [_FakeResponse(200, {})]
        rest.patch_entry("*7", {"comment": "c"})
        call = rest.session.calls[0]
        self.assertEqual((call["method"], call["url"], call["json"]),
                         ("PATCH", rest.base + "/ip/dns/static/*7", {"comment": "c"}))

    def test_delete_entry_targets_record_url(self):
        rest = self._rest()
        rest.session.responses = [_FakeResponse(204)]
        rest.delete_entry("*7")
        call = rest.session.calls[0]
        self.assertEqual((call["method"], call["url"], call["json"]),
                         ("DELETE", rest.base + "/ip/dns/static/*7", None))

    # -- обработка ответов и ошибок --

    def test_http_error_raises_device_error_with_detail(self):
        rest = self._rest()
        rest.session.responses = [
            _FakeResponse(400, {"message": "failure=already has such name"})]
        with self.assertRaises(DeviceError) as ctx:
            rest.create_entry({"name": "x.com"})
        text = str(ctx.exception)
        self.assertIn("[gw]", text)
        self.assertIn("HTTP 400", text)
        self.assertIn("already has such name", text)

    def test_http_error_logs_full_response_body(self):
        # причина в detail: тело ответа выводится целиком, а не только message
        rest = self._rest()
        rest.session.responses = [
            _FakeResponse(500, {"detail": "not enough permissions (9)",
                                "error": 500, "message": "Internal Server Error"})]
        with self.assertRaises(DeviceError) as ctx:
            rest.create_entry({"name": "x.com"})
        text = str(ctx.exception)
        self.assertIn("HTTP 500", text)
        self.assertIn("not enough permissions (9)", text)

    def test_http_error_without_json_body(self):
        rest = self._rest()
        rest.session.responses = [_FakeResponse(502, None, text="Bad Gateway")]
        with self.assertRaises(DeviceError) as ctx:
            rest.get_dns_static()
        self.assertIn("Bad Gateway", str(ctx.exception))

    def test_network_error_raises_device_error(self):
        rest = self._rest()
        rest.session.responses = [_FakeExceptions.RequestException("conn reset")]
        with self.assertRaises(DeviceError) as ctx:
            rest.get_dns_static()
        self.assertIn("conn reset", str(ctx.exception))

    def test_204_and_bad_json_return_none(self):
        rest = self._rest()
        rest.session.responses = [_FakeResponse(204)]
        self.assertIsNone(rest.delete_entry("*1"))
        rest.session.responses = [_FakeResponse(200, None, text="")]
        self.assertIsNone(rest._request("GET", "/ip/dns/static"))


if __name__ == "__main__":
    unittest.main()
