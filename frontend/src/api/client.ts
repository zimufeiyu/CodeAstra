import { getAuthenticatedCsrfToken } from "./authClient";

export type ReviewFinding = {
  rule_id: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  confidence: number;
  category: string;
  file: string;
  start_line: number;
  end_line: number;
  title: string;
  evidence: string;
  impact: string;
  suggestion: string;
  impact_level?: "critical" | "high" | "medium" | "low";
  exploitability?: "high" | "medium" | "low";
  exposure?: "internet" | "authenticated" | "internal" | "local" | "unknown";
  risk_score?: number;
  severity_reason?: string;
};

export type ReviewResponse = {
  summary: string;
  findings: ReviewFinding[];
  uncovered: string[];
};


export type ReviewLanguage = "python" | "cpp";
export type ReviewMode = "paste" | "single" | "project";

export type ModelProfile = {
  profile_id: string;
  provider: "local" | "deepseek";
  model: string;
  display_name: string;
  available: boolean;
  unavailable_reason?: string | null;
  context_tokens: number;
  supports_json: boolean;
  requires_user_api_key?: boolean;
};

export type ModelSelection = Pick<
  ModelProfile,
  "profile_id" | "provider" | "model" | "display_name"
> & { selection_source?: "fixed" | "auto" | "manual" };

export type GitLabReviewOrigin = {
  type: "gitlab";
  gitlab_host: string;
  project_id: number;
  project_path: string;
  merge_request_iid: number;
  merge_request_url: string;
  base_sha: string;
  head_sha: string;
  selected_paths: string[];
  changed_ranges?: Record<string, GitLabChangedRange[]> | null;
};

export type LocalDiffReviewOrigin = {
  type: "local_diff";
  old_label: string;
  new_label: string;
  selected_paths: string[];
  changed_ranges?: Record<string, GitLabChangedRange[]> | null;
  old_sha256: Record<string, string>;
  new_sha256: Record<string, string>;
};

export type ReviewOrigin = GitLabReviewOrigin | LocalDiffReviewOrigin;

export type GitLabAccountProfile = {
  gitlab_host: string;
  user_id: number;
  username: string;
  name: string;
  avatar_url?: string | null;
  web_url?: string | null;
};

export type GitLabChangedRange = { start_line: number; end_line: number };

export type GitLabFileChange = {
  old_path: string;
  new_path: string;
  change_type: "added" | "modified" | "deleted" | "renamed";
  language: ReviewLanguage | null;
  old_content: string | null;
  new_content: string | null;
  diff: string;
  changed_ranges: GitLabChangedRange[];
  diff_truncated: boolean;
  selectable: boolean;
  unavailable_reason: string | null;
};

export type GitLabMergeRequestPreview = {
  gitlab_host: string;
  project_id: number;
  project_path: string;
  merge_request_iid: number;
  title: string;
  web_url: string;
  base_sha: string;
  head_sha: string;
  files: GitLabFileChange[];
};


export type LocalDiffFileInput = {
  filename: string;
  content: string;
};

export type LocalDiffFileChange = Omit<
  GitLabFileChange,
  "old_content" | "new_content"
> & {
  old_content: string;
  new_content: string;
  old_sha256: string;
  new_sha256: string;
};

export type LocalDiffPreview = {
  old_label: string;
  new_label: string;
  files: LocalDiffFileChange[];
};

export type ReviewSourceFile = {
  file_id: string;
  relative_path: string;
  language: ReviewLanguage;
  content: string;
  sha256: string;
  line_offsets: number[];
};

export type FindingVerification = {
  range_valid: boolean;
  evidence_matched: boolean;
  static_confirmed: boolean;
  cross_file_checked: boolean;
  deduplicated: boolean;
};

export type SessionFinding = {
  finding_id: string;
  source: "static" | "llm" | "merged";
  analyzer: string;
  rule_id: string;
  category: string;
  severity: ReviewFinding["severity"];
  confidence: number;
  file_id: string;
  file?: string;
  start_line: number;
  start_column: number;
  end_line: number;
  end_column: number;
  title: string;

  hover_summary: string;
  detail: string;
  evidence: string;
  impact: string;
  suggestion: string;
  verification: FindingVerification;
  use_def_evidence?: UseDefEvidence | null;
};

