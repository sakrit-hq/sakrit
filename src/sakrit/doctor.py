# SPDX-License-Identifier: Apache-2.0
"""``sakrit doctor`` — a static net for "forgot to wrap" (roadmap #16, Q8).

Scans Python source with :mod:`ast` — the scanned code is **never imported or
executed** — for consequential-looking calls (HTTP mutations, SMTP sends, Stripe
mutations, boto3 mutating verbs, write-SQL ``execute``) that are not lexically
under a Sakrit guard. It is a **heuristic**, not a runtime guarantee: false
positives are cheap by design (Q8) — review the call and either wrap it or
annotate it ``# sakrit: safe`` / ``@sakrit.safe`` and move on. False negatives
are inherent (dynamic dispatch, cross-file flow); the doctor narrows the gap, the
ledger closes it.

A call is considered covered when it sits lexically inside a function that is
(a) decorated with ``@…effect(…)``, (b) passed by name to ``….guard(…)`` /
``….guard_async(…)`` in the same file, or (c) marked ``@…safe``. Coverage is
purely lexical and per-file — a helper called *from* a guarded function is still
flagged; annotate it after review. A module that IS effect machinery (a ledger, a
migration runner) opts out wholesale with a ``# sakrit: safe-file`` comment.

Fail-closed reporting: a file that cannot be parsed or decoded is a loud
``SAKRIT000`` finding (it was not verified), never a silent skip.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Finding", "scan_path", "scan_paths", "scan_source"]

# SAKRIT000 — the file was not scanned (parse/decode failure): not verified, loud.
# SAKRIT001 — an unguarded consequential call.
PARSE_FAILURE = "SAKRIT000"
UNGUARDED_CALL = "SAKRIT001"

_SAFE_COMMENT = re.compile(r"#\s*sakrit:\s*safe\b")
# Whole-file opt-out, for a module that IS effect machinery (a ledger, a migration
# runner) rather than one that calls effects. Deliberately a distinct token from the
# line marker so a file is never suppressed by accident.
_SAFE_FILE = re.compile(r"#\s*sakrit:\s*safe-file\b")

# The consequential-call catalog. Explicit and modest on purpose: each entry is a
# shape the scanner can resolve *lexically within one file*; anything cleverer
# (cross-file flow, dynamic dispatch) is out of the net's reach and documented so.
_HTTP_MODULES = {"requests", "httpx"}
_HTTP_MUTATING = {"post", "put", "patch", "delete"}
_HTTP_VERB_STRINGS = {"POST", "PUT", "PATCH", "DELETE"}  # requests.request("POST", …)
_HTTP_CLIENT_CTORS = {("requests", "Session"), ("httpx", "Client"), ("httpx", "AsyncClient")}
_SMTP_CTORS = {("smtplib", "SMTP"), ("smtplib", "SMTP_SSL"), ("smtplib", "LMTP")}
_SMTP_METHODS = {"sendmail", "send_message"}
_STRIPE_MUTATING = {"create", "modify", "delete", "cancel", "capture", "confirm", "pay", "refund"}
_BOTO3_MUTATING_PREFIXES = (
    "send_",
    "create_",
    "delete_",
    "put_",
    "update_",
    "start_",
    "run_",
    "terminate_",
    "stop_",
)
_BOTO3_MUTATING_EXACT = {"publish", "invoke"}
_WRITE_SQL = (
    "insert",
    "update",
    "delete",
    "replace",
    "create",
    "drop",
    "alter",
    "truncate",
    "merge",
)
_SKIP_DIRS = {"__pycache__", "site-packages", "node_modules", "build", "dist", "venv"}


@dataclass(frozen=True)
class Finding:
    """One doctor finding, addressable as ``path:line:col``."""

    path: str
    line: int
    col: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: {self.code} {self.message}"


def _dotted(expr: ast.expr) -> list[str] | None:
    """``a.b.c`` → ``["a", "b", "c"]``; anything not a plain Name/Attribute chain → None."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return list(reversed(parts))
    return None


def _terminal_name(expr: ast.expr) -> str | None:
    """The last identifier of a decorator/call target: ``sk.effect(…)`` → ``effect``."""
    if isinstance(expr, ast.Call):
        expr = expr.func
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return None


