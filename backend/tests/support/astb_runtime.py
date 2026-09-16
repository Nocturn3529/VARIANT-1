"""Small explicit ASTB runtime owners for dependency-focused unit tests."""

from types import SimpleNamespace

from session_runtime import RuntimeIdentity


class StaticRuntimeRegistry:
    def __init__(self, release_id: str = "astb.test.release.v1") -> None:
        self.release_id = str(release_id)

    def ensure_runtime(self, chat_id: str):
        return SimpleNamespace(
            chat_id=str(chat_id),
            identity=RuntimeIdentity(
                catalog_release_id=self.release_id,
                environment_digest="test-environment",
                mount_revision=1,
            ),
            kernel_generation=1,
        )


__all__ = ["StaticRuntimeRegistry"]
