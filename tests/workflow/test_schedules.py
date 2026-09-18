from datetime import date, time

from llm_agent_eval.schedules import Schedule, ScheduleStore, due_daily_slots


def test_daily_slots_are_timezone_aware_and_idempotently_locked():
    slots = due_daily_slots(date(2026, 3, 7), date(2026, 3, 10), time(9), "Asia/Kolkata")
    assert len(slots) == 4
    store = ScheduleStore()
    schedule = Schedule("s1", "Asia/Kolkata", time(9))
    assert store.acquire(schedule, slots[0], "worker-a") is True
    assert store.acquire(schedule, slots[0], "worker-b") is False
    assert store.enqueue_key(schedule, slots[0]) == store.enqueue_key(schedule, slots[0])
