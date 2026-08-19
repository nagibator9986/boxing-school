"""Архитектурный тест: правило зависимостей из ``docs/INTERFACES.md`` §1.1.

```
app.types          ← не импортирует ничего из app.*
app.config         ← только app.types
app.kb             ← app.types, app.config
app.storage        ← app.types, app.config
app.channels       ← app.types, app.config              (НЕ знает про Gemini, KB и core)
app.llm            ← app.types, app.config              (НЕ знает про Wazzup, KB и tools)
app.tools          ← app.types, app.config, app.kb, app.storage   (НЕ знает про LLM и core)
app.core           ← всё вышеперечисленное
```

Правило не косметическое. Пока ``google.genai`` живёт только в ``app.llm``,
миграция на другого поставщика модели — это замена одного пакета за интерфейсом
``LLMClient``. Пока ``app.tools`` не знает про LLM, детерминированные инструменты
можно вызывать и тестировать без модели вообще. Одна «удобная» ссылка ломает
и то и другое, и обнаруживается это через полгода при попытке что-то поменять.

Проверка статическая — по дереву разбора, а не по импорту модулей: импорт
скрывает ``if TYPE_CHECKING`` и ленивые импорты внутри функций, а именно они и
нужны как разрешённое исключение.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"


def modules_of(package: str) -> list[Path]:
    """Все ``.py`` пакета ``app.<package>`` (или сам модуль ``app/<package>.py``)."""
    directory = APP / package
    if directory.is_dir():
        return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)
    single = APP / f"{package}.py"
    return [single] if single.is_file() else []


def runtime_imports(path: Path) -> list[tuple[str, int]]:
    """Модули, импортируемые НА УРОВНЕ МОДУЛЯ, вне ``if TYPE_CHECKING``.

    Ленивые импорты внутри функций и импорты только для аннотаций разрешены:
    они не создают зависимости во время выполнения.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []

    for node in tree.body:
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Import):
                found.extend((alias.name, child.lineno) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                found.append((child.module, child.lineno))

    # Импорты внутри функций отбрасываем отдельно: ast.walk их всё равно достанет.
    lazy = {
        (sub.module or sub.names[0].name, sub.lineno)
        for func in ast.walk(tree)
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
        for sub in ast.walk(func)
        if isinstance(sub, (ast.Import, ast.ImportFrom))
    }
    lazy_lines = {line for _, line in lazy}
    return [(name, line) for name, line in found if line not in lazy_lines]


def _is_type_checking(test: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` в любой из двух распространённых записей."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def package_of(module: str) -> str:
    """``app.tools.booking`` → ``tools``; не из ``app`` → пустая строка."""
    if module == "app" or not module.startswith("app."):
        return ""
    return module.split(".")[1]


# --------------------------------------------------------------------------- #
# SDK модели
# --------------------------------------------------------------------------- #
def test_google_genai_lives_only_in_llm_layer() -> None:
    """``google.genai`` — только в ``app/llm/*``.

    Это и есть цена возможности сменить поставщика модели, не переписывая бота.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts or path.parent.name == "llm":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "google" or name.startswith("google."):
                    offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}: {name}")

    assert not offenders, "google.genai вне app/llm:\n" + "\n".join(offenders)


# --------------------------------------------------------------------------- #
# Правило зависимостей
# --------------------------------------------------------------------------- #
#: Что каждому пакету разрешено импортировать из ``app.*`` во время выполнения.
ALLOWED: dict[str, set[str]] = {
    "types": set(),
    "config": {"types"},
    "logging_conf": {"types", "config"},
    "kb": {"types", "config"},
    "storage": {"types", "config", "logging_conf"},
    "channels": {"types", "config", "logging_conf"},
    "llm": {"types", "config", "logging_conf", "llm", "observability"},
    "tools": {"types", "config", "logging_conf", "kb", "storage", "tools"},
    "notify": {"types", "config", "logging_conf", "kb", "storage", "tools", "notify"},
}


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_dependencies(package: str) -> None:
    """Каждый пакет импортирует только то, что ему разрешено контрактом."""
    allowed = ALLOWED[package] | {package}
    offenders: list[str] = []

    for path in modules_of(package):
        for module, line in runtime_imports(path):
            imported = package_of(module)
            if imported and imported not in allowed:
                offenders.append(f"{path.relative_to(APP.parent)}:{line}: {module}")

    assert not offenders, f"app.{package} нарушает §1.1:\n" + "\n".join(offenders)


def test_llm_does_not_import_tools() -> None:
    """``app.llm`` не знает про ``app.tools``.

    Схемы инструментов приходят в LLM-слой как ``Sequence[ToolSpec]`` (тип из
    ``app.types``), исполнение — как объект протокола ``ToolExecutor``. Собирает
    и то и другое ``app.core.pipeline``.
    """
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}: {module}"
        for path in modules_of("llm")
        for module, line in runtime_imports(path)
        if package_of(module) == "tools"
    ]

    assert not offenders, "app.llm тянет app.tools:\n" + "\n".join(offenders)


def test_tools_do_not_import_llm() -> None:
    """``app.tools`` не знает про LLM: инструменты детерминированы и без модели."""
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}: {module}"
        for path in modules_of("tools")
        for module, line in runtime_imports(path)
        if package_of(module) == "llm"
    ]

    assert not offenders, "app.tools тянет app.llm:\n" + "\n".join(offenders)


def test_tools_and_llm_do_not_import_core() -> None:
    """Ни инструменты, ни LLM не знают про ``app.core`` — иначе получим цикл."""
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}: {module}"
        for package in ("tools", "llm", "channels", "kb", "storage")
        for path in modules_of(package)
        for module, line in runtime_imports(path)
        if package_of(module) == "core"
    ]

    assert not offenders, "нижний слой тянет app.core:\n" + "\n".join(offenders)


def test_channels_do_not_know_about_gemini_or_kb() -> None:
    """Слой каналов не знает ни про модель, ни про базу знаний, ни про метрики."""
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}: {module}"
        for path in modules_of("channels")
        for module, line in runtime_imports(path)
        if package_of(module) in {"llm", "kb", "core", "observability"}
    ]

    assert not offenders, "app.channels вышел за свой слой:\n" + "\n".join(offenders)


def test_types_module_is_dependency_free() -> None:
    """``app.types`` не импортирует ничего из ``app.*`` — он корень графа."""
    offenders = [
        f"app/types.py:{line}: {module}"
        for module, line in runtime_imports(APP / "types.py")
        if package_of(module)
    ]

    assert not offenders, "app.types обзавёлся зависимостями:\n" + "\n".join(offenders)


def test_config_reads_environment_alone() -> None:
    """``os.environ`` где-либо, кроме ``app/config.py``, запрещён контрактом."""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "config.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                value = node.value
                if isinstance(value, ast.Name) and value.id == "os":
                    offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}")

    assert not offenders, "чтение окружения мимо app.config:\n" + "\n".join(offenders)
