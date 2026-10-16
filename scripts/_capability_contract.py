"""Strict, stdlib-only contract for local capability observations.

The validator is deliberately offline. It validates a project-owned outbox but
does not contact or mutate the central registry, and therefore cannot become a
deployment availability dependency.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional


CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_CONTRACT_VERSION = "1.0.0"
REGISTRY_REPOSITORY = (
    "nwcfollowthewhiterabbit/rabbit-erp-customization-registry"
)
OUTBOX_PATH = "capabilities/outbox"
PUBLICATION_MODE = "central-reviewed-import"

CONFIG_KEYS = {
    "schema_version",
    "contract_version",
    "project_key",
    "project_aliases",
    "registry_repository",
    "observation_schema_sha256",
    "outbox_path",
    "publication_mode",
    "deployment_gate",
}
OBSERVATION_KEYS = {
    "schema_version",
    "contract_version",
    "observation_id",
    "event",
    "observed_at",
    "project_key",
    "source_revision",
    "feature",
    "implementation",
    "dependencies",
    "relationships",
    "differences",
    "evidence",
    "privacy_review",
}
FEATURE_KEYS = {
    "local_id",
    "name",
    "description",
    "business_problem",
    "category",
}
IMPLEMENTATION_KEYS = {
    "kind",
    "apps",
    "modules",
    "repository_paths",
    "doctypes",
}
DEPENDENCY_KEYS = {
    "frappe_versions",
    "erpnext_versions",
    "apps",
    "doctypes",
    "external_services",
}
RELATIONSHIP_KEYS = {"capability_ids", "observation_ids"}
EVIDENCE_KEYS = {"repository_paths", "test_paths", "adr_paths"}
PRIVACY_KEYS = {
    "reviewed",
    "contains_raw_feedback",
    "contains_personal_data",
    "contains_secrets",
    "contains_runtime_urls",
}

EVENTS = {"discovered", "implemented", "reused", "varied", "retired"}
CATEGORIES = {
    "ui-ux-improvement",
    "generic-erp-improvement",
    "integration",
    "workflow",
    "warehouse",
    "crm",
    "logistics",
    "3pl",
    "rental",
    "localization",
    "reporting",
    "permissions",
    "infrastructure",
    "other",
}
IMPLEMENTATION_KINDS = {
    "original",
    "copied",
    "adapted",
    "shared-component",
    "configuration-only",
    "retired",
}

REPOSITORY_COORDINATE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)
LOCAL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,95}$")
OBSERVATION_ID = re.compile(r"^OBS-[a-z0-9][a-z0-9-]{2,95}$")
CAPABILITY_ID = re.compile(r"^CAP-[A-Z0-9]+-[0-9]{4}$")
SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
OBSERVED_AT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
SEMVER = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")

URL_REFERENCE = re.compile(
    r"(?i)(?:\b(?!github:)[a-z][a-z0-9+.-]{0,31}:(?://|[^\s])|"
    r"(?<![:/])//(?:[a-z0-9-]+\.)+[a-z]{2,63}(?:[/:]|$)|"
    r"\bmailto:|\bwww\.)"
)
PRIVATE_LOCATION = re.compile(
    r"(?i)(?:\blocalhost(?::[0-9]+)?\b|"
    r"\b127\.0\.0\.1(?::[0-9]+)?\b|"
    r"\b10(?:\.[0-9]{1,3}){3}(?::[0-9]+)?\b|"
    r"\b192\.168(?:\.[0-9]{1,3}){2}(?::[0-9]+)?\b|"
    r"\b172\.(?:1[6-9]|2[0-9]|3[01])"
    r"(?:\.[0-9]{1,3}){2}(?::[0-9]+)?\b)"
)
PLAIN_HOSTNAME = re.compile(
    r"(?i)(?<![A-Z0-9_.-])"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}(?::[0-9]{1,5})?(?![A-Z0-9_.-])"
)
IPV4_CANDIDATE = re.compile(
    r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])"
)
IPV6_CANDIDATE = re.compile(
    r"(?<![0-9A-Fa-f:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]+"
    r"(?![0-9A-Fa-f:])"
)
EMAIL_ADDRESS = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
PHONE_NUMBER = re.compile(r"(?<!\w)\+?[0-9](?:[ ()-]*[0-9]){8,}(?!\w)")
SENSITIVE_MATERIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+)?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]{10,}\b"),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|token|credential|api[_-]?key)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
)
FORBIDDEN_PATH_PARTS = {".git", "credentials", "secrets"}
FORBIDDEN_PATH_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}


def strict_json_value(text: str) -> Any:
    """Load JSON while rejecting duplicate keys."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicates)


def _object(
    value: Any,
    expected_keys: set[str],
    label: str,
    errors: list[str],
) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return None
    actual = set(value)
    missing = sorted(expected_keys - actual)
    unknown = sorted(actual - expected_keys)
    if missing:
        errors.append(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        errors.append(f"{label} has unknown keys: {', '.join(unknown)}")
    return value


def _text(
    value: Any,
    label: str,
    errors: list[str],
    *,
    maximum: int = 500,
    pattern: Optional[re.Pattern[str]] = None,
) -> Optional[str]:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
        or len(value) > maximum
    ):
        errors.append(
            f"{label} must be a trimmed single-line string of 1..{maximum} characters"
        )
        return None
    if pattern is not None and pattern.fullmatch(value) is None:
        errors.append(f"{label} has invalid format")
    return value


def _text_list(
    value: Any,
    label: str,
    errors: list[str],
    *,
    pattern: Optional[re.Pattern[str]] = None,
    paths: bool = False,
    minimum: int = 0,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return []
    if len(value) < minimum:
        errors.append(f"{label} must contain at least {minimum} item(s)")
    checked: list[str] = []
    for index, item in enumerate(value):
        item_label = f"{label}[{index}]"
        text = _text(
            item,
            item_label,
            errors,
            maximum=300 if paths else 200,
            pattern=pattern,
        )
        if text is None:
            continue
        if paths and not safe_repository_path(text):
            errors.append(f"{item_label} must be a safe repository-relative path")
        checked.append(text)
    if len(checked) != len(set(checked)):
        errors.append(f"{label} contains duplicate items")
    return checked


def safe_repository_path(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.casefold() in FORBIDDEN_PATH_PARTS for part in path.parts)
        or path.suffix.casefold() in FORBIDDEN_PATH_SUFFIXES
    ):
        return False
    name = path.name.casefold()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return False
    return True


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)
    elif isinstance(value, str):
        yield value


def _human_and_external_service_strings(value: dict[str, Any]) -> list[str]:
    selected: list[str] = []
    feature = value.get("feature")
    if isinstance(feature, dict):
        selected.extend(
            feature[key]
            for key in ("name", "description", "business_problem")
            if isinstance(feature.get(key), str)
        )
    differences = value.get("differences")
    if isinstance(differences, list):
        selected.extend(child for child in differences if isinstance(child, str))
    dependencies = value.get("dependencies")
    external_services = (
        dependencies.get("external_services")
        if isinstance(dependencies, dict)
        else None
    )
    if isinstance(external_services, list):
        selected.extend(
            child for child in external_services if isinstance(child, str)
        )
    return selected


def _contains_ip_address(text: str) -> bool:
    for match in IPV4_CANDIDATE.finditer(text):
        try:
            ipaddress.IPv4Address(match.group(0))
        except ipaddress.AddressValueError:
            continue
        return True
    for match in IPV6_CANDIDATE.finditer(text):
        candidate = match.group(0)
        if candidate.count(":") < 2:
            continue
        try:
            ipaddress.IPv6Address(candidate)
        except ipaddress.AddressValueError:
            continue
        return True
    return False


