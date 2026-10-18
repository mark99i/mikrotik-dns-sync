"""REST-клиент RouterOS 7 для mikrotik_dns_sync.

Инкапсулирует работу с REST API роутера: сессию с Basic-аутентификацией,
URL-кодирование идентификаторов записей и приведение ошибок HTTP/сети
к DeviceError с телом ответа в сообщении.
"""

from typing import Any
from urllib.parse import quote

import requests

from models import DeviceConfig, Payload, RawRow


class DeviceError(RuntimeError):
    """Ошибка взаимодействия с устройством."""


class RouterOsRest:
    """Минимальный клиент REST API RouterOS 7 (HTTP Basic auth).

    Соответствие методов консоли (актуальная документация MikroTik):
      GET    -> print   (чтение)
      PUT    -> add     (создание, 201 Created)
      PATCH  -> set     (частичное изменение записи, 200 OK)
      DELETE -> remove  (удаление, 204 No Content)

    Сессия requests переиспользуется между проходами 1 и 2, чтобы не
    устанавливать TLS-соединение заново для каждого устройства.
    """

    def __init__(self, dev: DeviceConfig):
        self.dev = dev
        self.base = f"{dev.scheme}://{dev.host}:{dev.port}/rest"
        self.session = requests.Session()
        self.session.auth = (dev.username, dev.password)
        self.session.verify = dev.verify
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = dev.timeout

    @staticmethod
    def _record_path(entry_id: str) -> str:
        # RouterOS не декодирует %2A, поэтому '*' оставляем как есть,
        # остальные спецсимволы кодируем.
        return "/ip/dns/static/" + quote(entry_id, safe="*")

    def _request(self, method: str, path: str, body: Payload | None = None) -> Any:
        url = self.base + path
        try:
            resp = self.session.request(method, url, json=body, timeout=self.timeout)
        except requests.exceptions.RequestException as exc:
            raise DeviceError(f"[{self.dev.name}] {method} {path}: {exc}")
        if resp.status_code >= 400:
            raise DeviceError(f"[{self.dev.name}] {method} {path} -> HTTP {resp.status_code}: "
                              f"{self._error_detail(resp)}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    @staticmethod
    def _error_detail(resp) -> str:
        # причина отказа у RouterOS — в поле detail тела, обобщённый message
        # бесполезен, поэтому выводим тело целиком
        body = (resp.text or "").strip()
        return body[:500] if body else "<пустое тело ответа>"

    def get_dns_static(self) -> list[RawRow]:
        data = self._request("GET", "/ip/dns/static")
        return data if isinstance(data, list) else []

    def create_entry(self, payload: Payload) -> None:
        self._request("PUT", "/ip/dns/static", payload)

    def patch_entry(self, entry_id: str, payload: Payload) -> None:
        self._request("PATCH", self._record_path(entry_id), payload)

    def delete_entry(self, entry_id: str) -> None:
        self._request("DELETE", self._record_path(entry_id))
