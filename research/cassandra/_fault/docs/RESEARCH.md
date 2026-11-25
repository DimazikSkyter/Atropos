# Что именно воспроизводит стенд

## Вывод

Описанный режим реален как **compaction backlog / compaction debt**: скорость создания SSTable устойчиво выше скорости их слияния. Число SSTable и read/write amplification растут, свободное место уменьшается, а фоновые и клиентские операции начинают конкурировать за CPU, диск и память. Apache Cassandra прямо рекомендует считать большое число pending compactions признаком того, что compaction threads не успевают за записью.

Стенд создаёт этот режим намеренно: частый flush, LCS с маленькими SSTable, один compactor и общий лимит compaction 1 MiB/s. Есть два варианта финала:

- управляемый: накопить долг, затем `pause`/`kill` первой ноды;
- естественный: дать первой ноде меньше CPU и последовательно ограничить data filesystem трёх нод.

Это воспроизводит системную положительную обратную связь, но не заявляет воспроизведение конкретного исторического deadlock или исключения без исходного stack trace.

## Важная поправка к сценарию падения

Apache Cassandra не переназначает token ranges оставшимся нодам автоматически после обычного отказа. Это сделано специально, чтобы краткая недоступность не запускала ненужное перемещение данных. Для замены требуется явный bootstrap/replace/decommission.

При RF=3 координатор всё равно направляет запись всем трём репликам. При недоступной реплике он сохраняет hint на локальном диске, а CL=ONE позволяет ответить клиенту после одного подтверждения. Поэтому после отказа растут hints, меняется распределение координаторских запросов и позже появляется тяжёлая replay/repair/streaming-нагрузка, но данные сами по себе мгновенно не «переезжают» на соседей.

Для трёх нод и RF=3 каждая партиция имеет копию на каждой ноде. Для пяти нод RF остаётся 3, и набор трёх реплик зависит от token range. Вариант из пяти нод ближе к обычной production-топологии; вариант из трёх быстрее демонстрирует исчерпание общих ресурсов.

Слово «шарды» может указывать и на ScyllaDB: она совместима с Cassandra API, но шардирует работу внутри каждой ноды. У ScyllaDB есть явные метрики compaction backlog, normalized backlog, pending/failed/postponed compactions. Значит класс проблемы актуален и там, хотя конкретная автоматика и симптомы отличаются.

## На что смотреть

- `cassandra_compaction_pendingtasks` и `cassandra_table_pendingcompactions`;
- `cassandra_table_livesstablecount`;
- `cassandra_table_livediskspaceused` и свободное место Windows-хоста;
- client request rate/latency, dropped messages, thread-pool queues;
- `nodetool compactionstats`, `tpstats`, `netstats`, `statushandoff`;
- hints и streaming при возвращении/замене ноды.

## Источники

- [Apache Cassandra: Compaction overview](https://cassandra.apache.org/doc/stable/cassandra/managing/operating/compaction/overview.html)
- [Apache Cassandra: диагностика через nodetool](https://cassandra.apache.org/doc/stable/cassandra/troubleshooting/use_nodetool.html)
- [Apache Cassandra: JMX metrics, включая PendingTasks и LiveDiskSpaceUsed](https://cassandra.apache.org/doc/stable/cassandra/managing/operating/metrics.html)
- [Apache Cassandra: compaction throughput](https://cassandra.apache.org/doc/latest/cassandra/managing/configuration/cass_yaml_file.html#compaction-throughput)
- [Apache Cassandra: hinted handoff](https://cassandra.apache.org/doc/stable/cassandra/managing/operating/hints.html)
- [Apache Cassandra: failure detector и отсутствие автоматического удаления ноды](https://cassandra.apache.org/doc/latest/cassandra/architecture/dynamo.html)
- [Apache Cassandra: явная замена и bootstrap нод](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/topo_changes.html)
- [ScyllaDB: compaction manager metrics](https://docs.scylladb.com/manual/stable/reference/metrics.html#compaction-manager)
- [ScyllaDB: compaction overview](https://docs.scylladb.com/manual/stable/kb/compaction.html)
