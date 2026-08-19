from machinist.harness.base import Harness, HarnessCapabilities


class Codex(Harness):
    name = "codex"
    default_command = "codex"
    capabilities = HarnessCapabilities("cli-enforced")

    def spec_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "exec", "--sandbox", "read-only", "--ephemeral"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "exec", "--full-auto"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv
