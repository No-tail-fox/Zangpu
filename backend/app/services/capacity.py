from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping
from time import time_ns
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.app.limits.model_pool import ModelPoolLimiter, ModelPoolPolicy, ModelPoolSnapshot

CapacityState = Literal["idle", "available", "saturated", "queued"]


class AdminModelPoolCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pool_id: str
    model_ids: list[str]
    state: CapacityState
    active_count: int
    active_limit: int
    active_remaining: int
    pool_queue_count: int
    next_active_expires_at_ms: int
    next_queue_expires_at_ms: int
    observed_at_ms: int


class AdminCapacitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: CapacityState
    pool_count: int
    active_count: int
    active_limit: int
    active_remaining: int
    global_queue_count: int
    global_queue_limit: int
    global_queue_remaining: int
    observed_at_ms: int
    pools: list[AdminModelPoolCapacity]


class AdminCapacityService:
    def __init__(
        self,
        limiter: ModelPoolLimiter,
        *,
        policies: Mapping[str, ModelPoolPolicy],
        global_queue_limit: int,
        epoch_milliseconds: Callable[[], int] | None = None,
    ) -> None:
        if not 0 <= global_queue_limit <= 10_000:
            raise ValueError("global queue limit is invalid")
        pools: dict[str, dict[str, object]] = {}
        grouped_models: defaultdict[str, list[str]] = defaultdict(list)
        for model_id, policy in policies.items():
            grouped_models[policy.pool_id].append(model_id)
            existing = pools.setdefault(
                policy.pool_id,
                {"active_limit": policy.active_limit},
            )
            if existing["active_limit"] != policy.active_limit:
                raise ValueError("models sharing a pool must use the same active limit")
        self._limiter = limiter
        self._global_queue_limit = global_queue_limit
        self._pools = tuple(
            (pool_id, int(pools[pool_id]["active_limit"]), sorted(grouped_models[pool_id]))
            for pool_id in sorted(pools)
        )
        self._epoch_milliseconds = epoch_milliseconds or (lambda: time_ns() // 1_000_000)

    async def snapshot(self) -> AdminCapacitySnapshot:
        if not self._pools:
            return AdminCapacitySnapshot(
                state="idle",
                pool_count=0,
                active_count=0,
                active_limit=0,
                active_remaining=0,
                global_queue_count=0,
                global_queue_limit=self._global_queue_limit,
                global_queue_remaining=self._global_queue_limit,
                observed_at_ms=self._epoch_milliseconds(),
                pools=[],
            )

        observations = await asyncio.gather(
            *(
                self._limiter.observe(
                    pool_id=pool_id,
                    active_limit=active_limit,
                    queue_limit=self._global_queue_limit,
                )
                for pool_id, active_limit, _model_ids in self._pools
            )
        )
        pools = [
            self._pool_capacity(pool_id, model_ids, observation)
            for (pool_id, _active_limit, model_ids), observation in zip(
                self._pools, observations, strict=True
            )
        ]
        latest = max(observations, key=lambda item: item.observed_at_ms)
        active_count = sum(pool.active_count for pool in pools)
        active_limit = sum(pool.active_limit for pool in pools)
        active_remaining = sum(pool.active_remaining for pool in pools)
        global_queue_count = latest.queue_count
        state: CapacityState
        if global_queue_count > 0:
            state = "queued"
        elif active_count == 0:
            state = "idle"
        elif any(pool.state == "saturated" for pool in pools):
            state = "saturated"
        else:
            state = "available"
        return AdminCapacitySnapshot(
            state=state,
            pool_count=len(pools),
            active_count=active_count,
            active_limit=active_limit,
            active_remaining=active_remaining,
            global_queue_count=global_queue_count,
            global_queue_limit=self._global_queue_limit,
            global_queue_remaining=max(self._global_queue_limit - global_queue_count, 0),
            observed_at_ms=latest.observed_at_ms,
            pools=pools,
        )

    @staticmethod
    def _pool_capacity(
        pool_id: str,
        model_ids: list[str],
        snapshot: ModelPoolSnapshot,
    ) -> AdminModelPoolCapacity:
        state: CapacityState
        if snapshot.pool_queue_count > 0:
            state = "queued"
        elif snapshot.active_count == 0:
            state = "idle"
        elif snapshot.active_remaining == 0:
            state = "saturated"
        else:
            state = "available"
        return AdminModelPoolCapacity(
            pool_id=pool_id,
            model_ids=model_ids,
            state=state,
            active_count=snapshot.active_count,
            active_limit=snapshot.active_limit,
            active_remaining=snapshot.active_remaining,
            pool_queue_count=snapshot.pool_queue_count,
            next_active_expires_at_ms=snapshot.next_active_expires_at_ms,
            next_queue_expires_at_ms=snapshot.next_queue_expires_at_ms,
            observed_at_ms=snapshot.observed_at_ms,
        )
