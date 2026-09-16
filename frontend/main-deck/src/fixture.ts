type FixtureMessage = Readonly<{
  type: string;
  [key: string]: unknown;
}>;

const now = Date.now() / 1000;

type UsageFixtureModel = Readonly<{
  key: string;
  model: string;
  provider: string;
  providerName: string;
  runtimeId?: string;
  weight: number;
  costPerMillion: number;
}>;

const usageFixtureModels: UsageFixtureModel[] = [
  {key: "local:qwen3.5-14b", model: "Qwen3.5 14B", provider: "local", providerName: "Local", runtimeId: "llama_cpp", weight: .32, costPerMillion: 0},
  {key: "local:deepseek-r1-7b", model: "DeepSeek R1 7B", provider: "local", providerName: "Local", runtimeId: "llama_cpp", weight: .20, costPerMillion: 0},
  {key: "local:gemma-3-12b", model: "Gemma 3 12B", provider: "local", providerName: "Local", runtimeId: "openai_compatible", weight: .13, costPerMillion: 0},
  {key: "openai:gpt-5.4", model: "gpt-5.4", provider: "openai", providerName: "OpenAI", weight: .16, costPerMillion: 6.2},
  {key: "anthropic:claude-sonnet-4-6", model: "claude-sonnet-4-6", provider: "anthropic", providerName: "Anthropic", weight: .11, costPerMillion: 8.4},
  {key: "gemini:gemini-3.5-flash", model: "gemini-3.5-flash", provider: "gemini", providerName: "Google AI", weight: .08, costPerMillion: 1.15},
];

