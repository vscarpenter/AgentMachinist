"""Codex profile used only inside the disposable background container boundary.

Codex 0.151's Linux sandbox uses Bubblewrap, which needs namespace and mount
operations restricted by Docker's default seccomp/capability profile. Leave that
Docker boundary intact: this adapter explicitly relies on the outer container,
including read-only Workshop mounts for Spec/Review. It is not registered as a
host Harness and cannot be constructed with a generic subprocess runner.
"""

from __future__ import annotations

from functools import partial

from machinist.background_runtime import ContainerRuntime
from machinist.config import HarnessConfig, harness_identifier
from machinist.harness.base import Harness, HarnessCapabilities, HarnessError
from machinist.harness.codex import Codex


def get_background_harness(
    config: HarnessConfig,
    *,
    runtime: ContainerRuntime,
    phase: str,
) -> Harness:
    """Bind the pilot's Codex profile to an explicit outer execution boundary."""
    if not isinstance(runtime, ContainerRuntime):
        raise HarnessError("background Harness requires a ContainerRuntime")
    if harness_identifier(config.name) != "codex":
        raise HarnessError("the background pilot currently supports Codex only")
    if phase not in {"spec", "execute", "review"}:
        raise HarnessError("background Harness requires an explicit supported Phase")
    return _ContainerCodex(config, runtime=runtime, phase=phase)


class _ContainerCodex(Codex):
    capabilities = HarnessCapabilities("container-read-only-mount")

    def __init__(self, config: HarnessConfig, *, runtime: ContainerRuntime, phase: str):
        super().__init__(
            config,
            runner=partial(
                runtime.harness_runner,
                read_only_workshop=phase != "execute",
            ),
        )
        # The container runner is a supervisor adapter, not a generic injected
        # subprocess function. Keep signals on the main thread and forward the
        # current cancellation callback, including a shorter repair deadline.
        self._uses_supervisor = True
        self._phase = phase

    def spec_argv(self, prompt: str) -> list[str]:
        return self._container_argv(prompt, "spec")

    def implement_argv(self, prompt: str) -> list[str]:
        return self._container_argv(prompt, "execute")

    def review_argv(self, prompt: str) -> list[str]:
        return self._container_argv(prompt, "review")

    def _container_argv(self, prompt: str, phase: str) -> list[str]:
        if phase != self._phase:
            raise HarnessError(
                "background Harness Phase does not match its mount policy"
            )
        # CLI contract checked against the pinned rust-v0.151.0 sources:
        # utils/cli/src/shared_options.rs and sandbox_mode_cli_arg.rs.
        return [
            self.command,
            "exec",
            "--sandbox",
            "danger-full-access",
            "-c",
            'approval_policy="never"',
            "--ephemeral",
            *self._passthrough_argv(),
            prompt,
        ]
