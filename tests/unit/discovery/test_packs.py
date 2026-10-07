from pathlib import Path

import pytest
import yaml

from story_agent.config import ConfigError
from story_agent.discovery.packs import Category, Pack, PackSet, load_packs

ROOT = Path(__file__).resolve().parents[3]


def test_repo_packs_load(packs: PackSet) -> None:
    assert packs.ids == ["banking", "generic"]
    assert [p.id for p in packs.lineage("banking")] == ["generic", "banking"]


def test_generic_checklist_matches_the_brief(packs: PackSet) -> None:
    ids = packs.checklist("generic").ids
    assert ids == [
        "actors_permissions",
        "primary_flow",
        "alternate_failure_flows",
        "business_rules_limits",
        "data_records",
        "integrations",
        "notifications",
        "reporting",
        "security",
        "performance_availability",
        "audit",
        "edge_cases",
        "out_of_scope",
    ]


def test_banking_pack_has_every_category_from_the_brief(packs: PackSet) -> None:
    banking = {c.id for p in packs.lineage("banking") for c in p.categories}
    required = {
        "kyc_cdd",
        "aml_sanctions",
        "maker_checker",
        "limits_velocity",
        "cutoff_settlement",
        "fees_charges",
        "reversals_refunds",
        "reconciliation",
        "idempotency",
        "audit_retention",
        "consent_notification",
        "fraud_risk_checks",
        "regulatory_reporting",
        "data_privacy",
        "channels",
        "business_continuity",
        "payment_rails",
    }
    assert required <= banking


def test_subdomain_filters_categories_and_sets_must_haves(packs: PackSet) -> None:
    cards = packs.checklist("banking", "cards")
    assert "dispute_handling" in cards.ids
    assert "credit_decisioning" not in cards.ids
    assert "dispute_handling" in cards.must_have_ids
    assert "primary_flow" in cards.must_have_ids
    lending = packs.checklist("banking", "lending")
    assert "credit_decisioning" in lending.must_have_ids
    assert "dispute_handling" not in lending.ids


def test_unknown_subdomain_is_ignored(packs: PackSet) -> None:
    checklist = packs.checklist("banking", "nonsense")
    assert checklist.subdomain is None
    assert "credit_decisioning" not in checklist.ids


def test_india_subpack_is_optional(packs: PackSet) -> None:
    without = packs.checklist("banking", "payments_transfers")
    with_india = packs.checklist("banking", "payments_transfers", ["india_rails", "bogus"])
    assert "upi_rules" not in without.ids
    assert {"upi_rules", "neft_rtgs_imps_rules", "rbi_directions"} <= set(with_india.ids)
    assert with_india.subpacks == ("india_rails",)
    assert "upi_rules" in with_india.must_have_ids
    assert "payment_rails" in without.ids


def test_actor_catalogue_merges_generic_and_banking(packs: PackSet) -> None:
    names = {a.id for a in packs.checklist("banking").actors}
    assert {"end_user", "maker", "checker"} <= names


@pytest.mark.parametrize(
    ("text", "domain", "subdomain", "subpacks"),
    [
        (
            "Customer disputes a card transaction and wants a provisional credit.",
            "banking",
            "cards",
            [],
        ),
        ("Send money by UPI to a VPA.", "banking", "payments_transfers", ["india_rails"]),
        ("Patients book clinic appointments online.", "generic", None, []),
        ("", "generic", None, []),
    ],
)
def test_detect(
    packs: PackSet, text: str, domain: str, subdomain: str | None, subpacks: list[str]
) -> None:
    got = packs.detect(text)
    assert (got.domain, got.subdomain, got.subpacks) == (domain, subdomain, subpacks)


def test_detect_matches_plurals_placeholders_and_not_substrings(packs: PackSet) -> None:
    assert packs.detect("two loans and savings accounts at the bank").domain == "banking"
    assert packs.detect("The branch uses <IFSC_1> and an <AADHAAR_1> id with UPI").subpacks == [
        "india_rails"
    ]
    assert packs.detect("a bankrupt grapefruit stand").domain == "generic"


def test_detection_is_repeatable(packs: PackSet) -> None:
    text = "A customer disputes a card transaction."
    assert packs.detect(text) == packs.detect(text)


def test_checklist_is_repeatable_and_ordered(packs: PackSet) -> None:
    a = packs.checklist("banking", "payments_transfers", ["india_rails"])
    b = packs.checklist("banking", "payments_transfers", ["india_rails"])
    assert a == b
    assert a.ids[:2] == ["actors_permissions", "primary_flow"]


def test_new_industry_needs_only_a_yaml_file(tmp_path: Path) -> None:
    domains = tmp_path / "domains"
    domains.mkdir()
    for name in ("generic.yaml", "banking.yaml"):
        (domains / name).write_text((ROOT / "config" / "domains" / name).read_text())
    insurance = {
        "schema_version": "1.0",
        "id": "insurance",
        "name": "Insurance",
        "extends": "generic",
        "hints": [{"term": "policyholder", "weight": 2}, "claim", "premium"],
        "subdomains": [{"id": "claims", "name": "Claims", "hints": ["claim", "adjuster"]}],
        "categories": [
            {
                "id": "claim_triage",
                "name": "Claim triage",
                "must_have_in": ["claims"],
                "weight": 5,
                "applies_to": ["claims"],
                "probes": ["How is a claim triaged?"],
            }
        ],
    }
    (domains / "insurance.yaml").write_text(yaml.safe_dump(insurance))
    packs = load_packs(tmp_path)
    detection = packs.detect("The policyholder files a claim and an adjuster reviews it.")
    assert (detection.domain, detection.subdomain) == ("insurance", "claims")
    checklist = packs.checklist(detection.domain, detection.subdomain)
    assert "claim_triage" in checklist.must_have_ids
    assert "primary_flow" in checklist.ids


def _write(tmp_path: Path, name: str, data: dict[str, object]) -> None:
    (tmp_path / "domains").mkdir(exist_ok=True)
    (tmp_path / "domains" / name).write_text(yaml.safe_dump(data))


def _cat(cid: str = "a", **extra: object) -> dict[str, object]:
    return {"id": cid, "name": cid, "probes": ["p"], **extra}


def test_generic_pack_is_required(tmp_path: Path) -> None:
    _write(tmp_path, "x.yaml", {"id": "x", "name": "X"})
    with pytest.raises(ConfigError, match="generic"):
        load_packs(tmp_path)


@pytest.mark.parametrize(
    ("pack", "message"),
    [
        ({"id": "x", "name": "X", "extends": "nope"}, "unknown domain pack"),
        ({"id": "x", "name": "X", "categories": [_cat("a"), _cat("a")]}, "duplicate category"),
        (
            {"id": "x", "name": "X", "categories": [_cat("a", applies_to=["zz"])]},
            "unknown sub-domains",
        ),
        ({"id": "x", "name": "X", "schema_version": "9"}, "unsupported"),
        (
            {
                "id": "x",
                "name": "X",
                "categories": [{"id": "Bad Id", "name": "n", "probes": ["p"]}],
            },
            "invalid pack",
        ),
        ({"id": "x", "name": "X", "categories": [_cat("a", probes=[])]}, "invalid pack"),
        ({"id": "x", "name": "X", "surprise": 1}, "invalid pack"),
        ({"id": "y", "name": "X"}, "must match file name"),
        (
            {
                "id": "x",
                "name": "X",
                "subdomains": [{"id": "s", "name": "s"}, {"id": "s", "name": "s"}],
            },
            "duplicate sub-domain",
        ),
    ],
)
def test_invalid_packs_are_rejected(tmp_path: Path, pack: dict[str, object], message: str) -> None:
    _write(
        tmp_path, "generic.yaml", {"id": "generic", "name": "Generic", "categories": [_cat("p")]}
    )
    _write(tmp_path, "x.yaml", pack)
    with pytest.raises(ConfigError, match=message):
        load_packs(tmp_path)


def test_extends_cycle_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "generic.yaml", {"id": "generic", "name": "G", "categories": [_cat("p")]})
    _write(tmp_path, "a.yaml", {"id": "a", "name": "A", "extends": "b"})
    _write(tmp_path, "b.yaml", {"id": "b", "name": "B", "extends": "a"})
    with pytest.raises(ConfigError, match="cycle"):
        load_packs(tmp_path)


def test_child_category_overrides_parent_by_id(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "generic.yaml",
        {"id": "generic", "name": "G", "categories": [_cat("p"), _cat("q")]},
    )
    _write(
        tmp_path,
        "x.yaml",
        {
            "id": "x",
            "name": "X",
            "extends": "generic",
            "hints": ["thing"],
            "categories": [_cat("p", must_have=True, weight=5)],
        },
    )
    checklist = load_packs(tmp_path).checklist("x")
    assert checklist.ids == ["p", "q"]
    assert "p" in checklist.must_have_ids


def test_pack_model_helpers() -> None:
    category = Category(id="a", name="a", probes=["p"], applies_to=["s"], must_have_in=["s"])
    assert category.applies("s")
    assert not category.applies(None)
    assert category.is_must_have("s")
    assert not category.is_must_have(None)
    assert Pack(id="x", name="X").extends is None
    assert PackSet is not None
