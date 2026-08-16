from machinist.harness.base import Harness


class Codex(Harness):
    name = "codex"
    default_command = "codex"

    def spec_argv(self, prompt: str) -> list[str]:
        return [self.command, "exec", prompt]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "exec", "--full-auto", prompt]
