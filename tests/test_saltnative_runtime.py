from __future__ import annotations

import ctypes
import json
import queue
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.runtime.salty_native import (
    _Batch,
    _ContextParams,
    _ModelParams,
    _ThreadpoolParams,
    _JsonCompletionDetector,
    _StopSequenceMatcher,
    _Utf8TokenDecoder,
    _apply_reasoning_to_rendered_prompt,
    _adaptive_identity_context_limit,
    _highest_logit_token,
    _prompt_batch_ranges,
    SaltyNativeAdapterSpec,
    SaltyNativeProfile,
    SaltyNativeRuntime,
)
from app.backend.runtime.salty_native_worker import (
    SaltyNativeWorkerRuntime,
    SaltyNativeRuntimeError,
    _GenerationPreviewAccumulator,
    _WorkerCancellationTimeout,
    _generation_preview_kind,
    _assign_process_to_job,
    _close_job,
    _create_kill_on_close_job,
    _resolve_worker_python,
)


def test_constrained_choice_selects_only_the_highest_allowed_logit() -> None:
    logits = (ctypes.c_float * 8)(0.0, 8.0, 2.0, 99.0, 3.0, 7.0, 0.0, 0.0)

    assert _highest_logit_token(logits, [1, 2, 5]) == 1
    with pytest.raises(ValueError, match="at least one token"):
        _highest_logit_token(logits, [])


def test_prompt_batch_ranges_never_exceed_native_batch_capacity() -> None:
    assert _prompt_batch_ranges(1, 256) == ((0, 1),)
    assert _prompt_batch_ranges(256, 256) == ((0, 256),)
    assert _prompt_batch_ranges(257, 256) == ((0, 256), (256, 257))
    assert _prompt_batch_ranges(1_025, 256) == (
        (0, 256),
        (256, 512),
        (512, 768),
        (768, 1_024),
        (1_024, 1_025),
    )
    assert all(
        0 < end - start <= 256
        for start, end in _prompt_batch_ranges(32_000, 256)
    )


@pytest.mark.parametrize("token_count,batch_size", [(0, 256), (-1, 256), (1, 0)])
def test_prompt_batch_ranges_reject_invalid_counts(token_count: int, batch_size: int) -> None:
    with pytest.raises(ValueError):
        _prompt_batch_ranges(token_count, batch_size)


class _SilentProtocolStdin:
    def write(self, _value: str) -> None:
        return None

    def flush(self) -> None:
        return None


class _ProtocolStdin:
    def __init__(self, responses: queue.Queue[dict]) -> None:
        self.responses = responses
        self.request: dict | None = None

    def write(self, value: str) -> None:
        self.request = json.loads(value)

    def flush(self) -> None:
        assert self.request is not None
        self.responses.put({"id": "stale-request", "ok": True, "result": {}})
        self.responses.put(
            {
                "id": None,
                "ok": False,
                "error": {"type": "invalid_worker_response"},
            }
        )
        self.responses.put(
            {
                "id": self.request["id"],
                "event": "generation_preview",
                "generation_preview": {
                    "kind": "output",
                    "tail_text": "partial answer",
                    "token_count": 3,
                },
            }
        )
        self.responses.put(
            {
                "id": self.request["id"],
                "ok": True,
                "result": {"correlated": True},
            }
        )


def test_private_worker_waits_for_its_correlated_response() -> None:
    runtime = object.__new__(SaltyNativeWorkerRuntime)
    runtime._lock = threading.RLock()
    runtime._responses = queue.Queue()
    runtime._process = SimpleNamespace(
        stdin=_ProtocolStdin(runtime._responses),
        poll=lambda: None,
        returncode=None,
    )
    runtime._start = lambda: None

    assert runtime._request("status", timeout=1) == {"correlated": True}


def test_private_worker_delivers_only_correlated_generation_preview_events() -> None:
    runtime = object.__new__(SaltyNativeWorkerRuntime)
    runtime._lock = threading.RLock()
    runtime._responses = queue.Queue()
    runtime._process = SimpleNamespace(
        stdin=_ProtocolStdin(runtime._responses),
        poll=lambda: None,
        returncode=None,
    )
    runtime._start = lambda: None
    previews: list[dict[str, object]] = []

    assert runtime._request("generate", timeout=1, on_preview=previews.append) == {
        "correlated": True
    }
    assert previews == [
        {"kind": "output", "tail_text": "partial answer", "token_count": 3}
    ]


@pytest.mark.parametrize('event',['generation_preview','generation_progress'])
@pytest.mark.parametrize('advancing',[True,False])
def test_generation_timeout_tracks_progress_not_total_duration(monkeypatch,event,advancing):
    import app.backend.runtime.salty_native_worker as module
    now=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    class Responses:
        def __init__(self):self.index=0
        def put(self,_):pass
        def get(self,timeout):
            self.index+=1;now[0]+=400
            if self.index==6:return {'id':stdin.request['id'],'ok':True,'result':{'completed':True}}
            return {'id':stdin.request['id'],'event':event,event:{
                'phase':'prefill','token_count':self.index if advancing else 1}}
    runtime=object.__new__(SaltyNativeWorkerRuntime)
    runtime._lock=threading.RLock();runtime._responses=Responses()
    stdin=_ProtocolStdin(runtime._responses)
    runtime._process=SimpleNamespace(stdin=stdin,poll=lambda:None)
    runtime._start=lambda:None
    terminated=[];runtime._terminate_process_locked=lambda:terminated.append(True)
    if advancing:
        assert runtime._request('generate',timeout=900)=={'completed':True}
        assert now[0]>900 and not terminated
    else:
        with pytest.raises(TimeoutError,match='no progress'):
            runtime._request('generate',timeout=900)
        assert terminated==[True]


