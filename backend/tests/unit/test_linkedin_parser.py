"""LinkedIn parsing on the verified contracts: ``ads_engine.connectors.linkedin.parser``.

Traceability (AAP 0.11.2): every payload comes from ``backend/tests/contracts/linkedin/`` through
the root ``contract`` fixture, or from the redacted live captures through ``captured_payloads``.
Each documented sample runs through its parser verbatim. The parser's docstrings name the only
two substitutions, both made here and nowhere else: the ``ad_analytics`` sample pivots by
organization, so its pivot value is replaced by a creative URN, and the single-pivot
``ad_analytics_statistics`` sample gains the demographic value a ``CAMPAIGN`` plus ``MEMBER_*``
request returns. Every hand-built element is validated against its contract's response schema
with ``simulation.payloads.validate_response`` before it is parsed, so it can carry no field the
contract does not define. Deliberately malformed inputs, built to reach a defect, are the one
exception.

What the module guarantees, in the order of the plan:

1. Documented samples and live captures parse with their surface's parser.
2. Form identity: a versioned form URN splits into the canonical URN and an integer version
   (AAP 0.3.3 "Form identity").
3. Lead decoding: the email is decoded only with the response's own form version, a decoration
   that does not describe the response is a "form map mismatch", and no plaintext email is ever
   retained (AAP 0.3.2 "Personal data", 0.3.3 "Consistency check").
4. Entities and analytics: status maps, budgets as exact decimals, UTC days, demographic facets
   and absent metrics as ``None``, never zero (AAP 0.11.1 "no silent defaults").

Identifiers are clearly fake (``99000…``), and the lead email is an ``example.com`` address that
exists only in memory. Everything is pure: no database, no network.
"""

import copy
import dataclasses
import hashlib
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import pytest

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.linkedin.parser import (
    CAMPAIGN_GROUP_SEARCH_STATUSES,
    CAMPAIGN_GROUP_STATUS_MAP,
    CAMPAIGN_SEARCH_STATUSES,
    CAMPAIGN_STATUS_MAP,
    CREATIVE_STATUS_MAP,
    PREFIX_ACCOUNT,
    PREFIX_CAMPAIGN,
    REASON_FORM_MAP_MISMATCH,
    REASON_NO_EMAIL_ANSWER,
    REASON_NO_EMAIL_QUESTION,
    REASON_UNKNOWN_FORM_VERSION,
    SEGMENT_FACETS,
    SEGMENT_PIVOTS,
    AdAccountRow,
    CampaignGroupRow,
    CampaignRow,
    CreativeDailyRow,
    CreativeRow,
    DecodedEmail,
    FormVersionMap,
    KnownVersions,
    LeadFormRow,
    Money,
    ParsedLead,
    PostRow,
    SegmentRow,
    canonical_form_urn,
    decode_email,
    form_map_from_decoration,
    parse_ad_account,
    parse_analytics_daily,
    parse_campaign,
    parse_campaign_group,
    parse_creative,
    parse_lead,
    parse_lead_form,
    parse_post,
    parse_segment,
    parse_versioned_form_urn,
    sponsored_account_urn,
    sponsored_campaign_urn,
    urn_id,
)
from ads_engine.simulation.payloads import Contract, validate_response

pytestmark = pytest.mark.contract

ContractLoader = Callable[[str, str], Contract]
"""The root ``contract`` fixture: ``contract(source, name)``."""

Conform = Callable[[str, dict[str, Any]], dict[str, Any]]
"""``conform(surface, element)``: validate a hand-built element and return it unchanged."""

# ---------------------------------------------------------------------------------------------
# Fake identities and the in-memory lead email
# ---------------------------------------------------------------------------------------------

ACCOUNT_URN = "urn:li:sponsoredAccount:990000001"
GROUP_URN = "urn:li:sponsoredCampaignGroup:990000101"
CAMPAIGN_URN = "urn:li:sponsoredCampaign:990000201"
OTHER_CAMPAIGN_URN = "urn:li:sponsoredCampaign:990000202"
CREATIVE_URN = "urn:li:sponsoredCreative:990001101"
OTHER_CREATIVE_URN = "urn:li:sponsoredCreative:990001102"
FORM_ID = 990000301
FORM_URN = f"urn:li:leadGenForm:{FORM_ID}"
LEAD_ID = "99000000-0000-0000-0000-000000000001-4"

EMAIL_QUESTION = 11
NAME_QUESTION = 12
WORK_EMAIL_QUESTION = 13
COMPANY_QUESTION = 14

RAW_EMAIL = "  Alice@Example.COM "
EMAIL = "alice@example.com"
FIRST_NAME = "Alice"
EMAIL_SHA256 = hashlib.sha256(EMAIL.encode("utf-8")).hexdigest()

SUBMITTED_MS = 1_780_000_000_123
"""2026-05-28T20:26:40.123Z as epoch milliseconds."""

# The documented batch sample nests these empty Rest.li maps inside `results`; the client skips
# them the same way, because no response id is ever named `statuses` or `errors`.
BATCH_META_KEYS = frozenset({"statuses", "errors"})

# Surfaces whose response body schema requires `paging` beside `elements`.
_PAGED_SURFACES = frozenset({"lead_forms", "lead_form_responses"})

# ---------------------------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------------------------


def _wrap(surface: str, element: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap one element in the response body its surface returns, for schema validation."""
    body: dict[str, Any]
    if surface == "ad_account":
        body = dict(element)
    elif surface in {"posts", "lead_form_responses_batch"}:
        # Batch-get bodies: posts keyed by URN, lead responses keyed by plain response id.
        body = {"results": {element["id"]: dict(element)}}
    else:
        body = {"elements": [dict(element)]}
        if surface in _PAGED_SURFACES:
            body["paging"] = {"start": 0, "count": 1}
    return body


@pytest.fixture
def conform(contract: ContractLoader) -> Conform:
    """Return ``conform(surface, element)``, which fails the test on a contract violation."""

    def _conform(surface: str, element: dict[str, Any]) -> dict[str, Any]:
        validate_response(contract("linkedin", surface), _wrap(surface, element))
        return element

    return _conform


def _sample(contract: ContractLoader, surface: str) -> dict[str, Any]:
    """A deep copy of a surface's documented sample, after checking it against its schema.

    The copy keeps the cached contract intact when a test substitutes a value.
    """
    loaded = contract("linkedin", surface)
    sample = loaded.response.documented_sample
    assert isinstance(sample, dict), f"linkedin.{surface} must publish a documented sample"
    validate_response(loaded, sample)
    return copy.deepcopy(sample)


def _batch_entries(results: Mapping[str, Any]) -> dict[str, Any]:
    """The response elements of a batch ``results`` map, keyed by plain response id."""
    return {key: value for key, value in results.items() if key not in BATCH_META_KEYS}


def _known_from_decorations(elements: Sequence[Mapping[str, Any]]) -> KnownVersions:
    """Build ``KnownVersions`` from each element's own ``form`` decoration.

    The version is parsed from that element's ``versionedLeadGenFormUrn``, never from the
    decoration, which carries no version field.
    """
    known: dict[tuple[str, int], FormVersionMap] = {}
    for element in elements:
        form = element.get("form")
        if form is None:
            continue
        canonical, version = parse_versioned_form_urn(element["versionedLeadGenFormUrn"])
        known[(canonical, version)] = form_map_from_decoration(canonical, version, form)
    return known


def _defect(call: Callable[[], object]) -> QueryDefect:
    """Run ``call``, expecting the LinkedIn validation defect, and return it."""
    with pytest.raises(QueryDefect) as caught:
        call()
    assert caught.value.code == "validation"
    assert caught.value.source == "linkedin"
    return caught.value


def _strings(value: object) -> Iterator[str]:
    """Every string inside a nested structure of mappings, sequences and sets."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _strings(item)


# ---------------------------------------------------------------------------------------------
# Hand-built lead responses
# ---------------------------------------------------------------------------------------------


def _versioned(version: int, form_id: int = FORM_ID) -> str:
    """``urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{form_id},{version})``."""
    return f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{form_id},{version})"


def _question(question_id: int, predefined: str, name: str) -> dict[str, Any]:
    """One decorated or ``leadForms`` question."""
    return {"questionId": question_id, "name": name, "predefinedField": predefined}


