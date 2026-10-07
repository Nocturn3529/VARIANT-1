import pytest

from session_runtime.registry import SessionRuntimeRegistry
from session_runtime.repository import SessionRuntimeRepository


def test_settlement_is_immutable_and_scoped_to_admission(tmp_path):
    repo=SessionRuntimeRepository(str(tmp_path/'history.sqlite3'))
    registry=SessionRuntimeRegistry(repo);registry.ensure_runtime('owner');registry.ensure_runtime('other')
    receipt={'run_id':'logical-run','status':'cancelled','settled':True,'settled_at':1,'tool_calls':300}
    first=repo.store_run_settlement('owner','first',receipt,'Partial work retained')
    assert repo.store_run_settlement('owner','first',receipt,'Partial work retained')==first
    with pytest.raises(RuntimeError):
        repo.store_run_settlement('owner','first',{**receipt,'status':'ok'},'Invented success')
    second=repo.store_run_settlement('owner','second',{**receipt,'status':'ok','settled_at':2},'Explicit resumed result')
    assert repo.get_run_settlement('owner',run_id='logical-run',admission_id='first')['receipt']['status']=='cancelled'
    assert repo.get_run_settlement('owner',run_id='logical-run')['sequence']==second['sequence']
    assert repo.get_run_settlement('other',run_id='logical-run') is None
    page=repo.run_settlement_history('owner',limit=1)
    assert page['has_more'] and page['items'][0]['receipt']['tool_calls']==300
    assert repo.run_settlement_history('owner',after=page['next_cursor'])['items'][0]['admission_id']=='second'


def test_unsettled_is_rejected_and_final_answer_truncation_is_explicit(tmp_path):
    repo=SessionRuntimeRepository(str(tmp_path/'bounded.sqlite3'))
    SessionRuntimeRegistry(repo).ensure_runtime('owner')
    with pytest.raises(ValueError):
        repo.store_run_settlement('owner','admit',{'run_id':'run','settled':False})
    result=repo.store_run_settlement('owner','admit',{'run_id':'run','settled':True},'x'*20000)
    assert result['reply_truncated'] and len(result['final_reply'])==16000


def test_existing_database_adds_history_without_reinterpreting_legacy_tickets(tmp_path):
    path = str(tmp_path/'legacy.sqlite3')
    repo = SessionRuntimeRepository(path)
    registry = SessionRuntimeRegistry(repo)
    registry.ensure_runtime('owner')
    ticket = registry.enqueue_input('owner', 'Legacy input', delivery='steer')
    with repo._connect() as conn:
        conn.execute('DROP TABLE astb_run_settlement')
        conn.execute('DROP INDEX idx_ticket_admission_proof')
    reopened = SessionRuntimeRepository(path)
    assert reopened.get_ticket(ticket.ticket_id).text == 'Legacy input'
    assert reopened.run_settlement_history('owner')['items'] == []
    assert reopened.completed_peer_ticket_ids('owner', 'new-run', 'new-admission') == []
    reopened.store_run_settlement('owner', 'new-admission', {'run_id':'new-run', 'settled':True})
    assert len(reopened.run_settlement_history('owner')['items']) == 1
