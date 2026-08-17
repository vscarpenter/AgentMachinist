from machinist.harness.base import Harness


class OpenCode(Harness):
    name = "opencode"
    default_command = "opencode"

    def spec_argv(self, prompt: str) -> list[str]:
        # The built-in "plan" agent cannot edit files.
        return [self.command, "run", "--agent", "plan", prompt]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "run", prompt]