def _text_answer(question_id: int, text: str) -> dict[str, Any]:
    """One free-text answer, the shape email answers arrive in."""
    return {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": text}}}


def _default_questions() -> list[dict[str, Any]]:
    """An email and a first-name question."""
    return [
        _question(EMAIL_QUESTION, "EMAIL", "email"),
        _question(NAME_QUESTION, "FIRST_NAME", "firstName"),
    ]


def _default_answers() -> list[dict[str, Any]]:
    """The email (untrimmed, mixed case) and the first name."""
    return [_text_answer(EMAIL_QUESTION, RAW_EMAIL), _text_answer(NAME_QUESTION, FIRST_NAME)]


def _lead(
    *,
    version: int = 4,
    answers: list[dict[str, Any]] | None = None,
    questions: list[dict[str, Any]] | None = None,
    decorated: bool = True,
    creative: str | None = CREATIVE_URN,
    test_lead: bool = False,
) -> dict[str, Any]:
    """A sponsored lead response as the ``LEAD_FIELDS`` projection returns it.

    Args:
        version: The form version in ``versionedLeadGenFormUrn``.
        answers: ``formResponse.answers``; the email and first name by default.
        questions: The decoration's questions; an email and a first-name question by default.
        decorated: Whether the ``form`` decoration is present.
        creative: ``associatedEntity.associatedCreative``; ``None`` omits ``associatedEntity``.
        test_lead: ``testLead``.
    """
    element: dict[str, Any] = {
        "id": LEAD_ID,
        "submittedAt": SUBMITTED_MS,
        "testLead": test_lead,
        "leadType": "SPONSORED",
        "owner": {"sponsoredAccount": ACCOUNT_URN},
        "versionedLeadGenFormUrn": _versioned(version),
        "leadMetadata": {"sponsoredLeadMetadata": {"campaign": CAMPAIGN_URN}},
        "formResponse": {"answers": _default_answers() if answers is None else answers},
    }
    if creative is not None:
        element["associatedEntity"] = {"associatedCreative": creative}
    if decorated:
        element["form"] = {
            "id": FORM_ID,
            "name": "Fake white paper form",
            "content": {"questions": _default_questions() if questions is None else questions},
        }
    return element


def _lead_form(
    *, version: int = 4, questions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A ``leadForms?q=owner`` element describing its current version."""
    return {
        "id": FORM_ID,
        "versionId": version,
        "name": "Fake white paper form",
        "state": "PUBLISHED",
        "owner": {"sponsoredAccount": ACCOUNT_URN},
        "content": {"questions": _default_questions() if questions is None else questions},
    }


# ---------------------------------------------------------------------------------------------
# Live captures: the parser matching each captured surface
# ---------------------------------------------------------------------------------------------

_MEMBER_PIVOT_RE = re.compile(r"\b(MEMBER_[A-Z_]+)\b")
_CAMPAIGN_FACET_RE = re.compile(r"campaigns=List\((urn:li:sponsoredCampaign:[0-9]+)\)")


def _capture_elements(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The ``elements`` of a captured page body."""
    elements = body.get("elements", [])
    assert isinstance(elements, list), "a captured page body must carry an elements array"
    return elements


def _parse_captures(captures: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Parse every successful capture with its surface's parser; count parsed items per surface.

    Ad accounts are parsed first, so a campaign group's bare ``dailyBudget`` is read in the
    currency of its own captured account. ``access_token`` belongs to ``linkedin/auth.py`` and
    carries nothing this parser reads.
    """
    successful = [
        capture for capture in captures if 200 <= int(capture["response"]["status"]) < 300
    ]
    currencies: dict[str, str] = {}
    for capture in successful:
        if capture["surface"] == "linkedin.ad_account":
            account = parse_ad_account(capture["response"]["body"])
            currencies[account.urn] = account.currency
    counts: dict[str, int] = {}
    for capture in successful:
        source, _dot, name = str(capture["surface"]).partition(".")
        assert source == "linkedin", f"a LinkedIn capture names {capture['surface']!r}"
        body = capture["response"]["body"]
        url = unquote(str(capture["request"]["url"]))
        parsed = 0
        if name == "access_token":
            continue
        if name == "ad_account":
            parse_ad_account(body)
            parsed = 1
        elif name == "ad_campaign_groups":
            for element in _capture_elements(body):
                parse_campaign_group(element, account_currency=currencies.get(element["account"]))
                parsed += 1
        elif name == "ad_campaigns":
            for element in _capture_elements(body):
                parse_campaign(element, account_currency=currencies.get(element["account"]))
                parsed += 1
        elif name == "creatives":
            for element in _capture_elements(body):
                parse_creative(element)
                parsed += 1
        elif name == "posts":
            for key, element in body["results"].items():
                parse_post(key, element)
                parsed += 1
        elif name == "ad_analytics":
            for element in _capture_elements(body):
                parse_analytics_daily(element)
                parsed += 1
        elif name == "ad_analytics_campaign_delivery":
            for element in _capture_elements(body):
                (campaign_urn,) = element["pivotValues"]
                assert sponsored_campaign_urn(campaign_urn) == campaign_urn
                parsed += 1
        elif name in ("ad_analytics_statistics", "ad_analytics_member_segments"):
            pivots = _MEMBER_PIVOT_RE.findall(url)
            assert len(pivots) == 1, "a segment capture requests exactly one MEMBER_* pivot"
            campaign = _CAMPAIGN_FACET_RE.search(url)
            if name == "ad_analytics_member_segments":
                assert campaign is not None, "a member-segments capture names its one campaign"
            for element in _capture_elements(body):
                parse_segment(
                    element,
                    pivots[0],
                    campaign_urn=campaign.group(1) if campaign else None,
                    surface=name,
                )
                parsed += 1
        elif name == "lead_forms":
            for element in _capture_elements(body):
                parse_lead_form(element)
                parsed += 1
        elif name == "lead_form_responses":
            elements = _capture_elements(body)
            known = _known_from_decorations(elements)
            for element in elements:
                parse_lead(element, known)
                parsed += 1
        elif name == "lead_form_responses_batch":
            entries = _batch_entries(body["results"])
            known = _known_from_decorations(list(entries.values()))
            for element in entries.values():
                parse_lead(element, known, surface="lead_form_responses_batch")
                decode_email(element, known)
                parsed += 1
        else:
            pytest.fail(f"no parser covers the captured surface {capture['surface']!r}")
        counts[name] = counts.get(name, 0) + parsed
    return counts


def test_live_captures_parse_with_their_surface_parser(
    captured_payloads: Callable[[str], list[dict[str, Any]]],
) -> None:
    """Every redacted live capture parses; none exist until ``capture-fixtures`` has run."""
    captures = captured_payloads("linkedin")
    counts = _parse_captures(captures)
    assert set(counts) <= {str(capture["surface"]).partition(".")[2] for capture in captures}


def test_capture_dispatch_parses_every_surface_shape(
    contract: ContractLoader, conform: Conform
) -> None:
    """The capture dispatcher reaches every parser, proven on captures built from the samples.

    The dispatcher above otherwise runs only once live captures exist, so it is exercised here
    on capture records whose bodies are the documented samples (with the two documented
    substitutions), plus an error response that is skipped and an access-token exchange. The
    two segment-fallback surfaces share the organization-pivoted Analytics Finder sample, so
    their bodies are hand-built elements checked against their own contracts.
    """
    analytics = _sample(contract, "ad_analytics")
    for element in analytics["elements"]:
        element["pivotValues"] = [CREATIVE_URN]
    statistics = _sample(contract, "ad_analytics_statistics")
    for element in statistics["elements"]:
        element["pivotValues"] = [*element["pivotValues"], "urn:li:seniority:5"]
    bodies: dict[str, Any] = {
        "ad_account": _sample(contract, "ad_account"),
        "ad_campaign_groups": _sample(contract, "ad_campaign_groups"),
        "ad_campaigns": _sample(contract, "ad_campaigns"),
        "creatives": _sample(contract, "creatives"),
        "posts": _sample(contract, "posts"),
        "ad_analytics": analytics,
        "ad_analytics_statistics": statistics,
        "ad_analytics_campaign_delivery": {
            "elements": [
                conform(
                    "ad_analytics_campaign_delivery",
                    {"pivotValues": [CAMPAIGN_URN], "impressions": 400},
                ),
                conform(
                    "ad_analytics_campaign_delivery",
                    {"pivotValues": [OTHER_CAMPAIGN_URN], "impressions": 0},
                ),
            ]
        },
        "ad_analytics_member_segments": {
            "elements": [
                conform(
                    "ad_analytics_member_segments",
                    {"pivotValues": ["urn:li:seniority:5"], "impressions": 120, "clicks": 3},
                )
            ]
        },
        "lead_forms": _sample(contract, "lead_forms"),
        "lead_form_responses": _sample(contract, "lead_form_responses"),
        "lead_form_responses_batch": _sample(contract, "lead_form_responses_batch"),
    }
    urls = {
        "ad_analytics_statistics": (
            "https://api.linkedin.com/rest/adAnalytics?q=statistics"
            "&pivots=List(CAMPAIGN,MEMBER_SENIORITY)&timeGranularity=ALL"
        ),
        "ad_analytics_campaign_delivery": (
            "https://api.linkedin.com/rest/adAnalytics?q=analytics&pivot=CAMPAIGN"
            "&timeGranularity=ALL&accounts=List(urn%3Ali%3AsponsoredAccount%3A990000001)"
        ),
        "ad_analytics_member_segments": (
            "https://api.linkedin.com/rest/adAnalytics?q=analytics&pivot=MEMBER_SENIORITY"
            "&timeGranularity=ALL&campaigns=List(urn%3Ali%3AsponsoredCampaign%3A990000201)"
        ),
    }
    captures: list[dict[str, Any]] = [
        {
            "surface": f"linkedin.{name}",
            "captured_at": "2026-10-01T00:00:00Z",
            "request": {
                "method": "GET",
                "url": urls.get(name, f"https://api.linkedin.com/rest/{name}"),
            },
            "response": {"status": 200, "body": body},
        }
        for name, body in bodies.items()
    ]
    captures.append(
        {
            "surface": "linkedin.ad_campaigns",
            "captured_at": "2026-10-01T00:00:00Z",
            "request": {"method": "GET", "url": "https://api.linkedin.com/rest/adCampaigns"},
            "response": {"status": 429, "body": {"message": "throttled"}},
        }
    )
    captures.append(
        {
            "surface": "linkedin.access_token",
            "captured_at": "2026-10-01T00:00:00Z",
            "request": {"method": "POST", "url": "https://www.linkedin.com/oauth/v2/accessToken"},
            "response": {"status": 200, "body": {"access_token": "REDACTED", "expires_in": 1}},
        }
    )
    counts = _parse_captures(captures)
    assert counts == {
        "ad_account": 1,
        "ad_campaign_groups": 2,
        "ad_campaigns": 1,
        "creatives": 1,
        "posts": 1,
        "ad_analytics": 2,
        "ad_analytics_statistics": 1,
        "ad_analytics_campaign_delivery": 2,
        "ad_analytics_member_segments": 1,
        "lead_forms": 1,
        "lead_form_responses": 1,
        "lead_form_responses_batch": 1,
    }


def test_capture_dispatch_fails_on_a_surface_no_parser_covers() -> None:
    """A capture of a surface this parser does not know fails loudly, never silently passes."""
    capture = {
        "surface": "linkedin.unknown_surface",
        "captured_at": "2026-10-01T00:00:00Z",
        "request": {"method": "GET", "url": "https://api.linkedin.com/rest/unknown"},
        "response": {"status": 200, "body": {}},
    }
    with pytest.raises(pytest.fail.Exception, match="unknown_surface"):
        _parse_captures([capture])


# ---------------------------------------------------------------------------------------------
# Phase 1: documented samples, verbatim
# ---------------------------------------------------------------------------------------------


def test_documented_lead_responses_sample_parses_verbatim(contract: ContractLoader) -> None:
    """The Lead Sync "Find Lead Form Responses by Owner" sample parses with its own version.

    The sample carries no ``form`` decoration, so ``KnownVersions`` built from decorations is
    empty and its email cannot be decoded: ``no_email`` with "unknown form version", never a
    guess from another map.
    """
    sample = _sample(contract, "lead_form_responses")
    elements = sample["elements"]
    known = _known_from_decorations(elements)
    real_leads = 0
    for element in elements:
        lead = parse_lead(element, known)
        if element["testLead"]:
            assert lead is None
            continue
        assert isinstance(lead, ParsedLead)
        real_leads += 1
        canonical, version = parse_versioned_form_urn(element["versionedLeadGenFormUrn"])
        assert (lead.lead_form_urn, lead.lead_form_version) == (canonical, version)
        assert lead.external_id == element["id"]
        if element.get("form") is None and (canonical, version) not in known:
            assert (lead.email_status, lead.email_reason) == (
                "no_email",
                REASON_UNKNOWN_FORM_VERSION,
            )
            assert lead.email_sha256 is None
            assert lead.version_map is None
    assert real_leads >= 1

    [lead] = [parse_lead(element, known) for element in elements]
    assert lead == ParsedLead(
        external_id="aaaabbbb-0000-cccc-1111-dddd2222eeee-5",
        submitted_at=datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=1686182358881),
        account_urn="urn:li:sponsoredAccount:522529623",
        campaign_urn="urn:li:sponsoredCampaign:367378525",
        creative_urn="urn:li:sponsoredCreative:121608865",
        lead_form_urn="urn:li:leadGenForm:3162",
        lead_form_version=1,
        email_sha256=None,
        email_status="no_email",
        email_reason=REASON_UNKNOWN_FORM_VERSION,
        version_map=None,
    )


def test_documented_lead_sample_decodes_with_a_stored_map_of_its_exact_version(
    contract: ContractLoader,
) -> None:
    """With a stored map for version 1, the sample is decoded with that map and nothing else.

    Its question 1 answer is free text, not an address, so the lead is "no email answer"; the
    answer itself is never returned, hashed or echoed.
    """
    [element] = _sample(contract, "lead_form_responses")["elements"]
    stored = FormVersionMap.from_storage("urn:li:leadGenForm:3162", 1, ["1"])
    lead = parse_lead(element, {(stored.canonical_urn, stored.version): stored})
    assert lead is not None
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_NO_EMAIL_ANSWER)
    assert "Hello World" not in repr(lead)
    other_version = FormVersionMap.from_storage("urn:li:leadGenForm:3162", 2, ["1"])
    unknown = parse_lead(element, {("urn:li:leadGenForm:3162", 2): other_version})
    assert unknown is not None
    assert unknown.email_reason == REASON_UNKNOWN_FORM_VERSION


def test_documented_batch_sample_parses_each_result_by_plain_id(contract: ContractLoader) -> None:
    """The "Get Multiple Lead Form responses" sample: each ``results`` value, keyed by plain id."""
    sample = _sample(contract, "lead_form_responses_batch")
    results = sample["results"]
    # The documented sample nests empty `statuses` and `errors` maps inside `results`.
    assert {key: results[key] for key in BATCH_META_KEYS if key in results} == {
        key: {} for key in BATCH_META_KEYS if key in results
    }
    entries = _batch_entries(results)
    assert entries, "the batch sample must hold at least one response"
    known = _known_from_decorations(list(entries.values()))
    for key, element in entries.items():
        assert not key.startswith("urn:"), "batch results are keyed by plain response ids"
        lead = parse_lead(element, known, surface="lead_form_responses_batch")
        if element["testLead"]:
            assert lead is None
            continue
        assert lead is not None
        assert lead.external_id == key
        canonical, version = parse_versioned_form_urn(
            element["versionedLeadGenFormUrn"], surface="lead_form_responses_batch"
        )
        assert (lead.lead_form_urn, lead.lead_form_version) == (canonical, version)
        decoded = decode_email(element, known)
        assert isinstance(decoded, DecodedEmail)
        assert (decoded.status, decoded.reason) == (lead.email_status, lead.email_reason)
        assert decoded.sha256 == lead.email_sha256


def test_documented_lead_forms_sample_maps_exactly_its_email_questions(
    contract: ContractLoader,
) -> None:
    """The ``leadForms`` sample yields its current version's map; it asks for no email."""
    sample = _sample(contract, "lead_forms")
    for element in sample["elements"]:
        row = parse_lead_form(element)
        assert isinstance(row, LeadFormRow)
        questions = element["content"]["questions"]
        assert row.canonical_urn == f"urn:li:leadGenForm:{element['id']}"
        assert row.external_id == str(element["id"])
        assert row.current_version == element["versionId"] == row.version_map.version
        assert row.version_map.canonical_urn == row.canonical_urn
        assert row.version_map.question_ids == frozenset(q["questionId"] for q in questions)
        assert row.version_map.email_question_ids == tuple(
            sorted(q["questionId"] for q in questions if q.get("predefinedField") == "EMAIL")
        )
    [row] = [parse_lead_form(element) for element in sample["elements"]]
    assert row.canonical_urn == "urn:li:leadGenForm:6755260984438374400"
    assert row.name == "LeadGen form for Nimbus 2000"
    assert row.version_map.email_question_ids == ()
    assert row.version_map.question_ids == frozenset({10548, 10540})


def test_hand_built_lead_form_maps_exactly_its_email_questions(conform: Conform) -> None:
    """Only ``EMAIL`` questions are email questions in a form that asks no work email."""
    element = conform(
        "lead_forms",
        _lead_form(
            version=7,
            questions=[
                _question(31, "EMAIL", "email"),
                _question(30, "FIRST_NAME", "firstName"),
                _question(29, "EMAIL", "emailAgain"),
                _question(32, "COMPANY_NAME", "company"),
            ],
        ),
    )
    row = parse_lead_form(element)
    assert row.current_version == 7
    assert row.version_map.email_question_ids == (29, 31)
    assert row.version_map.question_ids == frozenset({29, 30, 31, 32})


def test_documented_analytics_sample_with_a_creative_pivot(contract: ContractLoader) -> None:
    """Daily creative analytics: exact decimal cost, the UTC day of ``dateRange.start``, ints."""
    sample = _sample(contract, "ad_analytics")
    # Verbatim, the sample pivots by organization, which no fact_creative_daily row can key.
    for element in sample["elements"]:
        _defect(lambda element=element: parse_analytics_daily(element))
    for element in sample["elements"]:
        element["pivotValues"] = [CREATIVE_URN]
    validate_response(contract("linkedin", "ad_analytics"), sample)
    first, second = (parse_analytics_daily(element) for element in sample["elements"])
    assert isinstance(second, CreativeDailyRow)
    assert second.creative_urn == CREATIVE_URN
    assert second.cost == Decimal("19.91833")
    assert isinstance(second.cost, Decimal)
    assert first.cost == Decimal("0.0")
    assert type(second.date) is date
    assert (first.date, second.date) == (date(2024, 5, 28), date(2024, 5, 29))
    assert (second.impressions, second.landing_page_clicks) == (165, 11)
    assert type(second.impressions) is int
    assert type(second.landing_page_clicks) is int
    # A returned zero stays zero; a metric the element omits is unavailable, never zero.
    assert second.website_conversions == 0
    assert (second.clicks, second.form_opens, second.leads) == (None, None, None)


def test_documented_statistics_sample_with_its_demographic_value(contract: ContractLoader) -> None:
    """Segment rows: the campaign and value from ``pivotValues``; absent metrics are ``None``."""
    sample = _sample(contract, "ad_analytics_statistics")
    [element] = sample["elements"]
    # Verbatim, the single-pivot sample names no demographic value.
    _defect(lambda: parse_segment(element, "MEMBER_SENIORITY"))
    element["pivotValues"] = [*element["pivotValues"], "urn:li:seniority:5"]
    validate_response(contract("linkedin", "ad_analytics_statistics"), sample)
    row = parse_segment(element, "MEMBER_SENIORITY")
    assert row == SegmentRow(
        campaign_urn="urn:li:sponsoredCampaign:1234567",
        facet="seniority",
        value="urn:li:seniority:5",
        impressions=54494,
        clicks=177,
        cost=None,
        leads=None,
        website_conversions=None,
    )


def test_documented_ad_account_sample(contract: ContractLoader) -> None:
    """``GET adAccounts/{id}``: the probe's row, with the currency of every amount below it."""
    row = parse_ad_account(_sample(contract, "ad_account"))
    assert row == AdAccountRow(
        external_id="123456",
        urn="urn:li:sponsoredAccount:123456",
        name="Company A",
        currency="USD",
        raw_status="ACTIVE",
        test=False,
    )


def test_documented_campaign_groups_sample(contract: ContractLoader) -> None:
    """Every documented group parses; run schedules are UTC instants from epoch milliseconds."""
    sample = _sample(contract, "ad_campaign_groups")
    rows = [parse_campaign_group(element) for element in sample["elements"]]
    assert all(isinstance(row, CampaignGroupRow) for row in rows)
    refresh, new = rows
    assert refresh.urn == "urn:li:sponsoredCampaignGroup:603407684"
    assert refresh.account_urn == "urn:li:sponsoredAccount:506223315"
    assert (refresh.status, refresh.raw_status) == ("active", "ACTIVE")
    assert refresh.run_start == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        milliseconds=1234567890987
    )
    assert refresh.run_end == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        milliseconds=9876543210123
    )
    assert (refresh.daily_budget, refresh.total_budget) == (None, None)
    assert new.run_end is None


def test_documented_campaigns_sample(contract: ContractLoader) -> None:
    """The documented campaign: a daily budget object as an exact decimal in its currency."""
    [element] = _sample(contract, "ad_campaigns")["elements"]
    row = parse_campaign(element, account_currency="USD")
    assert isinstance(row, CampaignRow)
    assert row.urn == "urn:li:sponsoredCampaign:145282384"
    assert row.campaign_group_urn == "urn:li:sponsoredCampaignGroup:603030884"
    assert row.daily_budget == Money(amount=Decimal("18"), currency="USD")
    assert row.total_budget is None
    assert row.objective_type is None
    assert (row.status, row.raw_status) == ("active", "ACTIVE")


def test_documented_creatives_sample(contract: ContractLoader) -> None:
    """The documented creative: a share-backed draft with no lead-gen call to action."""
    [element] = _sample(contract, "creatives")["elements"]
    row = parse_creative(element)
    assert row == CreativeRow(
        urn="urn:li:sponsoredCreative:119962155",
        campaign_urn="urn:li:sponsoredCampaign:360035215",
        account_urn="urn:li:sponsoredAccount:520866471",
        name=None,
        intended_status="DRAFT",
        status=CREATIVE_STATUS_MAP["DRAFT"],
        is_serving=False,
        post_urn="urn:li:share:6778045555198214144",
        lead_form_urn=None,
        cta_label=None,
    )


def test_documented_posts_sample(contract: ContractLoader) -> None:
    """The documented post: commentary is the intro text; empty content has no headline."""
    sample = _sample(contract, "posts")
    rows = [parse_post(key, element) for key, element in sample["results"].items()]
    assert rows == [
        PostRow(
            urn="urn:li:share:6844785523593134080",
            headline=None,
            intro_text="Test Org commentary-Updated Config",
        )
    ]


# ---------------------------------------------------------------------------------------------
# Phase 2: form identity
# ---------------------------------------------------------------------------------------------


def test_parse_versioned_form_urn_splits_the_canonical_urn_and_version() -> None:
    """The version is an integer taken only from the versioned URN."""
    parsed = parse_versioned_form_urn("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123,4)")
    assert parsed == ("urn:li:leadGenForm:123", 4)
    assert type(parsed[1]) is int


@pytest.mark.parametrize(
    "urn",
    [
        pytest.param("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123)", id="missing-version"),
        pytest.param("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123,)", id="empty-version"),
        pytest.param("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123,v4)", id="non-int"),
        pytest.param("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123,-4)", id="negative"),
        pytest.param("urn:li:versionedForm:(urn:li:leadGenForm:123,4)", id="wrong-prefix"),
        pytest.param("urn:li:versionedLeadGenForm:(urn:li:adForm:123,4)", id="wrong-inner"),
        pytest.param("urn:li:leadGenForm:123", id="canonical-not-versioned"),
        pytest.param(" urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123,4)", id="padded"),
        pytest.param("", id="empty"),
    ],
)
def test_parse_versioned_form_urn_rejects_malformed_urns(urn: str) -> None:
    """A malformed versioned URN is a validation defect on the lead-responses surface."""
    defect = _defect(lambda: parse_versioned_form_urn(urn))
    assert defect.endpoint == "lead_form_responses"


def test_parse_versioned_form_urn_rejects_a_version_beyond_64_bits() -> None:
    """Versions are Rest.li longs; a larger numeral is a defect, never a Python big int."""
    _defect(lambda: parse_versioned_form_urn(_versioned(2**63)))
    assert parse_versioned_form_urn(_versioned(2**63 - 1))[1] == 2**63 - 1


def test_parse_versioned_form_urn_names_the_given_surface_and_checks_it() -> None:
    """Defects name the surface the URN came from; an unknown surface is a caller bug."""
    defect = _defect(lambda: parse_versioned_form_urn("bad", surface="lead_form_responses_batch"))
    assert defect.endpoint == "lead_form_responses_batch"
    with pytest.raises(ValueError, match="unknown LinkedIn contract surface"):
        parse_versioned_form_urn(_versioned(1), surface="leadFormResponses")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("urn:li:adForm:123", id="ad-form"),
        pytest.param("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:123,4)", id="versioned"),
        pytest.param("urn:li:leadGenForm:123", id="canonical"),
    ],
)
def test_canonical_form_urn_names_one_form_for_every_reference(value: str) -> None:
    """Every reference form names ``urn:li:leadGenForm:123``; a canonical URN is unchanged."""
    assert canonical_form_urn(value) == "urn:li:leadGenForm:123"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("urn:li:adForm:abc", id="non-numeric-ad-form"),
        pytest.param("urn:li:leadGenForm:", id="empty-id"),
        pytest.param("urn:li:sponsoredCreative:123", id="other-entity"),
        pytest.param(123, id="not-a-string"),
    ],
)
def test_canonical_form_urn_rejects_other_references(value: Any) -> None:
    """Anything else is a validation defect on the given surface."""
    defect = _defect(lambda: canonical_form_urn(value, surface="creatives"))
    assert defect.endpoint == "creatives"
    with pytest.raises(ValueError, match="unknown LinkedIn contract surface"):
        canonical_form_urn("urn:li:leadGenForm:1", surface="nowhere")


