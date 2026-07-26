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


def _l1_decl(
    provider: QueryableProvider, *, on_absent: str = "surface", provider_read: str = "eventual"
) -> EffectDecl:
    return EffectDecl(
        "crm.ticket",
        {"subject": ArgClass.IDENTITY},
        reconcile=provider.reconcile,
        on_absent=on_absent,
        provider_read=provider_read,
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
    led = SqliteLedger(":memory:")
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
    led = SqliteLedger(":memory:")
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
    led = SqliteLedger(":memory:")
    provider = QueryableProvider()
    decl = _l1_decl(provider, on_absent="retry", provider_read="strong")
    key = _key("crash")
    led.claim(key, "global", "crm.ticket", "fp", reconcilable=True)
    led.mark_executing(key)

    sk = Sakrit(led, secret=SECRET)
    sk._registry["crm.ticket"] = decl
    sk.recover()

    # Declared retry-safe → re-claimable (the effect provably didn't happen).
    # Recovery blesses it INTENDED; the next claim re-owns it (P3-1).
    assert led.state_of(key) is EffectState.INTENDED


def test_l1_reconcile_unknown_stays_ambiguous() -> None:
    led = SqliteLedger(":memory:")
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
    led = SqliteLedger(":memory:")
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


def test_recover_isolates_a_raising_reconcile() -> None:
    # P1-15: one tool's reconcile raising (provider unreachable) must not strand the other
    # pending rows. V-12: a *raised* reconcile is transient — leave it EXECUTING (retried at
    # the next recovery), NOT ambiguated (which is permanent). The good key still resolves.
    led = SqliteLedger(":memory:")
    sk = Sakrit(led, secret=SECRET)

    def boom(key: str) -> Reconciliation:
        raise RuntimeError("provider unreachable")

    def ok(key: str) -> Reconciliation:
        return Reconciliation.settled({"adopted": True})

    decl_a = EffectDecl("crm.a", {"x": ArgClass.IDENTITY}, reconcile=boom)
    decl_b = EffectDecl("crm.b", {"x": ArgClass.IDENTITY}, reconcile=ok)
    sk._registry["crm.a"] = decl_a
    sk._registry["crm.b"] = decl_b

    led.claim("kA", "s", "crm.a", "fp", reconcilable=True)
    led.mark_executing("kA")
    led.claim("kB", "s", "crm.b", "fp", reconcilable=True)
    led.mark_executing("kB")

    sk.recover()

    assert led.state_of("kA") is EffectState.EXECUTING  # transient raise → retried next time
    assert led.state_of("kB") is EffectState.SUCCEEDED  # the other row still resolved


def test_v12_transient_reconcile_error_self_heals_on_next_recovery() -> None:
    # V-12: a raised reconcile leaves the row EXECUTING; the *next* recovery (fresh engine,
    # provider back up) reconciles it — a startup blip does not permanently strand the row.
    led = SqliteLedger(":memory:")
    provider = QueryableProvider()
    key = _key("blip")
    led.claim(key, "global", "crm.ticket", "fp", reconcilable=True)
    led.mark_executing(key)
    provider.settled[key] = "record"  # the effect actually landed

    calls = {"n": 0}

    def flaky(k: str) -> Reconciliation:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider mid-deploy")  # transient at first recovery
        return Reconciliation.settled(provider.settled[k])

    decl = EffectDecl("crm.ticket", {"subject": ArgClass.IDENTITY}, reconcile=flaky)

    sk1 = Sakrit(led, secret=SECRET)
    sk1._registry["crm.ticket"] = decl
    sk1.recover()
    assert led.state_of(key) is EffectState.EXECUTING  # not ambiguated by the blip

    sk2 = Sakrit(led, secret=SECRET)  # fresh process; provider recovered
    sk2._registry["crm.ticket"] = decl
    sk2.recover()
    assert led.state_of(key) is EffectState.SUCCEEDED  # self-healed


def test_p1_15_sliver_recover_retried_if_it_raises() -> None:
    # P1-15 sliver: a DB-level error in recover() must not permanently disable recovery —
    # _recovered is set only after recover() succeeds, so the next guard retries it.
    calls = {"n": 0}

    class FlakyRecover(SqliteLedger):
        def recover(self):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient DB error")
            return super().recover()

    led = FlakyRecover(":memory:")
    sk = Sakrit(led, secret=SECRET)

    @sk.effect(EffectDecl("email.send", {"to": ArgClass.IDENTITY}), key="k")
    def send(to: str) -> str:
        return "ok"

    import pytest

    with pytest.raises(RuntimeError):
        send(to="a@x.com")  # first guard: recover() raises → propagates, _recovered stays False
    assert send(to="a@x.com") == "ok"  # second guard: recover() retried and succeeded
    assert calls["n"] == 2
