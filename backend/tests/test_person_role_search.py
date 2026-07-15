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
