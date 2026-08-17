from machinist.harness.base import Harness, HarnessCapabilities


class ClaudeCode(Harness):
    name = "claude-code"
    default_command = "claude"
    capabilities = HarnessCapabilities("cli-enforced")

    def spec_argv(self, prompt: str) -> list[str]:
        return [
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

    def implement_argv(self, prompt: str) -> list[str]:
        # acceptEdits lets the headless run modify files without stalling
        # on interactive permission prompts.
        return [self.command, "-p", prompt, "--permission-mode", "acceptEdits"]