def _first_str_arg(call: ast.Call) -> str | None:
    """The first positional argument's string value, if statically visible.

    An f-string is resolved to its first literal fragment — enough to see the SQL
    verb in ``f"INSERT INTO {table} …"`` (the common interpolation shape)."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        for piece in arg.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                return piece.value
    return None


def _is_write_sql(sql: str) -> bool:
    return sql.lstrip().lower().startswith(_WRITE_SQL)


class _FileFacts:
    """Per-file lexical facts collected in one pre-pass over the tree."""

    def __init__(self, tree: ast.AST) -> None:
        self.module_alias: dict[str, str] = {}  # local name → tracked module ("r" → "requests")
        self.from_http_verbs: dict[str, str] = {}  # bare name → "module.verb"
        self.instances: dict[str, str] = {}  # var name → "http_client" | "smtp" | "boto3"
        self.guarded_fn_names: set[str] = set()  # names passed to ….guard(…)/….guard_async(…)
        tracked = _HTTP_MODULES | {"smtplib", "stripe", "boto3"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in tracked:
                        self.module_alias[alias.asname or root] = root
            elif isinstance(node, ast.ImportFrom):
                if node.module in _HTTP_MODULES:
                    for alias in node.names:
                        if alias.name in _HTTP_MUTATING:
                            verb = f"{node.module}.{alias.name}"
                            self.from_http_verbs[alias.asname or alias.name] = verb
            elif isinstance(node, ast.Assign):
                kind = self._ctor_kind(node.value)
                if kind is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.instances[target.id] = kind
            elif isinstance(node, ast.AnnAssign):
                if node.value is not None and isinstance(node.target, ast.Name):
                    kind = self._ctor_kind(node.value)
                    if kind is not None:
                        self.instances[node.target.id] = kind
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    kind = self._ctor_kind(item.context_expr)
                    if kind is not None and isinstance(item.optional_vars, ast.Name):
                        self.instances[item.optional_vars.id] = kind
            elif isinstance(node, ast.Call) and _terminal_name(node.func) in {
                "guard",
                "guard_async",
            }:
                for arg in node.args:
                    if isinstance(arg, ast.Name):
                        self.guarded_fn_names.add(arg.id)

    def _ctor_kind(self, expr: ast.expr) -> str | None:
        """Classify ``x = <ctor>(…)`` bindings the catalog cares about."""
        if not isinstance(expr, ast.Call):
            return None
        dotted = _dotted(expr.func)
        if dotted is None or len(dotted) != 2:
            return None
        root = self.module_alias.get(dotted[0])
        pair = (root, dotted[1]) if root else None
        if pair in _HTTP_CLIENT_CTORS:
            return "http_client"
        if pair in _SMTP_CTORS:
            return "smtp"
        if root == "boto3" and dotted[1] in {"client", "resource"}:
            return "boto3"
        return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, facts: _FileFacts, safe_lines: set[int]) -> None:
        self._path = path
        self._facts = facts
        self._safe_lines = safe_lines
        self._suppression_depth = 0  # >0 → inside a guarded / @safe / passed-to-guard function
        self.findings: list[Finding] = []

    # -- function scopes: decide suppression once, at the def --------------------------
    def _enter_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        deco_names = {_terminal_name(d) for d in node.decorator_list}
        suppressed = (
            bool(deco_names & {"effect", "safe"}) or node.name in self._facts.guarded_fn_names
        )
        self._suppression_depth += 1 if suppressed else 0
        self.generic_visit(node)
        self._suppression_depth -= 1 if suppressed else 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_fn(node)

    # -- the net -----------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        if self._suppression_depth == 0 and not self._line_safe(node):
            hit = self._classify(node)
            if hit is not None:
                self.findings.append(
                    Finding(
                        path=self._path,
                        line=node.lineno,
                        col=node.col_offset,
                        code=UNGUARDED_CALL,
                        message=(
                            f"unguarded consequential call: {hit} — wrap it with @sk.effect / "
                            "sk.guard, or annotate `# sakrit: safe` after review"
                        ),
                    )
                )
        self.generic_visit(node)

    def _line_safe(self, node: ast.Call) -> bool:
        return node.lineno in self._safe_lines or (node.end_lineno or 0) in self._safe_lines

    def _classify(self, node: ast.Call) -> str | None:
        """The catalog match for one call, or None. Returns the rendered call shape."""
        facts = self._facts
        func = node.func

        # smtplib.SMTP(…).sendmail(…) — a chained one-liner: resolve the inner ctor.
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _SMTP_METHODS
            and isinstance(func.value, ast.Call)
            and facts._ctor_kind(func.value) == "smtp"
        ):
            return f"smtplib …(…).{func.attr}(…)"

        # from requests import post → post(…)
        if isinstance(func, ast.Name) and func.id in facts.from_http_verbs:
            return f"{facts.from_http_verbs[func.id]}(…)"

        dotted = _dotted(func)
        if dotted is None:
            return None
        head, tail = dotted[0], dotted[-1]

        # requests.post(…) / httpx.delete(…) / requests.request("POST", …)
        module = facts.module_alias.get(head)
        if module in _HTTP_MODULES and len(dotted) == 2:
            if tail in _HTTP_MUTATING:
                return f"{module}.{tail}(…)"
            if tail == "request":
                verb = _first_str_arg(node)
                if verb is not None and verb.upper() in _HTTP_VERB_STRINGS:
                    return f'{module}.request("{verb.upper()}", …)'

        # stripe.PaymentIntent.create(…) — any chain rooted at the stripe alias.
        if module == "stripe" and len(dotted) >= 3 and tail in _STRIPE_MUTATING:
            return f"{'.'.join(['stripe', *dotted[1:]])}(…)"

        # Tracked instances: session/client HTTP verbs, SMTP sends, boto3 mutations.
        kind = facts.instances.get(head)
        if kind == "http_client" and len(dotted) == 2:
            if tail in _HTTP_MUTATING:
                return f"{head}.{tail}(…)  [requests/httpx client]"
            if tail == "request":
                verb = _first_str_arg(node)
                if verb is not None and verb.upper() in _HTTP_VERB_STRINGS:
                    return f'{head}.request("{verb.upper()}", …)  [requests/httpx client]'
        if kind == "smtp" and len(dotted) == 2 and tail in _SMTP_METHODS:
            return f"{head}.{tail}(…)  [smtplib]"
        if (
            kind == "boto3"
            and len(dotted) == 2
            and (tail.startswith(_BOTO3_MUTATING_PREFIXES) or tail in _BOTO3_MUTATING_EXACT)
        ):
            return f"{head}.{tail}(…)  [boto3]"

        # DBAPI write SQL — receiver-agnostic: .execute("INSERT …") on anything.
        if tail in {"execute", "executemany"}:
            sql = _first_str_arg(node)
            if sql is not None and _is_write_sql(sql):
                return f"{head}.{tail}(<write SQL>)"

        return None


def scan_source(source: str, path: str = "<string>") -> list[Finding]:
    """Scan one source text. A parse failure is a loud ``SAKRIT000`` finding."""
    if _SAFE_FILE.search(source):
        return []  # the whole module is reviewed effect-machinery — opted out explicitly
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [
            Finding(
                path=path,
                line=exc.lineno or 1,
                col=(exc.offset or 1) - 1,
                code=PARSE_FAILURE,
                message=f"could not parse — file NOT verified: {exc.msg}",
            )
        ]
    safe_lines = {
        i for i, text in enumerate(source.splitlines(), start=1) if _SAFE_COMMENT.search(text)
    }
    scanner = _Scanner(path, _FileFacts(tree), safe_lines)
    scanner.visit(tree)
    return sorted(scanner.findings, key=lambda f: (f.line, f.col))


def scan_path(path: Path) -> list[Finding]:
    """Scan one ``.py`` file. Decode/read failures are loud ``SAKRIT000`` findings."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [
            Finding(
                path=str(path),
                line=1,
                col=0,
                code=PARSE_FAILURE,
                message=f"could not read — file NOT verified: {exc}",
            )
        ]
    return scan_source(source, str(path))


def _iter_py_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for candidate in sorted(root.rglob("*.py")):
        parts = candidate.relative_to(root).parts
        if any(p in _SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        yield candidate


def scan_paths(paths: Iterable[Path]) -> tuple[list[Finding], int]:
    """Scan files/directories. Returns ``(findings, files_scanned)``."""
    findings: list[Finding] = []
    scanned = 0
    for root in paths:
        if not root.exists():
            # Fail closed: a path the caller named but that doesn't exist would otherwise
            # read as "scanned clean" — a typo'd CI path silently verifying nothing.
            findings.append(
                Finding(
                    path=str(root),
                    line=1,
                    col=0,
                    code=PARSE_FAILURE,
                    message="path does not exist — nothing was verified",
                )
            )
            continue
        for file in _iter_py_files(root):
            scanned += 1
            findings.extend(scan_path(file))
    return findings, scanned