def test_form_version_map_storage_round_trip() -> None:
    """``lead_form_version.email_question_ids`` (``text[]``) keeps the preference order."""
    stored = FormVersionMap.from_storage(FORM_URN, 4, ["13", "11"])
    assert stored == FormVersionMap(FORM_URN, 4, (13, 11), None)
    assert stored.stored_email_question_ids == ["13", "11"]
    with pytest.raises(ValueError, match="digit strings"):
        FormVersionMap.from_storage(FORM_URN, 4, ["eleven"])


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(("urn:li:adForm:1", 1, ()), "canonical_urn", id="non-canonical"),
        pytest.param((FORM_URN, -1, ()), "non-negative", id="negative-version"),
        pytest.param((FORM_URN, True, ()), "non-negative", id="boolean-version"),
        pytest.param((FORM_URN, 1, (11, 11)), "repeat", id="repeated-email-id"),
        pytest.param((FORM_URN, 1, (11,), frozenset({12})), "among", id="email-outside"),
    ],
)
def test_form_version_map_validates_its_identity(arguments: tuple[Any, ...], message: str) -> None:
    """A map with a malformed identity or inconsistent ids cannot be built."""
    with pytest.raises(ValueError, match=message):
        FormVersionMap(*arguments)


# ---------------------------------------------------------------------------------------------
# Phase 3: lead decoding and the decoration consistency check
# ---------------------------------------------------------------------------------------------


def test_decorated_lead_hashes_the_trimmed_lowercased_email(conform: Conform) -> None:
    """A decorated version-4 response decodes with its own map; only the hash is kept."""
    lead = parse_lead(conform("lead_form_responses", _lead()), {})
    assert lead is not None
    assert lead.email_status == "ok"
    assert lead.email_reason is None
    assert lead.email_sha256 == hashlib.sha256(b"alice@example.com").hexdigest() == EMAIL_SHA256
    assert (lead.lead_form_urn, lead.lead_form_version) == (FORM_URN, 4)
    # The decoration's map passed the check, so sync can persist it as lead_form_version.
    assert lead.version_map == FormVersionMap(
        FORM_URN, 4, (EMAIL_QUESTION,), frozenset({EMAIL_QUESTION, NAME_QUESTION})
    )


def test_an_answer_outside_the_decoration_is_a_form_map_mismatch(conform: Conform) -> None:
    """The decoration does not describe this response, so it is never decoded with any map.

    ``KnownVersions`` holds version 3, whose map does contain the answered question: it is
    another version's map and must not be used.
    """
    element = conform(
        "lead_form_responses",
        _lead(answers=[*_default_answers(), _text_answer(WORK_EMAIL_QUESTION, "bob@example.com")]),
    )
    version_3 = FormVersionMap(
        FORM_URN,
        3,
        (WORK_EMAIL_QUESTION,),
        frozenset({EMAIL_QUESTION, NAME_QUESTION, WORK_EMAIL_QUESTION}),
    )
    known = {(FORM_URN, 3): version_3}
    lead = parse_lead(element, known)
    assert lead is not None
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_FORM_MAP_MISMATCH)
    assert (lead.email_sha256, lead.version_map) == (None, None)
    assert lead.lead_form_version == 4
    decoded = decode_email(element, known)
    assert (decoded.email, decoded.sha256, decoded.status, decoded.reason) == (
        None,
        None,
        "no_email",
        REASON_FORM_MAP_MISMATCH,
    )


def test_an_answer_outside_the_stored_map_of_its_version_is_a_mismatch(conform: Conform) -> None:
    """Without a decoration, a stored map with known questions drives the same check."""
    element = conform(
        "lead_form_responses",
        _lead(
            decorated=False,
            answers=[*_default_answers(), _text_answer(COMPANY_QUESTION, "Fake Corp")],
        ),
    )
    stored = FormVersionMap(FORM_URN, 4, (EMAIL_QUESTION,), frozenset({11, 12}))
    lead = parse_lead(element, {(FORM_URN, 4): stored})
    assert lead is not None
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_FORM_MAP_MISMATCH)


@pytest.mark.parametrize(
    "known",
    [
        pytest.param({}, id="no-maps"),
        pytest.param(
            {(FORM_URN, 3): FormVersionMap(FORM_URN, 3, (EMAIL_QUESTION,))}, id="other-version"
        ),
        pytest.param(
            {
                ("urn:li:leadGenForm:990000399", 4): FormVersionMap(
                    "urn:li:leadGenForm:990000399", 4, (EMAIL_QUESTION,)
                )
            },
            id="other-form",
        ),
    ],
)
def test_no_decoration_and_no_map_of_that_version_is_an_unknown_version(
    conform: Conform, known: dict[tuple[str, int], FormVersionMap]
) -> None:
    """Only the exact ``(canonical_urn, version)`` map may decode an undecorated response."""
    lead = parse_lead(conform("lead_form_responses", _lead(decorated=False)), known)
    assert lead is not None
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_UNKNOWN_FORM_VERSION)
    assert lead.email_sha256 is None


def test_no_decoration_decodes_with_the_lead_forms_map_of_that_version(conform: Conform) -> None:
    """The ``leadForms`` sync supplies the current version's map, which decodes the lead."""
    form = parse_lead_form(conform("lead_forms", _lead_form(version=4)))
    known = {(form.canonical_urn, form.current_version): form.version_map}
    lead = parse_lead(conform("lead_form_responses", _lead(decorated=False)), known)
    assert lead is not None
    assert (lead.email_status, lead.email_sha256) == ("ok", EMAIL_SHA256)
    # Only a decoration teaches a new map; a stored one is already persisted.
    assert lead.version_map is None


def test_no_decoration_decodes_with_a_map_rebuilt_from_storage(conform: Conform) -> None:
    """A stored map knows only its email ids, so no consistency check can apply to it."""
    stored = FormVersionMap.from_storage(FORM_URN, 4, [str(EMAIL_QUESTION)])
    element = conform("lead_form_responses", _lead(decorated=False))
    lead = parse_lead(element, {(FORM_URN, 4): stored})
    assert lead is not None
    assert lead.email_sha256 == EMAIL_SHA256


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param([_text_answer(NAME_QUESTION, FIRST_NAME)], id="email-unanswered"),
        pytest.param([_text_answer(EMAIL_QUESTION, "   ")], id="blank"),
        pytest.param([_text_answer(EMAIL_QUESTION, "alice.example.com")], id="no-at"),
        pytest.param([_text_answer(EMAIL_QUESTION, "alice@@example.com")], id="two-ats"),
        pytest.param([_text_answer(EMAIL_QUESTION, "@example.com")], id="no-local-part"),
        pytest.param(
            [
                {
                    "questionId": EMAIL_QUESTION,
                    "answerDetails": {"multipleChoiceAnswer": {"options": [1]}},
                }
            ],
            id="not-free-text",
        ),
    ],
)
def test_no_usable_email_answer_is_no_email(
    conform: Conform, answers: list[dict[str, Any]]
) -> None:
    """An email question without a usable address is ``no_email``; the map is still learned."""
    lead = parse_lead(conform("lead_form_responses", _lead(answers=answers)), {})
    assert lead is not None
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_NO_EMAIL_ANSWER)
    assert lead.email_sha256 is None
    assert lead.version_map is not None


def test_a_form_without_an_email_question_is_no_email(conform: Conform) -> None:
    """A version that asks for no email can never be matched to a HubSpot contact."""
    element = conform(
        "lead_form_responses",
        _lead(
            questions=[_question(NAME_QUESTION, "FIRST_NAME", "firstName")],
            answers=[_text_answer(NAME_QUESTION, FIRST_NAME)],
        ),
    )
    lead = parse_lead(element, {})
    assert lead is not None
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_NO_EMAIL_QUESTION)
    assert lead.version_map is not None
    assert lead.version_map.email_question_ids == ()


def test_a_work_email_is_used_when_the_email_answer_is_unusable(conform: Conform) -> None:
    """``EMAIL`` is preferred, and ``WORK_EMAIL`` is the documented fallback."""
    questions = [
        *_default_questions(),
        _question(WORK_EMAIL_QUESTION, "WORK_EMAIL", "workEmail"),
    ]
    answers = [
        _text_answer(EMAIL_QUESTION, "not an address"),
        _text_answer(WORK_EMAIL_QUESTION, RAW_EMAIL),
    ]
    lead = parse_lead(
        conform("lead_form_responses", _lead(questions=questions, answers=answers)), {}
    )
    assert lead is not None
    assert lead.email_sha256 == EMAIL_SHA256
    assert lead.version_map is not None
    assert lead.version_map.email_question_ids == (EMAIL_QUESTION, WORK_EMAIL_QUESTION)


def test_no_plaintext_email_or_answer_is_retained(conform: Conform) -> None:
    """Neither the address nor any free-text answer survives into the parsed lead."""
    lead = parse_lead(conform("lead_form_responses", _lead()), {})
    assert lead is not None
    retained = [*_strings(dataclasses.asdict(lead)), repr(lead), str(lead)]
    for text in retained:
        assert "alice" not in text.lower(), "the parsed lead retains the email or first name"
        assert "example.com" not in text.lower()
    assert not {field.name for field in dataclasses.fields(ParsedLead)} & {"email", "answers"}


def test_decode_email_returns_the_normalised_plaintext_with_the_same_hash(
    conform: Conform,
) -> None:
    """``decode_email`` is the only function returning plaintext; its hash is the stored one."""
    element = conform("lead_form_responses", _lead())
    lead = parse_lead(element, {})
    decoded = decode_email(element, {}, surface="lead_form_responses")
    assert lead is not None
    assert decoded == DecodedEmail(email=EMAIL, sha256=EMAIL_SHA256, status="ok", reason=None)
    assert decoded.sha256 == lead.email_sha256
    # The address never reaches a repr, so a traceback or log line cannot carry it.
    assert EMAIL not in repr(decoded)
    batch = conform("lead_form_responses_batch", _lead())
    assert decode_email(batch, {}).email == EMAIL


def test_lead_instants_and_links(conform: Conform) -> None:
    """``submittedAt`` is a UTC instant; the campaign and creative come from their links."""
    lead = parse_lead(conform("lead_form_responses", _lead()), {})
    assert lead is not None
    assert lead.submitted_at == datetime(2026, 5, 28, 20, 26, 40, 123000, tzinfo=UTC)
    assert lead.submitted_at.utcoffset() == timedelta(0)
    assert lead.campaign_urn == CAMPAIGN_URN
    assert lead.creative_urn == CREATIVE_URN
    assert lead.account_urn == ACCOUNT_URN
    assert lead.external_id == LEAD_ID


def test_an_absent_or_null_creative_is_the_unknown_creative(conform: Conform) -> None:
    """A lead without ``associatedEntity.associatedCreative`` credits the unknown creative."""
    absent = parse_lead(conform("lead_form_responses", _lead(creative=None)), {})
    null_element = _lead(creative=None)
    null_element["associatedEntity"] = {"associatedCreative": None}
    null = parse_lead(conform("lead_form_responses", null_element), {})
    assert absent is not None
    assert absent.creative_urn is None
    assert null is not None
    assert null.creative_urn is None


def test_projected_links_are_read_and_must_agree(conform: Conform) -> None:
    """Under ``LEAD_FIELDS`` only the ``*Info`` decorations carry the links; both must agree."""
    element = _lead(creative=None)
    del element["leadMetadata"]
    del element["owner"]["sponsoredAccount"]
    element["leadMetadataInfo"] = {
        "sponsoredLeadMetadataInfo": {"campaign": {"id": CAMPAIGN_URN, "name": "Fake"}}
    }
    element["associatedEntityInfo"] = {"associatedCreative": {"id": CREATIVE_URN}}
    lead = parse_lead(conform("lead_form_responses", element), {})
    assert lead is not None
    assert (lead.campaign_urn, lead.creative_urn, lead.account_urn) == (
        CAMPAIGN_URN,
        CREATIVE_URN,
        None,
    )
    disagreeing_campaign = copy.deepcopy(element)
    disagreeing_campaign["leadMetadata"] = {
        "sponsoredLeadMetadata": {"campaign": OTHER_CAMPAIGN_URN}
    }
    _defect(lambda: parse_lead(conform("lead_form_responses", disagreeing_campaign), {}))
    disagreeing_creative = copy.deepcopy(element)
    disagreeing_creative["associatedEntity"] = {"associatedCreative": OTHER_CREATIVE_URN}
    _defect(lambda: parse_lead(conform("lead_form_responses", disagreeing_creative), {}))


