from chat_session import ConnectionSession
from chat_stream import stream_meta
from run_context import Variant1RunContext, bind_run_context


def test_native_goal_start_has_exact_run_and_admission_identity():
    session=ConnectionSession(viewed_session_id='parent')
    session.active.runtime_chat_id='parent'
    session.active.runtime_admission_id='admission'
    session.active.turn_source='goal'
    session.active.turn_client_id='effect'
    with bind_run_context(Variant1RunContext.create(source='goal',session_id='parent',run_id='run')):
        assert stream_meta(session)=={'client_id':'effect','source':'goal','session_id':'parent',
                                     'admission_id':'admission','run_id':'run'}
    with bind_run_context(Variant1RunContext.create(source='subagent',session_id='child',run_id='foreign-run')):
        assert 'run_id' not in stream_meta(session)
