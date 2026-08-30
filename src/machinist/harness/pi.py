import json
import subprocess

from machinist.harness.base import Harness, HarnessCapabilities


class Pi(Harness):
    name = "pi"
    default_command = "pi"
    capabilities = HarnessCapabilities("cli-enforced")

    def authentication_argv(self) -> list[str]:
        selector = (
            ["--model", self.config.model]
            if self.config.model
            else ["--provider", "google"]
        )
        return [
            self.command,
            "auth",
            "check",
            *selector,
            "--json",
            "--no-refresh",
        ]

    def authentication_ready(self, result: subprocess.CompletedProcess) -> bool:
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout or "")
        except (json.JSONDecodeError, TypeError):
            return False
        return payload.get("status") == "ready"

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
        argv = [self.command, "-p", "--no-session"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv
