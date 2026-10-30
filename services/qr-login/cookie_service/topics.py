"""Public topic data and the final native publishing request boundary."""

import json
import re
from typing import Literal
from urllib.parse import parse_qs

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TOPICS = 5


class Topic(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    name: str = Field(min_length=1, max_length=100)
    source: Literal["search", "recommend", "new"] = "search"

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if re.search(r"[\s#@\x00-\x1f\x7f]", value):
            raise ValueError("Use a topic name without separators")
        return value

    @model_validator(mode="after")
    def valid_identity(self):
        if self.source == "new":
            if self.id is not None:
                raise ValueError("A custom topic has no selected platform ID")
        elif self.id is None or not self.id.strip("0"):
            raise ValueError("Select an existing platform topic")
        return self


class Topics(BaseModel):
    topics: list[Topic] = Field(default_factory=list, max_length=MAX_TOPICS)

    @field_validator("topics")
    @classmethod
    def unique_topics(cls, values):
        names = {v.name.casefold() for v in values}
        ids = [v.id for v in values if v.id is not None]
        if len(names) != len(values) or len(set(ids)) != len(ids):
            raise ValueError("Select each topic once")
        return values


def candidates(data, source):
    key = "sug_list" if source == "search" else "challenge_list"
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError("Invalid topic response")
    result, seen = [], set()
    for item in data[key][:100]:
        if not isinstance(item, dict):
            continue
        try:
            topic = Topic(id=str(item.get("cid", "")), name=item.get("cha_name"), source=source)
        except ValueError:
            continue
        if topic.id in seen:
            continue
        seen.add(topic.id)
        views = item.get("view_count")
        views = views if type(views) is int and 0 <= views <= 10**18 else None
        result.append({**topic.model_dump(), "views": views})
        if len(result) == 20:
            break
    return result


def verify_submission(body, topics):
    """Reject plain text, forged IDs and incorrect UTF-16 spans before network egress."""
    try:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {key: values[0] for key, values in parse_qs(body).items() if len(values) == 1}
        data = payload.get("common", payload)
        text = data["text"].encode("utf-16-le")
        extras = data["text_extra"]
        extras = json.loads(extras) if isinstance(extras, str) else extras
        challenges = data.get("challenges", [])
        challenges = json.loads(challenges) if isinstance(challenges, str) else challenges
        if not isinstance(extras, list) or not isinstance(challenges, list):
            raise ValueError("Invalid topic metadata")
        challenge_ids = {str(value) for value in challenges}
        actual = [item for item in extras if item.get("type") == 1]
        if len(actual) != len(topics):
            raise ValueError("Unexpected topic count")
        for topic, item in zip(topics, actual, strict=True):
            start, end = item["start"], item["end"]
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError("Invalid topic position")
            identity = str(item["hashtag_id"])
            if (
                item["hashtag_name"] != topic.name
                or text[start * 2 : end * 2].decode("utf-16-le") != "#" + topic.name
                or (topic.id is not None and identity != topic.id)
                or not re.fullmatch(r"[0-9]{1,30}", identity)
                or (identity.strip("0") and identity not in challenge_ids)
            ):
                raise ValueError("Topic binding differs from the confirmed selection")
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        raise ValueError("Native topic binding could not be verified") from error
