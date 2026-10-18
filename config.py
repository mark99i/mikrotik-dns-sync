"""Загрузка и валидация конфигурации mikrotik_dns_sync (config.json5).

Файл читается как JSON5, неизвестные ключи вызывают предупреждение,
значения проверяются декларативно моделями pydantic, а ошибки pydantic
переводятся в SyncError с читаемым сообщением.
"""

import logging

import json5
from pydantic import ValidationError

from models import AppConfig

LOG = logging.getLogger("dns-sync")


class SyncError(RuntimeError):
    """Ошибка конфигурации (критическая)."""


# Неизвестные ключи конфигурации вызывают предупреждение — ловим опечатки
KNOWN_TOP_KEYS = frozenset(
    {"delete_comment", "adopt_foreign", "timeout", "max_workers",
     "default_forward_to", "devices"})
KNOWN_DEVICE_KEYS = frozenset(
    {"name", "host", "username", "password", "password_env", "address_list",
     "scheme", "port", "verify_ssl", "ca_cert", "enabled", "exclude_domains"})

# Убранные опции: имя -> подсказка, чем заменить
REMOVED_TOP_KEYS = {
    "prune_stale": "опция удалена — для удаления записи ставьте ей "
                   "комментарий-маркер (delete_comment)",
}


def _warn_unknown_keys(where: str, keys: set[str],
                       known: frozenset[str], removed: dict[str, str]) -> None:
    for key in sorted(keys - known):
        hint = f"; {removed[key]}" if key in removed else ""
        LOG.warning("неизвестный параметр '%s' %s%s", key, where, hint)


def _validation_error(exc: ValidationError) -> SyncError:
    """ValidationError pydantic -> SyncError с читаемым сообщением."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"]) or "конфигурация"
        ctx = err.get("ctx") or {}
        err_type = err["type"]
        if err_type == "literal_error":
            expected = str(ctx.get("expected", "")).replace(" or ", " или ")
            msg = f"должно быть одним из: {expected}"
        elif err_type == "greater_than":
            msg = f"должно быть больше {ctx.get('gt', 0):g}"
        elif err_type == "greater_than_equal":
            msg = f"должно быть не меньше {ctx.get('ge'):g}"
        elif err_type == "less_than_equal":
            msg = f"должно быть не больше {ctx.get('le'):g}"
        else:
            msg = {
                "missing": "не задано обязательное поле",
                "string_too_short": "не должно быть пустым",
                "int_parsing": "должно быть целым числом",
                "float_parsing": "должно быть числом",
                "float_type": "должно быть числом",
                "bool_parsing": "должно быть true или false",
            }.get(err_type)
            if msg is None:
                msg = err["msg"]
                if err_type == "value_error":
                    msg = msg.removeprefix("Value error, ")
        parts.append(f"{loc} — {msg}")
    return SyncError("; ".join(parts))


def load_config(path: str) -> AppConfig:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json5.load(fh)
    except FileNotFoundError:
        raise SyncError(f"файл конфигурации не найден: {path}")
    except ValueError as exc:   # json5 бросает голый ValueError
        raise SyncError(f"ошибка JSON5 в {path}: {exc}")
    if not isinstance(raw, dict):
        raise SyncError("конфигурация должна быть JSON-объектом")

    _warn_unknown_keys("в конфигурации", set(raw), KNOWN_TOP_KEYS, REMOVED_TOP_KEYS)

    raw_devices = raw.get("devices", [])
    if not isinstance(raw_devices, list):
        raise SyncError("'devices' должен быть списком")
    for idx, dev in enumerate(raw_devices, 1):
        if not isinstance(dev, dict):
            raise SyncError(f"devices[{idx}] должен быть объектом")
        if not str(dev.get("name") or "").strip():
            dev["name"] = f"device-{idx}"
        _warn_unknown_keys(f"в устройстве '{dev['name']}'", set(dev),
                           KNOWN_DEVICE_KEYS, {})
        dev["timeout"] = raw.get("timeout", 15)   # глобальный — для всех устройств

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
