"""Discriminated outcomes shared by executable chat-turn stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class ChatStageContinue(Generic[T]):
    value: T


@dataclass(frozen=True)
class ChatStageDone:
    """The turn was handled without entering later stages."""

    payload: dict | None = None
    release_busy: bool = False


@dataclass(frozen=True)
class ChatStageFail:
    """A stage failed with a safe explanation for the user."""

    error: str
    user_message: str
    cause: BaseException | None = None


ChatStageResult: TypeAlias = (
    ChatStageContinue[T] | ChatStageDone | ChatStageFail
)