export type RepairIntentOption = {
  option_id: string;
  kind: "rename_existing" | "declare_parameter" | "declare_local" | "import_symbol" | "custom_behavior" | "defer";
  label: string;
  symbol?: string | null;
  module?: string | null;
  requires_input: "initializer" | "module" | "behavior" | "none";
  input_label?: string | null;
};

export type UseDefEvidence = {
  unresolved_name: string;
  scope_kind: string;
  scope_symbol?: string | null;
  statement_kind: string;
  statement_start_line: number;
  statement_end_line: number;
  statement_text: string;
  visible_parameters?: string[];
  visible_imports?: string[];
  visible_assignments?: string[];
  similar_candidates?: Array<{ name: string; kind: string; confidence: number; rationale: string }>;
  cross_file_exports?: Array<{ name: string; relative_path?: string | null; rationale: string }>;
  explanation: string;
  outcome: "safe_plan" | "needs_intent";
  options: RepairIntentOption[];
};

export type RepairIntentPreviewRequest = {
  review_id: string;
  finding_id: string;
  base_sha: string;
  option_id: string;
  intent_kind: RepairIntentOption["kind"];
  selected_symbol?: string | null;
  import_source?: string | null;
  initializer?: string | null;
  user_intent?: string | null;
};

export type ReviewRevision = {
  revision_id: string;
  finding_id: string;
  file_id: string;
  relative_path: string;
  created_at: string;
  before_sha256: string;
  after_sha256: string;
  diff?: string;
  explanation?: string | null;
  validation?: string[];
  undone_at?: string | null;
};

export type ReviewSession = {
  review_id: string;
  title: string;
  model: ModelSelection;
  mode: ReviewMode;
  status: "planning" | "queued" | "analyzing" | "reviewing" | "validating" | "aggregating" | "completed" | "cancelled" | "failed";
  created_at: string;
  expires_at: string;
  files: ReviewSourceFile[];
  findings: SessionFinding[];
  coverage: Array<{ language: ReviewLanguage; analyzer: string; available: boolean; message: string }>;
  summary: {
    total: number;
    critical: number;
    high: number;
    medium: number;
    low: number;
    info: number;
    text: string;
  };
  error?: string | null;
  error_code?: string | null;
  recheck_attempt_id?: string | null;
  recheck_attempt_status?: "running" | "completed" | "failed" | "timed_out" | null;
  recheck_deadline_at?: string | null;
  finding_decisions?: Record<string, "fixed" | "accepted_risk" | "deferred" | "dismissed">;
  decided_findings?: Record<string, SessionFinding>;
  finding_states?: Record<string, "active" | "candidate_ready" | "fixed_pending_revalidation" | "fixed_verified" | "accepted_risk" | "deferred" | "dismissed" | "reopened">;
  finding_decision_history?: Array<{
    finding_id: string;
    action: "decided" | "reopened";
    decision?: "fixed" | "accepted_risk" | "deferred" | "dismissed" | null;
    created_at: string;
    reason: string;
    revision_retained: boolean;
  }>;
  ignored_finding_fingerprints?: string[];
  origin?: ReviewOrigin | null;
  revisions?: ReviewRevision[];
};

export type InstanceHealth = {
  endpoint_id: string;
  inflight_requests: number;
  inflight_tokens: number;
  circuit_open: boolean;
  available?: boolean;
  reason_code?: "connection_refused" | "timeout" | "unreachable" | "health_check_failed" | "circuit_open" | null;
};

export type GatewayHealthResponse = {
  instances: InstanceHealth[];
};

export type ReviewStreamEvent = {
  event: "status" | "delta" | "reset" | "final" | "error";
  data: Record<string, unknown>;
};

export class ApiError extends Error {
  readonly status?: number;
  readonly code?: string;
  readonly details?: Record<string, unknown>;

