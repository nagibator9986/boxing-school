"""Ответ из базы знаний, когда модель недоступна.

Проверяется главное свойство :mod:`app.core.degraded`: карточка собирается из
снимка KB и ничего не добавляет от себя, а на темах, где отвечать обязан
человек, функция молчит и уступает место честной заглушке.
"""

from __future__ import annotations

import pytest

from app.core import degraded, lexicon
from app.core.pipeline import PipelineDeps, process_inbound
from app.kb import loader as kb_loader
from app.kb.models import KBSnapshot
from app.types import DecisionAction, IntentHint, Language, LLMQuotaError, Scope

from tests.conftest import RecordingQueue, webhook_payload

CHAT_ID = "77015550909"


class DeadLLM:
    """Модель, которой нет: ровно та ошибка, что пришла с боевого ключа."""

    _ERROR = "квота Gemini исчерпана: Your prepayment credits are depleted."

    async def generate(self, req, executor):  # type: ignore[no-untyped-def]
        raise LLMQuotaError(self._ERROR)

    async def extract_lead(self, rendered, *, lang):  # type: ignore[no-untyped-def]
        # Ключ один на обе задачи: разбор переписки на лид падает так же.
        raise LLMQuotaError(self._ERROR)


@pytest.fixture
async def dead_deps(kb, state, sessionmaker, settings) -> PipelineDeps:
    """Полный пайплайн на sqlite в памяти, но с мёртвой моделью."""
    kb_loader.swap(kb)
    return PipelineDeps(
        sessionmaker=sessionmaker,
        state=state,
        llm=DeadLLM(),
        kb=kb_loader.get_snapshot,
        queue=RecordingQueue(),
        settings=settings,
    )


# --------------------------------------------------------------------------- #
# Что база знаний отвечает сама
# --------------------------------------------------------------------------- #
def test_price_answer_carries_both_scopes(kb: KBSnapshot) -> None:
    """Город и районные центры в одном ответе.

    Какой тариф нужен клиенту, обычно выясняет модель вопросом про район — а её
    в этот момент нет. Городская цена в ответ жителю райцентра ошибается в два с
    половиной раза, и это уже не деградация, а дезинформация.
    """
    answer = degraded.kb_answer(kb, intents=(IntentHint.PRICE,), lang=Language.RU)

    assert answer is not None
    city = kb.text("card.price_city_title", Language.RU)
    region = kb.text("card.price_region_title", Language.RU)
    assert city in answer
    assert region in answer


def test_schedule_answer_is_a_statement_not_a_question(kb: KBSnapshot) -> None:
    """После деградации бот встаёт на паузу — спрашивать он права не имеет.

    Поэтому на «когда занятия» уходит расписание всех городских залов, а не
    просьба назвать номер зала: ответ на неё уже некому было бы прочитать.
    """
    answer = degraded.kb_answer(kb, intents=(IntentHint.SCHEDULE,), lang=Language.RU)

    assert answer is not None
    assert kb.text("card.pick_gym", Language.RU) not in answer
    filled = [gym for gym in kb.active_gyms(Scope.CITY) if gym.schedule]
    assert filled, "фикстура без расписания не проверяет ничего"
    for gym in filled:
        assert (gym.title.get(Language.RU) or gym.title.ru) in answer


def test_location_answer_lists_gyms(kb: KBSnapshot) -> None:
    """«Где вы» — тот же список залов, что и в рабочем ходе."""
    answer = degraded.kb_answer(kb, intents=(IntentHint.LOCATION,), lang=Language.RU)

    assert answer is not None
    assert kb.text("card.gyms_city_title", Language.RU) in answer


def test_location_answer_promises_nothing(kb: KBSnapshot) -> None:
    """«Напишите номер зала — пришлю расписание» здесь выполнить некому.

    Живой прогон 01.09.2026 на пустом ключе: карточка залов дошла до клиента
    вместе со своим приглашением к следующему шагу, а бот в ту же секунду встал
    на паузу. Обещание, которого он не выполнит, хуже, чем его отсутствие.
    """
    for lang in (Language.RU, Language.KK):
        answer = degraded.kb_answer(kb, intents=(IntentHint.LOCATION,), lang=lang)
        assert answer is not None
        assert kb.text("card.pick_gym", lang) not in answer
        assert answer.strip() == answer, "хвост карточки остался пустой строкой"


def test_price_wins_over_schedule(kb: KBSnapshot) -> None:
    """В «сколько стоит и когда занятия» человек первым делом ждёт цену."""
    answer = degraded.kb_answer(
        kb, intents=(IntentHint.SCHEDULE, IntentHint.PRICE), lang=Language.RU
    )

    assert answer is not None
    assert kb.text("card.price_city_title", Language.RU) in answer
    assert kb.text("card.schedule_title", Language.RU) not in answer


def test_kazakh_answer_is_kazakh(kb: KBSnapshot) -> None:
    """Язык диалога соблюдается и в аварии."""
    answer = degraded.kb_answer(kb, intents=(IntentHint.PRICE,), lang=Language.KK)

    assert answer is not None
    assert kb.text("card.price_city_title", Language.KK) in answer


