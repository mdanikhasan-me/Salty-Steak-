import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from app.backend.chat.code_verification import installed_tools, source_blocks, validate_plan, verify_project
from app.backend.automation.verification_terminal import run_check


def python_plan(test="assert add(2,3)==5\nassert add(-2,0)==-2"):
    return {"requirements":["add two numbers"],"sources":[{"block":0,"path":"calculator.py"}],
            "test_files":[{"path":"_checks/test_calc.py","content":
                "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\nfrom calculator import add\n"+test+"\nprint('CHECKS_OK')\n"}],
            "checks":[{"name":"addition behavior","kind":"test","argv":["python","_checks/test_calc.py"],
                       "requirements":[0],"stdout_contains":["CHECKS_OK"]}],"limitations":[]}


def verify(answer, tmp_path, plan, **kwargs):
    return verify_project(answer,"Write add(a,b)",work_root=tmp_path,plan_factory=lambda _:plan,
                          should_stop=kwargs.pop('should_stop',lambda:False),publish=lambda _:None,**kwargs)


def test_python_runs_behavior_and_preserves_sources_and_logs(tmp_path):
    answer="```python\ndef add(a,b): return a+b\n```"
    report=verify(answer,tmp_path,python_plan())
    assert report['status']=='passed',report
    assert report['checks'][0]['exit_code']==0
    root=Path(report['work_directory'])
    assert (root/'calculator.py').read_text()=='def add(a,b): return a+b\n'
    assert json.loads((root/'_evidence/result.json').read_text())['covered_requirements']==[0]


def test_embedded_host_uses_packaged_python_not_desktop_executable(tmp_path,monkeypatch):
    python=tmp_path/'.python/python.exe';python.parent.mkdir();python.write_bytes(b'fixture')
    host=tmp_path/'Salty Steak.exe';host.write_bytes(b'host')
    monkeypatch.setattr(sys,'executable',str(host))
    assert installed_tools(tmp_path)['python']==[str(python.resolve())]
    python.unlink()
    assert 'python' not in installed_tools(tmp_path)


def test_python_wrong_behavior_fails_with_actual_assertion(tmp_path):
    report=verify('```python\ndef add(a,b): return a-b\n```',tmp_path,python_plan())
    assert report['status']=='failed'
    assert 'AssertionError' in report['checks'][0]['stderr']


def test_test_plan_cannot_redefine_assertion_failure_as_success(tmp_path):
    plan=python_plan();plan['checks'][0].update(expected_exit_code=1,stdout_contains=[])
    result=verify('```python\ndef add(a,b):return a-b\n```',tmp_path,plan)
    assert result['status']=='unverified' and 'exit code zero' in result['issues'][0]

def test_pytest_only_skipped_does_not_verify_behavior(tmp_path):
    plan=python_plan();plan['checks'][0].update(argv=['python','-m','pytest','_checks'],stdout_contains=[])
    result=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan,
        runner=lambda *a,**kw:{'status':'completed','exit_code':0,'stdout':'1 skipped in 0.01s','stderr':''})
    assert result['status']=='unverified' and not result['covered_requirements']


def test_expected_cli_error_requires_and_checks_stderr(tmp_path):
    plan={'requirements':['reject invalid input'], 'sources':[{'block':0,'path':'main.py'}],
          'checks':[{'name':'reject invalid','kind':'run','argv':['python','main.py'],
                     'requirements':[0],'expected_exit_code':2,'stderr_contains':['invalid input']} ]}
    answer='```python\nimport sys\nprint("invalid input",file=sys.stderr)\nraise SystemExit(2)\n```'
    assert verify(answer,tmp_path,plan)['status']=='passed'
    plan['checks'][0]['stderr_contains']=['different error']
    assert verify(answer,tmp_path,plan)['status']=='failed'


def test_real_failure_repair_and_rerun_preserve_distinct_evidence(tmp_path):
    from app.backend.chat.output_checks import verify_and_repair
    bad='```python\ndef add(a,b): return a-b\n```'
    good='```python\ndef add(a,b): return a+b\n```'
    observed=[]
    def repair(draft,report,attempt):
        assert 'AssertionError' in report['checks'][0]['stderr']
        observed.append(report['artifact_sha256'])
        return good
    answer,report=verify_and_repair(bad,check=lambda draft:verify(draft,tmp_path,python_plan()),
        repair=repair,should_stop=lambda:False,publish=lambda _:None)
    assert answer==good and report['status']=='passed' and report['repairs']==1
    assert [a['status'] for a in report['attempts']]==['failed','passed']
    assert report['attempts'][0]['work_directory']!=report['attempts'][1]['work_directory']
    assert observed and observed[0]!=report['artifact_sha256']


