"""Watchlist and alert schemas (spec §24.2, §26.2). [P2 behaviour]"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

Channel = Literal["email", "in_app"]
Severity = Literal["info", "warning", "critical"]


class WatchlistItem(BaseModel):
    ticker: str


class Watchlist(BaseModel):
    id: str
    name: str
    items: list[WatchlistItem] = []


class WatchlistCreate(BaseModel):
    # Trimmed, 1-128 characters: the column is VARCHAR(128), so a longer name was a 500.
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class WatchlistItemCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)


class AlertEventType(BaseModel):
    """One entry of the alert event-type registry (FR-AL-010): enough for a client to render the
    rule form without a hard-coded list."""

    key: str
    label: str
    description: str
    params_schema: dict[str, Any]  # JSON Schema of the type's params
    default_enabled: bool = True


class Alert(BaseModel):
    id: str
    event_type: str
    ticker: str | None = None  # None: every company on the user's watchlist
    params: dict[str, Any] = {}
    channels: list[Channel] = ["in_app"]
    enabled: bool = True
    supported: bool = True  # False when the event type is no longer offered (FR-AL-017)
    created_at: str


class AlertCreate(BaseModel):
    event_type: str = Field(max_length=64)
    ticker: str | None = Field(default=None, max_length=16)
    params: dict[str, Any] = {}
    channels: list[Channel] = Field(default=["in_app"], min_length=1)
    enabled: bool = True


class AlertUpdate(BaseModel):
    params: dict[str, Any] | None = None
    channels: list[Channel] | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class Notification(BaseModel):
    id: str
    title: str
    body: str | None = None
    channel: Channel
    company_id: str | None = None
    ticker: str | None = None
    event_type: str | None = None
    severity: Severity | None = None
    # Backend-issued evidence ids: `filing:<id>`, `news:<id>`, `price:<T>:<from>:<to>`, chunk ids.
    source_ids: list[str] = []
    read: bool = False
    created_at: str
