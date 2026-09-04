"""Schema, compatibility helpers, and loader for machinist.yaml."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from machinist.github import normalize_repository_identity
from machinist.runtime_paths import RuntimePathError, read_text_file

CONFIG_FILENAME = "machinist.yaml"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_INSTRUCTION_FILES_PER_PHASE = 16
MAX_INSTRUCTION_PATH_BYTES = 1024
MAX_PHASE_INSTRUCTION_BYTES = 100_000


class ConfigError(Exception):
    """A machinist.yaml problem the user must fix."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses silent mapping-key replacement."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def strict_yaml_load(text: str) -> Any:
    """Load untrusted YAML safely while rejecting duplicate mapping keys."""
    return yaml.load(text, Loader=_UniqueKeySafeLoader)


class InstructionResolutionError(ConfigError):
    """A configured repository instruction source could not be read safely."""


class StrictModel(BaseModel):
    # Unknown keys in a hand-edited config file are almost always typos.
    model_config = ConfigDict(extra="forbid")


class HarnessName(str, Enum):
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    PI = "pi"
    CODEX = "codex"


HarnessIdentifier: TypeAlias = HarnessName | str


def harness_identifier(value: HarnessIdentifier) -> str:
    """Return one built-in or plugin identifier as a stable string."""
    return value.value if isinstance(value, HarnessName) else value


def _validated_harness_identifier(value: Any) -> HarnessIdentifier:
    if isinstance(value, HarnessName):
        return value
    if not isinstance(value, str):
        raise ValueError("harness name must be a string")
    try:
        return HarnessName(value)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
            raise ValueError(
                "harness name must be 1-64 lowercase letters, digits, or hyphens"
            ) from None
        return value


class HarnessPhase(str, Enum):
    SPEC = "spec"
    EXECUTE = "execute"
    REVIEW = "review"


# Adapter-owned flags cannot be repeated in extra_args because many CLIs let the
# last occurrence win. Keep this map beside configuration validation so unsafe
# combinations fail before a harness process starts. Adapter tests should keep
# it synchronized with the argv each adapter owns.
RESERVED_HARNESS_EXTRA_ARGS: dict[HarnessName, dict[HarnessPhase, frozenset[str]]] = {
    HarnessName.CLAUDE_CODE: {
        HarnessPhase.SPEC: frozenset(
            {
                "-p",
                "--",
                "--add-dir",
                "--allowedTools",
                "--dangerously-skip-permissions",
                "--disallowedTools",
                "--model",
                "--no-session-persistence",
                "--output-format",
                "--permission-mode",
                "--permission-prompt-tool",
                "--setting-sources",
                "--settings",
                "--tools",
            }
        ),
        HarnessPhase.EXECUTE: frozenset(
            {
                "-p",
                "--",
                "--add-dir",
                "--allowedTools",
                "--dangerously-skip-permissions",
                "--disallowedTools",
                "--model",
                "--no-session-persistence",
                "--permission-mode",
                "--permission-prompt-tool",
                "--setting-sources",
                "--settings",
                "--tools",
            }
        ),
    },
    HarnessName.CODEX: {
        HarnessPhase.SPEC: frozenset(
            {
                "exec",
                "-C",
                "-c",
                "--",
                "--add-dir",
                "--approval-policy",
                "--ask-for-approval",
                "--cd",
                "--dangerously-bypass-approvals-and-sandbox",
                "--ephemeral",
                "--full-auto",
                "--model",
                "--config",
                "--profile",
                "--sandbox",
            }
        ),
        HarnessPhase.EXECUTE: frozenset(
            {
                "exec",
                "-C",
                "-c",
                "--",
                "--add-dir",
                "--approval-policy",
                "--ask-for-approval",
                "--cd",
                "--dangerously-bypass-approvals-and-sandbox",
                "--ephemeral",
                "--full-auto",
                "--model",
                "--config",
                "--profile",
                "--sandbox",
            }
        ),
    },
    HarnessName.PI: {
        HarnessPhase.SPEC: frozenset(
            {
                "-p",
                "--",
                "--extensions",
                "--model",
                "--no-extensions",
                "--no-prompt-templates",
                "--no-session",
                "--no-skills",
                "--prompt-templates",
                "--session",
                "--skills",
                "--tools",
            }
        ),
        HarnessPhase.EXECUTE: frozenset(
            {
                "-p",
                "--",
                "--extensions",
                "--model",
                "--no-session",
                "--session",
                "--skills",
                "--tools",
            }
        ),
    },
    HarnessName.OPENCODE: {
        HarnessPhase.SPEC: frozenset(
            {
                "run",
                "--",
                "--agent",
                "--continue",
                "--model",
                "--pure",
                "--session",
            }
        ),
        HarnessPhase.EXECUTE: frozenset(
            {
                "run",
                "--",
                "--agent",
                "--continue",
                "--model",
                "--pure",
                "--session",
            }
        ),
    },
}


def reserved_harness_extra_args(
    name: HarnessIdentifier, phase: HarnessPhase | str
) -> frozenset[str]:
    """Return argv tokens controlled by the named adapter and Phase."""
    if not isinstance(name, HarnessName):
        return frozenset()
    resolved = HarnessPhase(phase)
    custody_phase = HarnessPhase.SPEC if resolved is HarnessPhase.REVIEW else resolved
    return RESERVED_HARNESS_EXTRA_ARGS[name][custody_phase]


def validate_harness_extra_args(
    name: HarnessIdentifier,
    phase: HarnessPhase | str,
    extra_args: list[str],
) -> None:
    """Reject args that can replace adapter-owned custody or sandbox controls."""
    resolved_phase = HarnessPhase(phase)
    reserved = reserved_harness_extra_args(name, resolved_phase)
    for argument in extra_args:
        if not argument or "\x00" in argument:
            raise ValueError(
                "extra_args entries must be non-empty argv strings without NUL bytes"
            )
        key = argument.split("=", 1)[0] if argument.startswith("--") else argument
        if argument.startswith("-") and not argument.startswith("--"):
            short_match = next(
                (
                    candidate
                    for candidate in reserved
                    if len(candidate) == 2 and argument.startswith(candidate)
                ),
                None,
            )
            key = short_match or key
        if key in reserved:
            raise ValueError(
                f"extra_args cannot set adapter-owned argument '{key}' "
                f"for {harness_identifier(name)} {resolved_phase.value}"
            )


class HarnessPhaseConfig(StrictModel):
    """Optional per-Phase overrides inherited from the legacy harness block."""

    name: HarnessIdentifier | None = None
    command: str | None = None
    model: str | None = None
    extra_args: list[str] | None = None
    timeout_minutes: int | None = Field(default=None, ge=1, le=240)

    @field_validator("command", "model")
    @classmethod
    def _optional_text_is_argv_safe(cls, value: str | None) -> str | None:
        return _validate_optional_harness_text(value)

    @field_validator("name", mode="before")
    @classmethod
    def _valid_name(cls, value: Any) -> HarnessIdentifier | None:
        return None if value is None else _validated_harness_identifier(value)


