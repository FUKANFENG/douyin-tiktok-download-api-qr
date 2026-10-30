import json
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from cookie_service.topics import Topics, candidates, verify_submission


def selection():
    return Topics(topics=[{"id": "123", "name": "美食", "source": "search"}]).topics


def payload():
    # Emoji occupies two UTF-16 code units, matching the native JS editor.
    return {
        "text": "标题 😀 #美食",
        "text_extra": json.dumps(
            [{"start": 6, "end": 9, "type": 1, "hashtag_id": "123", "hashtag_name": "美食"}]
        ),
        "challenges": '["123"]',
    }


def test_native_topic_data_is_checked_for_both_form_and_json_submissions():
    verify_submission(urlencode(payload()), selection())
    verify_submission(json.dumps({"common": payload()}), selection())


@pytest.mark.parametrize(
    "change",
    [
        {"hashtag_id": "999"},
        {"hashtag_name": "美景"},
        {"start": 5},
        {"end": 8},
    ],
)
def test_wrong_id_name_or_position_is_rejected(change):
    body = payload()
    item = json.loads(body["text_extra"])[0]
    item.update(change)
    body["text_extra"] = json.dumps([item])
    with pytest.raises(ValueError):
        verify_submission(urlencode(body), selection())


def test_plain_hash_text_is_not_accepted_as_an_existing_topic_binding():
    with pytest.raises(ValueError):
        verify_submission(
            json.dumps({"common": {"text": "#美食", "text_extra": [], "challenges": []}}), selection()
        )


@pytest.mark.parametrize(
    "items",
    [
        [{"name": "美食", "source": "search"}],
        [{"id": "123", "name": "#美食"}],
        [{"id": "123", "name": "美 食"}],
        [{"id": "123", "name": "美食"}] * 2,
        [{"name": str(i), "source": "new"} for i in range(6)],
    ],
)
def test_selections_require_valid_explicit_and_unique_topics(items):
    with pytest.raises(ValidationError):
        Topics(topics=items)


def test_platform_candidates_are_bounded_and_do_not_expose_extra_fields():
    data = {
        "sug_list": [
            {"cid": "123", "cha_name": "美食", "view_count": 1234, "secret": "ignored"},
            {"cid": "bad", "cha_name": "bad"},
            {"cid": "123", "cha_name": "duplicate"},
        ]
    }
    assert candidates(data, "search") == [{"id": "123", "name": "美食", "source": "search", "views": 1234}]


def test_explicit_custom_topic_accepts_the_native_zero_id_marker():
    chosen = Topics(topics=[{"name": "自定义", "source": "new"}]).topics
    body = {
        "text": "#自定义",
        "text_extra": [{"start": 0, "end": 4, "type": 1, "hashtag_id": 0, "hashtag_name": "自定义"}],
        "challenges": [],
    }
    verify_submission(json.dumps({"common": body}), chosen)