def test_private_worker_restarts_after_unacknowledged_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.backend.runtime.salty_native_worker as worker_module

    runtime = object.__new__(SaltyNativeWorkerRuntime)
    runtime._lock = threading.RLock()
    runtime._responses = queue.Queue()
    runtime._process = SimpleNamespace(
        stdin=_SilentProtocolStdin(),
        poll=lambda: None,
        returncode=None,
    )
    runtime._start = lambda: None
    terminated: list[bool] = []
    runtime._terminate_process_locked = lambda: terminated.append(True)
    monkeypatch.setattr(worker_module, "_CANCELLATION_GRACE_SECONDS", 0.01)

    with pytest.raises(_WorkerCancellationTimeout):
        runtime._request(
            "generate",
            timeout=1,
            should_stop=lambda: True,
            cancellation_path=tmp_path / "cancel.signal",
        )

    assert terminated == [True]


def test_generation_returns_cancelled_after_forced_worker_restart(
    tmp_path: Path,
) -> None:
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "bin",
        source_sha256="a" * 64,
    )
    runtime.warmup = lambda: {}  # type: ignore[method-assign]

    def request(*_args, **_kwargs):
        raise _WorkerCancellationTimeout("forced restart")

    runtime._request = request  # type: ignore[method-assign]

    result = runtime.generate(
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=16,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
        should_stop=lambda: True,
    )

    assert result.cancelled is True
    assert result.finish_reason == "cancelled"
    assert result.text == ""
    assert result.token_ids == []
    assert result.technical_details["forced_worker_restart"] is True


def test_generation_preview_only_marks_an_unclosed_structured_think_block() -> None:
    assert _generation_preview_kind("ordinary prose about reasoning") == "output"
    assert _generation_preview_kind("<think>inspect the request") == "reasoning"
    assert _generation_preview_kind("<think>inspect</think>final answer") == "output"


def test_generation_preview_accumulator_separates_reasoning_and_answer() -> None:
    preview = _GenerationPreviewAccumulator(limit=24)

    assert preview.feed("<thi") == {
        "kind": "output",
        "tail_text": "<thi",
        "token_count": 1,
        "character_count": 4,
    }
    reasoning = preview.feed("nk>careful analysis")
    assert reasoning == {
        "kind": "reasoning",
        "tail_text": "careful analysis",
        "token_count": 2,
        "character_count": len("<think>careful analysis"),
    }
    answer = preview.feed("</think>final answer")
    assert answer == {
        "kind": "output",
        "tail_text": "final answer",
        "token_count": 3,
        "character_count": len("<think>careful analysis</think>final answer"),
    }


def test_generation_preview_accumulator_keeps_only_bounded_tail() -> None:
    preview = _GenerationPreviewAccumulator(limit=12)

    result = preview.feed("ordinary " + ("x" * 40))

    assert result["kind"] == "output"
    assert result["tail_text"] == "x" * 12
    assert result["token_count"] == 1
    assert result["character_count"] == len("ordinary " + ("x" * 40))


def test_json_completion_detector_handles_nested_values_and_escaped_quotes() -> None:
    detector = _JsonCompletionDetector()

    assert detector.feed('  {"message":"brace } and \\') is False
    assert detector.feed('"quoted\\"","items":[1,{"ok":true}]') is False
    assert detector.feed("}") is True
    assert detector.feed("   ") is True


def test_json_generation_stops_at_complete_root_before_whitespace_ceiling() -> None:
    decoded: list[list[int]] = []
    sampled = [10, 11, 12, 13, 14, 15, 16, 17]
    runtime, _trimmed, _cleared = _kv_reuse_runtime(decoded, sampled)
    pieces = {
        10: b'{',
        11: b'"a"',
        12: b':',
        13: b'{',
        14: b'"b"',
        15: b':1',
        16: b'}',
        17: b'}',
    }
    runtime._piece_bytes = lambda token: pieces[token]
    runtime._prompt_with_budget = lambda *_: ([1, 2, 3], 0, "contract")

    result = runtime.generate(
        messages=[{"role": "user", "content": "return JSON"}],
        maximum_output_tokens=32,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
        response_format="json",
    )

    assert result.text == '{"a":{"b":1}}'
    assert result.finish_reason == "json_complete"
    assert result.technical_details["generated_output_tokens"] == 8
    assert sampled == []


def test_private_worker_passes_reasoning_and_output_modes_to_native_runtime(
    tmp_path: Path,
) -> None:
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "bin",
        source_sha256="a" * 64,
    )
    captured: dict[str, object] = {}
    runtime.warmup = lambda: {}  # type: ignore[method-assign]

    def request(command, payload, **_kwargs):
        captured["command"] = command
        captured["payload"] = dict(payload)
        return {
            "text": "ok",
            "token_ids": [1],
            "omitted_turns": 0,
            "cancelled": False,
            "finish_reason": "end_of_generation",
            "technical_details": {},
        }

    runtime._request = request  # type: ignore[method-assign]

    runtime.generate(
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=8192,
        temperature=0.8,
        top_p=0.95,
        top_k=40,
        repetition_penalty=1.1,
        seed=-1,
        context_window_tokens=32768,
        reasoning_mode="instant",
        maximum_output_mode="automatic",
        response_format="json",
    )

    assert captured["command"] == "generate"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["reasoning_mode"] == "instant"
    assert payload["maximum_output_mode"] == "automatic"
    assert payload["response_format"] == "json"


def test_private_worker_warmup_primes_one_bounded_instant_decode_pass(
    tmp_path: Path,
) -> None:
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "bin",
        source_sha256="a" * 64,
    )
    runtime.load = lambda: {}  # type: ignore[method-assign]
    payloads: list[dict[str, object]] = []

    def request(command, payload, **_kwargs):
        assert command == "generate"
        payloads.append(dict(payload))
        return {"token_ids": [1, 2, 3], "cancelled": False}

    runtime._request = request  # type: ignore[method-assign]

    status = runtime.warmup()

    assert len(payloads) == 1
    assert all(payload["maximum_output_tokens"] == 4 for payload in payloads)
    assert all(payload["reasoning_mode"] == "instant" for payload in payloads)
    assert all(payload["context_window_tokens"] == 512 for payload in payloads)
    assert status["warmup_passes"] == 1
    assert status["warmup_generated_tokens"] == 3


def test_private_worker_warmup_keeps_identity_specialists_lazy(
    tmp_path: Path,
) -> None:
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "runtime" / "bin",
        source_sha256="a" * 64,
        profile=SaltyNativeProfile(
            context_limit=262_144,
            resident_context_limit=32_768,
        ),
        adapters=[
            {
                "adapter_id": "base-steak-2-0-identity-introduction-repair-v1",
                "path": str(tmp_path / "intro.gguf"),
                "sha256": "b" * 64,
                "activation": "identity_intent",
            },
            {
                "adapter_id": "base-steak-2-0-identity-full-v1",
                "path": str(tmp_path / "full.gguf"),
                "sha256": "c" * 64,
                "activation": "identity_intent",
            },
        ],
    )
    runtime.load = lambda: {}  # type: ignore[method-assign]
    payloads: list[tuple[str, dict[str, object]]] = []

    def request(command, payload, **_kwargs):
        payloads.append((command, dict(payload)))
        return {"token_ids": [1], "cancelled": False}

    runtime._request = request  # type: ignore[method-assign]

    runtime.warmup()

    identity_payloads = [
        payload for command, payload in payloads if command == "generate_identity"
    ]
    assert identity_payloads == []
    assert runtime._warmup_passes == 1


def test_native_abi_matches_audited_header_layout() -> None:
    assert ctypes.sizeof(_ModelParams) == 72
    assert ctypes.sizeof(_ContextParams) == 160
    assert ctypes.sizeof(_Batch) == 56
    assert ctypes.sizeof(_ThreadpoolParams) == 528
    assert _ModelParams.n_gpu_layers.offset == 16
    assert _ContextParams.flash_attn_type.offset == 48
    assert _ContextParams.type_k.offset == 104
    assert _ContextParams.samplers.offset == 136
    assert _ThreadpoolParams.poll.offset == 520


def test_incremental_utf8_decoder_preserves_characters_split_between_tokens() -> None:
    decoder = _Utf8TokenDecoder()
    encoded = "🥔".encode("utf-8")

    decoded = "".join(decoder.feed(bytes([value])) for value in encoded)
    decoded += decoder.finish()

    assert decoded == "🥔"
    assert "\ufffd" not in decoded


def test_incremental_utf8_decoder_omits_an_incomplete_final_character() -> None:
    decoder = _Utf8TokenDecoder()

    decoded = decoder.feed("🥔".encode("utf-8")[:2]) + decoder.finish()

    assert decoded == ""
    assert "\ufffd" not in decoded


def test_stop_sequence_matcher_finds_cross_piece_boundary_without_full_rebuild() -> None:
    matcher = _StopSequenceMatcher(["END", "STOP"])

    assert matcher.feed("answer ST") is None
    assert matcher.feed("OP ignored") == len("answer ")


def test_stop_sequence_matcher_preserves_declared_sequence_priority() -> None:
    matcher = _StopSequenceMatcher(["later", "early"])

    assert matcher.feed("early then later") == len("early then ")
    assert _StopSequenceMatcher([""]).feed("anything") == 0


def test_daily_native_profile_keeps_32k_resident_and_262k_selectable() -> None:
    profile = SaltyNativeProfile()
    assert profile.profile_id == "base_steak_2_32k_adaptive_262k"
    assert profile.gpu_layers == 99
    assert profile.context_limit == 262_144
    assert profile.resident_context_limit is None
    assert profile.initial_context_limit == 32_768
    assert profile.batch_size == 256
    assert profile.micro_batch_size == 128
    assert profile.kv_precision == "q8_0"
    assert profile.threads == 12
    assert profile.thread_poll == 100
    assert profile.cuda_output_projection is False
    assert profile.host_kv_above_context is None
    assert profile.host_kv_threshold == 65_536


def test_architectural_maximum_places_kv_on_host_without_changing_daily_policy(
    tmp_path: Path,
) -> None:
    seen: list[tuple[int, bool]] = []

    class FakeLlama:
        @staticmethod
        def llama_context_default_params() -> _ContextParams:
            return _ContextParams()

        @staticmethod
        def llama_init_from_model(model: int, params: _ContextParams) -> int:
            assert model == 17
            seen.append((int(params.n_ctx), bool(params.offload_kqv)))
            return len(seen)

    runtime = SaltyNativeRuntime(
        model_path=tmp_path / "Base-Steak-2.0.gguf",
        library_directory=tmp_path,
        source_sha256="a" * 64,
    )
    api = SimpleNamespace(native=FakeLlama())

    assert runtime._create_context(api, 17, 32_768) == 1
    assert runtime._create_context(api, 17, 262_144) == 2
    assert seen == [(32_768, True), (262_144, False)]


def test_equal_host_threshold_places_daily_32k_kv_on_host(tmp_path: Path) -> None:
    seen: list[tuple[int, bool]] = []

    class FakeLlama:
        @staticmethod
        def llama_context_default_params() -> _ContextParams:
            return _ContextParams()

        @staticmethod
        def llama_init_from_model(model: int, params: _ContextParams) -> int:
            seen.append((int(params.n_ctx), bool(params.offload_kqv)))
            return 1

    runtime = SaltyNativeRuntime(
        model_path=tmp_path / "Base-Steak-2.0.gguf",
        library_directory=tmp_path,
        source_sha256="a" * 64,
        profile=SaltyNativeProfile(
            resident_context_limit=32_768,
            host_kv_above_context=32_768,
        ),
    )

    assert runtime._create_context(SimpleNamespace(native=FakeLlama()), 17, 32_768) == 1
    assert seen == [(32_768, False)]


def test_native_adapter_specs_are_hash_bound_and_applied_with_their_scale(
    tmp_path: Path,
) -> None:
    spec = SaltyNativeAdapterSpec(
        adapter_id="identity-v1",
        path=str(tmp_path / "identity.gguf"),
        sha256="b" * 64,
        scale=0.75,
    )
    applied: list[tuple[int, list[int], list[float]]] = []

    class FakeLlama:
        @staticmethod
        def llama_set_adapters_lora(context, handles, count, scales):
            applied.append(
                (
                    context,
                    [int(handles[index]) for index in range(count)],
                    [float(scales[index]) for index in range(count)],
                )
            )
            return 0

    runtime = SaltyNativeRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path,
        source_sha256="a" * 64,
        adapters=[spec],
    )
    runtime._adapter_handles = [41]

    runtime._apply_adapters(SimpleNamespace(native=FakeLlama()), 17)

    assert applied == [(17, [41], pytest.approx([0.75]))]
    with pytest.raises(ValueError, match="sha256"):
        SaltyNativeAdapterSpec(
            adapter_id="bad",
            path="bad.gguf",
            sha256="not-a-hash",
        )


def test_conditional_native_adapter_is_disabled_by_default_and_explicitly_enabled(
    tmp_path: Path,
) -> None:
    spec = SaltyNativeAdapterSpec(
        adapter_id="identity-v1",
        path=str(tmp_path / "identity.gguf"),
        sha256="b" * 64,
        scale=0.5,
        activation="identity_intent",
    )
    applied: list[tuple[int, list[int], list[float]]] = []

    class FakeLlama:
        @staticmethod
        def llama_set_adapters_lora(context, handles, count, scales):
            applied.append(
                (
                    context,
                    [int(handles[index]) for index in range(count)] if handles else [],
                    [float(scales[index]) for index in range(count)] if scales else [],
                )
            )
            return 0

    runtime = SaltyNativeRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path,
        source_sha256="a" * 64,
        adapters=[spec],
    )
    runtime._adapter_handles = [41]

    assert runtime._apply_adapters(SimpleNamespace(native=FakeLlama()), 17) == ()
    assert runtime._apply_adapters(
        SimpleNamespace(native=FakeLlama()), 17, ["identity-v1"]
    ) == ("identity-v1",)
    assert applied == [
        (17, [], []),
        (17, [41], pytest.approx([0.5])),
    ]


def test_private_worker_exposes_conditional_adapter_ids(tmp_path: Path) -> None:
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "bin",
        source_sha256="a" * 64,
        adapters=[
            {
                "adapter_id": "identity-v1",
                "path": str(tmp_path / "identity.gguf"),
                "sha256": "b" * 64,
                "scale": 0.5,
                "activation": "identity_intent",
            },
            {
                "adapter_id": "always-v1",
                "path": str(tmp_path / "always.gguf"),
                "sha256": "c" * 64,
                "scale": 1.0,
            },
        ],
    )

    assert runtime.conditional_adapter_ids("identity_intent") == ("identity-v1",)
    assert runtime.conditional_adapter_ids("missing") == ()


def test_generation_preview_can_begin_inside_the_template_opened_reasoning_block() -> None:
    preview = _GenerationPreviewAccumulator(reasoning_open=True)

    reasoning = preview.feed("careful analysis")
    assert reasoning["kind"] == "reasoning"
    assert reasoning["tail_text"] == "careful analysis"
    assert reasoning["token_count"] == 1

    answer = preview.feed("</think>final answer")
    assert answer["kind"] == "output"
    assert answer["tail_text"] == "final answer"
    assert answer["token_count"] == 2


def test_private_worker_sends_registered_adapters_in_the_load_contract(
    tmp_path: Path,
) -> None:
    adapter = {
        "adapter_id": "identity-v1",
        "path": str(tmp_path / "identity.gguf"),
        "sha256": "b" * 64,
        "scale": 1.0,
    }
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "bin",
        source_sha256="a" * 64,
        adapters=[adapter],
    )
    captured: dict[str, object] = {}

    def request(command, payload, **_kwargs):
        captured["command"] = command
        captured["payload"] = payload
        return {"loaded": True}

    runtime._request = request  # type: ignore[method-assign]

    runtime.load()

    assert captured["command"] == "load"
    assert captured["payload"]["adapters"] == [adapter]


def test_private_worker_uses_dedicated_identity_generation_command(
    tmp_path: Path,
) -> None:
    runtime = SaltyNativeWorkerRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "runtime" / "bin",
        source_sha256="a" * 64,
    )
    captured: dict[str, object] = {}
    runtime.warmup = lambda: {}  # type: ignore[method-assign]

    def request(command, payload, **_kwargs):
        captured["command"] = command
        captured["payload"] = payload
        return {
            "text": "I am Base Steak 2.0.",
            "token_ids": [1, 2],
            "omitted_turns": 0,
            "cancelled": False,
            "finish_reason": "end_of_generation",
            "technical_details": {"identity_context_used": True},
        }

    runtime._request = request  # type: ignore[method-assign]
    result = runtime.generate_identity(
        messages=[{"role": "user", "content": "What is your name?"}],
        maximum_output_tokens=32,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.1,
        seed=20260819,
        enabled_adapter_ids=["base-steak-2-0-identity-introduction-v1"],
    )

    assert captured["command"] == "generate_identity"
    assert captured["payload"]["enabled_adapter_ids"] == [
        "base-steak-2-0-identity-introduction-v1"
    ]
    assert result.technical_details["identity_context_used"] is True


def test_identity_context_expands_to_prompt_need_within_selected_ceiling_without_replacing_main_state(
    tmp_path: Path,
) -> None:
    adapter_id = "base-steak-2-0-identity-model_name-v1"
    runtime = SaltyNativeRuntime(
        model_path=tmp_path / "model.gguf",
        library_directory=tmp_path / "runtime" / "bin",
        profile=SaltyNativeProfile(
            context_limit=262_144,
            resident_context_limit=32_768,
        ),
        source_sha256="a" * 64,
        adapters=[
            SaltyNativeAdapterSpec(
                adapter_id=adapter_id,
                path=str(tmp_path / "identity.gguf"),
                sha256="b" * 64,
                activation="identity_intent",
            )
        ],
    )
    runtime._api = object()  # type: ignore[assignment]
    runtime._model = 1
    runtime._context = 11
    runtime._vocab = 2
    runtime._allocated_context_limit = 32_768
    runtime._identity_context = 22
    runtime._identity_context_limit = 32_768
    runtime._identity_active_adapter_ids = ()
    runtime._identity_resident_tokens = []
    runtime._identity_decode_token_buffer = None
    runtime._identity_decode_token_batch = None
    main_resident = [1, 2, 3]
    runtime._resident_tokens = main_resident
    prompt_token_count = 50_000
    runtime._format_chat = lambda *_: (b"rendered", "contract")  # type: ignore[method-assign]
    runtime._tokenize = lambda _prompt: list(range(prompt_token_count))  # type: ignore[method-assign]
    calls: list[dict[str, object]] = []

    def generate(**kwargs):
        selected = int(kwargs["context_window_tokens"])
        calls.append(
            {
                "selected": selected,
                "profile_limit": runtime.profile.context_limit,
                "resident_limit": runtime.profile.initial_context_limit,
                "context": runtime._context,
            }
        )
        runtime._allocated_context_limit = selected
        return SimpleNamespace(
            technical_details={
                "effective_context_limit": selected,
                "allocated_context_limit": selected,
            }
        )

    runtime.generate = generate  # type: ignore[method-assign]

    maximum = runtime.generate_identity(
        messages=[{"role": "user", "content": "What is your name?"}],
        maximum_output_tokens=128,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
        context_window_tokens=262_144,
        enabled_adapter_ids=[adapter_id],
    )

    assert calls[-1] == {
        "selected": 53_248,
        "profile_limit": 262_144,
        "resident_limit": 32_768,
        "context": 22,
    }
    assert maximum.technical_details["identity_context_mode"] == (
        "prompt_sized_selected_ceiling"
    )
    assert maximum.technical_details["identity_context_selected_ceiling"] == 262_144
    assert maximum.technical_details["identity_context_effective_limit"] == 53_248
    assert maximum.technical_details["identity_context_limit"] == 53_248
    assert maximum.technical_details["identity_selected_context_honored"] is True
    assert runtime._identity_context_limit == 53_248
    assert runtime.profile.context_limit == 262_144
    assert runtime._context == 11
    assert runtime._allocated_context_limit == 32_768
    assert runtime._resident_tokens is main_resident

    prompt_token_count = 100
    default = runtime.generate_identity(
        messages=[{"role": "user", "content": "Who trained you?"}],
        maximum_output_tokens=128,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
        context_window_tokens=32_768,
        enabled_adapter_ids=[adapter_id],
    )

    assert calls[-1]["selected"] == 32_768
    assert default.technical_details["identity_context_limit"] == 32_768
    assert runtime._identity_context_limit == 32_768


def test_identity_context_allocator_covers_every_selected_ceiling_without_overallocating() -> None:
    assert _adaptive_identity_context_limit(
        selected_ceiling=262_144,
        resident_limit=32_768,
        prompt_tokens=100,
        reserved_output_tokens=128,
    ) == 32_768
    assert _adaptive_identity_context_limit(
        selected_ceiling=262_144,
        resident_limit=32_768,
        prompt_tokens=50_000,
        reserved_output_tokens=128,
    ) == 53_248
    assert _adaptive_identity_context_limit(
        selected_ceiling=262_144,
        resident_limit=32_768,
        prompt_tokens=300_000,
        reserved_output_tokens=128,
    ) == 262_144
    assert _adaptive_identity_context_limit(
        selected_ceiling=16_384,
        resident_limit=32_768,
        prompt_tokens=100,
        reserved_output_tokens=128,
    ) == 16_384


def test_manifest_native_profile_selects_base_steak_2_runtime_policy() -> None:
    profile = SaltyNativeProfile.from_manifest(
        {
            "profile_id": "base_steak_2_0_64k",
            "gpu_layers": 99,
            "context_limit": 65536,
            "resident_context_limit": 32768,
            "batch_size": 256,
            "micro_batch_size": 128,
            "threads": 12,
            "thread_poll": 100,
            "kv_precision": "q8_0",
            "cuda_output_projection": False,
        }
    )

    assert profile.profile_id == "base_steak_2_0_64k"
    assert profile.gpu_layers == 99
    assert profile.context_limit == 65536
    assert profile.resident_context_limit == 32768
    assert profile.initial_context_limit == 32768
    assert profile.batch_size == 256
    assert profile.micro_batch_size == 128
    assert profile.kv_precision == "q8_0"


