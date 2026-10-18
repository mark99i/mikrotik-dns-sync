"""Вспомогательные функции mikrotik_dns_sync.

Маркер удаления, сравнение записей, выбор локального forward-to устройства
и компактная печать payload для лога.
"""

import json
from collections import Counter

from models import Entry, Payload, Snapshot


def compact(payload: Payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def is_delete_marker(comment: str, marker: str) -> bool:
    return comment.strip().lower() == marker


def field_diffs(left: Entry, right: Entry) -> list[str]:
    """Имена полей (в терминах RouterOS), по которым записи различаются.

    forward-to, ttl и comment в сравнение не входят: это локальные настройки
    устройства, различие между устройствами — норма, а не конфликт.
    """
    if left.match_subdomain != right.match_subdomain:
        return ["match-subdomain"]
    return []


def device_forward_to(snap: Snapshot) -> str | None:
    """Преобладающий forward-to среди FWD-записей устройства.

    Используется при создании новых записей: forward-to — локальная настройка
    устройства (у каждого роутера своё), синхронизации не подлежит. Если
    управляемых записей нет, смотрим посторонние FWD этого же устройства.
    """
    foreign_flat = (entry for entries in snap.foreign.values() for entry in entries)
    for source in (snap.managed.values(), foreign_flat):
        counts = Counter(entry.forward_to for entry in source if entry.forward_to)
        if counts:
            best = max(counts.values())
            return min(address for address, num in counts.items() if num == best)
    return None