function modelUsageFixture(): Record<string, unknown> {
  const end = new Date(now * 1000);
  end.setUTCHours(12, 0, 0, 0);
  const start = new Date(end);
  start.setUTCDate(end.getUTCDate() - 29);
  const daily = Array.from({length: 30}, (_, dayIndex) => {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + dayIndex);
    const wave = .60 + dayIndex * .013 + Math.sin(dayIndex * .64) * .16 + Math.sin(dayIndex * 1.7) * .06;
    const totalTokens = Math.max(480000, Math.round(3900000 * wave));
    const models = usageFixtureModels.map((definition, modelIndex) => {
      const drift = 1 + Math.sin((dayIndex + 1) * (modelIndex + 2) * .31) * .12;
      const tokens = Math.round(totalTokens * definition.weight * drift);
      const promptTokens = Math.round(tokens * .76);
      const completionTokens = tokens - promptTokens;
      const successfulRequests = Math.max(1, Math.round(tokens / 14500));
      const failedRequests = (dayIndex + modelIndex * 3) % 17 === 0 ? 1 : 0;
      const cancelledRequests = (dayIndex * 2 + modelIndex) % 29 === 0 ? 1 : 0;
      const requests = successfulRequests + failedRequests + cancelledRequests;
      const local = definition.provider === "local";
      const avgLatencyMs = local
        ? 820 + modelIndex * 145 + Math.sin(dayIndex * .51) * 110
        : 1260 + modelIndex * 120 + Math.sin(dayIndex * .37) * 160;
      const avgTtftMs = local
        ? 260 + modelIndex * 54 + Math.sin(dayIndex * .43) * 42
        : 510 + modelIndex * 38 + Math.sin(dayIndex * .29) * 65;
      const prefillTps = local ? 118 + modelIndex * 19 + Math.sin(dayIndex * .4) * 8 : null;
      const generationTps = local ? 42 + modelIndex * 7 + Math.sin(dayIndex * .33) * 4 : null;
      return {
        key: definition.key,
        model: definition.model,
        provider: definition.provider,
        provider_name: definition.providerName,
        runtime_id: definition.runtimeId || "",
        requests,
        prompt_tokens: promptTokens,
        completion_tokens: completionTokens,
        tokens,
        inference_time_s: Number((tokens / (local ? 63 : 91)).toFixed(3)),
        timed_calls: successfulRequests,
        successful_requests: successfulRequests,
        failed_requests: failedRequests,
        cancelled_requests: cancelledRequests,
        success_rate: Number((successfulRequests / (successfulRequests + failedRequests) * 100).toFixed(2)),
        avg_latency_ms: Number(avgLatencyMs.toFixed(2)),
        p50_latency_ms: Number((avgLatencyMs * .91).toFixed(2)),
        p95_latency_ms: Number((avgLatencyMs * 1.68).toFixed(2)),
        p99_latency_ms: Number((avgLatencyMs * 2.14).toFixed(2)),
        avg_ttft_ms: Number(avgTtftMs.toFixed(2)),
        p50_ttft_ms: Number((avgTtftMs * .92).toFixed(2)),
        p95_ttft_ms: Number((avgTtftMs * 1.55).toFixed(2)),
        p99_ttft_ms: Number((avgTtftMs * 1.92).toFixed(2)),
        prefill_tps: prefillTps === null ? null : Number(prefillTps.toFixed(2)),
        generation_tps: generationTps === null ? null : Number(generationTps.toFixed(2)),
        avg_tokens: Number((tokens / successfulRequests).toFixed(2)),
        avg_prompt_tokens: Number((promptTokens / successfulRequests).toFixed(2)),
        avg_completion_tokens: Number((completionTokens / successfulRequests).toFixed(2)),
        p50_tokens: Number((tokens / successfulRequests * 1.03).toFixed(2)),
        p95_tokens: Number((tokens / successfulRequests * 1.8).toFixed(2)),
        cached_prompt_tokens: local ? 0 : Math.round(promptTokens * .21),
        cache_hit_calls: local ? 0 : Math.max(1, Math.round(successfulRequests * .42)),
        cost_usd: Number((tokens / 1000000 * definition.costPerMillion).toFixed(6)),
        cost_status: local ? "unavailable" : "estimated",
        exact_calls: 0,
        estimated_calls: local ? 0 : successfulRequests,
        unpriced_calls: local ? successfulRequests : 0,
      };
    });
    return {date: date.toISOString().slice(0, 10), models};
  });
  const models = usageFixtureModels.map(definition => {
    const rows = daily.map(day => day.models.find(row => row.key === definition.key)!);
    const sum = (field: keyof typeof rows[number]) => rows.reduce((total, row) => total + Number(row[field] || 0), 0);
    const weighted = (field: keyof typeof rows[number]) => {
      const weight = sum("successful_requests");
      return weight ? rows.reduce((total, row) => (
        total + Number(row[field] || 0) * Number(row.successful_requests || 0)
      ), 0) / weight : 0;
    };
    const successful = sum("successful_requests");
    const failed = sum("failed_requests");
    return {
      key: definition.key,
      model: definition.model,
      provider: definition.provider,
      provider_name: definition.providerName,
      runtime_id: definition.runtimeId || "",
      requests: sum("requests"),
      successful_requests: successful,
      failed_requests: failed,
      cancelled_requests: sum("cancelled_requests"),
      success_rate: Number((successful / Math.max(1, successful + failed) * 100).toFixed(2)),
      prompt_tokens: sum("prompt_tokens"),
      completion_tokens: sum("completion_tokens"),
      tokens: sum("tokens"),
      inference_time_s: sum("inference_time_s"),
      timed_calls: sum("timed_calls"),
      avg_latency_ms: Number(weighted("avg_latency_ms").toFixed(2)),
      p50_latency_ms: Number(weighted("p50_latency_ms").toFixed(2)),
      p95_latency_ms: Number(weighted("p95_latency_ms").toFixed(2)),
      p99_latency_ms: Number(weighted("p99_latency_ms").toFixed(2)),
      avg_ttft_ms: Number(weighted("avg_ttft_ms").toFixed(2)),
      p50_ttft_ms: Number(weighted("p50_ttft_ms").toFixed(2)),
      p95_ttft_ms: Number(weighted("p95_ttft_ms").toFixed(2)),
      p99_ttft_ms: Number(weighted("p99_ttft_ms").toFixed(2)),
      prefill_tps: definition.provider === "local" ? Number(weighted("prefill_tps").toFixed(2)) : null,
      generation_tps: definition.provider === "local" ? Number(weighted("generation_tps").toFixed(2)) : null,
      avg_tokens: Number((sum("tokens") / Math.max(1, successful)).toFixed(2)),
      avg_prompt_tokens: Number((sum("prompt_tokens") / Math.max(1, successful)).toFixed(2)),
      avg_completion_tokens: Number((sum("completion_tokens") / Math.max(1, successful)).toFixed(2)),
      p50_tokens: Number(weighted("p50_tokens").toFixed(2)),
      p95_tokens: Number(weighted("p95_tokens").toFixed(2)),
      cached_prompt_tokens: sum("cached_prompt_tokens"),
      cache_hit_calls: sum("cache_hit_calls"),
      cost_usd: sum("cost_usd"),
      cost_status: definition.provider === "local" ? "unavailable" : "estimated",
      exact_calls: sum("exact_calls"),
      estimated_calls: sum("estimated_calls"),
      unpriced_calls: sum("unpriced_calls"),
    };
  });
  const totals = models.reduce((result, row) => {
    result.requests += row.requests;
    result.successful_requests += row.successful_requests;
    result.failed_requests += row.failed_requests;
    result.cancelled_requests += row.cancelled_requests;
    result.prompt_tokens += row.prompt_tokens;
    result.completion_tokens += row.completion_tokens;
    result.tokens += row.tokens;
    result.inference_time_s += row.inference_time_s;
    result.timed_calls += row.timed_calls;
    result.cached_prompt_tokens += row.cached_prompt_tokens;
    result.cache_hit_calls += row.cache_hit_calls;
    result.cost_usd += row.cost_usd;
    result.exact_calls += row.exact_calls;
    result.estimated_calls += row.estimated_calls;
    result.unpriced_calls += row.unpriced_calls;
    result[row.provider === "local" ? "local_requests" : "cloud_requests"] += row.requests;
    return result;
  }, {
    requests: 0,
    successful_requests: 0,
    failed_requests: 0,
    cancelled_requests: 0,
    prompt_tokens: 0,
    completion_tokens: 0,
    tokens: 0,
    inference_time_s: 0,
    timed_calls: 0,
    cached_prompt_tokens: 0,
    cache_hit_calls: 0,
    cost_usd: 0,
    exact_calls: 0,
    estimated_calls: 0,
    unpriced_calls: 0,
    local_requests: 0,
    cloud_requests: 0,
  });
  const totalSuccessful = Math.max(1, totals.successful_requests);
  const weightedModelMetric = (field: "avg_latency_ms" | "p50_latency_ms" | "p95_latency_ms" | "p99_latency_ms" | "avg_ttft_ms" | "p50_ttft_ms" | "p95_ttft_ms" | "p99_ttft_ms") => (
    models.reduce((sum, model) => sum + Number(model[field] || 0) * model.successful_requests, 0) / totalSuccessful
  );
  const sumDays = (rows: typeof daily) => rows.reduce((result, day) => {
    for (const model of day.models) {
      result.requests += model.requests;
      result.successful += model.successful_requests;
      result.failed += model.failed_requests;
      result.tokens += model.tokens;
    }
    return result;
  }, {requests: 0, successful: 0, failed: 0, tokens: 0});
  const thisWeek = sumDays(daily.slice(-7));
  const lastWeek = sumDays(daily.slice(-14, -7));
  const change = (current: number, previous: number) => previous
    ? Number(((current - previous) / previous * 100).toFixed(2))
    : null;
  const hourlyActivity = daily.slice(-7).map((day, dayIndex) => {
    const dayRequests = day.models.reduce((sum, model) => sum + model.requests, 0);
    const daySuccessful = day.models.reduce((sum, model) => sum + model.successful_requests, 0);
    const dayFailed = day.models.reduce((sum, model) => sum + model.failed_requests, 0);
    const dayCancelled = day.models.reduce((sum, model) => sum + model.cancelled_requests, 0);
    const dayTokens = day.models.reduce((sum, model) => sum + model.tokens, 0);
    const dayInference = day.models.reduce((sum, model) => sum + model.inference_time_s, 0);
    const dayCached = day.models.reduce((sum, model) => sum + model.cached_prompt_tokens, 0);
    const dayCost = day.models.reduce((sum, model) => sum + model.cost_usd, 0);
    const weights = Array.from({length: 24}, (_, hour) => (
      .18 + Math.max(0, Math.sin((hour - 7) / 24 * Math.PI * 2)) + Math.max(0, Math.sin((hour - 17) / 24 * Math.PI * 2)) * .7
    ));
    const weightTotal = weights.reduce((sum, value) => sum + value, 0);
    return {
      date: day.date,
      hours: weights.map((weight, hour) => {
        const share = weight / weightTotal;
        const requests = Math.round(dayRequests * share);
        return {
          hour,
          requests,
          successful: Math.min(requests, Math.round(daySuccessful * share)),
          failed: Math.min(requests, Math.round(dayFailed * share)),
          cancelled: Math.min(requests, Math.round(dayCancelled * share)),
          tokens: Math.round(dayTokens * share * (1 + Math.sin((hour + dayIndex) * .71) * .08)),
          inference_time_s: Number((dayInference * share).toFixed(3)),
          cached_prompt_tokens: Math.round(dayCached * share),
          cost_usd: Number((dayCost * share).toFixed(6)),
        };
      }),
    };
  });
  const hourlyPattern = Array.from({length: 24}, (_, hour) => hourlyActivity.reduce((result, day) => {
    const row = day.hours[hour];
    result.requests += row.requests;
    result.successful += row.successful;
    result.failed += row.failed;
    result.cancelled += row.cancelled;
    result.tokens += row.tokens;
    return result;
  }, {hour, requests: 0, successful: 0, failed: 0, cancelled: 0, tokens: 0}));
  const peakHours = [...hourlyPattern]
    .sort((left, right) => right.requests - left.requests || right.tokens - left.tokens)
    .slice(0, 3);
  const lastDay = hourlyActivity.at(-1)?.hours || [];
  const previousDay = hourlyActivity.at(-2)?.hours || [];
  const last24Requests = lastDay.reduce((sum, row) => sum + row.requests, 0);
  const previous24Requests = previousDay.reduce((sum, row) => sum + row.requests, 0);
  const last24Tokens = lastDay.reduce((sum, row) => sum + row.tokens, 0);
  return {
    days: 30,
    start: start.toISOString().slice(0, 10),
    end: end.toISOString().slice(0, 10),
    daily,
    models,
    totals: {
      ...totals,
      cost_status: "partial",
      success_rate: Number((totals.successful_requests / Math.max(1, totals.successful_requests + totals.failed_requests) * 100).toFixed(2)),
      avg_latency_ms: Number(weightedModelMetric("avg_latency_ms").toFixed(2)),
      p50_latency_ms: Number(weightedModelMetric("p50_latency_ms").toFixed(2)),
      p95_latency_ms: Number(weightedModelMetric("p95_latency_ms").toFixed(2)),
      p99_latency_ms: Number(weightedModelMetric("p99_latency_ms").toFixed(2)),
      avg_ttft_ms: Number(weightedModelMetric("avg_ttft_ms").toFixed(2)),
      p50_ttft_ms: Number(weightedModelMetric("p50_ttft_ms").toFixed(2)),
      p95_ttft_ms: Number(weightedModelMetric("p95_ttft_ms").toFixed(2)),
      p99_ttft_ms: Number(weightedModelMetric("p99_ttft_ms").toFixed(2)),
      avg_tokens: Number((totals.tokens / totalSuccessful).toFixed(2)),
      avg_prompt_tokens: Number((totals.prompt_tokens / totalSuccessful).toFixed(2)),
      avg_completion_tokens: Number((totals.completion_tokens / totalSuccessful).toFixed(2)),
      p50_tokens: Number((totals.tokens / totalSuccessful * 1.03).toFixed(2)),
      p95_tokens: Number((totals.tokens / totalSuccessful * 1.8).toFixed(2)),
    },
    week_over_week: {
      this_week: thisWeek,
      last_week: lastWeek,
      change_pct: {
        requests: change(thisWeek.requests, lastWeek.requests),
        tokens: change(thisWeek.tokens, lastWeek.tokens),
      },
    },
    recent_activity: {
      last_24h_requests: last24Requests,
      prev_24h_requests: previous24Requests,
      last_24h_tokens: last24Tokens,
      change_24h_pct: change(last24Requests, previous24Requests),
    },
    peak_hours: peakHours,
    hourly_pattern: hourlyPattern,
    hourly_activity: hourlyActivity,
    hourly_available: true,
    hour_timezone: "UTC",
    current_hour_utc: new Date(now * 1000).toISOString(),
  };
}

