import copy
import sqlite3

from artifacts import ContentAddressedArtifactStore
from session_catalog.catalog import (
    CatalogRepository,
    build_catalog_document,
)
from session_runtime.models import RuntimeIdentity
from session_runtime.repository import SessionRuntimeRepository
from tools import ToolRegistry


def test_structural_rebase_cas_discards_old_overlays_but_preserves_mutation_mode(
    tmp_path,
):
    path = str(tmp_path / "session.sqlite3")
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    catalogs = CatalogRepository(path, artifacts)
    first_document = build_catalog_document(ToolRegistry())
    first_release = catalogs.publish(first_document)
    second_document = copy.deepcopy(first_document)
    second_document["source_digest"] = "structural-test-release"
    second_document["categories"][0]["summary"] += " structurally revised"
    second_release = catalogs.publish(second_document)

    runtimes = SessionRuntimeRepository(path)
    record = runtimes.ensure_runtime(
        "chat-structural",
        RuntimeIdentity(catalog_release_id=first_release, mount_revision=7),
    )
    record = runtimes.mutation_authority_cas(
        record.chat_id, True, actor="test"
    )
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE mutation_draft(chat_id TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO mutation_draft(chat_id) VALUES (?)", (record.chat_id,)
        )

    result = catalogs.structural_rebase(
        record.chat_id,
        expected_release_id=first_release,
        target_release_id=second_release,
        expected_runtime_version=record.version,
        environment_digest="env-second",
    )

    reopened = runtimes.get_runtime(record.chat_id)
    assert result["changed"] is True
    assert reopened.identity.catalog_release_id == second_release
    assert reopened.identity.selected_category_id == ""
    assert reopened.identity.overlay_revision == 0
    assert reopened.identity.environment_digest == "env-second"
    assert reopened.kernel_generation == 1
    assert reopened.mutation_write_enabled is True
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM mutation_draft WHERE chat_id=?",
            (record.chat_id,),
        ).fetchone()[0] == 0
