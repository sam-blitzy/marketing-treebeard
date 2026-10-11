r"""Configuration loader for ``config/*.yaml``: thresholds, ``config_hash`` and CRM bindings.

AAP 0.6.2: ``config/thresholds.yaml``, ``config/content_assets.yaml`` and
``config/hubspot_mapping.yaml`` are loaded here, and their hash is stored on every analysis
run. :meth:`Thresholds.load` reads all three and returns one immutable :class:`Thresholds`:

* **Thresholds** (AAP 0.5.4): every minimum sample, ratio and window a rule uses is a named,
  dotted key such as ``k1.rule_of_three``, read with :meth:`Thresholds.int`,
  :meth:`Thresholds.decimal` or :meth:`Thresholds.ratio`. A missing key raises
  :class:`ConfigError`; no accessor returns a silent default. Rules copy exactly the keys they
  used into ``evidence.thresholds`` with :meth:`Thresholds.subset`.
* **HubSpot mapping** (AAP 0.5.4.7): the MQL and Qualified Lead bindings
  (:class:`StageBinding`) and the meeting rules ``crm.booking_sources`` and
  ``crm.meeting_outcome_exclusions``, which are merged into the thresholds ``crm`` namespace.
  The resolved, portal-verified form of the bindings is :class:`CrmBindings`, persisted per
  HubSpot dataset as ``source_dataset.crm_bindings`` through :meth:`CrmBindings.to_json`.
* **White paper registry** (AAP 0.5.2, 0.5.4.7): :class:`ContentAsset` entries keyed by
  canonical lead-form URN and by creative override.
* **config_hash**: SHA-256 of the canonical JSON of the three *parsed* files, so editing a
  comment leaves it unchanged while any value change alters it (AAP 0.3.2: a changed hash at
  worker startup enqueues a default-window analysis).
* **Overrides** (AAP 0.11.2 scenario fixtures): :meth:`Thresholds.load` also takes dotted-key
  ``overrides`` and a replacement registry, applied to the parsed files before validation, so
  no caller parses the YAML itself. :func:`config_fingerprint` hashes the files' raw bytes, a
  cheap change detector for configurations memoised across calls.

Load-time validation fails with :class:`ConfigError`, which names the file and dotted key:

* a file that is not UTF-8 or not valid YAML (a NUL character included), holds a duplicate
  key, or parses to a string key or value UTF-8 cannot encode (a lone surrogate from an
  escape such as ``"\uD800"``) fails, naming the file and, for such text, its key;
* every key ``config/thresholds.yaml`` ships is required, with its type and range checked;
* ``window.presets`` must contain ``window.default_days`` (AAP 0.5.4.1), and every preset,
  the trusted minimum and the sustained minimum fit within ``window.max_days``, itself at most
  LinkedIn's 180-day daily retention (AAP 0.2.4);
* a lead form or a creative listed under two assets fails (AAP 0.5.2); an override that
  contradicts its creative's own form mapping fails through :meth:`Thresholds.check_overrides`,
  which the runner calls against the stored creatives, because those are not known at file
  load (AAP 0.5.4.7);
* a ``crm`` key defined in both ``thresholds.yaml`` and ``hubspot_mapping.yaml`` fails.

Config directory (one resolver, used everywhere): :func:`resolve_config_dir` returns
``./config`` when it exists, else ``../config``, both relative to the working directory. In the
containers that is ``/app/config``, bind-mounted read-only; on the host, ``backend/`` resolves
``../config``. No environment variable selects it.

Purity: file reads only. No database, network, settings or logging, and no import of
``sync``, ``connectors``, ``simulation`` or ``api``. Ratios and money thresholds are exposed as
``Decimal``, never ``float``, so rule arithmetic on ``Decimal`` facts stays exact.

Example:
    >>> thresholds = Thresholds.load()  # doctest: +SKIP
    >>> thresholds.presets, thresholds.default_days  # doctest: +SKIP
    ((14, 28, 56, 90), 28)
    >>> thresholds.ratio("budget.capped_day_ratio")  # doctest: +SKIP
    Decimal('0.95')
    >>> thresholds.subset("k1.rule_of_three", "window.presets")  # doctest: +SKIP
    {'k1.rule_of_three': 3, 'window.presets': [14, 28, 56, 90]}
"""

from __future__ import annotations

import builtins
import hashlib
import json
import math
import re
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

import yaml

from ads_engine.db.repositories import ContentAssetRow

__all__ = [
    "CONFIG_FILES",
    "CONTENT_ASSETS_FILE",
    "CREATIVE_URN_PREFIX",
    "CRM_BINDINGS_DOCUMENT",
    "FORM_URN_PREFIX",
    "HUBSPOT_MAPPING_FILE",
    "LINKEDIN_DAILY_RETENTION_DAYS",
    "THRESHOLDS_FILE",
    "VERSIONED_FORM_URN_PREFIX",
    "BindingMode",
    "ConfigError",
    "ContentAsset",
    "CrmBindings",
    "GoogleCrmBinding",
    "HubSpotMapping",
    "ResolvedBinding",
    "StageBinding",
    "Thresholds",
    "config_fingerprint",
    "override_value",
    "resolve_config_dir",
]

THRESHOLDS_FILE: Final = "thresholds.yaml"
"""Rule thresholds, window settings and feedback durations."""

CONTENT_ASSETS_FILE: Final = "content_assets.yaml"
"""The white paper registry."""

HUBSPOT_MAPPING_FILE: Final = "hubspot_mapping.yaml"
"""The MQL and Qualified Lead bindings and the meeting rules."""

CONFIG_FILES: Final[tuple[str, ...]] = (THRESHOLDS_FILE, CONTENT_ASSETS_FILE, HUBSPOT_MAPPING_FILE)
"""The three files :meth:`Thresholds.load` reads from the config directory."""

CRM_BINDINGS_DOCUMENT: Final = "source_dataset.crm_bindings"
"""The ``ConfigError.file`` of a stored :class:`CrmBindings` document that fails to decode."""

FORM_URN_PREFIX: Final = "urn:li:leadGenForm:"
"""Prefix of a canonical LinkedIn lead form URN, the registry's join key for every version."""

VERSIONED_FORM_URN_PREFIX: Final = "urn:li:versionedLeadGenForm:"
"""Prefix of a versioned lead form URN, which the registry rejects in favour of the canonical."""

CREATIVE_URN_PREFIX: Final = "urn:li:sponsoredCreative:"
"""Prefix of a LinkedIn creative URN, the key of a registry creative override."""

LINKEDIN_DAILY_RETENTION_DAYS: Final = 180
"""LinkedIn keeps daily analytics for 6 months, so ``window.max_days`` may not exceed this."""

type BindingMode = Literal["stage_label", "property"]
"""How a CRM count is bound: a lifecycle stage label, or a contact date property."""

_BINDING_MODES: Final[tuple[str, ...]] = ("stage_label", "property")

_SLUG_RE: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FORM_URN_RE: Final = re.compile(r"urn:li:leadGenForm:[0-9]+")
_CREATIVE_URN_RE: Final = re.compile(r"urn:li:sponsoredCreative:[0-9]+")
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}")

# The ``crm`` keys of hubspot_mapping.yaml that are merged into the thresholds ``crm``
# namespace, beside crm.lead_maturity_days and crm.veto_min_leads from thresholds.yaml.
_MERGED_CRM_KEYS: Final[tuple[str, ...]] = ("booking_sources", "meeting_outcome_exclusions")
_MERGED_CRM_DOTTED_KEYS: Final[frozenset[str]] = frozenset(
    f"crm.{name}" for name in _MERGED_CRM_KEYS
)

_YAML_MERGE_TAG: Final = "tag:yaml.org,2002:merge"


# ---------------------------------------------------------------------------
# Errors and the config directory
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """A configuration file, or a stored CRM binding document, is missing or invalid.

    The worker, the CLI and the API surface it, and ``runner.py`` marks a run failed with it.

    Attributes:
        file: The file that failed (``thresholds.yaml``, ``content_assets.yaml``,
            ``hubspot_mapping.yaml``), :data:`CRM_BINDINGS_DOCUMENT` for a stored binding
            document, or the directory that was searched.
        key: The dotted key at fault, for example ``window.presets`` or
            ``assets[1].lead_forms[0]``; ``None`` when the whole file is at fault.
        detail: The problem, without the file and key prefix.
    """

    def __init__(self, file: str, key: str | None, detail: str) -> None:
        self.file = file
        self.key = key
        self.detail = detail
        location = file if key is None else f"{file}: {key}"
        super().__init__(f"{location}: {detail}")

    def __reduce__(self) -> tuple[type[ConfigError], tuple[str, str | None, str]]:
        """Pickle with the constructor's own arguments, not the formatted message."""
        return (type(self), (self.file, self.key, self.detail))


def _prefixed(error: ConfigError, prefix: str) -> ConfigError:
    """Re-anchor an error raised on a nested value under its parent's dotted key."""
    if error.key is None:
        key = prefix
    elif error.key.startswith("["):
        key = f"{prefix}{error.key}"
    else:
        key = f"{prefix}.{error.key}"
    return ConfigError(error.file, key, error.detail)


def resolve_config_dir(cwd: Path | None = None) -> Path:
    """Return the config directory: ``./config`` if it exists, else ``../config``.

    Both candidates are relative to the working directory: ``/app/config`` in the containers
    (bind-mounted read-only), ``../config`` when run from ``backend/`` on the host. No
    environment variable selects the directory, so every process resolves the same one.

    Args:
        cwd: The directory to resolve from; the process working directory when ``None``.

    Returns:
        The first candidate that is a directory.

    Raises:
        ConfigError: When neither candidate is a directory; the message names both.
    """
    base = Path.cwd() if cwd is None else Path(cwd)
    candidates = (base / "config", base.parent / "config")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise ConfigError(
        "config",
        None,
        f"no config directory: neither {candidates[0]} nor {candidates[1]} is a directory",
    )