class HarnessConfig(StrictModel):
    # These fields remain the compatibility surface consumed by 0.3.x callers.
    name: HarnessIdentifier = HarnessName.CLAUDE_CODE
    command: str | None = None
    model: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    timeout_minutes: int = Field(default=30, ge=1, le=240)
    spec_timeout_minutes: int = Field(default=10, ge=1, le=60)
    spec: HarnessPhaseConfig | None = None
    execute: HarnessPhaseConfig | None = None
    review: HarnessPhaseConfig | None = None

    @field_validator("command", "model")
    @classmethod
    def _optional_text_is_argv_safe(cls, value: str | None) -> str | None:
        return _validate_optional_harness_text(value)

    @field_validator("name", mode="before")
    @classmethod
    def _valid_name(cls, value: Any) -> HarnessIdentifier:
        return _validated_harness_identifier(value)

    @model_validator(mode="after")
    def _extra_args_preserve_adapter_controls(self) -> "HarnessConfig":
        for phase_name, profile in (("spec", self.spec), ("review", self.review)):
            if (
                profile is not None
                and profile.timeout_minutes is not None
                and profile.timeout_minutes > 60
            ):
                raise ValueError(
                    f"harness.{phase_name}.timeout_minutes cannot exceed 60"
                )
        # The base remains directly consumable by older callers, so validate it
        # for both Phases even when a profile overrides it.
        for phase in HarnessPhase:
            validate_harness_extra_args(self.name, phase, self.extra_args)
            name, extra_args = self._resolved_name_and_args(phase)
            validate_harness_extra_args(name, phase, extra_args)
        return self

    def _profile(self, phase: HarnessPhase) -> HarnessPhaseConfig | None:
        if phase is HarnessPhase.SPEC:
            return self.spec
        if phase is HarnessPhase.REVIEW:
            return self.review
        return self.execute

    def _resolved_name_and_args(
        self, phase: HarnessPhase
    ) -> tuple[HarnessIdentifier, list[str]]:
        profile = self._profile(phase)
        if profile is None:
            return self.name, list(self.extra_args)
        name = profile.name if profile.name is not None else self.name
        if "extra_args" in profile.model_fields_set:
            return name, list(profile.extra_args or [])
        if name != self.name:
            return name, []
        return name, list(self.extra_args)

    def for_phase(self, phase: HarnessPhase | str) -> "HarnessConfig":
        """Resolve a Phase profile to the legacy HarnessConfig runtime shape."""
        resolved_phase = HarnessPhase(phase)
        profile = self._profile(resolved_phase)
        provider_changed = (
            profile is not None
            and profile.name is not None
            and profile.name != self.name
        )

        def inherited(field: str, base: Any) -> Any:
            if profile is not None and field in profile.model_fields_set:
                return getattr(profile, field)
            if provider_changed and field in {"command", "model"}:
                return None
            if provider_changed and field == "extra_args":
                return []
            return base

        name = inherited("name", self.name) or self.name
        extra_args = inherited("extra_args", self.extra_args)
        read_only_phase = resolved_phase in {HarnessPhase.SPEC, HarnessPhase.REVIEW}
        base_timeout = (
            self.spec_timeout_minutes if read_only_phase else self.timeout_minutes
        )
        phase_timeout = inherited("timeout_minutes", base_timeout)
        if phase_timeout is None:
            phase_timeout = base_timeout
        # model_construct deliberately avoids recursively validating a resolved
        # object; the parent model validator already checked its effective argv.
        return HarnessConfig.model_construct(
            name=name,
            command=inherited("command", self.command),
            model=inherited("model", self.model),
            extra_args=list(extra_args or []),
            timeout_minutes=(
                self.timeout_minutes if read_only_phase else int(phase_timeout)
            ),
            spec_timeout_minutes=(
                int(phase_timeout) if read_only_phase else self.spec_timeout_minutes
            ),
            spec=None,
            execute=None,
            review=None,
        )


def _validate_optional_harness_text(value: str | None) -> str | None:
    if value is not None and (not value.strip() or "\x00" in value):
        raise ValueError("must be non-empty and contain no NUL bytes")
    return value


def _normalize_instruction_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("instruction paths must be strings")
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(
            "instruction paths must be non-empty repository-relative POSIX paths"
        )
    if len(value.encode("utf-8")) > MAX_INSTRUCTION_PATH_BYTES:
        raise ValueError(
            f"instruction paths cannot exceed {MAX_INSTRUCTION_PATH_BYTES} bytes"
        )

    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value.startswith("~")
        or value.startswith("./")
        or value.endswith("/")
        or "//" in value
        or ".." in path.parts
        or path.as_posix() in {"", "."}
        or path.as_posix() != value
    ):
        raise ValueError(
            "instruction paths must be canonical repository-relative POSIX paths"
        )
    return value


