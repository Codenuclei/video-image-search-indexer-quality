from __future__ import annotations

import pytest

from app.routers.drive import list_drive_files_page


class _Result:
    def __init__(self, *, count: int | None = None) -> None:
        self._count = count

    def scalar_one(self) -> int:
        assert self._count is not None
        return self._count

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[object]:
        return []


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(count=0) if len(self.statements) == 1 else _Result()


@pytest.mark.asyncio
async def test_drive_files_page_scopes_count_and_items_to_root_folder() -> None:
    session = _RecordingSession()

    result = await list_drive_files_page(
        status=None,
        source="drive",
        root_folder_id="folder-123",
        limit=50,
        offset=0,
        session=session,  # type: ignore[arg-type]
    )

    assert result["total"] == 0
    assert len(session.statements) == 2
    for statement in session.statements:
        compiled = statement.compile()
        assert "drive_files.root_folder_id" in str(compiled)
        assert "folder-123" in compiled.params.values()
