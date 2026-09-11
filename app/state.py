"""Upstash Redis two-phase locks, quiet-hours queue, and classification DLQ."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import redis

from app.config import Settings
from app.models import FailedClassificationItem, QuietQueueItem

logger = logging.getLogger(__name__)

SEEN_PREFIX = "story:seen:"
CLAIMING_PREFIX = "story:claiming:"
QUIET_QUEUE_KEY = "queue:quiet_hours"
DLQ_KEY = "queue:failed_classifications"
POLLER_LOCK_KEY = "lock:poller"
MORNING_LOCK_KEY = "lock:morning_burst"

_CLAIM_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
local ok = redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[1])
if ok then
  return 1
end
return 0
"""

_CLAIM_MANY_SCRIPT = """
local claimed = {}
for i = 2, #ARGV do
  local key_index = i - 1
  local seen_key = KEYS[(key_index * 2) - 1]
  local claiming_key = KEYS[key_index * 2]
  if redis.call('EXISTS', seen_key) == 0 then
    local ok = redis.call('SET', claiming_key, '1', 'NX', 'EX', ARGV[1])
    if ok then
      table.insert(claimed, ARGV[i])
    end
  end
end
return claimed
"""

_PROMOTE_SCRIPT = """
redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
redis.call('DEL', KEYS[2])
return 1
"""

_DRAIN_SCRIPT = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
redis.call('DEL', KEYS[1])
return items
"""


class RedisState:
    def __init__(self, settings: Settings, client: redis.Redis | None = None) -> None:
        self._settings = settings
        self._redis = client or redis.from_url(
            settings.upstash_redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
        )
        self._claim_sha: str | None = None
        self._claim_many_sha: str | None = None
        self._promote_sha: str | None = None
        self._drain_sha: str | None = None
        self._lua_enabled = False
        try:
            self._claim_sha = self._redis.script_load(_CLAIM_SCRIPT)
            self._claim_many_sha = self._redis.script_load(_CLAIM_MANY_SCRIPT)
            self._promote_sha = self._redis.script_load(_PROMOTE_SCRIPT)
            self._drain_sha = self._redis.script_load(_DRAIN_SCRIPT)
            self._lua_enabled = True
        except redis.RedisError:
            logger.warning("redis Lua unavailable; using native command fallbacks")

    def close(self) -> None:
        try:
            self._redis.close()
        except Exception:
            logger.debug("redis close failed", exc_info=True)

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def is_seen(self, story_id: str) -> bool:
        return bool(self._redis.exists(_seen_key(story_id)))

    def try_claim(self, story_id: str) -> bool:
        if self._lua_enabled:
            try:
                result = self._eval(
                    _CLAIM_SCRIPT,
                    self._claim_sha,
                    keys=[_seen_key(story_id), _claiming_key(story_id)],
                    args=[str(self._settings.claim_ttl_seconds)],
                )
                return int(result or 0) == 1
            except redis.RedisError:
                self._disable_lua()
        return self._try_claim_native(story_id)

    def try_claim_many(self, story_ids: Sequence[str]) -> set[str]:
        unique_ids = list(dict.fromkeys(story_ids))
        if not unique_ids:
            return set()
        if self._lua_enabled:
            keys = [key for story_id in unique_ids for key in (_seen_key(story_id), _claiming_key(story_id))]
            try:
                result = self._eval(
                    _CLAIM_MANY_SCRIPT,
                    self._claim_many_sha,
                    keys=keys,
                    args=[str(self._settings.claim_ttl_seconds), *unique_ids],
                )
                return {str(story_id) for story_id in (result or [])}
            except redis.RedisError:
                self._disable_lua()
        return {story_id for story_id in unique_ids if self._try_claim_native(story_id)}

    def mark_seen_and_release(self, story_id: str) -> None:
        if self._lua_enabled:
            try:
                self._eval(
                    _PROMOTE_SCRIPT,
                    self._promote_sha,
                    keys=[_seen_key(story_id), _claiming_key(story_id)],
                    args=[str(self._settings.story_ttl_seconds)],
                )
                return
            except redis.RedisError:
                self._disable_lua()
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(_seen_key(story_id), "1", ex=self._settings.story_ttl_seconds)
        pipe.delete(_claiming_key(story_id))
        pipe.execute()

    def release_claim(self, story_id: str) -> None:
        self._redis.delete(_claiming_key(story_id))

    def push_quiet(self, item: QuietQueueItem) -> None:
        self._redis.rpush(QUIET_QUEUE_KEY, item.model_dump_json())

    def drain_quiet(self) -> list[QuietQueueItem]:
        raw_items = self._drain_raw()
        parsed: list[QuietQueueItem] = []
        for raw in raw_items or []:
            try:
                parsed.append(QuietQueueItem.model_validate_json(raw))
            except Exception:
                logger.exception("skipping corrupt quiet-queue payload")
        return parsed

    def restore_quiet(self, items: Sequence[QuietQueueItem]) -> None:
        if not items:
            return
        payload = [item.model_dump_json() for item in items]
        self._redis.rpush(QUIET_QUEUE_KEY, *payload)

    def push_dlq(self, item: FailedClassificationItem) -> None:
        self._redis.rpush(DLQ_KEY, item.model_dump_json())

    def acquire_job_lock(self, name: str, ttl_seconds: int) -> bool:
        return bool(self._redis.set(_job_lock_key(name), "1", nx=True, ex=ttl_seconds))

    def release_job_lock(self, name: str) -> None:
        self._redis.delete(_job_lock_key(name))

    def _drain_raw(self) -> list[str]:
        if self._lua_enabled:
            try:
                raw = self._eval(_DRAIN_SCRIPT, self._drain_sha, keys=[QUIET_QUEUE_KEY], args=[])
                return list(raw or [])
            except redis.RedisError:
                self._disable_lua()
        pipe = self._redis.pipeline(transaction=True)
        pipe.lrange(QUIET_QUEUE_KEY, 0, -1)
        pipe.delete(QUIET_QUEUE_KEY)
        items, _deleted = pipe.execute()
        return list(items or [])

    def _try_claim_native(self, story_id: str) -> bool:
        if self._redis.exists(_seen_key(story_id)):
            return False
        return bool(
            self._redis.set(
                _claiming_key(story_id),
                "1",
                nx=True,
                ex=self._settings.claim_ttl_seconds,
            )
        )

    def _disable_lua(self) -> None:
        if self._lua_enabled:
            logger.warning("redis Lua failed at runtime; switching to native command fallbacks")
        self._lua_enabled = False

    def _eval(
        self,
        script: str,
        sha: str | None,
        *,
        keys: list[str],
        args: list[str],
    ) -> object:
        if sha:
            try:
                return self._redis.evalsha(sha, len(keys), *keys, *args)
            except redis.exceptions.NoScriptError:
                logger.warning("redis script missing from cache; reloading")
        return self._redis.eval(script, len(keys), *keys, *args)


def _seen_key(story_id: str) -> str:
    return f"{SEEN_PREFIX}{story_id}"


def _claiming_key(story_id: str) -> str:
    return f"{CLAIMING_PREFIX}{story_id}"


def _job_lock_key(name: str) -> str:
    if name == "poller":
        return POLLER_LOCK_KEY
    if name == "morning_burst":
        return MORNING_LOCK_KEY
    return f"lock:{name}"