class PhaseInstructionsConfig(StrictModel):
    """Repository-local prompt additions for one pipeline Phase."""

    paths: list[str] = Field(
        default_factory=list,
        max_length=MAX_INSTRUCTION_FILES_PER_PHASE,
    )
    append: str | None = None

    @field_validator("paths")
    @classmethod
    def _safe_unique_paths(cls, value: list[str]) -> list[str]:
        normalized = [_normalize_instruction_path(path) for path in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("instruction paths cannot contain duplicates")
        return normalized

    @field_validator("append")
    @classmethod
    def _bounded_inline_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or "\x00" in value:
            raise ValueError(
                "instructions append must be non-empty and contain no NUL bytes"
            )
        if len(value.encode("utf-8")) > MAX_PHASE_INSTRUCTION_BYTES:
            raise ValueError(
                "instructions append cannot exceed "
                f"{MAX_PHASE_INSTRUCTION_BYTES} UTF-8 bytes"
            )
        return value


class InstructionsConfig(StrictModel):
    """Per-Phase repository instruction overlays."""

    spec: PhaseInstructionsConfig = Field(default_factory=PhaseInstructionsConfig)
    execute: PhaseInstructionsConfig = Field(default_factory=PhaseInstructionsConfig)
    review: PhaseInstructionsConfig = Field(default_factory=PhaseInstructionsConfig)

    def for_phase(self, phase: HarnessPhase | str) -> PhaseInstructionsConfig:
        resolved_phase = HarnessPhase(phase)
        if resolved_phase is HarnessPhase.SPEC:
            return self.spec
        if resolved_phase is HarnessPhase.REVIEW:
            return self.review
        return self.execute

    def evidence(self, phase: HarnessPhase | str, text: str) -> dict[str, Any]:
        """Checkpoint-ready audit facts about one Phase's resolved overlay.

        Spec and Execute persist exactly this mapping; nothing reads it back,
        so one vocabulary serves both Phases.
        """
        profile = self.for_phase(phase)
        return {
            "instructions_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "instruction_paths": list(profile.paths),
            "instruction_append": profile.append is not None,
        }

    def resolve(self, phase: HarnessPhase | str, repo_root: str | Path) -> str:
        """Read and combine one Phase's safe repository-local instructions."""
        resolved_phase = HarnessPhase(phase)
        profile = self.for_phase(resolved_phase)
        parts: list[str] = []
        if profile.paths:
            root = _resolve_instruction_root(repo_root)
            seen: set[Path] = set()
            for relative_path in profile.paths:
                resolved_path, text = _read_instruction_file(
                    root,
                    relative_path,
                    phase=resolved_phase,
                )
                if resolved_path in seen:
                    raise InstructionResolutionError(
                        f"{resolved_phase.value} instruction paths resolve to "
                        f"the same file: {relative_path}"
                    )
                seen.add(resolved_path)
                if text:
                    parts.append(text)
        if profile.append is not None:
            parts.append(profile.append)

        combined = "\n\n".join(parts)
        size = len(combined.encode("utf-8"))
        if size > MAX_PHASE_INSTRUCTION_BYTES:
            raise InstructionResolutionError(
                f"{resolved_phase.value} instructions are {size} bytes; maximum is "
                f"{MAX_PHASE_INSTRUCTION_BYTES}"
            )
        return combined


def _resolve_instruction_root(repo_root: str | Path) -> Path:
    supplied = Path(repo_root)
    try:
        root = supplied.expanduser().resolve(strict=True)
    except OSError as exc:
        raise InstructionResolutionError(
            f"cannot resolve repository root {supplied}: {exc}"
        ) from exc
    if not root.is_dir():
        raise InstructionResolutionError(f"repository root is not a directory: {root}")
    return root


def _read_instruction_file(
    root: Path,
    relative_path: str,
    *,
    phase: HarnessPhase,
) -> tuple[Path, str]:
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise InstructionResolutionError(
            f"cannot resolve {phase.value} instruction {relative_path}: {exc}"
        ) from exc
    if not resolved.is_relative_to(root):
        raise InstructionResolutionError(
            f"{phase.value} instruction escapes repository root: {relative_path}"
        )
    if not resolved.is_file():
        raise InstructionResolutionError(
            f"{phase.value} instruction is not a regular file: {relative_path}"
        )
    try:
        with resolved.open("rb") as stream:
            payload = stream.read(MAX_PHASE_INSTRUCTION_BYTES + 1)
    except OSError as exc:
        raise InstructionResolutionError(
            f"cannot read {phase.value} instruction {relative_path}: {exc}"
        ) from exc
    if len(payload) > MAX_PHASE_INSTRUCTION_BYTES:
        raise InstructionResolutionError(
            f"{phase.value} instruction {relative_path} exceeds "
            f"{MAX_PHASE_INSTRUCTION_BYTES} bytes"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstructionResolutionError(
            f"{phase.value} instruction is not valid UTF-8: {relative_path}"
        ) from exc
    if "\x00" in text:
        raise InstructionResolutionError(
            f"{phase.value} instruction contains a NUL byte: {relative_path}"
        )
    return resolved, text


class WorkspaceStrategy(str, Enum):
    WORKTREE = "worktree"
    CLONE = "clone"


class CleanupPolicy(str, Enum):
    ALWAYS = "always"
    ON_SUCCESS = "on_success"
    NEVER = "never"


class SpecSource(str, Enum):
    LOCAL = "local"
    GITHUB_ACTIONS = "github-actions"


class SpecInstall(str, Enum):
    PYPI = "pypi"
    CHECKOUT = "checkout"


class LabelsConfig(StrictModel):
    trigger: str = "agent-task"
    approved: str = "machinist:approved"

    @field_validator("trigger", "approved")
    @classmethod
    def _workflow_safe_label(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .:_/-]{0,49}", value):
            raise ValueError(
                "label must be 1-50 characters using letters, digits, spaces, "
                "'.', ':', '_', '/', or '-'"
            )
        return value


class GitHubConfig(StrictModel):
    repo: str | None = None
    spec_source: SpecSource = SpecSource.LOCAL
    spec_install: SpecInstall = SpecInstall.PYPI
    manage_workflows: bool = True
    spec_secret_env: str | None = None
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    poll_interval_seconds: int = Field(default=60, ge=10)

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, value: str | None) -> str | None:
        normalized = normalize_repository_identity(value)
        if value is not None and (normalized is None or normalized != value.casefold()):
            raise ValueError("must look like 'owner/repo'")
        return value

    @field_validator("spec_secret_env")
    @classmethod
    def _secret_environment_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", value):
            raise ValueError(
                "spec_secret_env must be an uppercase environment variable name"
            )
        return value


class WorkspaceConfig(StrictModel):
    root: Path = Path("~/.machinist/workspaces")
    strategy: WorkspaceStrategy = WorkspaceStrategy.WORKTREE
    cleanup: CleanupPolicy = CleanupPolicy.ON_SUCCESS
    branch_prefix: str = "agent/"

    @field_validator("branch_prefix")
    @classmethod
    def _safe_branch_prefix(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*/", value)
            or ".." in value
            or "//" in value
        ):
            raise ValueError(
                "branch_prefix must be a safe Git ref prefix ending in '/'"
            )
        return value

    def resolved_root(self) -> Path:
        return self.root.expanduser().resolve()


class GateMutationPolicy(str, Enum):
    FORBID = "forbid"
    ALLOW = "allow"


class VerificationGateConfig(StrictModel):
    name: str
    command: str
    timeout_minutes: int = Field(default=30, ge=1, le=240)
    required: bool = True
    mutation_policy: GateMutationPolicy = GateMutationPolicy.FORBID

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,49}", value):
            raise ValueError(
                "gate name must be 1-50 characters using letters, digits, spaces, '.', '_', or '-'"
            )
        return value

    @field_validator("command")
    @classmethod
    def _nonempty_command(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("gate command must be non-empty and contain no NUL bytes")
        return value

    @model_validator(mode="after")
    def _advisory_gate_is_read_only(self) -> "VerificationGateConfig":
        if not self.required and self.mutation_policy is GateMutationPolicy.ALLOW:
            raise ValueError("advisory verification gates cannot allow mutations")
        return self


class VerificationConfig(StrictModel):
    gates: list[VerificationGateConfig] = Field(default_factory=list)
    # When true, Execute tells the harness about the gate commands and lets it
    # run exactly those commands to iterate before finishing. The controller's
    # own gate run afterwards remains the authoritative check either way.
    harness_may_run_gates: bool = True

    @field_validator("gates")
    @classmethod
    def _unique_gate_names(
        cls, value: list[VerificationGateConfig]
    ) -> list[VerificationGateConfig]:
        names = [gate.name.casefold() for gate in value]
        if len(names) != len(set(names)):
            raise ValueError("verification gate names must be unique")
        return value


class TestsConfig(StrictModel):
    """Legacy 0.3.x test-gate shape; normalized by resolved_verification_gates."""

    command: str | None = None

    @field_validator("command")
    @classmethod
    def _legacy_command_is_usable(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("tests.command must be non-empty or null")
        return value


class ReviewConfig(StrictModel):
    """Independent read-only review before a pull request becomes ready."""

    enabled: bool = False


class TelemetryConfig(StrictModel):
    """Optional aggregate OTLP/HTTP export; disabled without an endpoint."""

    otlp_endpoint: str | None = None
    timeout_seconds: int = Field(default=5, ge=1, le=30)

    @field_validator("otlp_endpoint")
    @classmethod
    def _http_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError(
                "OTLP endpoint must be an HTTP(S) URL without credentials or fragments"
            )
        return value


class Weekday(str, Enum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


_WEEKDAYS = tuple(Weekday)


def _validate_timezone_name(value: str) -> str:
    if value == "local":
        return value
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("must be 'local' or a valid IANA timezone") from exc
    return value


class AllowedHoursConfig(StrictModel):
    start: str
    end: str
    timezone: str = "local"
    days: list[Weekday] = Field(default_factory=lambda: list(_WEEKDAYS))

    @field_validator("start", "end")
    @classmethod
    def _clock_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("must use 24-hour HH:MM format")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @field_validator("days")
    @classmethod
    def _usable_days(cls, value: list[Weekday]) -> list[Weekday]:
        if not value:
            raise ValueError("allowed_hours.days must contain at least one day")
        if len(value) != len(set(value)):
            raise ValueError("allowed_hours.days cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _nonempty_window(self) -> "AllowedHoursConfig":
        if self.start == self.end:
            raise ValueError("allowed-hours start and end cannot be equal")
        return self

    def contains(self, moment: datetime | None = None) -> bool:
        """Return whether an instant falls in this window, including overnight windows."""
        current = moment or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.astimezone()
        if self.timezone == "local":
            current = current.astimezone()
        else:
            current = current.astimezone(ZoneInfo(self.timezone))

        current_time = current.time().replace(tzinfo=None)
        start = time.fromisoformat(self.start)
        end = time.fromisoformat(self.end)
        today = _WEEKDAYS[current.weekday()]
        allowed = set(self.days)
        if start < end:
            return today in allowed and start <= current_time < end
        if current_time >= start:
            return today in allowed
        previous = _WEEKDAYS[(current - timedelta(days=1)).weekday()]
        return current_time < end and previous in allowed


class TaskBudgetConfig(StrictModel):
    max_tasks_per_day: int | None = Field(default=None, ge=1, le=10_000)
    max_runtime_minutes_per_day: int | None = Field(default=None, ge=1, le=24 * 60)
    timezone: str = "local"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        return _validate_timezone_name(value)

    @model_validator(mode="after")
    def _has_a_limit(self) -> "TaskBudgetConfig":
        if self.max_tasks_per_day is None and self.max_runtime_minutes_per_day is None:
            raise ValueError("task_budget must set at least one daily limit")
        return self


class QueueConfig(StrictModel):
    max_tasks_per_pass: int = Field(default=1, ge=1, le=100)
    allowed_hours: AllowedHoursConfig | None = None
    task_budget: TaskBudgetConfig | None = None


class NotificationBackend(str, Enum):
    DISABLED = "disabled"
    DESKTOP = "desktop"
    COMMAND = "command"
    WEBHOOK = "webhook"


class NotificationEvent(str, Enum):
    FAILURE = "failure"
    SPEC_READY = "spec_ready"
    APPROVAL_STALE = "approval_stale"
    PR_READY = "pr_ready"


class CommandNotificationConfig(StrictModel):
    # Future runners must use argv with shell=False and send event JSON on stdin.
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=5, ge=1, le=30)

    @field_validator("argv")
    @classmethod
    def _safe_argv(cls, value: list[str]) -> list[str]:
        if any(not part or "\x00" in part for part in value):
            raise ValueError(
                "notification argv entries must be non-empty and contain no NUL bytes"
            )
        return value


class WebhookNotificationConfig(StrictModel):
    # URLs often contain credentials, so configuration names an environment
    # variable rather than encouraging a secret in machinist.yaml.
    url_env: str = "MACHINIST_NOTIFICATION_WEBHOOK_URL"
    authorization_env: str | None = None
    timeout_seconds: int = Field(default=5, ge=1, le=30)

    @field_validator("url_env", "authorization_env")
    @classmethod
    def _environment_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("must be an environment variable name")
        return value


class NotificationConfig(StrictModel):
    backend: NotificationBackend = NotificationBackend.DESKTOP
    events: list[NotificationEvent] = Field(
        default_factory=lambda: [NotificationEvent.FAILURE]
    )
    command: CommandNotificationConfig | None = None
    webhook: WebhookNotificationConfig | None = None

    @field_validator("events")
    @classmethod
    def _unique_events(cls, value: list[NotificationEvent]) -> list[NotificationEvent]:
        if len(value) != len(set(value)):
            raise ValueError("notification events cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _backend_shape(self) -> "NotificationConfig":
        if self.backend is NotificationBackend.COMMAND:
            if self.command is None or self.webhook is not None:
                raise ValueError("command backend requires command and forbids webhook")
        elif self.backend is NotificationBackend.WEBHOOK:
            if self.webhook is None or self.command is not None:
                raise ValueError("webhook backend requires webhook and forbids command")
        elif self.command is not None or self.webhook is not None:
            raise ValueError(
                "desktop/disabled notification backends cannot configure command or webhook"
            )
        return self


def _normalize_denied_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError(
            "denied paths must be non-empty repository-relative POSIX paths"
        )
    candidate = value.rstrip("/")
    path = PurePosixPath(candidate)
    if path.is_absolute() or candidate in {"", "."} or ".." in path.parts:
        raise ValueError("denied paths must stay within the repository")
    return path.as_posix()


class LimitsConfig(StrictModel):
    max_issue_body_chars: int = Field(default=50_000, ge=1, le=1_000_000)
    max_spec_chars: int = Field(default=100_000, ge=1, le=2_000_000)
    max_changed_files: int = Field(default=100, ge=1, le=100_000)
    max_changed_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    denied_paths: list[str] = Field(default_factory=lambda: [".machinist"])
    allow_binary: bool = False
    # A harness must never green the gate by deleting the tests that fail it.
    # Opt in per repository when an approved Spec legitimately removes tests.
    allow_test_deletions: bool = False

    @field_validator("denied_paths")
    @classmethod
    def _safe_denied_paths(cls, value: list[str]) -> list[str]:
        normalized = [_normalize_denied_path(item) for item in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("denied_paths cannot contain duplicates")
        return normalized

    def path_is_denied(self, relative_path: str) -> bool:
        """Fail closed for invalid paths and match exact denied directory prefixes."""
        try:
            normalized = _normalize_denied_path(relative_path)
        except ValueError:
            return True
        return any(
            normalized == denied or normalized.startswith(f"{denied}/")
            for denied in self.denied_paths
        )


class MachinistConfig(StrictModel):
    version: Literal[1] = 1
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    instructions: InstructionsConfig = Field(default_factory=InstructionsConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @field_validator("version", mode="before")
    @classmethod
    def _exact_config_version(cls, value: Any) -> Any:
        # Literal validation normally coerces True and 1.0 to integer 1. A
        # version marker is a protocol boundary, so accept the integer only.
        if type(value) is not int or value != 1:
            raise ValueError("only integer config version 1 is supported")
        return value

    @model_validator(mode="after")
    def _compatible_dispatch_and_gates(self) -> "MachinistConfig":
        if self.tests.command is not None and self.verification.gates:
            raise ValueError(
                "configure either legacy tests.command or verification.gates, not both"
            )
        return self

    def harness_for(self, phase: HarnessPhase | str) -> HarnessConfig:
        """Return a legacy-shaped, fully resolved harness config for a Phase."""
        return self.harness.for_phase(phase)

    def resolve_instructions(
        self,
        phase: HarnessPhase | str,
        repo_root: str | Path,
    ) -> str:
        """Return a Phase's bounded instruction overlay for one repository."""
        return self.instructions.resolve(phase, repo_root)

    def resolved_verification_gates(self) -> tuple[VerificationGateConfig, ...]:
        """Normalize named gates or the legacy tests.command to one ordered API."""
        if self.verification.gates:
            return tuple(self.verification.gates)
        if self.tests.command is None:
            return ()
        return (
            VerificationGateConfig(
                name="legacy-tests",
                command=self.tests.command,
                timeout_minutes=self.harness_for(HarnessPhase.EXECUTE).timeout_minutes,
                required=True,
                # The legacy shell gate could mutate files before commit. Keep
                # that behavior until a repository opts into named gates.
                mutation_policy=GateMutationPolicy.ALLOW,
            ),
        )

    @classmethod
    def starter_projection(
        cls,
        *,
        harness_name: str | None = None,
        test_command: str | None = None,
        manage_workflows: bool = True,
        spec_source: str | None = None,
        notification_backend: str | None = None,
        notification_events: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return the sparse, validated configuration written by ``init``."""
        validated = cls.model_validate(
            _starter_input(
                harness_name=harness_name,
                test_command=test_command,
                manage_workflows=manage_workflows,
                spec_source=spec_source,
                notification_backend=notification_backend,
                notification_events=notification_events,
            )
        )
        projection = _starter_projection(
            validated,
            include_notification_backend=notification_backend is not None,
            include_notification_events=notification_events is not None,
        )
        # Guard this projection as a persisted protocol, not merely a display.
        cls.model_validate(projection)
        return projection

    def effective_projection(self) -> dict[str, Any]:
        """Return canonical JSON-safe runtime behavior after compatibility resolution."""
        effective = self.model_dump(mode="json")
        harnesses: dict[str, Any] = {}
        for phase in HarnessPhase:
            harness = self.harness_for(phase)
            timeout = (
                harness.spec_timeout_minutes
                if phase in {HarnessPhase.SPEC, HarnessPhase.REVIEW}
                else harness.timeout_minutes
            )
            harnesses[phase.value] = {
                "name": harness_identifier(harness.name),
                "command": harness.command,
                "model": harness.model,
                "extra_args": list(harness.extra_args),
                "timeout_minutes": timeout,
            }
        effective["harness"] = harnesses
        effective["workspace"]["root"] = str(self.workspace.resolved_root())
        effective["verification"] = {
            "gates": [
                gate.model_dump(mode="json")
                for gate in self.resolved_verification_gates()
            ]
        }
        effective.pop("tests", None)
        return effective


def _starter_input(
    *,
    harness_name: str | None,
    test_command: str | None,
    manage_workflows: bool,
    spec_source: str | None,
    notification_backend: str | None,
    notification_events: list[str] | None,
) -> dict[str, Any]:
    supplied: dict[str, Any] = {
        "version": 1,
        "tests": {"command": test_command},
        "github": {"manage_workflows": manage_workflows},
        "review": {"enabled": True},
    }
    if harness_name is not None:
        supplied["harness"] = {"name": harness_name}
    if spec_source is not None:
        supplied["github"]["spec_source"] = spec_source
    notifications = _starter_notification_input(
        notification_backend,
        notification_events,
    )
    if notifications is not None:
        supplied["notifications"] = notifications
    return supplied


def _starter_notification_input(
    backend: str | None,
    events: list[str] | None,
) -> dict[str, Any] | None:
    if backend is None and events is None:
        return None
    notifications: dict[str, Any] = {}
    if backend is not None:
        notifications["backend"] = backend
    if events is not None:
        notifications["events"] = events
    return notifications


def _starter_projection(
    config: MachinistConfig,
    *,
    include_notification_backend: bool,
    include_notification_events: bool,
) -> dict[str, Any]:
    values = config.model_dump(mode="json")
    projection: dict[str, Any] = {
        "version": values["version"],
        "harness": {"name": values["harness"]["name"]},
        "tests": {"command": values["tests"]["command"]},
        "github": _starter_github_projection(config, values),
        "workspace": {
            key: values["workspace"][key]
            for key in ("root", "strategy", "cleanup", "branch_prefix")
        },
        "review": {"enabled": values["review"]["enabled"]},
    }
    notifications = _starter_notification_projection(
        values,
        include_backend=include_notification_backend,
        include_events=include_notification_events,
    )
    if notifications is not None:
        projection["notifications"] = notifications
    return projection


def _starter_github_projection(
    config: MachinistConfig,
    values: dict[str, Any],
) -> dict[str, Any]:
    github = {
        key: values["github"][key]
        for key in ("repo", "spec_source", "labels", "poll_interval_seconds")
    }
    if config.github.manage_workflows != GitHubConfig().manage_workflows:
        github["manage_workflows"] = values["github"]["manage_workflows"]
    return github


def _starter_notification_projection(
    values: dict[str, Any],
    *,
    include_backend: bool,
    include_events: bool,
) -> dict[str, Any] | None:
    if not include_backend and not include_events:
        return None
    notifications: dict[str, Any] = {}
    if include_backend:
        notifications["backend"] = values["notifications"]["backend"]
    if include_events:
        notifications["events"] = values["notifications"]["events"]
    return notifications


def config_json_schema() -> dict[str, Any]:
    """Return the generated JSON Schema for editors and integration tooling."""
    return MachinistConfig.model_json_schema()


def load_config(path: str | Path = CONFIG_FILENAME) -> MachinistConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found. This repo is not configured yet. "
            "Run 'machinist onboard' (recommended) or 'machinist onboard --setup-pr' on GitHub "
            "— fallback: 'machinist init'. "
            "After setup, verify with 'machinist doctor --run-gates'. "
            "Guide: https://agentmachinist.vinny.dev/first-run-guide.html"
        )
    try:
        text = read_config_text(path)
    except UnicodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8 text") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = strict_yaml_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return MachinistConfig.model_validate(data or {})
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{_format_errors(exc)}") from exc


def read_config_text(path: str | Path) -> str:
    """Read one bounded regular UTF-8 config file without following its leaf."""
    target = Path(path)
    try:
        return read_text_file(target, max_bytes=MAX_CONFIG_BYTES)
    except UnicodeError as exc:
        raise ConfigError(f"{target} is not valid UTF-8 text") from exc
    except RuntimePathError as exc:
        raise ConfigError(f"cannot safely read {target}: {exc}") from exc


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)
