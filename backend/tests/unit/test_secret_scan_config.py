"""Scanner cases for the four assignment rules in the repository-root ``.gitleaks.toml``.

The ``n8n-webhook-secret``, ``session-secret``, ``linkedin-token`` and ``hubspot-token`` rules
must report a literal credential whether its key is bare (env file, shell ``export``, YAML,
Makefile) or quoted (JSON, Python dict, TOML, subscript, keyword argument), including a
digits-only value whose entropy is below the 3.5 floor of the built-in generic-api-key rule,
which therefore never covers it. A value of 16 characters or more is reported in every key form.
A shorter one, of any length down to one character, is reported as a quoted string, as a bare
run of digits (a JSON or Python number), and, after a bare upper-case name, as a bare value
ended by a quote, backtick, ``;``, whitespace, comment or line end (env files, ``export``,
inline ``NAME=value cmd``, Makefile, YAML with a trailing comment); the n8n header names are
also reported as ``curl -H`` and HTTP header lines. The short controls are low-entropy (1, 7, 8
and 10 digits; 2 to 15 letters and digits), so no entropy test can be what reports them, and
none is a lone repeated letter such as ``zzz``, which gitleaks' built-in global allowlist, not
modelled here, already excuses.

The rules must report nothing for schema entries, templates, short ``${NAME:-value}`` defaults,
empty assignments, longer key names, and code references: lower-case settings annotations,
upper-case ``Final`` annotations, ``SecretStr(...)`` calls, variables, ``None``, subscript
comparisons, call results, prose, sed and awk patterns, and elided ``...`` values.
Attribute paths and short placeholder values (``changeme``, the exact ``simulated``, ``test-``,
``your-``, an ``x`` mask, ``example``, ``dummy``, ``fake``) do match, and the code-reference
allowlist excuses them; a value that merely starts with ``simulated`` is a credential like any
other and is reported. The short stand-in values three unit tests committed match too, and the
historical-stub allowlist excuses them only in those three stub test files.

The rule and allowlist patterns are compiled with Python ``re``. They are written in the subset
that Go RE2 and Python ``re`` share (scoped ``(?i:...)`` flags; no lookaround, backreferences or
possessive quantifiers), so on the samples below Python matches exactly what gitleaks matches
(the two engines differ only in which non-ASCII characters count as whitespace, and the one
non-ASCII sample, the stub ``café``, holds none; its committed source spells it as a Unicode
escape, which gitleaks decodes before matching).
``_secrets`` picks the reported secret as gitleaks 8.30.1 does for a rule with no
``secretGroup``. gitleaks scans this file too, so every key-plus-value sample is assembled at
run time from parts, and no literal here matches a rule.
No test performs network or database I/O.
"""

import math
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

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

# The n8n header names, which an HTTP request or `curl -H` carries as `Name: value`.
HEADER_RULE_KEYS: Mapping[str, tuple[str, ...]] = {
    "n8n-webhook-secret": tuple(
        key for key in RULE_KEYS["n8n-webhook-secret"] if key.startswith("X-")
    ),
}

# A digits-only 32-character value, the shape of the live n8n secret: 1234567890 repeated.
DIGITS = "".join(str(i % 10) for i in range(1, 33))

# The long form reports a value of this many characters or more in every key form.
LONG_FLOOR = 16

# Short low-entropy literals, from one character up to one below the long form. None is a lone
# repeated letter, which gitleaks' built-in global allowlist excuses and this module does not
# model.
SHORT_VALUES: Mapping[str, str] = {
    "one-digit": "7",
    "two-characters": "k9",
    "three-characters": "kq7",
    "seven-digits": DIGITS[:7],
    "seven-letters": "klmnopq",
    "eight-digits": DIGITS[:8],
    "ten-digits": DIGITS[:10],
    "ten-letters": "klmnopqrst",
    "fifteen-letters": "klmno" * 3,
}

# Digits-only values, short and long, for the bare-number forms.
DIGIT_VALUES: Mapping[str, str] = {
    "one-digit": "7",
    "seven-digits": DIGITS[:7],
    "eight-digits": DIGITS[:8],
    "ten-digits": DIGITS[:10],
    "thirty-two-digits": DIGITS,
}

# Header values: every short value, whatever its characters, and the long digits-only one.
HEADER_VALUES: Mapping[str, str] = {**SHORT_VALUES, "thirty-two-digits": DIGITS}

