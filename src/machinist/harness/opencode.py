import re
import subprocess

from machinist.harness.base import (
    Harness,
    HarnessCapabilities,
    HarnessCIProfile,
    HarnessDescriptor,
)


class OpenCode(Harness):
    name = "opencode"
    default_command = "opencode"
    capabilities = HarnessCapabilities("advisory")
    descriptor = HarnessDescriptor(
        contract_version=1,
        display_name="OpenCode",
        documentation_url="https://opencode.ai/docs/",
        phases=frozenset({"spec", "execute", "review"}),
        ci_spec=HarnessCIProfile(
            install_argv=("npm", "install", "-g", "opencode-ai@1.18.25"),
            secret_env="ANTHROPIC_API_KEY",
        ),
    )

    def authentication_argv(self) -> list[str]:
        return [self.command, "auth", "list", "--pure"]

    def authentication_ready(self, result: subprocess.CompletedProcess) -> bool:
        if result.returncode != 0:
            return False
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        return (
            re.search(
                r"\b[1-9][0-9]*\s+(?:credentials|environment variables)\b",
                output,
                re.IGNORECASE,
            )
            is not None
        )

    def spec_argv(self, prompt: str) -> list[str]:
        # The built-in "plan" agent cannot edit files.
        argv = [self.command, "run", "--pure", "--agent", "plan"]
        argv.extend(self._passthrough_argv())
        argv.append(prompt)
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "run"]
        argv.extend(self._passthrough_argv())
        argv.append(prompt)
        return argv
