"""Configuration contracts shared by UI and backend; no sample data or secrets."""
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from .datasources import (
    Datasource,
    DatasourceService,
    MemoryDatasourceRepository,
    validate_catalog,
    validate_url,
)
from .metric_transforms import transform_result, validate_transform


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    datasource_id: str
    query: str
    description: str = ''
    interval_seconds: int = 30
    drop_labels: tuple[str, ...] = ()
    extra_labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*', self.name):
            raise ValueError('Некорректное имя выходной метрики Prometheus.')
        if not isinstance(self.query, str) or not self.query.strip() or not self.datasource_id:
            raise ValueError('Укажите PromQL и источник данных.')
        if (isinstance(self.interval_seconds, bool) or not isinstance(self.interval_seconds, int)
                or not 1 <= self.interval_seconds <= 86400):
            raise ValueError('Интервал должен быть от 1 до 86400 секунд.')
        if not isinstance(self.description, str):
            raise ValueError('Описание должно быть строкой.')
        validate_transform(self.name, self.drop_labels, self.extra_labels)
        object.__setattr__(self, 'drop_labels', tuple(sorted(set(self.drop_labels))))
        object.__setattr__(self, 'extra_labels', dict(self.extra_labels))

    def transform_result(self, data):
        return transform_result(data, name=self.name, drop_labels=self.drop_labels, extra_labels=self.extra_labels)

    def payload(self, source: Datasource) -> dict:
        if source.id != self.datasource_id:
            raise ValueError('Источник не соответствует определению метрики.')
        return {**asdict(self), 'datasource': asdict(source)}


@dataclass(frozen=True)
class ServiceSettings:
    server_url: str = 'http://localhost:8000'
    server_token_env: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'server_url', validate_url(self.server_url))
        if (not isinstance(self.server_token_env, str) or
                (self.server_token_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.server_token_env))):
            raise ValueError('Укажите имя переменной окружения с токеном сервера.')


def validate_references(sources: Iterable[Datasource], definitions: Iterable[MetricDefinition]):
    by_uid = {source.id: source for source in sources}
    for definition in definitions:
        if definition.datasource_id not in by_uid:
            raise ValueError('Источник используется сохранёнными метриками или был удалён. Сначала измените определения.')
        if not by_uid[definition.datasource_id].prometheus_compatible:
            raise ValueError('Источник используется определениями PromQL; требуется VictoriaMetrics или Prometheus.')


class MemoryConfigRepository(MemoryDatasourceRepository):
    """Session-local demo/test repository; production always uses PostgreSQL."""

    def __init__(self):
        super().__init__()
        self._definitions = {}
        self._settings = ServiceSettings()

    def edit_sources(self, change):
        with self._lock:
            updated = validate_catalog(change(self._sources))
            validate_references(updated, self._definitions.values())
            self._sources = updated
            return updated

    def definitions(self):
        with self._lock:
            return sorted(self._definitions.values(), key=lambda item: item.name)

    def save(self, item):
        if isinstance(item, Datasource):
            return DatasourceService(self).save(item)
        if not isinstance(item, MetricDefinition):
            raise TypeError('Expected Datasource or MetricDefinition')
        with self._lock:
            validate_references(self._sources, [item])
            self._definitions[item.name] = item

    def save_definition(self, item: MetricDefinition, *, previous_name=None):
        """Explicit catalog create/edit; a name collision never overwrites another entry."""
        if not isinstance(item, MetricDefinition):
            raise TypeError('Expected MetricDefinition')
        with self._lock:
            if previous_name is not None and previous_name not in self._definitions:
                raise ValueError('Сохранённая метрика была удалена. Обновите каталог.')
            if item.name != previous_name and item.name in self._definitions:
                raise ValueError('Метрика с таким именем уже есть в каталоге. Укажите другое имя.')
            validate_references(self._sources, [item])
            if previous_name is not None:
                self._definitions.pop(previous_name)
            self._definitions[item.name] = item

    def delete(self, kind, key):
        if kind == 'source':
            return DatasourceService(self).delete(key)
        if kind != 'definition':
            raise ValueError('Неизвестный вид настройки.')
        with self._lock:
            self._definitions.pop(key, None)

    def settings(self):
        with self._lock:
            return self._settings

    def save_settings(self, settings: ServiceSettings):
        if not isinstance(settings, ServiceSettings):
            raise TypeError('Expected ServiceSettings')
        with self._lock:
            self._settings = settings
