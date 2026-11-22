from __future__ import annotations

import asyncio
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest

from model_runtime import engine_manager
from model_runtime import openai_gateway
from model_runtime.benchmark import InferenceBenchmark
from model_runtime.inference_observability import InferenceObservability
from model_runtime.remote_nodes import RemoteNodeManager
from model_runtime.runtime_installer import RuntimeInstaller
from model_runtime.runtime_recipes import (
    RUNTIME_RECIPE_LAUNCH_JOB,
    RuntimeRecipeManager,
)
from work_fabric.service import WorkService
from ws_config import _local_models_msg


async def _broadcast(_message):
    return None


def test_model_scanner_reads_only_user_drop_folder(tmp_path):
    data_root = tmp_path / "data"
    filename = "family-v1-7b-Q4.gguf"
    projector = "mmproj-family-v1-7b-f16.gguf"
    for repo in ("repo-a", "repo-b"):
        folder = data_root / "models" / "user" / repo
        folder.mkdir(parents=True)
        (folder / filename).write_bytes(repo.encode("utf-8"))
        (folder / projector).write_bytes(b"projector")
    ignored = data_root / "models" / "base"
    ignored.mkdir(parents=True)
    (ignored / "bundled.gguf").write_bytes(b"bundled")

    rows = engine_manager.scan_models(str(data_root))

    assert [row["name"] for row in rows] == [
        f"repo-a/{filename}", f"repo-b/{filename}",
    ]
    assert len({row["path"] for row in rows}) == 2
    assert all(Path(row["mmproj"]).parent == Path(row["path"]).parent for row in rows)
    assert [row["size_bytes"] for row in rows] == [6, 6]


def test_model_scanner_rejects_link_escape(tmp_path):
    folder = tmp_path / "models" / "user"
    outside = tmp_path / "outside.gguf"
    folder.mkdir(parents=True)
    outside.write_bytes(b"outside")
    link = folder / "linked.gguf"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    assert engine_manager.scan_models(str(tmp_path)) == []


def test_local_models_snapshot_uses_full_path_identity(tmp_path):
    current = str(tmp_path / "models" / "user" / "repo-b" / "model.gguf")
    items = [
        {"name": "repo-a/model.gguf", "path": str(
            tmp_path / "models" / "user" / "repo-a" / "model.gguf")},
        {"name": "repo-b/model.gguf", "path": current},
    ]
    runtime = SimpleNamespace(models=SimpleNamespace(scan_models=lambda: items))
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        router=SimpleNamespace(cfg={"local": {"model": current}}),
        require_runtime=lambda: runtime,
        _local_model_switching=False,
    )

    message = _local_models_msg(server)

    assert message["current"] == current
    assert message["current_name"] == "model.gguf"
    assert message["folder"] == str(tmp_path / "models" / "user")
    assert message["items"] == items


def test_local_models_snapshot_canonicalizes_unique_relative_path(tmp_path):
    first = str(tmp_path / "models" / "user" / "repo-a" / "model.gguf")
    second = str(tmp_path / "models" / "user" / "repo-b" / "other.gguf")
    items = [
        {"name": "repo-a/model.gguf", "path": first},
        {"name": "repo-b/other.gguf", "path": second},
    ]
    runtime = SimpleNamespace(models=SimpleNamespace(scan_models=lambda: items))
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        router=SimpleNamespace(cfg={
            "local": {"model": "MODELS/USER/REPO-A/MODEL.GGUF"},
        }),
        require_runtime=lambda: runtime,
        _local_model_switching=False,
    )

    assert _local_models_msg(server)["current"] == first


def test_local_models_snapshot_does_not_guess_ambiguous_basename(tmp_path):
    items = [
        {"name": "repo-a/model.gguf", "path": str(
            tmp_path / "models" / "user" / "repo-a" / "model.gguf")},
        {"name": "repo-b/model.gguf", "path": str(
            tmp_path / "models" / "user" / "repo-b" / "model.gguf")},
    ]
    runtime = SimpleNamespace(models=SimpleNamespace(scan_models=lambda: items))
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        router=SimpleNamespace(cfg={"local": {"model": "model.gguf"}}),
        require_runtime=lambda: runtime,
        _local_model_switching=False,
    )

    assert _local_models_msg(server)["current"] == "model.gguf"


