"""Provider-agnostic typed mail models."""

from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)


class _Accounted(_Base):
    """Base of every model that reports which account it came from."""

    account: str = ""


class Unsubscribe(_Base):
    """Sender-controlled unsubscribe options; shown to the user, never visited."""

    url: str | None = None
    mailto: str | None = None
    one_click: bool = False


class MessageSummary(_Accounted):
    id: str
    thread_id: str
    from_: str = Field(alias="from")
    to: str
    subject: str
    date: str
    snippet: str
    labels: list[str]
    internal_date: int
    unread: bool
    starred: bool
    is_important: bool | None
    category: str | None
    is_newsletter: bool
    reply_to: str = ""
    unsubscribe: Unsubscribe | None = None
    suspicious: bool = False
    suspicious_reasons: list[str] = []


class Attachment(_Base):
    filename: str
    mime_type: str
    size: int


class Message(MessageSummary):
    cc: str
    body: str
    attachments: list[Attachment]
    # Characters of hidden HTML text removed while parsing; internal input to
    # the server's suspicion assessment, never serialized.
    hidden_removed: int = Field(default=0, exclude=True)


class Thread(_Accounted):
    messages: list[Message]
    replied_by_me: bool


class Label(_Base):
    id: str
    name: str
    type: str


class DraftSummary(MessageSummary):
    draft_id: str


class Draft(_Accounted):
    draft_id: str
    message_id: str
    thread_id: str | None
    # Set when a recipient appears in the thread only as a Reply-To address.
    warning: str | None = None


class LabelState(_Base):
    id: str
    labels: list[str]


M = TypeVar("M", bound=_Accounted)


def with_account(model: M, alias: str) -> M:
    if not isinstance(model, _Accounted):
        raise TypeError(f"{type(model).__name__} has no account field")
    update: dict = {"account": alias}
    if isinstance(model, Thread):
        update["messages"] = [m.model_copy(update={"account": alias}) for m in model.messages]
    return model.model_copy(update=update)