def test_test_leads_are_never_stored(conform: Conform) -> None:
    """``testLead`` true yields ``None`` before anything else is read."""
    assert parse_lead(conform("lead_form_responses", _lead(test_lead=True)), {}) is None
    malformed = {"testLead": True, "id": 5}
    assert parse_lead(malformed, {}) is None


_LEAD_DEFECTS: list[Any] = [
    pytest.param(lambda e: e.pop("id"), id="missing-id"),
    pytest.param(lambda e: e.update(id="   "), id="blank-id"),
    pytest.param(lambda e: e.update(id=5), id="non-string-id"),
    pytest.param(lambda e: e.pop("submittedAt"), id="missing-submitted-at"),
    pytest.param(lambda e: e.update(submittedAt="soon"), id="non-integer-submitted-at"),
    pytest.param(lambda e: e.update(submittedAt=10**17), id="instant-beyond-dates"),
    pytest.param(lambda e: e.update(testLead="no"), id="non-boolean-test-lead"),
    pytest.param(lambda e: e.pop("testLead"), id="missing-test-lead"),
    pytest.param(lambda e: e.pop("versionedLeadGenFormUrn"), id="missing-form-urn"),
    pytest.param(lambda e: e.pop("leadMetadata"), id="missing-campaign"),
    pytest.param(
        lambda e: e.update(leadMetadata={"sponsoredLeadMetadata": {"campaign": "urn:li:x:1"}}),
        id="malformed-campaign",
    ),
    pytest.param(lambda e: e.update(leadMetadata=[]), id="non-object-metadata"),
    pytest.param(lambda e: e.pop("formResponse"), id="missing-form-response"),
    pytest.param(lambda e: e.update(formResponse={"answers": {}}), id="non-array-answers"),
    pytest.param(lambda e: e.update(formResponse={"answers": [7]}), id="non-object-answer"),
    pytest.param(
        lambda e: e["formResponse"]["answers"].append(_text_answer(EMAIL_QUESTION, "x@y.z")),
        id="repeated-answer",
    ),
    pytest.param(
        lambda e: e["form"]["content"]["questions"].append(
            _question(EMAIL_QUESTION, "EMAIL", "again")
        ),
        id="repeated-decorated-question",
    ),
    pytest.param(lambda e: e["form"].pop("content"), id="decoration-without-content"),
    pytest.param(lambda e: e.update(form="decorated"), id="non-object-decoration"),
    pytest.param(
        lambda e: e["form"]["content"]["questions"].append(
            {"questionId": 15, "predefinedField": "EMAIL\x00"}
        ),
        id="nul-in-predefined-field",
    ),
    pytest.param(
        lambda e: e["formResponse"]["answers"][0]["answerDetails"]["textQuestionAnswer"].update(
            answer=7
        ),
        id="non-string-answer",
    ),
]


@pytest.mark.parametrize("break_element", _LEAD_DEFECTS)
@pytest.mark.parametrize("surface", ["lead_form_responses", "lead_form_responses_batch"])
def test_malformed_lead_responses_are_defects(
    surface: str, break_element: Callable[[dict[str, Any]], object]
) -> None:
    """A response that breaks its contract is a defect naming the surface, never a guess."""
    element = _lead()
    break_element(element)
    defect = _defect(lambda: parse_lead(element, {}, surface=surface))
    assert defect.endpoint == surface
    assert "alice" not in str(defect).lower()


def test_lead_parsers_reject_non_objects_and_other_surfaces() -> None:
    """Only a JSON object on a lead-responses surface can be parsed as a lead."""
    _defect(lambda: parse_lead([], {}))
    _defect(lambda: decode_email("lead", {}))
    with pytest.raises(ValueError, match="lead_form_responses"):
        parse_lead(_lead(), {}, surface="lead_forms")
    with pytest.raises(ValueError, match="lead_form_responses"):
        decode_email(_lead(), {}, surface="posts")


def test_an_unencodable_email_answer_is_no_email_not_a_defect(conform: Conform) -> None:
    """An answer UTF-8 cannot encode is never stored, so it is simply not an address."""
    element = conform("lead_form_responses", _lead())
    element["formResponse"]["answers"][0] = _text_answer(EMAIL_QUESTION, "alice\ud800@example.com")
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_reason == REASON_NO_EMAIL_ANSWER


# ---------------------------------------------------------------------------------------------
# Phase 4: entities and analytics
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def campaign_element(contract: ContractLoader) -> dict[str, Any]:
    """The documented campaign, re-identified with fake ids."""
    [element] = _sample(contract, "ad_campaigns")["elements"]
    element.update(id=990000201, account=ACCOUNT_URN, campaignGroup=GROUP_URN)
    return element


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ACTIVE", "active"),
        ("PAUSED", "paused"),
        ("DRAFT", "paused"),
        ("ARCHIVED", "ended"),
        ("COMPLETED", "ended"),
        ("CANCELED", "ended"),
        ("PENDING_DELETION", "ended"),
        ("REMOVED", "ended"),
    ],
)
def test_parse_campaign_normalises_status_and_keeps_the_raw_one(
    conform: Conform, campaign_element: dict[str, Any], raw: str, expected: str
) -> None:
    """Every campaign status maps to active, paused or ended; the raw status is kept."""
    campaign_element["status"] = raw
    row = parse_campaign(conform("ad_campaigns", campaign_element))
    assert (row.status, row.raw_status) == (expected, raw)
    assert CAMPAIGN_STATUS_MAP[raw] == expected


def test_parse_campaign_budgets_are_exact_decimals_in_their_currency(
    conform: Conform, campaign_element: dict[str, Any]
) -> None:
    """Daily and lifetime budgets keep every digit; the objective is kept raw."""
    campaign_element.update(
        dailyBudget={"amount": "125.50", "currencyCode": "EUR"},
        totalBudget={"amount": "5000.123456", "currencyCode": "EUR"},
        objectiveType="LEAD_GENERATION",
    )
    campaign_element.pop("runSchedule")
    row = parse_campaign(conform("ad_campaigns", campaign_element), account_currency="EUR")
    assert row.daily_budget == Money(amount=Decimal("125.50"), currency="EUR")
    assert row.total_budget == Money(amount=Decimal("5000.123456"), currency="EUR")
    assert isinstance(row.daily_budget.amount, Decimal)
    assert row.objective_type == "LEAD_GENERATION"
    assert (row.run_start, row.run_end) == (None, None)
    assert row.urn == "urn:li:sponsoredCampaign:990000201"
    assert row.account_urn == ACCOUNT_URN


def test_parse_campaign_never_converts_between_currencies(
    conform: Conform, campaign_element: dict[str, Any]
) -> None:
    """A budget in another currency than the account's is a defect; a bad code is a bug."""
    element = conform("ad_campaigns", campaign_element)
    _defect(lambda: parse_campaign(element, account_currency="EUR"))
    with pytest.raises(ValueError, match="ISO 4217"):
        parse_campaign(element, account_currency="usd")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("status", "PAUSED_FOREVER", id="unmapped-status"),
        pytest.param("status", "Some free text", id="unmapped-free-text"),
        pytest.param("id", True, id="boolean-id"),
        pytest.param("id", -5, id="negative-id"),
        pytest.param("id", "12a", id="non-numeric-id"),
        pytest.param("id", "1" * 20, id="id-beyond-64-bits"),
        pytest.param("account", "urn:li:organization:1", id="wrong-account-prefix"),
        pytest.param("name", "bad\x00name", id="nul-in-name"),
        pytest.param("name", "bad\ud800name", id="unencodable-name"),
        pytest.param("dailyBudget", "18", id="bare-daily-budget"),
        pytest.param("dailyBudget", {"amount": 18.5, "currencyCode": "USD"}, id="float-amount"),
        pytest.param("dailyBudget", {"amount": "-1", "currencyCode": "USD"}, id="negative"),
        pytest.param("dailyBudget", {"amount": "NaN", "currencyCode": "USD"}, id="not-a-number"),
        pytest.param("dailyBudget", {"amount": "1e12", "currencyCode": "USD"}, id="too-large"),
        pytest.param("dailyBudget", {"amount": "0.0000001", "currencyCode": "USD"}, id="scale"),
        pytest.param("dailyBudget", {"amount": "1" * 65, "currencyCode": "USD"}, id="too-long"),
        pytest.param(
            "dailyBudget", {"amount": "1e9999999999999999999", "currencyCode": "USD"}, id="exp"
        ),
        pytest.param("dailyBudget", {"amount": True, "currencyCode": "USD"}, id="boolean"),
        pytest.param("dailyBudget", {"amount": "1", "currencyCode": "usd"}, id="bad-currency"),
        pytest.param("dailyBudget", {"amount": "1"}, id="missing-currency"),
        pytest.param("runSchedule", {"end": 1}, id="schedule-without-start"),
        pytest.param("runSchedule", {"start": 10**17}, id="start-beyond-dates"),
        pytest.param("campaignGroup", 5, id="non-string-group"),
        pytest.param("objectiveType", ["LEAD_GENERATION"], id="non-string-objective"),
    ],
)
def test_malformed_campaigns_are_defects(
    campaign_element: dict[str, Any], key: str, value: object
) -> None:
    """Fields that break the contract or the text and numeric storage are defects."""
    campaign_element[key] = value
    defect = _defect(lambda: parse_campaign(campaign_element))
    assert "free text" not in str(defect).lower()


