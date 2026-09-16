"""Dependency-light capsule protocol and serializer registry contracts.

The host validates capsule manifests against a pinned kernel runtime profile;
it must not import worker codecs or probe packages installed in Variant1Backend.
The worker supplies the same document using its actually installed versions.
"""

from __future__ import annotations

from typing import Any, Mapping


WORKER_CAPSULE_SCHEMA = "variant1.kernel-capsule-worker.v1"
WORKER_NAMESPACE_SCHEMA = "variant1.kernel-namespace-worker.v1"
WORKER_CAPTURE_REQUEST_SCHEMA = "variant1.kernel-capsule-capture-request.v1"
SERIALIZER_REGISTRY_SCHEMA = "variant1.kernel-serializer-registry.v1"


def serializer_registry_document(
    package_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project exact package versions into the stable codec registry."""

    packages = {
        str(name): str(version or "")
        for name, version in dict(package_versions or {}).items()
    }
    numpy_version = packages.get("numpy", "")
    pandas_version = packages.get("pandas", "")
    pyarrow_version = packages.get("pyarrow", "")
    safetensors_version = packages.get("safetensors", "")
    return {
        "schema": SERIALIZER_REGISTRY_SCHEMA,
        "serializers": [
            {
                "id": "arrow.ipc.v1",
                "revision": 1,
                "portable": True,
                "available": bool(pyarrow_version),
                "media_type": "application/vnd.apache.arrow.file",
                "packages": {"pyarrow": pyarrow_version},
            },
            {
                "id": "bytes.v1",
                "revision": 1,
                "portable": True,
                "available": True,
                "media_type": "application/octet-stream",
                "packages": {},
            },
            {
                "id": "dataclass.fields.v1",
                "revision": 1,
                "portable": False,
                "available": True,
                "media_type": "application/json",
                "packages": {},
            },
            {
                "id": "json.strict.v1",
                "revision": 1,
                "portable": True,
                "available": True,
                "media_type": "application/json",
                "packages": {},
            },
            {
                "id": "numpy.npy.v1",
                "revision": 1,
                "portable": True,
                "available": bool(numpy_version),
                "media_type": "application/x-npy",
                "packages": {"numpy": numpy_version},
            },
            {
                "id": "pandas.arrow.v1",
                "revision": 1,
                "portable": True,
                "available": bool(pandas_version and pyarrow_version),
                "media_type": "application/vnd.apache.arrow.file",
                "packages": {
                    "pandas": pandas_version,
                    "pyarrow": pyarrow_version,
                },
            },
            {
                "id": "safetensors.numpy.v1",
                "revision": 1,
                "portable": True,
                "available": bool(numpy_version and safetensors_version),
                "media_type": "application/x-safetensors",
                "packages": {
                    "numpy": numpy_version,
                    "safetensors": safetensors_version,
                },
            },
            {
                "id": "text.utf8.v1",
                "revision": 1,
                "portable": True,
                "available": True,
                "media_type": "text/plain; charset=utf-8",
                "packages": {},
            },
            {
                "id": "tuple.tree.v1",
                "revision": 1,
                "portable": True,
                "available": True,
                "media_type": "application/json",
                "packages": {},
            },
        ],
    }


__all__ = [
    "SERIALIZER_REGISTRY_SCHEMA",
    "WORKER_CAPSULE_SCHEMA",
    "WORKER_CAPTURE_REQUEST_SCHEMA",
    "WORKER_NAMESPACE_SCHEMA",
    "serializer_registry_document",
]
