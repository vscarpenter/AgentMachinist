from machinist.harness.base import Harness


class ClaudeCode(Harness):
    name = "claude-code"
    default_command = "claude"

    def spec_argv(self, prompt: str) -> list[str]:
        return [self.command, "-p", prompt, "--output-format", "text"]

    def implement_argv(self, prompt: str) -> list[str]:
        # acceptEdits lets the headless run modify files without stalling
        # on interactive permission prompts.
        return [self.command, "-p", prompt, "--permission-mode", "acceptEdits"]
