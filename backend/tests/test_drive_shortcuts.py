from app.drive.traverse import FOLDER_MIME, SHORTCUT_MIME, plan_child_traversal


def test_real_folder_is_listed_and_traversed():
    plan = plan_child_traversal(
        {"id": "f1", "name": "Photos", "mimeType": FOLDER_MIME},
        follow_shortcuts=True,
    )
    assert plan.include_in_listing is True
    assert plan.traverse_folder_id == "f1"


def test_folder_shortcut_is_listed_and_traversed_when_enabled():
    plan = plan_child_traversal(
        {
            "id": "sc1",
            "name": "UG iPhone Data",
            "mimeType": SHORTCUT_MIME,
            "shortcutDetails": {
                "targetId": "real-folder-id",
                "targetMimeType": FOLDER_MIME,
            },
        },
        follow_shortcuts=True,
    )
    assert plan.include_in_listing is True
    assert plan.traverse_folder_id == "real-folder-id"
    assert plan.path_segment == "UG iPhone Data"


def test_folder_shortcut_ignored_when_disabled():
    plan = plan_child_traversal(
        {
            "id": "sc1",
            "name": "Master Folder",
            "mimeType": SHORTCUT_MIME,
            "shortcutDetails": {
                "targetId": "real-folder-id",
                "targetMimeType": FOLDER_MIME,
            },
        },
        follow_shortcuts=False,
    )
    assert plan.include_in_listing is False
    assert plan.traverse_folder_id is None


def test_file_shortcut_is_never_traversed():
    plan = plan_child_traversal(
        {
            "id": "sc2",
            "name": "linked.jpg",
            "mimeType": SHORTCUT_MIME,
            "shortcutDetails": {
                "targetId": "img-id",
                "targetMimeType": "image/jpeg",
            },
        },
        follow_shortcuts=True,
    )
    assert plan.traverse_folder_id is None
