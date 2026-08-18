from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import redis
from redis.exceptions import RedisError, ResponseError


class AsyncRunQueueUnavailable(RuntimeError):
    """Raised when Redis cannot serve the async queue operation."""


class AsyncRunQueueMessageError(RuntimeError):
    """Raised when a stream entry violates the safe event contract."""

    def __init__(self, message: str, *, message_id: str | None = None) -> None:
        super().__init__(message)
        self.message_id = message_id


@dataclass(frozen=True)
class RedisStreamMessage:
    message_id: str
    run_id: UUID
    attempt: int
    state_version: int


class RedisCapacityLease:
    def __init__(
        self,
        queue: RedisRunQueue,
        member: str,
        lease_seconds: int,
    ) -> None:
        self._queue = queue
        self._member = member
        self._lease_seconds = lease_seconds
        self._released = False

    def renew(self) -> bool:
        if self._released:
            return False
        return self._queue._renew_capacity(
            self._member,
            self._lease_seconds,
        )

    def release(self) -> bool:
        if self._released:
            return False
        released = self._queue._release_capacity(self._member)
        if released:
            self._released = True
        return released


_ACQUIRE_CAPACITY_SCRIPT = """
local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local member = ARGV[3]
local limit = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local existing = redis.call('ZSCORE', KEYS[1], member)
if existing then
    redis.call('ZADD', KEYS[1], expiry, member)
    return 1
end
if redis.call('ZCARD', KEYS[1]) >= limit then
    return 0
end
redis.call('ZADD', KEYS[1], expiry, member)
return 1
"""

_RENEW_CAPACITY_SCRIPT = """
local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local member = ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local existing = redis.call('ZSCORE', KEYS[1], member)
if not existing then
    return 0
end
redis.call('ZADD', KEYS[1], expiry, member)
return 1
"""

_PUBLISH_OUTBOX_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then
    return existing
