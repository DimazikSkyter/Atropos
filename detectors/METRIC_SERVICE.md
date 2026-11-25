# Сервис метаданных метрик

`apps.services.MetricService` хранит только описание метрик:
`name`, `labels`, `additional_labels`, `start`, `end`, `step`.
Получение списка имён через API находится в вызывающем слое. Сервис принимает
готовый список или успешный API envelope с таким списком. Значения метрик,
буферы samples и сетевой клиент в сервисе отсутствуют.

```python
from apps.services import MetricService

service = MetricService()

# Ответ API уже получен вызывающим кодом.
api_response = {'status': 'success', 'data': ['memory_used', 'memory_total']}
service.load(api_response, start=1_700_000_000, end=1_700_000_600, step=15)
# Или: service.load_names(api_response['data'], start=..., end=..., step=...)

service.update('memory_used', labels={'instance': 'node-a'})
service.update('memory_total', labels={'instance': 'node-a'})
service.add_additional_labels(service.snapshot(), {'team': 'platform'})
selected = service.filter(labels={'team': 'platform', 'instance': 'node-a'})
groups = selected.group_by(['instance', 'team'])

plan = service.compose(
    'memory_ratio',
    lambda used, total: used / total,
    bindings={
        'used': selected.get('memory_used').key,
        'total': selected.get('memory_total').key,
    },
)

assert plan.bindings['used'].name == 'memory_used'
assert plan.parameters == ('used', 'total')
inputs = service.resolve('memory_ratio')
assert inputs['used'].labels == {'instance': 'node-a'}
assert inputs['used'].additional_labels == {'team': 'platform'}
assert inputs['used'].timestamp_range == (1_700_000_000, 1_700_000_600)
assert inputs['used'].step == 15
```

`compose` и `resolve` не вызывают лямбду. `MetricComposition.function` содержит
саму функцию, а `bindings` — отображение имён её параметров в `MetricPointer`.
Вызывающий слой получает текущие метаданные через `resolve`, загружает нужные
значения и сам решает, как подставить их в функцию и выполнить вычисление.
Композиция — отдельное описание, а не новый временной ряд с вычисленными samples.

## Метаданные и загрузка списка

```python
from apps.services import MetricMetadata, MetricPointer

metric = MetricMetadata(
    name='cpu', labels={'instance': 'node-a'}, additional_labels={'role': 'primary'},
    start=100, end=700, step=15,
)
service.add(metric)
exact = MetricPointer('cpu', {'instance': 'node-a'})
assert service.get(exact) == metric
```

`labels` — исходные лейблы; `additional_labels` — отдельные локальные лейблы.
`effective_labels` объединяет их с `__name__`. Метаданные и словари неизменяемы;
переданные словари копируются. `to_dict()` даёт обычный JSON-совместимый словарь.

Все временные настройки задаются в секундах, допускаются дробные значения.
Требуется `start <= end` и конечный положительный `step`. Это рабочая конфигурация,
а не утверждение о наличии данных или фактических timestamps samples.

| Метод | Действие |
|---|---|
| `load_names(names, start=..., end=..., step=...)` | Принять готовый список имён из API |
| `load(payload, start=..., end=..., step=...)` | Принять тот же список или envelope `{'status': 'success', 'data': [...]}` |
| `replace(metrics)` | Атомарно заменить каталог объектами `MetricMetadata` |
| `add(metadata)` | Добавить метрику с полным описанием |
| `update(reference, name=..., labels=..., start=..., end=..., step=...)` | Изменить указанные поля существующего описания |
| `set_window(targets, start=..., end=..., step=...)` | Атомарно поменять диапазон и шаг выбранного набора |
| `get(reference)` / `snapshot()` / `metrics` / `names` | Получить метаданные, снимок каталога, кортеж описаний или имена |
| `remove(reference)` / `clear()` | Удалить метрику / весь каталог, аннотации и композиции |

`reference` — имя, точный `MetricPointer` или `MetricMetadata`.
Имя должно соответствовать ровно одному набору исходных лейблов, иначе возникает
`AmbiguousMetricError`. `filter(...).one()` также требует ровно одну метрику.

При загрузке имён для каждого имени сохраняются известные наборы исходных
лейблов и аннотации. Диапазон и шаг заменяются переданной конфигурацией.
Новое имя получает пустые лейблы. Имена, отсутствующие в новом списке,
исчезают из текущего каталога; при возвращении имён восстанавливаются известные
лейблы. Для полного обновления исходных наборов лейблов используйте `replace`.
Ошибочный ответ API, повторные имена или невалидные метаданные не меняют состояние.

## Дополнительные лейблы, фильтры и группы

Локальные лейблы учитываются в `filter`, `group_by`, `common_labels`,
`label_names`, `label_values` и выборе входов композиции. Они не меняют
исходный ключ метрики: `MetricPointer(name, source_labels)`.
Перекрывать исходные лейблы или `__name__` запрещено. Ключи — непустые строки,
значения — строки; пустое значение допустимо.

- `add_additional_labels(targets, labels)` добавляет отсутствующие ключи.
- `update_additional_labels(targets, labels)` меняет существующие ключи.
- `set_additional_labels(targets, labels)` заменяет словарь, `{}` очищает его.
- `remove_additional_labels(targets, names)` удаляет существующие ключи.

`targets` принимает одну ссылку на метрику, iterable ссылок или `MetricCollection`.
Пакетные изменения проверяются целиком перед записью. Аннотации сохраняются при
перезагрузке каталога и временном исчезновении метрики. Их можно менять по точному
сохранённому указателю и во время отсутствия. Явное `remove` забывает аннотации
метрики; `clear` очищает всё состояние. Хранение — в памяти, без фонового опроса.

Фильтры: `prefix`, `name_regex`, `names`, `exclude_names`, `labels`, `label_regex`.
Условия объединяются через AND; regex использует Python `fullmatch`.
Отсутствующий лейбл не совпадает с пустой строкой или regex `.*`.

`group_by(['instance', 'team'])` возвращает словарь с ключами вида
`(('instance', 'node-a'), ('team', 'platform'))` и значениями `MetricCollection`.
`missing='drop'` исключает метрики без нужного лейбла; `missing='raise'` сообщает
ошибку; `missing='group'` использует `None`, отдельно от пустой строки.
`group_by([])` даёт общую группу `()`.

`union`, `intersection`, `difference` управляют составом каталогов по исходным
указателям. `union` отклоняет конфликтующие метаданные, либо использует явно
заданный `conflict='left'/'right'`. `intersection` сохраняет метаданные слева.

## Привязки лямбды

```python
# Параметры можно автоматически связать с одноимёнными уникальными метриками.
automatic = service.compose('difference', lambda memory_total, memory_used: memory_total - memory_used)

# Или явно выбрать метрику через любой эффективный лейбл.
primary = service.filter(labels={'role': 'primary'}).one()
service.compose('primary_metric', lambda source: source, bindings={'source': primary})

# resolve возвращает актуальную конфигурацию для сохранённых указателей.
service.update('memory_used', start=200, end=500, step=5)
assert service.resolve('memory_ratio')['used'].step == 5
```

Все параметры, включая параметры с default, должны получить привязку.
Поддерживаются обычные и keyword-only параметры; `*args`, `**kwargs` и
positional-only параметры отклоняются. Привязки хранятся в порядке параметров
функции; порядок метрик в каталоге не влияет на выбор.

`compose(name, function, bindings=None)` сохраняет описание в сервисе.
`overwrite=True` явно заменяет существующую композицию.
`get_composition`, `remove_composition`, `compositions` дают управление реестром.
`MetricCollection.compose` создаёт описание без регистрации в сервисе.

При изменении имени или исходных лейблов через `update` сервис переносит указатели
в зарегистрированных композициях на новое описание. Старый отдельно сохранённый
объект композиции остаётся прежним снимком; для актуального описания используйте
имя композиции в `service.resolve(name)`. Изменение additional_labels, диапазона
или шага не меняет указатель, и `resolve` сразу возвращает новые метаданные.
Удаление метрики или её отсутствие после загрузки каталога вызывает `KeyError`
при разрешении композиции; случайная подстановка другой метрики исключена.

Сервис не исполняет функции, не загружает samples и не совмещает временные сетки.
Потокобезопасность и исполнение описаний обеспечивает вызывающий слой.

## Проверка

```powershell
uv run python -m unittest tests.test_metric_service -v
uv run python -m unittest discover -s tests -t . -v
```

Проверяются загрузка готовых API-списков, метаданные и шаг, сохранение аннотаций,
атомарность изменений, фильтры/группы и точность привязок лямбды. Отдельный тест
подтверждает, что регистрация, разрешение и перенос привязок не вызывают функцию.
