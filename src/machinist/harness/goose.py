import json
from pathlib import Path

from machinist.harness.base import (
    Harness,
    HarnessCapabilities,
    HarnessDescriptor,
    HarnessError,
)


class Goose(Harness):
    name = "goose"
    default_command = "goose"
    # The developer builtin that reads files also writes them and runs shell
    # commands, so Spec and Review custody rests on controller postconditions.
    capabilities = HarnessCapabilities("advisory")
    # pressly/goose, a Go database migration tool, installs the same executable.
    auto_select = False
    # Goose ships through an install script or Homebrew, not a pinnable
    # package, so there is no hosted Spec CI profile.
    descriptor = HarnessDescriptor(
        contract_version=1,
        display_name="Goose",
        documentation_url="https://goose-docs.ai/docs/",
        phases=frozenset({"spec", "execute", "review"}),
    )

    def environment_overrides(self) -> dict[str, str]:
        # approve and smart_approve wait for a confirmation no headless run can give.
        return {"GOOSE_MODE": "auto"}

    def spec_argv(self, prompt: str) -> list[str]:
        # Even with -q, text output mixes the tool transcript into the answer, so
        # Spec and Review read JSON and keep only the final assistant message.
        # --no-profile drops personal extensions and keeps only developer.
        argv = [
            self.command,
            "run",
            "-q",
            "--no-session",
            "--no-profile",
            "--with-builtin",
            "developer",
            "--output-format",
            "json",
        ]
        argv.extend(self._passthrough_argv())
        argv.extend(["--text", prompt])
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "run", "--no-session"]
        argv.extend(self._passthrough_argv())
        argv.extend(["--text", prompt])
        return argv

    def generate_spec(self, prompt: str, cwd: Path) -> str:
        return _final_answer(super().generate_spec(prompt, cwd))

    def review(self, prompt: str, cwd: Path) -> str:
        return _final_answer(super().review(prompt, cwd))


def _final_answer(stdout: str) -> str:
    """Return the text of the last assistant message in a JSON transcript."""
    try:
        messages = json.loads(stdout)["messages"]
        replies = [message for message in messages if message["role"] == "assistant"]
        # Only the last reply counts; earlier text is mid-task narration.
        texts = (
            [item["text"] for item in replies[-1]["content"] if item["type"] == "text"]
            if replies
            else []
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise HarnessError(f"goose output is not a JSON transcript: {exc}") from exc
    if not texts:
        raise HarnessError("goose transcript has no final assistant answer")
    return "\n".join(texts)
