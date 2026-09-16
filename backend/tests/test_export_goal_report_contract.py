from types import SimpleNamespace
from ws_goals import _with_reports


def test_missing_child_report_does_not_hide_authoritative_goal_state():
    def missing(*args):raise LookupError('child report unavailable')
    runtime=SimpleNamespace(catalog=SimpleNamespace(children=SimpleNamespace(inspect=missing)))
    snapshot={'goal':{'owner_chat_id':'owner','status':'cancelled'},
              'effects':[{'kind':'agent.spawn','step_id':'one','response':{'child_id':'child'}}]}
    result=_with_reports(SimpleNamespace(require_runtime=lambda:runtime),snapshot)
    assert result['goal']['status']=='cancelled'
    assert result['reports'][0]['status']=='unavailable' and result['reports'][0]['text']==''
