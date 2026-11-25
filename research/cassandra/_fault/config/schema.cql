CREATE KEYSPACE IF NOT EXISTS compaction_lab
WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3};

CREATE TABLE IF NOT EXISTS compaction_lab.events (
    id uuid PRIMARY KEY,
    payload blob,
    sequence_no bigint,
    created_at timestamp
) WITH compaction = {
    'class': 'LeveledCompactionStrategy',
    'sstable_size_in_mb': '16'
}
AND compression = {'class': 'LZ4Compressor'}
AND memtable_flush_period_in_ms = 3000
AND speculative_retry = 'NONE';

