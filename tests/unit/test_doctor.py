# SPDX-License-Identifier: Apache-2.0
"""``sakrit doctor`` — Gate D: the net flags an unguarded ``requests.post``, stays
silent on a guarded one and on an opt-out-annotated one, and ``--check`` fails a
build with findings. Plus the precision edges: import tracking (an unrelated
``post()`` is NOT flagged), the passed-to-guard idiom, chained SMTP, write-vs-read
SQL, and the fail-closed parse-failure finding."""

from pathlib import Path

import sakrit
from sakrit.cli import main
from sakrit.doctor import PARSE_FAILURE, UNGUARDED_CALL, scan_paths, scan_source


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


def test_nonexistent_path_is_a_loud_finding(tmp_path: Path) -> None:
    # Fail closed: a typo'd CI path must not read as "scanned clean".
    findings, scanned = scan_paths([tmp_path / "no-such-dir"])
    assert scanned == 0
    assert [f.code for f in findings] == [PARSE_FAILURE]
    assert main(["doctor", "--check", str(tmp_path / "no-such-dir")]) == 1


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
