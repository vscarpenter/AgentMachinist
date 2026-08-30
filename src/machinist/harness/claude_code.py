import json
import subprocess

from machinist.harness.base import Harness, HarnessCapabilities


class ClaudeCode(Harness):
    name = "claude-code"
    default_command = "claude"
    capabilities = HarnessCapabilities("cli-enforced")

    def authentication_argv(self) -> list[str]:
        return [self.command, "auth", "status"]

    def authentication_ready(self, result: subprocess.CompletedProcess) -> bool:
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError):
            return False
        return payload.get("loggedIn") is True

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
        if self.allowed_commands:
            # acceptEdits still denies headless Bash, so verification-gate
            # commands need explicit allow rules: exact plus prefix, letting
            # the harness iterate on subsets (e.g. one failing test file).
            argv.append("--allowedTools")
            for command in self.allowed_commands:
                argv.extend([f"Bash({command})", f"Bash({command}:*)"])
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        return argv
