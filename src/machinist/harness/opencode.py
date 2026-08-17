from machinist.harness.base import Harness, HarnessCapabilities


class OpenCode(Harness):
    name = "opencode"
    default_command = "opencode"
    capabilities = HarnessCapabilities("advisory")

    def spec_argv(self, prompt: str) -> list[str]:
        # The built-in "plan" agent cannot edit files.
        return [self.command, "run", "--pure", "--agent", "plan", prompt]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "run", prompt]
