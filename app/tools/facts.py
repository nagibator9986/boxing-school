"""Факты о школе — инструмент ``get_kb_fact``.

Ищет готовый ответ в ``kb/faq.yaml`` и в организационном слое ``kb/policies.yaml``
по теме и географии. Ответ отдаётся **как есть**, на обоих языках: тексты писал
владелец школы, и переписывать их модели незачем (``render_hint=VERBATIM``).

Главное правило здесь — отрицательное. Если по теме ответа нет, инструмент
обязан вернуть честный ``no_data`` с фразой-заглушкой из ``app/kb/gaps.py``,
а не «похожий» ответ из соседней темы. «Справка, наверное, обычная от
педиатра» — это выдумка, из-за которой родитель зря съездит в поликлинику;
``escalate_if_empty`` в записи FAQ превращает такой случай в передачу
администратору.
"""

from __future__ import annotations

from typing import Any, Final

from app.kb.gaps import gap_for_topic, say_no_data
from app.kb.models import FAQ_TOPICS, Bilingual, FaqEntry, KBSnapshot
from app.types import (
    EscalationReason,
    GapRef,
    Language,
    RenderHint,
    Scope,
    ToolContext,
    ToolResult,
)

#: Темы, по которым организационный слой ``policies.yaml`` может дать факт,
#: даже если запись FAQ пуста. Расширять только вместе с KB-SPEC.
_POLICY_TOPICS: Final[tuple[str, ...]] = ("contacts",)


def _scope_of(value: str | None) -> Scope | None:
    """Строка -> :class:`Scope` (``city`` / ``region`` / ``any``)."""
    if value is None:
        return Scope.ANY
    try:
        scope = Scope(str(value).strip().lower())
    except ValueError:
        return None
    if scope in (Scope.CITY, Scope.REGION, Scope.ANY):
        return scope
    if scope is Scope.ALL:
        return Scope.ANY
    return None


def _policy_fact(kb: KBSnapshot, topic: str) -> Bilingual | None:
    """Факт из ``policies.yaml``, если он там есть.

    Сегодня организационный слой закрывает единственную тему — часы работы
    администратора для темы ``contacts``.
    """
    if topic == "contacts":
        hours = kb.policies.work_hours
        if hours is not None and hours.filled:
            return hours
    return None


def _entry_payload(entry: FaqEntry, lang: Language) -> dict[str, Any]:
    """Запись FAQ в форме, которую ждёт контракт инструмента."""
    return {
        "id": entry.id,
        "topic": entry.topic,
        "scope": entry.scope.value,
        "answer": entry.answer.get(lang) or entry.answer.ru,
        "answer_ru": entry.answer.ru,
        "answer_kk": entry.answer.kk,
        "source": entry.source.value,
        "gap_ref": entry.gap_ref.value if entry.gap_ref is not None else None,
        "requires_tool": entry.requires_tool,
        "forbidden_claims": list(entry.forbidden_claims),
    }


def _gap_of(entry: FaqEntry | None, topic: str) -> GapRef | None:
    """Пробел, стоящий за пустой темой: сначала из записи, потом из реестра."""
    if entry is not None and entry.gap_ref is not None:
        return entry.gap_ref
    return gap_for_topic(topic)


async def get_kb_fact(ctx: ToolContext, *, topic: str, scope: str = "any") -> ToolResult:
    """data: {answer_ru, answer_kk, source, gap_ref}. Пустой answer -> no_data + фраза-заглушка,
    при escalate_if_empty=True -> needs_operator. render_hint=VERBATIM."""
    kb = ctx.kb
    lang = ctx.lang

    topic_key = str(topic).strip().lower() if topic else ""
    if topic_key not in FAQ_TOPICS:
        return ToolResult.invalid_input(f"неизвестная тема '{topic}'")

    parsed_scope = _scope_of(scope)
    if parsed_scope is None:
        return ToolResult.invalid_input(f"неизвестный scope '{scope}'")

    entries = kb.faq_entries(topic_key, parsed_scope)
    answered = [entry for entry in entries if entry.answered]
    entry = answered[0] if answered else (entries[0] if entries else None)

    # --- готовый ответ владельца ------------------------------------------- #
    if entry is not None and entry.answered:
        data = _entry_payload(entry, lang)
        data["also"] = [_entry_payload(other, lang) for other in answered[1:3]]
        policy = _policy_fact(kb, topic_key)
        if policy is not None:
            data["work_hours_ru"] = policy.ru
            data["work_hours_kk"] = policy.kk
        caveats: list[str] = []
        if entry.forbidden_claims:
            caveats.append(
                "Ни при каких условиях не утверждай следующее: " + "; ".join(entry.forbidden_claims)
            )
        if entry.requires_tool:
            caveats.append(
                f"Цифры по этой теме бери только из инструмента {entry.requires_tool}, не из текста ответа."
            )
        return ToolResult.success(
            data=data,
            render_hint=RenderHint.VERBATIM,
            caveats=caveats,
            meta={"faq_id": entry.id, "scope": parsed_scope.value},
        )

    # --- ответа нет: факт из организационного слоя -------------------------- #
    policy = _policy_fact(kb, topic_key)
    if policy is not None:
        return ToolResult.success(
            data={
                "id": f"policies_{topic_key}",
                "topic": topic_key,
                "scope": parsed_scope.value,
                "answer": policy.get(lang) or policy.ru,
                "answer_ru": policy.ru,
                "answer_kk": policy.kk,
                "source": "policies",
                "gap_ref": None,
                "requires_tool": None,
                "forbidden_claims": [],
            },
            render_hint=RenderHint.VERBATIM,
            caveats=["Это часы работы администратора, а не расписание тренировок — не путай."],
            meta={"from": "policies.yaml"},
        )

    # --- ответа нет вообще: честная заглушка -------------------------------- #
    gap = _gap_of(entry, topic_key)
    say = say_no_data(kb, gap) if gap is not None else kb.bilingual_text("gap.generic")
    payload = {
        "status": "no_data",
        "topic": topic_key,
        "scope": parsed_scope.value,
        "answer_ru": None,
        "answer_kk": None,
        "source": None,
        "gap_ref": gap.value if gap is not None else None,
        "faq_id": entry.id if entry is not None else None,
    }
    if entry is not None and entry.escalate_if_empty:
        return ToolResult.needs_operator(
            say=say,
            reason=EscalationReason.NO_DATA,
            gap_ref=gap,
        )
    return ToolResult.no_data(say=say, gap_ref=gap, data=payload)


__all__ = ["get_kb_fact"]