def test_remote_node_inventory_is_persistent_and_normalizes_v1(tmp_path):
    manager = RemoteNodeManager(str(tmp_path))
    row = manager.save({
        "name": "Workstation",
        "base_url": "http://10.0.0.8:8000/v1/",
    })
    assert row["base_url"] == "http://10.0.0.8:8000"

    loaded = RemoteNodeManager(str(tmp_path))
    assert loaded.get(row["id"])["name"] == "Workstation"


def test_inference_control_jobs_keep_identity_across_service_restart(
    tmp_path, monkeypatch,
):
    work_path = str(tmp_path / "work.sqlite3")
    first_work = WorkService.open(work_path)
    installer = RuntimeInstaller(str(tmp_path), _broadcast)
    recipes = RuntimeRecipeManager(
        str(tmp_path), installer, SimpleNamespace(), _broadcast,
    )
    installer.bind_work(first_work)
    recipes.bind_work(first_work)
    target = {
        "id": "managed-native:vllm",
        "runtime_id": "vllm",
        "label": "Managed vLLM",
        "manageable": True,
    }
    monkeypatch.setattr(installer, "target", lambda _target_id: dict(target))

    install_job = installer.start(
        "vllm", target_id="managed-native:vllm",
    )
    recipe = recipes.save({
        "name": "Remote model",
        "runtime_id": "openai_compatible",
        "model": "remote/model",
        "endpoint": "http://127.0.0.1:8123",
    })
    launch = recipes.start_launch(recipe["id"])

    second_work = WorkService.open(work_path)
    reopened_installer = RuntimeInstaller(str(tmp_path), _broadcast)
    reopened_recipes = RuntimeRecipeManager(
        str(tmp_path), reopened_installer, SimpleNamespace(), _broadcast,
    )
    reopened_installer.bind_work(second_work)
    reopened_recipes.bind_work(second_work)

    assert reopened_installer.jobs_snapshot()["items"][0]["id"] == install_job["id"]
    assert second_work.jobs.require(launch["job_id"]).kind == RUNTIME_RECIPE_LAUNCH_JOB


def test_llamacpp_runtime_download_is_a_work_job(tmp_path, monkeypatch):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    router = SimpleNamespace(
        engine=SimpleNamespace(binary=str(tmp_path / "packaged" / "llama-server.exe")),
        cfg={"local": {}},
    )
    installer = RuntimeInstaller(str(tmp_path), _broadcast, router=router)
    installer.bind_work(work)
    target = {
        "id": "managed-binary:llamacpp",
        "runtime_id": "llamacpp",
        "label": "Managed llama.cpp",
        "manageable": True,
        "tag": "b10679",
    }
    monkeypatch.setattr(installer, "target", lambda _target_id: dict(target))

    projected = installer.start(
        "llamacpp",
        target_id=target["id"],
        backend="cuda",
    )
    record = work.jobs.require(projected["id"])

    assert record.input_manifest["runtime_id"] == "llamacpp"
    assert record.input_manifest["backend"] == "cuda"
    assert record.input_manifest["tag"] == "b10679"


def test_packaged_llamacpp_does_not_impersonate_managed_install(tmp_path):
    name = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
    binary = tmp_path / "packaged" / "bin" / name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"packaged runtime")
    router = SimpleNamespace(
        engine=SimpleNamespace(binary=str(binary)),
        app_root=str(tmp_path / "packaged"),
        cfg={"local": {}},
    )
    installer = RuntimeInstaller(str(tmp_path / "data"), _broadcast, router=router)

    targets = {item["id"]: item for item in installer.discover_targets(force=True)}

    assert targets["managed-binary:llamacpp"]["installed"] is False
    assert targets["bundled-llamacpp"]["installed"] is True
    assert installer.local_runtime_status()["install_source"] == "bundled"


