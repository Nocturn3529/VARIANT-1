"""WebSocket control plane for the local inference platform."""

from __future__ import annotations


def _platform(srv) -> dict:
    return {
        "type": "inference:platform",
        "targets": srv.runtime_installer.discover_targets(),
        "install_jobs": srv.runtime_installer.jobs_snapshot().get("items", []),
        "local_runtime": srv.runtime_installer.local_runtime_status(),
        "recipes": srv.runtime_recipes.list(),
        "nodes": srv.remote_nodes.list(),
        "benchmarks": srv.inference_benchmarks.snapshot(),
        "gateway": {
            "enabled": True,
            "base_path": "/v1",
            "models_path": "/v1/models",
            "chat_path": "/v1/chat/completions",
            "follows_active_runtime": True,
        },
    }


def _operations(srv) -> dict:
    operations = srv.inference_observability.snapshot()
    return {
        **operations,
        "runtime": srv.runtime_recipes.process_status(),
        "install_jobs": srv.runtime_installer.jobs_snapshot().get("items", []),
        "benchmark_job": srv.inference_benchmarks.snapshot().get("job"),
        "benchmark_recommendations": srv.inference_benchmarks.recommendations(),
        "cloud_usage": srv.router.usage_snapshot(),
        "model_usage": srv.router.model_usage_snapshot(days=30),
    }


