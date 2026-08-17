from machinist.harness.base import Harness, HarnessCapabilities


class Codex(Harness):
    name = "codex"
    default_command = "codex"
    capabilities = HarnessCapabilities("cli-enforced")

    def spec_argv(self, prompt: str) -> list[str]:
        return [self.command, "exec", "--sandbox", "read-only", "--ephemeral", prompt]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "exec", "--full-auto", prompt]
