import pytest
from app.backend.runtime.resource_pacing import GenerationPacer,normalize_resource_mode

class Clock:
    def __init__(self):self.time=0.;self.sleeps=[]
    def now(self):return self.time
    def sleep(self,seconds):self.sleeps.append(seconds);self.time+=seconds

def test_turtle_adds_matching_idle_time_and_limits_stop_latency():
    clock=Clock();pacer=GenerationPacer('turtle',clock=clock.now,sleep=clock.sleep)
    clock.time+=.2
    assert pacer.pause()
    assert clock.time==pytest.approx(.4)
    assert sum(clock.sleeps)==pytest.approx(.2)
    assert max(clock.sleeps)<=.05
    clock.time+=.1
    assert pacer.pause()
    assert pacer.idle_seconds==pytest.approx(.3)

def test_normal_mode_never_adds_sleep():
    clock=Clock();pacer=GenerationPacer('normal',clock=clock.now,sleep=clock.sleep)
    clock.time=1
    assert pacer.pause()
    assert clock.sleeps==[]

def test_stop_interrupts_long_prefill_pause():
    clock=Clock();pacer=GenerationPacer('turtle',clock=clock.now,sleep=clock.sleep)
    clock.time=10
    assert not pacer.pause(lambda:clock.time>=10.1)
    assert clock.time<=10.15

def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):normalize_resource_mode('turbo')
