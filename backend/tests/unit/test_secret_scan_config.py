"""Scanner cases for the four assignment rules in the repository-root ``.gitleaks.toml``.

The ``n8n-webhook-secret``, ``session-secret``, ``linkedin-token`` and ``hubspot-token`` rules
must report a literal credential whether its key is bare (env file, shell, YAML) or quoted
(JSON, Python dict, TOML, subscript), including a digits-only value whose entropy is below the
3.5 floor of the built-in generic-api-key rule, which therefore never covers it. They must
report nothing for schema entries, templates, empty assignments, longer key names and code
references, the last of which match but are excused by the code-reference allowlist.

The rule and allowlist patterns are compiled with Python ``re``. They are written in the subset
that Go RE2 and Python ``re`` share (no lookaround, backreferences or possessive quantifiers),
so on the ASCII samples below Python matches exactly what gitleaks matches (the two engines
differ only in which non-ASCII characters count as whitespace). gitleaks scans this file too,
so every key-plus-value sample is assembled at run time from parts, and no literal here
matches a rule.
No test performs network or database I/O.
"""

import math
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

CONFIG_NAME = ".gitleaks.toml"

# The built-in generic-api-key rule of gitleaks v8.30.1 reports only values above this entropy.
GENERIC_API_KEY_ENTROPY = 3.5

# The code-reference allowlist pattern for attribute paths such as `settings.session_secret`.
ATTRIBUTE_PATH_REGEX = r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+$"

# Key names assembled from parts, so no source line pairs a full key name with a value.
_SECRET = "_SEC" + "RET"
_TOKEN = "_TOK" + "EN"
RULE_KEYS: Mapping[str, tuple[str, ...]] = {
    "n8n-webhook-secret": (
        "N8N_WEBHOOK" + _SECRET,
        "N8N_GOOGLE_ADS_WEBHOOK" + _TOKEN,
        "X-Webhook-" + "Secret",
        "X-Webhook-" + "Token",
    ),
    "session-secret": ("SESSION" + _SECRET,),
    "linkedin-token": (
        "LINKEDIN_ACCESS" + _TOKEN,
        "LINKEDIN_REFRESH" + _TOKEN,
        "LINKEDIN_CLIENT" + _SECRET,
    ),
    "hubspot-token": (
        "HUBSPOT_ACCESS" + _TOKEN,
        "HUBSPOT_PRIVATE_APP" + _TOKEN,
    ),
}
ASSIGNMENT_RULES = tuple(RULE_KEYS)

# A digits-only 32-character value, the shape of the live n8n secret: 1234567890 repeated.
DIGITS = "".join(str(i % 10) for i in range(1, 33))

# Each form renders one assignment of `value` to `key`, as a line of the named file type.
LineForm = Callable[[str, str], str]

POSITIVE_FORMS: Mapping[str, LineForm] = {
    "bare-equals": lambda key, value: f"{key}={value}\n",
    "bare-colon": lambda key, value: f"{key}: {value}\n",
    "json": lambda key, value: f'{{"{key}": "{value}"}}\n',
    "python-dict": lambda key, value: f"headers = {{'{key}': '{value}'}}\n",
    "toml": lambda key, value: f'"{key}" = "{value}"\n',
    "subscript": lambda key, value: f'environ["{key}"] = "{value}"\n',
}

NEGATIVE_FORMS: Mapping[str, LineForm] = {
    "json-schema": lambda key, value: f'"{key}": {{"type": "string"}},\n',
    "brace-template": lambda key, value: f'"{key}": "{{{key}}}",\n',
    "shell-template": lambda key, value: f'"{key}": "${{{key}}}",\n',
    "empty-bare": lambda key, value: f"{key}=\nGOOGLE_DAILY_CALL_BUDGET={value}\n",
    "empty-quoted": lambda key, value: f'"{key}": "",\n"next": "{value}"\n',
    "suffix-key-quoted": lambda key, value: f'"{key}_HEADER": "{value}",\n',
    "suffix-key-bare": lambda key, value: f"{key}_FILE={value}\n",
}

RULE_KEY_CASES = [
    pytest.param(rule_id, key, id=key) for rule_id, keys in RULE_KEYS.items() for key in keys
]


def _form_cases(forms: Mapping[str, LineForm]) -> list[object]:
    """Return one parameter set per (rule, key, form), identified as ``key-form``."""
    return [
        pytest.param(rule_id, key, form, id=f"{key}-{form}")
        for rule_id, keys in RULE_KEYS.items()
        for key in keys
        for form in forms
    ]


def _shannon_entropy(text: str) -> float:
    """Return the Shannon entropy of ``text`` in bits per character, as gitleaks computes it."""
    size = len(text)
    return -sum(count / size * math.log2(count / size) for count in Counter(text).values())


def _secrets(pattern: re.Pattern[str], text: str) -> list[str]:
    """Return the secret group (group 1) of every match of ``pattern`` in ``text``."""
    return [match.group(1) for match in pattern.finditer(text)]