# Short placeholder values, each of which the code-reference allowlist must excuse.
PLACEHOLDER_VALUES: Mapping[str, str] = {
    value: value
    for value in (
        "changeme12",
        "simulated",
        "test-value",
        "your-token",
        "xxxxxxxxxx",
        "example123",
        "dummy-value",
        "fake-token",
    )
}

# The stand-in header value the simulator's webhook requests carry, excused only exactly.
SIMULATED_HEADER_VALUE = "simulated"
SIMULATED_ALLOWLIST_REGEX = r"^simulated$"

# Credentials that start with the stand-in, and a control that starts with no placeholder word.
SIMULATED_LOOKALIKES: Mapping[str, str] = {
    "simulated-long-suffix": SIMULATED_HEADER_VALUE + "27" * 8,
    "simulated-short-suffix": SIMULATED_HEADER_VALUE + "k9",
    "non-placeholder-prefix": "authvalue" + "27" * 8,
}

# Short stand-in credentials three unit tests committed, which history keeps, and the three stub
# test files the historical-stub allowlist confines them to.
HISTORICAL_STUBS = ("4242", "ABC", "abc", "a/b+c", "café", "red", "short", "short7x", "xyz")
STUB_PATHS = (
    "backend/tests/unit/test_cli.py",
    "backend/tests/unit/test_connector_http.py",
    "backend/tests/unit/test_connector_errors_types.py",
)

# A product source file and a test file whose name only resembles one of the three: a
# historical stub is as reportable in them as any other value.
UNSCOPED_PATHS = ("backend/src/x.py", "backend/tests/unit/test_client.py")

# Short values no committed test uses, so they are reportable even in the three test files:
# an unrelated value, and a stub with a character appended and prepended.
UNLISTED_SHORT_VALUES: Mapping[str, str] = {
    "unrelated": "kq7m",
    "stub-appended": "abc" + "d",
    "stub-prepended": "x" + "abc",
}

# Each form renders one assignment of `value` to `key`, as a line of the named file type.
LineForm = Callable[[str, str], str]

POSITIVE_FORMS: Mapping[str, LineForm] = {
    "bare-equals": lambda key, value: f"{key}={value}\n",
    "bare-colon": lambda key, value: f"{key}: {value}\n",
    "export": lambda key, value: f"export {key}={value}\n",
    "export-semicolon": lambda key, value: f"export {key}={value};\n",
    "inline-command": lambda key, value: f"{key}={value} docker compose up\n",
    "yaml-comment": lambda key, value: f"  {key}: {value}  # comment\n",
    "makefile": lambda key, value: f"{key} := {value}\n",
    "json": lambda key, value: f'{{"{key}": "{value}"}}\n',
    "python-dict": lambda key, value: f"headers = {{'{key}': '{value}'}}\n",
    "toml": lambda key, value: f'"{key}" = "{value}"\n',
    "subscript": lambda key, value: f'environ["{key}"] = "{value}"\n',
    "kwarg": lambda key, value: f'Settings({key.lower()}="{value}")\n',
}

# Forms whose value is a bare number, so only a digits-only value is a literal in them.
DIGIT_FORMS: Mapping[str, LineForm] = {
    "json-number": lambda key, value: f'{{"{key}": {value}}}\n',
    "kwarg-number": lambda key, value: f"Settings({key.lower()}={value})\n",
}

# Forms of an HTTP header line, for the n8n header names only.
HEADER_FORMS: Mapping[str, LineForm] = {
    "curl-header": lambda key, value: f"curl -H '{key}: {value}' https://n8n.invalid/hook\n",
    "http-header": lambda key, value: f"{key}: {value}\r\n",
    "http-header-lower": lambda key, value: f"{key.lower()}: {value}\r\n",
}

ALL_FORMS: Mapping[str, LineForm] = {**POSITIVE_FORMS, **DIGIT_FORMS, **HEADER_FORMS}

# Code that names a setting without assigning it a literal; each renders one line for `key`.
CodeForm = Callable[[str], str]

