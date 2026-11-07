"""Provider-agnostic typed mail models."""

from typing import Any, TypeVar

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
    # Gmail part id, internal input to download links; never serialized.
    part_id: str = Field(default="", exclude=True)
    # Provider handle for fetching the bytes from this same read (Gmail attachmentId); never serialized.
    fetch_id: str = Field(default="", exclude=True)


class Message(MessageSummary):
    cc: str
    body: str
    attachments: list[Attachment]
    # Characters of hidden HTML text removed while parsing; internal input to
    # the server's suspicion assessment, never serialized.
    hidden_removed: int = Field(default=0, exclude=True)
    # Whether the message sits in spam; internal input to download rules, never serialized.
    spam: bool = Field(default=False, exclude=True)
    # core.billing.BillingResult set by the server's read tools; presented by the server, never serialized.
    billing: Any = Field(default=None, exclude=True)


class AttachmentDownload(_Base):
    message: Message
    attachment: Attachment
    data: bytes


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


def with_account(model: M, alias: str) -> M:  # noqa: UP047 - keep the TypeVar form used across the models
    if not isinstance(model, _Accounted):
        raise TypeError(f"{type(model).__name__} has no account field")
    update: dict = {"account": alias}
    if isinstance(model, Thread):
        update["messages"] = [m.model_copy(update={"account": alias}) for m in model.messages]
    return model.model_copy(update=update)