def test_parse_campaign_rejects_a_non_object() -> None:
    """A campaign must be a JSON object."""
    _defect(lambda: parse_campaign(["not", "an", "object"]))


def test_amount_forms_the_http_layer_produces(campaign_element: dict[str, Any]) -> None:
    """Integers and Decimals (JSON numbers as ``connectors/http.py`` decodes them) are exact.

    These are not JSON-native values, so they are checked against the parser directly.
    """
    campaign_element["dailyBudget"] = {"amount": 18, "currencyCode": "USD"}
    campaign_element["totalBudget"] = {"amount": Decimal("100.500000000"), "currencyCode": "USD"}
    row = parse_campaign(campaign_element)
    assert row.daily_budget == Money(Decimal(18), "USD")
    assert row.total_budget is not None
    assert str(row.total_budget.amount) == "100.500000000"
    campaign_element["dailyBudget"] = {"amount": "-0E+19", "currencyCode": "USD"}
    zero = parse_campaign(campaign_element).daily_budget
    assert zero is not None
    assert zero.amount.is_zero()
    assert not zero.amount.is_signed()
    campaign_element["dailyBudget"] = {"amount": Decimal("Infinity"), "currencyCode": "USD"}
    _defect(lambda: parse_campaign(campaign_element))
    campaign_element["id"] = Decimal("990000201")
    campaign_element["dailyBudget"] = None
    assert parse_campaign(campaign_element).external_id == "990000201"
    for bad_id in (Decimal("1.5"), Decimal("1E+30"), 2**63):
        campaign_element["id"] = bad_id
        _defect(lambda: parse_campaign(campaign_element))


def test_search_status_lists() -> None:
    """Campaigns are requested in all eight statuses; groups in the seven documented ones."""
    assert set(CAMPAIGN_SEARCH_STATUSES) == {
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "COMPLETED",
        "CANCELED",
        "DRAFT",
        "PENDING_DELETION",
        "REMOVED",
    }
    assert len(CAMPAIGN_SEARCH_STATUSES) == 8
    assert {
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "CANCELED",
        "DRAFT",
        "PENDING_DELETION",
        "REMOVED",
    } <= set(CAMPAIGN_GROUP_SEARCH_STATUSES)
    assert set(CAMPAIGN_SEARCH_STATUSES) <= set(CAMPAIGN_STATUS_MAP)
    assert set(CAMPAIGN_GROUP_SEARCH_STATUSES) <= set(CAMPAIGN_GROUP_STATUS_MAP)


@pytest.mark.parametrize(
    ("surface", "statuses"),
    [
        ("ad_campaigns", CAMPAIGN_SEARCH_STATUSES),
        ("ad_campaign_groups", CAMPAIGN_GROUP_SEARCH_STATUSES),
    ],
)
def test_search_status_lists_equal_the_contract_requests(
    contract: ContractLoader, surface: str, statuses: tuple[str, ...]
) -> None:
    """The status list each contract's request records is exactly the one the client sends."""
    url = contract("linkedin", surface).request.url
    match = re.search(r"status:\(values:List\(([A-Z_,]+)\)\)", url)
    assert match is not None, f"{surface} must record its status search"
    assert tuple(match.group(1).split(",")) == statuses


def test_campaign_group_budgets(conform: Conform, contract: ContractLoader) -> None:
    """A group's lifetime budget is an object; its bare daily budget is in the account currency."""
    [element, _other] = _sample(contract, "ad_campaign_groups")["elements"]
    element.update(
        id=990000101,
        account=ACCOUNT_URN,
        dailyBudget="250.75",
        totalBudget={"amount": "10000", "currencyCode": "GBP"},
        objectiveType="LEAD_GENERATION",
    )
    conform("ad_campaign_groups", element)
    row = parse_campaign_group(element, account_currency="GBP")
    assert row.urn == GROUP_URN
    assert row.daily_budget == Money(Decimal("250.75"), "GBP")
    assert row.total_budget == Money(Decimal("10000"), "GBP")
    # A bare amount has no currency of its own, so it cannot be read without the account's.
    _defect(lambda: parse_campaign_group(element))
    _defect(lambda: parse_campaign_group(element, account_currency="USD"))
    element["totalBudget"] = "10000"
    _defect(lambda: parse_campaign_group(element, account_currency="GBP"))
    with pytest.raises(ValueError, match="ISO 4217"):
        parse_campaign_group(element, account_currency="GB")


@pytest.mark.parametrize("raw", sorted(CAMPAIGN_GROUP_STATUS_MAP))
def test_campaign_group_statuses(conform: Conform, contract: ContractLoader, raw: str) -> None:
    """Every documented group status, the CANCELLED spelling included, normalises."""
    [element, _other] = _sample(contract, "ad_campaign_groups")["elements"]
    element["status"] = raw
    row = parse_campaign_group(conform("ad_campaign_groups", element))
    assert (row.status, row.raw_status) == (CAMPAIGN_GROUP_STATUS_MAP[raw], raw)
    expected = {"ACTIVE": "active", "PAUSED": "paused", "DRAFT": "paused"}.get(raw, "ended")
    assert row.status == expected


def test_malformed_campaign_groups_are_defects(contract: ContractLoader) -> None:
    """An unmapped status, a missing account or a non-object group is a defect."""
    [element, _other] = _sample(contract, "ad_campaign_groups")["elements"]
    for key, value in (("status", "EXPIRED"), ("account", None), ("name", 7)):
        broken = {**element, key: value}
        _defect(lambda broken=broken: parse_campaign_group(broken))
    _defect(lambda: parse_campaign_group("group"))


@pytest.mark.parametrize(
    "destination",
    [
        pytest.param(f"urn:li:adForm:{FORM_ID}", id="ad-form"),
        pytest.param(FORM_URN, id="canonical"),
        pytest.param(_versioned(3), id="versioned"),
    ],
)
def test_creative_lead_gen_destination_is_the_canonical_form(
    conform: Conform, destination: str
) -> None:
    """Every version of a form joins the same content asset through its canonical URN."""
    element = conform(
        "creatives",
        {
            "id": CREATIVE_URN,
            "campaign": CAMPAIGN_URN,
            "account": ACCOUNT_URN,
            "name": "Fake white paper ad",
            "intendedStatus": "ACTIVE",
            "isServing": True,
            "content": {"reference": "urn:li:ugcPost:990000501"},
            "leadgenCallToAction": {"destination": destination, "label": "DOWNLOAD"},
        },
    )
    row = parse_creative(element)
    assert (row.lead_form_urn, row.cta_label) == (FORM_URN, "DOWNLOAD")
    assert (row.status, row.is_serving, row.name) == ("active", True, "Fake white paper ad")
    assert row.post_urn == "urn:li:ugcPost:990000501"


@pytest.mark.parametrize("raw", sorted(CREATIVE_STATUS_MAP))
def test_creative_statuses_and_numeric_ids(conform: Conform, raw: str) -> None:
    """Every intended status normalises; a bare numeric id becomes the creative URN."""
    element = conform(
        "creatives",
        {
            "id": "urn:li:sponsoredCreative:990001101",
            "campaign": CAMPAIGN_URN,
            "intendedStatus": raw,
            "content": {"reference": "urn:li:adInlineContent:1"},
        },
    )
    row = parse_creative(element)
    assert (row.status, row.intended_status) == (CREATIVE_STATUS_MAP[raw], raw)
    assert (row.post_urn, row.account_urn, row.is_serving) == (None, None, None)
    element["id"] = 990001101
    assert parse_creative(element).urn == CREATIVE_URN


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"intendedStatus": "LIVE"}, id="unmapped-status"),
        pytest.param({"id": "urn:li:sponsoredCreative:abc"}, id="malformed-urn-id"),
        pytest.param({"content": {"reference": "urn:li:share:abc"}}, id="malformed-post"),
        pytest.param({"leadgenCallToAction": {"label": "DOWNLOAD"}}, id="no-destination"),
        pytest.param(
            {"leadgenCallToAction": {"destination": "https://x.invalid", "label": "JOIN"}},
            id="non-form-destination",
        ),
        pytest.param({"isServing": "yes"}, id="non-boolean-serving"),
        pytest.param({"campaign": "urn:li:sponsoredCampaign:"}, id="campaign-without-id"),
    ],
)
def test_malformed_creatives_are_defects(change: dict[str, Any]) -> None:
    """A creative that breaks its contract is a defect."""
    element = {"id": CREATIVE_URN, "campaign": CAMPAIGN_URN, "intendedStatus": "ACTIVE"}
    element.update(change)
    _defect(lambda: parse_creative(element))


def test_post_headlines_and_intro_text(conform: Conform) -> None:
    """The headline is the media title, else the article title; blank texts are ``None``."""
    base = {"id": "urn:li:share:990000601", "author": "urn:li:organization:990000701"}
    media = conform(
        "posts",
        {
            **base,
            "lifecycleState": "PUBLISHED",
            "commentary": "  Download the fake guide  ",
            "content": {"media": {"id": "urn:li:document:1", "title": "  Fake guide  "}},
        },
    )
    row = parse_post("urn:li:share:990000601", media)
    assert (row.headline, row.intro_text) == ("Fake guide", "Download the fake guide")
    article = {
        **base,
        "lifecycleState": "PUBLISHED",
        "commentary": "   ",
        "content": {"article": {"source": "https://example.com/fake", "title": "Fake article"}},
    }
    row = parse_post("urn:li:ugcPost:990000602", conform("posts", article))
    assert (row.headline, row.intro_text) == ("Fake article", None)
    blank_media = {**article, "content": {"media": {"id": "urn:li:document:1", "title": " "}}}
    assert parse_post("urn:li:share:990000601", conform("posts", blank_media)).headline is None
    untitled = {**article, "content": {"media": {"id": "urn:li:document:1"}}}
    assert parse_post("urn:li:share:990000601", conform("posts", untitled)).headline is None


