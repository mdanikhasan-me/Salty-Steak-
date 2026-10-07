import json

import pytest

from app.backend.chat.agent_loop import AgentLoop
from app.backend.chat.runners import LiveRunners
from app.backend.chat.task_runtime import TaskContext
from app.backend.chat.web_intent import effective_web_settings


@pytest.mark.parametrize('mode', ['instant', 'cooking', 'lock_in'])
@pytest.mark.parametrize('prompt', [
    'What is the cheapest 2TB Gen4 NVMe SSD currently in stock in Bangladesh?',
    'Find a monitor under 15000 taka from local shops only.',
    'Give me links to public observatory archives.',
])
def test_live_offers_and_links_enable_retrieval_in_every_mode(mode, prompt):
    result = effective_web_settings({'reasoning_mode': mode, 'research_available': False}, prompt)
    assert result['research_forced']
    assert result['reasoning_mode'] == mode


@pytest.mark.parametrize('prompt', [
    'What is a budget?', 'Explain how to find the cheapest route in a graph.',
    'Write a function to find links in an HTML string.',
    'Do not browse; recommend a budget category from general knowledge.',
])
def test_conceptual_work_and_opt_out_do_not_force_retrieval(prompt):
    assert not effective_web_settings({}, prompt).get('research_forced')


def test_failed_initial_choice_recovers_using_observed_error():
    class Broker:
        def __init__(self): self.calls = []
        def invoke(self, request):
            self.calls.append(request)
            if request['capability'] == 'application.launch':
                raise ValueError('No installed application matches: browser')
            return {'status': 'succeeded', 'url': 'https://example.org/', 'title': 'Example'}
    broker = Broker()
    prompts = []
    def generate(messages):
        prompts.append(messages)
        if len(prompts) == 1:
            assert 'No installed application matches' in messages[-1]['content']
            return json.dumps({'action': 'browser.control', 'reason': 'Use the owned browser.',
                               'arguments': {'command': 'open_url', 'url': 'https://example.org/'}})
        return json.dumps({'action': 'respond', 'answer': 'Opened Example.'})
    runners = LiveRunners(broker=broker, generate=generate,
        capabilities=['application.launch', 'browser.control'],
        continue_until_satisfied=True, authority_mode='full_access', task=TaskContext())
    outcome = runners.run_action(decision={'capability': 'application.launch',
        'arguments': {'target': 'browser'}}, request='Open https://example.org/')
    assert outcome['status'] == 'completed'
    assert [c['capability'] for c in broker.calls] == ['application.launch', 'browser.control']


def test_initial_permission_failure_does_not_seek_a_workaround():
    class Broker:
        def invoke(self, request): raise PermissionError('Permission denied')
    def generate(messages): raise AssertionError('No retry of permission refusal')
    outcome = LiveRunners(broker=Broker(), generate=generate,
        capabilities=['application.launch'], continue_until_satisfied=True).run_action(
            decision={'capability': 'application.launch', 'arguments': {'target': 'browser'}},
            request='Open a browser')
    assert outcome['status'] == 'failed'


def test_argument_repair_uses_structured_channel():
    def text(messages): raise AssertionError('Unconstrained generation must not repair JSON')
    runner = LiveRunners(generate=text,
        generate_structured=lambda messages: '{"target":"notepad"}')
    assert runner._repair_arguments('invalid field') == {'target': 'notepad'}


def test_task_constraints_survive_native_history_eviction():
    # The native runtime evicts old user turns. Its protected system context
    # must continue to contain the exact original budget and scope.
    task = 'Compare local offers under 15000 BDT. Do not place an order.'
    seen = []
    def generate(messages):
        seen.append(messages)
        return '{"action":"respond","answer":"No purchase made."}'
    AgentLoop(broker=object(), generate=generate, capabilities=['browser.control']).run(task)
    assert task in seen[0][0]['content']


