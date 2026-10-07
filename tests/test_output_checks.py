from pathlib import Path

from app.backend.chat.output_checks import check_output, verify_and_repair


def test_incomplete_game_fails_even_when_model_ended_normally():
    report = check_output('```html\n<html><script>const highScoreEl = document', request='make a game')
    assert report['status'] == 'failed'
    assert any('unclosed script' in issue for issue in report['issues'])


def test_python_is_parsed_not_executed():
    report = check_output('```python\nraise RuntimeError("must never run")\n```')
    assert report['status'] == 'unverified'
    assert check_output('```python\ndef broken(\n```')['status'] == 'failed'


def test_unavailable_browser_is_not_a_pass(tmp_path):
    report = check_output('```html\n<html></html>\n```', package_root=tmp_path)
    assert report['status'] == 'unverified'


def test_repair_checks_each_new_answer_and_preserves_evidence():
    seen=[]
    def check(answer):
        seen.append(answer)
        return {'status':'passed' if answer=='fixed' else 'failed','issues':['broken']}
    answer, report=verify_and_repair('broken',check=check,repair=lambda *_:'fixed',
        should_stop=lambda:False,publish=lambda _:None)
    assert answer=='fixed' and report['status']=='passed'
    assert seen==['broken','fixed'] and report['repairs']==1


def test_repair_is_bounded_and_does_not_turn_failure_into_success():
    repairs=[]
    def repair(answer,report,attempt):
        repairs.append(attempt)
        return answer
    _,report=verify_and_repair('bad',check=lambda _:{'status':'failed','issues':['bad']},
        repair=repair,should_stop=lambda:False,publish=lambda _:None)
    assert repairs==[1,2] and report['status']=='failed' and len(report['attempts'])==3


def test_cancel_before_check_does_not_execute_or_repair():
    def forbidden(*_):raise AssertionError('called after cancellation')
    _,report=verify_and_repair('draft',check=forbidden,repair=forbidden,
        should_stop=lambda:True,publish=forbidden)
    assert report['status']=='cancelled'


def test_unverified_is_not_retried_blindly():
    def forbidden(*_):raise AssertionError('unsupported runtime must not loop')
    _,report=verify_and_repair('text',check=lambda _:{'status':'unverified'},
        repair=forbidden,should_stop=lambda:False,publish=lambda _:None)
    assert report['status']=='unverified' and report['repairs']==0
