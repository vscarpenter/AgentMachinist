from machinist.harness.base import Harness, HarnessCapabilities


class Pi(Harness):
    name = "pi"
    default_command = "pi"
    capabilities = HarnessCapabilities("cli-enforced")

    def spec_argv(self, prompt: str) -> list[str]:
        argv = [
            self.command,
            "-p",
            "--tools",
            "read,grep,find,ls",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-session",
        ]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "-p"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv
