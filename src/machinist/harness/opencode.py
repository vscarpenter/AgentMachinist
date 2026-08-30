import re
import subprocess

from machinist.harness.base import Harness, HarnessCapabilities


class OpenCode(Harness):
    name = "opencode"
    default_command = "opencode"
    capabilities = HarnessCapabilities("advisory")

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
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv

    def implement_argv(self, prompt: str) -> list[str]:
        argv = [self.command, "run"]
        if self.config.model:
            argv.extend(["--model", self.config.model])
        if self.config.extra_args:
            argv.extend(self.config.extra_args)
        argv.append(prompt)
        return argv