def test_repair_reuses_original_requirements_and_harness_not_easier_new_tests(tmp_path):
    from app.backend.chat.code_verification import VerificationPlanSession
    session=VerificationPlanSession();calls=[]
    def factory(payload):
        calls.append(payload)
        assert len(calls)==1, 'A repair must not replace the test that exposed the defect'
        return python_plan()
    def check(answer):
        return verify_project(answer,'Add two numbers',work_root=tmp_path,
            plan_factory=lambda payload:session.plan(payload,factory),should_stop=lambda:False,publish=lambda _:None)
    first=check('```python\ndef add(a,b):return a-b\n```')
    second=check('```python\ndef add(a,b):return a+b\n```')
    assert first['status']=='failed' and second['status']=='passed'
    assert first['requirements']==second['requirements']
    assert first['source_hashes']['_checks/test_calc.py']==second['source_hashes']['_checks/test_calc.py']
    assert first['source_hashes']['calculator.py']!=second['source_hashes']['calculator.py']


def test_multiple_source_files_can_import_each_other(tmp_path):
    answer='```python\nfrom helper import twice\ndef add(a,b): return twice(a)+b\n```\n```python\ndef twice(a): return a*2\n```'
    plan=python_plan('assert add(2,3)==7\nassert add(0,-2)==-2')
    plan['sources'].append({'block':1,'path':'helper.py'})
    assert verify(answer,tmp_path,plan)['status']=='passed'


@pytest.mark.skipif(not shutil.which('node'),reason='Node unavailable')
def test_javascript_runs_tests_in_node(tmp_path):
    plan={'requirements':['return square'],'sources':[{'block':0,'path':'square.cjs'}],
          'test_files':[{'path':'_checks/test.cjs','content':"const assert=require('node:assert/strict');const square=require('../square.cjs');assert.equal(square(4),16);assert.equal(square(-3),9);"}],
          'checks':[{'name':'squares','kind':'test','argv':['node','_checks/test.cjs'],'requirements':[0]}]}
    assert verify('```javascript\nmodule.exports=n=>n*n;\n```',tmp_path,plan)['status']=='passed'


def test_compile_only_is_not_functionally_verified(tmp_path):
    plan=python_plan();plan['checks']=[{'name':'parse','kind':'build','argv':['python','-m','py_compile','calculator.py'],'requirements':[0]}]
    report=verify('```python\ndef add(a,b):return a-b\n```',tmp_path,plan)
    assert report['status']=='unverified' and report['checks'][0]['passed']


def test_missing_tool_is_unverified_not_repaired_as_code_failure(tmp_path):
    plan=python_plan();plan['checks'][0]['argv']=['no_such_compiler','_checks/test_calc.py']
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan,tools={'python':[sys.executable]})
    assert report['status']=='unverified'
    assert 'not available' in report['issues'][0]


def test_changed_delivered_code_invalidates_test_result(tmp_path):
    plan=python_plan("from pathlib import Path\nPath('calculator.py').write_text('replacement')")
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan)
    assert report['status']=='failed'
    assert any('changed a source' in issue for issue in report['issues'])


@pytest.mark.parametrize('path',['../escape.py','C:\\outside.py','a:stream','CON.py','/outside.py','_evidence/result.json'])
def test_plan_paths_cannot_escape_or_replace_evidence(tmp_path,path):
    plan=python_plan();plan['sources'][0]['path']=path
    report=verify('```python\nx=1\n```',tmp_path,plan)
    assert report['status']=='unverified'
    assert not list(tmp_path.iterdir())


def test_source_map_cannot_omit_a_file(tmp_path):
    report=verify('```python\nx=1\n```\n```python\ny=2\n```',tmp_path,python_plan())
    assert report['status']=='unverified' and 'Every source' in report['issues'][0]


def test_case_collisions_rejected(tmp_path):
    plan=python_plan();plan['support_files']=[{'path':'Calculator.py','content':'replacement'}]
    assert verify('```python\nx=1\n```',tmp_path,plan)['status']=='unverified'


