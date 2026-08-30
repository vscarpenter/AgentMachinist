import subprocess

from machinist.harness.base import (
    Harness,
    HarnessCapabilities,
    HarnessCIProfile,
    HarnessDescriptor,
)


class Codex(Harness):
    name = "codex"
    default_command = "codex"
    capabilities = HarnessCapabilities("cli-enforced")
    descriptor = HarnessDescriptor(
        contract_version=1,
        display_name="Codex",
        documentation_url="https://developers.openai.com/codex/",
        phases=frozenset({"spec", "execute", "review"}),
        ci_spec=HarnessCIProfile(
            install_argv=("npm", "install", "-g", "@openai/codex@0.151.0"),
            secret_env="OPENAI_API_KEY",
        ),
    )

    def authentication_argv(self) -> list[str]:
        return [self.command, "login", "status"]

    def authentication_ready(self, result: subprocess.CompletedProcess) -> bool:
        output = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
        return result.returncode == 0 and "logged in" in output

    def spec_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "exec", "--sandbox", "read-only", "--ephemeral"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        # Codex 0.151 removed the legacy `--full-auto` shorthand from
        # `codex exec`. Spell out the non-interactive policy so the adapter's
        # interface remains stable across that CLI change: writes stay inside
        # the Workshop and approval prompts fail closed instead of hanging a
        # headless Task Run.
        argv = [
            self.command,
            "exec",
            "--sandbox",
            "workspace-write",
            "-c",
            'approval_policy="never"',
            "--ephemeral",
        ]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv
