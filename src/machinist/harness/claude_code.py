from machinist.harness.base import Harness, HarnessCapabilities


class ClaudeCode(Harness):
    name = "claude-code"
    default_command = "claude"
    capabilities = HarnessCapabilities("cli-enforced")

    def spec_argv(self, prompt: str) -> list[str]:
        argv = [
            self.command,
            "-p",
            prompt,
            "--output-format",
            "text",
            "--permission-mode",
            "plan",
            "--tools",
            "Read,Grep,Glob",
            "--no-session-persistence",
        ]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        # acceptEdits lets the headless run modify files without stalling
        # on interactive permission prompts.
        argv = [self.command, "-p", prompt, "--permission-mode", "acceptEdits"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        return argv