def register(on):
    @on("inference:platform:get")
    async def _get_platform(srv, websocket, session, msg):
        await websocket.send_json(_platform(srv))

    @on("inference:operations")
    async def _get_operations(srv, websocket, session, msg):
        await websocket.send_json(_operations(srv))

    @on("inference:doctor")
    async def _doctor(srv, websocket, session, msg):
        await websocket.send_json(srv.runtime_installer.doctor(str(msg.get("runtime_id") or "")))

    @on("inference:install")
    async def _install(srv, websocket, session, msg):
        runtime_id = str(msg.get("runtime_id") or "")
        operation = str(msg.get("operation") or "install")
        target_id = str(msg.get("target_id") or "")
        backend = str(msg.get("backend") or "auto")
        if operation == "uninstall":
            selected = srv.runtime_installer.target(target_id) if target_id else srv.runtime_installer.recommended_target(runtime_id, install=True)
            target_id = str((selected or {}).get("id") or target_id)
            active = srv.runtime_recipes.get(srv.runtime_recipes.active_recipe_id)
            if active and str(active.get("target_id") or "") == target_id:
                raise RuntimeError("evict the active recipe before uninstalling its runtime target")
        job = srv.runtime_installer.start(
            runtime_id,
            operation=operation,
            target_id=target_id,
            backend=backend,
        )
        await websocket.send_json({"type": "inference:install:job", **job})
        await websocket.send_json(_platform(srv))

    @on("inference:install:cancel")
    async def _cancel_install(srv, websocket, session, msg):
        cancelled = await srv.runtime_installer.cancel(str(msg.get("id") or msg.get("job_id") or ""))
        await websocket.send_json({"type": "inference:install:cancelled", "cancelled": cancelled})
        await websocket.send_json(_platform(srv))

    @on("inference:install:jobs")
    async def _install_jobs(srv, websocket, session, msg):
        await websocket.send_json(srv.runtime_installer.jobs_snapshot())

    @on("inference:recipes")
    async def _recipes(srv, websocket, session, msg):
        await websocket.send_json(srv.runtime_recipes.list())

    @on("inference:recipe:save")
    async def _save_recipe(srv, websocket, session, msg):
        value = msg.get("recipe") if isinstance(msg.get("recipe"), dict) else msg
        row = srv.runtime_recipes.save(value)
        await websocket.send_json({"type": "inference:recipe:saved", "recipe": row})
        await websocket.send_json(srv.runtime_recipes.list())

    @on("inference:recipe:remove")
    async def _remove_recipe(srv, websocket, session, msg):
        removed = srv.runtime_recipes.remove(str(msg.get("id") or ""))
        await websocket.send_json({"type": "inference:recipe:removed", "removed": removed})
        await websocket.send_json(srv.runtime_recipes.list())

    @on("inference:recipe:launch")
    async def _launch_recipe(srv, websocket, session, msg):
        status = srv.runtime_recipes.start_launch(str(msg.get("id") or ""))
        await websocket.send_json({"type": "inference:runtime:process", **status})

    @on("inference:recipe:cancel")
    async def _cancel_recipe(srv, websocket, session, msg):
        cancelled = await srv.runtime_recipes.cancel_launch()
        await websocket.send_json({"type": "inference:recipe:cancelled", "cancelled": cancelled})
        await websocket.send_json(srv.runtime_recipes.list())

    @on("inference:runtime:evict")
    async def _evict(srv, websocket, session, msg):
        await srv.runtime_recipes.evict()
        await websocket.send_json(srv.runtime_recipes.list())

    @on("inference:runtime:restart")
    async def _restart(srv, websocket, session, msg):
        await websocket.send_json({"type": "inference:runtime:process", **(await srv.runtime_recipes.restart())})

    @on("inference:logs")
    async def _logs(srv, websocket, session, msg):
        await websocket.send_json(srv.runtime_recipes.logs_snapshot(int(msg.get("limit") or 200)))

    @on("inference:nodes")
    async def _nodes(srv, websocket, session, msg):
        await websocket.send_json(srv.remote_nodes.list())

    @on("inference:node:save")
    async def _save_node(srv, websocket, session, msg):
        value = msg.get("node") if isinstance(msg.get("node"), dict) else msg
        row = srv.remote_nodes.save(value)
        await websocket.send_json({"type": "inference:node:saved", "node": row})
        await websocket.send_json(srv.remote_nodes.list())

    @on("inference:node:remove")
    async def _remove_node(srv, websocket, session, msg):
        removed = srv.remote_nodes.remove(str(msg.get("id") or ""))
        await websocket.send_json({"type": "inference:node:removed", "removed": removed})
        await websocket.send_json(srv.remote_nodes.list())

    @on("inference:node:probe")
    async def _probe_node(srv, websocket, session, msg):
        await websocket.send_json({"type": "inference:node:probe", **(await srv.remote_nodes.probe(str(msg.get("id") or "")))})
        await websocket.send_json(srv.remote_nodes.list())

    @on("inference:node:recipe:import")
    async def _import_node_recipe(srv, websocket, session, msg):
        node_id = str(msg.get("node_id") or "")
        node = srv.remote_nodes.get(node_id)
        source = msg.get("recipe") if isinstance(msg.get("recipe"), dict) else {}
        if not node or not source:
            raise ValueError("remote node and recipe are required")
        remote_id = str(source.get("id") or source.get("recipe_id") or "").strip()
        if not remote_id:
            raise ValueError("remote recipe has no ID")
        model = str(source.get("model") or source.get("model_id") or source.get("model_path") or remote_id)
        row = srv.runtime_recipes.save({
            "name": f"{node['name']} · {source.get('name') or remote_id}",
            "runtime_id": "openai_compatible",
            "model": model,
            "model_format": source.get("model_format") or source.get("format") or "remote",
            "node_id": node_id,
            "remote_recipe_id": remote_id,
            "endpoint": node["base_url"],
            "context_size": source.get("context_size") or 32768,
        })
        await websocket.send_json({"type": "inference:recipe:saved", "recipe": row})
        await websocket.send_json(srv.runtime_recipes.list())

    @on("inference:benchmark:start")
    async def _start_benchmark(srv, websocket, session, msg):
        engine = srv.router.engine
        job = srv.inference_benchmarks.start(
            str(msg.get("endpoint") or getattr(engine, "base_url", "")),
            str(msg.get("model") or getattr(engine, "api_model", "") or srv.router.model_name),
            runtime_id=str(msg.get("runtime_id") or srv.router.inference_runtime_id),
            rounds=int(msg.get("rounds") or 3),
            prompt=str(msg.get("prompt") or "Explain why local inference is useful in three concise sentences."),
        )
        await websocket.send_json({"type": "inference:benchmark:job", **job})

    @on("inference:benchmark:cancel")
    async def _cancel_benchmark(srv, websocket, session, msg):
        cancelled = await srv.inference_benchmarks.cancel()
        await websocket.send_json({"type": "inference:benchmark:cancelled", "cancelled": cancelled})

    @on("inference:benchmarks")
    async def _benchmarks(srv, websocket, session, msg):
        await websocket.send_json(srv.inference_benchmarks.snapshot())

__all__ = ["register"]