def test_resident_context_must_fit_the_selectable_profile() -> None:
    with pytest.raises(ValueError, match="resident_context_limit"):
        SaltyNativeProfile(context_limit=32768, resident_context_limit=65536)


def test_native_context_grows_and_returns_to_resident_capacity_without_reloading_weights() -> None:
    created_limits: list[int] = []
    detached: list[int] = []
    freed: list[int] = []
    attached: list[tuple[int, int]] = []

    class FakeLlama:
        @staticmethod
        def llama_context_default_params() -> _ContextParams:
            return _ContextParams()

        @staticmethod
        def llama_init_from_model(model: int, params: _ContextParams) -> int:
            assert model == 202
            created_limits.append(int(params.n_ctx))
            return 400 + len(created_limits)

        @staticmethod
        def llama_detach_threadpool(context: int) -> None:
            detached.append(context)

        @staticmethod
        def llama_free(context: int) -> None:
            freed.append(context)

        @staticmethod
        def llama_attach_threadpool(context: int, threadpool: int, _: object) -> None:
            attached.append((context, threadpool))

    runtime = object.__new__(SaltyNativeRuntime)
    runtime.profile = SaltyNativeProfile(
        context_limit=65536,
        resident_context_limit=32768,
        batch_size=256,
        micro_batch_size=128,
    )
    runtime._api = SimpleNamespace(native=FakeLlama())
    runtime._model = 202
    runtime._context = 101
    runtime._threadpool = 303
    runtime._allocated_context_limit = 32768
    runtime._decode_token_buffer = object()
    runtime._decode_token_batch = object()

    assert runtime._ensure_context_allocation(65536) is True
    assert runtime._allocated_context_limit == 65536
    assert runtime._context == 401
    assert runtime._ensure_context_allocation(32768) is True
    assert runtime._allocated_context_limit == 32768
    assert runtime._context == 402
    assert runtime._ensure_context_allocation(16384) is False
    assert created_limits == [65536, 32768]
    assert detached == [101, 401]
    assert freed == [101, 401]
    assert attached == [(401, 303), (402, 303)]


def _kv_reuse_runtime(decoded: list[list[int]], sampled: list[int]):
    """Build a runtime whose native calls are recorded instead of executed."""

    trimmed: list[tuple[int, int, int]] = []
    cleared: list[bool] = []

    class FakeLlama:
        @staticmethod
        def llama_get_memory(context: int) -> int:
            return 777

        @staticmethod
        def llama_memory_seq_rm(memory: int, seq: int, start: int, end: int) -> bool:
            trimmed.append((seq, start, end))
            return True

        @staticmethod
        def llama_memory_clear(memory: int, data: bool) -> None:
            cleared.append(bool(data))

        @staticmethod
        def llama_batch_get_one(tokens, count: int):
            # Keep the live buffer rather than a snapshot: the decode loop
            # reuses one allocation and mutates it in place between tokens.
            return (tokens, count)

        @staticmethod
        def llama_decode(context: int, batch) -> int:
            tokens, count = batch
            decoded.append([int(tokens[index]) for index in range(count)])
            return 0

        @staticmethod
        def llama_sampler_sample(sampler: int, context: int, index: int) -> int:
            return sampled.pop(0)

        @staticmethod
        def llama_vocab_is_eog(vocab: int, token: int) -> bool:
            return token == 0

        @staticmethod
        def llama_sampler_free(sampler: int) -> None:
            return None

    runtime = object.__new__(SaltyNativeRuntime)
    runtime.profile = SaltyNativeProfile(context_limit=4096, batch_size=256)
    runtime._lock = threading.RLock()
    runtime._api = SimpleNamespace(native=FakeLlama())
    runtime._model = 1
    runtime._context = 2
    runtime._vocab = 3
    runtime._threadpool = 4
    runtime._allocated_context_limit = 4096
    runtime._decode_token_buffer = None
    runtime._decode_token_batch = None
    runtime._resident_tokens = []
    runtime._runtime_id = "test"
    runtime._load_seconds = 0.0
    runtime._verified_source_sha256 = None
    runtime.source_sha256 = "0" * 64
    runtime.model_path = Path("model.gguf")
    runtime._sampler = lambda **_: 55
    runtime._piece_bytes = lambda token: b"x"
    runtime._ensure_context_allocation = lambda limit: False
    return runtime, trimmed, cleared


def test_automatic_output_ceiling_reserves_a_context_proportional_prompt_budget() -> None:
    decoded: list[list[int]] = []
    runtime, _trimmed, _cleared = _kv_reuse_runtime(decoded, [90, 0])
    captured: dict[str, int] = {}

    def prompt_with_budget(_messages, reserved, limit, _reasoning_mode):
        captured.update(reserved=reserved, limit=limit)
        return [1, 2, 3], 0, "contract"

    runtime._prompt_with_budget = prompt_with_budget
    result = runtime.generate(
        messages=[{"role": "user", "content": "what is your name"}],
        maximum_output_tokens=8192,
        reserved_output_tokens=8192,
        maximum_output_mode="automatic",
        context_window_tokens=4096,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )

    assert captured == {"reserved": 512, "limit": 4096}
    assert result.technical_details["requested_reserved_output_tokens"] == 8192
    assert result.technical_details["reserved_output_tokens"] == 512
    assert result.technical_details["maximum_output_token_ceiling"] == 4093


def test_manual_output_limit_keeps_the_full_requested_reservation() -> None:
    decoded: list[list[int]] = []
    runtime, _trimmed, _cleared = _kv_reuse_runtime(decoded, [90, 0])
    captured: dict[str, int] = {}

    def prompt_with_budget(_messages, reserved, limit, _reasoning_mode):
        captured.update(reserved=reserved, limit=limit)
        return [1, 2, 3], 0, "contract"

    runtime._prompt_with_budget = prompt_with_budget
    result = runtime.generate(
        messages=[{"role": "user", "content": "hello"}],
        maximum_output_tokens=2048,
        reserved_output_tokens=2048,
        maximum_output_mode="manual",
        context_window_tokens=4096,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )

    assert captured == {"reserved": 2048, "limit": 4096}
    assert result.technical_details["requested_reserved_output_tokens"] == 2048
    assert result.technical_details["reserved_output_tokens"] == 2048


def test_native_json_response_format_reaches_sampler_and_provenance() -> None:
    decoded: list[list[int]] = []
    runtime, _trimmed, _cleared = _kv_reuse_runtime(decoded, [90, 0])
    captured: dict[str, object] = {}

    def sampler(**kwargs):
        captured.update(kwargs)
        return 55

    runtime._sampler = sampler
    runtime._prompt_with_budget = lambda *_: ([1, 2, 3], 0, "contract")

    result = runtime.generate(
        messages=[{"role": "user", "content": "return one JSON object"}],
        maximum_output_tokens=32,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
        response_format="json",
    )

    assert captured["response_format"] == "json"
    assert result.technical_details["response_format_effective"] == "json"


def test_native_generation_rejects_unknown_response_format() -> None:
    decoded: list[list[int]] = []
    runtime, _trimmed, _cleared = _kv_reuse_runtime(decoded, [90, 0])

    with pytest.raises(ValueError, match="response_format must be text or json"):
        runtime.generate(
            messages=[{"role": "user", "content": "hello"}],
            maximum_output_tokens=8,
            temperature=0.0,
            top_p=1.0,
            top_k=1,
            repetition_penalty=1.0,
            seed=0,
            response_format="xml",
        )


def test_native_generation_reuses_the_resident_prefix_across_turns() -> None:
    decoded: list[list[int]] = []
    sampled = [90, 91, 0, 92, 0]
    runtime, trimmed, cleared = _kv_reuse_runtime(decoded, sampled)

    runtime._prompt_with_budget = lambda *_: ([1, 2, 3, 4, 5], 0, "contract")
    runtime.generate(
        messages=[{"role": "user", "content": "first"}],
        maximum_output_tokens=8,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )
    # A cold cache has no reusable prefix, so the whole prompt is evaluated.
    assert decoded[0] == [1, 2, 3, 4, 5]
    assert cleared == [True]
    assert runtime._resident_tokens == [1, 2, 3, 4, 5, 90, 91]

    decoded.clear()
    # The follow-up turn repeats the first exchange and appends two new tokens.
    runtime._prompt_with_budget = lambda *_: (
        [1, 2, 3, 4, 5, 90, 91, 6, 7],
        0,
        "contract",
    )
    runtime.generate(
        messages=[{"role": "user", "content": "second"}],
        maximum_output_tokens=8,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )
    # Only the appended tail is prefilled. No empty-range trim is attempted on
    # the hybrid cache, because every resident token is already a valid prefix.
    assert trimmed == []
    assert cleared == [True]
    assert decoded[0] == [6, 7]


def test_live_speed_counts_native_tokens_even_when_utf8_pieces_are_empty():
    runtime,_,_=_kv_reuse_runtime([], [90,91,92,0])
    runtime._prompt_with_budget=lambda *_:([1,2,3],0,'contract')
    pieces={90:b'\xe2',91:b'\x82',92:b'\xac'}
    runtime._piece_bytes=lambda token:pieces[token]
    progress=[];text=[]
    result=runtime.generate(messages=[{'role':'user','content':'test'}],maximum_output_tokens=4,
        temperature=0,top_p=1,top_k=1,repetition_penalty=1,seed=0,
        on_progress=progress.append,on_text=text.append)
    decoded=[p for p in progress if p['phase']=='decode']
    assert [p['token_count'] for p in decoded]==[1,2,3]
    assert text==['€']
    assert len(result.token_ids)==3
    assert decoded[-1]['decode_tokens_per_second'] is not None


@pytest.mark.parametrize('prompt_length,output_limit', [(30,64),(5000,2048),(20000,4096)])
def test_adaptive_context_keeps_all_prompt_and_output_capacity(prompt_length,output_limit):
    decoded = []
    runtime, _, _ = _kv_reuse_runtime(decoded, [90,0])
    runtime.profile = replace(runtime.profile, context_limit=32768,
        resident_context_limit=4096, adaptive_context_allocation=True)
    allocations = []
    runtime._ensure_context_allocation = lambda limit: allocations.append(limit) or False
    runtime._prompt_with_budget = lambda *_: ([1]*prompt_length,0,'contract')
    result = runtime.generate(messages=[{'role':'user','content':'test'}],
        maximum_output_tokens=output_limit,context_window_tokens=32768,
        temperature=0,top_p=1,top_k=1,repetition_penalty=1,seed=42)
    assert prompt_length+output_limit <= allocations[0] <= 32768
    assert allocations[0] < 32768
    assert result.omitted_turns == 0
    assert result.technical_details['maximum_output_token_ceiling'] == output_limit
    assert result.technical_details['input_context_tokens'] == prompt_length


def test_native_generation_rebuilds_when_the_conversation_diverges() -> None:
    decoded: list[list[int]] = []
    runtime, trimmed, cleared = _kv_reuse_runtime(decoded, [90, 0, 91, 0])

    runtime._prompt_with_budget = lambda *_: ([1, 2, 3], 0, "contract")
    runtime.generate(
        messages=[{"role": "user", "content": "first"}],
        maximum_output_tokens=4,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )
    decoded.clear()

    # An unrelated conversation shares no prefix, so the cache is discarded.
    runtime._prompt_with_budget = lambda *_: ([8, 9], 0, "contract")
    runtime.generate(
        messages=[{"role": "user", "content": "unrelated"}],
        maximum_output_tokens=4,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )
    assert trimmed == []
    assert cleared == [True, True]
    assert decoded[0] == [8, 9]


def test_native_generation_always_evaluates_at_least_one_token() -> None:
    decoded: list[list[int]] = []
    runtime, trimmed, _cleared = _kv_reuse_runtime(decoded, [90, 0, 91, 0])
    prompt = [4, 5, 6]

    runtime._prompt_with_budget = lambda *_: (list(prompt), 0, "contract")
    runtime.generate(
        messages=[{"role": "user", "content": "same"}],
        maximum_output_tokens=4,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )
    decoded.clear()

    # Resending an identical prompt must still evaluate its final token,
    # because sampling reads the logits the last decode produced.
    runtime.generate(
        messages=[{"role": "user", "content": "same"}],
        maximum_output_tokens=4,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=0,
    )
    assert trimmed == [(0, 2, -1)]
    assert decoded[0] == [6]


def test_failed_decode_forgets_the_resident_prefix() -> None:
    decoded: list[list[int]] = []
    runtime, _trimmed, _cleared = _kv_reuse_runtime(decoded, [90, 0])
    runtime._prompt_with_budget = lambda *_: ([1, 2, 3], 0, "contract")
    runtime._api.native.llama_decode = staticmethod(lambda context, batch: 1)

    with pytest.raises(SaltyNativeRuntimeError):
        runtime.generate(
            messages=[{"role": "user", "content": "boom"}],
            maximum_output_tokens=4,
            temperature=0.0,
            top_p=1.0,
            top_k=1,
            repetition_penalty=1.0,
            seed=0,
        )
    # A partially written cache must never be advertised as reusable.
    assert runtime._resident_tokens == []


def test_missing_manifest_profile_preserves_27b_fallback() -> None:
    assert SaltyNativeProfile.from_manifest({}) == SaltyNativeProfile()


def test_instant_closes_the_embedded_empty_think_block_after_rendering() -> None:
    rendered = b"<|im_start|>assistant\n<think>\n"

    instant, instant_contract = _apply_reasoning_to_rendered_prompt(
        rendered,
        "instant",
    )
    cooking, cooking_contract = _apply_reasoning_to_rendered_prompt(
        rendered,
        "cooking",
    )

    assert instant == b"<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert instant_contract == "embedded_template_empty_think_closed"
    assert cooking == rendered
    assert cooking_contract == "embedded_template_open_think"


def test_instant_closes_an_empty_block_after_native_assistant_header() -> None:
    prompt, contract = _apply_reasoning_to_rendered_prompt(
        b"<|im_start|>user\nhello<|im_end|>\n<|im_start|>assistant\n",
        "instant",
    )

    assert prompt.endswith(b"<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert contract == "embedded_template_empty_think_closed"


def test_legacy_reasoning_aliases_map_to_the_two_rendered_contracts() -> None:
    rendered = b"<think>\n"

    assert _apply_reasoning_to_rendered_prompt(rendered, "off")[0].endswith(
        b"<think>\n\n</think>\n\n"
    )
    assert _apply_reasoning_to_rendered_prompt(rendered, "auto")[0] == rendered
    assert _apply_reasoning_to_rendered_prompt(rendered, "deep")[0] == rendered


def test_private_runtime_excludes_server_rpc_and_cli_executables() -> None:
    root = (
        Path(__file__).parents[1]
        / "workspace/runtime/salty-native-steak20"
    )
    names = {path.name.casefold() for path in root.rglob("*") if path.is_file()}
    assert "third_party_license.txt" in names
    assert not {name for name in names if name.endswith(".exe")}
    assert "ggml-rpc.dll" not in names
    assert "llama-server-impl.dll" not in names


def test_private_worker_selects_packaged_python_instead_of_desktop_host(
    tmp_path: Path,
) -> None:
    packaged_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    packaged_python.parent.mkdir(parents=True)
    packaged_python.write_bytes(b"python")
    selected = _resolve_worker_python(
        tmp_path,
        current_executable=tmp_path / "Salty Steak.exe",
    )
    assert selected == packaged_python.resolve()


def test_private_worker_prefers_owned_interpreter_over_venv_launcher(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter = tmp_path / ".python" / "python.exe"
    launcher.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher")
    interpreter.write_bytes(b"interpreter")
    selected = _resolve_worker_python(
        tmp_path,
        current_executable=tmp_path / "Salty Steak.exe",
    )
    assert selected == interpreter.resolve()


def test_private_worker_rejects_desktop_host_as_python(tmp_path: Path) -> None:
    try:
        _resolve_worker_python(
            tmp_path,
            current_executable=tmp_path / "Salty Steak.exe",
        )
    except SaltyNativeRuntimeError as error:
        assert "packaged Python interpreter" in str(error)
    else:
        raise AssertionError("The desktop host was accepted as a worker interpreter")


def test_windows_worker_job_can_be_created_and_closed() -> None:
    job = _create_kill_on_close_job()
    assert job is not None
    _close_job(job)


def test_missing_worker_job_is_a_cross_platform_noop() -> None:
    _assign_process_to_job(None, None)  # type: ignore[arg-type]
    _close_job(None)
