"""Tests for leave_at computation and the notification timing/state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from when2leave.db import NotifyState
from when2leave.notifier import compute_leave_at, evaluate_notification

EVENT_START = datetime(2026, 7, 9, 18, 0, tzinfo=UTC)


def test_compute_leave_at_subtracts_travel_and_prep() -> None:
    leave_at = compute_leave_at(EVENT_START, travel_time_s=1800, prep_buffer_s=600)
    assert leave_at == EVENT_START - timedelta(minutes=30) - timedelta(minutes=10)


def _base_kwargs(**overrides: object) -> dict[str, object]:
    leave_at = EVENT_START - timedelta(minutes=40)  # 30 min travel + 10 min prep
    kwargs: dict[str, object] = {
        "now": leave_at - timedelta(minutes=20),
        "event_title": "Dentist",
        "leave_at": leave_at,
        "travel_time_s": 1800,
        "notify_lead_s": 900,  # 15 min
        "notify_state": NotifyState.NONE,
        "last_notified_leave_at": None,
        "reshift_threshold_s": 300,
    }
    kwargs.update(overrides)
    return kwargs


def test_no_notification_far_before_heads_up_window() -> None:
    new_state, notification = evaluate_notification(**_base_kwargs())
    assert new_state == NotifyState.NONE
    assert notification is None


def test_heads_up_fires_when_entering_lead_window() -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    new_state, notification = evaluate_notification(
        **_base_kwargs(now=leave_at - timedelta(minutes=10))
    )
    assert new_state == NotifyState.HEADS_UP_SENT
    assert notification is not None
    assert notification.kind == "heads_up"


def test_heads_up_stays_quiet_on_repeat_calls_without_worsening() -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    new_state, notification = evaluate_notification(
        **_base_kwargs(
            now=leave_at - timedelta(minutes=5),
            notify_state=NotifyState.HEADS_UP_SENT,
            last_notified_leave_at=leave_at,
        )
    )
    assert new_state == NotifyState.HEADS_UP_SENT
    assert notification is None


def test_heads_up_resent_when_leave_at_worsens_beyond_threshold() -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    worse_leave_at = leave_at - timedelta(minutes=10)  # traffic got worse
    new_state, notification = evaluate_notification(
        **_base_kwargs(
            now=worse_leave_at - timedelta(minutes=5),
            leave_at=worse_leave_at,
            notify_state=NotifyState.HEADS_UP_SENT,
            last_notified_leave_at=leave_at,
        )
    )
    assert new_state == NotifyState.HEADS_UP_SENT
    assert notification is not None
    assert notification.kind == "heads_up"


def test_heads_up_not_resent_for_small_worsening_under_threshold() -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    slightly_worse = leave_at - timedelta(minutes=2)  # under the 5 min threshold
    new_state, notification = evaluate_notification(
        **_base_kwargs(
            now=slightly_worse - timedelta(minutes=5),
            leave_at=slightly_worse,
            notify_state=NotifyState.HEADS_UP_SENT,
            last_notified_leave_at=leave_at,
        )
    )
    assert new_state == NotifyState.HEADS_UP_SENT
    assert notification is None


def test_leave_now_fires_at_leave_at() -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    new_state, notification = evaluate_notification(
        **_base_kwargs(now=leave_at, notify_state=NotifyState.HEADS_UP_SENT)
    )
    assert new_state == NotifyState.LEAVE_NOW_SENT
    assert notification is not None
    assert notification.kind == "leave_now"


def test_running_late_fires_when_first_seen_after_leave_at() -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    new_state, notification = evaluate_notification(
        **_base_kwargs(now=leave_at + timedelta(minutes=5), notify_state=NotifyState.NONE)
    )
    assert new_state == NotifyState.RUNNING_LATE_SENT
    assert notification is not None
    assert notification.kind == "running_late"


@pytest.mark.parametrize("state", [NotifyState.LEAVE_NOW_SENT, NotifyState.RUNNING_LATE_SENT])
def test_no_further_notifications_after_terminal_state(state: NotifyState) -> None:
    leave_at = EVENT_START - timedelta(minutes=40)
    new_state, notification = evaluate_notification(
        **_base_kwargs(now=leave_at + timedelta(minutes=10), notify_state=state)
    )
    assert new_state == state
    assert notification is None
