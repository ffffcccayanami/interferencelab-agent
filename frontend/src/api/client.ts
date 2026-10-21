/** 后端 API 客户端（类型化，单一出口，便于测试与替换）。 */

const baseURL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

export interface ParamSpec {
  type: "int" | "str";
  min?: number;
  max?: number;
  default?: number;
  max_length?: number;
}

export interface ValidationEntry {
  entry_id: string;
  title: string;
  description: string;
  params: Record<string, ParamSpec>;
  timeout_s: number;
  requires_approval: boolean;
}

export interface Meta {
  task_types: string[];
  llm_configured: boolean;
  llm_model_id: string;
  llm_key_fingerprint: string;
  llm_endpoint: string;
  llm_auth_mode: string;
  llm_provider: string;
  llm_profile: string;
  llm_deployment: string;
  config_source: string;
  max_upload_mb: number;
  queue_size: number;
  workers: number;
  gpu_concurrency: number;
  cpu_concurrency: number;
  validation_entries: ValidationEntry[];
}

export interface LLMHealth {
  enabled: boolean;
  configured: boolean;
  endpoint: string;
  auth_mode: string;
  model_id: string;
  health_path: string;
  reachable: boolean;
  status_code: number | null;
  latency_ms: number | null;
  models: string[];
  model_present: boolean | null;
  streaming_supported: string;
  error: string | null;
}

export interface ArtifactInfo {
  artifact_id: string;
  kind: string;
  filename: string;
  size: number;
  sha256: string;
  download_url: string;
}

export interface PendingApproval {
  approval_id: string;
  entry?: ValidationEntry;
  params: Record<string, number>;
}

export interface NodeTiming {
  id: string;
  run_id: string;
  node: string;
  status: string;
  duration_ms: number;
  created_at: string;
}

export interface LLMCall {
  id: string;
  node: string;
  attempt: number;
  model: string;
  endpoint_host: string;
  streamed: boolean;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  reasoning_tokens: number | null;
  ttft_first_token_ms: number | null;
  ttft_first_content_ms: number | null;
  total_ms: number | null;
  stream_ms: number | null;
  prefill_tokens_per_s_est: number | null;
  decode_tokens_per_s: number | null;
  content_tokens_per_s: number | null;
  finish_reason: string | null;
  outcome: string;
  error_code: string | null;
  created_at: string;
}

export interface ServerMetrics {
  status: string;
  counters?: Record<string, number>;
  gauges?: Record<string, number>;
  captured_at?: { start: string; end: string };
}

export interface EvalCheck {
  name: string;
  label: string;
  actual: number | null;
  threshold: number;
  passed: boolean;
  unit: string;
}

export interface EvalRegressionDetail {
  metric: string;
  label: string;
  baseline: number;
  current: number;
  delta: number;
}

export interface RunEvaluation {
  status: string;
  reason: string;
  checks: EvalCheck[];
  baseline_run_id: string | null;
  baseline?: {
    run_id: string;
    finished_at: string | null;
    throughput_pct: number | null;
    ttft_p50_reduction_pct: number | null;
  } | null;
  regression?: { tolerance_pct: number; details: EvalRegressionDetail[] } | null;
}

export interface RunInfo {
  run_id: string;
  task_type: string;
  status: string;
  stage: string;
  question: string;
  degraded: boolean;
  warnings: string[];
  error: { code: string; message: string; details?: unknown } | null;
  pending_approval: PendingApproval | null;
  artifacts: ArtifactInfo[];
  artifact_count: number;
  result_summary: Record<string, unknown> | null;
  duration_ms: number | null;
  queue_wait_ms: number | null;
  llm_calls_count: number | null;
  eval_status: string | null;
  benchmark_key: string | null;
  baseline_run_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  node_timings?: NodeTiming[];
  llm_calls?: LLMCall[];
  server_metrics?: ServerMetrics | null;
}

export interface PercentilePair {
  p50: number | null;
  p95: number | null;
  max?: number | null;
}

