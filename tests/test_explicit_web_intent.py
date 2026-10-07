import pytest
from app.backend.chat.web_intent import explicit_web_intent, effective_web_settings

@pytest.mark.parametrize("prompt", [
    "Search the public web for an obscure observatory yearbook.",
    "Please fetch related data from websites.",
    "Find online the original report and give its page number.",
    "Do a web search for the instrument's calibration.",
    "Please research the history of this observatory.",
    "Read https://docs.python.org/3/library/asyncio-task.html and explain cancellation.",
    "Summarize https://example.org/report.",
])
def test_explicit_requests_enable_search_without_persistent_toggle(prompt):
    original={"research_available":False,"web_search_enabled":False}
    result=effective_web_settings(original,prompt)
    assert result["research_forced"] and result["web_search_enabled"]
    assert result["research_profile"]=='instant'
    assert original=={"research_available":False,"web_search_enabled":False}

@pytest.mark.parametrize("prompt", ["Hello", "Explain binary search", "Write a search function", "What is a website?", "What is web search?", "Explain how to search the internet", "Translate `search the web` into French"])
def test_ordinary_prompts_do_not_force_search(prompt):
    assert explicit_web_intent(prompt) is None

@pytest.mark.parametrize("prompt", ["Do not search the web; explain from memory", "Answer without web search", "Don't browse the internet"])
def test_explicit_opt_out_overrides_selected_research(prompt):
    result=effective_web_settings({"research_available":True,"research_forced":True},prompt)
    assert not result['research_forced'] and not result['web_search_enabled']

def test_deep_research_is_a_distinct_explicit_choice():
    assert effective_web_settings({},'Do in-depth research on atmospheric measurements')['research_profile']=='cooking'

def test_search_instruction_inside_a_json_payload_is_not_executed():
    assert explicit_web_intent('{"example":"Search the public web for reports"}') is None

def test_quoted_url_reading_instruction_is_data():
    assert explicit_web_intent('Translate "Read https://example.org/report" into French') is None
    assert explicit_web_intent('Write Python to fetch https://example.org/report') is None