@pytest.mark.parametrize(
    ("key", "element"),
    [
        pytest.param("urn:li:organization:1", {}, id="not-a-post-key"),
        pytest.param(None, {}, id="no-key"),
        pytest.param("urn:li:share:1", [], id="non-object-post"),
        pytest.param("urn:li:share:1", {"content": []}, id="non-object-content"),
        pytest.param("urn:li:share:1", {"commentary": 3}, id="non-string-commentary"),
    ],
)
def test_malformed_posts_are_defects(key: object, element: object) -> None:
    """A batch key that is not a post URN, or a malformed post, is a defect."""
    _defect(lambda: parse_post(key, element))


def _daily(**metrics: Any) -> dict[str, Any]:
    """A daily creative analytics element for 2026-09-30 (UTC)."""
    day = {"year": 2026, "month": 9, "day": 30}
    return {"pivotValues": [CREATIVE_URN], "dateRange": {"start": day, "end": dict(day)}, **metrics}


def test_analytics_daily_reads_every_requested_metric(conform: Conform) -> None:
    """Each requested metric maps to its column; the day is the UTC day LinkedIn returned."""
    element = conform(
        "ad_analytics",
        _daily(
            costInLocalCurrency="42.123456",
            impressions=1000,
            clicks=25,
            landingPageClicks=20,
            oneClickLeadFormOpens=12,
            oneClickLeads=4,
            externalWebsiteConversions=1,
        ),
    )
    assert parse_analytics_daily(element) == CreativeDailyRow(
        creative_urn=CREATIVE_URN,
        date=date(2026, 9, 30),
        cost=Decimal("42.123456"),
        impressions=1000,
        clicks=25,
        landing_page_clicks=20,
        form_opens=12,
        leads=4,
        website_conversions=1,
    )
    without_end = _daily()
    del without_end["dateRange"]["end"]
    row = parse_analytics_daily(conform("ad_analytics", without_end))
    assert row.date == date(2026, 9, 30)
    assert (row.cost, row.impressions, row.leads) == (None, None, None)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"pivotValues": []}, id="no-pivot"),
        pytest.param({"pivotValues": [CREATIVE_URN, OTHER_CREATIVE_URN]}, id="two-pivots"),
        pytest.param({"pivotValues": [7]}, id="non-string-pivot"),
        pytest.param({"pivotValues": "urn"}, id="non-array-pivot"),
        pytest.param(
            {"dateRange": {"start": {"year": 2026, "month": 2, "day": 30}}}, id="invalid-date"
        ),
        pytest.param(
            {
                "dateRange": {
                    "start": {"year": 2026, "month": 9, "day": 30},
                    "end": {"year": 2026, "month": 10, "day": 1},
                }
            },
            id="not-daily",
        ),
        pytest.param({"dateRange": {"start": {"year": 2026, "month": 9}}}, id="missing-day"),
        pytest.param({"costInLocalCurrency": 1.5}, id="float-cost"),
        pytest.param({"impressions": "many"}, id="non-integer-count"),
        pytest.param({"clicks": -1}, id="negative-count"),
    ],
)
def test_malformed_analytics_are_defects(change: dict[str, Any]) -> None:
    """A daily element that is not one creative on one valid day, or has bad metrics, fails."""
    element = _daily()
    element.update(change)
    _defect(lambda: parse_analytics_daily(element))
    _defect(lambda: parse_analytics_daily(None))


@pytest.mark.parametrize(
    ("pivot", "value", "facet"),
    [
        ("MEMBER_JOB_FUNCTION", "urn:li:function:8", "job_function"),
        ("MEMBER_SENIORITY", "urn:li:seniority:6", "seniority"),
        ("MEMBER_INDUSTRY", "urn:li:industry:4", "industry"),
    ],
)
def test_segment_facets(conform: Conform, pivot: str, value: str, facet: str) -> None:
    """Each demographic pivot maps to its ``fact_segment_window.facet``, in either order."""
    assert SEGMENT_FACETS[pivot] == facet
    assert pivot in SEGMENT_PIVOTS
    element = conform(
        "ad_analytics_statistics",
        {
            "pivotValues": [value, CAMPAIGN_URN],
            "costInLocalCurrency": "12.5",
            "impressions": 400,
            "clicks": 9,
            "oneClickLeads": 2,
            "externalWebsiteConversions": 0,
        },
    )
    row = parse_segment(element, pivot)
    assert row == SegmentRow(
        campaign_urn=CAMPAIGN_URN,
        facet=facet,
        value=value,
        impressions=400,
        clicks=9,
        cost=Decimal("12.5"),
        leads=2,
        website_conversions=0,
    )


def test_segment_analytics_finder_fallback_takes_the_requested_campaign(conform: Conform) -> None:
    """A single-pivot element carries only the value; the requested campaign is attached."""
    element = conform("ad_analytics_statistics", {"pivotValues": ["urn:li:industry:4"]})
    row = parse_segment(element, "MEMBER_INDUSTRY", campaign_urn=990000201)
    assert (row.campaign_urn, row.value, row.leads) == (CAMPAIGN_URN, "urn:li:industry:4", None)
    both = {"pivotValues": [CAMPAIGN_URN, "urn:li:industry:4"]}
    assert parse_segment(both, "MEMBER_INDUSTRY", campaign_urn=CAMPAIGN_URN).facet == "industry"


@pytest.mark.parametrize(
    ("pivot_values", "campaign"),
    [
        pytest.param(["urn:li:industry:4"], None, id="no-campaign"),
        pytest.param([CAMPAIGN_URN, OTHER_CAMPAIGN_URN, "urn:li:industry:4"], None, id="two"),
        pytest.param([OTHER_CAMPAIGN_URN, "urn:li:industry:4"], CAMPAIGN_URN, id="other"),
        pytest.param([CAMPAIGN_URN], None, id="no-value"),
        pytest.param([CAMPAIGN_URN, "urn:li:seniority:5"], None, id="other-facet"),
        pytest.param(["urn:li:sponsoredCampaign:x", "urn:li:industry:4"], None, id="bad-urn"),
    ],
)
def test_inconsistent_segment_pivots_are_defects(
    pivot_values: list[str], campaign: str | None
) -> None:
    """The campaign and exactly one value of the requested facet must be identifiable."""
    element = {"pivotValues": pivot_values}
    _defect(lambda: parse_segment(element, "MEMBER_INDUSTRY", campaign_urn=campaign))


def test_segment_argument_checks() -> None:
    """An unknown pivot or a malformed requested campaign is a caller bug."""
    with pytest.raises(ValueError, match="MEMBER_JOB_FUNCTION"):
        parse_segment({"pivotValues": []}, "CAMPAIGN")
    with pytest.raises(ValueError, match="campaign_id"):
        parse_segment({"pivotValues": []}, "MEMBER_INDUSTRY", campaign_urn="campaign-1")
    _defect(lambda: parse_segment("element", "MEMBER_INDUSTRY"))


def test_ad_account_fields(conform: Conform, contract: ContractLoader) -> None:
    """``test`` defaults to false; the currency must be an ISO 4217 code."""
    account = _sample(contract, "ad_account")
    account.update(id=990000001, test=True)
    assert parse_ad_account(conform("ad_account", account)).test is True
    del account["test"]
    row = parse_ad_account(conform("ad_account", account))
    assert (row.test, row.urn) == (False, ACCOUNT_URN)
    for key, value in (("currency", "dollars"), ("currency", None), ("test", "no")):
        _defect(lambda key=key, value=value: parse_ad_account({**account, key: value}))
    _defect(lambda: parse_ad_account(None))


def test_urn_helpers() -> None:
    """``sponsored_*_urn`` builds a URN from an id or URN; ``urn_id`` inverts it."""
    assert sponsored_account_urn(123) == "urn:li:sponsoredAccount:123"
    assert sponsored_account_urn("123") == "urn:li:sponsoredAccount:123"
    assert sponsored_account_urn("urn:li:sponsoredAccount:123") == "urn:li:sponsoredAccount:123"
    assert urn_id(sponsored_account_urn(123), PREFIX_ACCOUNT) == "123"
    assert sponsored_campaign_urn(990000201) == CAMPAIGN_URN
    assert urn_id(CAMPAIGN_URN, PREFIX_CAMPAIGN) == "990000201"
    for bad in (True, -1, 1.0, "12a", "urn:li:sponsoredCampaign:1", None):
        with pytest.raises(ValueError, match="account_id"):
            sponsored_account_urn(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="campaign_id"):
        sponsored_campaign_urn("urn:li:sponsoredAccount:1")
    with pytest.raises(ValueError, match="does not start"):
        urn_id(CAMPAIGN_URN, PREFIX_ACCOUNT)
    with pytest.raises(ValueError, match="does not start"):
        urn_id(CAMPAIGN_URN, "")
    with pytest.raises(ValueError, match="not numeric"):
        urn_id("urn:li:sponsoredAccount:12a", PREFIX_ACCOUNT)
