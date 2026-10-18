"""Модели данных mikrotik_dns_sync (pydantic v2).

Конфигурация (DeviceConfig, AppConfig), DNS-записи (Entry, DesiredEntry),
снимок устройства после сбора (Snapshot) и статистика применения (Stats).
Проверки значений — декларативные (Literal, границы, min_length); «диалект»
config.json5 (password_env, verify_ssl/ca_cert, порт по scheme) разбирают
model-валидаторы.
"""

import os
from typing import Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, PositiveFloat,
                      field_validator, model_validator)

# Псевдонимы типов (PEP 695 — Statement to define type aliases):
type RawRow = dict[str, Any]     # одна запись из REST-ответа RouterOS
type Payload = dict[str, str]    # тело PUT/PATCH-запроса к RouterOS


# --------------------------------------------------------------------------
# Нормализация значений RouterOS
# --------------------------------------------------------------------------

def ros_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1")


def ros_bool_str(value: bool) -> str:
    return "true" if value else "false"


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

class DeviceConfig(BaseModel):
    """Конфигурация одного устройства.

    Проверки значений — декларативные: `Literal` для scheme, границы port,
    `PositiveFloat` для timeout, непустые обязательные строки. «Диалект»
    config.json5 (password_env, verify_ssl/ca_cert, порт по умолчанию в
    зависимости от scheme) разбирает model_validator.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    address_list: str = Field(min_length=1)  # имя address-list на ЭТОМ устройстве:
                                             # по нему фильтруем при сборе и его же пишем
    scheme: Literal["http", "https"] = "https"
    port: int = Field(default=443, ge=1, le=65535)
    verify: bool | str = False  # False | True | путь к CA-сертификату
    timeout: PositiveFloat = 15.0
    enabled: bool = True
    exclude_domains: list[str] = Field(default_factory=list)

    @field_validator("exclude_domains", mode="before")
    @classmethod
    def _normalize_excludes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):    # одна строка вместо списка
            value = [value]
        return sorted({str(item).strip().lower() for item in value
                       if str(item).strip()})

    @field_validator("name", "host", "username", "address_list", mode="before")
    @classmethod
    def _strip_strings(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="before")
    @classmethod
    def _config_dialect(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        name = data.get("name") or "?"
        password = data.get("password")
        if password is None and data.get("password_env"):
            password = os.environ.get(data["password_env"])
        if not password:
            raise ValueError(f"устройство '{name}': задайте 'password' или 'password_env'")
        data["password"] = password
        scheme = str(data.get("scheme") or "https").lower()
        data["scheme"] = scheme
        if data.get("port") in (None, ""):
            data["port"] = 443 if scheme == "https" else 80
        if data.get("verify_ssl") and "verify" not in data:
            # путь к CA-сертификату (rest-ca.crt) или True для системных CA
            data["verify"] = data.get("ca_cert") or True
        return data


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    devices: list[DeviceConfig]
    delete_comment: str = "del"
    adopt_foreign: bool = True    # перехватывать одноимённые FWD-записи,
                                  # находящиеся вне настроенного address-list
    max_workers: int = 4
    default_forward_to: str = ""  # forward-to для новых записей на устройстве,
                                  # где своих записей ещё нет; "" — не задан

    @field_validator("default_forward_to", mode="before")
    @classmethod
    def _strip_default_forward_to(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("delete_comment", mode="before")
    @classmethod
    def _normalize_marker(cls, value: Any) -> str:
        return str(value if value is not None else "del").strip().lower()

    @field_validator("max_workers", mode="after")
    @classmethod
    def _clamp_workers(cls, value: int) -> int:
        return max(1, value)

    @model_validator(mode="after")
    def _devices_present_and_unique(self) -> "AppConfig":
        if not self.devices:
            raise ValueError("в конфигурации нет устройств")
        seen: set[str] = set()
        for dev in self.devices:
            if dev.name in seen:
                raise ValueError(f"дубликат имени устройства: '{dev.name}'")
            seen.add(dev.name)
        return self


# --------------------------------------------------------------------------
# Модель записи
# --------------------------------------------------------------------------

class Entry(BaseModel):
    """FWD-запись /ip/dns/static с устройств (значения уже нормализованы)."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    forward_to: str
    match_subdomain: bool
    ttl: str | None
    comment: str
    disabled: bool
    address_list: str

    @classmethod
    def from_raw(cls, row: RawRow) -> "Entry":
        return cls(
            id=str(row.get(".id", "")),
            name=str(row.get("name", "")),
            forward_to=str(row.get("forward-to", "") or ""),
            match_subdomain=ros_bool(row.get("match-subdomain", "false")),
            ttl=str(row["ttl"]) if row.get("ttl") else None,
            comment=str(row.get("comment", "") or ""),
            disabled=ros_bool(row.get("disabled", "false")),
            address_list=str(row.get("address-list", "") or ""),
        )

    @property
    def key(self) -> str:
        return self.name.lower()


class Snapshot(BaseModel):
    """Результат прохода 1 для одного устройства."""

    model_config = ConfigDict(frozen=True)

    device: DeviceConfig
    managed: dict[str, Entry] = Field(default_factory=dict)        # key -> запись в списке
    foreign: dict[str, list[Entry]] = Field(default_factory=dict)  # FWD вне списка (по имени)


class DesiredEntry(BaseModel):
    """Целевое состояние записи (после слияния всех устройств).

    Локальные поля устройства (forward-to, ttl, comment) в целевое состояние
    не входят: forward_to здесь — лишь резерв для создания на пустом
    устройстве, ttl и комментарий скрипт не пишет вовсе.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    forward_to: str             # с устройства-источника; резерв для устройств,
                                # где своих FWD-записей ещё нет
    match_subdomain: bool
    disabled: bool
    deleted: bool
    seen_on: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Статистика применения
# --------------------------------------------------------------------------

class Stats(BaseModel):
    created: int = 0
    updated: int = 0
    adopted: int = 0   # обновлено за счёт перехвата посторонней записи
    deleted: int = 0
    skipped: int = 0   # пропущено из-за adopt_foreign=false
    failed: int = 0
