"""Chat-scoped immutable settlement observations, separate from checkpoints."""
from __future__ import annotations

import json
import time

from core_invariants import canonical_json

RUN_HISTORY_SQL = '''
CREATE TABLE IF NOT EXISTS astb_run_settlement (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    admission_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    final_reply TEXT NOT NULL,
    reply_truncated INTEGER NOT NULL,
    settled_at REAL NOT NULL,
    UNIQUE(chat_id, admission_id),
    FOREIGN KEY(chat_id) REFERENCES astb_chat_runtime(chat_id)
);
CREATE INDEX IF NOT EXISTS idx_run_settlement_identity
    ON astb_run_settlement(chat_id, run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_run_settlement_chat_cursor
    ON astb_run_settlement(chat_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ticket_admission_proof
    ON astb_input_ticket(chat_id,json_extract(proof_json,'$.admission_id'),state,ticket_id);
'''


class RunSettlementHistory:
    def completed_peer_ticket_ids(self, chat_id, run_id, admission_id, *, after='', limit=200):
        cap = max(1, min(200, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ticket_id FROM astb_input_ticket WHERE chat_id=? "
                "AND json_extract(proof_json,'$.admission_id')=? AND state='completed' "
                "AND json_extract(proof_json,'$.run_id')=? AND source LIKE 'peer:%' "
                "AND ticket_id>? ORDER BY ticket_id LIMIT ?",
                (str(chat_id), str(admission_id), str(run_id), str(after), cap),
            ).fetchall()
        return [row['ticket_id'] for row in rows]

    def store_run_settlement(self, chat_id, admission_id, receipt, final_reply=''):
        if (not chat_id or not admission_id or not receipt.get('run_id')
                or receipt.get('settled') is not True):
            raise ValueError('A settlement needs exact chat/run/admission identities')
        from observability.display_projection import safe_display
        reply = str(safe_display(str(final_reply or '')))
        payload = canonical_json(receipt)
        if len(payload.encode('utf-8')) > 256 * 1024:
            raise ValueError('Settlement receipt exceeds its bounded record size')
        values = (
            str(chat_id), str(admission_id), str(receipt['run_id']), payload,
            reply[:16000], int(len(reply) > 16000),
            float(receipt.get('settled_at') or time.time()),
        )
        with self._transaction(immediate=True) as conn:
            old = conn.execute(
                'SELECT * FROM astb_run_settlement WHERE chat_id=? AND admission_id=?',
                values[:2],
            ).fetchone()
            if old is not None:
                keys = ('chat_id', 'admission_id', 'run_id', 'receipt_json', 'final_reply', 'reply_truncated')
                if tuple(old[k] for k in keys) != values[:6]:
                    raise RuntimeError('Conflicting terminal settlement for an admission')
            else:
                conn.execute(
                    'INSERT INTO astb_run_settlement('
                    'chat_id,admission_id,run_id,receipt_json,final_reply,reply_truncated,settled_at'
                    ') VALUES(?,?,?,?,?,?,?)', values,
                )
        return self.get_run_settlement(chat_id, run_id=receipt['run_id'], admission_id=admission_id)

    @staticmethod
    def _settlement_row(row):
        if row is None:
            return None
        result = dict(row)
        result['receipt'] = json.loads(result.pop('receipt_json'))
        result['reply_truncated'] = bool(result['reply_truncated'])
        result['basis'] = 'native_turn_settlement'
        return result

    def get_run_settlement(self, chat_id, *, run_id, admission_id=''):
        if not run_id:
            return None
        where, args = 'chat_id=? AND run_id=?', [str(chat_id), str(run_id)]
        if admission_id:
            where += ' AND admission_id=?'
            args.append(str(admission_id))
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM astb_run_settlement WHERE ' + where + ' ORDER BY sequence DESC LIMIT 1',
                args,
            ).fetchone()
        return self._settlement_row(row)

    def run_settlement_history(self, chat_id, *, after=0, limit=50):
        cap = max(1, min(200, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM astb_run_settlement WHERE chat_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                (str(chat_id), max(0, int(after)), cap + 1),
            ).fetchall()
        return {
            'items': [self._settlement_row(row) for row in rows[:cap]],
            'has_more': len(rows) > cap,
            'next_cursor': rows[cap - 1]['sequence'] if len(rows) > cap else None,
        }
