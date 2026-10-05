from kernel_runtime.worker_bridge import _decode_host_result


def test_session_context_repr_only_advertises_available_fields():
    metadata = _decode_host_result({'schema':'variant1.session-context-result.v1',
                                  'view_id':'view','items':[],'counts':{}}, None)
    assert "['items']" in repr(metadata) and "['counts']" in repr(metadata)
    assert "['text']" not in repr(metadata)
    expanded = _decode_host_result({'schema':'variant1.session-context-result.v1',
                                  'view_id':'view','text':'x'*100_000}, None)
    assert "['text']" in repr(expanded) and len(repr(expanded)) < 300
    assert len(expanded['text']) == 100_000