# --------------------------------------------------------------------------- #
# Где карточка неуместна
# --------------------------------------------------------------------------- #
def test_no_answer_without_intent(kb: KBSnapshot) -> None:
    """Интент не распознан — наверху остаётся честная заглушка."""
    assert degraded.kb_answer(kb, intents=(), lang=Language.RU) is None
    assert degraded.kb_answer(kb, intents=(IntentHint.OTHER,), lang=Language.RU) is None


def test_sensitive_intents_suppress_the_card(kb: KBSnapshot) -> None:
    """Прайс в ответ на «ребёнок получил травму» хуже молчания.

    Просьба удалить данные, жалоба на безопасность, «отпишите меня» и прямой
    запрос человека обязаны попасть к администратору без карточки — даже если
    рядом в сообщении есть слово «сколько».
    """
    for intent in (IntentHint.STOP, IntentHint.ERASE, IntentHint.SAFETY, IntentHint.MANAGER):
        assert (
            degraded.kb_answer(kb, intents=(IntentHint.PRICE, intent), lang=Language.RU) is None
        ), intent.value


def test_tail_key_exists_in_kb(kb: KBSnapshot) -> None:
    """Хвост «администратор уже подключается» обязан быть в i18n на двух языках."""
    for lang in (Language.RU, Language.KK):
        assert kb.text(degraded.TAIL_KEY, lang).strip()


# --------------------------------------------------------------------------- #
# Связка с лексиконом
# --------------------------------------------------------------------------- #
def test_real_client_phrases_reach_the_card(kb: KBSnapshot) -> None:
    """Живые формулировки родителей проходят путь «текст → интент → карточка»."""
    phrases = {
        "сколько стоит абонемент": "card.price_city_title",
        "где вы находитесь": "card.gyms_city_title",
        "какое расписание": "card.schedule_title",
    }
    for phrase, key in phrases.items():
        intents = lexicon.intent_hints(phrase, lexicon=kb.lexicon)
        answer = degraded.kb_answer(kb, intents=intents, lang=Language.RU)
        assert answer is not None, phrase
        assert kb.text(key, Language.RU) in answer, phrase


# --------------------------------------------------------------------------- #
# Сквозь весь пайплайн с мёртвой моделью
# --------------------------------------------------------------------------- #
async def test_dead_model_still_answers_the_price_question(dead_deps, kb) -> None:
    """Кредиты на ключе кончились — а цена всё равно уходит клиенту.

    До этой правки на любой вопрос уходило «Секунду, у меня сбой на стороне
    сервиса»: 01.09.2026 это увидели почти все написавшие. Цену, адреса и
    расписание собирает код, и молчать о них из-за неоплаченного ключа незачем.
    """
    decisions = await process_inbound(
        dead_deps, webhook_payload("wz-dead-1", "Сколько стоит абонемент?", chat_id=CHAT_ID)
    )

    texts = [out.text for d in decisions for out in d.outbound]
    joined = "\n".join(texts)
    assert kb.text("card.price_city_title", Language.RU) in joined
    assert kb.text("error.generic", Language.RU) not in joined
    assert kb.text(degraded.TAIL_KEY, Language.RU) in joined


async def test_dead_model_still_escalates_and_pauses(dead_deps) -> None:
    """Карточка администратору и пауза остаются: карточка из KB — не полный ответ."""
    decisions = await process_inbound(
        dead_deps, webhook_payload("wz-dead-2", "Где вы находитесь?", chat_id=CHAT_ID)
    )

    assert [d.action for d in decisions] == [DecisionAction.ESCALATE]
    assert any("kb_card" in (d.reason or "") for d in decisions)
    assert all(
        d.escalation_reason is not None for d in decisions
    ), "администратор обязан узнать о сбое"


async def test_dead_model_keeps_the_honest_stub_when_kb_has_no_answer(dead_deps, kb) -> None:
    """Вопрос не из тех, что закрывает база знаний, — честная заглушка на месте."""
    decisions = await process_inbound(
        dead_deps,
        webhook_payload("wz-dead-3", "А тренер — мастер спорта?", chat_id=CHAT_ID),
    )

    joined = "\n".join(out.text for d in decisions for out in d.outbound)
    assert kb.text("error.generic", Language.RU) in joined


async def test_owner_is_told_what_to_pay_for(dead_deps, monkeypatch) -> None:
    """Тревога называет причину, а не «пайплайн упал».

    Владелец узнал о пустом счёте только потому, что спросил вручную: карточки
    эскалации говорят «нужен живой ответ» и молчат о том, что чинить. Повтор
    подавляется на 15 минут, поэтому поток клиентов не превращается в поток
    одинаковых тревог.
    """
    from app.notify import manager as manager_mod

    raised: list[tuple[str, str]] = []

    async def spy(deps, text, *, code):  # type: ignore[no-untyped-def]
        raised.append((code, text))

    monkeypatch.setattr(manager_mod, "notify_alert", spy)

    await process_inbound(
        dead_deps, webhook_payload("wz-dead-4", "Сколько стоит?", chat_id=CHAT_ID)
    )

    assert [code for code, _ in raised] == ["llm_quota"]
    assert "кредит" in raised[0][1].lower()
    assert "ai.studio" in raised[0][1]
