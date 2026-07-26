# SPDX-License-Identifier: Apache-2.0
"""L1 reconcile + L2R: recovery asks the provider "did this happen?"."""

from sakrit import Sakrit, SqliteLedger
from sakrit.core import (
    ArgClass,
    EffectDecl,
    EffectState,
    Reconciliation,
    positional_key,
)
from sakrit.core.coordinate import Coordinate

SECRET = b"deployment-secret"


class QueryableProvider:
    """A provider that isn't idempotent but *is* queryable (L1)."""

    def __init__(self) -> None:
        self.deliveries: list[str] = []
        self.settled: dict[str, str] = {}  # key → provider record, if it happened
        self.answer_absent = False  # force reconcile to report ABSENT
        self.answer_unknown = False  # force reconcile to report UNKNOWN

    def send(self, key: str) -> str:
        self.deliveries.append(key)
        record = f"provider-record-{len(self.deliveries)}"
        self.settled[key] = record
        return record

    def reconcile(self, key: str) -> Reconciliation:
        if self.answer_unknown:
            return Reconciliation.unknown()
        if self.answer_absent or key not in self.settled:
            return Reconciliation.absent()
        return Reconciliation.settled(self.settled[key])


def _key(name: str) -> str:
    return positional_key(Coordinate("global", name.encode()), "crm.ticket")


def _l1_decl(provider: QueryableProvider, *, on_absent: str = "surface") -> EffectDecl:
    return EffectDecl(
        "crm.ticket",
        {"subject": ArgClass.IDENTITY},
        reconcile=provider.reconcile,
        on_absent=on_absent,
    )


def test_level_derivation() -> None:
    assert EffectDecl("t").level == "L0"
    assert EffectDecl("t", provider_key_param="k").level == "L2"
    assert EffectDecl("t", reconcile=lambda k: Reconciliation.absent()).level == "L1"
    assert (
        EffectDecl("t", provider_key_param="k", reconcile=lambda k: Reconciliation.absent()).level
        == "L2R"
    )


def test_l1_reconcile_settled_adopts_provider_record() -> None:
    led = SqliteLedger()
    provider = QueryableProvider()
    decl = _l1_decl(provider)
    key = _key("crash")
    # Simulate a crash in the window: the provider got it, but we never recorded.
    led.claim(key, "global", "crm.ticket", "fp", reconcilable=True)
    led.mark_executing(key)
    provider.send(key)  # the effect happened at the provider

    sk = Sakrit(led, secret=SECRET)
    sk.effect(decl, key="crash")(lambda subject: provider.send(key))  # register the tool
    sk.recover()

    assert led.state_of(key) is EffectState.SUCCEEDED  # reconciled to settled
    assert len(provider.deliveries) == 1  # not re-sent


def test_l1_reconcile_absent_surfaces_by_default() -> None:
    led = SqliteLedger()
    provider = QueryableProvider()
    decl = _l1_decl(provider, on_absent="surface")  # irreversible default
    key = _key("crash")
    led.claim(key, "global", "crm.ticket", "fp", reconcilable=True)
    led.mark_executing(key)  # crashed; provider never got it → ABSENT

    sk = Sakrit(led, secret=SECRET)
    sk._registry["crm.ticket"] = decl  # register
    sk.recover()

    # ABSENT from a possibly-lagging read is not trusted for an irreversible effect.
    assert led.state_of(key) is EffectState.AMBIGUOUS


def test_l1_reconcile_absent_retry_when_declared() -> None:
    led = SqliteLedger()
    provider = QueryableProvider()
    decl = _l1_decl(provider, on_absent="retry")
    key = _key("crash")
    led.claim(key, "global", "crm.ticket", "fp", reconcilable=True)
    led.mark_executing(key)

    sk = Sakrit(led, secret=SECRET)
    sk._registry["crm.ticket"] = decl
    sk.recover()

    # Declared retry-safe → re-claimable (the effect provably didn't happen).
    assert led.state_of(key) is EffectState.CLAIMED


def test_l1_reconcile_unknown_stays_ambiguous() -> None:
    led = SqliteLedger()
    provider = QueryableProvider()
    provider.answer_unknown = True
    decl = _l1_decl(provider)
    key = _key("crash")
    led.claim(key, "global", "crm.ticket", "fp", reconcilable=True)
    led.mark_executing(key)

    sk = Sakrit(led, secret=SECRET)
    sk._registry["crm.ticket"] = decl
    sk.recover()

    assert led.state_of(key) is EffectState.AMBIGUOUS


def test_l2r_reconcile_removes_the_ttl_cliff() -> None:
    # L2R: past the provider's key TTL, reconcile (not blind re-dispatch) resolves it.
    led = SqliteLedger()
    provider = QueryableProvider()
    decl = EffectDecl(
        "pay.charge",
        {"amount": ArgClass.IDENTITY},
        provider_key_param="idempotency_key",
        reconcile=provider.reconcile,
    )
    assert decl.level == "L2R"
    key = positional_key(Coordinate("global", b"chg"), "pay.charge")
    led.claim(key, "global", "pay.charge", "fp", provider_dedup=True, reconcilable=True)
    led.mark_executing(key)
    provider.send(key)  # the charge happened

    sk = Sakrit(led, secret=SECRET)
    sk._registry["pay.charge"] = decl
    sk.recover()

    assert led.state_of(key) is EffectState.SUCCEEDED  # reconciled, no TTL cliff
    assert len(provider.deliveries) == 1
