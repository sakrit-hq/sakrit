# SPDX-License-Identifier: Apache-2.0
"""``sakrit doctor`` — Gate D: the net flags an unguarded ``requests.post``, stays
silent on a guarded one and on an opt-out-annotated one, and ``--check`` fails a
build with findings. Plus the precision edges: import tracking (an unrelated
``post()`` is NOT flagged), the passed-to-guard idiom, chained SMTP, write-vs-read
SQL, and the fail-closed parse-failure finding."""

import json
from pathlib import Path

import pytest

import sakrit
from sakrit.cli import main
from sakrit.doctor import (
    PARSE_FAILURE,
    UNGUARDED_CALL,
    findings_to_json,
    findings_to_sarif,
    scan_paths,
    scan_source,
)


def _codes(src: str) -> list[str]:
    return [f.code for f in scan_source(src)]


def _messages(src: str) -> list[str]:
    return [f.message for f in scan_source(src)]


# --- Gate D core ---------------------------------------------------------------------
def test_unguarded_requests_post_is_flagged() -> None:
    src = """
import requests

def notify(url, body):
    return requests.post(url, json=body)
"""
    assert _codes(src) == [UNGUARDED_CALL]
    assert "requests.post" in _messages(src)[0]


def test_guarded_by_effect_decorator_is_silent() -> None:
    src = """
import requests

@sk.effect(DECL, key="notify")
def notify(url, body):
    return requests.post(url, json=body)
"""
    assert _codes(src) == []


def test_safe_comment_is_silent() -> None:
    src = """
import requests

def notify(url, body):
    return requests.post(url, json=body)  # sakrit: safe
"""
    assert _codes(src) == []


def test_safe_decorator_is_silent() -> None:
    src = """
import requests
import sakrit

@sakrit.safe
def health_ping(url):
    return requests.post(url)
"""
    assert _codes(src) == []


def test_passed_to_guard_is_silent() -> None:
    # The plain-path idiom: the tool fn is passed to sk.guard rather than decorated.
    src = """
import requests

def send(to):
    return requests.post("https://mail/send", json={"to": to})

sk.guard(DECL, send, kwargs={"to": "ops@x.com"}, key="invoice-1")
"""
    assert _codes(src) == []


# --- catalog coverage ----------------------------------------------------------------
def test_http_get_is_not_consequential() -> None:
    src = "import requests\nrequests.get('https://x')\n"
    assert _codes(src) == []


def test_unrelated_bare_post_is_not_flagged() -> None:
    # Precision: `post` that was NOT imported from requests/httpx must not fire.
    src = """
def post(x):
    return x

post("hello")
"""
    assert _codes(src) == []


def test_from_import_post_is_flagged() -> None:
    src = "from requests import post\npost('https://x')\n"
    assert _codes(src) == [UNGUARDED_CALL]


def test_aliased_module_is_tracked() -> None:
    src = "import httpx as h\nh.delete('https://x/1')\n"
    assert _codes(src) == [UNGUARDED_CALL]


def test_http_client_instance_verbs_are_flagged() -> None:
    src = """
import httpx
client = httpx.Client()
client.post("https://x")
client.get("https://x")
"""
    assert _codes(src) == [UNGUARDED_CALL]  # post yes, get no


def test_requests_request_verb_string() -> None:
    src = "import requests\nrequests.request('POST', 'https://x')\n"
    assert _codes(src) == [UNGUARDED_CALL]
    src_get = "import requests\nrequests.request('GET', 'https://x')\n"
    assert _codes(src_get) == []


def test_smtplib_with_block_and_chained() -> None:
    src = """
import smtplib

with smtplib.SMTP("localhost") as s:
    s.sendmail("a@x", ["b@x"], "hi")

smtplib.SMTP("localhost").send_message(msg)
"""
    assert _codes(src) == [UNGUARDED_CALL, UNGUARDED_CALL]


def test_stripe_mutation_is_flagged() -> None:
    src = """
import stripe
stripe.PaymentIntent.create(amount=100, currency="usd")
stripe.PaymentIntent.retrieve("pi_1")
"""
    assert _codes(src) == [UNGUARDED_CALL]  # create yes, retrieve no


def test_boto3_mutating_verbs_are_flagged() -> None:
    src = """
import boto3
ses = boto3.client("ses")
ses.send_email(Source="a@x", Destination={}, Message={})
ses.get_send_quota()
"""
    assert _codes(src) == [UNGUARDED_CALL]  # send_email yes, get_send_quota no


