from machinist.harness.base import Harness


class Pi(Harness):
    name = "pi"
    default_command = "pi"

    def spec_argv(self, prompt: str) -> list[str]:
        # Exclude the edit/write tools so spec generation stays read-only.
        return [self.command, "-p", "-xt", "edit,write", prompt]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "-p", prompt]