  constructor(message: string, status?: number, code?: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
const reviewSystemPrompt =
  "\u8bf7\u8f93\u51fa\u4e2d\u6587\u4ee3\u7801\u5ba1\u67e5\u7ed3\u679c\uff0c\u53ea\u8fd4\u56de\u7b26\u5408 ReviewResponse schema \u7684\u5b8c\u6574 JSON\uff0c\u4e0d\u8981\u4f7f\u7528 Markdown\u3002";

function endpoint(path: string): string {
  if (!apiBaseUrl) {
    return path;
  }

  return apiBaseUrl.replace(/\/$/, "") + path;
}

async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    if (error instanceof TypeError) {
      throw new ApiError(
        "无法连接 CodeAstra 服务，请检查网络连接，或确认服务正在运行后重试。",
        0,
        "codeastra_unreachable",
      );
    }
    throw error;
  }
}

function createRequestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? "review-" + Date.now();
}

function messageForStatus(status: number): string {
  if (status === 401) return "账号已在其他设备登录或会话已过期，请重新登录。";
  if (status === 403) return "当前账号没有权限，或页面安全令牌已过期，请刷新后重试。";
  if (status === 409) return "当前内容已发生变化，请刷新后重新操作。";
  if (status === 413) return "请求内容过大，请缩小文件或项目后重试。";
  if (status === 422) return "输入内容无法安全处理，请根据提示修改后重试。";
  if (status === 429) return "请求过于频繁，请稍后重试。";
  if (status === 502) {
    return "\u6a21\u578b\u672a\u80fd\u751f\u6210\u5b8c\u6574\u7684\u5ba1\u67e5\u7ed3\u679c\uff0c\u8bf7\u7f29\u77ed\u8f93\u5165\u6216\u7a0d\u540e\u91cd\u8bd5\u3002";
  }

  if (status === 503) {
    return "\u6a21\u578b\u670d\u52a1\u6682\u65f6\u4e0d\u53ef\u7528\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002";
  }

  if (status >= 500) {
    return "\u6a21\u578b\u670d\u52a1\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002";
  }

  return "\u8bf7\u6c42\u672a\u80fd\u5b8c\u6210\uff0c\u8bf7\u68c0\u67e5\u8f93\u5165\u540e\u91cd\u8bd5\u3002";
}

async function readJson<T>(response: Response, useServerErrorDetail = false): Promise<T> {
  if (!response.ok) {
    let message = messageForStatus(response.status);
    let code: string | undefined;
    let details: Record<string, unknown> | undefined;
    if (response.status < 500 || useServerErrorDetail) {
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string" && body.detail.trim()) {
          const translated: Record<string, string> = {
            "authentication required": "账号已在其他设备登录或会话已过期，请重新登录。",
            "invalid CSRF token": "页面安全令牌已过期，请刷新后重试。",
            "password change required": "首次登录需要先修改密码。",
          };
          message = translated[body.detail] ?? body.detail;
        }
        if (body.detail && typeof body.detail === "object") {
          const detail = body.detail as { code?: unknown; error_code?: unknown; message?: unknown; context?: unknown };
          if (typeof detail.message === "string" && detail.message.trim()) message = detail.message;
          const rawCode = detail.error_code ?? detail.code;
          if (typeof rawCode === "string" && rawCode.trim()) code = rawCode;
          details = { ...(body.detail as Record<string, unknown>) };
          delete details.code;
          delete details.error_code;
          delete details.message;
          if (detail.context && typeof detail.context === "object") {
            details = { ...details, ...(detail.context as Record<string, unknown>) };
          }
          delete details.context;
        }
      } catch {
        // Keep the safe status-based fallback for non-JSON responses.
      }
    }
    throw new ApiError(message, response.status, code, details);
  }

  return (await response.json()) as T;
}

export async function reviewCode(code: string): Promise<ReviewResponse> {
  const response = await apiFetch(endpoint("/v1/review"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: createRequestId(),
      model: "qwen3-8b",
      messages: [
        { role: "system", content: reviewSystemPrompt },
        { role: "user", content: code },
      ],
      max_output_tokens: 32768,
      temperature: 0,
    }),
  });

  return readJson<ReviewResponse>(response);
}

export async function getInstanceHealth(): Promise<GatewayHealthResponse> {
  const response = await apiFetch(endpoint("/health/instances"), { method: "GET" });

  return readJson<GatewayHealthResponse>(response);
}

export async function getModelProfiles(): Promise<ModelProfile[]> {
  const response = await apiFetch(endpoint("/v1/model-profiles"), { method: "GET" });
  return readJson<ModelProfile[]>(response);
}
export type DeploymentMode = "ppu_local" | "deepseek_only" | "hybrid";
export type DeploymentStatus = {
  mode: DeploymentMode;
  default_profile_id: string;
  local_enabled: boolean;
  deepseek_enabled: boolean;
  configured_endpoints: string[];
  manifest_path: string;
  apply_enabled: boolean;
};
export type CapabilityReport = {
  status: "ready" | "ready_with_warnings" | "missing_runtime" | "missing_device" | "missing_model" | "unsupported" | "already_running";
  platform: string;
  checks: Array<{ name: string; ok: boolean; detail: string }>;
  detected_endpoints: string[];
  recommended_mode: DeploymentMode;
  can_manage_local_model: boolean;
  warnings: string[];
};
export type ModelCandidate = {
  path: string;
  architecture: string;
  torch_dtype: string;
  context_tokens: number;
  shard_count: number;
  supported: boolean;
  unavailable_reason?: string | null;
};
export type DeploymentPlan = {
  plan_id: string;
  mode: DeploymentMode;
  default_profile_id: string;
  action: string;
  model_path?: string | null;
  endpoints: string[];
  device_ids: string[];
  warnings: string[];
  restart_required: boolean;
};
export type DeploymentPlanRequest = {
  mode: DeploymentMode;
  model_path?: string | null;
  endpoints: string[];
  device_ids: string[];
};

export async function getDeploymentStatus(): Promise<DeploymentStatus> {
  return readJson<DeploymentStatus>(await apiFetch(endpoint("/v1/deployment/status")));
}

export async function probeDeploymentServer(): Promise<CapabilityReport> {
  return readJson<CapabilityReport>(await apiFetch(endpoint("/v1/deployment/probe"), { method: "POST" }), true);
}

export async function discoverDeploymentModels(): Promise<ModelCandidate[]> {
  return readJson<ModelCandidate[]>(await apiFetch(endpoint("/v1/deployment/models")), true);
}

export async function createDeploymentPlan(payload: DeploymentPlanRequest): Promise<DeploymentPlan> {
  return readJson<DeploymentPlan>(await apiFetch(endpoint("/v1/deployment/plan"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }), true);
}

export async function applyDeploymentPlan(plan: DeploymentPlan): Promise<void> {
  await readJson(await apiFetch(endpoint("/v1/deployment/apply"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan, confirm: true }),
  }), true);
}


export type DeepSeekModel = { id: string; display_name: string };

export async function getDeepSeekModels(apiKey: string): Promise<DeepSeekModel[]> {
  const response = await apiFetch(endpoint("/v1/integrations/deepseek/models"), {
    method: "GET",
    headers: { "X-DeepSeek-API-Key": apiKey },
  });
  return (await readJson<{ models: DeepSeekModel[] }>(response, true)).models;
}


export async function reviewCodeStream(
  code: string,
  onEvent: (event: ReviewStreamEvent) => void,
  signal?: AbortSignal,
): Promise<ReviewResponse> {
  const response = await apiFetch(endpoint("/v1/review/stream"), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    signal,
    body: JSON.stringify({
      request_id: createRequestId(),
      model: "qwen3-8b",
      messages: [
        { role: "system", content: reviewSystemPrompt },
        { role: "user", content: code },
      ],
      max_output_tokens: 32768,
      temperature: 0,
    }),
  });

  if (!response.ok) {
    throw new ApiError(messageForStatus(response.status), response.status);
  }
  if (!response.body) {
    throw new ApiError("流式连接不可用，请稍后重试。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const consume = (block: string): ReviewResponse | null => {
    const eventLine = block.split("\n").find((line) => line.startsWith("event:"));
    const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
    if (!eventLine || !dataLine) return null;
    const event = eventLine.slice(6).trim() as ReviewStreamEvent["event"];
    const data = JSON.parse(dataLine.slice(5).trim()) as Record<string, unknown>;
    const parsed = { event, data };
    onEvent(parsed);
    if (event === "error") {
      throw new ApiError(String(data.message ?? "模型服务请求失败，请稍后重试。"));
    }
    if (event === "final") {
      return data as unknown as ReviewResponse;
    }
    return null;
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const result = consume(buffer.slice(0, boundary));
      if (result) {
        await reader.cancel();
        return result;
      }
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }

  throw new ApiError("流式响应未返回完整审查结果，请稍后重试。");
}


export type ReviewFilePayload = {
  filename: string;
  language: ReviewLanguage;
  content: string;
};

export type ReviewCreatePayload = {
  filename?: string;
  language?: ReviewLanguage;
  content?: string;
  files?: ReviewFilePayload[];
  origin?: ReviewOrigin;
  local_diff_base_files?: ReviewFilePayload[];
  model_profile_id?: string;
  deepseek_selection_mode?: "auto" | "manual";
  deepseek_model?: string;
};

export type ReviewCreated = {
  review_id: string;
  status: string;
  expires_at: string;
};

export type SessionReviewEvent = {
  id?: number;
  event: "stage" | "chunk" | "progress" | "finding" | "complete" | "error" | "cancelled";
  data: Record<string, unknown>;
};

export async function verifyGitLabAccount(
  host: string,
  privateToken: string,
): Promise<GitLabAccountProfile> {
  const response = await apiFetch(endpoint("/v1/integrations/gitlab/account/verify"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host, private_token: privateToken }),
  });
  return readJson<GitLabAccountProfile>(response, true);
}

export async function previewGitLabMergeRequest(
  mergeRequestUrl: string,
  privateToken: string,
  signal?: AbortSignal,
): Promise<GitLabMergeRequestPreview> {
  const response = await apiFetch(endpoint("/v1/integrations/gitlab/merge-request/preview"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      merge_request_url: mergeRequestUrl,
      private_token: privateToken || null,
    }),
    signal,
  });
  return readJson<GitLabMergeRequestPreview>(response, true);
}

export async function previewLocalDiff(
  oldFile: LocalDiffFileInput,
  newFile: LocalDiffFileInput,
  signal?: AbortSignal,
): Promise<LocalDiffPreview> {
  const response = await apiFetch(endpoint("/v1/integrations/local-diff/preview"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_file: oldFile, new_file: newFile }),
    signal,
  });
  return readJson<LocalDiffPreview>(response, true);
}

export async function createReviewSession(
  mode: ReviewMode,
  payload: ReviewCreatePayload,
  deepseekApiKey?: string,
): Promise<ReviewCreated> {
  const response = await apiFetch(endpoint(`/v1/reviews/${mode}`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(deepseekApiKey ? { "X-DeepSeek-API-Key": deepseekApiKey } : {}),
    },
    body: JSON.stringify(payload),
  });
  return readJson<ReviewCreated>(response);
}

export async function getReviewSession(reviewId: string, signal?: AbortSignal): Promise<ReviewSession> {
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}`), { method: "GET", signal });
  return readJson<ReviewSession>(response);
}

export async function cancelReviewSession(reviewId: string): Promise<void> {
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/cancel`), {
    method: "POST",
  });
  if (!response.ok && response.status !== 404) {
    throw new ApiError(messageForStatus(response.status), response.status);
  }
}

export async function resumeReviewSession(reviewId: string, deepseekApiKey?: string): Promise<void> {
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/resume`), {
    method: "POST",
    headers: deepseekApiKey ? { "X-DeepSeek-API-Key": deepseekApiKey } : {},
  });
  if (!response.ok) {
    throw new ApiError(messageForStatus(response.status), response.status);
  }
}

export async function streamReviewSession(
  reviewId: string,
  onEvent: (event: SessionReviewEvent) => void,
  signal?: AbortSignal,
): Promise<ReviewSession> {
  let lastEventId = 0;
  let reconnectAttempt = 0;
  const maxReconnectAttempts = 4;
  const delays = [250, 500, 1000, 2000, 5000];

  const waitBeforeReconnect = async () => {
    const delay = delays[Math.min(Math.max(0, reconnectAttempt - 1), delays.length - 1)];
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, delay);
      signal?.addEventListener(
        "abort",
        () => {
          window.clearTimeout(timer);
          reject(new DOMException("stopped", "AbortError"));
        },
        { once: true },
      );
    });
  };

  while (true) {
    if (signal?.aborted) throw new DOMException("stopped", "AbortError");
    try {
      const headers: Record<string, string> = { Accept: "text/event-stream" };
      if (lastEventId > 0) headers["Last-Event-ID"] = String(lastEventId);
      const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/events`), {
        method: "GET",
        headers,
        signal,
      });
      if (!response.ok) throw new ApiError(messageForStatus(response.status), response.status);
      if (!response.body) throw new ApiError("流式连接不可用，请稍后重试。");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let terminalEvent = false;

      const consume = (block: string) => {
        const lines = block.split("\n");
        const idLine = lines.find((line) => line.startsWith("id:"));
        const eventLine = lines.find((line) => line.startsWith("event:"));
        const dataLine = lines.find((line) => line.startsWith("data:"));
        if (!eventLine || !dataLine) return;
        if (idLine) {
          const parsedId = Number(idLine.slice(3).trim());
          if (Number.isSafeInteger(parsedId) && parsedId > lastEventId) {
            lastEventId = parsedId;
          }
        }
        const event = eventLine.slice(6).trim() as SessionReviewEvent["event"];
        const data = JSON.parse(dataLine.slice(5).trim()) as Record<string, unknown>;
        onEvent({ id: lastEventId || undefined, event, data });
        if (event === "cancelled") throw new DOMException("stopped", "AbortError");
        if (event === "complete" || event === "error") terminalEvent = true;
      };

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          consume(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
        if (done && buffer.trim()) {
          consume(buffer);
          buffer = "";
        }
        if (done || terminalEvent) break;
      }
      if (terminalEvent) {
        await reader.cancel();
        return getReviewSession(reviewId);
      }
      const snapshot = await getReviewSession(reviewId);
      if (["completed", "failed", "cancelled"].includes(snapshot.status)) return snapshot;
    } catch (streamError) {
      if (signal?.aborted || (streamError instanceof DOMException && streamError.name === "AbortError")) {
        throw new DOMException("stopped", "AbortError");
      }
      if (
        streamError instanceof ApiError
        && streamError.status !== undefined
        && streamError.status > 0
      ) throw streamError;
    }

    reconnectAttempt += 1;
    if (reconnectAttempt > maxReconnectAttempts) {
      try {
        const snapshot = await getReviewSession(reviewId);
        if (["completed", "failed", "cancelled"].includes(snapshot.status)) return snapshot;
      } catch (snapshotError) {
        if (snapshotError instanceof ApiError && snapshotError.status === 401) throw snapshotError;
      }
      throw new ApiError("连接多次中断，审查仍在后台继续。请点击“重新连接”，不要重复创建审查。", 0, "stream_reconnect_exhausted");
    }
    onEvent({
      event: "stage",
      data: {
        stage: "connection_reconnecting",
        message: `连接中断，正在重连（${reconnectAttempt}/${maxReconnectAttempts}）`,
        attempt: reconnectAttempt,
        max_attempts: maxReconnectAttempts,
      },
    });
    await waitBeforeReconnect();
  }
}

export function safeArtifactFilename(value: string | null, fallback: string): string {
  let decoded = value?.trim() || "";
  const extended = decoded.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  const regular = decoded.match(/filename\s*=\s*(?:"([^"]*)"|([^;]+))/i);
  if (extended?.[1]) {
    try {
      decoded = decodeURIComponent(extended[1].trim());
    } catch {
      decoded = extended[1].trim();
    }
  } else if (regular) {
    decoded = (regular[1] ?? regular[2] ?? "").trim();
  }
  const basename = decoded.split(/[\\/]/).pop() || fallback;
  const cleaned = basename
    .replace(/[\u0000-\u001f\u007f<>:"|?*]/g, "-")
    .replace(/[. ]+$/g, "")
    .trim()
    .slice(0, 180);
  if (!cleaned || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(cleaned)) {
    return `download-${fallback.replace(/[^A-Za-z0-9_.-]/g, "-")}`;
  }
  return cleaned;
}

export async function downloadArtifact(path: string, fallbackFilename: string): Promise<string> {
  let response: Response;
  try {
    response = await apiFetch(endpoint(path), { method: "GET", credentials: "same-origin" });
  } catch (error) {
    throw new ApiError(
      error instanceof Error ? `下载连接中断：${error.message}` : "下载连接中断，请重试。",
      0,
      "download_interrupted",
    );
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json() as { detail?: string | { message?: string } };
      detail = typeof body.detail === "string" ? body.detail : body.detail?.message ?? "";
    } catch {
      detail = "";
    }
    const fallback = response.status === 404
      ? "交付文件不存在或已失效，请刷新审查后重试。"
      : response.status === 409
        ? "当前修订状态不允许下载，请刷新审查后重试。"
        : messageForStatus(response.status);
    throw new ApiError(detail || fallback, response.status, `artifact_http_${response.status}`);
  }
  const blob = await response.blob();
  const filename = safeArtifactFilename(
    response.headers.get("Content-Disposition"),
    fallbackFilename,
  );
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  }
  return filename;
}
export type ReviewHistoryItem = {
  review_id: string;
  title: string;
  mode: ReviewMode;
  status: ReviewSession["status"];
  created_at: string;
  expires_at: string;
  file_count: number;
  file_names: string[];
  summary: ReviewSession["summary"];
  error?: string | null;
  origin?: ReviewOrigin | null;
};

export type ReviewHistoryResponse = {
  items: ReviewHistoryItem[];
  limit: number;
  offset: number;
};

export type FollowupMessage = {
  message_id: string;
  review_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
};

export type FollowupActionResponse =
  | { action: "answer"; messages: FollowupMessage[] }
  | { action: "fix_candidate"; candidate: FixCandidate; phase: "awaiting_confirmation" };

export type FollowupCodeContext = {
  kind: "finding" | "selection";
  file_id: string;
  finding_id?: string;
  start_line?: number;
  end_line?: number;
  selected_code?: string;
};

export async function listReviewSessions(
  limit = 20,
  offset = 0,
): Promise<ReviewHistoryResponse> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  const response = await apiFetch(endpoint(`/v1/reviews?${query.toString()}`), {
    method: "GET",
  });
  return readJson<ReviewHistoryResponse>(response);
}
export async function renameReviewSession(
  reviewId: string,
  title: string,
): Promise<ReviewSession> {
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}`), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return readJson<ReviewSession>(response);
}

export async function deleteReviewSession(reviewId: string): Promise<void> {
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}`), {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new ApiError(messageForStatus(response.status), response.status);
  }
}



export type FindingDecisionResponse = {
  session: ReviewSession;
  revised_review: ReviewSession | null;
};

export type ReopenFindingResponse = {
  session: ReviewSession;
  revision_retained: boolean;
  already_reopened: boolean;
};

export type FollowupFixPreviewRequest = {
  instruction: string;
  base_sha: string;
  context: FollowupCodeContext;
};

export async function decideReviewFinding(
  reviewId: string,
  findingId: string,
  decision: "accepted_risk" | "deferred" | "dismissed",
): Promise<FindingDecisionResponse> {
  const response = await apiFetch(
    endpoint(`/v1/reviews/${reviewId}/findings/${findingId}/decision`),
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ decision }),
    },
  );
  return readJson<FindingDecisionResponse>(response, true);
}

export async function reopenReviewFinding(
  reviewId: string,
  findingId: string,
): Promise<ReopenFindingResponse> {
  const response = await apiFetch(
    endpoint(`/v1/reviews/${reviewId}/findings/${findingId}/reopen`),
    { method: "POST" },
  );
  return readJson<ReopenFindingResponse>(response, true);
}

export type FixCandidate = {
  candidate_id: string;
  review_id: string;
  finding_id: string;
  file_id: string;
  relative_path: string;
  created_at: string;
  expires_at: string;
  base_sha256: string;
  after_sha256: string;
  diff: string;
  explanation: string;
  validation: string[];
  output_token_budget: number;
};

export type FixConfirmationResponse = {
  session: ReviewSession;
  revised_review: ReviewSession | null;
  phase: "applied";
};