def test_write_sql_execute_is_flagged_read_is_not() -> None:
    src = """
cur.execute("INSERT INTO t VALUES (1)")
cur.execute("SELECT * FROM t")
cur.execute(f"UPDATE t SET x = {v}")
"""
    assert _codes(src) == [UNGUARDED_CALL, UNGUARDED_CALL]


def test_parse_failure_is_a_loud_finding() -> None:
    findings = scan_source("def broken(:\n")
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert "NOT verified" in findings[0].message


def test_safe_file_marker_suppresses_whole_module() -> None:
    src = """# sakrit: safe-file — this module is the effect machinery itself
import requests
requests.post('https://x')
"""
    assert _codes(src) == []


# --- A-6/A-7: markers are real comments only, coverage is precise --------------------
def test_safe_file_marker_inside_a_string_literal_does_not_suppress() -> None:
    # A-6: the marker text in a *string* (a docstring quoting it, a help message) must NOT
    # opt the file out — only a real comment does. Previously a raw-source regex matched here.
    src = """\
DOCS = "annotate a machinery module with # sakrit: safe-file at the top"
import requests
requests.post("https://x")
"""
    assert _codes(src) == [UNGUARDED_CALL]  # the call is still flagged


def test_safe_line_marker_inside_a_string_does_not_suppress() -> None:
    # A-7: the line marker in string data on the call line must not suppress the call.
    src = """\
import requests
requests.post("https://x", headers={"note": "# sakrit: safe"})
"""
    assert _codes(src) == [UNGUARDED_CALL]


def test_real_safe_comment_still_suppresses() -> None:
    src = 'import requests\nrequests.post("https://x")  # sakrit: safe\n'
    assert _codes(src) == []


def test_ambiguous_guard_name_does_not_suppress_an_unrelated_def() -> None:
    # A-7: `def send` must not be silenced just because *some* `send` was passed to .guard()
    # elsewhere in the file. With two defs sharing the name, coverage can't be attributed.
    src = """\
import requests

def send():
    requests.post("https://real-unrelated")

class C:
    def send(self):
        requests.post("https://also-unrelated")

sk.guard(decl, send, key="k")
"""
    assert _codes(src) == [UNGUARDED_CALL, UNGUARDED_CALL]  # both flagged, not suppressed


def test_unambiguous_guard_name_still_suppresses() -> None:
    src = """\
import requests

def send():
    requests.post("https://x")

sk.guard(decl, send, key="k")
"""
    assert _codes(src) == []  # single def of `send`, guarded by name → covered


def test_bare_foreign_effect_decorator_does_not_suppress() -> None:
    # A-7: a bare `@x.effect` (not call-shaped) from an unrelated library must not suppress.
    src = """\
import requests

@celery.effect
def task():
    requests.post("https://x")
"""
    assert _codes(src) == [UNGUARDED_CALL]


def test_nonexistent_path_is_a_loud_finding(tmp_path: Path) -> None:
    # Fail closed: a typo'd CI path must not read as "scanned clean".
    findings, scanned = scan_paths([tmp_path / "no-such-dir"])
    assert scanned == 0
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert main(["doctor", "--check", str(tmp_path / "no-such-dir")]) == 1


def test_null_byte_source_is_a_loud_finding_not_a_crash() -> None:
    # Corpus-hardening: ast.parse raises ValueError (not SyntaxError) on null bytes.
    # It must surface as a loud SAKRIT000, never an escaped exception.
    findings = scan_source("x = 1\x00\n", "weird.py")
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert "NOT verified" in findings[0].message


def _raise_recursion(*_a: object, **_k: object) -> object:
    raise RecursionError("maximum recursion depth exceeded")


def test_recursionerror_from_parse_degrades_to_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    # F-4.2: a pathologically deep file exhausts the recursion limit *inside ast.parse* on some
    # Python versions (3.11-3.13). The doctor must degrade to a loud SAKRIT000, not let it escape.
    # Forced deterministically — a real 20k-deep expression destabilizes the interpreter itself
    # (raises mid-C-construction on some versions, hangs tearing the AST down on others), which is
    # about CPython, not our handler.
    import ast

    monkeypatch.setattr(ast, "parse", _raise_recursion)  # doctor calls ast.parse
    findings = scan_source("x = 1\n", "deep.py")
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert "NOT verified" in findings[0].message


def test_recursionerror_from_walk_degrades_to_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    # ...and on other versions (3.10/3.14) the deep walk is what overflows. Same requirement.
    monkeypatch.setattr("sakrit.doctor._Scanner.visit", _raise_recursion)
    findings = scan_source("import requests\nrequests.post(u)\n", "deep2.py")
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert "NOT verified" in findings[0].message


def test_existing_root_with_no_python_is_fail_closed(tmp_path: Path) -> None:
    # F-4.3: an existing dir with no .py files (an emptied path after a rename) must not read as
    # "scanned clean" — same threat as a typo'd path. Loud SAKRIT000; --check fails.
    (tmp_path / "notes.txt").write_text("no python here")
    findings, scanned = scan_paths([tmp_path])
    assert scanned == 0
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert main(["doctor", "--check", str(tmp_path)]) == 1


def test_o_write_failure_exits_2_not_1(tmp_path: Path) -> None:
    # F-4.1: a failed -o write must not masquerade as exit 1 (findings). It's a usage error → 2.
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import requests\nrequests.post('https://x')\n")
    unwritable = tmp_path / "nonexistent-dir" / "out.json"  # parent doesn't exist
    assert main(["doctor", "--format", "json", "-o", str(unwritable), str(dirty)]) == 2


def test_scanned_code_syntaxwarning_does_not_leak(capsys: pytest.CaptureFixture[str]) -> None:
    # A legacy invalid escape in the *scanned* code must not spew a SyntaxWarning from the doctor.
    scan_source("import re\n\ndef f():\n    return re.match('\\d', 'x')\n", "legacy.py")
    assert "SyntaxWarning" not in capsys.readouterr().err


def test_own_tree_is_doctor_clean() -> None:
    # Dogfood: Sakrit's own source stays doctor-clean (ledger.py carries the reviewed
    # `# sakrit: safe-file` opt-out — its write SQL IS the guard). A new finding here
    # means new code needs wrapping or an explicit reviewed annotation.
    src_root = Path(__file__).parents[2] / "src" / "sakrit"
    findings, scanned = scan_paths([src_root])
    assert scanned > 0
    assert findings == []


# --- the runtime marker --------------------------------------------------------------
def test_safe_marker_is_runtime_inert() -> None:
    def fn(x: int) -> int:
        return x

    assert sakrit.safe(fn) is fn  # identity, no wrapper


