"""«Да» на предложение бота — это согласие на конкретный текст, а не пустое слово."""

from __future__ import annotations

import pytest

from app.kb.agreement import is_bare_agreement, question_sentence, split_agreement, with_agreement

WORDS = ("да", "давайте", "хорошо", "ок", "конечно", "иә", "жарайды", "подходит")
PROPOSAL = (
    "Занятия по боксу проходят по понедельникам, средам и пятницам в 19:00. "
    "Записать Айназарова Али на ближайшее занятие в понедельник?"
)


@pytest.mark.parametrize("text", ["Да", "да", "Да, давайте", "ОК", "Иә", "Хорошо, подходит!", "Да, в 19", "Давайте в среду"])
def test_bare_agreement(text: str) -> None:
    assert is_bare_agreement(text, WORDS)


@pytest.mark.parametrize("text", ["Нет", "Да, но в среду", "Айназаров Али", "Бокс", "", "да да да да да да да", "Нет, в 19", "в 19", "Ок, спасибо"])
def test_not_a_bare_agreement(text: str) -> None:
    assert not is_bare_agreement(text, WORDS)


def test_agreement_carries_the_proposal() -> None:
    expanded = with_agreement("Да", PROPOSAL, WORDS)

    own, proposal = split_agreement(expanded)
    assert own == "Да"
    assert proposal == PROPOSAL
    assert question_sentence(proposal) == "Записать Айназарова Али на ближайшее занятие в понедельник?"


def test_agreement_to_a_statement_or_a_real_answer_is_left_alone() -> None:
    assert with_agreement("Да", "Передаю ваш вопрос администратору.", WORDS) == "Да"
    assert with_agreement("Бокс", PROPOSAL, WORDS) == "Бокс"
    assert with_agreement("Да", None, WORDS) == "Да"


def test_gym_title_quotes_do_not_break_the_marker() -> None:
    """В предложении бывают «ёлочки» из названия зала — пометка разбирается целиком."""
    text = with_agreement("Да", "Записать в зал «Рахат» на понедельник в 19:00?", WORDS)

    assert split_agreement(text)[1] == "Записать в зал «Рахат» на понедельник в 19:00?"


def test_already_expanded_text_is_not_expanded_twice() -> None:
    once = with_agreement("Да", PROPOSAL, WORDS)

    assert with_agreement(once, PROPOSAL, WORDS) == once
