from app.db.deadlock import is_deadlock_error


class DeadlockDetectedError(Exception):
    pass


def test_is_deadlock_error_direct():
    assert is_deadlock_error(DeadlockDetectedError("deadlock detected"))


def test_is_deadlock_error_wrapped():
    try:
        raise DeadlockDetectedError("deadlock detected")
    except DeadlockDetectedError as inner:
        outer = RuntimeError("sqlalchemy error")
        outer.__cause__ = inner
        assert is_deadlock_error(outer)


def test_is_deadlock_error_message_only():
    assert is_deadlock_error(RuntimeError("deadlock detected DETAIL: Process 1 waits"))


def test_is_deadlock_error_negative():
    assert not is_deadlock_error(ValueError("connection refused"))
