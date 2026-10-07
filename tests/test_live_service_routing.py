"""End-to-end through the real _generate_turn.

A user message goes into the production chat path, the model reply routes it,
and the persisted conversation is inspected. Only the model itself is scripted.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.application import Application

BOILABIN = (
    "Create a distinctive premium Boilabin ecommerce brand logo and icon. "
    "Boilabin is an ecommerce marketplace. Deliverables: a standalone symbol, a "
    "horizontal lockup with the Boilabin wordmark, and a small monochrome "
    "version. Minimal, premium, flat vector. Do not use a shopping cart. No "
    "shopping bag. Avoid a delivery truck. No generic globe."
)

IMAGE_REPLY = (
    "<think>\nThe user wants a brand identity.\n</think>\n\n"
    "```json\n"
    + json.dumps(
        {
            "action": "generate_image",
            "reason": "The user asked for a brand identity.",
            "brief": {
                "subject": "Boilabin ecommerce brand identity",
                "image_type": "logo",
                "brand": "Boilabin",
                "deliverables": [
                    "standalone symbol",
                    "horizontal symbol with Boilabin wordmark",
                    "small monochrome version",
                ],
            },
        }
    )
    + "\n```"
)


@pytest.fixture()
def application(tmp_path: Path):
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "local.toml").write_text(
        '[training]\ndevice="cpu"\nprecision="fp32"\n[server]\nhost="127.0.0.1"\nport=0\n',
        encoding="utf-8",
    )
    app = Application(tmp_path, recover_operations=False)
    yield app
    app.close()


def _wire(app, tmp_path: Path, reply: str, *, image_ready: bool = True):
    """Give the application a scripted model and a stated image runtime."""

    source_sha256 = "8" * 64
    captured: dict = {"messages": None}

    class FakeRuntime:
        profile = SimpleNamespace(profile_id="routing-test", context_limit=32768)
        loaded = True

        def describe(self) -> dict:
            return {
                "loaded": self.loaded,
                "runtime_id": "91000000-0000-4000-8000-000000000099",
                "configured_context_limit": 32768,
                "source_sha256": source_sha256,
                "model_path": str(tmp_path / "base.gguf"),
            }

        def load(self) -> dict:
            self.loaded = True
            return self.describe()

        warmup = load

        def unload(self) -> None:
            self.loaded = False

        @staticmethod
        def generate(**kwargs):
            # Kept so a test can inspect exactly what the model was shown.
            # The routing pass is the first: an image turn makes a second call
            # to author the render brief, and recording only the newest one
            # left these assertions reading the brief instruction instead of
            # the capability manifest they are about.
            if captured["messages"] is None:
                captured["messages"] = list(kwargs.get("messages") or [])
                captured["reasoning_mode"] = kwargs.get("reasoning_mode")
            captured.setdefault("calls", []).append(list(kwargs.get("messages") or []))
            generated_reply = reply(kwargs) if callable(reply) else reply
            return SimpleNamespace(
                cancelled=False,
                text=generated_reply,
                token_ids=[1, 2],
                omitted_turns=0,
                finish_reason="end_of_generation",
                technical_details={"finish_reason": "end_of_generation"},
            )

    runtime = FakeRuntime()
    app.model_bundle_runtime = runtime
    app.chat.model_bundle_runtime = runtime
    app.chat.model_bundle = {
        "id": "base-steak-2-0-9b",
        "display_name": "Base Steak 2.0",
        "checksum": source_sha256,
    }
    app.chat.image_generation_model = {
        "activation_allowed": image_ready,
        "external_service_required": False,
        "runtime_loaded": False,
        "runtime_reason": None if image_ready else "Missing local image pipeline",
    }
    return captured


def _send(app, text: str, *, settings=None):
    conversation = app.create_conversation()
    operation = app.chat.start_message(conversation["id"], text, generation_settings=settings)
    completed = app.operations.wait(operation["id"], timeout=20)
    assert completed["state"] == "completed", completed
    messages = app.get_conversation(conversation["id"])["messages"]
    return conversation["id"], messages


@pytest.mark.parametrize('change_profile',[False,True])
def test_internal_review_deadline_recovery_commits_unchanged_model_answer(application,tmp_path,monkeypatch,change_profile):
    from app.backend.chat.runners import LiveRunners
    _wire(application,tmp_path,'A recovered answer.')
    application.chat.model_bundle['runtime_family']='salty_native_steak35'
    runtime=application.chat.model_bundle_runtime
    original_generate=runtime.generate;original_describe=runtime.describe
    state={'reloaded':False}
    def describe():
        result=original_describe()
        if state['reloaded']:result['runtime_id']='91000000-0000-4000-8000-000000000100'
        return result
    runtime.describe=describe;runtime.warmup=describe
    def generate(**kwargs):
        response=original_generate(**kwargs)
        if kwargs['messages'][0]['content']=='Timeout probe':
            state['reloaded']=True;response.cancelled=True;response.text='';response.finish_reason='cancelled'
        return response
    runtime.generate=generate
    def research(self,**kwargs):
        self.generate_research_review([{'role':'system','content':'Timeout probe'}],time_budget_seconds=0)
        answer=self.generate_with_preview([{'role':'system','content':'Answer probe'}],lambda _:None)
        if change_profile:
            with application.database.transaction() as connection:
                connection.execute("UPDATE active_chat_runtime SET profile_id='another-profile' WHERE singleton=1")
        return {'answer':answer,'status':'completed','sources':[],'claims':[]}
    monkeypatch.setattr(LiveRunners,'run_research',research)
    if change_profile:
        conversation=application.create_conversation()
        operation=application.chat.start_message(conversation['id'],'Search the public web for a report.')
        result=application.operations.wait(operation['id'],timeout=20)
        assert result['state']=='failed'
        assert 'active model bundle changed' in str(result['error'])
        return
    _,messages=_send(application,'Search the public web for a report.')
    assert messages[-1]['content']=='A recovered answer.'
    recovery=messages[-1]['technical_details']['runtime_recovery']
    assert recovery['runtime_id'].endswith('099') and recovery['recovered_runtime_id'].endswith('100')


# ------------------------------------------------- routing through the path


def test_a_branding_request_reaches_image_generation_with_no_keyword(
    application, tmp_path: Path
) -> None:
    """The exact request that used to fall through the regex."""

    _wire(application, tmp_path, IMAGE_REPLY)

    conversation_id, messages = _send(application, BOILABIN)

    details = messages[1]["technical_details"]
    proposal = details["host_action_proposal"]
    assert proposal["kind"] == "image.generate"
    assert proposal["state"] == "pending_review"
    # The prompt is the checked render brief, not the user's raw message.
    prompt = proposal["arguments"]["prompt"]
    assert "TASK: logo" in prompt
    assert "Boilabin" in prompt
    assert "standalone symbol" in prompt
    # Prohibitions travel as a separate negative prompt. In the positive one
    # they are things to draw, which is how DO NOT INCLUDE ended up written
    # across a real render.
    assert "DO NOT INCLUDE" not in prompt
    negative_prompt = proposal["arguments"]["negative_prompt"].casefold()
    for forbidden in ("cart", "bag", "truck", "globe"):
        assert forbidden in negative_prompt
        assert forbidden not in prompt.casefold()
    # The request itself authorizes the render; no second confirmation is owed.
    # This fixture has no diffusion worker, so the automatic start falls back to
    # the still-persisted proposal after the runtime call fails.
    assert proposal["requires_confirmation"] is False
    assert proposal["execution_allowed"] is True
    assert proposal["authorization_source"] == "explicit_conversation_image_request"


def test_explicit_image_request_starts_without_a_second_confirmation(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(application, tmp_path, IMAGE_REPLY)
    started: list[dict[str, object]] = []

    def start_image(
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        generation_settings=None,
    ) -> dict[str, str]:
        started.append(
            {
                "conversation_id": conversation_id,
                "proposal_id": proposal_id,
                "assistant_message_id": assistant_message_id,
                "generation_settings": generation_settings,
            }
        )
        return {"id": "automatic-image-operation"}

    monkeypatch.setattr(application.chat, "confirm_image_generation", start_image)
    conversation_id, messages = _send(application, BOILABIN)

    assert len(started) == 1
    assert started[0]["conversation_id"] == conversation_id
    proposal = messages[1]["technical_details"]["host_action_proposal"]
    assert started[0]["proposal_id"] == proposal["id"]
    assert proposal["requires_confirmation"] is False
    assert messages[1]["technical_details"]["turn_completion"] == "rendering"


def test_explicit_image_mode_reuses_a_complete_model_authored_brief(
    application, tmp_path: Path
) -> None:
    complete_reply = json.dumps(
        {
            "subject": "a copper lighthouse at blue hour",
            "image_type": "illustration",
            "style": "cinematic atmospheric lighting",
            "composition": "low angle with the glowing lantern centered",
            "colour": "deep blue twilight and warm copper",
            "background": "calm reflective water and distant rocks",
            "required_elements": ["copper lighthouse", "glowing lantern"],
        }
    )
    captured = _wire(application, tmp_path, complete_reply)

    _conversation_id, messages = _send(
        application,
        "Generate an image of a copper lighthouse at blue hour.",
        settings={"image_mode": True},
    )

    authoring_calls = [
        call
        for call in captured.get("calls", [])
        if any(
            message["role"] == "system"
            and "diffusion image model" in message["content"]
            for message in call
        )
    ]
    assert len(authoring_calls) == 1
    # Image mode is already an explicit command. The only model call authors
    # the visual brief; no separate generation repeats the route decision.
    assert len(captured.get("calls", [])) == 1
    proposal = messages[1]["technical_details"]["host_action_proposal"]
    assert "copper lighthouse" in proposal["arguments"]["prompt"].casefold()
    assert "deep blue twilight" in proposal["arguments"]["prompt"].casefold()


def test_the_image_job_is_persisted_against_the_conversation(
    application, tmp_path: Path
) -> None:
    _wire(application, tmp_path, IMAGE_REPLY)

    conversation_id, _ = _send(application, BOILABIN)

    stored = application.chat.image_store.latest_for_conversation(conversation_id)
    assert stored is not None
    assert stored.brief.brand == "Boilabin"
    assert stored.revision == 1
    # Survives a fresh reader, as after a restart.
    from app.backend.imaging.store import ImageJobStore

    assert ImageJobStore(application.database).latest_for_conversation(
        conversation_id
    ).job_id == stored.job_id


def test_an_ordinary_question_stays_an_ordinary_answer(
    application, tmp_path: Path
) -> None:
    """Routing must not turn every descriptive request into a picture."""

    _wire(application, tmp_path, "LoRA trains small low-rank adapters instead of the full weights.")

    conversation_id, messages = _send(application, "Explain how LoRA works.")

    details = messages[1]["technical_details"]
    assert "host_action_proposal" not in details
    assert "LoRA" in messages[1]["content"]
    assert application.chat.image_store.latest_for_conversation(conversation_id) is None


def test_research_availability_never_runs_a_keyword_prefetch(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _wire(
        application,
        tmp_path,
        "I would need to research that before making a current claim.",
    )
    searches: list[str] = []
    monkeypatch.setattr(
        application.chat.web_search,
        "search",
        lambda query, limit=6: searches.append(query) or [],
    )

    _conversation_id, messages = _send(
        application,
        "What is the latest CUDA release?",
        settings={"research_available": True, "web_search_enabled": True},
    )

    assert searches == []
    assert "research" in " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    details = messages[1]["technical_details"]
    assert details["web_search"]["state"] == "available"

def test_explicit_research_control_reaches_research_even_when_model_answers_normally(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backend.chat.runners import LiveRunners
    _wire(application, tmp_path, "An answer from memory is not enough here.")
    calls=[]
    def research(self, *, decision, request):
        calls.append((decision, request, self.research_profile))
        return {"answer":"Verified with public sources.","status":"completed","sources":[],"claims":[]}
    monkeypatch.setattr(LiveRunners,"run_research",research)
    _,messages=_send(application,"How do event loops work?",settings={"research_available":True,"research_command":True})
    assert len(calls)==1, messages[-1]
    assert calls[0][2]=="instant"
    assert "Verified with public sources" in messages[-1]["content"]


def test_plain_language_web_request_reaches_research_with_toggle_off(application, tmp_path, monkeypatch):
    from app.backend.chat.runners import LiveRunners
    _wire(application, tmp_path, "A memory-only answer.")
    calls=[]
    def research(self, *, decision, request):
        calls.append((decision, request, self.research_profile))
        return {"answer":"Retrieved the requested public report.","status":"completed","sources":[],"claims":[]}
    monkeypatch.setattr(LiveRunners,"run_research",research)
    _,messages=_send(application,"Search the public web for the original observatory yearbook.",
                     settings={"research_available":False,"web_search_enabled":False})
    assert len(calls)==1
    assert calls[0][2]=='instant'
    assert 'Retrieved the requested' in messages[-1]['content']
    assert messages[-1]['technical_details']['web_intent_source']=='explicit_user_request'


def test_research_off_never_offers_web_even_when_runtime_is_available(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _wire(application, tmp_path, "A plain answer.")
    monkeypatch.setattr(application.chat, "_research_runtime_available", lambda: True)
    searches: list[str] = []
    monkeypatch.setattr(application.chat.web_search, "search", lambda query, limit=6: searches.append(query) or [])

    _conversation_id, messages = _send(
        application,
        "What is the current CUDA release?",
        settings={"research_available": False, "web_search_enabled": True},
    )

    system_text = " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    assert '"research"' not in system_text
    assert searches == []
    details = messages[1]["technical_details"]
    assert details["research_profile"] == "verification"
    assert details["web_search"]["state"] == "off"


def test_learned_research_prediction_becomes_a_direct_answer_when_research_is_off(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(application, tmp_path, "A Python list can be changed in place.")
    monkeypatch.setattr(
        application.chat,
        "_learned_route_decision",
        lambda *_args, **_kwargs: ("research", {"route": "research"}),
    )
    searches: list[str] = []
    monkeypatch.setattr(
        application.chat.web_search,
        "search",
        lambda query, limit=6: searches.append(query) or [],
    )

    _, messages = _send(application, "Explain a Python list.")

    assert searches == []
    assert messages[1]["content"] == "A Python list can be changed in place."
    controller = messages[1]["technical_details"]["learned_route_controller"]
    assert controller["route_correction"] == "research_control_off"


def test_steak35_without_routing_adapter_classifies_a_direct_chat_turn(
    application, tmp_path: Path
) -> None:
    from app.backend.training.route_dataset import ROUTE_SYSTEM

    def reply(kwargs):
        messages = kwargs.get("messages") or []
        if messages and messages[0].get("content") == ROUTE_SYSTEM:
            return "A"
        return "Python lists can be changed in place."

    captured = _wire(application, tmp_path, reply)
    application.chat.model_bundle["runtime_family"] = "salty_native_steak35"

    _, messages = _send(application, "Explain Python lists.")

    assert len(captured["calls"]) >= 2
    assert messages[1]["content"] == "Python lists can be changed in place."
    controller = messages[1]["technical_details"]["learned_route_controller"]
    assert controller["controller"] == "base_model_constrained_route_classifier"
    assert controller["route"] == "respond"


def test_steak35_cooking_can_finish_reasoning_and_answer_without_forced_repair(
    application, tmp_path: Path
) -> None:
    from app.backend.training.route_dataset import ROUTE_SYSTEM

    def reply(kwargs):
        messages = kwargs.get("messages") or []
        if messages and messages[0].get("content") == ROUTE_SYSTEM:
            return "A"
        if kwargs.get("reasoning_mode") == "cooking":
            assert kwargs["maximum_output_tokens"] == 1024
            return (
                "<think>List contents may be changed in place.</think>"
                "A Python list is mutable: you can change its contents in place."
            )
        return "A Python list is mutable: you can change its contents in place."

    captured = _wire(application, tmp_path, reply)
    application.chat.model_bundle["runtime_family"] = "salty_native_steak35"

    _, messages = _send(
        application,
        "Explain why Python lists are mutable.",
        settings={"reasoning_mode": "cooking", "maximum_output_mode": "automatic"},
    )

    assert "mutable" in messages[1]["content"]
    assert "<think>" not in messages[1]["content"]
    assert any(call and call[0].get("content") == ROUTE_SYSTEM for call in captured["calls"])
    assert len(captured["calls"]) == 2
    assert messages[1]["technical_details"]["direct_response_recovery"] is None


@pytest.mark.parametrize('mode',['instant','cooking'])
@pytest.mark.parametrize('workspace',['chat','code'])
def test_short_prompt_can_produce_a_complete_long_answer(application,tmp_path,mode,workspace):
    from app.backend.training.route_dataset import ROUTE_SYSTEM
    long_answer='\n'.join(f'{i}. This is a complete section of the requested guide.' for i in range(1,151))
    answer_limits=[]
    def reply(kwargs):
        if kwargs['messages'][0].get('content')==ROUTE_SYSTEM:return 'A'
        answer_limits.append(kwargs['maximum_output_tokens'])
        if kwargs.get('reasoning_mode')=='cooking':
            assert kwargs['maximum_output_tokens']==1024
            return '<think>Prepare all sections.</think>'
        assert kwargs['maximum_output_tokens']==16384
        assert '</think>' not in kwargs.get('stop_sequences',[])
        return long_answer
    _wire(application,tmp_path,reply)
    application.chat.model_bundle['runtime_family']='salty_native_steak35'
    conversation=application.create_conversation(workspace)
    op=application.chat.start_message(conversation['id'],'Write a comprehensive guide.',generation_settings={
        'workspace_mode':workspace,'reasoning_mode':mode,'maximum_output_mode':'automatic',
        'context_window_tokens':32768})
    completed=application.operations.wait(op['id'],timeout=20)
    assert completed['state']=='completed', completed
    answer=application.get_conversation(conversation['id'])['messages'][-1]
    assert answer['content']==long_answer
    assert answer_limits==([1024,16384] if mode=='cooking' else [16384])


def test_cancelled_cooking_does_not_start_an_answer_or_repair(application,tmp_path):
    from app.backend.training.route_dataset import ROUTE_SYSTEM
    captured=_wire(application,tmp_path,lambda kwargs:'A' if kwargs['messages'][0].get('content')==ROUTE_SYSTEM else '<think>unfinished')
    application.chat.model_bundle['runtime_family']='salty_native_steak35'
    original=application.chat.model_bundle_runtime.generate
    def generate(**kwargs):
        result=original(**kwargs)
        if kwargs.get('reasoning_mode')=='cooking':result.cancelled=True
        return result
    application.chat.model_bundle_runtime.generate=generate
    conv=application.create_conversation('chat')
    op=application.chat.start_message(conv['id'],'Write a report.',generation_settings={'reasoning_mode':'cooking'})
    done=application.operations.wait(op['id'],timeout=20)
    assert done['state']=='interrupted'
    assert len(captured['calls'])==2
    assert not any(m['role']=='assistant' for m in application.get_conversation(conv['id'])['messages'])


def test_empty_cooking_phase_starts_answer_without_extra_memo(application,tmp_path):
    from app.backend.training.route_dataset import ROUTE_SYSTEM
    def reply(kwargs):
        if kwargs['messages'][0].get('content')==ROUTE_SYSTEM:return 'A'
        return '<think></think>' if kwargs.get('reasoning_mode')=='cooking' else 'A complete answer.'
    captured=_wire(application,tmp_path,reply)
    application.chat.model_bundle['runtime_family']='salty_native_steak35'
    _,messages=_send(application,'Explain it.',settings={'reasoning_mode':'cooking'})
    assert len(captured['calls'])==3
    assert messages[-1]['content']=='A complete answer.'
    assert messages[-1]['technical_details']['native_cooking_budget']['answer_pass_started'] is True
    assert messages[-1]['technical_details']['direct_response_recovery'] is None
    assert all(e['state']!='running' for e in messages[-1]['technical_details']['activity_journal'] if e['id']=='cooking-answer')


def test_lock_in_uses_max_reasoning_then_checks_repairs_and_rechecks(application,tmp_path,monkeypatch):
    from app.backend.chat import output_checks
    from app.backend.training.route_dataset import ROUTE_SYSTEM
    seen=[]
    def check(text,**kwargs):
        seen.append(text)
        return {'status':'passed' if 'fixed' in text else 'failed','issues':[] if 'fixed' in text else ['broken start button'],'scope':'scripted browser check'}
    monkeypatch.setattr(output_checks,'check_output',check)
    def reply(kwargs):
        first=kwargs['messages'][0].get('content','')
        if first==ROUTE_SYSTEM:return 'A'
        if first.startswith('Review the supplied answer'):return '{"needs_revision":false,"issues":[]}'
        if first.startswith('Revise the draft'):return 'The fixed answer.'
        if kwargs.get('reasoning_mode')=='cooking':
            assert kwargs['maximum_output_tokens']==8192
            return '<think>Check the implementation carefully.</think>'
        return 'The broken answer.'
    _wire(application,tmp_path,reply);application.chat.model_bundle['runtime_family']='salty_native_steak35'
    _,messages=_send(application,'Create a working example.',settings={'reasoning_mode':'lock_in','context_window_tokens':32768})
    answer=messages[-1]
    assert answer['content']=='The fixed answer.'
    assert seen==['The broken answer.','The fixed answer.']
    assert answer['technical_details']['lock_in_verification']['repairs']==1
    assert answer['technical_details']['response_mode']=='lock_in'


def test_lock_in_reports_unverified_when_no_execution_check_exists(application,tmp_path,monkeypatch):
    from app.backend.chat import output_checks
    from app.backend.training.route_dataset import ROUTE_SYSTEM
    monkeypatch.setattr(output_checks,'check_output',lambda *a,**k:{'status':'unverified','issues':[],'scope':'No execution check for prose.'})
    def reply(kwargs):
        first=kwargs['messages'][0].get('content','')
        if first==ROUTE_SYSTEM:return 'A'
        if first.startswith('Review the supplied answer'):return '{"needs_revision":false,"issues":[]}'
        return '<think>Consider it.</think>' if kwargs.get('reasoning_mode')=='cooking' else 'A useful explanation.'
    _wire(application,tmp_path,reply);application.chat.model_bundle['runtime_family']='salty_native_steak35'
    _,messages=_send(application,'Explain this.',settings={'reasoning_mode':'lock_in'})
    assert 'not fully runtime-verified' in messages[-1]['content']
    assert messages[-1]['technical_details']['lock_in_verification']['status']=='unverified'


def test_lock_in_research_review_keeps_sources_and_never_generates_a_code_test_plan(application,tmp_path,monkeypatch):
    from app.backend.chat.runners import LiveRunners
    from app.backend.chat import output_checks
    def no_code(*args, **kwargs):
        raise AssertionError('Research must not enter code checks')
    monkeypatch.setattr(output_checks, 'check_output', no_code)
    bad = 'Buy this product for 35000 BDT: [item](https://shop.example/item).'
    good = 'No offer under 35000 BDT was confirmed. The observed offer was exactly 35000 BDT.'
    observations = [{'product':'SSD','price':'35000','price_display':'35000 BDT',
        'currency_code':'BDT','stock':'in_stock','url':'https://shop.example/item'}]
    monkeypatch.setattr(LiveRunners, 'run_research', lambda self,**kwargs: {
        'answer':bad,'status':'completed','claims':[],'observations':observations,
        'sources':[{'url':'https://shop.example/item','validation':'validated'}],
        'evidence_limits':{},'research':{'evidence_sufficient':True}})
    reviews=[]
    def reply(kwargs):
        first=kwargs['messages'][0]['content']
        packet=json.loads(kwargs['messages'][-1]['content'])
        if first.startswith('Review this research answer'):
            reviews.append(packet)
            assert packet['retrieved_evidence']['verified_products'][0]['price']=='35000 BDT'
            return json.dumps({'needs_revision':packet['answer']==bad,
                'issues':['Equal to the limit is not under the limit.'] if packet['answer']==bad else []})
        if first.startswith("Answer the user's question"):
            assert packet['evidence']['question']=='Find an SSD under 35000 BDT from local shops only.'
            assert 'Do not create code' in first
            return good
        raise AssertionError('Unexpected generation '+first[:150])
    _wire(application,tmp_path,reply)
    application.chat.model_bundle['runtime_family']='salty_native_steak35'
    _,messages=_send(application,'Find an SSD under 35000 BDT from local shops only.',
        settings={'reasoning_mode':'lock_in','context_window_tokens':32768})
    answer=messages[-1]
    assert answer['content']==good
    assert len(reviews)==2
    assert answer['technical_details']['lock_in_verification']['kind']=='research'
    assert answer['technical_details']['lock_in_verification']['repairs']==1
    assert answer['technical_details']['orchestration']['answer']==good


def test_lock_in_runs_real_terminal_tests_repairs_and_retests(application,tmp_path):
    from app.backend.training.route_dataset import ROUTE_SYSTEM
    from app.backend.chat.code_verification import PLAN_INSTRUCTION
    broken='```python\ndef add(a,b): return a-b\n```'
    fixed='```python\ndef add(a,b): return a+b\n```'
    plan={'requirements':['add positive and negative numbers'],
          'sources':[{'block':0,'path':'calculator.py'}],
          'test_files':[{'path':'_checks/test_calc.py','content':
              "import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\nfrom calculator import add\nassert add(2,3)==5\nassert add(-2,1)==-1\nprint('CHECKS_OK')\n"}],
          'checks':[{'name':'addition assertions','kind':'test','argv':['python','_checks/test_calc.py'],
                     'requirements':[0],'stdout_contains':['CHECKS_OK']}],'limitations':[]}
    def reply(kwargs):
        first=kwargs['messages'][0].get('content','')
        if first==ROUTE_SYSTEM:return 'A'
        if first==PLAN_INSTRUCTION:
            kwargs['on_preview']({'kind':'output','tail_text':'private test plan','token_count':31,'character_count':17,'decode_tokens_per_second':1.5})
            current=application.database.fetch_one("SELECT result_json FROM operations WHERE state='running' ORDER BY created_at DESC LIMIT 1")
            progress=json.loads(current['result_json'])['lock_in_verification']
            assert progress['status']=='planning' and progress['token_count']==31
            assert 'private test plan' not in json.dumps(progress)
            return json.dumps(plan)
        if first.startswith('Review the supplied answer'):
            kwargs['on_preview']({'kind':'output','tail_text':'private critique','token_count':15,'character_count':16})
            return '{"needs_revision":false,"issues":[]}'
        if first.startswith('Revise the draft'):
            assert 'AssertionError' in kwargs['messages'][-1]['content']
            return fixed
        if kwargs.get('reasoning_mode')=='cooking':return '<think>Test the arithmetic.</think>'
        return broken
    _wire(application,tmp_path,reply);application.chat.model_bundle['runtime_family']='salty_native_steak35'
    _,messages=_send(application,'Write add(a,b) in Python.',settings={'reasoning_mode':'lock_in'})
    answer=messages[-1];report=answer['technical_details']['lock_in_verification']
    assert answer['content']==fixed
    assert report['status']=='passed' and report['repairs']==1
    assert report['attempts'][0]['checks'][0]['exit_code']!=0
    assert report['attempts'][1]['checks'][0]['exit_code']==0
    assert report['attempts'][0]['artifact_sha256']!=report['attempts'][1]['artifact_sha256']
    assert report['checks'][0]['audit_record_id']
    assert Path(report['work_directory'],'_evidence/result.json').is_file()


def test_cooking_activity_ends_reasoning_before_answer_and_preserves_prefill_time(application, tmp_path, monkeypatch):
    import time
    from app.backend.chat import service
    from app.backend.training.route_dataset import ROUTE_SYSTEM

    clock = time.monotonic
    offset = [0.0]
    monkeypatch.setattr(service, 'time', SimpleNamespace(
        monotonic=lambda: clock() + offset[0], perf_counter=time.perf_counter, sleep=time.sleep,
    ))

    def reply(kwargs):
        if kwargs['messages'][0].get('content') == ROUTE_SYSTEM:
            return 'A'
        preview = kwargs['on_preview']
        if kwargs.get('reasoning_mode') == 'cooking':
            offset[0] += 10
            preview({'kind': 'reasoning', 'tail_text': 'Consider inputs.', 'token_count': 2, 'character_count': 16})
            offset[0] += 20
            return '<think>Consider inputs.</think>'
        # The second pass has a long prefill; the reasoning stage has already ended.
        offset[0] += 40
        preview({'kind': 'output', 'tail_text': 'A complete answer.', 'token_count': 3, 'character_count': 18})
        offset[0] += 30
        return 'A complete answer.'

    _wire(application, tmp_path, reply)
    application.chat.model_bundle['runtime_family'] = 'salty_native_steak35'
    _, messages = _send(application, 'Explain it.', settings={'reasoning_mode': 'cooking'})
    entries = {item['id']: item for item in messages[-1]['technical_details']['activity_journal']}
    assert 9_000 <= entries['prefill']['updated_elapsed_ms'] < 15_000
    assert 29_000 <= entries['reasoning']['updated_elapsed_ms'] < 35_000
    assert entries['reasoning']['updated_elapsed_ms'] <= entries['cooking-answer']['started_elapsed_ms']
    assert entries['cooking-answer']['updated_elapsed_ms'] >= 99_000


@pytest.mark.parametrize('callback',['generate_with_preview','generate_structured','generate_structured_with_preview','generate_final_with_preview'])
def test_runner_answers_keep_selected_limit_and_report_truncation(application,tmp_path,monkeypatch,callback):
    from app.backend.chat.runners import LiveRunners
    long_answer='This answer continues across many sections. '*100
    seen=[]
    def reply(kwargs):
        if kwargs['messages'][0]['content']=='Answer producer probe':
            seen.append(kwargs['maximum_output_tokens'])
            assert kwargs['maximum_output_tokens']==16384
            if callback.startswith('generate_structured'):
                assert kwargs['maximum_output_mode'] == 'automatic'
                return json.dumps({'action':'respond','answer':long_answer})
            return long_answer
        return '{}'
    _wire(application,tmp_path,reply)
    application.chat.model_bundle['runtime_family']='salty_native_steak35'
    original=application.chat.model_bundle_runtime.generate
    def generate(**kwargs):
        result=original(**kwargs)
        if kwargs['messages'][0]['content']=='Answer producer probe':result.finish_reason='maximum_output'
        return result
    application.chat.model_bundle_runtime.generate=generate
    def research(self,**kwargs):
        args = ([{'role':'system','content':'Answer producer probe'}],)
        text=getattr(self,callback)(*args) if callback=='generate_structured' else getattr(self,callback)(*args,lambda _:None)
        if callback.startswith('generate_structured'):
            text=json.loads(text)['answer']
        return {'answer':text,'status':'completed','sources':[],'claims':[]}
    monkeypatch.setattr(LiveRunners,'run_research',research)
    _,messages=_send(application,'Write a complete report.',settings={'context_window_tokens':32768,
        'maximum_output_mode':'automatic','research_available':True,'research_command':True})
    details=messages[-1]['technical_details']
    assert seen==[16384]
    assert details['finish_reason']=='maximum_output'
    assert details['turn_completion']=='partial'
    assert details['answer_generation']['maximum_output_tokens']==16384


@pytest.mark.parametrize('limit',[64,32768])
def test_final_recovery_respects_manual_limit_and_never_hides_truncation(application,tmp_path,limit):
    seen=[]
    def reply(kwargs):
        if any('previous draft failed visible-output validation' in m.get('content','') for m in kwargs['messages']):
            seen.append(kwargs['maximum_output_tokens'])
            return 'A recovered answer that has not finished yet.'
        return '<think>The draft is still unfinished.'
    _wire(application,tmp_path,reply,image_ready=False)
    application.chat.model_bundle_runtime.profile.context_limit=131072
    original=application.chat.model_bundle_runtime.generate
    def generate(**kwargs):
        result=original(**kwargs);result.finish_reason='maximum_output';return result
    application.chat.model_bundle_runtime.generate=generate
    _,messages=_send(application,'Write a detailed report.',settings={'context_window_tokens':131072,
        'maximum_output_mode':'manual','maximum_output_tokens':limit})
    details=messages[-1]['technical_details']
    assert seen==[limit]
    assert details['finish_reason']=='maximum_output'
    assert details['turn_completion']=='partial'


@pytest.mark.parametrize('repair_finish',['maximum_output','end_of_generation'])
def test_routing_prose_repair_reports_its_own_completion(application,tmp_path,repair_finish):
    def reply(kwargs):
        if kwargs['messages'][0].get('content','').startswith('Your previous reply was JSON'):
            return 'The requested explanation is complete.'
        return '{"unexpected_protocol":"unfinished"}'
    _wire(application,tmp_path,reply,image_ready=False)
    original=application.chat.model_bundle_runtime.generate
    def generate(**kwargs):
        result=original(**kwargs)
        result.finish_reason=repair_finish if result.text.startswith('The requested') else 'maximum_output'
        return result
    application.chat.model_bundle_runtime.generate=generate
    _,messages=_send(application,'Explain the topic.')
    details=messages[-1]['technical_details']
    assert details['finish_reason']==repair_finish
    assert details['turn_completion']==('partial' if repair_finish=='maximum_output' else 'done')


def test_the_routing_instruction_is_absent_when_nothing_is_reachable(
    application, tmp_path: Path
) -> None:
    captured = _wire(application, tmp_path, "Plain answer.", image_ready=False)

    _send(application, "Say hello.")

    system_text = " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    # No image backend and no granted capability means nothing to offer, so the
    # turn carries no routing preamble at all.
    assert "generate_image" not in system_text


# ------------------------------------------------------- instant vs cooking


@pytest.mark.parametrize("mode", ["instant", "cooking"])
def test_both_modes_route_an_image_request_identically(
    application, tmp_path: Path, mode: str
) -> None:
    captured = _wire(application, tmp_path, IMAGE_REPLY)

    _, messages = _send(
        application, BOILABIN, settings={"reasoning_mode": mode}
    )

    assert captured["reasoning_mode"] == mode
    proposal = messages[1]["technical_details"]["host_action_proposal"]
    assert proposal["kind"] == "image.generate"
    assert "Boilabin" in proposal["arguments"]["prompt"]

    # The capability manifest is identical in both modes; only thinking differs.
    system_text = " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    assert "generate_image" in system_text


def test_the_text_model_writes_the_brief_before_the_image_model_draws_it(
    application, tmp_path: Path
) -> None:
    """The step the architecture always described, now actually taken.

    A routing decision is made in the same breath as the answer, so the brief
    inside it is whatever fitted there — "cow", "realistic". A diffusion model
    given three words draws three words. One bounded pass turns the request
    into something worth rendering, and the guard still runs after it.
    """

    captured = _wire(application, tmp_path, IMAGE_REPLY)
    _send(application, BOILABIN)

    calls = captured["calls"]
    # Found by what the call is, not by where it lands. A turn makes several
    # bounded passes — the route is checked against the request before it runs
    # — and pinning an index makes this test fail when another one is added
    # rather than when the brief stops being authored.
    authoring_call = next(
        (
            call
            for call in calls
            if any(
                message["role"] == "system"
                and "diffusion image model" in message["content"]
                for message in call
            )
        ),
        None,
    )
    assert authoring_call is not None, "the brief was never authored"
    authoring = " ".join(
        message["content"] for message in authoring_call if message["role"] == "system"
    )
    # The image model has no negation in its positive conditioning, so what
    # must not appear has to be collected somewhere it can subtract.
    assert "negative_constraints" in authoring
    # The request it is authoring from, not a fresh guess at one.
    user_text = " ".join(
        message["content"] for message in authoring_call if message["role"] == "user"
    )
    assert "Boilabin" in user_text


def test_generate_that_uses_the_assistant_description_as_its_referent(
    application, tmp_path: Path
) -> None:
    described = (
        "A rain-soaked neon street in Dhaka at blue hour, framed through a "
        "cinematic 35mm lens with rickshaw reflections on the pavement."
    )
    latest = "Generate an image of what you just described."

    def scripted(kwargs) -> str:
        messages = list(kwargs.get("messages") or [])
        system = " ".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        if "diffusion image model" in system:
            return json.dumps(
                {
                    "subject": "rain-soaked neon street in Dhaka with rickshaw reflections",
                    "image_type": "concept_art",
                    "style": "cinematic 35mm environment concept art",
                    "background": "blue-hour city haze",
                }
            )
        newest_user = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if latest in newest_user:
            return json.dumps(
                {
                    "action": "generate_image",
                    "reason": "The user explicitly requested the described scene.",
                    "brief": {
                        "subject": "what you just described",
                        "image_type": "concept_art",
                    },
                }
            )
        return described

    captured = _wire(application, tmp_path, scripted)
    conversation = application.create_conversation()
    first = application.chat.start_message(conversation["id"], "Imagine a cinematic street scene.")
    assert application.operations.wait(first["id"], timeout=20)["state"] == "completed"
    second = application.chat.start_message(conversation["id"], latest)
    assert application.operations.wait(second["id"], timeout=20)["state"] == "completed"

    messages = application.get_conversation(conversation["id"])["messages"]
    proposal = messages[-1]["technical_details"]["host_action_proposal"]
    assert proposal["kind"] == "image.generate"
    assert "rain-soaked neon street" in proposal["arguments"]["prompt"]
    assert "what you just described" not in proposal["arguments"]["prompt"]
    authoring_user_text = " ".join(
        str(message.get("content") or "")
        for call in captured["calls"]
        for message in call
        if message.get("role") == "user"
        and any(
            candidate.get("role") == "system"
            and "diffusion image model" in str(candidate.get("content") or "")
            for candidate in call
        )
    )
    assert described in authoring_user_text


@pytest.mark.parametrize("mode", ["instant", "cooking"])
def test_neither_mode_turns_an_explanation_into_an_image(
    application, tmp_path: Path, mode: str
) -> None:
    _wire(application, tmp_path, "LoRA adds low-rank adapters.")

    _, messages = _send(
        application, "Explain how LoRA works.", settings={"reasoning_mode": mode}
    )

    assert "host_action_proposal" not in messages[1]["technical_details"]