def test_cancel_does_not_plan_or_start_process(tmp_path):
    def forbidden(_):raise AssertionError('planner invoked')
    report=verify_project('```python\nx=1\n```','test',work_root=tmp_path,
                          plan_factory=forbidden,should_stop=lambda:True,publish=lambda _:None)
    assert report['status']=='cancelled' and not list(tmp_path.iterdir())


def test_timeout_and_output_are_bounded(tmp_path):
    result=run_check([sys.executable,'-u','-c',"import time;print('x'*50000);time.sleep(20)"],tmp_path,
                     should_stop=lambda:False,timeout_seconds=.3,output_limit=1000)
    assert result['status']=='timed_out'
    assert result['output_truncated'] and len(result['stdout'])<=1000
    assert result['duration_seconds']<8


def test_cancel_running_command(tmp_path):
    stop=threading.Event();timer=threading.Timer(.3,stop.set);timer.start()
    try:
        result=run_check([sys.executable,'-c','import time;time.sleep(20)'],tmp_path,should_stop=stop.is_set)
    finally:timer.cancel()
    assert result['status']=='cancelled' and result['duration_seconds']<8


def test_output_assertion_is_enforced(tmp_path):
    plan=python_plan();plan['test_files'][0]['content']="print('wrong')\n"
    plan['checks'][0].update(kind='run',argv=['python','_checks/test_calc.py'],stdout_contains=['expected'])
    report=verify('```python\nx=1\n```',tmp_path,plan)
    assert report['status']=='failed' and report['checks'][0]['exit_code']==0


def test_no_dependency_installation(tmp_path):
    plan=python_plan();plan['checks'][0].update(kind='run',argv=['python','-m','pip','install','example'])
    report=verify('```python\nx=1\n```',tmp_path,plan)
    assert report['status']=='unverified' and 'install' in report['issues'][0]


def test_model_plan_cannot_claim_pass_without_running_a_harness(tmp_path):
    plan=python_plan();plan['test_files']=[]
    plan['checks'][0]['argv']=['python','-c','pass']
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan)
    assert report['status']=='unverified' and 'harness' in report['issues'][0]


def test_supplied_but_unused_harness_is_not_a_pass(tmp_path):
    plan=python_plan();plan['checks'][0]['argv']=['python','calculator.py']
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan)
    assert report['status']=='unverified' and 'harness' in report['issues'][0]


def test_eval_snippet_cannot_masquerade_as_cli_behavior(tmp_path):
    plan=python_plan();plan['checks'][0].update(kind='run',argv=['python','-c','print("CHECKS_OK")'])
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan)
    assert report['status']=='unverified' and 'eval' in report['issues'][0]


def test_missing_module_is_a_prerequisite_not_a_code_failure(tmp_path):
    plan=python_plan();plan['test_files'][0]['content']='import salty_missing_dependency_987654\n'
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan)
    assert report['status']=='unverified'
    assert 'prerequisite' in report['issues'][0]


def test_zero_tests_is_not_a_pass(tmp_path):
    plan=python_plan();plan['checks'][0]['stdout_contains']=[]
    plan['test_files'][0]['content']='print("Ran 0 tests")\n'
    report=verify('```python\ndef add(a,b):return a+b\n```',tmp_path,plan)
    assert report['status']=='unverified' and report['covered_requirements']==[]


@pytest.mark.skipif(not shutil.which('dotnet'),reason='.NET SDK unavailable')
def test_csharp_build_and_behavior_tests(tmp_path):
    plan={'requirements':['add numbers correctly'],'sources':[{'block':0,'path':'Calculator.cs'}],
          'support_files':[{'path':'Check.csproj','content':'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'}],
          'test_files':[{'path':'_checks/Program.cs','content':'using System; class Program { static int Main() { if (Calculator.Add(2,3)!=5 || Calculator.Add(-2,1)!=-1) return 1; Console.WriteLine("CHECKS_OK"); return 0; } }'}],
          'checks':[{'name':'build C#','kind':'build','argv':['dotnet','build','Check.csproj','--ignore-failed-sources','--verbosity','quiet'],'requirements':[],'timeout_seconds':120},
                    {'name':'C# behavior assertions','kind':'test','argv':['dotnet','bin/Debug/net8.0/Check.dll'],'requirements':[0],'stdout_contains':['CHECKS_OK']}]}
    report=verify('```csharp\npublic static class Calculator { public static int Add(int a,int b) => a+b; }\n```',tmp_path,plan)
    assert report['status']=='passed',report