export async function previewReviewFix(
  reviewId: string,
  findingId: string,
  deepseekApiKey?: string,
  signal?: AbortSignal,
): Promise<FixCandidate> {
  const csrfToken = getAuthenticatedCsrfToken();
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/findings/${findingId}/fix-preview`), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      ...(deepseekApiKey ? { "X-DeepSeek-API-Key": deepseekApiKey } : {}),
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    signal,
  });
  return (await readJson<{ candidate: FixCandidate }>(response, true)).candidate;
}

export async function previewReviewFixWithIntent(
  reviewId: string,
  findingId: string,
  intent: RepairIntentPreviewRequest,
  signal?: AbortSignal,
): Promise<FixCandidate> {
  const csrfToken = getAuthenticatedCsrfToken();
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/findings/${findingId}/fix-preview/intent`), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: JSON.stringify(intent),
    signal,
  });
  return (await readJson<{ candidate: FixCandidate }>(response, true)).candidate;
}

export async function confirmReviewFix(
  reviewId: string,
  candidateId: string,
): Promise<FixConfirmationResponse> {
  const csrfToken = getAuthenticatedCsrfToken();
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/fix-candidates/confirm`), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: JSON.stringify({ candidate_id: candidateId }),
  });
  return readJson<FixConfirmationResponse>(response, true);
}

export async function cancelReviewFix(reviewId: string, candidateId: string): Promise<void> {
  const csrfToken = getAuthenticatedCsrfToken();
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/fix-candidates/${candidateId}`), {
    method: "DELETE",
    credentials: "same-origin",
    headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
  });
  if (!response.ok) throw new ApiError(messageForStatus(response.status), response.status);
}


export async function getReviewRevisions(reviewId: string): Promise<ReviewRevision[]> {
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/revisions`), {
    method: "GET",
  });
  return (await readJson<{ items: ReviewRevision[] }>(response)).items;
}

export async function undoReviewRevision(
  reviewId: string,
  revisionId: string,
  deepseekApiKey?: string,
): Promise<ReviewSession> {
  const response = await apiFetch(
    endpoint(`/v1/reviews/${reviewId}/revisions/${revisionId}/undo`),
    {
      method: "POST",
      headers: deepseekApiKey ? { "X-DeepSeek-API-Key": deepseekApiKey } : {},
    },
  );
  return (await readJson<{ revised_review: ReviewSession }>(response, true)).revised_review;
}


export async function getReviewFollowups(
  reviewId: string,
  context?: FollowupCodeContext,
): Promise<FollowupMessage[]> {
  const historyContext = context
    ? { ...context, selected_code: undefined }
    : undefined;
  const query = historyContext
    ? `?context=${encodeURIComponent(JSON.stringify(historyContext))}`
    : "";
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/followups${query}`), {
    method: "GET",
  });
  return (await readJson<{ messages: FollowupMessage[] }>(response)).messages;
}
export async function askReviewFollowup(
  reviewId: string,
  question: string,
  context?: FollowupCodeContext,
  deepseekApiKey?: string,
  baseSha?: string,
): Promise<FollowupActionResponse> {
  const csrfToken = getAuthenticatedCsrfToken();
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/followups`), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
      ...(deepseekApiKey ? { "X-DeepSeek-API-Key": deepseekApiKey } : {}),
    },
    body: JSON.stringify({ question, context, base_sha: baseSha }),
  });
  const result = await readJson<
    FollowupActionResponse | { messages: FollowupMessage[] }
  >(response, true);
  if (!("action" in result)) return { action: "answer", messages: result.messages };
  return result;
}

export async function previewReviewFollowupFix(
  reviewId: string,
  payload: FollowupFixPreviewRequest,
  deepseekApiKey?: string,
  signal?: AbortSignal,
): Promise<FixCandidate> {
  const csrfToken = getAuthenticatedCsrfToken();
  const response = await apiFetch(endpoint(`/v1/reviews/${reviewId}/followups/fix-preview`), {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
      ...(deepseekApiKey ? { "X-DeepSeek-API-Key": deepseekApiKey } : {}),
    },
    body: JSON.stringify(payload),
    signal,
  });
  return (await readJson<{ candidate: FixCandidate }>(response, true)).candidate;
}


