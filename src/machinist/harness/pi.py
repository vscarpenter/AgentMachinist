from machinist.harness.base import Harness, HarnessCapabilities


class Pi(Harness):
    name = "pi"
    default_command = "pi"
    capabilities = HarnessCapabilities("cli-enforced")

    def spec_argv(self, prompt: str) -> list[str]:
        return [
            self.command,
            "-p",
            "--tools",
            "read,grep,find,ls",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-session",
            prompt,
        ]

    def implement_argv(self, prompt: str) -> list[str]:
        return [self.command, "-p", prompt]