const messages: FixtureMessage[] = [
  {
    type: "config",
    mode: "local",
    provider: "xai",
    cloud_model: "grok-4.6",
    providers: [
      {
        name: "xai", display_name: "xAI", description: "Grok models through xAI.",
        api_style: "openai", auth_style: "bearer", base_url: "https://api.x.ai/v1",
        default_model: "grok-4.3", model: "grok-4.6", configured: true,
        credential_count: 1, supports_vision: true, supports_reasoning: true,
        auth_methods: ["oauth", "api_key"], credential_env_vars: ["XAI_API_KEY"],
        signup_url: "https://console.x.ai/", base_url_editable: true,
      },
      {
        name: "openai-codex", display_name: "OpenAI Codex (ChatGPT OAuth)",
        description: "ChatGPT subscription inference over the Codex Responses wire.",
        api_style: "openai", auth_style: "bearer", base_url: "https://chatgpt.com/backend-api/codex",
        default_model: "gpt-5.3-codex-spark", model: "gpt-5.6-sol", configured: true,
        credential_count: 0, supports_vision: true, supports_reasoning: true,
        auth_methods: ["oauth"], credential_env_vars: [], base_url_editable: false,
      },
      {
        name: "ollama", display_name: "Ollama Desktop Cloud",
        description: "Signed-in Ollama Desktop Cloud through its loopback helper.",
        api_style: "openai", auth_style: "optional", base_url: "http://127.0.0.1:11434/v1",
        default_model: "gemma4:31b-cloud", model: "gemma4:31b-cloud", configured: true,
        credential_count: 0, supports_vision: true, supports_reasoning: true,
        auth_methods: ["external"], credential_env_vars: [], base_url_editable: false,
      },
      {
        name: "hermes", display_name: "Hermes Agent (Nous OAuth)",
        description: "Signed-in Nous inference through Hermes Agent's loopback proxy.",
        api_style: "openai", auth_style: "optional", base_url: "http://127.0.0.1:8645/v1",
        default_model: "upstage/solar-pro4:free", model: "upstage/solar-pro4:free", configured: true,
        credential_count: 0, supports_vision: true, supports_reasoning: true,
        auth_methods: ["external"], credential_env_vars: [], base_url_editable: false,
      },
      {
        name: "openai", display_name: "OpenAI", description: "OpenAI API models.",
        api_style: "openai", auth_style: "bearer", base_url: "https://api.openai.com/v1",
        default_model: "gpt-4o", model: "gpt-5.4", configured: true,
        credential_count: 1, supports_vision: true, supports_reasoning: true,
        auth_methods: ["api_key"], credential_env_vars: ["OPENAI_API_KEY"],
        signup_url: "https://platform.openai.com/", base_url_editable: true,
      },
      {
        name: "anthropic", display_name: "Anthropic", description: "Claude Messages API.",
        api_style: "anthropic", auth_style: "x-api-key", base_url: "https://api.anthropic.com",
        default_model: "claude-sonnet-4-6", model: "claude-sonnet-4-6", configured: false,
        credential_count: 0, supports_vision: true, supports_reasoning: true,
        auth_methods: ["api_key"], credential_env_vars: ["ANTHROPIC_API_KEY"],
        signup_url: "https://console.anthropic.com/", base_url_editable: true,
      },
    ],
    credentials_by_provider: {
      xai: [{id: "xai-fixture", label: "Personal", priority: 0, enabled: true, base_url: "", source: "pool", status: "ready", failures: 0, last_error: ""}],
      openai: [{id: "openai-fixture", label: "Workbench", priority: 0, enabled: true, base_url: "", source: "pool", status: "ready", failures: 0, last_error: ""}],
    },
    oauth: {
      xai: true,
      xai_detail: {connected: true, auth_flow: "pkce", source: "oauth"},
      openai_codex: true,
      openai_codex_detail: {connected: true, auth_flow: "device_code", source: "oauth"},
    },
    custom_endpoints: [{
      id: "custom-studio", name: "Studio endpoint", base_url: "http://127.0.0.1:9001/v1",
      model: "studio-model", context_length: 32768, discover_models: true,
      models: ["studio-model", "studio-fast"], is_current: false,
      has_api_key: false, credential_count: 0,
    }],
    inference_platform: {
      targets: [], install_jobs: [], nodes: {items: []},
      local_runtime: {
        supported: true, tag: "b10679", recommended_backend: "cuda",
        installed: false, backend: "", version: "", binary: "",
        active_binary: "bin\\llama-server.exe", managed_active: false,
        update_available: false, installed_builds: [],
        runtime_root: "C:\\Users\\demo\\AppData\\Local\\VARIANT-1\\runtime\\llamacpp",
      },
      gateway: {enabled: true, base_path: "/v1", models_path: "/v1/models", chat_path: "/v1/chat/completions"},
    },
  },
  {
    type: "chat:sessions",
    items: [
      {
        id: "fixture-welcome",
        title: "Welcome to VARIANT-1",
        message_count: 2,
        updated_at: now,
        pinned: true,
      },
      {
        id: "fixture-trip-planning",
        title: "Trip planning",
        message_count: 4,
        updated_at: now - 3600,
      },
    ],
  },
  {
    type: "chat:session",
    session: {
      id: "fixture-welcome",
      title: "Welcome to VARIANT-1",
      messages: [
        {
          role: "user",
          text: "Give me a quick overview of this project.",
          ts: now - 30,
        },
        {
          role: "assistant",
          text: "The Main Deck is the shared workspace for chat, project scope, tools, and local runtime state.\n\n- **Chat** keeps the conversation readable.\n- **Steps** record the work behind each answer.\n- **Terminal, Review, and Browser** hold the detailed tool output.\n\nThe first improvement should be making those layers feel like one calm operator workflow.",
          ts: now - 25,
          steps: [
            {
              id: "fixture-step-think",
              kind: "thinking",
              label: "Mapped the Main Deck structure",
              detail: "Separated conversation, activity, and tool surfaces.",
              status: "done",
              ts: now - 29,
            },
            {
              id: "fixture-step-read",
              kind: "tool",
              tool: "read_file",
              label: "Read the chat components",
              detail: "ChatDestination, ChatMessageList, and ChatComposer",
              status: "ok",
              evidence: [{
                id: "fixture-evidence-chat",
                kind: "file",
                label: "ChatMessageList.tsx",
                value: "frontend/main-deck/src/chat/ChatMessageList.tsx",
              }],
              ts: now - 28,
            },
            {
              id: "fixture-step-review",
              kind: "tool",
              tool: "grep",
              label: "Audited the visual hierarchy",
              detail: "Typography, turn spacing, activity disclosure, and composer controls",
              status: "ok",
              ts: now - 27,
            },
            {
              id: "fixture-step-finish",
              kind: "step",
              label: "Prepared the interface recommendation",
              status: "done",
              ts: now - 26,
            },
            {
              id: "fixture-python-cell", call_id: "fixture-python-call", kind: "tool", tool: "ipython",
              label: "Inspect the chat modules", status: "ok", ts: now - 26, duration_ms: 320,
              args_preview: JSON.stringify({code: "modules = ['ChatDestination', 'ChatMessageList', 'ChatComposer']\nlen(modules)"}),
              result_preview: JSON.stringify({execution_count: 1, kernel_generation: 3, result: 3}),
            },
          ],
          receipt: {
            model: "Qwen3.5-4B-BF16",
            provider: "local",
            route: "local",
            durationMs: 4200,
            promptTokens: 5720,
            cachedInputTokens: 0,
            toolCount: 3,
            measurement: "provider_reported",
          },
        },
      ],
    },
  },
  {
    type: "chat:runtime",
    id: "fixture-welcome",
    runtime: {action_surface: "trusted-local.v1", kernel: {state: "ready", generation: 3}, selected_category_id: "coding", mount_revision: 1,
      mutation_enabled: false, mutation_effective_enabled: false, mutation_toggle_available: true, continuation_state: "ready"},
  },
  {
    type: "chat:context",
    schema: "variant1.session-context.v1",
    session_id: "fixture-welcome",
    status: "ready",
    captured_at: now - 24,
    model: "Qwen3.5-4B-BF16",
    measurement: "provider_reported",
    category_measurement: "estimated",
    used_tokens: 5720,
    context_limit_tokens: 16384,
    available_tokens: 10664,
    output_reserve_tokens: 512,
    cached_input_tokens: 0,
    percent_used: 34.9,
    categories: [
      {id: "messages", label: "Messages", tokens: 1800, percent: 11.0},
      {id: "tools", label: "Tools", tokens: 1220, percent: 7.4},
      {id: "skills", label: "Skills", tokens: 620, percent: 3.8},
      {id: "mcps", label: "MCPs", tokens: 510, percent: 3.1},
      {id: "plugins", label: "Plugins", tokens: 420, percent: 2.6},
      {id: "memory", label: "Memory", tokens: 750, percent: 4.6},
      {id: "other", label: "Other", tokens: 400, percent: 2.4},
    ],
  },
  {
    type: "hardware:telemetry",
    cpu_percent: 18,
    memory_percent: 42,
    gpu_percent: 7,
    cpu: {
      name: "AMD Ryzen 5 5600G with Radeon Graphics",
      utilization_pct: 18,
      variant1_utilization_pct: 3.8,
      logical_processors: 12,
      speed_mhz: 4230,
      temperature_c: 48,
      power_draw_w: 31.4,
    },
    memory: {
      total_mb: 15770,
      used_mb: 13520,
      available_mb: 2250,
      utilization_pct: 85.7,
      variant1_used_mb: 1018,
      variant1_utilization_pct: 6.5,
    },
    disk: {
      name: "Disk (C:)",
      utilization_pct: 23,
      variant1_utilization_pct: 8.2,
      read_bytes_per_second: 134217728,
      write_bytes_per_second: 37748736,
      response_time_ms: 1.4,
    },
    gpus: [{
      index: 0,
      name: "NVIDIA GeForce RTX 4070",
      utilization_pct: 16,
      variant1_utilization_pct: 4.1,
      vram_used_mb: 11878,
      vram_total_mb: 12288,
      temperature_c: 36,
      power_draw_w: 42,
      power_limit_w: 170,
    }],
    ram_total_mb: 15770,
    ram_available_mb: 2250,
    variant1_rss_mb: 1018,
    ts: now,
  },
  {
    type: "cloud:usage",
    cloud_usage: {
      period: "2026-08",
      today: "2026-08-12",
      providers: {
        openai: {
          name: "OpenAI",
          calls: 318,
          cost_usd: 96.4,
          cost_status: "estimated",
          last_model: "gpt-5.4",
          cached_prompt_tokens: 120000,
          limits: {
            health: "operational",
            requests: {used: 318, limit: 500, remaining: 182, reset: "38s"},
            tokens: {used: 54200, limit: 90000, remaining: 35800, reset: "1s"},
          },
        },
        anthropic: {
          name: "Anthropic",
          calls: 31,
          cost_usd: 72.8,
          cost_status: "estimated",
          last_model: "claude-sonnet-4-6",
          cached_prompt_tokens: 18000,
          limits: {
            health: "operational",
            requests: {used: 31, limit: 50, remaining: 19, reset: "60s"},
            tokens: {used: 28600, limit: 40000, remaining: 11400, reset: "60s"},
          },
        },
        gemini: {
          name: "Google AI",
          calls: 9,
          cost_usd: 34.2,
          cost_status: "estimated",
          last_model: "gemini-3.5-flash",
          limits: {
            health: "operational",
            requests: {used: 9, limit: 60, remaining: 51},
            tokens: {used: 14800, limit: 60000, remaining: 45200},
          },
        },
        xai: {
          name: "xAI",
          calls: 4,
          cost_usd: 15.2,
          cost_status: "exact",
          last_model: "grok-4.3",
          limits: {
            health: "operational",
            requests: {used: 4, limit: 30, remaining: 26},
            tokens: {used: 3200, limit: 20000, remaining: 16800},
          },
        },
      },
      total: {
        calls: 362,
        cost_usd: 218.6,
        today_cost_usd: 8.42,
        cost_status: "estimated",
        cached_prompt_tokens: 138000,
        budget_usd: 300,
        remaining_budget_usd: 81.4,
        projected_cost_usd: 319.2,
        budget_used_pct: 72.9,
        month_elapsed_pct: 38.7,
        days_remaining: 19,
      },
    },
  },
  {
    type: "model:usage",
    model_usage: modelUsageFixture(),
  },
  {
    type: "model:request_manifests",
    dropped_events: 1,
    publish_failures: 1,
    items: [
      {
        type: "model:request_manifest",
        schema: "variant1.model_request_manifest.v2",
        manifest_id: "fixture-request-cloud",
        logical_call_id: "fixture-call-cloud",
        attempt: 1,
        captured_at: now - 8,
        route: {
          provider: "openai",
          model: "gpt-5.4",
          physical_mode: "cloud",
          selected_mode: "cloud",
          api_style: "responses",
        },
        messages: {source: {count: 18}, rendered: {count: 15}},
        tools: {requested_count: 7, rendered_count: 6},
        images: {requested_count: 2, rendered_count: 2},
        budget: {
          estimated_input_tokens_lower_bound: 28420,
          context_limit_tokens: 128000,
          output_reserve_tokens: 8192,
          remaining_margin_tokens: 91388,
          over_budget: false,
        },
        context_lineage: {
          selection: {considered: 32, kept: 24, dropped: 8},
          transforms: [{kind: "supersession", affected_count: 4}],
        },
        usage: {
          linked: true,
          provider_reported: true,
          measurement: "provider_reported",
          prompt_tokens: 26120,
          completion_tokens: 1840,
          total_tokens: 27960,
          cached_tokens: 12200,
          cost_usd: 0.1842,
        },
        provenance: {
          context_receipt_available: true,
          selection_available: true,
          observation_projection_available: true,
          supersession_available: true,
          usage_available: true,
        },
        privacy: {
          policy: "metadata_only_v2",
          prompt_text_stored: false,
          tool_values_stored: false,
          image_data_stored: false,
          headers_stored: false,
          url_stored: false,
          exact_payload_ref: null,
        },
      },
      {
        type: "model:request_manifest",
        schema: "variant1.model_request_manifest.v2",
        manifest_id: "fixture-request-local",
        logical_call_id: "fixture-call-local",
        attempt: 2,
        captured_at: now - 19,
        route: {
          provider: "llama.cpp",
          model: "Qwen3.5-4B-BF16",
          physical_mode: "local",
          selected_mode: "local",
          api_style: "openai-compatible",
        },
        messages: {source: {count: 12}, rendered: {count: 9}},
        tools: {requested_count: 4, rendered_count: 4},
        images: {requested_count: 0, rendered_count: 0},
        budget: {
          estimated_input_tokens_lower_bound: 17120,
          context_limit_tokens: 16384,
          output_reserve_tokens: 1024,
          remaining_margin_tokens: -1760,
          over_budget: true,
        },
        context_lineage: {
          selection: {considered: 28, kept: 17, dropped: 11},
          transforms: [
            {kind: "compression", input_count: 9, output_count: 4},
            {kind: "supersession", affected_count: 3},
          ],
        },
        provenance: {
          context_receipt_available: true,
          selection_available: true,
          compression_receipt_available: true,
          supersession_available: true,
          usage_available: false,
        },
        privacy: {
          policy: "debug_retention_fixture",
          prompt_text_stored: true,
          tool_values_stored: false,
          image_data_stored: false,
          headers_stored: false,
          url_stored: false,
          exact_payload_ref: null,
        },
      },
      {
        type: "model:request_manifest",
        schema: "variant1.model_request_manifest.v2",
        manifest_id: "fixture-request-gemini",
        logical_call_id: "fixture-call-gemini",
        attempt: 1,
        captured_at: now - 31,
        route: {
          provider: "google",
          model: "gemini-3.5-flash",
          physical_mode: "cloud",
          selected_mode: "auto",
          api_style: "generate-content",
        },
        messages: {source: {count: 8}, rendered: {count: 8}},
        tools: {requested_count: 2, rendered_count: 2},
        images: {requested_count: 1, rendered_count: 1},
        budget: {
          estimated_input_tokens_lower_bound: 9440,
          context_limit_tokens: 1048576,
          output_reserve_tokens: 16000,
          remaining_margin_tokens: 1035040,
          over_budget: false,
        },
        context_lineage: {
          selection: {considered: 14, kept: 14, dropped: 0},
          transforms: [],
        },
        usage: {
          linked: true,
          provider_reported: true,
          measurement: "provider_reported",
          prompt_tokens: 9240,
          completion_tokens: 630,
          total_tokens: 9870,
          cost_usd: 0.0128,
        },
        provenance: {
          context_receipt_available: true,
          selection_available: true,
          observation_projection_available: true,
          usage_available: true,
        },
        privacy: {
          policy: "metadata_only_v2",
          prompt_text_stored: false,
          tool_values_stored: false,
          image_data_stored: false,
          headers_stored: false,
          url_stored: false,
          exact_payload_ref: null,
        },
      },
    ],
  },
];

function dispatchFixtures(): void {
  document.dispatchEvent(new CustomEvent("variant1:fixture-state", {detail: "connected"}));
  messages.forEach(message => {
    document.dispatchEvent(new CustomEvent("variant1:fixture-message", {detail: message}));
  });
}

if (document.body.dataset.variant1FixtureReady === "1") {
  dispatchFixtures();
} else {
  document.addEventListener("variant1:fixture-ready", dispatchFixtures, {once: true});
}