CODE_REFERENCE_FORMS: Mapping[str, CodeForm] = {
    "secretstr-call": lambda key: f'{key.lower()}=SecretStr("x"),\n',
    "variable-value": lambda key: f'"{key}": short_secret,\n',
    "keyword-variable": lambda key: f"set_env({key}=secret_value)\n",
    "subscript-compare": lambda key: f'headers["{key}"] == secret\n',
    "none-value": lambda key: f'{{"{key}": None}}\n',
    "call-value": lambda key: f"{key}=read_secret(\n",
    "prose": lambda key: f"header {key}: missing or empty\n",
    "awk-pattern": lambda key: f"  /^{key}=/ {{ print }}\n",
    "sed-substitution": lambda key: f"sed -i 's/^{key}=.*/x/' .env\n",
    "elided-bare": lambda key: f"{key}=...\n",
    "elided-quoted": lambda key: f'{key}="..."\n',
}

# Lines that carry a short value without assigning it to the name; each renders `key`, `value`.
SHORT_NEGATIVE_FORMS: Mapping[str, LineForm] = {
    "default-expansion": lambda key, value: f"${{{key}:-{value}}}\n",
}

# The short values a `${NAME:-value}` default leaves alone: with its leading `-`, a default of
# 15 characters or more is a long-form literal after `NAME:`, which is reported like any other.
DEFAULT_VALUES: Mapping[str, str] = {
    name: value for name, value in SHORT_VALUES.items() if len("-" + value) < LONG_FLOOR
}

# The literal forms the historical stubs were committed in: a dict entry and a keyword argument.
STUB_FORMS: Mapping[str, LineForm] = {form: POSITIVE_FORMS[form] for form in ("json", "kwarg")}

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

# Keys that are also Python identifiers, so a settings field can be named after them.
IDENTIFIER_KEY_CASES = [
    pytest.param(rule_id, key, id=key)
    for rule_id, keys in RULE_KEYS.items()
    for key in keys
    if key.isidentifier()
]


def _form_cases(forms: Mapping[str, LineForm | CodeForm]) -> list[object]:
    """Return one parameter set per (rule, key, form), identified as ``key-form``."""
    return [
        pytest.param(rule_id, key, form, id=f"{key}-{form}")
        for rule_id, keys in RULE_KEYS.items()
        for key in keys
        for form in forms
    ]


def _value_cases(
    forms: Mapping[str, LineForm],
    values: Mapping[str, str],
    keys: Mapping[str, tuple[str, ...]] = RULE_KEYS,
) -> list[object]:
    """Return one parameter set per (rule, key, form, value), identified as ``key-form-value``."""
    return [
        pytest.param(rule_id, key, form, value, id=f"{key}-{form}-{name}")
        for rule_id, rule_keys in keys.items()
        for key in rule_keys
        for form in forms
        for name, value in values.items()
    ]


def _shannon_entropy(text: str) -> float:
    """Return the Shannon entropy of ``text`` in bits per character, as gitleaks computes it."""
    size = len(text)
    return -sum(count / size * math.log2(count / size) for count in Counter(text).values())


def _secrets(pattern: re.Pattern[str], text: str) -> list[str]:
    """Return the secret gitleaks 8.30.1 reports for every match of ``pattern`` in ``text``.

    For a rule with no ``secretGroup``, gitleaks trims newlines from the matched text, runs the
    regex on it again and reports that match's first non-empty capture group, or the trimmed
    text when every group is empty.
    """
    secrets: list[str] = []
    for match in pattern.finditer(text):
        matched = match.group(0).strip("\n")
        rematch = pattern.search(matched)
        groups: tuple[str | None, ...] = rematch.groups() if rematch else ()
        secrets.append(next((group for group in groups if group), matched))
    return secrets


