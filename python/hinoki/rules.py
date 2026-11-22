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

from hinoki.handlerhints import hints_of
from hinoki.imports import imports_of, package_modules, within
from hinoki.routing import Route
from hinoki.schema import Schema

if TYPE_CHECKING:
    from hinoki.app import Hinoki

SCHEMA_VERSION = 2  # 2: status "warning" and summary.warning


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
    WARNING = "warning"  # reported, never failing (the waiver notices)


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
    fix: str = ""  # the exact change that satisfies the rule

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
            "fix": self.fix,
        }

    def text(self) -> str:
        line = f"{self.location}: {self.rule_id} [{self.status}] {self.message}"
        return f"{line}\n  fix: {self.fix}" if self.fix else line


@dataclasses.dataclass(frozen=True, slots=True)
class Project:
    """What rules inspect: the application registry."""

    app: Hinoki
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
    fix: ClassVar[str] = ""  # shown under a violation: the exact change to make
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
            fix=self.fix if status is Status.VIOLATED else "",
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
    fix = (
        "add access=<policy> to the action (or list it in except_ "
        "when it is public on purpose)"
    )
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


OWN_TRANSACTIONS = ("hinoki.admin.",)  # framework routes that manage their own


class RequireAtomic(Rule):
    id = "HNK-TXN-001"
    fix = (
        "add atomic=True to the action (or declare it on a "
        "RouteGroup(..., atomic=True) group)"
    )
    explanation = (
        "Actions that change state must run in a transaction so a failure rolls "
        "everything back before the error is reported."
    )

    def __init__(
        self,
        methods: Iterable[str] = ("POST", "PUT", "PATCH", "DELETE"),
        match: str = "*",
        except_: Iterable[str] = (),
    ) -> None:
        self.methods = frozenset(m.upper() for m in methods)
        self.match = match
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        return (
            f"Transactions.require_atomic(methods={sorted(self.methods)}, "
            f"match={self.match!r}, except_={list(self.exceptions)})"
        )

    def check(self, project: Project) -> Iterator[Diagnostic]:
        hit = 0
        for route in project.app.routes:
            if route.method not in self.methods or not matches(route, [self.match]):
                continue
            hit += 1
            if matches(route, self.exceptions):
                continue
            if route.handler.__module__.startswith(OWN_TRANSACTIONS):
                continue  # the admin's writes open their own short transactions
            if not route.atomic:
                yield self.diagnostic(
                    f"{route.method} {route.path} ({route.handler.__qualname__}) "
                    "does not declare atomic=True",
                    where(route.handler),
                    subject=qualified(route),
                )
        if self.match != "*" and hit == 0:
            yield self.diagnostic(
                f"match={self.match!r} names no state-changing action, so nothing "
                "was checked",
                self.id,
                Status.UNVERIFIABLE,
                "",
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
        pending_types = [spec.ftype for spec in schema.specs().values()]
        while pending_types:
            level = pending_types.pop()
            if level.schema is not None:
                pending.append(level.schema)
            pending.extend(variant for _, variant in level.variants)
            pending_types.extend(level.children())


def served_names(schema: type[Schema]) -> set[str]:
    """Every name a client can read from ``schema``: its fields, their wire names
    (aliases) and its computed fields."""
    specs = schema.specs().values()
    return {
        *(spec.name for spec in specs),
        *(spec.wire_name for spec in specs),
        *schema.computed_fields(),
    }


class ForbidExposure(Rule):
    id = "HNK-SCH-001"
    fix = (
        "remove the field from the response Schema, or return a "
        "Schema that does not carry it"
    )
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
            hints = hints_of(route.handler)
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
                for name in sorted(self.fields & served_names(schema)):
                    yield self.diagnostic(
                        f"{schema.__name__}.{name} is exposed by "
                        f"{route.method} {route.path}",
                        where(schema),
                        subject=f"{schema.__module__}.{schema.__qualname__}.{name}",
                    )


def models_in(annotation: object) -> Iterator[type]:
    """Model classes named by an annotation (through ``list``, ``Page``, unions)."""
    from hinoki.orm import Model  # noqa: PLC0415 - orm is only needed to check this

    if isinstance(annotation, type) and issubclass(annotation, Model):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from models_in(argument)


class ForbidModelResponses(Rule):
    id = "HNK-API-001"
    fix = "return a Schema (for example PostOut.from_model(row)) instead of the Model"
    explanation = (
        "An action must not return a Model (or a list/page of them): a response "
        "schema names exactly what a client may see, so a column never leaks "
        "because someone added it to the table."
    )

    def __init__(self, match: str = "*", except_: Iterable[str] = ()) -> None:
        self.match = match
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        return (
            f"Actions.forbid_model_responses(match={self.match!r}, "
            f"except_={list(self.exceptions)})"
        )

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            if not matches(route, [self.match]) or matches(route, self.exceptions):
                continue
            annotation = hints_of(route.handler).get("return")
            for model in models_in(annotation):
                yield self.diagnostic(
                    f"{route.method} {route.path} ({route.handler.__qualname__}) "
                    f"returns the model {model.__name__}; return a Schema",
                    where(route.handler),
                    subject=qualified(route),
                )


class RequireReturnType(Rule):
    id = "HNK-API-002"
    fix = "annotate the action's return type (a Schema, or None for status 204)"
    explanation = (
        "The return annotation is the response contract that OpenAPI and rules "
        "read. An action without one (or typed Any/object) has no checkable response."
    )

    def __init__(self, match: str = "*", except_: Iterable[str] = ()) -> None:
        self.match = match
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        return (
            f"Actions.require_return_type(match={self.match!r}, "
            f"except_={list(self.exceptions)})"
        )

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            if not matches(route, [self.match]) or matches(route, self.exceptions):
                continue
            hints = hints_of(route.handler)
            if "return" in hints and not is_open(hints["return"]):
                continue
            yield self.diagnostic(
                f"{route.method} {route.path} ({route.handler.__qualname__}) "
                "does not declare a concrete return type",
                where(route.handler),
                subject=qualified(route),
            )


def returns_plain_list(annotation: object) -> bool:
    """The annotation is, or is a union with, a ``list[...]``."""
    if typing.get_origin(annotation) is list or annotation is list:
        return True
    return any(returns_plain_list(argument) for argument in typing.get_args(annotation))


class RequirePagination(Rule):
    id = "HNK-API-003"
    fix = "return Page[Item] or CursorPage[Item] and paginate the action"
    explanation = (
        "A GET that returns a plain list grows with the table. A Page or a "
        "CursorPage bounds the response."
    )

    def __init__(self, match: str = "*", except_: Iterable[str] = ()) -> None:
        self.match = match
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        return (
            f"Actions.require_pagination(match={self.match!r}, "
            f"except_={list(self.exceptions)})"
        )

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            if route.method != "GET" or not matches(route, [self.match]):
                continue
            if matches(route, self.exceptions):
                continue
            if returns_plain_list(hints_of(route.handler).get("return")):
                yield self.diagnostic(
                    f"GET {route.path} ({route.handler.__qualname__}) returns a "
                    "plain list, not a Page",
                    where(route.handler),
                    subject=qualified(route),
                )


class RequirePermissions(Rule):
    id = "HNK-ACC-002"
    fix = "name at least one permission in the Access policy of the action"
    explanation = (
        "An Access with no permission accepts any authenticated caller. This "
        "project requires every protected action to name what it needs."
    )

    def __init__(self, match: str = "*", except_: Iterable[str] = ()) -> None:
        self.match = match
        self.exceptions = tuple(except_)

    def summary(self) -> str:
        return (
            f"Actions.require_permissions(match={self.match!r}, "
            f"except_={list(self.exceptions)})"
        )

    def check(self, project: Project) -> Iterator[Diagnostic]:
        for route in project.app.routes:
            if not matches(route, [self.match]) or matches(route, self.exceptions):
                continue
            if route.access is not None and not route.access.permissions:
                yield self.diagnostic(
                    f"{route.method} {route.path} ({route.handler.__qualname__}) "
                    "names an Access policy without permissions",
                    where(route.handler),
                    subject=qualified(route),
                )


def request_schemas(route: Route) -> Iterator[type[Schema]]:
    """Schema classes the action accepts (every parameter, not the return type)."""
    for name, annotation in hints_of(route.handler).items():
        if name != "return":
            yield from schemas_in(annotation)


def is_bounded_text(field_type: Any) -> bool:
    constraints = field_type.info.constraints()
    return "max_length" in constraints or "one_of" in constraints


def has_unbounded_text(field_type: Any) -> bool:
    """A text value without a limit: the field itself, or the elements of a list
    or the values of a dict (at any depth), each of which a client may send at
    any length. Dict keys from clients are bounded by the validator."""
    if field_type.kind == "str":
        return not is_bounded_text(field_type)
    return any(has_unbounded_text(child) for child in field_type.children())


class RequireStringLimits(Rule):
    id = "HNK-SCH-002"
    fix = "bound the field: Annotated[str, Field(max_length=...)] or one_of=..."
    explanation = (
        "A text field of a request Schema without max_length accepts a body of "
        "any size up to the server limit, and stores it."
    )

    def summary(self) -> str:
        return "Schemas.require_string_limits()"

    def check(self, project: Project) -> Iterator[Diagnostic]:
        seen: set[type[Schema]] = set()
        for route in project.app.routes:
            for schema in reachable(request_schemas(route)):
                if schema in seen:
                    continue
                seen.add(schema)
                for name, spec in sorted(schema.specs().items()):
                    if has_unbounded_text(spec.ftype):
                        yield self.diagnostic(
                            f"{schema.__name__}.{name} is read from "
                            f"{route.method} {route.path} without a max_length",
                            where(schema),
                            subject=f"{schema.__module__}.{schema.__qualname__}.{name}",
                        )


def declared_models(match: str) -> list[Any]:
    """Concrete models whose module matches ``match`` (``fnmatch``)."""
    from hinoki.orm import Model  # noqa: PLC0415 - orm is only needed to check this

    found: dict[Any, None] = {}
    pending: list[Any] = list(Model.__subclasses__())
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        if not model.__abstract__ and fnmatch.fnmatchcase(model.__module__, match):
            found[model] = None
    return sorted(found, key=lambda item: (item.__module__, item.__qualname__))


def unverifiable_models(rule: Rule, match: str) -> Diagnostic:
    return rule.diagnostic(
        f"match={match!r} names no model, so nothing was checked",
        rule.id,
        Status.UNVERIFIABLE,
        "",
    )


class RequireExplicitTable(Rule):
    id = "HNK-DB-001"
    fix = "name the table with @db.table(...) or table=... on the model"
    explanation = (
        "A derived table name follows the Python class and module names, so a "
        "rename changes the physical table. Pinning it keeps migration identity."
    )

    def __init__(self, match: str = "*") -> None:
        self.match = match

    def summary(self) -> str:
        return f"Models.require_explicit_table(match={self.match!r})"

    def check(self, _project: Project) -> Iterator[Diagnostic]:
        models = declared_models(self.match)
        if not models:
            yield unverifiable_models(self, self.match)
        for model in models:
            if model.__table_is_implicit__:
                yield self.diagnostic(
                    f"{model.__qualname__} uses the derived table name "
                    f"{model.__table__!r}",
                    where(model),
                    subject=f"{model.__module__}.{model.__qualname__}",
                )


def is_indexed(model: Any, column: str) -> bool:
    """The column leads a primary key, a unique column or group, or an index."""
    from hinoki.orm import MARK, FieldMeta  # noqa: PLC0415 - orm is only needed here

    unique_columns = {
        item.name
        for item in dataclasses.fields(model)
        if item.metadata.get(MARK, FieldMeta()).unique
    }
    groups = (*model.__indexes__, *model.__unique__)
    return (
        column == model.__primary_key__
        or column in unique_columns
        or any(group[0] == column for group in groups)
    )


class RequireForeignKeyIndex(Rule):
    id = "HNK-DB-002"
    fix = (
        'index the column: @db.indexed("<column>") on the model, or an index or '
        "unique group that starts with it"
    )
    explanation = (
        "A foreign key column without an index makes joins and parent deletes "
        "scan the child table."
    )

    def __init__(self, match: str = "*") -> None:
        self.match = match

    def summary(self) -> str:
        return f"Models.require_foreign_key_index(match={self.match!r})"

    def check(self, _project: Project) -> Iterator[Diagnostic]:
        models = declared_models(self.match)
        if not models:
            yield unverifiable_models(self, self.match)
        for model in models:
            relations = model.__relations__
            for relation in relations.values():
                if relation.forward and not is_indexed(model, relation.column):
                    found = self.diagnostic(
                        f"{model.__qualname__}.{relation.column} references "
                        f"{relation.model.__name__} and has no index",
                        where(model),
                        subject=f"{model.__module__}.{model.__qualname__}."
                        f"{relation.column}",
                    )
                    column_fix = self.fix.replace("<column>", relation.column)
                    yield dataclasses.replace(found, fix=column_fix)


class ForbidImports(Rule):
    id = "HNK-IMP-001"
    fix = (
        "move the code to an allowed layer, or depend on it "
        "through the layer in between"
    )
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
    forbid_model_responses: ClassVar[Callable[..., Rule]] = ForbidModelResponses
    require_return_type: ClassVar[Callable[..., Rule]] = RequireReturnType
    require_pagination: ClassVar[Callable[..., Rule]] = RequirePagination
    require_permissions: ClassVar[Callable[..., Rule]] = RequirePermissions


class Transactions:
    require_atomic: ClassVar[Callable[..., Rule]] = RequireAtomic


class Schemas:
    forbid_exposure: ClassVar[Callable[..., Rule]] = ForbidExposure
    require_string_limits: ClassVar[Callable[..., Rule]] = RequireStringLimits


class Models:
    """Namespace of model rules; ``match`` is an ``fnmatch`` on the model's module."""

    require_explicit_table: ClassVar[Callable[..., Rule]] = RequireExplicitTable
    require_foreign_key_index: ClassVar[Callable[..., Rule]] = RequireForeignKeyIndex
