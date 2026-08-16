from machinist.harness.base import Harness


class Pi(Harness):
    name = "pi"
    default_command = "pi"

    def spec_argv(self, prompt: str) -> list[str]:
        return [self.command, "-p", prompt]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "-p", prompt]