export interface MonitorSummary {
  window_hours: number;
  since: string;
  truncated: boolean;
  generated_at: string;
  runs: {
    total: number;
    finished: number;
    by_status: Record<string, number>;
    success_rate: number | null;
    duration_ms: PercentilePair;
    failure_codes: { code: string; count: number }[];
    slowest: { run_id: string; task_type: string; status: string; duration_ms: number | null }[];
    evaluation: {
      counts: Record<string, number>;
      evaluated: number;
      pass_rate: number | null;
      regressions: {
        run_id: string;
        task_type: string;
        eval_status: string | null;
        reason: string | null;
        finished_at: string | null;
        baseline_run_id: string | null;
      }[];
    };
  };
  llm: {
    calls: number;
    errors: number;
    streamed: number;
    tokens: { prompt: number; completion: number; reasoning: number };
    ttft_first_token_ms: PercentilePair;
    ttft_first_content_ms: PercentilePair;
    decode_tokens_per_s: PercentilePair;
    content_tokens_per_s: PercentilePair;
    by_node: { node: string; calls: number; ttft_p95_ms: number | null }[];
  };
  nodes: Record<string, { calls: number; p50_ms: number | null; p95_ms: number | null; max_ms: number }>;
  alerts: { code: string; message: string }[];
}

export interface RunEvent {
  seq?: number;
  type: string;
  payload: Record<string, unknown>;
}

export interface TrendPoint {
  ts: string;
  calls: number;
  runs: number;
  failures: number;
  ttft_p50: number | null;
  ttft_p95: number | null;
  decode_p50: number | null;
  duration_p50: number | null;
}

export interface MonitorTrend {
  hours: number;
  buckets: number;
  since: string;
  generated_at: string;
  series: TrendPoint[];
}

export class ApiFailure extends Error {
  readonly code: string;
  readonly details: unknown;

  constructor(code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiFailure";
    this.code = code;
    this.details = details;
  }
}

async function parseFailure(response: Response): Promise<ApiFailure> {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const error = (payload as { error?: { code?: string; message?: string; details?: unknown } } | null)?.error;
  return new ApiFailure(
    error?.code ?? "HTTP_ERROR",
    error?.message ?? `请求失败（HTTP ${response.status}）`,
    error?.details
  );
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${baseURL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init.headers ?? {})
    }
  });
  if (!response.ok) {
    throw await parseFailure(response);
  }
  return (await response.json()) as T;
}

export function artifactUrl(artifact: ArtifactInfo): string {
  return `${baseURL}${artifact.download_url}`;
}

export function getMeta(): Promise<Meta> {
  return request<Meta>("/api/meta");
}

export function getLLMHealth(): Promise<LLMHealth> {
  return request<LLMHealth>("/api/llm/health");
}

export function getMonitorSummary(hours = 0): Promise<MonitorSummary> {
  return request<MonitorSummary>(`/api/monitor/summary${hours ? `?hours=${hours}` : ""}`);
}

export function getMonitorTrend(hours = 24, buckets = 24): Promise<MonitorTrend> {
  return request<MonitorTrend>(`/api/monitor/trend?hours=${hours}&buckets=${buckets}`);
}

export async function uploadFile(file: File): Promise<{ upload_id: string; filename: string; detected_type: string }> {
  const form = new FormData();
  form.append("file", file);
  return request("/api/uploads", { method: "POST", body: form });
}

export function createRun(payload: {
  task_type: string;
  question: string;
  upload_ids: string[];
  options: Record<string, unknown>;
}): Promise<{ run_id: string; status: string; queue_position: number }> {
  return request("/api/runs", { method: "POST", body: JSON.stringify(payload) });
}

export function listRuns(limit = 30): Promise<{ runs: RunInfo[] }> {
  return request(`/api/runs?limit=${limit}`);
}

export function getRun(runId: string): Promise<RunInfo> {
  return request<RunInfo>(`/api/runs/${runId}`);
}

export function approveRun(runId: string, approvalId: string, params: Record<string, number>): Promise<unknown> {
  return request(`/api/runs/${runId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approval_id: approvalId, params })
  });
}

export function cancelRun(runId: string): Promise<unknown> {
  return request(`/api/runs/${runId}/cancel`, { method: "POST" });
}

export async function getReport(runId: string): Promise<string> {
  const response = await fetch(`${baseURL}/api/runs/${runId}/report`);
  if (!response.ok) {
    throw await parseFailure(response);
  }
  return response.text();
}
