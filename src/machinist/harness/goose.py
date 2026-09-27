from machinist.harness.base import (
    Harness,
    HarnessCapabilities,
    HarnessDescriptor,
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
        # -q keeps stdout to the response the Spec and Review parsers read;
        # --no-profile drops personal extensions and keeps only developer.
        argv = [
            self.command,
            "run",
            "-q",
            "--no-session",
            "--no-profile",
            "--with-builtin",
            "developer",
        ]
        argv.extend(self._passthrough_argv())
        argv.extend(["--text", prompt])
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "run", "--no-session"]
        argv.extend(self._passthrough_argv())
        argv.extend(["--text", prompt])
        return argv
