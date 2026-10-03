"""Stable source-owner cursor over native checkpoints, not another checkpoint.

SQLite rowids of a composite-key table can be renumbered by VACUUM or reused
after deletion. This tiny transactional index gives commits durable ordinals;
it retains no payload and changes no native checkpoint/checksum semantics.
"""
from __future__ import annotations


def initialize(conn):
    conn.executescript("""
      BEGIN IMMEDIATE;
      CREATE TABLE IF NOT EXISTS context_snapshot_order(
        ordinal INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT NOT NULL,thread_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,snapshot_id TEXT NOT NULL,UNIQUE(thread_id,sequence));
      CREATE INDEX IF NOT EXISTS context_snapshot_owner ON context_snapshot_order(chat_id,ordinal);
      CREATE TABLE IF NOT EXISTS context_snapshot_scope(chat_id TEXT PRIMARY KEY,epoch INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS context_snapshot_meta(name TEXT PRIMARY KEY);
      CREATE TRIGGER IF NOT EXISTS context_snapshot_insert AFTER INSERT ON agent_snapshots BEGIN
        INSERT INTO context_snapshot_order(chat_id,thread_id,sequence,snapshot_id)
          SELECT chat_id,new.thread_id,new.seq,new.snapshot_id FROM agent_threads WHERE thread_id=new.thread_id;
      END;
      CREATE TRIGGER IF NOT EXISTS context_snapshot_delete AFTER DELETE ON agent_snapshots BEGIN
        INSERT INTO context_snapshot_scope(chat_id,epoch)
          SELECT chat_id,1 FROM context_snapshot_order WHERE thread_id=old.thread_id AND sequence=old.seq
          ON CONFLICT(chat_id) DO UPDATE SET epoch=epoch+1;
        DELETE FROM context_snapshot_order WHERE thread_id=old.thread_id AND sequence=old.seq;
      END;
      DROP TRIGGER IF EXISTS context_snapshot_owner_change;
      CREATE TRIGGER context_snapshot_owner_change AFTER UPDATE OF chat_id,tombstoned_at ON agent_threads
      WHEN (old.chat_id IS NOT new.chat_id OR old.tombstoned_at IS NOT new.tombstoned_at)
      AND EXISTS(SELECT 1 FROM context_snapshot_order WHERE thread_id=old.thread_id) BEGIN
        INSERT INTO context_snapshot_scope(chat_id,epoch) SELECT old.chat_id,1 WHERE old.chat_id<>''
          ON CONFLICT(chat_id) DO UPDATE SET epoch=epoch+1;
        INSERT INTO context_snapshot_scope(chat_id,epoch) SELECT new.chat_id,1 WHERE old.chat_id<>'' AND new.chat_id IS NOT old.chat_id
          ON CONFLICT(chat_id) DO UPDATE SET epoch=epoch+1;
        UPDATE context_snapshot_order SET chat_id=new.chat_id WHERE thread_id=new.thread_id;
      END;
      DROP TRIGGER IF EXISTS context_snapshot_thread_delete;
      CREATE TRIGGER context_snapshot_thread_delete BEFORE DELETE ON agent_threads
      WHEN EXISTS(SELECT 1 FROM context_snapshot_order WHERE thread_id=old.thread_id) BEGIN
        INSERT INTO context_snapshot_scope(chat_id,epoch) VALUES(old.chat_id,1)
          ON CONFLICT(chat_id) DO UPDATE SET epoch=epoch+1;
        DELETE FROM context_snapshot_order WHERE thread_id=old.thread_id;
      END;
      INSERT OR IGNORE INTO context_snapshot_order(chat_id,thread_id,sequence,snapshot_id)
        SELECT t.chat_id,s.thread_id,s.seq,s.snapshot_id FROM agent_snapshots s
        JOIN agent_threads t ON t.thread_id=s.thread_id
        WHERE NOT EXISTS(SELECT 1 FROM context_snapshot_meta WHERE name='backfilled') ORDER BY s.rowid;
      INSERT OR IGNORE INTO context_snapshot_meta VALUES('backfilled');
      COMMIT;
    """)


def boundary(conn, chat_id):
    row = conn.execute('SELECT COALESCE(MAX(ordinal),0) AS through_ordinal FROM context_snapshot_order WHERE chat_id=?', (chat_id,)).fetchone()
    epoch = conn.execute('SELECT epoch FROM context_snapshot_scope WHERE chat_id=?', (chat_id,)).fetchone()
    return {'through_ordinal': row['through_ordinal'], 'epoch': epoch[0] if epoch else 0}


def page(conn, chat_id, *, after_ordinal, through_ordinal, limit):
    return [dict(row) for row in conn.execute(
        'SELECT o.ordinal,t.thread_id,s.seq AS sequence,s.snapshot_id,s.parent_snapshot_id,'
        's.run_id,t.source,s.completed_node,s.next_node,s.created_at,s.updated_at '
        'FROM context_snapshot_order o JOIN agent_threads t ON t.thread_id=o.thread_id '
        'JOIN agent_snapshots s ON s.thread_id=o.thread_id AND s.seq=o.sequence AND s.snapshot_id=o.snapshot_id '
        'WHERE o.chat_id=? AND o.ordinal>? AND o.ordinal<=? AND t.tombstoned_at IS NULL '
        'ORDER BY o.ordinal LIMIT ?', (chat_id, after_ordinal, through_ordinal, max(1, min(int(limit), 200))))]
