"""Datasource configuration and lifecycle; connection checks use injected adapters."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, replace
from numbers import Real
from threading import RLock
from typing import Callable, Iterable, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

DATASOURCE_KINDS = ('VictoriaMetrics', 'Prometheus', 'InfluxDB', 'ClickHouse')
DEFAULT_URLS = {
    'VictoriaMetrics': 'http://localhost:8428', 'Prometheus': 'http://localhost:9090',
    'InfluxDB': 'http://localhost:8086', 'ClickHouse': 'http://localhost:8123',
}


def validate_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Нужен HTTP(S) URL.')
    if any(ord(character) < 32 for character in value):
        raise ValueError('URL не должен содержать управляющие символы.')
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError('Нужен HTTP(S) URL без пароля, query-параметров и fragment.')
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError('Некорректный порт URL.') from exc
    return value


@dataclass(frozen=True, slots=True)
class Datasource:
    """Stable UID and settings; credentials are environment references, never values.

    The first five fields retain the existing UI's persisted/positional contract.
    Empty auth_type infers bearer/token from the legacy token_env field.
    """

    id: str
    name: str
    url: str
    kind: str = 'VictoriaMetrics'
    token_env: str = ''
    is_default: bool = False
    auth_type: str = ''
    username: str = ''
    password_env: str = ''
    database: str = ''
    organization: str = ''
    bucket: str = ''
    influx_version: str = '2'
    timeout_seconds: float = 20
    tls_verify: bool = True
    min_interval_seconds: int = 15
    revision: int = 1

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.id):
            raise ValueError('UID источника должен содержать 1–64 буквы, цифры, дефисы или подчёркивания.')
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Укажите имя источника.')
        object.__setattr__(self, 'name', self.name.strip())
        object.__setattr__(self, 'url', validate_url(self.url))
        if self.kind not in DATASOURCE_KINDS:
            raise ValueError('Неизвестный тип источника.')
        for field in ('token_env', 'password_env', 'username', 'database', 'organization', 'bucket'):
            value = getattr(self, field)
            if not isinstance(value, str) or any(ord(character) < 32 for character in value):
                raise ValueError(f'Некорректное поле {field}.')
            object.__setattr__(self, field, value.strip())
        for field in ('token_env', 'password_env'):
            value = getattr(self, field)
            if value and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
                raise ValueError('Укажите имя переменной окружения с секретом.')
        inferred = ('token' if self.kind == 'InfluxDB' else 'bearer') if self.token_env else 'none'
        auth = self.auth_type or inferred
        allowed = {'none', 'basic', 'bearer'} if self.prometheus_compatible else {'none', 'basic'}
        if self.kind == 'InfluxDB':
            if self.influx_version not in {'1', '2', '3'}:
                raise ValueError('Выберите версию InfluxDB: 1, 2 или 3.')
            allowed = {'none', 'basic'} if self.influx_version == '1' else {'none', 'token'}
            if self.influx_version == '2' and (not self.organization or not self.bucket):
                raise ValueError('Для InfluxDB 2 укажите организацию и bucket.')
            if self.influx_version in {'1', '3'} and not self.database:
                raise ValueError('Укажите базу данных InfluxDB.')
        if auth not in allowed:
            raise ValueError('Этот способ авторизации не поддерживается выбранным типом источника.')
        if auth in {'bearer', 'token'} and not self.token_env:
            raise ValueError('Укажите переменную окружения с токеном.')
        if auth == 'basic' and (not self.username or not self.password_env):
            raise ValueError('Для Basic auth укажите пользователя и переменную окружения с паролем.')
        if auth not in {'bearer', 'token'} and self.token_env:
            raise ValueError('Поле токена допустимо только для авторизации токеном.')
        if auth != 'basic' and (self.password_env or self.username):
            raise ValueError('Пользователь и пароль допустимы только для Basic auth.')
        object.__setattr__(self, 'auth_type', auth)
        if self.kind == 'ClickHouse' and not self.database:
            object.__setattr__(self, 'database', 'default')
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, Real)
                or not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 300):
            raise ValueError('Timeout должен быть от 1 до 300 секунд.')
        if (isinstance(self.min_interval_seconds, bool) or not isinstance(self.min_interval_seconds, int)
                or not 1 <= self.min_interval_seconds <= 86400):
            raise ValueError('Минимальный шаг должен быть от 1 до 86400 секунд.')
        if not isinstance(self.is_default, bool) or not isinstance(self.tls_verify, bool):
            raise ValueError('Настройки default и TLS должны быть boolean.')
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError('Ревизия источника должна быть положительным целым числом.')

    @property
    def uid(self) -> str:
        return self.id

    @property
    def prometheus_compatible(self) -> bool:
        return self.kind in {'VictoriaMetrics', 'Prometheus'}

    @property
    def query_language(self) -> str:
        if self.kind == 'InfluxDB':
            return {'1': 'InfluxQL', '2': 'Flux', '3': 'SQL'}[self.influx_version]
        return {'VictoriaMetrics': 'MetricsQL', 'Prometheus': 'PromQL', 'ClickHouse': 'SQL'}[self.kind]


@dataclass(frozen=True, slots=True)
class DatasourceHealth:
    uid: str
    status: str
    message: str
    checked_at: float

    def __post_init__(self):
        if self.status not in {'ok', 'error', 'demo'}:
            raise ValueError('Неизвестный статус проверки источника.')


SourceChange = Callable[[tuple[Datasource, ...]], Iterable[Datasource]]


class DatasourceRepository(Protocol):
    def sources(self) -> list[Datasource]: ...

    def edit_sources(self, change: SourceChange) -> tuple[Datasource, ...]:
        """Apply one catalog change atomically, enforcing repository references."""
        ...


def validate_catalog(sources: Iterable[Datasource]) -> tuple[Datasource, ...]:
    sources = tuple(sources)
    names, uids = set(), set()
    for source in sources:
        if not isinstance(source, Datasource):
            raise TypeError('Expected Datasource')
        if source.id in uids or source.name.casefold() in names:
            raise ValueError('Имя и UID источника должны быть уникальными.')
        names.add(source.name.casefold())
        uids.add(source.id)
    if sources and sum(source.is_default for source in sources) != 1:
        raise ValueError('Должен быть ровно один источник по умолчанию.')
    return sources


class MemoryDatasourceRepository:
    def __init__(self):
        self._sources = ()
        self._lock = RLock()

    def sources(self) -> list[Datasource]:
        with self._lock:
            return list(self._sources)

    def edit_sources(self, change: SourceChange) -> tuple[Datasource, ...]:
        with self._lock:
            updated = validate_catalog(change(self._sources))
            self._sources = updated
            return updated


class DatasourceService:
    """Grafana-style datasource lifecycle shared by detectors and UI.

    UID survives edits, names are unique, and a nonempty catalog has one default.
    Persistence and connection adapters are injected; this service owns no client.
    """

    def __init__(self, repository: DatasourceRepository | None = None, *,
                 checker: Callable[[Datasource], DatasourceHealth] | None = None):
        self.repository = MemoryDatasourceRepository() if repository is None else repository
        self.checker = checker

    def list(self, *, kind: str | None = None, search: str = '') -> tuple[Datasource, ...]:
        sources = (source for source in self.repository.sources()
                   if (kind is None or source.kind == kind)
                   and search.casefold() in source.name.casefold())
        return tuple(sorted(sources, key=lambda source: (not source.is_default, source.name.casefold(), source.id)))

    def get(self, uid: str) -> Datasource:
        for source in self.repository.sources():
            if source.id == uid:
                return source
        raise KeyError(uid)

    def default(self) -> Datasource | None:
        return next((source for source in self.list() if source.is_default), None)

    @staticmethod
    def _save(current: tuple[Datasource, ...], source: Datasource) -> tuple[Datasource, ...]:
        if not isinstance(source, Datasource):
            raise TypeError('Expected Datasource')
        by_uid = {item.id: item for item in current}
        previous = by_uid.get(source.id)
        if previous is not None and previous.revision != source.revision:
            raise ValueError('Конфликт ревизии источника. Обновите страницу и повторите изменение.')
        if previous is None and source.revision != 1:
            raise ValueError('Новый источник должен иметь ревизию 1.')
        by_uid[source.id] = source
        sources = tuple(by_uid.values())
        if source.is_default:
            sources = tuple(replace(item, is_default=item.id == source.id) for item in sources)
        elif not any(item.is_default for item in sources):
            alternatives = [item for item in sources if item.id != source.id]
            selected = min(alternatives or [source], key=lambda item: (item.name.casefold(), item.id))
            sources = tuple(replace(item, is_default=item.id == selected.id) for item in sources)
        return DatasourceService._revise(current, sources)

    @staticmethod
    def _revise(current, updated):
        previous = {source.id: source for source in current}
        return validate_catalog(
            replace(source, revision=previous[source.id].revision + 1)
            if source.id in previous and source != previous[source.id] else source
            for source in updated
        )

    @classmethod
    def from_postgres(cls, dsn: str | None = None, *, checker=None):
        """Use the same persistent catalog as UI, without importing UI code."""
        from .postgres import PostgresConfigRepository
        return cls(PostgresConfigRepository(dsn), checker=checker)

    def save(self, source: Datasource) -> Datasource:
        sources = self.repository.edit_sources(lambda current: self._save(current, source))
        return next(item for item in sources if item.id == source.id)

    def create(self, name: str, url: str, kind: str = 'VictoriaMetrics', **settings) -> Datasource:
        return self.save(Datasource(uuid4().hex, name, url, kind, **settings))

    def update(self, uid: str, **changes) -> Datasource:
        if 'id' in changes or 'revision' in changes:
            raise ValueError('UID и ревизию нельзя менять при редактировании источника.')
        def change(current):
            previous = next((source for source in current if source.id == uid), None)
            if previous is None:
                raise KeyError(uid)
            return self._save(current, replace(previous, **changes))
        sources = self.repository.edit_sources(change)
        return next(source for source in sources if source.id == uid)

    def set_default(self, uid: str) -> Datasource:
        return self.update(uid, is_default=True)

    def delete(self, uid: str) -> None:
        def change(current):
            if not any(source.id == uid for source in current):
                raise KeyError(uid)
            remaining = tuple(source for source in current if source.id != uid)
            if remaining and not any(source.is_default for source in remaining):
                selected = min(remaining, key=lambda item: (item.name.casefold(), item.id))
                remaining = tuple(replace(source, is_default=source.id == selected.id) for source in remaining)
            return self._revise(current, remaining)
        self.repository.edit_sources(change)

    def test_connection(self, uid: str) -> DatasourceHealth:
        source = self.get(uid)
        if self.checker is None:
            raise RuntimeError('Адаптер проверки подключения не настроен.')
        result = self.checker(source)
        if not isinstance(result, DatasourceHealth) or result.uid != source.id:
            raise ValueError('Адаптер вернул некорректный результат проверки.')
        return result

    @staticmethod
    def demo_check(source: Datasource) -> DatasourceHealth:
        return DatasourceHealth(source.id, 'demo', 'Демо: настройки сохранены. Сетевое подключение не проверялось.', time.time())
