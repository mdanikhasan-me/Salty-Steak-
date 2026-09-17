"""Cooperative text-generation pacing; no global GPU or power settings changed."""
from __future__ import annotations
import time

def normalize_resource_mode(value: object) -> str:
    mode = str(value or 'normal').strip().casefold()
    if mode not in {'normal', 'turtle'}:
        raise ValueError('resource_mode must be normal or turtle')
    return mode

class GenerationPacer:
    def __init__(self, mode='normal', *, clock=time.perf_counter, sleep=time.sleep):
        self.mode=normalize_resource_mode(mode)
        self.clock,self.sleep=clock,sleep
        self.started=clock()
        self.idle_seconds=0.0
        self.active_seconds=0.0

    def pause(self, should_stop=None) -> bool:
        if self.mode=='normal':return True
        # Sampling has synchronized the decoder before this boundary. Add the
        # same idle duration as measured active work: target half duty, not a
        # promise of 50% hardware utilization or reduced resident memory.
        active=max(0.0,self.clock()-self.started)
        self.active_seconds+=active
        began=self.clock();deadline=began+active
        while self.clock()<deadline:
            if should_stop and should_stop():
                self.idle_seconds+=self.clock()-began
                return False
            self.sleep(max(0.0,min(.05,deadline-self.clock())))
        self.idle_seconds+=self.clock()-began
        self.started=self.clock()
        return not (should_stop and should_stop())

    def details(self):
        return {'resource_mode_effective':self.mode,
                'generation_duty_target':.5 if self.mode=='turtle' else 1.0,
                'pacing_idle_seconds':round(self.idle_seconds,4),
                'pacing_active_seconds':round(self.active_seconds,4),
                'resource_limit_scope':'text_generation_pacing_not_gpu_memory_or_peak_utilization'}
