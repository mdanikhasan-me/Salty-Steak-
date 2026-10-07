from app.backend.chat.progress_summary import reasoning_progress_summary

def test_summary_never_quotes_trace_or_claims_tests_ran():
    key,label=reasoning_progress_summary('Secret123: I will run unittest and verify test cases later.')
    assert key=='checks'
    assert label=='Planning checks and examples'
    assert 'Secret123' not in label and 'passed' not in label

def test_latest_topic_changes_without_copying_code():
    assert reasoning_progress_summary('Plan argparse flags. Now consider malformed input errors.')[0]=='edge_cases'
    assert reasoning_progress_summary('No recognized terms here.')[0]=='reasoning'

def test_live_rate_survives_privacy_filter_without_revealing_reasoning():
    from app.backend.chat.service import _bounded_generation_preview
    p=_bounded_generation_preview({'kind':'reasoning','tail_text':'private material',
        'token_count':20,'decode_tokens_per_second':1.75,'decode_duration_seconds':10.5})
    assert p['tail_text']==''
    assert p['decode_tokens_per_second']==1.75
    assert p['decode_duration_seconds']==10.5
    for invalid in (float('nan'),float('inf'),-1,'bad'):
        p=_bounded_generation_preview({'tail_text':'answer','decode_tokens_per_second':invalid})
        assert 'decode_tokens_per_second' not in p
