from __future__ import annotations

from datetime import datetime, timezone

from app.models import Classification, FailedClassificationItem, QuietQueueItem
from app.state import CLAIMING_PREFIX, QUIET_QUEUE_KEY, SEEN_PREFIX


def test_two_phase_claim_then_seen(state, redis_client) -> None:
    assert state.try_claim("s1") is True
    assert state.try_claim("s1") is False
    assert redis_client.exists(f"{CLAIMING_PREFIX}s1")
    assert not redis_client.exists(f"{SEEN_PREFIX}s1")

    state.mark_seen_and_release("s1")
    assert redis_client.exists(f"{SEEN_PREFIX}s1")
    assert not redis_client.exists(f"{CLAIMING_PREFIX}s1")
    assert state.try_claim("s1") is False


def test_release_claim_allows_retry(state, redis_client) -> None:
    assert state.try_claim("s2") is True
    state.release_claim("s2")
    assert not redis_client.exists(f"{CLAIMING_PREFIX}s2")
    assert state.try_claim("s2") is True


def test_seen_blocks_claim_even_without_claiming_key(state, redis_client) -> None:
    redis_client.set(f"{SEEN_PREFIX}s3", "1")
    assert state.try_claim("s3") is False


def test_quiet_queue_drain_is_atomic_and_ordered(state) -> None:
    items = [
        QuietQueueItem(
            story_id=f"s{i}",
            classification=Classification(
                is_new_grad_swe=True,
                company=f"Co{i}",
                role_title="SWE",
                job_link=None,
                urgency_score=3,
                reason="drop",
            ),
            queued_at=datetime.now(timezone.utc),
        )
        for i in range(3)
    ]
    for item in items:
        state.push_quiet(item)

    drained = state.drain_quiet()
    assert [item.story_id for item in drained] == ["s0", "s1", "s2"]
    assert state.drain_quiet() == []

    state.restore_quiet(drained)
    restored = state.drain_quiet()
    assert [item.story_id for item in restored] == ["s0", "s1", "s2"]


def test_dlq_push(state, redis_client) -> None:
    state.push_dlq(
        FailedClassificationItem(
            story_id="bad",
            error="parse failed",
            failed_at=datetime.now(timezone.utc),
            link_urls=["https://example.com"],
        )
    )
    assert redis_client.llen("queue:failed_classifications") == 1


def test_job_lock(state) -> None:
    assert state.acquire_job_lock("poller", ttl_seconds=30) is True
    assert state.acquire_job_lock("poller", ttl_seconds=30) is False
    state.release_job_lock("poller")
    assert state.acquire_job_lock("poller", ttl_seconds=30) is True


def test_lua_drain_deletes_key(state, redis_client) -> None:
    redis_client.rpush(QUIET_QUEUE_KEY, "{}")
    state.drain_quiet()
    assert redis_client.exists(QUIET_QUEUE_KEY) == 0