end
local message_id = redis.call(
    'XADD',
    KEYS[2],
    '*',
    'run_id',
    ARGV[1],
    'attempt',
    ARGV[2],
    'state_version',
    ARGV[3]
)
redis.call('SET', KEYS[1], message_id)
return message_id
"""


class RedisRunQueue:
    def __init__(
        self,
        redis_url: str | None,
        stream_name: str,
        consumer_group: str,
        *,
        redis_client: Any | None = None,
    ) -> None:
        self.stream_name = stream_name.strip()
        self.consumer_group = consumer_group.strip()
        if not self.stream_name or not self.consumer_group:
            raise ValueError("stream_name and consumer_group are required")
        self.capacity_key = (
            f"{self.stream_name}:{self.consumer_group}:capacity"
        )
        if redis_client is not None:
            self.redis = redis_client
        elif redis_url is None or not redis_url.strip():
            self.redis = None
        else:
            try:
                self.redis = redis.Redis.from_url(
                    redis_url.strip(),
                    decode_responses=True,
                )
            except (RedisError, OSError, TypeError, ValueError):
                self.redis = None

        self._acquire_capacity_script = None
        self._renew_capacity_script = None
        self._publish_outbox_script = None
        if self.redis is not None:
            try:
                self._acquire_capacity_script = self.redis.register_script(
                    _ACQUIRE_CAPACITY_SCRIPT
                )
                self._renew_capacity_script = self.redis.register_script(
                    _RENEW_CAPACITY_SCRIPT
                )
                self._publish_outbox_script = self.redis.register_script(
                    _PUBLISH_OUTBOX_SCRIPT
                )
            except (RedisError, OSError, TypeError, ValueError):
                self.redis = None

    def ping(self) -> bool:
        client = self._client()
        try:
            return bool(client.ping())
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def ensure_group(self) -> bool:
        client = self._client()
        try:
            client.xgroup_create(
                name=self.stream_name,
                groupname=self.consumer_group,
                id="0-0",
                mkstream=True,
            )
            return True
        except ResponseError as exc:
            if str(exc).startswith("BUSYGROUP"):
                return True
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def put_payload(
        self,
        payload_key: str,
        payload_json: str,
        ttl_seconds: int,
    ) -> bool:
        client = self._client()
        if not payload_key or not payload_json or ttl_seconds <= 0:
            raise ValueError("payload key, JSON and TTL are required")
        try:
            stored = client.set(payload_key, payload_json, ex=ttl_seconds)
            if not stored:
                raise AsyncRunQueueUnavailable(
                    "async queue is unavailable"
                )
            return True
        except AsyncRunQueueUnavailable:
            raise
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def get_payload(self, payload_key: str) -> str | None:
        client = self._client()
        try:
            value = client.get(payload_key)
            if value is None:
                return None
            return value.decode() if isinstance(value, bytes) else str(value)
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def delete_payload(self, payload_key: str) -> bool:
        client = self._client()
        try:
            return int(client.delete(payload_key)) == 1
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def publish(
        self,
        run_id: UUID,
        attempt: int,
        state_version: int,
        *,
        outbox_id: UUID | None = None,
    ) -> str:
        client = self._client()
        normalized_run_id = _normalize_run_id(run_id)
        if attempt < 0 or state_version < 0:
            raise ValueError("attempt and state_version must not be negative")
        normalized_outbox_id = (
            None if outbox_id is None else _normalize_run_id(outbox_id)
        )
        try:
            if normalized_outbox_id is not None:
                if self._publish_outbox_script is None:
                    raise AsyncRunQueueUnavailable(
                        "async queue is unavailable"
                    )
                return str(
                    self._publish_outbox_script(
                        keys=[
                            self._outbox_dedupe_key(normalized_outbox_id),
                            self.stream_name,
                        ],
                        args=[
                            str(normalized_run_id),
                            str(attempt),
                            str(state_version),
                        ],
                    )
                )
            return str(
                client.xadd(
                    self.stream_name,
                    {
                        "run_id": str(normalized_run_id),
                        "attempt": str(attempt),
                        "state_version": str(state_version),
                    },
                )
            )
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def read(
        self,
        consumer_name: str,
        count: int,
        block_ms: int,
    ) -> list[RedisStreamMessage]:
        client = self._client()
        if count < 1 or block_ms < 0:
            raise ValueError("count must be positive and block_ms non-negative")
        try:
            response = client.xreadgroup(
                groupname=self.consumer_group,
                consumername=consumer_name,
                streams={self.stream_name: ">"},
                count=count,
                block=block_ms,
            )
            return _parse_stream_entries(response)
        except AsyncRunQueueMessageError:
            raise
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def claim_expired(
        self,
        consumer_name: str,
        min_idle_ms: int,
        count: int,
    ) -> list[RedisStreamMessage]:
        client = self._client()
        if min_idle_ms < 0 or count < 1:
            raise ValueError("min_idle_ms must be non-negative and count positive")
        try:
            response = client.xautoclaim(
                self.stream_name,
                self.consumer_group,
                consumer_name,
                min_idle_ms,
                start_id="0-0",
                count=count,
            )
            entries = response[1] if isinstance(response, (list, tuple)) else []
            return _parse_stream_entries(
                [(self.stream_name, entries)]
            )
        except AsyncRunQueueMessageError:
            raise
        except (RedisError, OSError, TypeError, ValueError, IndexError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def ack(self, message_id: str) -> bool:
        client = self._client()
        try:
            return int(client.xack(self.stream_name, self.consumer_group, message_id)) == 1
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def acquire_capacity(
        self,
        owner_id: str,
        run_id: UUID,
        limit: int,
        lease_seconds: int,
    ) -> RedisCapacityLease | None:
        client = self._client()
        if limit < 1 or lease_seconds < 0:
            raise ValueError("capacity limit must be positive and lease non-negative")
        member = _lease_member(owner_id, run_id)
        try:
            now_ms = self._time_ms()
            result = self._acquire_capacity_script(
                keys=[self.capacity_key],
                args=[
                    now_ms,
                    now_ms + (lease_seconds * 1000),
                    member,
                    limit,
                ],
            )
            if int(result) != 1:
                return None
            return RedisCapacityLease(self, member, lease_seconds)
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def _renew_capacity(self, member: str, lease_seconds: int) -> bool:
        self._client()
        if lease_seconds < 0:
            raise ValueError("lease_seconds must not be negative")
        try:
            now_ms = self._time_ms()
            result = self._renew_capacity_script(
                keys=[self.capacity_key],
                args=[
                    now_ms,
                    now_ms + (lease_seconds * 1000),
                    member,
                ],
            )
            return int(result) == 1
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def _release_capacity(self, member: str) -> bool:
        client = self._client()
        try:
            return int(client.zrem(self.capacity_key, member)) == 1
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def _time_ms(self) -> int:
        client = self._client()
        try:
            seconds, micros = client.time()
            return (int(seconds) * 1000) + (int(micros) // 1000)
        except (RedisError, OSError, TypeError, ValueError):
            raise AsyncRunQueueUnavailable(
                "async queue is unavailable"
            ) from None

    def _client(self):
        if self.redis is None:
            raise AsyncRunQueueUnavailable("async queue is unavailable")
        if self._acquire_capacity_script is None or self._renew_capacity_script is None:
            raise AsyncRunQueueUnavailable("async queue is unavailable")
        return self.redis

    def _outbox_dedupe_key(self, outbox_id: UUID) -> str:
        return (
            f"{self.stream_name}:{self.consumer_group}:outbox:"
            f"{outbox_id}"
        )


def _parse_stream_entries(response) -> list[RedisStreamMessage]:
    messages: list[RedisStreamMessage] = []
    try:
        for _stream_name, entries in response or []:
            for raw_message_id, fields in entries:
                message_id = str(raw_message_id)
                try:
                    if set(fields) != {"run_id", "attempt", "state_version"}:
                        raise ValueError("unexpected stream fields")
                    attempt = int(fields["attempt"])
                    state_version = int(fields["state_version"])
                    if attempt < 0 or state_version < 0:
                        raise ValueError("stream counters must not be negative")
                    run_id = _normalize_run_id(fields["run_id"])
                except (KeyError, TypeError, ValueError):
                    raise AsyncRunQueueMessageError(
                        "async queue message is invalid",
                        message_id=message_id,
                    ) from None
                messages.append(
                    RedisStreamMessage(
                        message_id=message_id,
                        run_id=run_id,
                        attempt=attempt,
                        state_version=state_version,
                    )
                )
    except AsyncRunQueueMessageError:
        raise
    except (KeyError, TypeError, ValueError):
        raise AsyncRunQueueMessageError(
            "async queue message is invalid"
        ) from None
    return messages


def _normalize_run_id(run_id: UUID) -> UUID:
    normalized = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    if normalized.version != 4:
        raise ValueError("run_id must be UUIDv4")
    return normalized


def _lease_member(owner_id: str, run_id: UUID) -> str:
    owner = owner_id.strip()
    if not owner or len(owner) > 128:
        raise ValueError("owner_id must be a non-empty bounded string")
    return f"{owner}:{_normalize_run_id(run_id)}:{uuid4()}"
