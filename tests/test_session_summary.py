"""Резюме отрезанного хвоста диалога: доезжает до модели и не стоит лишнего.

Резюме — единственная память о том, что было сказано до обрезки истории (имя
ребёнка, выбранный зал, договорённости). Если оно пишется в базу, но не
подмешивается в контекст, то это одновременно потеря памяти и платный вызов
модели впустую — на КАЖДОМ сообщении длинного диалога.

Базы здесь нет: репозитории подменены, проверяется поведение слоя сессии.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.core import session as conv_session
from app.core.session import SUMMARY_MARKER, load_history, maybe_summarize
from app.storage.models import Conversation
from app.types import LLMResponse


def make_conv(*, summary: str | None = None, msg_in_count: int = 0) -> Conversation:
    """Диалог без базы: слою сессии нужны только эти поля."""
    return Conversation(
        id=uuid4(),
        conv_key="whatsapp:77010000000",
        channel_id="ch",
        chat_type="whatsapp",
        chat_id="77010000000",
        lang="ru",
        state="active",
        summary=summary,
        msg_in_count=msg_in_count,
    )


def turn(text: str, role: str = "user") -> dict[str, Any]:
    """Элемент истории с одним текстом."""
    return {"role": role, "parts": [{"text": text}]}


class CountingLLM:
    """Двойник клиента модели: считает платные вызовы."""

    def __init__(self, text: str = "Родитель спрашивал про расписание.") -> None:
        self.calls = 0
        self._text = text

    async def generate(self, req, executor) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self._text, history=[])


@pytest.fixture
def stub_repos(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Подменяет репозитории и отдаёт управление их поведением."""
    box: dict[str, Any] = {"history": [], "saved": [], "history_calls": 0}

    async def fake_load_history(session, conv_id, *, max_turns: int):
        box["history_calls"] += 1
        return list(box["history"])

    async def fake_set_summary(session, conv_id, summary: str) -> None:
        box["saved"].append(summary)

    monkeypatch.setattr(conv_session.repo_message, "load_history", fake_load_history)
    monkeypatch.setattr(conv_session.repo_conversation, "set_summary", fake_set_summary)
    return box


# --------------------------------------------------------------------------- #
# Резюме в контексте
# --------------------------------------------------------------------------- #
async def test_summary_is_prepended_to_model_history(stub_repos: dict[str, Any]) -> None:
    """Сохранённое резюме обязано доезжать до модели первым элементом истории.

    Без этого обрезанный хвост теряется безвозвратно: на двадцать пятой реплике
    модель уже не помнит имя ребёнка, названное на третьей.
    """
    stub_repos["history"] = [turn("а во сколько занятия?"), turn("В 18:00.", role="model")]
    conv = make_conv(summary="Родитель — Айгуль, сын Алихан 9 лет, зал «Центральный».")

    history = await load_history(None, conv, max_turns=20)

    assert len(history) == 3
    head = history[0]
    assert head["role"] == "user"
    assert SUMMARY_MARKER in head["parts"][0]["text"]
    assert "Алихан" in head["parts"][0]["text"]
    assert history[1:] == stub_repos["history"]


async def test_history_without_summary_is_untouched(stub_repos: dict[str, Any]) -> None:
    """Нет резюме — нет и служебного элемента: короткий диалог ничего не платит."""
    stub_repos["history"] = [turn("привет")]
    conv = make_conv(summary=None)

    assert await load_history(None, conv, max_turns=20) == stub_repos["history"]


# --------------------------------------------------------------------------- #
# Стоимость пересчёта
# --------------------------------------------------------------------------- #
async def test_summary_is_not_recomputed_on_every_message(stub_repos: dict[str, Any]) -> None:
    """Готовое резюме не пересчитывается на каждое входящее.

    Раньше каждое сообщение диалога длиннее ``llm_history_turns`` оплачивало
    лишний полный ``generate()``, результат которого никто не читал.
    """
    stub_repos["history"] = [turn(f"сообщение {i}") for i in range(60)]
    conv = make_conv(summary="уже есть", msg_in_count=23)
    llm = CountingLLM()

    result = await maybe_summarize(None, conv, llm, max_turns=5)

    assert llm.calls == 0
    assert stub_repos["saved"] == []
    assert stub_repos["history_calls"] == 0
    assert result == "уже есть"


async def test_summary_is_refreshed_periodically(stub_repos: dict[str, Any]) -> None:
    """Раз в несколько реплик резюме всё же обновляется — иначе оно устареет."""
    stub_repos["history"] = [turn(f"сообщение {i}") for i in range(60)]
    conv = make_conv(summary="устаревшее", msg_in_count=25)
    llm = CountingLLM()

    result = await maybe_summarize(None, conv, llm, max_turns=5)

    assert llm.calls == 1
    assert stub_repos["saved"] == [result]
    assert result == "Родитель спрашивал про расписание."


async def test_first_summary_is_built_immediately(stub_repos: dict[str, Any]) -> None:
    """Первое резюме строится сразу, как только хвост начал теряться."""
    stub_repos["history"] = [turn(f"сообщение {i}") for i in range(60)]
    conv = make_conv(summary=None, msg_in_count=21)
    llm = CountingLLM()

    result = await maybe_summarize(None, conv, llm, max_turns=5)

    assert llm.calls == 1
    assert result == "Родитель спрашивал про расписание."
