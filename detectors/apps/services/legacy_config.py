"""Read-only, explicit import of the retired UI SQLite configuration."""
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from .configuration import MetricDefinition, validate_references
from .datasources import Datasource, validate_catalog


def read_legacy_configuration(path):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        try:
            rows = db.execute('SELECT kind, id, body FROM config ORDER BY kind, id').fetchall()
        except sqlite3.Error:
            raise ValueError('Файл не содержит прежнюю таблицу конфигурации UI.') from None
    sources, definitions, names = [], [], set()
    for kind, uid, body in rows:
        values = json.loads(body)
        if kind == 'source':
            source = Datasource(**values)
            if source.id != uid:
                raise ValueError('UID записи SQLite не совпадает с UID источника.')
            name, suffix = source.name, 2
            while name.casefold() in names:
                name = f'{source.name} ({suffix})'
                suffix += 1
            names.add(name.casefold())
            sources.append(replace(source, name=name))
        elif kind == 'definition':
            item = MetricDefinition(**values)
            if item.name != uid:
                raise ValueError('Имя записи SQLite не совпадает с именем определения.')
            definitions.append(item)
        else:
            raise ValueError('Неизвестный вид настройки SQLite; импорт остановлен.')
    default = next((source.id for source in sources if source.is_default), sources[0].id if sources else None)
    sources = validate_catalog(replace(source, is_default=source.id == default) for source in sources)
    validate_references(sources, definitions)
    return sources, definitions
