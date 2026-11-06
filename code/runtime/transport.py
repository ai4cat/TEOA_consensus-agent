import time, threading, asyncio
from functools import partial
from shared.request_transport import RequestTransport, available_memory
from shared.model_api.caller import ResilientCaller
from runtime.concurrency import AdaptiveLimit


class NominationCaller(ResilientCaller):
    def __init__(self, *args, controller, **kwargs):
        self.controller = controller
        super().__init__(*args, **kwargs)

    def send(self, *args, **kwargs):
        self.controller.check()
        return super().send(*args, **kwargs)


class OnlineTransport(RequestTransport):
    """Original wire format and response handling; add a pause-aware admission gate."""

    def __init__(self, *args, controller, **kwargs):
        self.controller = controller
        super().__init__(*args, **kwargs)
        self.adaptive = AdaptiveLimit(self.limit, self.maximum)
        self._key_active = [0] * len(self.keys)
        self._key_until = [0.0] * len(self.keys)
        self._ticket = threading.local()
        self._async_loop = None
        self._async_pending = 0

    def request(self, *args, **kwargs):
        self.controller.check()
        return super().request(*args, **kwargs)

    def effective_limit(self):
        memory = available_memory()
        if memory is not None and memory < 1024**3:
            return 0
        return (
            min(self.limit, 8)
            if memory is not None and memory < 4 * 1024**3
            else self.limit
        )

    def acquire(self):
        with self.cv:
            while True:
                self.controller.check()
                memory = available_memory()
                effective = self.effective_limit()
                self.stats["available_memory_bytes"] = memory
                self.stats["effective_limit"] = effective
                now = time.monotonic()
                eligible = [
                    i for i in range(len(self.keys)) if self._key_until[i] <= now
                ]
                if self.active < effective and eligible:
                    break
                self.cv.wait(0.25)
            self.active += 1
            self.stats["peak"] = max(self.stats["peak"], self.active)
            index = min(
                eligible,
                key=lambda i: (self._key_active[i], (i - self.slot) % len(self.keys)),
            )
            self.slot = (index + 1) % len(self.keys)
            self._key_active[index] += 1
            self._ticket.epoch = self.adaptive.epoch
            self.stats["started_requests"] += 1
            self.stats["key_slots"][str(index + 1)]["started"] += 1
            self.stats["key_slots"][str(index + 1)]["inflight"] = self._key_active[
                index
            ]
            self.status()
            return index

    def release(
        self,
        index,
        success,
        *,
        elapsed=0,
        http_status=None,
        retry_after=0,
        error_type="",
    ):
        with self.cv:
            self.active -= 1
            self._key_active[index] -= 1
            self.recent.append(bool(success))
            key = "completed" if success else "failed"
            self.stats[key + "_requests"] += 1
            slot = self.stats["key_slots"][str(index + 1)]
            slot[key] += 1
            slot["inflight"] = self._key_active[index]
            if http_status == 429:
                self._key_until[index] = max(
                    self._key_until[index], time.monotonic() + max(1, retry_after)
                )
                slot["cooldown_seconds"] = max(1, retry_after)
            self.adaptive.observe(
                success,
                elapsed,
                http_status=http_status,
                error_type=error_type,
                epoch=getattr(self._ticket, "epoch", None),
            )
            self.limit = self.adaptive.limit
            self.stats["adaptive"] = self.adaptive.record()
            self.cv.notify_all()
            self.status()

    async def request_async(self, *args, **kwargs):
        return await self.call_async(self.request, *args, **kwargs)

    async def call_async(self, function, *args, **kwargs):
        # Reserve async capacity BEFORE creating a network worker thread.
        loop = asyncio.get_running_loop()
        if self._async_loop is not loop:
            if self._async_pending:
                raise RuntimeError("Concurrent event loops are unsupported")
            self._async_loop = loop
            self._async_cv = asyncio.Condition()
        async with self._async_cv:
            while self._async_pending >= self.effective_limit():
                self.controller.check()
                try:
                    await asyncio.wait_for(self._async_cv.wait(), 0.25)
                except asyncio.TimeoutError:
                    pass
            self.controller.check()
            self._async_pending += 1
        try:
            future = loop.run_in_executor(
                self.executor, partial(function, *args, **kwargs)
            )
        except BaseException:
            async with self._async_cv:
                self._async_pending -= 1
                self._async_cv.notify_all()
            raise

        # On cancellation the HTTP thread can still be active; retain its slot
        # until the real worker finishes, not just until its waiter is cancelled.
        def completed(_):
            async def release_slot():
                async with self._async_cv:
                    self._async_pending -= 1
                    self._async_cv.notify_all()

            loop.create_task(release_slot())

        future.add_done_callback(completed)
        return await asyncio.shield(future)

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)


def make_transport(ctx):
    from runtime.provider_credentials import load_provider_keys

    keys = load_provider_keys(ctx.config)
    if not keys or len(set(keys)) != len(keys):
        raise ValueError("Distinct credentials required")
    return OnlineTransport(
        ctx.communications / "requests",
        keys,
        controller=ctx.controller,
        initial=ctx.config["initial_concurrency"],
        maximum=ctx.config["max_concurrency"],
        base_url=ctx.config["base_url"],
    ), keys
