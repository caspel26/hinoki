"""Optional, deterministic rules for an application built with Hinoki.

A ``RuleSet`` lists typed rule objects; every rule has a stable ID, a phase and
a severity. Rules produce ``Diagnostic`` values (data first, text derived), and
a diagnostic is either ``violated`` (demonstrated) or ``unverifiable`` (the
check could not decide). Nothing runs unless the application passes a
``RuleSet``; startup rules decide declarations, not what a function body does.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import inspect
import os
import typing
from collections.abc import Callable, Iterable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from hinoki.imports import imports_of, package_modules, within
from hinoki.routing import Route
from hinoki.schema import Schema

if TYPE_CHECKING:
    from hinoki.app import HinokiAPI

SCHEMA_VERSION = 1


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Phase(StrEnum):
    STATIC = "static"
    STARTUP = "startup"


class Status(StrEnum):
    VIOLATED = "violated"
    UNVERIFIABLE = "unverifiable"
    WAIVED = "waived"


@dataclasses.dataclass(frozen=True, slots=True)
class Diagnostic:
    rule_id: str
    severity: Severity
    phase: Phase
    message: str
    location: str
    explanation: str
    status: Status = Status.VIOLATED
    subject: str = ""
    waiver: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule_id,
            "severity": self.severity.value,
            "phase": self.phase.value,
            "status": self.status.value,
            "message": self.message,
            "location": self.location,
            "explanation": self.explanation,
            "subject": self.subject,
            "waiver": self.waiver,
        }

    def text(self) -> str:
        return f"{self.location}: {self.rule_id} [{self.status}] {self.message}"


@dataclasses.dataclass(frozen=True, slots=True)
class Project:
    """What rules inspect: the application registry."""

    app: HinokiAPI
    packages: tuple[str, ...] = ()


def qualified(route: Route) -> str:
    """Stable identity of an action. An inherited action is named after the API
    class it is mounted on, so two subclasses of one base never share a name."""
    if route.owner is not None:
        owner = route.owner
        return f"{owner.__module__}.{owner.__qualname__}.{route.handler.__name__}"
    return f"{route.handler.__module__}.{route.handler.__qualname__}"


def where(target: object) -> str:
    """``file:line`` of a function or class, else its name."""
    try:
        path = inspect.getsourcefile(target)  # type: ignore[arg-type]
        line = inspect.getsourcelines(target)[1]  # type: ignore[arg-type]
    except (OSError, TypeError):
        return getattr(target, "__qualname__", repr(target))
    if path is None:
        return getattr(target, "__qualname__", repr(target))
    with contextlib.suppress(ValueError):  # another drive on Windows
        path = os.path.relpath(path)
    return f"{path}:{line}"


class Rule:
    """Base class of rules. Subclasses set the class attributes and implement
    ``check``. ``effects`` lists side effects (running code, files, network,
    database) a check needs; tools refuse effectful checks unless allowed."""

    id: ClassVar[str] = ""
    phase: ClassVar[Phase] = Phase.STARTUP
    severity: ClassVar[Severity] = Severity.ERROR
    explanation: ClassVar[str] = ""
    effects: ClassVar[tuple[str, ...]] = ()

    def check(self, project: Project) -> Iterable[Diagnostic]:
        raise NotImplementedError

    def diagnostic(
        self,
        message: str,
        location: str,
        status: Status = Status.VIOLATED,
        subject: str | None = None,
    ) -> Diagnostic:
        """``subject`` identifies the thing for waivers; an empty subject marks a
        diagnostic that cannot be waived (the rule could not do its job)."""
        return Diagnostic(
            self.id,
            self.severity,
            self.phase,
            message,
            location,
            self.explanation,
            status,
            location if subject is None else subject,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase.value,
            "severity": self.severity.value,
            "explanation": self.explanation,
            "effects": list(self.effects),
            "summary": self.summary(),
        }

    def summary(self) -> str:
        return type(self).__name__


class RuleSet:
    """The rules an application enabled."""

    def __init__(self, *rules: Rule) -> None:
        ids = [(r.id, r.summary()) for r in rules]
        if any(not rule.id for rule in rules):
            raise ValueError("every rule needs a stable id")
        if len(set(ids)) != len(ids):
            raise ValueError("the same rule is listed twice")
        self.rules = rules

    def run(
        self,
        project: Project,
        phase: Phase | None = None,
        allow_effects: bool = False,
    ) -> list[Diagnostic]:
        """Every diagnostic of the rules in ``phase`` (all phases when ``None``).
        A rule that raises becomes an ``internal`` error diagnostic and the other
        rules still run; a rule with effects is skipped (and reported) unless
        ``allow_effects``."""
        found: list[Diagnostic] = []
        for rule in self.rules:
            if phase is not None and rule.phase is not phase:
                continue
            if rule.effects and not allow_effects:
                found.append(refused(rule))
                continue
            found.extend(run_rule(rule, project))
        return found


def refused(rule: Rule) -> Diagnostic:
    return Diagnostic(
        rule.id,
        Severity.ERROR,
        rule.phase,
        f"not run: it needs {', '.join(rule.effects)} (pass --allow-effects)",
        rule.id,
        rule.explanation,
        Status.UNVERIFIABLE,
        "",
    )


def run_rule(rule: Rule, project: Project) -> list[Diagnostic]:
    try:
        return list(rule.check(project))
    except (Exception, SystemExit) as exc:  # a rule may call sys.exit
        return [
            Diagnostic(
                rule.id,
                Severity.ERROR,
                rule.phase,
                f"the rule failed to run: {type(exc).__name__}: {exc}",
                rule.id,
                rule.explanation,
                Status.UNVERIFIABLE,
                "",
            )
        ]


def failing(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    """The diagnostics that should fail a build: errors that are not waived."""
    return [
        d
        for d in diagnostics
        if d.severity is Severity.ERROR and d.status is not Status.WAIVED
    ]


def matches(route: Route, patterns: Iterable[str]) -> bool:
    """``fnmatch`` against the action's full qualified name (so ``health`` alone
    never exempts a ``health`` action in another module; write ``*.health``)."""
    name = qualified(route)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


class RequireAccess(Rule):
    id = "HNK-ACC-001"
    explanation = (
        "Actions are public unless they name an Access policy. This project "
        "requires every matching action to state its policy explicitly."
    )

    def __init__(self, match: str = "*", except_: Iterable[str] = ()) -> None:
        self.match = match
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        skipped = list(self.exceptions)
        return f"Actions.require_access(match={self.match!r}, except_={skipped})"

    def check(self, project: Project) -> Iterator[Diagnostic]:
        hit = 0
        for route in project.app.routes:
            if not matches(route, [self.match]):
                continue
            hit += 1
            if matches(route, self.exceptions):
                continue
            if route.access is None:
                yield self.diagnostic(
                    f"{route.method} {route.path} ({route.handler.__qualname__}) "
                    "declares no access policy",
                    where(route.handler),
                    subject=qualified(route),
                )
        if self.match != "*" and hit == 0:
            yield self.diagnostic(
                f"match={self.match!r} names no action, so nothing was checked",
                self.id,
                Status.UNVERIFIABLE,
                "",
            )


class RequireAtomic(Rule):
    id = "HNK-TXN-001"
    explanation = (
        "Actions that change state must run in a transaction so a failure rolls "
        "everything back before the error is reported."
    )

    def __init__(
        self,
        methods: Iterable[str] = ("POST", "PUT", "PATCH", "DELETE"),
        except_: Iterable[str] = (),
    ) -> None:
        self.methods = frozenset(m.upper() for m in methods)
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        return f"Transactions.require_atomic(methods={sorted(self.methods)})"

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            if route.method not in self.methods or matches(route, self.exceptions):
                continue
            if not route.transaction:
                yield self.diagnostic(
                    f"{route.method} {route.path} ({route.handler.__qualname__}) "
                    "does not declare transaction=True",
                    where(route.handler),
                    subject=qualified(route),
                )


def is_open(annotation: object) -> bool:
    """The annotation does not say what is returned (missing, ``Any``, ``object``)."""
    if annotation is None or annotation in {typing.Any, object}:
        return True
    return any(is_open(argument) for argument in typing.get_args(annotation))


def schemas_in(annotation: object) -> Iterator[type[Schema]]:
    """Schema classes named by an annotation (through ``list``, ``Page``, unions)."""
    if isinstance(annotation, type) and issubclass(annotation, Schema):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from schemas_in(argument)


def subclasses(schema: type[Schema]) -> Iterator[type[Schema]]:
    for child in schema.__subclasses__():
        yield child
        yield from subclasses(child)


def reachable(start: Iterable[type[Schema]]) -> Iterator[type[Schema]]:
    """``start``, their subclasses (the runtime serialises the actual subclass)
    and every schema nested in their fields, each once."""
    seen: set[type[Schema]] = set()
    pending = list(start)
    while pending:
        schema = pending.pop()
        if schema in seen:
            continue
        seen.add(schema)
        pending.extend(subclasses(schema))
        yield schema
        for spec in schema.specs().values():
            level: Any = spec.ftype
            while level is not None:
                if level.schema is not None:
                    pending.append(level.schema)
                level = level.item


class ForbidExposure(Rule):
    id = "HNK-SCH-001"
    explanation = (
        "A field with a forbidden name must never appear in a schema an action "
        "returns, directly or nested, so it cannot reach a client."
    )

    def __init__(self, *fields: str) -> None:
        if not fields:
            raise ValueError("name at least one field")
        self.fields = frozenset(fields)

    def summary(self) -> str:
        return f"Schemas.forbid_exposure({', '.join(sorted(self.fields))})"

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            hints = typing.get_type_hints(route.handler)
            annotation = hints.get("return", None)
            if "return" not in hints or is_open(annotation):
                yield self.diagnostic(
                    f"{route.method} {route.path}: the return type does not name "
                    "its schema, so exposure cannot be checked",
                    where(route.handler),
                    Status.UNVERIFIABLE,
                    qualified(route),
                )
            returned = list(schemas_in(annotation))
            for schema in reachable(returned):
                for name in sorted(self.fields & set(schema.specs())):
                    yield self.diagnostic(
                        f"{schema.__name__}.{name} is exposed by "
                        f"{route.method} {route.path}",
                        where(schema),
                        subject=f"{schema.__module__}.{schema.__qualname__}.{name}",
                    )


class ForbidImports(Rule):
    id = "HNK-IMP-001"
    phase = Phase.STATIC
    explanation = (
        "Code under the source package must not import from the target package, "
        "keeping the dependency direction the project declared. Only direct "
        "imports in the project's own files are checked."
    )

    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target

    def summary(self) -> str:
        return f"Imports.forbid(source={self.source!r}, target={self.target!r})"

    def check(self, project: Project) -> Iterator[Diagnostic]:
        if not project.packages:
            yield self.diagnostic(
                "no packages were given to scan (pass packages to the check)",
                self.id,
                Status.UNVERIFIABLE,
                "",
            )
            return
        scanned = 0
        for package in project.packages:
            for module, path in package_modules(package):
                if within(module, self.source):
                    scanned += 1
                    yield from self.scan(module, path)
        if scanned == 0:
            yield self.diagnostic(
                f"no scanned module is under {self.source!r}; nothing was checked",
                self.id,
                Status.UNVERIFIABLE,
                "",
            )

    def scan(self, module: str, path: Path) -> Iterator[Diagnostic]:
        for found in imports_of(module, path):
            where_ = f"{found.file}:{found.line}"
            if not found.target:
                yield self.diagnostic(
                    f"{module} imports a module chosen at runtime; "
                    f"cannot tell whether it is {self.target}",
                    where_,
                    Status.UNVERIFIABLE,
                    f"{module} -> <dynamic>",
                )
            elif within(found.target, self.target):
                yield self.diagnostic(
                    f"{module} imports {found.target}, which is forbidden "
                    f"for {self.source}",
                    where_,
                    subject=f"{module} -> {found.target}",
                )


class Imports:
    forbid: ClassVar[Callable[..., Rule]] = ForbidImports


class Actions:
    """Namespace of action rules."""

    require_access: ClassVar[Callable[..., Rule]] = RequireAccess


class Transactions:
    require_atomic: ClassVar[Callable[..., Rule]] = RequireAtomic


class Schemas:
    forbid_exposure: ClassVar[Callable[..., Rule]] = ForbidExposure