def _reportable(
    patterns: Mapping[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
    text: str,
) -> list[tuple[str, str]]:
    """Return ``(rule id, secret)`` for each assignment-rule match the allowlist does not excuse.

    gitleaks tests an allowlist regex against the reported secret by default and excuses the
    finding when any regex matches anywhere in it, which ``re.search`` reproduces.
    """
    return [
        (rule_id, secret)
        for rule_id, pattern in patterns.items()
        for secret in _secrets(pattern, text)
        if not any(allowed.search(secret) for allowed in allowlist)
    ]


class PathAllowlist(NamedTuple):
    """A path-scoped allowlist with ``condition = "AND"``, compiled with Python ``re``."""

    paths: tuple[re.Pattern[str], ...]
    regexes: tuple[re.Pattern[str], ...]


def _reportable_at(
    patterns: Mapping[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
    path_allowlist: PathAllowlist,
    path: str,
    text: str,
) -> list[tuple[str, str]]:
    """Return what ``_reportable`` returns for ``text`` in the file at ``path``, less the
    findings ``path_allowlist`` excuses there.

    With ``condition = "AND"`` gitleaks excuses a finding only when one path regex matches the
    file path and one value regex matches the reported secret.
    """
    in_scope = any(allowed.search(path) for allowed in path_allowlist.paths)
    return [
        (rule_id, secret)
        for rule_id, secret in _reportable(patterns, allowlist, text)
        if not (in_scope and any(allowed.search(secret) for allowed in path_allowlist.regexes))
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


@pytest.fixture(scope="module")
def stub_allowlist(config: dict[str, Any]) -> PathAllowlist:
    """Return the historical-stub allowlist, compiled with Python ``re``.

    It is the one allowlist that targets exactly the four assignment rules and names paths. It
    must combine its paths and values with ``condition = "AND"`` and test the values against
    the reported secret, the default ``regexTarget``.
    """
    matching = [
        allowlist
        for allowlist in config["allowlists"]
        if sorted(allowlist.get("targetRules", ())) == sorted(ASSIGNMENT_RULES)
        and "paths" in allowlist
    ]
    assert len(matching) == 1, "expected exactly one path-scoped assignment-rule allowlist"
    (stubs,) = matching
    assert stubs.get("condition") == "AND"
    assert "regexTarget" not in stubs
    return PathAllowlist(
        paths=tuple(re.compile(regex) for regex in stubs["paths"]),
        regexes=tuple(re.compile(regex) for regex in stubs["regexes"]),
    )


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

    assert "secretGroup" not in rule, "each branch captures its own group; gitleaks takes the first"
    assert "entropy" not in rule
    assert re.search(r"\(\?<?[=!]", regex) is None, "lookaround is not valid Go RE2"
    assert r"\s*" not in regex, "a newline must never separate a key from its value"
    assert re.compile(regex).groups >= 1


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


@pytest.mark.parametrize("value", SHORT_VALUES.values())
def test_short_controls_are_low_entropy_and_shorter_than_the_long_form(value: str) -> None:
    assert 0 < len(value) < LONG_FLOOR
    assert _shannon_entropy(value) < GENERIC_API_KEY_ENTROPY
    assert len(set(value)) > 1 or value.isdigit(), "a lone repeated letter is excused built-in"


@pytest.mark.parametrize(
    ("rule_id", "key", "form", "value"),
    [
        *_value_cases(POSITIVE_FORMS, SHORT_VALUES),
        *_value_cases(DIGIT_FORMS, DIGIT_VALUES),
        *_value_cases(HEADER_FORMS, HEADER_VALUES, HEADER_RULE_KEYS),
    ],
)
def test_low_entropy_literal_of_any_length_is_reported_in_every_literal_form(
    rule_id: str,
    key: str,
    form: str,
    value: str,
    rules: dict[str, dict[str, Any]],
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = ALL_FORMS[form](key, value)

    assert any(keyword in line.lower() for keyword in rules[rule_id]["keywords"])
    assert _secrets(patterns[rule_id], line) == [value]
    assert not [allowed.pattern for allowed in allowlist if allowed.search(value)]
    assert _reportable(patterns, allowlist, line) == [(rule_id, value)]


@pytest.mark.parametrize(
    ("rule_id", "key", "form", "value"), _value_cases(SHORT_NEGATIVE_FORMS, DEFAULT_VALUES)
)
def test_short_value_not_assigned_to_the_name_is_not_reported(
    rule_id: str,
    key: str,
    form: str,
    value: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = SHORT_NEGATIVE_FORMS[form](key, value)

    assert _secrets(patterns[rule_id], line) == []
    assert _reportable(patterns, allowlist, line) == []


@pytest.mark.parametrize(
    ("rule_id", "key", "form", "value"),
    [
        *_value_cases(POSITIVE_FORMS, PLACEHOLDER_VALUES),
        *_value_cases(HEADER_FORMS, PLACEHOLDER_VALUES, HEADER_RULE_KEYS),
    ],
)
def test_short_placeholder_matches_but_is_excused_by_the_allowlist(
    rule_id: str,
    key: str,
    form: str,
    value: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = ALL_FORMS[form](key, value)

    assert len(value) < LONG_FLOOR
    assert _secrets(patterns[rule_id], line) == [value]
    assert [allowed.pattern for allowed in allowlist if allowed.search(value)]
    assert _reportable(patterns, allowlist, line) == []


def test_simulated_is_excused_only_as_the_exact_value(
    code_reference_regexes: tuple[str, ...],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    assert SIMULATED_ALLOWLIST_REGEX in code_reference_regexes
    assert [allowed.pattern for allowed in allowlist if allowed.search(SIMULATED_HEADER_VALUE)] == [
        SIMULATED_ALLOWLIST_REGEX
    ]
    for value in SIMULATED_LOOKALIKES.values():
        assert not [allowed.pattern for allowed in allowlist if allowed.search(value)], value


@pytest.mark.parametrize(
    ("rule_id", "key", "form", "value"),
    [
        *_value_cases(POSITIVE_FORMS, SIMULATED_LOOKALIKES),
        *_value_cases(HEADER_FORMS, SIMULATED_LOOKALIKES, HEADER_RULE_KEYS),
    ],
)
def test_value_starting_with_simulated_is_reported(
    rule_id: str,
    key: str,
    form: str,
    value: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = ALL_FORMS[form](key, value)

    assert _secrets(patterns[rule_id], line) == [value]
    assert _reportable(patterns, allowlist, line) == [(rule_id, value)]


@pytest.mark.parametrize(("rule_id", "key", "form"), _form_cases(CODE_REFERENCE_FORMS))
def test_code_reference_is_not_reported(
    rule_id: str,
    key: str,
    form: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = CODE_REFERENCE_FORMS[form](key)

    assert _secrets(patterns[rule_id], line) == []
    assert _reportable(patterns, allowlist, line) == []


@pytest.mark.parametrize(("rule_id", "key"), IDENTIFIER_KEY_CASES)
def test_lower_case_settings_annotation_is_not_reported(
    rule_id: str,
    key: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = f"    {key.lower()}: SecretStr | None = None\n"

    assert _secrets(patterns[rule_id], line) == []
    assert _reportable(patterns, allowlist, line) == []


@pytest.mark.parametrize(("rule_id", "key"), IDENTIFIER_KEY_CASES)
def test_upper_case_constant_annotation_is_not_reported(
    rule_id: str,
    key: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
) -> None:
    line = f'TEST_{key}: Final = "x"\n'

    assert _secrets(patterns[rule_id], line) == []
    assert _reportable(patterns, allowlist, line) == []


@pytest.mark.parametrize(
    ("rule_id", "key", "form", "value"),
    _value_cases(STUB_FORMS, {stub: stub for stub in HISTORICAL_STUBS}),
)
def test_historical_stub_is_excused_only_in_the_stub_test_files(
    rule_id: str,
    key: str,
    form: str,
    value: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
    stub_allowlist: PathAllowlist,
) -> None:
    line = STUB_FORMS[form](key, value)
    reported = [(rule_id, value)]

    assert _secrets(patterns[rule_id], line) == [value]
    assert _reportable(patterns, allowlist, line) == reported
    for path in STUB_PATHS:
        assert _reportable_at(patterns, allowlist, stub_allowlist, path, line) == []
        assert _reportable_at(patterns, allowlist, stub_allowlist, f"/repo/{path}", line) == []
    for path in UNSCOPED_PATHS:
        assert _reportable_at(patterns, allowlist, stub_allowlist, path, line) == reported


@pytest.mark.parametrize(
    ("rule_id", "key", "form", "value"), _value_cases(STUB_FORMS, UNLISTED_SHORT_VALUES)
)
def test_unlisted_short_value_is_reported_in_the_stub_test_files(
    rule_id: str,
    key: str,
    form: str,
    value: str,
    patterns: dict[str, re.Pattern[str]],
    allowlist: tuple[re.Pattern[str], ...],
    stub_allowlist: PathAllowlist,
) -> None:
    line = STUB_FORMS[form](key, value)

    reported = [(rule_id, value)]

    assert value not in HISTORICAL_STUBS
    for path in STUB_PATHS:
        assert _reportable_at(patterns, allowlist, stub_allowlist, path, line) == reported