# --- CLI / exit codes ----------------------------------------------------------------
def test_cli_check_fails_on_findings_and_passes_clean(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import requests\nrequests.post('https://x')\n")
    clean = tmp_path / "clean.py"
    clean.write_text("import requests\nrequests.get('https://x')\n")

    assert main(["doctor", str(dirty)]) == 0  # plain mode reports, exits 0
    assert main(["doctor", "--check", str(dirty)]) == 1  # CI gate fails
    assert main(["doctor", "--check", str(clean)]) == 0  # clean file passes


def test_cli_scans_directories_and_skips_hidden(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import requests\nrequests.post('https://x')\n")
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    (hidden / "junk.py").write_text("import requests\nrequests.post('https://x')\n")

    assert main(["doctor", "--check", str(tmp_path)]) == 1
    findings, scanned = scan_paths([tmp_path])
    assert scanned == 1  # the .venv file was skipped
    assert len(findings) == 1


# --- machine-readable output formats (frozen shapes) ---------------------------------
def test_json_envelope_shape() -> None:
    src = "import requests\n\ndef f(u):\n    return requests.post(u)\n"
    findings = scan_source(src, "app/notify.py")
    doc = findings_to_json(findings, files_scanned=1, version="9.9.9")

    assert doc["schema"] == "sakrit.doctor/1"
    assert doc["tool"] == {"name": "sakrit doctor", "version": "9.9.9"}
    assert doc["summary"] == {"files_scanned": 1, "findings": 1}
    (only,) = doc["findings"]
    assert only["code"] == UNGUARDED_CALL
    assert only["severity"] == "warning"
    assert only["path"] == "app/notify.py"
    assert only["line"] == 4 and only["col"] == 11  # 0-based col, matches render()
    assert "requests.post" in only["message"]


def test_json_severity_error_for_parse_failure(tmp_path: Path) -> None:
    bad = tmp_path / "broken.py"
    bad.write_text("def (:\n")  # syntax error → SAKRIT000
    findings, scanned = scan_paths([bad])
    doc = findings_to_json(findings, scanned, version="1.0.0")
    assert doc["findings"][0]["code"] == PARSE_FAILURE
    assert doc["findings"][0]["severity"] == "error"


def test_sarif_is_valid_211_shape() -> None:
    src = "import requests\n\ndef f(u):\n    return requests.post(u)\n"
    findings = scan_source(src, "app/notify.py")
    doc = findings_to_sarif(findings, version="9.9.9")

    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    (run,) = doc["runs"]
    driver = run["tool"]["driver"]
    assert driver["name"] == "sakrit-doctor"
    assert driver["version"] == "9.9.9"
    # Full rule catalog is always present, even with results present.
    assert {r["id"] for r in driver["rules"]} == {PARSE_FAILURE, UNGUARDED_CALL}
    (result,) = run["results"]
    assert result["ruleId"] == UNGUARDED_CALL
    assert result["level"] == "warning"
    region = result["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] == 4
    assert region["startColumn"] == 12  # SARIF is 1-based: col(11) + 1
    assert result["ruleIndex"] == [r["id"] for r in driver["rules"]].index(UNGUARDED_CALL)


def test_sarif_relativizes_absolute_paths_under_cwd() -> None:
    # F-5: an absolute path under cwd (the common $GITHUB_WORKSPACE/src CI habit) must be
    # relativized so GitHub code scanning can map the alert to a repo file.
    abs_path = str(Path.cwd() / "pkg" / "notify.py")
    findings = scan_source("import requests\nrequests.post(u)\n", abs_path)
    findings = [f for f in findings if f.code == UNGUARDED_CALL]
    doc = findings_to_sarif(findings, version="1.0.0")
    loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "pkg/notify.py"  # relative, forward-slash
    # JSON path relativizes too.
    jdoc = findings_to_json(findings, 1, version="1.0.0")
    assert jdoc["findings"][0]["path"] == "pkg/notify.py"


def test_sarif_partial_fingerprint_is_stable_across_line_moves() -> None:
    # F-6: the alert fingerprint must not change when the same finding merely shifts lines, or
    # GitHub closes+reopens the alert. Same call at a different line → same fingerprint.
    a = scan_source("import requests\nrequests.post(u)\n", "a.py")
    b = scan_source("import requests\n\n\n\nrequests.post(u)\n", "a.py")  # same call, moved down
    fa = findings_to_sarif([f for f in a if f.code == UNGUARDED_CALL], version="1.0.0")
    fb = findings_to_sarif([f for f in b if f.code == UNGUARDED_CALL], version="1.0.0")
    fp_a = fa["runs"][0]["results"][0]["partialFingerprints"]["sakritFindingV1"]
    fp_b = fb["runs"][0]["results"][0]["partialFingerprints"]["sakritFindingV1"]
    assert fp_a == fp_b and len(fp_a) == 16


def test_cli_sarif_with_check_exits_1_on_findings(tmp_path: Path) -> None:
    # m5: --check applies to sarif output too (only json was covered before).
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import requests\nrequests.post('https://x')\n")
    assert main(["doctor", "--format", "sarif", str(dirty)]) == 0  # report mode
    assert main(["doctor", "--format", "sarif", "--check", str(dirty)]) == 1  # gate fails


def test_sarif_emits_catalog_even_with_zero_findings() -> None:
    doc = findings_to_sarif([], version="1.0.0")
    run = doc["runs"][0]
    assert run["results"] == []
    assert len(run["tool"]["driver"]["rules"]) == 2  # ruleset always declared


def test_cli_json_output_is_parseable_and_check_orthogonal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import requests\nrequests.post('https://x')\n")

    # Report mode: exits 0 even with findings; stdout is a single valid JSON document.
    assert main(["doctor", "--format", "json", str(dirty)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "sakrit.doctor/1"
    assert doc["summary"]["findings"] == 1

    # --check flips only the exit code; the format is unchanged.
    assert main(["doctor", "--format", "json", "--check", str(dirty)]) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["findings"] == 1


def test_cli_sarif_written_to_file(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import requests\nrequests.post('https://x')\n")
    out = tmp_path / "out.sarif"

    assert main(["doctor", "--format", "sarif", "-o", str(out), str(dirty)]) == 0
    doc = json.loads(out.read_text())
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"][0]["ruleId"] == UNGUARDED_CALL
