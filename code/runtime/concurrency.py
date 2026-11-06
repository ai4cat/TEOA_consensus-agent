"""Bounded dispatch and two-window adaptive concurrency; no model payload changes."""
import math,time,threading
from collections import deque
from concurrent.futures import Future,ThreadPoolExecutor

class AdaptiveLimit:
    def __init__(self,initial=4,maximum=16,*,window_seconds=30,clock=time.monotonic):
        if not 1<=initial<=maximum:raise ValueError('Invalid concurrency bounds')
        self.limit=initial;self.maximum=maximum;self.window_seconds=window_seconds;self.clock=clock
        self.stable_windows=0;self.previous=None;self.last_decrease=-math.inf;self.epoch=0
        self.last_event='initial';self.windows=0;self._reset(clock())
    def _reset(self,now):
        self.start=now;self.samples=[];self.sample_goal=max(32,self.limit)
    def decrease(self,reason,now):
        # One burst of failures should not halve the limit once per failing request.
        if now-self.last_decrease<10:return
        self.limit=max(1,self.limit//2);self.epoch+=1;self.stable_windows=0;self.previous=None
        self.last_decrease=now;self.last_event=reason;self._reset(now)
    def observe(self,success,elapsed,*,http_status=None,error_type='',epoch=None):
        now=self.clock();timeout='timeout' in str(error_type).lower()
        if http_status==429 or timeout:
            self.decrease('rate_limit' if http_status==429 else 'timeout',now);return
        if epoch is not None and epoch!=self.epoch:return
        self.samples.append((bool(success),max(0,float(elapsed))))
        failures=sum(not s for s,_ in self.samples)
        if len(self.samples)>=32 and failures/len(self.samples)>.02:
            self.decrease('failure_rate',now);return
        duration=max(now-self.start,.001)
        if len(self.samples)<self.sample_goal or duration<self.window_seconds:return
        times=sorted(t for _,t in self.samples);p95=times[max(0,math.ceil(.95*len(times))-1)]
        rate=sum(s for s,_ in self.samples)/duration
        stable=failures/len(self.samples)<=.02
        if self.previous:
            stable=stable and p95<=self.previous['p95']*1.25 and rate>=self.previous['rate']*.9
        self.windows+=1
        if stable:
            self.stable_windows+=1;self.previous={'p95':p95,'rate':rate};self.last_event='stable_window'
            if self.stable_windows>=2:
                self.limit=min(self.maximum,self.limit*2);self.epoch+=1
                self.stable_windows=0;self.previous=None;self.last_event='doubled_or_at_ceiling'
        else:
            self.decrease('latency_or_throughput_regression',now)
        self._reset(now)
    def record(self):
        return {'policy':'two-stable-windows-v1','limit':self.limit,'maximum':self.maximum,
            'stable_windows':self.stable_windows,'completed_windows':self.windows,
            'window_samples':len(self.samples),'window_sample_goal':self.sample_goal,
            'minimum_window_seconds':self.window_seconds,'last_event':self.last_event}

class AdaptiveExecutor:
    """Only start workers that can use the current capacity, including small queues.

    Returned Futures support the existing as_completed callers. Queued work stays
    as lightweight tuples, not threads waiting on the HTTP admission gate.
    """
    def __init__(self,max_workers=None,*,transport,controller):
        self.transport=transport;self.controller=controller
        self.maximum=min(max_workers or transport.maximum,transport.maximum)
        self.pool=ThreadPoolExecutor(max_workers=self.maximum,thread_name_prefix='stage_task')
        self.cv=threading.Condition();self.queue=deque();self.active=0;self.closing=False;self.failure=None
        self.dispatcher=threading.Thread(target=self._dispatch,name='stage_dispatch',daemon=True)
        self.dispatcher.start()
    def submit(self,fn,*args,**kwargs):
        future=Future()
        with self.cv:
            if self.closing:raise RuntimeError('Executor is closed')
            if self.failure:future.set_exception(self.failure)
            else:self.queue.append((future,fn,args,kwargs));self.cv.notify_all()
        return future
    def _complete(self,outer,inner):
        try:
            if not outer.cancelled():
                try:outer.set_result(inner.result())
                except BaseException as exc:outer.set_exception(exc)
        finally:
            with self.cv:self.active-=1;self.cv.notify_all()
    def _dispatch(self):
        while True:
            with self.cv:
                try:self.controller.check()
                except BaseException as exc:
                    self.failure=exc
                    while self.queue:
                        future,*_=self.queue.popleft()
                        if not future.cancelled():future.set_exception(exc)
                if self.closing and not self.queue and not self.active:return
                capacity=min(self.maximum,self.transport.effective_limit())
                if self.queue and self.active<capacity and not self.failure:
                    outer,fn,args,kwargs=self.queue.popleft()
                    if not outer.set_running_or_notify_cancel():continue
                    self.active+=1
                    try:inner=self.pool.submit(fn,*args,**kwargs)
                    except BaseException as exc:
                        self.active-=1;outer.set_exception(exc);continue
                    inner.add_done_callback(lambda f,o=outer:self._complete(o,f))
                else:self.cv.wait(.1)
    def shutdown(self,wait=True,cancel_futures=False):
        with self.cv:
            self.closing=True
            if cancel_futures:
                while self.queue:self.queue.popleft()[0].cancel()
            self.cv.notify_all()
        if wait:self.dispatcher.join()
        self.pool.shutdown(wait=wait,cancel_futures=cancel_futures)
    def __enter__(self):return self
    def __exit__(self,*args):self.shutdown()