def _reportable(
    patterns: Mapping[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
    text: str,
) -> list[tuple[str, str]]:
    """Return ``(rule id, secret)`` for each assignment-rule match the allowlist does not excuse.

    gitleaks tests an allowlist regex against the secret group by default and excuses the
    finding when any regex matches anywhere in it, which ``re.search`` reproduces.
    """
    return [
        (rule_id, secret)
        for rule_id, pattern in patterns.items()
        for secret in _secrets(pattern, text)
        if not any(allowed.search(secret) for allowed in allowlist)
    ]


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    """Load the repository-root ``.gitleaks.toml``, found by walking up from this file."""
    for directory in Path(__file__).resolve().parents:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            with candidate.open("rb") as handle:
                return tomllib.load(handle)
    pytest.fail(f"{CONFIG_NAME} not found in any parent directory of {__file__}")


@pytest.fixture(scope="module")
def rules(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the four assignment rules keyed by rule id."""
    by_id: dict[str, dict[str, Any]] = {rule["id"]: rule for rule in config["rules"]}
    missing = [rule_id for rule_id in ASSIGNMENT_RULES if rule_id not in by_id]
    assert not missing, f"assignment rules missing from {CONFIG_NAME}: {missing}"
    return {rule_id: by_id[rule_id] for rule_id in ASSIGNMENT_RULES}


@pytest.fixture(scope="module")
def patterns(rules: dict[str, dict[str, Any]]) -> dict[str, re.Pattern[str]]:
    """Compile each assignment rule's regex with Python ``re``."""
    return {rule_id: re.compile(rule["regex"]) for rule_id, rule in rules.items()}


@pytest.fixture(scope="module")
def code_reference_regexes(config: dict[str, Any]) -> tuple[str, ...]:
    """Return the regexes of the code-reference and placeholder allowlist.

    It is the one allowlist that targets exactly the four assignment rules and names no paths,
    so it applies to every file.
    """
    matching = [
        allowlist
        for allowlist in config["allowlists"]
        if sorted(allowlist.get("targetRules", ())) == sorted(ASSIGNMENT_RULES)
        and "paths" not in allowlist
    ]
    assert len(matching) == 1, "expected exactly one path-free assignment-rule allowlist"
    regexes: list[str] = matching[0]["regexes"]
    return tuple(regexes)


@pytest.fixture(scope="module")
def allowlist(code_reference_regexes: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Compile the code-reference allowlist regexes with Python ``re``."""
    return tuple(re.compile(regex) for regex in code_reference_regexes)


def test_digits_only_value_is_below_the_generic_rule_entropy_floor() -> None:
    assert len(DIGITS) == 32
    assert DIGITS.isdigit()
    assert _shannon_entropy(DIGITS) < GENERIC_API_KEY_ENTROPY


@pytest.mark.parametrize("rule_id", ASSIGNMENT_RULES)
def test_assignment_rule_shape_is_re2_compatible_without_entropy_floor(
    rule_id: str, rules: dict[str, dict[str, Any]]
) -> None:
    rule = rules[rule_id]
    regex: str = rule["regex"]

    assert rule["secretGroup"] == 1
    assert "entropy" not in rule
    assert re.search(r"\(\?<?[=!]", regex) is None, "lookaround is not valid Go RE2"
    assert r"\s*" not in regex, "a newline must never separate a key from its value"
    assert re.compile(regex).groups == 1


@pytest.mark.parametrize(("rule_id", "key", "form"), _form_cases(POSITIVE_FORMS))
def test_literal_value_is_reported_for_bare_and_quoted_keys(
    rule_id: str,
    key: str,
    form: str,
    rules: dict[str, dict[str, Any]],
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = POSITIVE_FORMS[form](key, DIGITS)

    assert any(keyword in line.lower() for keyword in rules[rule_id]["keywords"])
    assert _secrets(patterns[rule_id], line) == [DIGITS]
    assert not [allowed.pattern for allowed in allowlist if allowed.search(DIGITS)]
    assert _reportable(patterns, allowlist, line) == [(rule_id, DIGITS)]


@pytest.mark.parametrize(("rule_id", "key", "form"), _form_cases(NEGATIVE_FORMS))
def test_schema_template_empty_and_longer_key_forms_are_not_reported(
    rule_id: str,
    key: str,
    form: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    text = NEGATIVE_FORMS[form](key, DIGITS)

    assert _secrets(patterns[rule_id], text) == []
    assert _reportable(patterns, allowlist, text) == []


@pytest.mark.parametrize(("rule_id", "key"), RULE_KEY_CASES)
def test_attribute_path_value_matches_but_is_excused_by_the_allowlist(
    rule_id: str,
    key: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
    code_reference_regexes: tuple[str, ...],
) -> None:
    attribute = "settings." + key.lower().replace("-", "_") + ".get_secret_value"
    line = f'    "{key}": {attribute}(),\n'

    assert _secrets(patterns[rule_id], line) == [attribute]
    assert ATTRIBUTE_PATH_REGEX in code_reference_regexes
    assert re.search(ATTRIBUTE_PATH_REGEX, attribute) is not None
    assert _reportable(patterns, allowlist, line) == []
