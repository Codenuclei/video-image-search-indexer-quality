import pytest

from app.search.local import SearchRoleContext, parse_role_context


def test_parse_teacher_with_students():
    text, ctx = parse_role_context("teacher with students")
    assert "student" in ctx.require_all_roles
    assert "non_student" in ctx.require_all_roles


def test_parse_named_person_with_students():
    text, ctx = parse_role_context("Alice with students at party")
    assert ctx.co_occur_roles == ("student",)
    assert "Alice" in text
    assert "party" in text


def test_parse_students_only():
    text, ctx = parse_role_context("students studying")
    assert ctx.require_all_roles == ("student",)
    assert "studying" in text


def test_parse_non_student_with_students_strips_keywords():
    _, ctx = parse_role_context("non students with students")
    assert ctx.require_all_roles == ("non_student", "student")


def test_parse_giving_cheque_to_students_is_not_role_filter():
    text, ctx = parse_role_context("giving cheque to students")
    assert ctx.co_occur_roles == ()
    assert ctx.require_all_roles == ()
    assert "cheque" in text


def test_parse_named_person_giving_cheque_to_students():
    text, ctx = parse_role_context("Pratham Mittal giving cheque to students")
    assert ctx.co_occur_roles == ()
    assert ctx.require_all_roles == ()
    assert ctx.student_context is True
    assert "Pratham Mittal" in text
    assert "cheque" in text


def test_query_has_student_context_for_to_students():
    from app.search.local import query_has_student_context

    assert query_has_student_context("giving cheque to students")
    assert query_has_student_context("Pratham Mittal standing with students")
    assert not query_has_student_context("Pratham Mittal giving cheque")


def test_parse_standing_with_students_uses_co_occur():
    text, ctx = parse_role_context("Pratham Mittal standing with students")
    assert ctx.co_occur_roles == ("student",)
    assert ctx.require_all_roles == ()
    assert "standing" in text


def test_students_holding_cheque_parsed_as_student_action():
    text, ctx = parse_role_context("Students holding cheque")
    assert ctx.require_all_roles == ("student",)
    assert "holding cheque" in text


def test_student_caption_regex_matches():
    from app.search.local import _STUDENT_CAPTION_RE

    assert _STUDENT_CAPTION_RE.search("Pratham Mittal standing with students in a hall")
    assert _STUDENT_CAPTION_RE.search("group of college students")
    assert not _STUDENT_CAPTION_RE.search("faculty meeting indoors")


def test_parse_with_graduates_remains_broad_visual_context():
    text, ctx = parse_role_context("Pratham Mittal with graduates")
    assert ctx.student_context is False
    assert ctx.co_occur_roles == ()
    assert ctx.require_all_roles == ()
    assert "Pratham Mittal" in text
    assert "graduates" in text


def test_pratham_mittal_student_activates_funnel_not_role_sql():
    from app.search.local import person_student_funnel_active

    _, ctx = parse_role_context("Pratham Mittal student")
    assert ctx.student_context is True
    assert ctx.co_occur_roles == ()
    assert ctx.require_all_roles == ()
    assert person_student_funnel_active(["Pratham Mittal"], ctx)
    assert not person_student_funnel_active(["Pratham Mittal"], SearchRoleContext())
    assert not person_student_funnel_active([], ctx)


def test_person_only_and_action_queries_do_not_activate_funnel():
    from app.search.local import person_student_funnel_active, query_has_student_context

    _, person_ctx = parse_role_context("Pratham Mittal")
    assert person_ctx.student_context is False
    assert not person_student_funnel_active(["Pratham Mittal"], person_ctx)

    _, action_ctx = parse_role_context("Pratham Mittal giving cheque")
    assert action_ctx.student_context is False
    assert not query_has_student_context("cheque")
    _, cheque_ctx = parse_role_context("giving cheque")
    assert cheque_ctx.student_context is False
    assert not person_student_funnel_active([], cheque_ctx)


def test_caption_graduates_and_group_id_cards_count_as_students():
    from app.search.local import caption_has_student_evidence

    assert caption_has_student_evidence("graduates standing on a stage")
    assert caption_has_student_evidence("people wearing id cards in a group photo")
    assert caption_has_student_evidence("a crowd with lanyards and name badges")
    assert not caption_has_student_evidence("a man wearing an id badge at a desk")
    assert not caption_has_student_evidence("faculty meeting indoors")


@pytest.mark.asyncio
async def test_resolve_unions_caption_and_extra_names_for_named_person():
    from unittest.mock import AsyncMock, patch

    from app.search.local import resolve_role_matching_file_ids

    role_ctx = SearchRoleContext(co_occur_roles=("student",), student_context=True)
    with (
        patch(
            "app.search.local.matching_drive_file_ids_for_roles",
            new=AsyncMock(return_value=["face-sql"]),
        ),
        patch(
            "app.search.local.drive_file_ids_with_student_captions",
            new=AsyncMock(return_value=["caption-only"]),
        ),
        patch(
            "app.search.local.drive_file_ids_with_extra_named_people",
            new=AsyncMock(return_value=["named-peer"]),
        ),
    ):
        ids = await resolve_role_matching_file_ids(
            session=None,
            drive_file_ids=["face-sql", "caption-only", "named-peer", "solo"],
            person_names=["Pratham Mittal"],
            role_ctx=role_ctx,
        )
    assert ids == ["face-sql", "caption-only", "named-peer"]


@pytest.mark.asyncio
async def test_students_only_does_not_union_captions_when_sql_hits():
    from unittest.mock import AsyncMock, patch

    from app.search.local import resolve_role_matching_file_ids

    role_ctx = SearchRoleContext(require_all_roles=("student",), student_context=True)
    with (
        patch(
            "app.search.local.matching_drive_file_ids_for_roles",
            new=AsyncMock(return_value=["face-sql"]),
        ),
        patch(
            "app.search.local.drive_file_ids_with_student_captions",
            new=AsyncMock(return_value=["caption-only"]),
        ) as cap,
        patch(
            "app.search.local.drive_file_ids_with_extra_named_people",
            new=AsyncMock(return_value=["named-peer"]),
        ) as named,
    ):
        ids = await resolve_role_matching_file_ids(
            session=None,
            drive_file_ids=["face-sql", "caption-only"],
            person_names=[],
            role_ctx=role_ctx,
        )
    assert ids == ["face-sql"]
    cap.assert_not_called()
    named.assert_not_called()


@pytest.mark.asyncio
async def test_student_evidence_filter_skipped_without_named_person():
    from app.schemas import SearchResultFile
    from app.search.local import filter_files_by_student_evidence

    files = [
        SearchResultFile(
            drive_file_id="a",
            name="a.jpg",
            path="/a.jpg",
            mime_type="image/jpeg",
        )
    ]
    out = await filter_files_by_student_evidence(
        session=None,
        files=files,
        person_names=[],
        role_ctx=SearchRoleContext(student_context=True, require_all_roles=("student",)),
    )
    assert out == files