@pytest.mark.asyncio
async def test_remote_recipe_launch_attaches_after_controller_is_ready(
    tmp_path, monkeypatch,
):
    class Nodes:
        def __init__(self):
            self.launched = []

        async def launch_node(self, node, recipe_id):
            self.launched.append((node["id"], recipe_id))

        def get(self, node_id):
            return {"id": node_id, "base_url": "http://node.test:8000"}

        async def evict(self, _node_id):
            return None

    nodes = Nodes()
    manager = RuntimeRecipeManager(
        str(tmp_path), SimpleNamespace(data_dir=str(tmp_path)), nodes, _broadcast,
    )
    recipe = manager.save({
        "name": "Remote model", "runtime_id": "openai_compatible",
        "model": "org/model", "node_id": "node-1",
        "remote_recipe_id": "recipe-9", "endpoint": "http://node.test:8000",
    })
    attached = []

    async def ready(_endpoint, timeout, *, headers, model):
        assert timeout == 180.0
        assert headers == {}
        assert model == "org/model"
        return True

    async def attach(value):
        attached.append(value["endpoint"])

    monkeypatch.setattr(manager, "_wait_ready", ready)
    monkeypatch.setattr(manager, "_attach_recipe", attach)
    await manager._launch(recipe)

    assert nodes.launched == [("node-1", "recipe-9")]
    assert attached == ["http://node.test:8000"]
    assert manager.active_recipe_id == recipe["id"]


@pytest.mark.asyncio
async def test_llamacpp_recipe_keeps_discovered_vision_projector(tmp_path):
    model = tmp_path / "family-v1-7b-q4.gguf"
    projector = tmp_path / "mmproj-family-v1-7b-f16.gguf"
    model.write_bytes(b"model")
    projector.write_bytes(b"projector")
    launches = []

    class Models:
        async def restart_engine(self, model_path, mmproj_path=""):
            launches.append((model_path, mmproj_path))
            return "switched"

    manager = RuntimeRecipeManager(
        str(tmp_path), SimpleNamespace(data_dir=str(tmp_path)),
        SimpleNamespace(), _broadcast,
    )
    manager.bind_host(SimpleNamespace(
        router=SimpleNamespace(inference_runtime_id="llamacpp"),
        require_runtime=lambda: SimpleNamespace(models=Models()),
    ))

    await manager._launch_llamacpp({"model_path": str(model)})

    assert launches == [(str(model), str(projector))]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rolled_back", "failed", "superseded"])
async def test_unconfirmed_gguf_recipe_never_publishes_ready(tmp_path, outcome):
    old, candidate = tmp_path / "old.gguf", tmp_path / "candidate.gguf"
    old.touch()
    candidate.touch()
    events = []
    manager = RuntimeRecipeManager(str(tmp_path), SimpleNamespace(), SimpleNamespace(), _broadcast,
                                   event_sink=lambda *event: events.append(event))
    first = manager.save({"model_path": str(old), "name": "Original"})
    target = manager.save({"model_path": str(candidate), "name": "Candidate"})
    engine = SimpleNamespace(model=str(old), ready=outcome != "failed")
    async def restart(*_args):
        return outcome
    manager.bind_host(SimpleNamespace(
        router=SimpleNamespace(inference_runtime_id="llamacpp", engine=engine),
        require_runtime=lambda: SimpleNamespace(models=SimpleNamespace(restart_engine=restart))))
    manager.active_recipe_id = first["id"]
    with pytest.raises(RuntimeError, match=outcome):
        await manager._launch(target)
    assert not any("Runtime ready: Candidate" in str(event) for event in events)
    assert manager.active_recipe_id == (first["id"] if engine.ready else "")
    assert outcome in manager.last_error


@pytest.mark.asyncio
async def test_remote_launch_uses_node_auth_for_readiness_attachment_and_retryable_cleanup(tmp_path, monkeypatch):
    import httpx
    from llm_router import LLMRouter
    nodes = RemoteNodeManager(str(tmp_path))
    monkeypatch.setenv("VARIANT_TEST_NODE_KEY", "fixture-only-token")
    node = nodes.save({"base_url": "http://inference.test", "management_url": "http://controller.test",
                       "api_key_env": "VARIANT_TEST_NODE_KEY"})
    events = []
    allow_evict = True
    def respond(request):
        events.append((request.method, request.url.host, request.url.path, request.headers.get("authorization")))
        if request.headers.get("authorization") != "Bearer fixture-only-token":
            return httpx.Response(401)
        if request.url.path == "/evict" and not allow_evict:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**kw, transport=httpx.MockTransport(respond)))
    router = LLMRouter({"mode": "local"}, str(tmp_path), data_dir=str(tmp_path))
    manager = RuntimeRecipeManager(str(tmp_path), SimpleNamespace(), nodes, _broadcast)
    manager.bind_host(SimpleNamespace(router=router))
    recipe = manager.save({"runtime_id": "openai_compatible", "model": "model-a", "name": "Remote",
                           "remote_recipe_id": "remote-a", "node_id": node["id"]})
    wait_ready = manager._wait_ready
    begin_launch = manager._begin_remote_launch
    async def edit_settings_before_post(snapshot, recipe_id):
        nodes.save({**node, "management_url": "http://replacement.test", "base_url": "http://new-inference.test"})
        await begin_launch(snapshot, recipe_id)
    monkeypatch.setattr(manager, "_begin_remote_launch", edit_settings_before_post)
    async def short_probe(endpoint, timeout, **kw):
        return await wait_ready(endpoint, timeout=0.1, **kw)
    monkeypatch.setattr(manager, "_wait_ready", short_probe)
    await manager._launch(recipe)
    assert manager.active_recipe_id == recipe["id"]
    assert router.engine.api_key_env == "VARIANT_TEST_NODE_KEY" and router.engine.ready
    assert [(e[1], e[2]) for e in events] == [
        ("controller.test", "/launch/remote-a"), ("inference.test", "/v1/models"), ("inference.test", "/v1/models")]
    assert router.engine.request_headers["Authorization"] == "Bearer fixture-only-token"
    await manager.evict()
    assert events[-1][1:3] == ("controller.test", "/evict")

    # A confirmed controller launch that never becomes ready still owns cleanup.
    events.clear()
    nodes.save(node)
    monkeypatch.setattr(manager, "_begin_remote_launch", begin_launch)
    allow_evict = False
    async def not_ready(*args, **kwargs):
        return False
    monkeypatch.setattr(manager, "_wait_ready", not_ready)
    with pytest.raises(RuntimeError):
        await manager._launch(recipe)
    assert [e[2] for e in events] == ["/launch/remote-a", "/evict"]
    assert not manager.process_status()["ready"]
    nodes.save({**node, "management_url": "http://other.test"})
    allow_evict = True
    await manager.evict()
    assert events[-1][1:3] == ("controller.test", "/evict")
    assert "fixture-only-token" not in json.dumps(router.cfg)


@pytest.mark.asyncio
async def test_stopping_an_inflight_remote_launch_reaps_only_an_acknowledged_start(tmp_path):
    entered, acknowledge = asyncio.Event(), asyncio.Event()
    evictions = []
    node = {"id": "remote", "base_url": "http://fixture.test"}
    class Nodes:
        def get(self, node_id):
            return node
        async def launch_node(self, node, recipe_id):
            entered.set()
            await acknowledge.wait()
        async def evict_node(self, owner):
            evictions.append(owner)
    manager = RuntimeRecipeManager(str(tmp_path), SimpleNamespace(), Nodes(), _broadcast)
    recipe = manager.save({"runtime_id": "openai_compatible", "model": "model-a", "node_id": node["id"]})
    launch = asyncio.create_task(manager._launch(recipe))
    await entered.wait()
    launch.cancel()
    await asyncio.sleep(0)
    assert not launch.done() and not evictions
    acknowledge.set()
    with pytest.raises(asyncio.CancelledError):
        await launch
    assert evictions == [node] and not manager.process_status()["ready"]


@pytest.mark.asyncio
async def test_recipe_watcher_leaves_initial_launch_failure_to_launcher(tmp_path):
    class FailedProcess:
        returncode = 7

        async def wait(self):
            return self.returncode

    manager = RuntimeRecipeManager(
        str(tmp_path), SimpleNamespace(data_dir=str(tmp_path)),
        SimpleNamespace(), _broadcast,
    )
    process = FailedProcess()
    manager.process = process

    await manager._watch_process(process, "recipe-under-test")

    assert manager.process is process
    assert manager._failure_status("recipe-under-test")["failures"] == 0


def test_recipe_supervisor_gates_three_recent_failures(tmp_path):
    manager = RuntimeRecipeManager(
        str(tmp_path), SimpleNamespace(data_dir=str(tmp_path)),
        SimpleNamespace(), _broadcast,
    )
    recipe = manager.save({
        "name": "Crashy runtime", "runtime_id": "vllm", "model": "org/model",
    })

    for _ in range(3):
        manager._record_failure(recipe["id"])

    status = manager.list()["items"][0]
    assert status["status"] == "error"
    assert status["crash_loop"] == {
        "failures": 3, "blocked": True, "window_s": 300,
    }
    with pytest.raises(RuntimeError, match="temporarily blocked"):
        manager.start_launch(recipe["id"])


@pytest.mark.asyncio
async def test_gateway_runtime_start_uses_shared_engine_lifecycle_gate():
    class Engine:
        ready = False
        proc = None

        def poll_process(self):
            return self.ready

        async def start(self):
            self.ready = True

    class Router:
        engine = Engine()

        @property
        def engine_ready(self):
            return self.engine.ready

    router = Router()
    assert await engine_manager.ensure_active_runtime(router) == "started"
    assert await engine_manager.ensure_active_runtime(router) == "already_ready"


def test_gateway_rejects_a_model_outside_the_attached_runtime():
    engine = SimpleNamespace(api_model="served/model")
    host = SimpleNamespace(
        router=SimpleNamespace(engine=engine, model_name="configured/model"),
    )

    rejected = openai_gateway._model_error(host, engine, "other/model")

    assert rejected is not None
    assert rejected.status_code == 400
    assert openai_gateway._model(host, engine) == "served/model"


def test_operations_history_aggregates_requests_and_resource_peaks(tmp_path):
    observability = InferenceObservability(str(tmp_path))
    observability.observe_inference({
        "request_id": "REQ1", "status": "complete", "ts": 1_900_000_000,
        "started_at": 1_899_999_999, "runtime_id": "vllm", "model": "org/model",
        "prompt_tokens": 100, "processed_prompt_tokens": 75,
        "cached_prompt_tokens": 25, "output_tokens": 50, "ttft_ms": 120,
        "decode_tps": 40, "time_to_last_token_s": 2,
    })
    observability._requests[-1]["finished_at"] = __import__("time").time()
    observability.observe_hardware({
        "ts": __import__("time").time(),
        "gpus": [{
            "index": 0, "name": "GPU", "utilization_pct": 70,
            "vram_used_mb": 4096,
        }],
        "ram_total_mb": 16000, "ram_available_mb": 8000,
    })

    snapshot = observability.snapshot()
    assert snapshot["windows"]["5m"]["requests"] == 1
    assert snapshot["windows"]["5m"]["cache_hit_pct"] == 25.0
    assert snapshot["windows"]["5m"]["avg_decode_tps"] == 40.0
    assert snapshot["resource_peaks"]["vram_used_mb"] == 4096
    assert snapshot["runtime_mix"] == [{"runtime_id": "vllm", "requests": 1}]


def test_benchmark_recommendations_use_latest_result_per_runtime_model(tmp_path):
    manager = InferenceBenchmark(str(tmp_path), _broadcast, lambda: {})
    manager.items = [
        {"created_at": 1, "runtime_id": "vllm", "model": "a", "decode_tps_avg": 10, "ttft_ms_avg": 100},
        {"created_at": 2, "runtime_id": "vllm", "model": "a", "decode_tps_avg": 20, "ttft_ms_avg": 90},
        {"created_at": 3, "runtime_id": "sglang", "model": "b", "decode_tps_avg": 30, "ttft_ms_avg": 120},
    ]

    ranked = manager.recommendations()
    assert [row["model"] for row in ranked] == ["b", "a"]
    assert ranked[1]["decode_tps"] == 20
