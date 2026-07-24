from app.workers.index_errors import friendly_index_error_message


def test_friendly_infailed_sql_transaction():
    msg = (
        "(sqlalchemy.dialects.postgresql.asyncpg.Error) "
        "<class 'asyncpg.exceptions.InFailedSQLTransactionError'>: "
        "current transaction is aborted, commands ignored until end of transaction block\n"
        "[SQL: INSERT INTO face_clusters ...]"
    )
    out = friendly_index_error_message(RuntimeError(msg))
    assert "transaction aborted" in out.lower() or "face clustering" in out.lower()
    assert "INSERT INTO" not in out
    assert len(out) < 200


def test_friendly_plain_message_passthrough():
    assert friendly_index_error_message(ValueError("download failed")) == "download failed"


def test_friendly_deadlock():
    out = friendly_index_error_message(RuntimeError("deadlock detected DETAIL: Process 1"))
    assert "deadlock" in out.lower()
    assert len(out) < 200
