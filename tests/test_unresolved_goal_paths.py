from pathlib import Path

import pytest

from app.backend.chat.goal_state import Predicate, filesystem_observer


@pytest.mark.parametrize('subject', [
    '$temp_file_path', '${temp_file_path}', '%TEMP%/target.tmp', 'target.tmp',
    r'C:\Temp\$target', r'C:\Temp\${target}', r'C:\Temp\%TARGET%',
    r'C:\Temp\{{target}}',
])
@pytest.mark.parametrize('kind', ['present', 'absent'])
def test_unresolved_goal_is_unknown_not_success(subject, kind):
    assert filesystem_observer()(Predicate(kind, subject)) is None


def test_concrete_target_distinguishes_present_and_absent(tmp_path):
    file = tmp_path / 'actual.tmp'
    observer = filesystem_observer()
    assert observer(Predicate('absent', str(file))) is True
    file.write_text('test fixture')
    assert observer(Predicate('absent', str(file))) is False
    assert observer(Predicate('present', str(file))) is True


def test_inaccessible_target_is_unknown(monkeypatch, tmp_path):
    def inaccessible(*args, **kwargs):
        raise PermissionError('denied')
    monkeypatch.setattr(Path, 'stat', inaccessible)
    assert filesystem_observer()(Predicate('absent', str(tmp_path/'private.tmp'))) is None