# ---------------------------------------------------------------------------
# YAML reading and tree helpers
# ---------------------------------------------------------------------------


class _StrictSafeLoader(yaml.SafeLoader):
    """PyYAML's safe loader, rejecting duplicate keys within one mapping.

    ``yaml.safe_load`` keeps the last of two equal keys silently, so a pasted second ``k1:``
    block would quietly replace every K1 threshold. This loader builds exactly the same
    objects as ``yaml.safe_load`` and fails on such a duplicate instead. Keys brought in by a
    YAML merge (``<<``) may still be overridden explicitly, as the YAML spec allows.
    """


def _construct_unique_mapping(
    loader: _StrictSafeLoader, node: yaml.MappingNode
) -> dict[Hashable, Any]:
    """Construct a mapping node after checking its explicit keys are distinct."""
    seen: set[Hashable] = set()
    for key_node, _value_node in node.value:
        if key_node.tag == _YAML_MERGE_TAG:
            continue
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, Hashable):
            # construct_mapping reports unhashable keys itself.
            continue
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    loader.flatten_mapping(node)
    return loader.construct_mapping(node, deep=True)


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _read_yaml(directory: Path, name: str) -> Mapping[str, Any]:
    """Read one config file as a mapping with the strict safe loader.

    Args:
        directory: The config directory.
        name: The file name, one of :data:`CONFIG_FILES`.

    Returns:
        The parsed top-level mapping.

    Raises:
        ConfigError: When the file is missing or unreadable, is not UTF-8, is not valid YAML
            (including a character YAML forbids, such as NUL, or nesting too deep to parse),
            holds a duplicate key, or its top level is not a mapping.
    """
    path = directory / name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(name, None, f"file not found: {path}") from None
    except UnicodeDecodeError as exc:
        raise ConfigError(name, None, f"not valid UTF-8: {exc.reason}") from None
    except OSError as exc:
        raise ConfigError(name, None, f"cannot read {path}: {exc.strerror or exc}") from None
    # The loader's reader rejects characters YAML forbids as it is constructed, so the
    # construction belongs inside the same YAML-to-ConfigError boundary as the parse.
    loader: _StrictSafeLoader | None = None
    try:
        loader = _StrictSafeLoader(text)
        document: object = loader.get_single_data()
    except yaml.YAMLError as exc:
        raise ConfigError(name, None, f"invalid YAML: {exc}") from None
    except RecursionError:
        raise ConfigError(name, None, "invalid YAML: nesting is too deep to parse") from None
    finally:
        if loader is not None:
            loader.dispose()
    if not isinstance(document, Mapping):
        found = "an empty file" if document is None else type(document).__name__
        raise ConfigError(name, None, f"top level must be a mapping, found {found}")
    return document


def _config_directory(path: Path | str | None) -> Path:
    """Return the config directory to read: ``path``, else :func:`resolve_config_dir`.

    Raises:
        ConfigError: When the directory does not exist, naming it.
    """
    directory = resolve_config_dir() if path is None else Path(path)
    if not directory.is_dir():
        raise ConfigError(str(directory), None, "config directory not found")
    return directory


def config_fingerprint(path: Path | str | None = None) -> str:
    """Return a SHA-256 hex digest of the raw bytes of the three config files.

    AAP 0.6.2: the config files are read here, so a configuration memoised outside this
    module (the scenario catalogue's ``thresholds_with``, AAP 0.11.2) needs a cheap change
    detector that does not parse them again. It reads :data:`CONFIG_FILES` only, and
    any byte edit to any of them, a comment included, changes it. It is not ``config_hash``,
    which covers the parsed values and ignores comments.

    Args:
        path: The config directory; :func:`resolve_config_dir` when ``None``.

    Returns:
        The digest over each file's name, byte length and bytes, in :data:`CONFIG_FILES`
        order.

    Raises:
        ConfigError: When the directory is missing (naming it), or a file is missing or
            unreadable (naming the file).
    """
    directory = _config_directory(path)
    digest = hashlib.sha256()
    for name in CONFIG_FILES:
        file_path = directory / name
        try:
            data = file_path.read_bytes()
        except FileNotFoundError:
            raise ConfigError(name, None, f"file not found: {file_path}") from None
        except OSError as exc:
            raise ConfigError(
                name, None, f"cannot read {file_path}: {exc.strerror or exc}"
            ) from None
        digest.update(name.encode("utf-8"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def override_value(value: object) -> object:
    """Return the parsed-YAML value a threshold override stands for.

    The one conversion of override values (AAP 0.11.2 scenario fixtures override thresholds
    with ``Decimal`` and tuple values), used by :meth:`Thresholds.load` and by memo keys. A
    ``Decimal`` becomes the number YAML would parse from it: an integral one an ``int``
    (``Decimal("21")`` is ``21``), any other a ``float`` (``Decimal("0.90")`` is ``0.9``), and a
    non-finite one a non-finite ``float``, which validation rejects under its key. Tuples and
    lists become lists, and mappings dicts, item by item; every other value is unchanged. Two
    overrides that a YAML file would spell the same therefore normalise to equal values with
    equal ``repr``.

    Args:
        value: An override value.

    Returns:
        The equivalent parsed-YAML value.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            return math.nan if value.is_nan() else float(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {child: override_value(item) for child, item in value.items()}
    if isinstance(value, list | tuple):
        return [override_value(item) for item in value]
    return value


def _with_override(
    tree: Mapping[str, Any], parts: Sequence[str], value: object, file: str, key: str
) -> dict[str, Any]:
    """Return a copy of a parsed document with the existing dotted key ``key`` set to ``value``.

    Only the mappings along the key's path are copied, so the parsed document, and any node a
    YAML alias shares with another key, is never mutated: exactly one key changes.

    Args:
        tree: The mapping ``parts`` is relative to.
        parts: The remaining parts of ``key``.
        value: The replacement, already passed through :func:`override_value`.
        file: The file the key belongs to, for errors.
        key: The full dotted key, for errors.

    Raises:
        ConfigError: When the key does not exist, so a misspelt override fails loudly.
    """
    head, rest = parts[0], parts[1:]
    if head not in tree:
        raise ConfigError(file, key, "threshold override names a missing key")
    updated = dict(tree)
    if not rest:
        updated[head] = value
        return updated
    child: object = tree[head]
    if not isinstance(child, Mapping):
        raise ConfigError(file, key, "threshold override names a missing key")
    updated[head] = _with_override(child, rest, value, file, key)
    return updated


def _join(parent: str, child: str) -> str:
    """Join a dotted parent key and a child key or ``[index]`` suffix."""
    if not parent:
        return child
    return f"{parent}{child}" if child.startswith("[") else f"{parent}.{child}"


def _describe(value: object) -> str:
    """Render a found value for an error message, bounded in length."""
    text = repr(value)
    return text if len(text) <= 60 else f"{text[:57]}..."


def _lone_surrogate(text: str) -> str | None:
    """Return the first character of ``text`` UTF-8 cannot encode (a lone surrogate), if any."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return text[exc.start]
    return None


def _check_encodable(value: object, file: str, key: str) -> None:
    r"""Fail on any string key or string value of a parsed tree that UTF-8 cannot encode.

    A YAML escape such as ``"\uD800"`` (or a ``\u`` surrogate pair, which PyYAML keeps as
    two lone surrogates) parses to text that no UTF-8 file, JSON document or log line can
    carry, so ``config_hash`` and any message quoting it would fail. Messages here quote such
    text with ``repr``, which escapes surrogates, so the error itself is always encodable.

    Args:
        value: The parsed value.
        file: The file it came from, for errors.
        key: Its dotted key, ``""`` at the top level.

    Raises:
        ConfigError: On a string value that cannot be encoded, under its dotted key; on a
            mapping key that cannot be encoded, under the mapping's dotted key (``None`` at
            the top level), naming the key in escaped form.
    """
    if isinstance(value, str):
        surrogate = _lone_surrogate(value)
        if surrogate is not None:
            raise ConfigError(
                file,
                key or None,
                f"must be UTF-8 encodable text, found the lone surrogate {surrogate!r} in "
                f"{_describe(value)}; write the character itself instead of a surrogate escape",
            )
        return
    if isinstance(value, Mapping):
        for child, item in value.items():
            if isinstance(child, str):
                surrogate = _lone_surrogate(child)
                if surrogate is not None:
                    raise ConfigError(
                        file,
                        key or None,
                        f"key {child!r} must be UTF-8 encodable text, found the lone surrogate "
                        f"{surrogate!r}; write the character itself instead of a surrogate escape",
                    )
            _check_encodable(item, file, _join(key, str(child)))
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check_encodable(item, file, _join(key, f"[{index}]"))


def _freeze(value: object, file: str, key: str) -> object:
    """Deep-freeze a parsed YAML tree: mappings become read-only proxies, lists tuples.

    Args:
        value: The parsed value.
        file: The file it came from, for errors.
        key: Its dotted key, ``""`` at the top level.

    Returns:
        The frozen value. Scalars (``None``, ``bool``, ``int``, finite ``float``, ``str``,
        ``date``, ``datetime``) are returned unchanged.

    Raises:
        ConfigError: On a key that is not a non-empty string without dots (dotted access
            would be ambiguous), a non-finite number, or a value type YAML tags such as
            ``!!binary`` or ``!!set`` produce, which no threshold uses.
    """
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for child, item in value.items():
            if not isinstance(child, str) or not child or "." in child:
                raise ConfigError(
                    file, key or None, f"key {child!r} must be a non-empty string without dots"
                )
            frozen[child] = _freeze(item, file, _join(key, child))
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item, file, _join(key, f"[{i}]")) for i, item in enumerate(value))
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(file, key or None, f"must be a finite number, found {value!r}")
    if value is None or isinstance(value, bool | int | float | str | date):
        return value
    raise ConfigError(file, key or None, f"unsupported value type {type(value).__name__}")