def _content_boundary_errors(value: dict[str, Any]) -> list[str]:
    errors = []
    for text in _walk_strings(value):
        if URL_REFERENCE.search(text):
            errors.append("observation contains a URL")
        if PRIVATE_LOCATION.search(text):
            errors.append("observation contains a private runtime location")
        if EMAIL_ADDRESS.search(text):
            errors.append("observation contains probable personal data")
        if any(pattern.search(text) for pattern in SENSITIVE_MATERIAL_PATTERNS):
            errors.append("observation contains probable secret material")
        if _contains_ip_address(text):
            errors.append("observation contains an IP address")
    human_and_external = _human_and_external_service_strings(value)
    if any(PHONE_NUMBER.search(text) for text in human_and_external):
        errors.append("observation contains probable personal data")
    if any(PLAIN_HOSTNAME.search(text) for text in human_and_external):
        errors.append("observation contains a hostname")
    return sorted(set(errors))


def capability_config_errors(
    config: Any,
    project_config: Optional[dict[str, Any]] = None,
) -> list[str]:
    errors: list[str] = []
    config_object = _object(config, CONFIG_KEYS, "capability config", errors)
    if config_object is None:
        return errors

    if (
        type(config_object.get("schema_version")) is not int
        or config_object.get("schema_version") != CAPABILITY_SCHEMA_VERSION
    ):
        errors.append(
            f"capability config schema_version must be integer {CAPABILITY_SCHEMA_VERSION}"
        )
    contract_version = config_object.get("contract_version")
    if (
        not isinstance(contract_version, str)
        or SEMVER.fullmatch(contract_version) is None
        or contract_version != CAPABILITY_CONTRACT_VERSION
    ):
        errors.append(
            f"capability config contract_version must be {CAPABILITY_CONTRACT_VERSION}"
        )
    project_key = _text(
        config_object.get("project_key"),
        "capability config project_key",
        errors,
        maximum=200,
        pattern=REPOSITORY_COORDINATE,
    )
    project_aliases = _text_list(
        config_object.get("project_aliases"),
        "capability config project_aliases",
        errors,
        pattern=REPOSITORY_COORDINATE,
    )
    if len({alias.casefold() for alias in project_aliases}) != len(project_aliases):
        errors.append("capability config project_aliases contains duplicate aliases")
    if (
        project_key is not None
        and project_key.casefold() in {alias.casefold() for alias in project_aliases}
    ):
        errors.append("capability config project_aliases must not contain project_key")
    _text(
        config_object.get("observation_schema_sha256"),
        "capability config observation_schema_sha256",
        errors,
        maximum=64,
        pattern=SHA256,
    )
    if config_object.get("registry_repository") != REGISTRY_REPOSITORY:
        errors.append(
            f"capability config registry_repository must be {REGISTRY_REPOSITORY}"
        )
    if config_object.get("outbox_path") != OUTBOX_PATH:
        errors.append(f"capability config outbox_path must be {OUTBOX_PATH}")
    if config_object.get("publication_mode") != PUBLICATION_MODE:
        errors.append(
            f"capability config publication_mode must be {PUBLICATION_MODE}"
        )
    if config_object.get("deployment_gate") is not False:
        errors.append("capability config deployment_gate must be false")

    source_repository = None
    if isinstance(project_config, dict):
        source = project_config.get("source")
        if isinstance(source, dict):
            source_repository = source.get("repository")
    if (
        isinstance(source_repository, str)
        and project_key is not None
        and project_key != source_repository
    ):
        errors.append(
            "capability config project_key must equal project.toml source.repository"
        )
    return errors


