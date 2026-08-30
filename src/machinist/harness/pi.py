import json
import subprocess

from machinist.harness.base import (
    Harness,
    HarnessCapabilities,
    HarnessCIProfile,
    HarnessDescriptor,
)


class Pi(Harness):
    name = "pi"
    default_command = "pi"
    capabilities = HarnessCapabilities("cli-enforced")
    descriptor = HarnessDescriptor(
        contract_version=1,
        display_name="Pi",
        documentation_url="https://github.com/badlogic/pi-mono",
        phases=frozenset({"spec", "execute", "review"}),
        ci_spec=HarnessCIProfile(
            install_argv=(
                "npm",
                "install",
                "-g",
                "@mariozechner/pi-coding-agent@0.73.1",
            ),
            secret_env="GEMINI_API_KEY",
        ),
    )

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