def test_compaction_bounds_huge_recent_output_and_retains_constraints():
    from app.backend.chat.agent_loop import MAX_TRANSCRIPT_CHARACTERS
    task = 'Only local products under 15000 BDT; never purchase.'
    loop = AgentLoop(broker=object(), generate=lambda _: '', capabilities=['browser.control'])
    system = 'Rules. Current task: ' + task
    transcript = [{'role': 'system', 'content': system}, {'role': 'user', 'content': task},
                  {'role': 'assistant', 'content': 'x' * 150000},
                  {'role': 'user', 'content': 'Newest result: no products in stock.'}]
    loop._compact_transcript(transcript, system_prompt=system, task=task)
    assert sum(len(m['content']) for m in transcript) <= MAX_TRANSCRIPT_CHARACTERS
    assert task in transcript[0]['content']
    assert transcript[-1]['content'] == 'Newest result: no products in stock.'


def test_owned_browser_capture_reaches_visual_analysis():
    from app.backend.chat.agent_loop import summarise_observation
    seen=[]
    def describe(path):
        seen.append(path)
        return 'The page has a red heading and a narrow text column.'
    result=summarise_observation('browser.control',{
        'status':'succeeded','command':'capture_preview','tab':'tab-1',
        'artifact':{'path':'owned-tab.png','width':1200,'height':800}},describe_screenshot=describe)
    assert seen==['owned-tab.png']
    assert result['visual_analysis'].startswith('The page has a red heading')
    assert result['tab']=='tab-1'


def test_browser_paginated_text_is_not_clipped_a_second_time():
    from app.backend.chat.agent_loop import summarise_observation
    result=summarise_observation('browser.control',{'status':'succeeded','command':'read_page',
        'summary':'A'*3990+'last fact','text_offset':4000,'next_text_offset':8000})
    assert result['summary'].endswith('last fact')
    assert result['next_text_offset']==8000


@pytest.mark.parametrize('actual,observed,expected',[
    ('https://www.example.org/',True,True),
    ('https://www.example.org/',False,False),
    ('https://www.example.org/login',True,False),
    ('https://other.example.org/',True,False),
])
def test_browser_goal_accepts_only_observed_www_redirect(actual,observed,expected):
    from app.backend.chat.goal_state import runtime_observer, Predicate
    class Broker:
        def invoke(self, request): return {'status':'succeeded','url':actual}
    steps=[{'action':'browser.control','arguments':{'command':'open_url','url':'https://example.org/'},
            'observation':{'status':'succeeded','url':actual}}] if observed else []
    observe=runtime_observer(broker=Broker(),orchestration={'steps':steps})
    assert observe(Predicate('active_url','https://example.org/')) is expected


def test_stock_status_is_not_a_software_release_query():
    from app.backend.research.ledger import ResearchLedger
    assert not ResearchLedger('Find current SSD prices and stock status in local retailers.').is_status_question
    assert ResearchLedger('What is the latest stable Python version?').is_status_question


@pytest.mark.parametrize('text,expected',[
    ('ConditionAvailable', 'in_stock'),
    ('Availability: Available', 'in_stock'),
    ('Stock status Available', 'in_stock'),
    ('Color options available. Add to cart.', 'unknown'),
    ('ConditionAvailable. Out of stock.', 'unknown'),
    ('ConditionAvailable. Pre-order only.', 'preorder'),
    ('ConditionUnavailable', 'unknown'),
])
def test_explicit_offer_labels_are_read_without_assuming_cart_means_stock(text,expected):
    from app.backend.research.pages import availability_of
    assert availability_of({'summary':text})==expected


def test_reporting_title_after_click_does_not_require_start_url_or_file():
    from app.backend.chat.goal_state import GoalSpec, Predicate, runtime_observer
    from app.backend.chat.service import _anchor_operational_goal_spec
    task='Open https://example.org/start, click Next and report the resulting page title.'
    spec=GoalSpec(goal=task,required=(Predicate('active_url','https://example.org/start'),
        Predicate('present','$artifact'),Predicate('browser_visible','true')))
    spec=_anchor_operational_goal_spec(spec,task)
    assert spec.required==(Predicate('browser_title_reported','$observed_title'),)
    class Broker:
        def invoke(self,request):return {'status':'succeeded','url':'https://example.org/result','title':'Research results'}
    for answer,expected in [('The title is Research results.',True),('Done.',False)]:
        observe=runtime_observer(broker=Broker(),orchestration={'answer':answer})
        assert observe(spec.required[0]) is expected