def _json_safe(value: object) -> Any:
    """Thaw a frozen tree into plain JSON types: dicts, lists, ISO-8601 dates."""
    if isinstance(value, Mapping):
        return {str(child): _json_safe(item) for child, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, date):  # also datetime, a subclass
        return value.isoformat()
    return value


def _canonical(value: object) -> object:
    """Normalise any mapping to ``dict`` and any sequence to ``list`` for hashing."""
    if isinstance(value, Mapping):
        return {child: _canonical(item) for child, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def _config_hash(
    thresholds: Mapping[str, Any],
    content_assets: Mapping[str, Any],
    hubspot_mapping: Mapping[str, Any],
) -> str:
    """Return the SHA-256 hex digest of the canonical JSON of the three parsed files.

    The parsed trees are hashed, not the file bytes, so a comment-only edit keeps the hash and
    any value change alters it. Keys are sorted and separators compact; dates render through
    ``str``.

    Raises:
        ConfigError: When a tree cannot be serialised, for example a non-string key, or its
            JSON cannot be encoded as UTF-8, for example a lone surrogate.
    """
    document = {
        "thresholds": _canonical(thresholds),
        "content_assets": _canonical(content_assets),
        "hubspot_mapping": _canonical(hubspot_mapping),
    }
    try:
        canonical = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        encoded = canonical.encode("utf-8")
    except (TypeError, ValueError) as exc:  # UnicodeEncodeError is a ValueError
        raise ConfigError("config", None, f"cannot compute config_hash: {exc}") from None
    return hashlib.sha256(encoded).hexdigest()


def _lookup(tree: Mapping[str, object], key: str, file: str) -> object:
    """Return the value at a dotted key of a frozen tree.

    Raises:
        ConfigError: When the key is empty, a prefix is not a mapping, or the key is missing.
    """
    if not key:
        raise ConfigError(file, key, "empty threshold key")
    node: object = tree
    walked: list[str] = []
    for part in key.split("."):
        if not isinstance(node, Mapping):
            raise ConfigError(file, key, f"{'.'.join(walked)} is not a mapping")
        if part not in node:
            raise ConfigError(file, key, "required key is missing")
        node = node[part]
        walked.append(part)
    return node


def _text_problem(value: object) -> str | None:
    """Return why a value is not a non-blank string, or ``None`` when it is one."""
    if not isinstance(value, str):
        return f"must be a string, found {_describe(value)}"
    if not value.strip():
        return "must not be blank"
    return None


def _name_problem(value: object) -> str | None:
    """Return why a value is not a property name: non-blank, no surrounding whitespace."""
    problem = _text_problem(value)
    if problem is None and isinstance(value, str) and value != value.strip():
        return f"must not have surrounding whitespace, found {value!r}"
    return problem


def _optional_text_problem(value: object) -> str | None:
    """Like :func:`_text_problem`, also accepting ``None``."""
    return None if value is None else _text_problem(value)


def _string_tuple(value: object, file: str, key: str) -> tuple[str, ...]:
    """Validate a YAML list of distinct non-blank strings and return it as a tuple.

    Raises:
        ConfigError: When the value is not a list, an item is not a non-blank string, or an
            item repeats.
    """
    if isinstance(value, str | Mapping) or not isinstance(value, Sequence):
        raise ConfigError(file, key, f"must be a list of strings, found {_describe(value)}")
    items: list[str] = []
    for index, item in enumerate(value):
        problem = _text_problem(item)
        if problem is not None:
            raise ConfigError(file, _join(key, f"[{index}]"), problem)
        if item in items:
            raise ConfigError(file, _join(key, f"[{index}]"), f"{item!r} is listed twice")
        items.append(item)
    return tuple(items)


def _reject_unknown(
    document: Mapping[Any, Any], allowed: Sequence[str], file: str, key: str | None
) -> None:
    """Fail on keys outside ``allowed``, so a misspelt key is never ignored silently."""
    unknown = sorted(str(name) for name in document if name not in allowed)
    if unknown:
        raise ConfigError(
            file, key, f"unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}"
        )


def _exact_keys(data: object, expected: Sequence[str], file: str, key: str | None) -> None:
    """Fail unless ``data`` is a mapping with exactly the ``expected`` keys."""
    if not isinstance(data, Mapping):
        raise ConfigError(file, key, f"must be a mapping, found {_describe(data)}")
    _reject_unknown(data, expected, file, key)
    missing = [name for name in expected if name not in data]
    if missing:
        raise ConfigError(file, key, f"missing key(s) {', '.join(missing)}")


# ---------------------------------------------------------------------------
# thresholds.yaml schema
# ---------------------------------------------------------------------------

type _Kind = Literal[
    "count", "count0", "share", "level", "positive", "nonneg", "percentile", "presets"
]
"""Value kinds of thresholds.yaml keys.

``count``: integer >= 1 (days, hours, clicks, impressions, leads, terms, variants).
``count0``: integer >= 0. ``share``: number in (0, 1]. ``level``: number in (0, 1)
(significance, power, confidence). ``positive``: number > 0 (multiples, ratios, steps, lifts,
outcome minimums that may be compared with fractional conversions). ``nonneg``: number >= 0.
``percentile``: number in [0, 100]. ``presets``: a non-empty list of ``count`` values.
Integer kinds take YAML integers only (``7``, not ``7.0``), so :meth:`Thresholds.int` never
fails on a validated key.
"""

_THRESHOLD_SCHEMA: Final[Mapping[str, _Kind]] = MappingProxyType(
    {
        # Analysis window (AAP 0.5.4.1).
        "window.default_days": "count",
        "window.trusted_min_days": "count",
        "window.presets": "presets",
        "window.sustained_min_days": "count",
        "window.max_days": "count",
        # P4 stale flag (AAP 0.3.2, 0.5.4.2).
        "sources.stale_after_hours": "count",
        # Budget history, utilization and history gates (AAP 0.5.4.1).
        "budget.max_observation_gap_hours": "count",
        "budget.capped_day_ratio": "share",
        "budget.capped_lost_is_share": "share",
        "budget.high_utilization": "share",
        "budget.under_utilization": "share",
        "budget.min_known_days": "count",
        "budget.min_spending_days": "count",
        "budget.min_capped_days": "count",
        "budget.sustained_share": "share",
        # CRM pipeline link (AAP 0.5.4.1); booking_sources and meeting_outcome_exclusions
        # arrive from hubspot_mapping.yaml.
        "crm.lead_maturity_days": "count0",
        "crm.veto_min_leads": "count",
        # Attribution floors (AAP 0.5.4.7).
        "attribution.floor_share": "share",
        # Keywords section finding (AAP 0.5.4.3).
        "keywords.min_search_term_coverage": "share",
        # K1-K3 (AAP 0.5.4.3).
        "k1.baseline_days": "count",
        "k1.min_clicks_floor": "count",
        "k1.rule_of_three": "positive",
        "k1.min_baseline_conversions": "positive",
        "k1.min_spend_cpa_multiple": "nonneg",
        "k2.min_conversions": "positive",
        "k2.max_cpa_ratio": "positive",
        "k2.adjacent_jaccard": "share",
        "k2.adjacent_max_terms": "count",
        "k2.adjacent_min_clicks": "count0",
        "k3.min_window_days": "count",
        "k3.min_clicks_per_half": "count",
        "k3.qs_span_days": "count",
        "k3.min_qs_days_per_span": "count",
        "k3.qs_drop": "positive",
        "k3.rise_ratio": "positive",
        "k3.min_first_half_conversions": "positive",
        # C1-C3b (AAP 0.5.4.4).
        "c1.min_window_days": "count",
        "c1.min_spend_day_share": "share",
        "c1.min_expected_outcomes": "positive",
        "c1b.min_window_days": "count",
        "scale.min_outcomes": "positive",
        "scale.max_cpa_ratio": "positive",
        "c3a.min_leads": "count",
        "c3a.max_cpl_ratio": "positive",
        "c3a.max_impression_share": "share",
        "c3b.jaccard": "share",
        "c3b.min_terms": "count",
        "c3b.min_conversions": "positive",
        "c3b.head_token_share": "share",
        # B1-B4 and G1 (AAP 0.5.4.5).
        "b1.step": "positive",
        "b2.poor_efficiency_ratio": "positive",
        "b2.min_expected_outcomes": "positive",
        "b3.spend_percentile": "percentile",
        "b3.headroom": "positive",
        "b4.min_weekend_days": "count",
        "b4.min_weekdays": "count",
        "b4.min_outcomes": "count",
        "b4.alpha": "level",
        # CR1-CR3 (AAP 0.5.4.6).
        "cr.min_impressions": "count",
        "cr.min_variants": "count",
        "cr.alpha": "level",
        "cr.power": "level",
        "cr.target_lift": "positive",
        "cr.fisher_min_expected": "positive",
        "cr.ci_level": "level",
        # W1, W2 (AAP 0.5.4.7).
        "w.min_ql": "count",
        "w.median_min_ql": "count",
        "w.double_down_ratio": "positive",
        "w.retire_spend_multiple": "positive",
        "w.retire_min_window_days": "count",
        "w.cohort_min_total_ql": "count0",
        # Feedback suppression (AAP 0.5.2).
        "feedback.dismiss_days": "count",
        "feedback.snooze_days": "count",
        "feedback.done_days": "count",
    }
)
"""Every key config/thresholds.yaml must define, with its value kind.

The file is the single source of these values: no rule falls back to a code default. A key
may be added to the file without a schema entry (its value is still frozen and readable), but
a key listed here and absent from the file fails config load.
"""


_NUMBER_RANGES: Final[Mapping[str, tuple[Callable[[Decimal], bool], str]]] = MappingProxyType(
    {
        "share": (lambda d: Decimal(0) < d <= Decimal(1), "in (0, 1]"),
        "level": (lambda d: Decimal(0) < d < Decimal(1), "in (0, 1)"),
        "positive": (lambda d: d > Decimal(0), "> 0"),
        "nonneg": (lambda d: d >= Decimal(0), ">= 0"),
        "percentile": (lambda d: Decimal(0) <= d <= Decimal(100), "in [0, 100]"),
    }
)
"""The accepted range of each numeric kind, with its wording for error messages."""


def _number(value: object, key: str) -> Decimal:
    """Return a YAML number as an exact ``Decimal``; ``bool`` is not a number.

    Raises:
        ConfigError: When the value is not an ``int`` or ``float``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(THRESHOLDS_FILE, key, f"must be a number, found {_describe(value)}")
    return Decimal(str(value))


def _integer(value: object, key: str, minimum: int) -> int:
    """Return a YAML integer of at least ``minimum``; ``bool`` and floats are rejected.

    Raises:
        ConfigError: When the value is not an integer or is below ``minimum``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(THRESHOLDS_FILE, key, f"must be an integer, found {_describe(value)}")
    if value < minimum:
        raise ConfigError(THRESHOLDS_FILE, key, f"must be >= {minimum}, found {value}")
    return value


def _check_kind(value: object, kind: _Kind, key: str) -> None:
    """Validate one thresholds.yaml value against its kind.

    Raises:
        ConfigError: When the value's type or range does not fit the kind.
    """
    match kind:
        case "count":
            _integer(value, key, 1)
        case "count0":
            _integer(value, key, 0)
        case "presets":
            if isinstance(value, str) or not isinstance(value, Sequence) or not value:
                raise ConfigError(
                    THRESHOLDS_FILE,
                    key,
                    f"must be a non-empty list of day counts, found {_describe(value)}",
                )
            for index, item in enumerate(value):
                _integer(item, _join(key, f"[{index}]"), 1)
        case "share" | "level" | "positive" | "nonneg" | "percentile":
            number = _number(value, key)
            accepts, expected = _NUMBER_RANGES[kind]
            if not accepts(number):
                raise ConfigError(
                    THRESHOLDS_FILE, key, f"must be {expected}, found {_describe(value)}"
                )


def _validate_thresholds(tree: Mapping[str, object]) -> None:
    """Validate the merged thresholds tree: every schema key, then cross-key rules.

    Cross-key rules (AAP 0.5.4.1, 0.2.4):

    * ``window.max_days`` <= :data:`LINKEDIN_DAILY_RETENTION_DAYS`;
    * ``window.default_days``, ``window.trusted_min_days``, ``window.sustained_min_days`` and
      every preset <= ``window.max_days``;
    * ``window.presets`` contains ``window.default_days``;
    * ``budget.under_utilization`` < ``budget.high_utilization``, so no day is both;
    * ``k3.min_qs_days_per_span`` <= ``k3.qs_span_days``, or the quality-score branch could
      never fire;
    * ``cr.min_variants`` >= 2, since CR1 compares pairs.

    Raises:
        ConfigError: Naming the first failing key.
    """
    for key, kind in _THRESHOLD_SCHEMA.items():
        _check_kind(_lookup(tree, key, THRESHOLDS_FILE), kind, key)

    def count(key: str) -> int:
        return _integer(_lookup(tree, key, THRESHOLDS_FILE), key, 0)

    max_days = count("window.max_days")
    if max_days > LINKEDIN_DAILY_RETENTION_DAYS:
        raise ConfigError(
            THRESHOLDS_FILE,
            "window.max_days",
            f"must be <= {LINKEDIN_DAILY_RETENTION_DAYS} (LinkedIn daily analytics "
            f"retention), found {max_days}",
        )
    for key in ("window.default_days", "window.trusted_min_days", "window.sustained_min_days"):
        if count(key) > max_days:
            raise ConfigError(
                THRESHOLDS_FILE,
                key,
                f"must be <= window.max_days ({max_days}), found {count(key)}",
            )
    presets = _lookup(tree, "window.presets", THRESHOLDS_FILE)
    # Already checked as a non-empty list by its schema kind; narrowed here for typing.
    preset_items: Sequence[object] = presets if isinstance(presets, Sequence) else ()
    for index, preset in enumerate(preset_items):
        if _integer(preset, f"window.presets[{index}]", 1) > max_days:
            raise ConfigError(
                THRESHOLDS_FILE,
                f"window.presets[{index}]",
                f"must be <= window.max_days ({max_days}), found {preset}",
            )
    default_days = count("window.default_days")
    if default_days not in preset_items:
        raise ConfigError(
            THRESHOLDS_FILE,
            "window.presets",
            f"must contain window.default_days ({default_days}), found {_describe(presets)}",
        )

    def number(key: str) -> Decimal:
        return _number(_lookup(tree, key, THRESHOLDS_FILE), key)

    if number("budget.under_utilization") >= number("budget.high_utilization"):
        raise ConfigError(
            THRESHOLDS_FILE,
            "budget.under_utilization",
            "must be below budget.high_utilization, so no day is both under- and high-utilized",
        )
    if count("k3.min_qs_days_per_span") > count("k3.qs_span_days"):
        raise ConfigError(
            THRESHOLDS_FILE,
            "k3.min_qs_days_per_span",
            f"must be <= k3.qs_span_days ({count('k3.qs_span_days')})",
        )
    if count("cr.min_variants") < 2:
        raise ConfigError(
            THRESHOLDS_FILE, "cr.min_variants", "must be >= 2: CR1 compares variants in pairs"
        )


# ---------------------------------------------------------------------------
# HubSpot mapping (AAP 0.5.4.7)
# ---------------------------------------------------------------------------
#
# The binding classes below have a field named ``property``, as the YAML and the stored JSON
# do. A field of that name shadows the ``property`` decorator inside the class body, so these
# classes define plain methods only.


@dataclass(frozen=True, slots=True)
class StageBinding:
    """How one CRM count (MQL or Qualified Lead) is bound to the portal, as configured.

    AAP 0.5.4.7. In ``stage_label`` mode the labels are resolved at startup, in order, among
    the ``lifecyclestage`` options; the first that matches wins, and the count uses that
    stage's ``hs_v2_date_entered_{stage id}`` property. In ``property`` mode a contact date or
    datetime property records the event directly; any labels are then kept for display only.

    The portal's default "Marketing Qualified Lead" stage is never assumed to mean "First
    meeting booked": MQL uses it only when the configuration names it. QL is observed from its
    own bound property and is never derived from MQL.

    Attributes:
        mode: ``stage_label`` or ``property``.
        stage_labels: The labels to try, in order. MQL's single ``stage_label`` is normalised
            to a one-tuple; QL's ``stage_labels`` keep their file order.
        property: The bound contact property in ``property`` mode; ``None`` in
            ``stage_label`` mode.

    Raises:
        ConfigError: On construction, when ``stage_label`` mode has no label or names a
            property, ``property`` mode has no property, or a label or name is blank.
    """

    mode: BindingMode
    stage_labels: tuple[str, ...]
    property: str | None

    def __post_init__(self) -> None:
        """Enforce the per-mode invariants."""
        file = HUBSPOT_MAPPING_FILE
        mode: object = self.mode
        if mode not in _BINDING_MODES:
            raise ConfigError(
                file, "mode", f"must be one of {', '.join(_BINDING_MODES)}, found {mode!r}"
            )
        labels: object = self.stage_labels
        if not isinstance(labels, tuple):
            raise ConfigError(file, "stage_labels", "must be a tuple of labels")
        for index, label in enumerate(labels):
            problem = _text_problem(label)
            if problem is not None:
                raise ConfigError(file, f"stage_labels[{index}]", problem)
        if self.property is not None:
            problem = _name_problem(self.property)
            if problem is not None:
                raise ConfigError(file, "property", problem)
        if mode == "stage_label":
            if not labels:
                raise ConfigError(file, "stage_labels", "stage_label mode needs at least one label")
            if self.property is not None:
                raise ConfigError(
                    file, "property", "must be null in stage_label mode; use mode: property"
                )
        elif self.property is None:
            raise ConfigError(file, "property", "property mode needs a contact property name")

    def to_json(self) -> dict[str, Any]:
        """Return the binding as plain JSON: ``{mode, stage_labels, property}``."""
        return {
            "mode": self.mode,
            "stage_labels": list(self.stage_labels),
            "property": self.property,
        }


def _parse_stage_binding(document: object, name: str) -> StageBinding:
    """Parse ``mql`` or ``ql`` of hubspot_mapping.yaml.

    Either key may carry a single ``stage_label`` or a ``stage_labels`` list, never both.

    Raises:
        ConfigError: Under the binding's dotted key.
    """
    file = HUBSPOT_MAPPING_FILE
    if not isinstance(document, Mapping):
        raise ConfigError(file, name, f"must be a mapping, found {_describe(document)}")
    _reject_unknown(document, ("mode", "stage_label", "stage_labels", "property"), file, name)
    if "mode" not in document:
        raise ConfigError(file, f"{name}.mode", "required key is missing")
    single = document.get("stage_label")
    many = document.get("stage_labels")
    labels: tuple[str, ...]
    if single is not None and many is not None:
        raise ConfigError(file, name, "set stage_label or stage_labels, not both")
    if single is not None:
        problem = _text_problem(single)
        if problem is not None:
            raise ConfigError(file, f"{name}.stage_label", problem)
        labels = (single,)
    elif many is not None:
        labels = _string_tuple(many, file, f"{name}.stage_labels")
    else:
        labels = ()
    try:
        return StageBinding(
            mode=document["mode"], stage_labels=labels, property=document.get("property")
        )
    except ConfigError as exc:
        raise _prefixed(exc, name) from None


@dataclass(frozen=True, slots=True)
class HubSpotMapping:
    """``config/hubspot_mapping.yaml``: the MQL and QL bindings and the meeting rules.

    AAP 0.5.4.7. MQL and QL are bound independently and never blended. A meeting counts as
    "booked after touch" only when its source is in ``booking_sources`` (for those sources the
    record's creation is the booking) and its outcome is not in
    ``meeting_outcome_exclusions``. Both lists are also exposed as the thresholds keys
    ``crm.booking_sources`` and ``crm.meeting_outcome_exclusions``.

    Attributes:
        mql: The MQL binding (default: the stage labelled "First meeting booked").
        ql: The Qualified Lead binding (default: "Qualified Lead", then "Sales Qualified
            Lead").
        booking_sources: ``hs_meeting_source`` values whose meetings can count as booked.
        meeting_outcome_exclusions: ``hs_meeting_outcome`` values never counted.
    """

    mql: StageBinding
    ql: StageBinding
    booking_sources: tuple[str, ...]
    meeting_outcome_exclusions: tuple[str, ...]

    def __post_init__(self) -> None:
        """Check the field types and the meeting lists' contents."""
        file = HUBSPOT_MAPPING_FILE
        for name in ("mql", "ql"):
            binding: object = getattr(self, name)
            if not isinstance(binding, StageBinding):
                raise ConfigError(file, name, "must be a StageBinding")
        for name in _MERGED_CRM_KEYS:
            values: object = getattr(self, name)
            if not isinstance(values, tuple):
                raise ConfigError(file, f"crm.{name}", "must be a tuple of strings")
            _string_tuple(values, file, f"crm.{name}")

    def to_json(self) -> dict[str, Any]:
        """Return the mapping as plain JSON, the ``hubspot_mapping`` of ``GET /config``."""
        return {
            "mql": self.mql.to_json(),
            "ql": self.ql.to_json(),
            "booking_sources": list(self.booking_sources),
            "meeting_outcome_exclusions": list(self.meeting_outcome_exclusions),
        }


def _parse_mapping(document: Mapping[str, Any]) -> HubSpotMapping:
    """Parse hubspot_mapping.yaml; every top-level key and both ``crm`` lists are required.

    Raises:
        ConfigError: Naming the failing key.
    """
    file = HUBSPOT_MAPPING_FILE
    _exact_keys(document, ("mql", "ql", "crm"), file, None)
    crm = document["crm"]
    _exact_keys(crm, _MERGED_CRM_KEYS, file, "crm")
    return HubSpotMapping(
        mql=_parse_stage_binding(document["mql"], "mql"),
        ql=_parse_stage_binding(document["ql"], "ql"),
        booking_sources=_string_tuple(crm["booking_sources"], file, "crm.booking_sources"),
        meeting_outcome_exclusions=_string_tuple(
            crm["meeting_outcome_exclusions"], file, "crm.meeting_outcome_exclusions"
        ),
    )


# ---------------------------------------------------------------------------
# Resolved CRM bindings, persisted as source_dataset.crm_bindings
# ---------------------------------------------------------------------------

_RESOLVED_KEYS: Final[tuple[str, ...]] = ("available", "property", "stage_id", "label", "reason")
_GOOGLE_KEYS: Final[tuple[str, ...]] = (
    "available",
    "campaign_property",
    "touch_date_property",
    "reason",
)
_BINDINGS_KEYS: Final[tuple[str, ...]] = ("mql", "ql", "google", "verified_at")


def _check_optional_texts(owner: object, names: Sequence[str], file: str) -> None:
    """Fail when any named attribute is neither ``None`` nor a non-blank string."""
    for name in names:
        problem = _optional_text_problem(getattr(owner, name))
        if problem is not None:
            raise ConfigError(file, name, problem)


def _decode_part[T](decode: Callable[[Mapping[str, Any]], T], data: Any, name: str) -> T:
    """Decode one nested part of a stored document, re-anchoring errors under ``name``."""
    try:
        return decode(data)
    except ConfigError as exc:
        raise _prefixed(exc, name) from None


def _check_bool(value: object, key: str, file: str) -> None:
    """Fail unless ``value`` is exactly ``True`` or ``False``."""
    if not isinstance(value, bool):
        raise ConfigError(file, key, f"must be true or false, found {_describe(value)}")


@dataclass(frozen=True, slots=True)
class ResolvedBinding:
    """One CRM count's binding as verified against the portal's own property definitions.

    AAP 0.3.3 startup property verification, 0.5.4.7. An unavailable binding makes its count
    "unavailable", shown as such and never as 0, with ``reason`` naming the missing stage or
    property.

    Attributes:
        available: Whether the count can be computed.
        property: The contact date property the count reads, ``hs_v2_date_entered_{stage id}``
            or the configured property; required when available.
        stage_id: The lifecycle stage id the label resolved to, in ``stage_label`` mode.
        label: The label this binding resolved: the lifecycle stage label that matched in
            ``stage_label`` mode, or the configured display label kept in ``property`` mode,
            when one is set. Only the MQL binding's label heads the white-paper MQL column,
            e.g. "MQL (First meeting booked)".
        reason: Why the binding is unavailable, for example "stage label 'First meeting
            booked' not found in lifecyclestage options"; required when unavailable.

    Raises:
        ConfigError: On construction, with :data:`CRM_BINDINGS_DOCUMENT` as the file, when an
            available binding names no property or an unavailable one gives no reason.
    """

    available: bool
    property: str | None = None
    stage_id: str | None = None
    label: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce types and the availability invariants."""
        file = CRM_BINDINGS_DOCUMENT
        _check_bool(self.available, "available", file)
        _check_optional_texts(self, ("property", "stage_id", "label", "reason"), file)
        if self.available and self.property is None:
            raise ConfigError(file, "property", "an available binding must name its property")
        if not self.available and self.reason is None:
            raise ConfigError(file, "reason", "an unavailable binding must state its reason")

    def to_json(self) -> dict[str, Any]:
        """Return the binding as plain JSON with every key present."""
        return {
            "available": self.available,
            "property": self.property,
            "stage_id": self.stage_id,
            "label": self.label,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ResolvedBinding:
        """Decode :meth:`to_json` output strictly: exactly its keys, correctly typed.

        Raises:
            ConfigError: On a missing or unknown key or a wrongly typed value.
        """
        _exact_keys(data, _RESOLVED_KEYS, CRM_BINDINGS_DOCUMENT, None)
        return cls(**{name: data[name] for name in _RESOLVED_KEYS})


@dataclass(frozen=True, slots=True)
class GoogleCrmBinding:
    """The optional Google-to-CRM link, as verified against the portal (AAP 0.3.3).

    The link exists only when ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and
    ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` are both configured and both confirmed by the
    properties API. Otherwise Google items carry ``no_crm_link`` and no Google QL count or
    pipeline veto is computed.

    Attributes:
        available: Whether the link can be used.
        campaign_property: The contact property holding the Google campaign id.
        touch_date_property: The datetime property holding when that touch happened.
        reason: Why the link is unavailable, for example
            "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"; required when unavailable.

    Raises:
        ConfigError: On construction, when an available link lacks a property or an
            unavailable one gives no reason.
    """

    available: bool
    campaign_property: str | None = None
    touch_date_property: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce types and the availability invariants."""
        file = CRM_BINDINGS_DOCUMENT
        _check_bool(self.available, "available", file)
        _check_optional_texts(self, ("campaign_property", "touch_date_property", "reason"), file)
        if self.available:
            for name in ("campaign_property", "touch_date_property"):
                if getattr(self, name) is None:
                    raise ConfigError(file, name, "an available Google link needs both properties")
        elif self.reason is None:
            raise ConfigError(file, "reason", "an unavailable Google link must state its reason")

    def to_json(self) -> dict[str, Any]:
        """Return the link as plain JSON with every key present."""
        return {
            "available": self.available,
            "campaign_property": self.campaign_property,
            "touch_date_property": self.touch_date_property,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GoogleCrmBinding:
        """Decode :meth:`to_json` output strictly: exactly its keys, correctly typed.

        Raises:
            ConfigError: On a missing or unknown key or a wrongly typed value.
        """
        _exact_keys(data, _GOOGLE_KEYS, CRM_BINDINGS_DOCUMENT, None)
        return cls(**{name: data[name] for name in _GOOGLE_KEYS})


@dataclass(frozen=True, slots=True)
class CrmBindings:
    """Every CRM binding verified against one HubSpot portal, stored per HubSpot dataset.

    ``connectors/hubspot/client.verify_bindings`` produces it, ``sync/datasets.py`` persists
    :meth:`to_json` as ``source_dataset.crm_bindings``, and attribution and ``GET /config``
    read it back with :meth:`from_json`. A live and a simulated portal resolve different stage
    ids, so the bindings belong to the dataset and retire with it.

    Attributes:
        mql: The resolved MQL binding.
        ql: The resolved Qualified Lead binding, independent of MQL.
        google: The Google-to-CRM link.
        verified_at: When the portal was checked; tz-aware.

    Raises:
        ConfigError: On construction, when a part has the wrong type or ``verified_at`` is
            naive.

    Example:
        >>> at = datetime(2026, 10, 7, tzinfo=UTC)
        >>> bindings = CrmBindings.unavailable("not yet verified", at=at)
        >>> CrmBindings.from_json(bindings.to_json()) == bindings
        True
    """

    mql: ResolvedBinding
    ql: ResolvedBinding
    google: GoogleCrmBinding
    verified_at: datetime

    def __post_init__(self) -> None:
        """Check the part types and that ``verified_at`` is tz-aware."""
        file = CRM_BINDINGS_DOCUMENT
        for name, expected in (("mql", ResolvedBinding), ("ql", ResolvedBinding)):
            if not isinstance(getattr(self, name), expected):
                raise ConfigError(file, name, f"must be a {expected.__name__}")
        google: object = self.google
        if not isinstance(google, GoogleCrmBinding):
            raise ConfigError(file, "google", "must be a GoogleCrmBinding")
        verified_at: object = self.verified_at
        if not isinstance(verified_at, datetime) or verified_at.utcoffset() is None:
            raise ConfigError(
                file, "verified_at", f"must be a tz-aware datetime, found {_describe(verified_at)}"
            )

    def to_json(self) -> dict[str, Any]:
        """Return plain JSON: the three bindings and ``verified_at`` as ISO-8601 UTC."""
        return {
            "mql": self.mql.to_json(),
            "ql": self.ql.to_json(),
            "google": self.google.to_json(),
            "verified_at": self.verified_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CrmBindings:
        """Decode a stored ``crm_bindings`` document strictly.

        ``from_json(b.to_json()) == b`` holds for every ``b``.

        Args:
            data: The stored document.

        Returns:
            The decoded bindings.

        Raises:
            ConfigError: With :data:`CRM_BINDINGS_DOCUMENT` as the file and the dotted key,
                on a missing or unknown key, a wrongly typed value or an invalid timestamp.
        """
        file = CRM_BINDINGS_DOCUMENT
        _exact_keys(data, _BINDINGS_KEYS, file, None)
        mql = _decode_part(ResolvedBinding.from_json, data["mql"], "mql")
        ql = _decode_part(ResolvedBinding.from_json, data["ql"], "ql")
        google = _decode_part(GoogleCrmBinding.from_json, data["google"], "google")
        stamp: object = data["verified_at"]
        if not isinstance(stamp, str):
            raise ConfigError(
                file, "verified_at", f"must be an ISO-8601 string, found {_describe(stamp)}"
            )
        try:
            verified_at = datetime.fromisoformat(stamp)
        except ValueError:
            raise ConfigError(
                file, "verified_at", f"invalid ISO-8601 timestamp {_describe(stamp)}"
            ) from None
        return cls(mql=mql, ql=ql, google=google, verified_at=verified_at)

    @classmethod
    def unavailable(cls, reason: str, *, at: datetime) -> CrmBindings:
        """Return bindings with every count and the Google link unavailable for ``reason``.

        Used when a HubSpot dataset has no ``crm_bindings`` yet, so attribution reports MQL,
        QL and the Google link as unavailable rather than 0.

        Args:
            reason: Why nothing is available, for example "CRM bindings not yet verified".
            at: The instant to record as ``verified_at``; tz-aware.

        Returns:
            The all-unavailable bindings.

        Raises:
            ConfigError: When ``reason`` is blank or ``at`` is naive.
        """
        return cls(
            mql=ResolvedBinding(available=False, reason=reason),
            ql=ResolvedBinding(available=False, reason=reason),
            google=GoogleCrmBinding(available=False, reason=reason),
            verified_at=at,
        )


# ---------------------------------------------------------------------------
# White paper registry (AAP 0.5.2, 0.5.4.7)
# ---------------------------------------------------------------------------


def _form_urn_problem(urn: object) -> str | None:
    """Return why ``urn`` is not a canonical lead form URN, or ``None`` when it is one."""
    problem = _text_problem(urn)
    if problem is not None or not isinstance(urn, str):
        return problem
    if urn.startswith(VERSIONED_FORM_URN_PREFIX):
        return (
            f"{urn!r} is a versioned lead form URN; list the canonical {FORM_URN_PREFIX}{{id}}, "
            "which covers every version of the form"
        )
    if not _FORM_URN_RE.fullmatch(urn):
        return f"must be a canonical lead form URN {FORM_URN_PREFIX}{{digits}}, found {urn!r}"
    return None


def _creative_urn_problem(urn: object) -> str | None:
    """Return why ``urn`` is not a creative URN, or ``None`` when it is one."""
    problem = _text_problem(urn)
    if problem is not None or not isinstance(urn, str):
        return problem
    if not _CREATIVE_URN_RE.fullmatch(urn):
        return f"must be a creative URN {CREATIVE_URN_PREFIX}{{digits}}, found {urn!r}"
    return None


@dataclass(frozen=True, slots=True)
class ContentAsset:
    """One white paper of the registry and the LinkedIn identifiers that resolve to it.

    Asset resolution has one precedence for spend and for leads (AAP 0.5.4.7): the creative's
    override first, otherwise the asset whose ``lead_form_urns`` holds the canonical form URN
    (the creative's call-to-action form for spend, the lead's own form for leads).

    Attributes:
        slug: Unique lowercase kebab-case identifier.
        title: Display title.
        lead_form_urns: Canonical ``urn:li:leadGenForm:{id}`` URNs, so every version of a form
            maps to this asset.
        creative_urns: Creative override URNs, ``urn:li:sponsoredCreative:{id}``, for
            creatives without a lead-gen form such as landing-page creatives.

    Raises:
        ConfigError: On construction, with :data:`CONTENT_ASSETS_FILE` as the file and a key
            relative to the asset, when the slug, title or a URN is malformed or a URN repeats.
    """

    slug: str
    title: str
    lead_form_urns: tuple[str, ...]
    creative_urns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the slug, title and URN formats."""
        file = CONTENT_ASSETS_FILE
        slug: object = self.slug
        problem = _text_problem(slug)
        if problem is None and isinstance(slug, str) and not _SLUG_RE.fullmatch(slug):
            problem = f"must be lowercase kebab-case such as 'my-white-paper', found {slug!r}"
        if problem is not None:
            raise ConfigError(file, "slug", problem)
        problem = _text_problem(self.title)
        if problem is not None:
            raise ConfigError(file, "title", problem)
        lists: tuple[tuple[str, object, Callable[[object], str | None]], ...] = (
            ("lead_forms", self.lead_form_urns, _form_urn_problem),
            ("creative_overrides", self.creative_urns, _creative_urn_problem),
        )
        for key, urns, check in lists:
            if not isinstance(urns, tuple):
                raise ConfigError(file, key, "must be a tuple of URNs")
            seen: set[object] = set()
            for index, urn in enumerate(urns):
                problem = check(urn)
                if problem is None and urn in seen:
                    problem = f"{urn!r} is listed twice"
                if problem is not None:
                    raise ConfigError(file, f"{key}[{index}]", problem)
                seen.add(urn)

    def to_json(self) -> dict[str, Any]:
        """Return the asset in the file's own key names, as ``GET /config`` lists it."""
        return {
            "slug": self.slug,
            "title": self.title,
            "lead_forms": list(self.lead_form_urns),
            "creative_overrides": list(self.creative_urns),
        }


def _urn_list(entry: Mapping[str, Any], name: str, key: str, *, required: bool) -> tuple[str, ...]:
    """Read ``lead_forms`` or ``creative_overrides`` of one registry entry as a tuple.

    Raises:
        ConfigError: When a required list is absent, or the value is not a list.
    """
    if name not in entry:
        if required:
            raise ConfigError(CONTENT_ASSETS_FILE, f"{key}.{name}", "required key is missing")
        return ()
    value: object = entry[name]
    if value is None and not required:
        return ()
    if isinstance(value, str | Mapping) or not isinstance(value, Sequence):
        raise ConfigError(
            CONTENT_ASSETS_FILE,
            f"{key}.{name}",
            f"must be a list of URNs, found {_describe(value)}",
        )
    return tuple(value)


def _parse_assets(document: Mapping[str, Any]) -> tuple[ContentAsset, ...]:
    """Parse content_assets.yaml: an ``assets`` list of registry entries, in file order.

    Cross-asset rules (a slug, form or creative listed under two assets) are enforced by
    :func:`_index_assets` when the :class:`Thresholds` is built.

    Raises:
        ConfigError: Naming the failing entry key, for example ``assets[1].lead_forms[0]``.
    """
    file = CONTENT_ASSETS_FILE
    _exact_keys(document, ("assets",), file, None)
    entries: object = document["assets"]
    if isinstance(entries, str | Mapping) or not isinstance(entries, Sequence):
        raise ConfigError(
            file,
            "assets",
            f"must be a list of assets (use [] for none), found {_describe(entries)}",
        )
    assets: list[ContentAsset] = []
    for index, entry in enumerate(entries):
        key = f"assets[{index}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(file, key, f"must be a mapping, found {_describe(entry)}")
        _reject_unknown(entry, ("slug", "title", "lead_forms", "creative_overrides"), file, key)
        for name in ("slug", "title"):
            if name not in entry:
                raise ConfigError(file, f"{key}.{name}", "required key is missing")
        forms = _urn_list(entry, "lead_forms", key, required=True)
        overrides = _urn_list(entry, "creative_overrides", key, required=False)
        try:
            asset = ContentAsset(
                slug=entry["slug"],
                title=entry["title"],
                lead_form_urns=forms,
                creative_urns=overrides,
            )
        except ConfigError as exc:
            raise _prefixed(exc, key) from None
        assets.append(asset)
    return tuple(assets)


def _index_assets(
    assets: Sequence[ContentAsset],
) -> tuple[dict[str, ContentAsset], dict[str, ContentAsset]]:
    """Index the registry by form URN and by creative override URN.

    AAP 0.5.2: a form or creative listed under two assets fails config load.

    Returns:
        ``(form URN -> asset, creative URN -> asset)``.

    Raises:
        ConfigError: On a repeated slug, or a form or creative listed under two assets.
    """
    file = CONTENT_ASSETS_FILE
    slugs: dict[str, int] = {}
    by_form: dict[str, ContentAsset] = {}
    by_creative: dict[str, ContentAsset] = {}
    for index, asset in enumerate(assets):
        item: object = asset
        if not isinstance(item, ContentAsset):
            raise ConfigError(file, f"assets[{index}]", "must be a ContentAsset")
        if asset.slug in slugs:
            raise ConfigError(
                file,
                f"assets[{index}].slug",
                f"slug {asset.slug!r} is already used by assets[{slugs[asset.slug]}]",
            )
        slugs[asset.slug] = index
        for name, urns, owners in (
            ("lead_forms", asset.lead_form_urns, by_form),
            ("creative_overrides", asset.creative_urns, by_creative),
        ):
            for position, urn in enumerate(urns):
                owner = owners.get(urn)
                if owner is not None:
                    raise ConfigError(
                        file,
                        f"assets[{index}].{name}[{position}]",
                        f"{urn!r} is listed under two assets, {owner.slug!r} and {asset.slug!r}",
                    )
                owners[urn] = asset
    return by_form, by_creative


# ---------------------------------------------------------------------------
# Thresholds: the loaded configuration
# ---------------------------------------------------------------------------


def _merge_crm(thresholds: Mapping[str, object], mapping: HubSpotMapping) -> dict[str, object]:
    """Merge the mapping's meeting lists into the thresholds ``crm`` namespace.

    Raises:
        ConfigError: When thresholds.yaml's ``crm`` is not a mapping, or defines a key that
            hubspot_mapping.yaml also defines.
    """
    merged = dict(thresholds)
    crm: object = thresholds.get("crm")
    if crm is None:
        crm_items: dict[str, object] = {}
    elif isinstance(crm, Mapping):
        crm_items = dict(crm)
    else:
        raise ConfigError(THRESHOLDS_FILE, "crm", f"must be a mapping, found {_describe(crm)}")
    for name in _MERGED_CRM_KEYS:
        if name in crm_items:
            raise ConfigError(
                THRESHOLDS_FILE,
                f"crm.{name}",
                f"is defined in both {THRESHOLDS_FILE} and {HUBSPOT_MAPPING_FILE}; "
                f"keep it in {HUBSPOT_MAPPING_FILE} only",
            )
        crm_items[name] = getattr(mapping, name)
    merged["crm"] = MappingProxyType(crm_items)
    return merged


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The loaded configuration: thresholds, HubSpot mapping, white paper registry, hash.

    Build it with :meth:`load` (or :meth:`from_trees` for already-parsed documents). Every
    instance is validated and immutable: ``raw`` is a read-only tree whose mappings are
    ``MappingProxyType`` and whose lists are tuples. Two instances compare equal, and hash
    equally, exactly when their ``config_hash`` is equal, since the hash covers all three
    parsed files.

    Dotted keys are exactly the names in ``config/thresholds.yaml`` (for example
    ``budget.capped_lost_is_share``, ``k2.adjacent_max_terms``, ``feedback.snooze_days``),
    plus ``crm.booking_sources`` and ``crm.meeting_outcome_exclusions`` merged from
    ``config/hubspot_mapping.yaml``.

    Attributes:
        raw: The merged thresholds tree, read-only.
        mapping: The HubSpot MQL/QL bindings and meeting rules.
        content_assets: The white paper registry, in file order.
        config_hash: SHA-256 hex digest of the three parsed files, stored on every run.
        source_dir: The config directory the files were read from.

    Raises:
        ConfigError: On construction, when ``raw`` fails validation, the merged ``crm`` lists
            differ from ``mapping``, the registry repeats a slug, form or creative, or
            ``config_hash`` is not a SHA-256 hex digest.
    """

    raw: Mapping[str, Any] = field(compare=False, repr=False)
    mapping: HubSpotMapping = field(compare=False)
    content_assets: tuple[ContentAsset, ...] = field(compare=False)
    config_hash: str
    source_dir: Path = field(compare=False)
    _assets_by_form: Mapping[str, ContentAsset] = field(init=False, repr=False, compare=False)
    _assets_by_creative: Mapping[str, ContentAsset] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Freeze ``raw``, validate every part and build the registry indexes."""
        raw: object = self.raw
        if not isinstance(raw, Mapping):
            raise ConfigError(THRESHOLDS_FILE, None, f"must be a mapping, found {_describe(raw)}")
        object.__setattr__(self, "raw", _freeze(raw, THRESHOLDS_FILE, ""))
        _validate_thresholds(self.raw)

        mapping: object = self.mapping
        if not isinstance(mapping, HubSpotMapping):
            raise ConfigError(HUBSPOT_MAPPING_FILE, None, "must be a HubSpotMapping")
        for name in _MERGED_CRM_KEYS:
            if _lookup(self.raw, f"crm.{name}", HUBSPOT_MAPPING_FILE) != getattr(mapping, name):
                raise ConfigError(
                    HUBSPOT_MAPPING_FILE,
                    f"crm.{name}",
                    "the thresholds crm namespace must carry the mapping's value",
                )

        assets: object = self.content_assets
        if not isinstance(assets, tuple):
            raise ConfigError(CONTENT_ASSETS_FILE, "assets", "must be a tuple of ContentAsset")
        by_form, by_creative = _index_assets(self.content_assets)
        object.__setattr__(self, "_assets_by_form", MappingProxyType(by_form))
        object.__setattr__(self, "_assets_by_creative", MappingProxyType(by_creative))

        digest: object = self.config_hash
        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
            raise ConfigError("config", None, "config_hash must be a SHA-256 hex digest")
        object.__setattr__(self, "source_dir", Path(self.source_dir))

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle the configuration as its constructor arguments, ``raw`` thawed.

        ``raw`` and the registry indexes are read-only ``MappingProxyType`` trees, which
        cannot be pickled, so a copy is rebuilt from a plain-``dict`` and ``list`` form of
        ``raw`` and the other fields: :meth:`__post_init__` freezes, validates and indexes it
        again. An overridden configuration (:meth:`load` with ``overrides``) round-trips with
        its values and its ``config_hash`` unchanged. The analysis runner hands a custom run's
        configuration to its analysis process this way.

        Returns:
            ``(Thresholds, (raw, mapping, content_assets, config_hash, source_dir))``.
        """
        return (
            type(self),
            (
                _canonical(self.raw),
                self.mapping,
                self.content_assets,
                self.config_hash,
                self.source_dir,
            ),
        )

    # -- construction ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        path: Path | str | None = None,
        *,
        overrides: Mapping[str, object] | None = None,
        content_assets: Sequence[ContentAsset] | None = None,
    ) -> Thresholds:
        """Read and validate ``thresholds.yaml``, ``content_assets.yaml``, ``hubspot_mapping.yaml``.

        AAP 0.5.1 ("Window and thresholds"), 0.6.2: the config files are loaded here and
        nowhere else. They are parsed with PyYAML's safe loader, extended only to reject a key
        repeated within one mapping.

        ``overrides`` and ``content_assets`` load the configuration with some values changed
        and no file written, as the scenario catalogue's fixtures need (AAP 0.11.2). Each
        override replaces the value of a dotted key the file already defines:
        ``crm.booking_sources`` and ``crm.meeting_outcome_exclusions`` in
        ``hubspot_mapping.yaml``, every other key in ``thresholds.yaml``. Values pass through
        :func:`override_value`, so ``Decimal("0.90")`` overrides exactly as ``0.9`` does. A
        registry replaces the parsed ``content_assets.yaml``. The edited documents then go
        through :meth:`from_trees`, so validation and ``config_hash`` are exactly those of
        files holding the same values (``window.presets`` must still contain
        ``window.default_days``).

        Args:
            path: The config directory; :func:`resolve_config_dir` when ``None``.
            overrides: Dotted key to replacement value; ``None`` or empty changes nothing.
            content_assets: A registry replacing ``content_assets.yaml``, in order; ``None``
                keeps the file's.

        Returns:
            The validated configuration.

        Raises:
            ConfigError: Naming the file and key, when the directory or a file is missing, a
                file is not valid YAML or not a mapping, an override names a key its file
                does not define, a registry entry is not a :class:`ContentAsset`, or any
                validation fails.
        """
        directory = _config_directory(path)
        trees: dict[str, Mapping[str, Any]] = {
            name: _read_yaml(directory, name) for name in CONFIG_FILES
        }
        for key, value in (overrides or {}).items():
            dotted: object = key
            if not isinstance(dotted, str):
                raise ConfigError(
                    THRESHOLDS_FILE,
                    None,
                    f"threshold override key must be a dotted string, found {_describe(dotted)}",
                )
            file = HUBSPOT_MAPPING_FILE if key in _MERGED_CRM_DOTTED_KEYS else THRESHOLDS_FILE
            trees[file] = _with_override(
                trees[file], key.split("."), override_value(value), file, key
            )
        if content_assets is not None:
            entries: list[dict[str, Any]] = []
            for index, asset in enumerate(content_assets):
                item: object = asset
                if not isinstance(item, ContentAsset):
                    raise ConfigError(
                        CONTENT_ASSETS_FILE, f"assets[{index}]", "must be a ContentAsset"
                    )
                entries.append(item.to_json())
            trees[CONTENT_ASSETS_FILE] = {"assets": entries}
        return cls.from_trees(
            thresholds=trees[THRESHOLDS_FILE],
            content_assets=trees[CONTENT_ASSETS_FILE],
            hubspot_mapping=trees[HUBSPOT_MAPPING_FILE],
            source_dir=directory.absolute(),
        )

    @classmethod
    def from_trees(
        cls,
        *,
        thresholds: Mapping[str, Any],
        content_assets: Mapping[str, Any],
        hubspot_mapping: Mapping[str, Any],
        source_dir: Path | str,
    ) -> Thresholds:
        r"""Build the configuration from the three already-parsed documents.

        :meth:`load` reads the files and calls this; tests call it with edited copies of the
        shipped trees. ``config_hash`` is computed from these trees exactly as :meth:`load`
        computes it from the files. Every string key and value of all three trees is checked
        to be UTF-8 encodable (a ``"\uD800"`` escape is not) before any other validation.

        Args:
            thresholds: The parsed ``thresholds.yaml``.
            content_assets: The parsed ``content_assets.yaml``.
            hubspot_mapping: The parsed ``hubspot_mapping.yaml``.
            source_dir: The directory the documents came from, for display.

        Returns:
            The validated configuration.

        Raises:
            ConfigError: Naming the file and key of the first validation failure.
        """
        documents: tuple[tuple[str, object], ...] = (
            (THRESHOLDS_FILE, thresholds),
            (CONTENT_ASSETS_FILE, content_assets),
            (HUBSPOT_MAPPING_FILE, hubspot_mapping),
        )
        for name, document in documents:
            if not isinstance(document, Mapping):
                raise ConfigError(
                    name, None, f"top level must be a mapping, found {_describe(document)}"
                )
        # Before any other check, so no later message or the hash meets unencodable text.
        for name, document in documents:
            _check_encodable(document, name, "")
        frozen = _freeze(thresholds, THRESHOLDS_FILE, "")
        tree: Mapping[str, object] = frozen if isinstance(frozen, Mapping) else {}
        mapping = _parse_mapping(hubspot_mapping)
        assets = _parse_assets(content_assets)
        return cls(
            raw=_merge_crm(tree, mapping),
            mapping=mapping,
            content_assets=assets,
            config_hash=_config_hash(thresholds, content_assets, hubspot_mapping),
            source_dir=Path(source_dir),
        )

    # -- dotted-key accessors ------------------------------------------------------------

    def _file_for(self, key: str) -> str:
        """Name the file a dotted key comes from, for error messages."""
        if key in _MERGED_CRM_DOTTED_KEYS:
            return HUBSPOT_MAPPING_FILE
        return THRESHOLDS_FILE

    def value(self, key: str) -> Any:
        """Return the value at a dotted key, as frozen in ``raw``.

        Mappings come back read-only and lists as tuples. There is no default: a missing key
        is a configuration error.

        Raises:
            ConfigError: When the key is missing.
        """
        return _lookup(self.raw, key, self._file_for(key))

    def int(self, key: str) -> builtins.int:
        """Return an integer threshold, such as ``window.default_days``.

        Raises:
            ConfigError: When the key is missing or its value is not a YAML integer
                (``bool`` and floats such as ``7.0`` are rejected).
        """
        value = self.value(key)
        if isinstance(value, bool) or not isinstance(value, builtins.int):
            raise ConfigError(
                self._file_for(key), key, f"must be an integer, found {_describe(value)}"
            )
        return value

    def decimal(self, key: str) -> Decimal:
        """Return a numeric threshold as an exact ``Decimal`` (``0.95`` is ``Decimal("0.95")``).

        Raises:
            ConfigError: When the key is missing or its value is not a number.
        """
        value = self.value(key)
        if isinstance(value, bool) or not isinstance(value, builtins.int | float):
            raise ConfigError(
                self._file_for(key), key, f"must be a number, found {_describe(value)}"
            )
        return Decimal(str(value))

    def ratio(self, key: str) -> Decimal:
        """Return a ratio, share or multiple as a ``Decimal`` in ``[0, inf)``.

        Raises:
            ConfigError: When the key is missing, not a number, or negative.
        """
        value = self.decimal(key)
        if value < 0:
            raise ConfigError(self._file_for(key), key, f"must be >= 0, found {value}")
        return value

    def strings(self, key: str) -> tuple[str, ...]:
        """Return a list of strings, such as ``crm.booking_sources``.

        Raises:
            ConfigError: When the key is missing or its value is not a list of strings.
        """
        value = self.value(key)
        if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
            raise ConfigError(
                self._file_for(key), key, f"must be a list of strings, found {_describe(value)}"
            )
        return tuple(str(item) for item in value)

    def subset(self, *keys: str) -> dict[str, Any]:
        """Return ``{dotted key: value}`` for exactly ``keys``, as plain JSON values.

        Values keep their YAML scalars (``0.95``, ``7``, ``[14, 28, 56, 90]``), so a rule's
        ``evidence.thresholds`` shows the configured values it used (AAP 0.5.4, 0.5.4.8).

        Raises:
            ConfigError: When any key is missing.
        """
        return {key: _json_safe(self.value(key)) for key in keys}

    def as_view(self) -> dict[str, Any]:
        """Return the full merged thresholds tree as plain JSON, for ``GET /config``.

        Every namespace of ``config/thresholds.yaml`` is present, with ``crm`` also carrying
        the mapping's ``booking_sources`` and ``meeting_outcome_exclusions``. ``GET /config``
        returns this as its ``thresholds`` field; the HubSpot mapping and the registry travel
        in their own fields, from :meth:`HubSpotMapping.to_json` and
        :meth:`ContentAsset.to_json`.
        """
        view: dict[str, Any] = _json_safe(self.raw)
        return view

    # -- window settings (AAP 0.5.4.1) ---------------------------------------------------

    @property
    def default_days(self) -> builtins.int:
        """``window.default_days``: the default analysis window, in complete native days."""
        return self.int("window.default_days")

    @property
    def trusted_min_days(self) -> builtins.int:
        """``window.trusted_min_days``: shorter windows are flagged ``short_window``."""
        return self.int("window.trusted_min_days")

    @property
    def presets(self) -> tuple[builtins.int, ...]:
        """``window.presets``, ascending and distinct: the picker and segment windows."""
        values = self.value("window.presets")
        return tuple(sorted({_integer(item, "window.presets", 1) for item in values}))

    @property
    def sustained_min_days(self) -> builtins.int:
        """``window.sustained_min_days``: sustained-pattern rules are withheld below it."""
        return self.int("window.sustained_min_days")

    @property
    def max_days(self) -> builtins.int:
        """``window.max_days``: the longest custom window (LinkedIn daily retention)."""
        return self.int("window.max_days")

    # -- white paper registry (AAP 0.5.2, 0.5.4.7) ----------------------------------------

    def asset_for_form(self, form_urn: str) -> ContentAsset | None:
        """Return the asset whose ``lead_forms`` lists this canonical form URN, if any."""
        return self._assets_by_form.get(form_urn)

    def asset_for_creative(self, creative_urn: str) -> ContentAsset | None:
        """Return the asset that lists this creative as an override, if any."""
        return self._assets_by_creative.get(creative_urn)

    def resolve_asset(
        self, *, creative_urn: str | None, form_urn: str | None
    ) -> ContentAsset | None:
        """Resolve an asset with the one precedence used for spend and for leads.

        AAP 0.5.4.7: the creative's override first; otherwise the asset whose form list holds
        the canonical form URN (the creative's call-to-action form for spend, the lead's own
        form for a lead).

        Args:
            creative_urn: The creative, when known.
            form_urn: The canonical form URN, when known.

        Returns:
            The asset, or ``None`` when neither identifier resolves.
        """
        if creative_urn is not None:
            override = self.asset_for_creative(creative_urn)
            if override is not None:
                return override
        return None if form_urn is None else self.asset_for_form(form_urn)

    def check_overrides(self, creative_forms: Mapping[str, str | None]) -> None:
        """Fail when an override contradicts its creative's own form mapping.

        AAP 0.5.4.7: an override whose creative's call-to-action form maps to a different
        asset fails config load. The stored creatives are not known when the files load, so
        ``runner.py`` calls this before each run against the active LinkedIn dataset. An
        override whose creative has no form, is not stored, or whose form maps to no asset or
        to the same asset is valid.

        Args:
            creative_forms: Stored creative URN to its canonical lead-form URN (``None`` when
                the creative has no lead-gen form).

        Raises:
            ConfigError: Listing every contradicting override, keyed at the first one.
        """
        conflicts: list[str] = []
        first_key: str | None = None
        for index, asset in enumerate(self.content_assets):
            for position, creative in enumerate(asset.creative_urns):
                form = creative_forms.get(creative)
                owner = None if form is None else self.asset_for_form(form)
                if owner is None or owner.slug == asset.slug:
                    continue
                conflicts.append(
                    f"{creative} overrides to {asset.slug!r} but its lead form {form} "
                    f"belongs to {owner.slug!r}"
                )
                if first_key is None:
                    first_key = f"assets[{index}].creative_overrides[{position}]"
        if conflicts:
            raise ConfigError(
                CONTENT_ASSETS_FILE,
                first_key,
                "an override contradicts its creative's own form mapping: " + "; ".join(conflicts),
            )

    def content_asset_rows(self) -> list[ContentAssetRow]:
        """Return the registry as ``db.repositories.sync_content_assets`` takes it, in order."""
        rows: list[ContentAssetRow] = []
        for asset in self.content_assets:
            row: ContentAssetRow = {
                "slug": asset.slug,
                "title": asset.title,
                "lead_form_urns": list(asset.lead_form_urns),
                "creative_urns": list(asset.creative_urns),
            }
            rows.append(row)
        return rows
