"""Скриншот владельца 14.10.2026: «снова предлагает найти зал — после того, как клиент выбрал».

Клиент выбрал Тобыл → «Записать ребёнка на первую пробную?» → «Да, конечно. Давайте
может с понедельника 19 числа» → «Как зовут ребёнка полностью, фамилия и имя?» →
«Скажите как одеться и что с собой нужно взять на тренировку» → текст владельца «что
взять» и следом «В каком районе вы живёте? Подберу ближайший к вам зал.»

Следующий шаг записи считался по одному последнему сообщению — в нём нет ни зала, ни
имени. Номера в тестах вымышленные.
"""

from __future__ import annotations

import pytest

from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.llm.client import FakeCall, FakeLLMClient, FakeTurn
from app.types import Language

from tests.conftest import RecordingQueue, webhook_payload

TOBYL = "region_tobyl"
NAME_QUESTION = "Как зовут ребёнка полностью, фамилия и имя?"


def _deps(kb, state, sessionmaker, settings) -> PipelineDeps:
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker, state=state, kb=kb_loader.get_snapshot, queue=RecordingQueue(),
        settings=settings, llm=FakeLLMClient([]),
    )


async def _say(deps: PipelineDeps, chat: str, number: int, text: str, *turns) -> str:
    deps.llm.reset(list(turns))
    decisions = await process_inbound(deps, webhook_payload(f"{chat}-{number}", text, chat_id=chat))
    return "\n".join(out.text or "" for d in decisions for out in d.outbound)


async def _tobyl_and_the_name_question(deps: PipelineDeps, chat: str) -> None:
    await _say(deps, chat, 1, "Какое расписание в Тобыле?",
               FakeTurn.tool(FakeCall("get_schedule", {"gym_id": TOBYL})), FakeTurn.answer(""))
    await _say(deps, chat, 2, "Да, конечно. Давайте может с понедельника 19 числа",
               FakeTurn.answer(NAME_QUESTION))


async def test_side_question_returns_to_the_name_not_to_the_district(kb, state, sessionmaker, settings) -> None:
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551410"
    await _tobyl_and_the_name_question(deps, chat)

    reply = await _say(
        deps, chat, 3, "Скажите как одеться и что с собой нужно взять на тренировку",
        FakeTurn.tool(FakeCall("get_kb_fact", {"topic": "gear", "question": "что взять с собой"})),
        FakeTurn.answer("Возьмите удобную одежду и воду."),
    )

    assert "С собой необходимо взять" in reply, reply
    assert kb.text("funnel.district", Language.RU) not in reply, reply
    assert reply.rstrip().endswith(NAME_QUESTION), reply


async def test_without_a_pending_question_the_gym_in_view_counts(kb, state, sessionmaker, settings) -> None:
    """Вопроса записи не было, но клиент смотрит на Тобыл — район не спрашивается."""
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551411"
    await _say(deps, chat, 1, "Какое расписание в Тобыле? Сыну 8 лет",
               FakeTurn.tool(FakeCall("get_schedule", {"gym_id": TOBYL})), FakeTurn.answer(""))

    reply = await _say(deps, chat, 2, "Понятно", FakeTurn.answer("Занятия проходят вечером."))

    assert kb.text("funnel.district", Language.RU) not in reply, reply
    assert reply.rstrip().endswith(kb.text("funnel.name", Language.RU)), "возраст и зал известны — дальше имя"


async def test_an_answer_to_the_question_is_not_followed_by_the_same_question(kb, state, sessionmaker, settings) -> None:
    """Клиент ответил на вопрос записи — повторять его нельзя."""
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551412"
    await _tobyl_and_the_name_question(deps, chat)

    reply = await _say(deps, chat, 3, "Курманова Айша", FakeTurn.answer("Спасибо!"))

    assert NAME_QUESTION not in reply, reply


@pytest.mark.parametrize(
    "handover",
    [
        "Что именно взять на тренировку, уточнит администратор. Передать ваш вопрос администратору?",
        "Это уточнит администратор.",
    ],
)
async def test_owner_text_goes_out_even_if_the_model_offers_a_handover(
    kb, state, sessionmaker, settings, handover
) -> None:
    """Живой прогон того же диалога: модель не позвала базу и написала «уточнит администратор»."""
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551413"
    await _tobyl_and_the_name_question(deps, chat)
    deps.llm.reset([FakeTurn.answer(handover)])

    decisions = await process_inbound(deps, webhook_payload(
        f"{chat}-3", "Скажите как одеться и что с собой нужно взять на тренировку", chat_id=chat
    ))

    reply = "\n".join(out.text or "" for d in decisions for out in d.outbound)
    assert "С собой необходимо взять" in reply, reply
    assert "администратор" not in reply.lower(), reply
    assert reply.rstrip().endswith(NAME_QUESTION), reply
    assert "get_kb_fact" in [inv.name for d in decisions for inv in d.invocations], "вызов виден в журнале хода"


@pytest.mark.parametrize(
    "said",
    [
        "Скажите как одеться и что с собой нужно взять на тренировку",
        "Что с собой нужно взять?",
        "Как одеться на тренировку?",
        "во что одеться?",
        "что брать на тренировку",
    ],
)
def test_the_clients_words_are_a_gear_question(kb, said) -> None:
    from app.core import lexicon
    from app.types import IntentHint

    assert IntentHint.GEAR in lexicon.intent_hints(said, lexicon=kb.lexicon), said


async def test_age_after_naming_tobyl_does_not_ask_the_district(kb, state, sessionmaker, settings) -> None:
    """Живой прогон 14.10.2026: «мы из Тобыла» → «Сыну 8 лет» → «В каком районе вы живёте?»."""
    deps = _deps(kb, state, sessionmaker, settings)
    chat = "77015551414"
    await _say(deps, chat, 1, "Здравствуйте, мы из Тобыла, где у вас там зал?",
               FakeTurn.answer("Зал в Тобыле на улице Тәуелсіздік. Подберу подходящую группу — сколько лет ребёнку?"))

    reply = await _say(deps, chat, 2, "8", FakeTurn.answer("Записать сына на пробное в Тобыле?"))

    assert reply != kb.text("funnel.district", Language.RU), "место клиент уже назвал"