def capability_schema_errors(schema: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return ["capability observation schema must be an object"]
    expected_keys = {
        "$id",
        "$defs",
        "type",
        "additionalProperties",
        "properties",
        "required",
    }
    if set(schema) != expected_keys:
        errors.append(
            "capability observation schema keys must be exactly "
            + ", ".join(sorted(expected_keys))
        )
    if schema.get("$id") != "rabbit-capability-observation/1.0.0":
        errors.append("capability observation schema $id is invalid")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        errors.append("capability observation schema must be a closed object")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != OBSERVATION_KEYS:
        errors.append("capability observation schema properties do not match contract")
    required = schema.get("required")
    if not isinstance(required, list) or set(required) != OBSERVATION_KEYS:
        errors.append("capability observation schema required fields do not match contract")
    if isinstance(properties, dict):
        schema_version = properties.get("schema_version")
        contract_version = properties.get("contract_version")
        if (
            not isinstance(schema_version, dict)
            or type(schema_version.get("const")) is not int
            or schema_version.get("const") != 1
        ):
            errors.append("capability observation schema_version const is invalid")
        if (
            not isinstance(contract_version, dict)
            or contract_version.get("const") != CAPABILITY_CONTRACT_VERSION
        ):
            errors.append("capability observation contract_version const is invalid")
        forbidden = {"architecture_status", "reuse_count"} & set(properties)
        if forbidden:
            errors.append(
                "capability observation schema contains central-only fields: "
                + ", ".join(sorted(forbidden))
            )
        relationships = properties.get("relationships")
        relationship_properties = (
            relationships.get("properties")
            if isinstance(relationships, dict)
            else None
        )
        capability_ids = (
            relationship_properties.get("capability_ids")
            if isinstance(relationship_properties, dict)
            else None
        )
        capability_id_items = (
            capability_ids.get("items")
            if isinstance(capability_ids, dict)
            else None
        )
        if (
            not isinstance(capability_id_items, dict)
            or capability_id_items.get("pattern") != CAPABILITY_ID.pattern
        ):
            errors.append("capability observation CAP-ID pattern is invalid")
        privacy = properties.get("privacy_review")
        privacy_properties = (
            privacy.get("properties") if isinstance(privacy, dict) else None
        )
        if not isinstance(privacy_properties, dict):
            errors.append("capability observation privacy consts are invalid")
        else:
            reviewed = privacy_properties.get("reviewed")
            if not isinstance(reviewed, dict) or reviewed.get("const") is not True:
                errors.append("capability observation privacy reviewed const is invalid")
            for key in sorted(PRIVACY_KEYS - {"reviewed"}):
                field = privacy_properties.get(key)
                if not isinstance(field, dict) or field.get("const") is not False:
                    errors.append(
                        f"capability observation privacy {key} const is invalid"
                    )
    return errors


def capability_observation_errors(
    observation: Any,
    *,
    expected_project_key: str,
    project_aliases: Iterable[str] = (),
    filename: Optional[str] = None,
) -> list[str]:
    errors: list[str] = []
    root = _object(observation, OBSERVATION_KEYS, "observation", errors)
    if root is None:
        return errors

    if (
        type(root.get("schema_version")) is not int
        or root.get("schema_version") != CAPABILITY_SCHEMA_VERSION
    ):
        errors.append(
            f"observation schema_version must be integer {CAPABILITY_SCHEMA_VERSION}"
        )
    if root.get("contract_version") != CAPABILITY_CONTRACT_VERSION:
        errors.append(
            f"observation contract_version must be {CAPABILITY_CONTRACT_VERSION}"
        )
    observation_id = _text(
        root.get("observation_id"),
        "observation_id",
        errors,
        maximum=100,
        pattern=OBSERVATION_ID,
    )
    if filename is not None and observation_id is not None:
        expected_filename = f"{observation_id}.json"
        if filename != expected_filename:
            errors.append(f"filename must be {expected_filename}")
    event = root.get("event")
    if event not in EVENTS:
        errors.append("event must be one of: " + ", ".join(sorted(EVENTS)))
    observed_at = _text(
        root.get("observed_at"),
        "observed_at",
        errors,
        maximum=20,
        pattern=OBSERVED_AT,
    )
    if observed_at is not None and OBSERVED_AT.fullmatch(observed_at):
        try:
            datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            errors.append("observed_at must be a valid UTC timestamp")
    project_key = _text(
        root.get("project_key"),
        "project_key",
        errors,
        maximum=200,
        pattern=REPOSITORY_COORDINATE,
    )
    allowed_project_keys = {
        candidate.casefold() for candidate in (expected_project_key, *project_aliases)
    }
    if project_key is not None and project_key.casefold() not in allowed_project_keys:
        errors.append(
            "project_key must match capabilities/config.json project_key or project_aliases"
        )
    source_revision = _text(
        root.get("source_revision"),
        "source_revision",
        errors,
        maximum=40,
        pattern=SOURCE_REVISION,
    )
    if source_revision == "0" * 40:
        errors.append("all-zero source_revision is not valid evidence")

    feature = _object(root.get("feature"), FEATURE_KEYS, "feature", errors)
    if feature is not None:
        _text(
            feature.get("local_id"),
            "feature.local_id",
            errors,
            maximum=96,
            pattern=LOCAL_ID,
        )
        _text(feature.get("name"), "feature.name", errors, maximum=120)
        _text(feature.get("description"), "feature.description", errors)
        _text(feature.get("business_problem"), "feature.business_problem", errors)
        if feature.get("category") not in CATEGORIES:
            errors.append(
                "feature.category must be one of: " + ", ".join(sorted(CATEGORIES))
            )

    implementation = _object(
        root.get("implementation"),
        IMPLEMENTATION_KEYS,
        "implementation",
        errors,
    )
    if implementation is not None:
        if implementation.get("kind") not in IMPLEMENTATION_KINDS:
            errors.append(
                "implementation.kind must be one of: "
                + ", ".join(sorted(IMPLEMENTATION_KINDS))
            )
        _text_list(implementation.get("apps"), "implementation.apps", errors)
        _text_list(implementation.get("modules"), "implementation.modules", errors)
        _text_list(
            implementation.get("repository_paths"),
            "implementation.repository_paths",
            errors,
            paths=True,
            minimum=1,
        )
        _text_list(
            implementation.get("doctypes"), "implementation.doctypes", errors
        )

    dependencies = _object(
        root.get("dependencies"), DEPENDENCY_KEYS, "dependencies", errors
    )
    if dependencies is not None:
        for key in sorted(DEPENDENCY_KEYS):
            _text_list(dependencies.get(key), f"dependencies.{key}", errors)

    relationships = _object(
        root.get("relationships"), RELATIONSHIP_KEYS, "relationships", errors
    )
    capability_ids: list[str] = []
    observation_ids: list[str] = []
    if relationships is not None:
        capability_ids = _text_list(
            relationships.get("capability_ids"),
            "relationships.capability_ids",
            errors,
            pattern=CAPABILITY_ID,
        )
        observation_ids = _text_list(
            relationships.get("observation_ids"),
            "relationships.observation_ids",
            errors,
            pattern=OBSERVATION_ID,
        )
        if observation_id is not None and observation_id in observation_ids:
            errors.append("observation cannot reference itself")

    differences = _text_list(root.get("differences"), "differences", errors)
    if event in {"reused", "varied"} and not (capability_ids or observation_ids):
        errors.append(f"{event} observation must reference a CAP-ID or OBS-ID")
    if event == "varied" and not differences:
        errors.append("varied observation must describe factual differences")
    if event == "retired" and isinstance(implementation, dict):
        if implementation.get("kind") != "retired":
            errors.append("retired observation must use implementation.kind=retired")

    evidence = _object(root.get("evidence"), EVIDENCE_KEYS, "evidence", errors)
    if evidence is not None:
        for key in sorted(EVIDENCE_KEYS):
            _text_list(
                evidence.get(key), f"evidence.{key}", errors, paths=True
            )

    privacy = _object(
        root.get("privacy_review"), PRIVACY_KEYS, "privacy_review", errors
    )
    if privacy is not None:
        if privacy.get("reviewed") is not True:
            errors.append("privacy_review.reviewed must be true")
        for key in sorted(PRIVACY_KEYS - {"reviewed"}):
            if privacy.get(key) is not False:
                errors.append(f"privacy_review.{key} must be false")

    errors.extend(_content_boundary_errors(root))
    return errors


def _load_json(path: Path) -> Any:
    return strict_json_value(path.read_text(encoding="utf-8"))


def _git_object_type(root: Path, object_name: str) -> tuple[Optional[str], str]:
    """Return one Git object type without trusting a shell or the working tree.

    Capability observations intentionally point at an earlier source commit.  A
    filesystem existence check against the current checkout would therefore be
    both insufficient and capable of accepting evidence added only later.
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-t", object_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        return None, "git executable is unavailable"
    except subprocess.TimeoutExpired:
        return None, "git object lookup timed out"
    except OSError as exc:
        return None, f"git object lookup failed: {exc}"
    if result.returncode != 0:
        return None, "Git object does not exist"
    object_type = result.stdout.strip()
    if object_type not in {"blob", "commit", "tag", "tree"}:
        return None, f"Git returned unexpected object type {object_type!r}"
    return object_type, ""


def _git_tree_entry(
    root: Path, source_revision: str, repository_path: str
) -> tuple[Optional[str], Optional[str], str]:
    """Return the exact tree mode/type without following a Git symlink blob."""

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "--literal-pathspecs",
                "ls-tree",
                "-z",
                "--full-tree",
                source_revision,
                "--",
                repository_path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        return None, None, "git executable is unavailable"
    except subprocess.TimeoutExpired:
        return None, None, "git tree lookup timed out"
    except OSError as exc:
        return None, None, f"git tree lookup failed: {exc}"
    if result.returncode != 0:
        return None, None, "Git tree lookup failed"

    matches: list[tuple[str, str]] = []
    for raw_entry in result.stdout.split("\0"):
        if not raw_entry:
            continue
        try:
            metadata, resolved_path = raw_entry.split("\t", 1)
            mode, object_type, _object_id = metadata.split(" ", 2)
        except ValueError:
            return None, None, "Git returned a malformed tree entry"
        if resolved_path == repository_path:
            matches.append((mode, object_type))
    if not matches:
        return None, None, "path does not exist in the source tree"
    if len(matches) != 1:
        return None, None, "Git returned multiple exact tree entries"
    mode, object_type = matches[0]
    return mode, object_type, ""


def source_revision_path_errors(
    root: Path,
    source_revision: str,
    paths: Iterable[tuple[str, str]],
) -> list[str]:
    """Verify repository-relative evidence against its exact source commit."""

    if SOURCE_REVISION.fullmatch(source_revision) is None:
        return []
    commit_type, failure = _git_object_type(root, source_revision)
    if commit_type != "commit":
        detail = failure or f"expected commit, found {commit_type}"
        return [f"source_revision {source_revision} is unavailable: {detail}"]

    errors: list[str] = []
    checked: set[str] = set()
    for label, repository_path in paths:
        if (
            repository_path in checked
            or not isinstance(repository_path, str)
            or not safe_repository_path(repository_path)
        ):
            continue
        checked.add(repository_path)
        mode, object_type, path_failure = _git_tree_entry(
            root, source_revision, repository_path
        )
        if object_type != "blob" or mode not in {"100644", "100755"}:
            detail = path_failure or (
                "expected regular file mode 100644 or 100755, "
                f"found {mode or 'unknown'} {object_type or 'object'}"
            )
            errors.append(
                f"{label} does not identify a file at source_revision "
                f"{source_revision}: {repository_path} ({detail})"
            )
    return errors


def _observation_source_paths(observation: dict[str, Any]) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    implementation = observation.get("implementation")
    if isinstance(implementation, dict):
        repository_paths = implementation.get("repository_paths")
        if isinstance(repository_paths, list):
            paths.extend(
                (f"implementation.repository_paths[{index}]", path)
                for index, path in enumerate(repository_paths)
                if isinstance(path, str)
            )
    evidence = observation.get("evidence")
    if isinstance(evidence, dict):
        for key in sorted(EVIDENCE_KEYS):
            evidence_paths = evidence.get(key)
            if isinstance(evidence_paths, list):
                paths.extend(
                    (f"evidence.{key}[{index}]", path)
                    for index, path in enumerate(evidence_paths)
                    if isinstance(path, str)
                )
    return paths


def capability_repository_errors(
    root: Path,
    project_config: Optional[dict[str, Any]] = None,
) -> list[str]:
    """Validate config, schema and every local observation without network I/O."""

    errors: list[str] = []
    config_path = root / "capabilities" / "config.json"
    schema_path = root / "capabilities" / "schemas" / "observation.schema.json"
    outbox_path = root / OUTBOX_PATH

    try:
        config = _load_json(config_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"capabilities/config.json cannot be loaded: {exc}"]
    errors.extend(capability_config_errors(config, project_config))

    try:
        schema = _load_json(schema_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(
            f"capabilities/schemas/observation.schema.json cannot be loaded: {exc}"
        )
    else:
        errors.extend(capability_schema_errors(schema))
        actual_schema_sha256 = hashlib.sha256(schema_path.read_bytes()).hexdigest()
        expected_schema_sha256 = (
            config.get("observation_schema_sha256")
            if isinstance(config, dict)
            else None
        )
        if actual_schema_sha256 != expected_schema_sha256:
            errors.append(
                "capabilities/schemas/observation.schema.json SHA-256 does not "
                "match capabilities/config.json"
            )

    if not outbox_path.is_dir() or outbox_path.is_symlink():
        errors.append("capabilities/outbox must be a real directory")
        return errors

    project_key = config.get("project_key") if isinstance(config, dict) else None
    if not isinstance(project_key, str):
        project_key = ""
    project_aliases = (
        config.get("project_aliases") if isinstance(config, dict) else None
    )
    if not isinstance(project_aliases, list):
        project_aliases = []
    valid_project_aliases = [
        alias for alias in project_aliases if isinstance(alias, str)
    ]
    observation_ids: set[str] = set()
    implementation_keys: set[tuple[str, str, str]] = set()
    for path in sorted(outbox_path.iterdir()):
        if path.name == ".gitkeep" and path.is_file() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            errors.append(
                f"{path.relative_to(root)} is not an allowed observation JSON file"
            )
            continue
        try:
            observation = _load_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path.relative_to(root)} cannot be loaded: {exc}")
            continue
        observation_errors = capability_observation_errors(
            observation,
            expected_project_key=project_key,
            project_aliases=valid_project_aliases,
            filename=path.name,
        )
        errors.extend(
            f"{path.relative_to(root)}: {failure}"
            for failure in observation_errors
        )
        if not isinstance(observation, dict):
            continue
        source_revision = observation.get("source_revision")
        if isinstance(source_revision, str):
            errors.extend(
                f"{path.relative_to(root)}: {failure}"
                for failure in source_revision_path_errors(
                    root,
                    source_revision,
                    _observation_source_paths(observation),
                )
            )
        observation_id = observation.get("observation_id")
        if isinstance(observation_id, str):
            if observation_id in observation_ids:
                errors.append(f"duplicate observation_id: {observation_id}")
            observation_ids.add(observation_id)
        feature = observation.get("feature")
        local_id = feature.get("local_id") if isinstance(feature, dict) else None
        if isinstance(local_id, str) and isinstance(source_revision, str):
            identity = (project_key, local_id, source_revision)
            if identity in implementation_keys:
                errors.append(
                    "duplicate capability observation identity: "
                    f"{project_key}/{local_id}@{source_revision}"
                )
            implementation_keys.add(identity)
    return errors
