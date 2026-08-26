import {
  AlertTriangle,
  ArrowRight,
  Check,
  FileCode2,
  FileDiff,
  ListChecks,
  History as HistoryIcon,
  Send,
  ShieldCheck,
  Square,
  X,
} from "lucide-react";
import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";

import "./styles.css";
import "./desktopLayout.css";
import {
  GatewayHealthResponse,
  ApiError,
  FollowupMessage,
  FixCandidate,
  ModelProfile,
  ReviewHistoryItem,
  ReviewRevision,
  ReviewSession,
  ReviewCreatePayload,
  GitLabFileChange,
  GitLabMergeRequestPreview,
  LocalDiffPreview,
  ReviewFilePayload,
  ReviewOrigin,
  RepairIntentOption,
  SessionFinding,
  UseDefEvidence,
  cancelReviewSession,
  cancelReviewFix,
  askReviewFollowup,
  createReviewSession,
  confirmReviewFix,
  decideReviewFinding,
  deleteReviewSession,
  downloadArtifact,
  getInstanceHealth,
  getModelProfiles,
  getDeepSeekModels,
  getReviewFollowups,
  getReviewSession,
  getReviewRevisions,
  listReviewSessions,
  previewReviewFix,
  previewReviewFixWithIntent,
  renameReviewSession,
  reopenReviewFinding,
  resumeReviewSession,
  streamReviewSession,
  undoReviewRevision,
} from "./api/client";
import { CodeViewer } from "./components/CodeViewer";
import { ProductBrand } from "./components/ProductBrand";
import type { CodeSelection } from "./components/CodeViewer";
import { FollowupContext, FollowupDialog } from "./components/FollowupDialog";
import { HistorySidebar } from "./components/HistorySidebar";
import { RevisionHistoryDialog } from "./components/RevisionHistoryDialog";
import { FixPreviewDialog } from "./components/FixPreviewDialog";
import { RepairIntentDialog } from "./components/RepairIntentDialog";
import { AttachmentMenu } from "./components/AttachmentMenu";
import { GitLabAccountManagement } from "./components/GitLabAccountDialog";
import { AccountSecurityPanel } from "./auth/AccountSecurityPanel";
import { SidebarAccountMenu } from "./auth/SidebarAccountMenu";
import { useOptionalAuthSession } from "./auth/AuthSessionContext";
import { GitLabImportDialog } from "./components/GitLabImportDialog";
import { LocalDiffDialog } from "./components/LocalDiffDialog";
import {
  loadActiveGitLabAccountId,
  loadGitLabAccounts,
  persistActiveGitLabAccountId,
  persistGitLabAccounts,
} from "./utils/gitlabAccounts";
import type { SavedGitLabAccount } from "./utils/gitlabAccounts";
import {
  mergeLocalAttachments,
  replaceGitLabAttachments,
  replaceLocalDiffAttachments,
} from "./utils/gitlabAttachments";
import {
  detectLanguage,
  duplicateCanonicalPaths,
  readSourceFileStrict,
  uniqueSnippetFilename,
  validateSourceText,
} from "./utils/languageDetector";
import { loadDeepSeekSettings, persistDeepSeekSettings } from "./utils/deepseekSettings";
import { beginGitLabOAuth, consumeGitLabOAuthCallback, defaultGitLabRedirectUri, hasGitLabOAuthToken } from "./utils/gitlabOAuth";

const severityLabels: Record<SessionFinding["severity"], string> = {
  critical: "严重",
  high: "高危",
  medium: "中危",
  low: "低危",
  info: "提示",
};

const fallbackModelProfiles: ModelProfile[] = [
  {
    profile_id: "local-qwen3-8b",
    provider: "local",
    model: "Qwen3-8B",
    display_name: "\u672c\u5730 Qwen3-8B",
    available: true,
    context_tokens: 40960,
    supports_json: true,
  },
  {
    profile_id: "deepseek-api",
    provider: "deepseek",
    model: "auto",
    display_name: "DeepSeek API",
    available: true,
    context_tokens: 131072,
    supports_json: true,
    requires_user_api_key: true,
  },
];

const MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024;
const MAX_REVIEW_SOURCE_BYTES = 8 * 1024 * 1024;
const HISTORY_PAGE_SIZE = 50;

type DraftAttachment = {
  id: string;
  filename: string;
  content: string;
  size: number;
  source?: "local" | "gitlab" | "local-diff";
  comparison?: {
    oldLabel: string;
    newLabel: string;
    changedRangeCount: number;
  };
};

function removeOriginPaths(
  origin: ReviewOrigin | null,
  removedPaths: Iterable<string>,
): ReviewOrigin | null {
  if (!origin) return null;
  const removed = new Set(removedPaths);
  const selectedPaths = origin.selected_paths.filter((item) => !removed.has(item));
  if (!selectedPaths.length) return null;
  const retained = new Set(selectedPaths);
  const changedRanges = Object.fromEntries(
    Object.entries(origin.changed_ranges ?? {}).filter(([item]) => retained.has(item)),
  );
  if (origin.type === "gitlab") {
    return { ...origin, selected_paths: selectedPaths, changed_ranges: changedRanges };
  }
  return {
    ...origin,
    selected_paths: selectedPaths,
    changed_ranges: changedRanges,
    old_sha256: Object.fromEntries(
      Object.entries(origin.old_sha256).filter(([item]) => retained.has(item)),
    ),
    new_sha256: Object.fromEntries(
      Object.entries(origin.new_sha256).filter(([item]) => retained.has(item)),
    ),
  };
}

export function repairFailureMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code === "scope_mismatch") {
    return "系统无法安全定位替换边界，未生成修改候选，原代码未改变。";
  }
  if (error instanceof ApiError && error.code === "model_output_invalid") {
    return "模型返回的修改内容无效，未生成修改候选，原代码未改变。";
  }
  return error instanceof Error ? error.message : fallback;
}

type ReviewProgress = {
  total: number;
  completed: number;
  queued: number;
  running: number;
  failed: number;
  coverage_percent: number;
  active_file?: string;
};

const decisionLabels = {
  fixed: "已修复",
  accepted_risk: "接受风险",
  deferred: "待处理",
  dismissed: "已驳回",
} as const;

export function parseReviewRoute(pathname: string): { reviewId: string; findingId?: string } | null {
  const match = pathname.match(/^\/reviews\/([^/]+)(?:\/findings\/([^/]+))?\/?$/);
  if (!match) return null;
  return {
    reviewId: decodeURIComponent(match[1]),
    findingId: match[2] ? decodeURIComponent(match[2]) : undefined,
  };
}

function App() {
  const authSession = useOptionalAuthSession();
  const [draftCode, setDraftCode] = useState("");
  const [attachments, setAttachments] = useState<DraftAttachment[]>([]);
  const [gitLabDialogOpen, setGitLabDialogOpen] = useState(false);
  const [gitLabOAuthToken, setGitLabOAuthToken] = useState<string | null>(null);
  const [gitLabOAuthError, setGitLabOAuthError] = useState("");
  const [localDiffDialogOpen, setLocalDiffDialogOpen] = useState(false);
  const [resumeGitLabImportAfterAccount, setResumeGitLabImportAfterAccount] = useState(false);
  const [gitLabAccounts, setGitLabAccounts] = useState<SavedGitLabAccount[]>(
    () => loadGitLabAccounts(),
  );
  const [activeGitLabAccountId, setActiveGitLabAccountId] = useState<string | null>(
    () => loadActiveGitLabAccountId(),
  );
  const [reviewOrigin, setReviewOrigin] = useState<ReviewOrigin | null>(null);
  const [localDiffBaseFiles, setLocalDiffBaseFiles] = useState<ReviewFilePayload[]>([]);
  const [historyItems, setHistoryItems] = useState<ReviewHistoryItem[]>([]);
  const [historyHasMore, setHistoryHasMore] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [historyRetryReset, setHistoryRetryReset] = useState(false);
  const [followups, setFollowups] = useState<FollowupMessage[]>([]);
  const [followupBusy, setFollowupBusy] = useState(false);
  const [followupError, setFollowupError] = useState("");
  const [followupContext, setFollowupContext] = useState<FollowupContext | null>(null);
  const [runningReviewId, setRunningReviewId] = useState<string | null>(null);
  const [viewedReviewId, setViewedReviewId] = useState<string | null>(null);
  const [fixBusyId, setFixBusyId] = useState<string | null>(null);
  const [fixCandidate, setFixCandidate] = useState<FixCandidate | null>(null);
  const [repairIntent, setRepairIntent] = useState<{
    finding: SessionFinding;
    evidence: UseDefEvidence;
    baseSha: string;
  } | null>(null);
  const [fixConfirmationBusy, setFixConfirmationBusy] = useState(false);
  const [revisionDialogOpen, setRevisionDialogOpen] = useState(false);
  const [revisions, setRevisions] = useState<ReviewRevision[]>([]);
  const [revisionBusyId, setRevisionBusyId] = useState<string | null>(null);
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [health, setHealth] = useState<GatewayHealthResponse | null>(null);
  const [healthUnavailable, setHealthUnavailable] = useState(false);
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>(
    fallbackModelProfiles,
  );
  const [selectedModelProfileId, setSelectedModelProfileId] =
    useState(() => loadDeepSeekSettings(authSession?.user.user_id).preferredProfileId);
  const [deepSeekSettings, setDeepSeekSettings] = useState(
    () => loadDeepSeekSettings(authSession?.user.user_id),
  );
  const [deepSeekModels, setDeepSeekModels] = useState<Array<{ id: string; display_name: string }>>([]);
  const [validatedDeepSeekKey, setValidatedDeepSeekKey] = useState("");
  const [deepSeekValidationBusy, setDeepSeekValidationBusy] = useState(false);
  const [error, setError] = useState("");
  const [isReviewing, setIsReviewing] = useState(false);
  const [stageMessage, setStageMessage] = useState("");
  const [progress, setProgress] = useState<ReviewProgress | null>(null);
  const [disconnectedReviewId, setDisconnectedReviewId] = useState<string | null>(null);
  const [artifactBusyKey, setArtifactBusyKey] = useState<string | null>(null);
  const [artifactState, setArtifactState] = useState<{
    key: string;
    path: string;
    fallback: string;
    label: string;
    message: string;
    error: boolean;
  } | null>(null);
  const [reopenBusyId, setReopenBusyId] = useState<string | null>(null);
  const [activeFileId, setActiveFileId] = useState<string | null>(null);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const fixPreviewControllerRef = useRef<AbortController | null>(null);
  const reviewIdRef = useRef<string | null>(null);
  const viewedReviewIdRef = useRef<string | null>(null);
  const followupContextIdentityRef = useRef<string | null>(null);
  const followupLoadSequenceRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const stoppedControllersRef = useRef(new WeakSet<AbortController>());
  const historyLoadingRef = useRef(false);

  function viewReview(reviewId: string | null) {
    viewedReviewIdRef.current = reviewId;
    setViewedReviewId(reviewId);
  }

  useEffect(() => {
    let ignore = false;
    let refreshTimer: number | undefined;
    const schedule = () => {
      if (ignore) return;
      const delay = document.hidden ? 30_000 : isReviewing ? 1_000 : 5_000;
      refreshTimer = window.setTimeout(() => void refreshHealth(), delay);
    };
    async function refreshHealth() {
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
      try {
        const snapshot = await getInstanceHealth();
        if (!ignore) {
          setHealth(snapshot);
          setHealthUnavailable(false);
        }
      } catch {
        if (!ignore) setHealthUnavailable(true);
      } finally {
        schedule();
      }
    }
    const handleVisibility = () => {
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
      if (document.hidden) schedule();
      else void refreshHealth();
    };
    document.addEventListener("visibilitychange", handleVisibility);
    void refreshHealth();
    return () => {
      ignore = true;
      document.removeEventListener("visibilitychange", handleVisibility);
      if (refreshTimer !== undefined) window.clearTimeout(refreshTimer);
    };
  }, [isReviewing, selectedModelProfileId, session?.model.profile_id]);
  useEffect(() => {
    void consumeGitLabOAuthCallback()
      .then((token) => {
        if (token) {
          setGitLabOAuthToken(token);
          setGitLabDialogOpen(true);
          window.history.replaceState({}, document.title, window.location.pathname);
        } else if (hasGitLabOAuthToken()) {
          setGitLabOAuthToken("active");
        }
      })
      .catch((callbackError) => setGitLabOAuthError(callbackError instanceof Error ? callbackError.message : "GitLab 授权失败，请重试。"));
  }, []);

  useEffect(() => {
    void openHistory();
    const route = parseReviewRoute(window.location.pathname);
    if (route) void openHistorySession(route.reviewId, route.findingId, false);
    const profilesRequest = typeof getModelProfiles === "function"
      ? getModelProfiles()
      : Promise.resolve(fallbackModelProfiles);
    void profilesRequest
      .then((profiles) => {
        setModelProfiles(profiles);
        setSelectedModelProfileId((current) =>
          profiles.some((item) => item.profile_id === current && item.available)
            ? current
            : profiles.find((item) => item.available)?.profile_id ?? current,
        );
      })
      .catch(() => setModelProfiles(fallbackModelProfiles));
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      const route = parseReviewRoute(window.location.pathname);
      if (route) {
        void openHistorySession(route.reviewId, route.findingId, false);
        return;
      }
      abortControllerRef.current?.abort();
      viewReview(null);
      setSession(null);
      setFollowupContext(null);
      setError("");
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => () => {
    abortControllerRef.current?.abort();
    fixPreviewControllerRef.current?.abort();
  }, []);


  useEffect(() => {
    persistDeepSeekSettings(deepSeekSettings, authSession?.user.user_id);
    if (validatedDeepSeekKey && validatedDeepSeekKey !== deepSeekSettings.apiKey) {
      setValidatedDeepSeekKey("");
      setDeepSeekModels([]);
    }
  }, [authSession?.user.user_id, deepSeekSettings, validatedDeepSeekKey]);

  useEffect(() => {
    followupLoadSequenceRef.current += 1;
    followupContextIdentityRef.current = null;
    setFollowups([]);
    setFollowupError("");
    setFollowupContext(null);
  }, [session?.review_id, session?.status]);

  const selectedModelProfile =
    modelProfiles.find((item) => item.profile_id === selectedModelProfileId)
    ?? fallbackModelProfiles[0];
  const activeModel = session?.model ?? selectedModelProfile;
  const assistantName = session?.model.display_name
    ?? (selectedModelProfile.provider === "deepseek"
      ? deepSeekSettings.selectionMode === "manual"
        ? deepSeekModels.find((item) => item.id === deepSeekSettings.manualModel)?.display_name
          ?? selectedModelProfile.model
        : "DeepSeek \u81ea\u52a8\u9009\u62e9"
      : selectedModelProfile.model);
  const activeModelProfile = modelProfiles.find(
    (item) => item.profile_id === activeModel.profile_id,
  ) ?? selectedModelProfile;

  const activeHealthInstances = useMemo(() => {
    if (!health) return [];
    const prefix = activeModel.provider === "deepseek"
      ? "deepseek-api-"
      : `${activeModel.profile_id}-`;
    return health.instances.filter((item) => item.endpoint_id.startsWith(prefix));
  }, [activeModel.profile_id, activeModel.provider, health]);

  const healthSummary = useMemo(() => {
    if (activeModel.provider === "deepseek") {
      if (!activeModelProfile.available) return "未配置";
      if (!deepSeekSettings.apiKey) return "待填写密钥";
      if (validatedDeepSeekKey !== deepSeekSettings.apiKey) return "待验证";
    }
    if (healthUnavailable) return "状态暂不可用";
    if (!health) return "连接中";
    if (activeHealthInstances.length === 0) return "未连接";
    if (activeHealthInstances.some((item) => item.available === false)) {
      return activeHealthInstances.every((item) => item.reason_code === "circuit_open")
        ? "恢复中"
        : "未运行";
    }
    if (activeHealthInstances.some((item) => item.circuit_open)) return "熔断";
    if (isReviewing || activeHealthInstances.some((item) => item.inflight_requests > 0)) {
      return "使用中";
    }
    return "可用";
  }, [activeHealthInstances, activeModel.provider, activeModelProfile.available, deepSeekSettings.apiKey, health, healthUnavailable, isReviewing, validatedDeepSeekKey]);

  const localHealthDetail = useMemo(() => {
    if (!health) return null;
    const profiles = [
      ["local-qwen3-8b-", "Qwen3-8B"],
      ["local-qwen3-32b-", "Qwen3-32B"],
    ] as const;
    const details = profiles.flatMap(([prefix, label]) => {
      const states = health.instances.filter((item) => item.endpoint_id.startsWith(prefix));
      if (!states.length) return [];
      const status = states.some((item) => item.available)
        ? "可用"
        : states.every((item) => item.reason_code === "circuit_open")
          ? "正在恢复"
          : states.some((item) => item.reason_code === "timeout")
            ? "连接超时"
            : "未运行";
      return [`${label}（${status}）`];
    });
    if (!details.length || details.every((item) => item.endsWith("（可用）"))) return null;
    return `本地模型服务未运行或正在恢复：${details.join("，")}`;
  }, [health]);

  const activeFile = session?.files.find((item) => item.file_id === activeFileId) ?? session?.files[0];
  const activeFindings = session?.findings.filter((item) => item.file_id === activeFile?.file_id) ?? [];
  const isReviewDetail = viewedReviewId !== null;
  const draftTextError = draftCode ? validateSourceText(draftCode) : null;
  const failedReviewUsesRecheck = session?.status === "failed";
  const processedFindings = useMemo(() => {
    if (!session) return [];
    const activeIds = new Set(session.findings.map((item) => item.finding_id));
    return Object.entries(session.finding_decisions ?? {}).flatMap(([findingId, decision]) => {
      if (activeIds.has(findingId)) return [];
      const finding = session.decided_findings?.[findingId];
      if (!finding) return [];
      const audit = [...(session.finding_decision_history ?? [])]
        .reverse()
        .find((item) => item.finding_id === findingId && item.action === "decided");
      return [{ finding, decision, audit }];
    });
  }, [session]);

  async function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    if (!selected.length) return;
    const selectedPaths = selected.map((file) => (
      (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
    ));
    const selectedNames = new Set(selectedPaths);
    const duplicatePaths = duplicateCanonicalPaths(selectedPaths);
    if (duplicatePaths.length) {
      setError(`存在重复或跨平台会冲突的文件路径，请重命名后重试：${duplicatePaths.join("、")}`);
      event.target.value = "";
      return;
    }
    const oversized = selected.find((file) => file.size > MAX_SOURCE_FILE_BYTES);
    if (oversized) {
      setError(`${oversized.name}：单个代码文件不能超过 2 MiB。`);
      event.target.value = "";
      return;
    }
    const retainedBytes = attachments
      .filter((item) => !selectedNames.has(item.filename))
      .reduce((total, item) => total + item.size, 0);
    if (retainedBytes + selected.reduce((total, file) => total + file.size, 0)
        > MAX_REVIEW_SOURCE_BYTES) {
      setError("一次审查的代码总量不能超过 8 MiB。");
      event.target.value = "";
      return;
    }
    let additions: DraftAttachment[];
    try {
      additions = await Promise.all(
        selected.map(async (file, index) => {
          const content = await readSourceFileStrict(file);
          const unsafeText = validateSourceText(content);
          if (unsafeText) throw new Error(`${file.name}：${unsafeText}`);
          return {
            id: [file.name, file.size, file.lastModified, index].join("-"),
            filename: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
            content,
            size: file.size,
            source: "local" as const,
          };
        }),
      );
    } catch (readError) {
      setError(readError instanceof Error ? readError.message : "文件读取失败，请确认文件是 UTF-8 文本。");
      event.target.value = "";
      return;
    }
    setAttachments((current) => mergeLocalAttachments(current, additions));
    setReviewOrigin((current) => removeOriginPaths(current, selectedNames));
    setLocalDiffBaseFiles((current) => current.filter((item) => !selectedNames.has(item.filename)));
    event.target.value = "";
    setError("");
  }

  function removeAttachment(id: string) {
    const removed = attachments.find((item) => item.id === id);
    setAttachments((current) => current.filter((item) => item.id !== id));
    if (removed?.source === "gitlab" || removed?.source === "local-diff") {
      setReviewOrigin((current) => removeOriginPaths(current, [removed.filename]));
      setLocalDiffBaseFiles((current) => (
        current.filter((item) => item.filename !== removed.filename)
      ));
    }
  }

  function saveGitLabAccount(account: SavedGitLabAccount) {
    setGitLabAccounts((current) => {
      const next = [
        ...current.filter((item) => item.account_id !== account.account_id),
        account,
      ];
      persistGitLabAccounts(next);
      return next;
    });
    setActiveGitLabAccountId(account.account_id);
    persistActiveGitLabAccountId(account.account_id);
    if (resumeGitLabImportAfterAccount) {
      authSession?.closeAccountSettings();
      setResumeGitLabImportAfterAccount(false);
      setGitLabDialogOpen(true);
    }
  }

  function activateGitLabAccount(accountId: string) {
    setActiveGitLabAccountId(accountId);
    persistActiveGitLabAccountId(accountId);
  }

  function deleteGitLabAccount(accountId: string) {
    setGitLabAccounts((current) => {
      const next = current.filter((item) => item.account_id !== accountId);
      persistGitLabAccounts(next);
      if (activeGitLabAccountId === accountId) {
        const nextActive = next[0]?.account_id ?? null;
        setActiveGitLabAccountId(nextActive);
        persistActiveGitLabAccountId(nextActive);
      }
      return next;
    });
  }

  function importGitLabFiles(preview: GitLabMergeRequestPreview, files: GitLabFileChange[]) {
    const additions: DraftAttachment[] = files.flatMap((file) => file.new_content === null ? [] : [{
      id: ["gitlab", preview.project_id, preview.merge_request_iid, preview.head_sha, file.new_path].join("-"),
      filename: file.new_path,
      content: file.new_content,
      size: new TextEncoder().encode(file.new_content).length,
      source: "gitlab",
    }]);
    setAttachments((current) => replaceGitLabAttachments(
      current.filter((item) => item.source !== "local-diff"),
      additions,
    ));
    setLocalDiffBaseFiles([]);
    setReviewOrigin({
      type: "gitlab",
      gitlab_host: preview.gitlab_host,
      project_id: preview.project_id,
      project_path: preview.project_path,
      merge_request_iid: preview.merge_request_iid,
      merge_request_url: preview.web_url,
      base_sha: preview.base_sha,
      head_sha: preview.head_sha,
      selected_paths: additions.map((item) => item.filename),
      changed_ranges: Object.fromEntries(
        files.map((file) => [file.new_path, file.changed_ranges]),
      ),
    });
    setGitLabDialogOpen(false);
    setError("");
  }

  function importLocalDiff(preview: LocalDiffPreview, oldContent: string) {
    const change = preview.files[0];
    if (!change?.selectable || !change.language) return;
    const addition: DraftAttachment = {
      id: ["local-diff", change.new_sha256, change.new_path].join("-"),
      filename: change.new_path,
      content: change.new_content,
      size: new TextEncoder().encode(change.new_content).length,
      source: "local-diff",
      comparison: {
        oldLabel: preview.old_label,
        newLabel: preview.new_label,
        changedRangeCount: change.changed_ranges.length,
      },
    };
    setAttachments((current) => replaceLocalDiffAttachments(
      current.filter((item) => item.source !== "gitlab"),
      [addition],
    ));
    setLocalDiffBaseFiles([{
      filename: change.new_path,
      language: change.language,
      content: oldContent,
    }]);
    setReviewOrigin({
      type: "local_diff",
      old_label: preview.old_label,
      new_label: preview.new_label,
      selected_paths: [change.new_path],
      changed_ranges: { [change.new_path]: change.changed_ranges },
      old_sha256: { [change.new_path]: change.old_sha256 },
      new_sha256: { [change.new_path]: change.new_sha256 },
    });
    setLocalDiffDialogOpen(false);
    setError("");
  }

  async function openHistory(reset = true) {
    if (historyLoadingRef.current) return;
    historyLoadingRef.current = true;
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const offset = reset ? 0 : historyItems.length;
      const result = await listReviewSessions(HISTORY_PAGE_SIZE, offset);
      setHistoryItems((current) => {
        const source = reset ? [] : current;
        const seen = new Set(source.map((item) => item.review_id));
        return [
          ...source,
          ...result.items.filter((item) => !seen.has(item.review_id)),
        ];
      });
      setHistoryHasMore(result.items.length === result.limit);
      setHistoryRetryReset(false);
    } catch (historyError) {
      setHistoryRetryReset(reset);
      setHistoryError(
        historyError instanceof Error
          ? historyError.message
          : "加载历史记录失败。",
      );
    } finally {
      historyLoadingRef.current = false;
      setHistoryLoading(false);
    }
  }

  async function openHistorySession(
    reviewId: string,
    findingId?: string,
    updateRoute = true,
  ) {
    abortControllerRef.current?.abort();
    viewReview(reviewId);
    if (updateRoute) window.history.pushState({}, "", `/reviews/${reviewId}`);
    setSession(null);
    setFollowupContext(null);
    setError("");
    try {
      const restored = await getReviewSession(reviewId);
      if (viewedReviewIdRef.current !== reviewId) return;
      displaySession(restored);
      const selectFinding = (result: ReviewSession) => {
        if (!findingId) return;
        const finding = result.findings.find((item) => item.finding_id === findingId);
        if (finding) {
          setActiveFileId(finding.file_id);
          setSelectedFindingId(finding.finding_id);
        } else {
          setError("该问题已处理或不存在，已显示当前审查结果。");
        }
      };
      selectFinding(restored);
      if (!["completed", "failed", "cancelled"].includes(restored.status)) {
        const completed = await attachToReview(reviewId, undefined, "正在重新连接审查进度");
        if (completed && viewedReviewIdRef.current === reviewId) selectFinding(completed);
      }
    } catch (historyError) {
      if (historyError instanceof DOMException && historyError.name === "AbortError") return;
      if (viewedReviewIdRef.current === reviewId) {
        if (historyError instanceof ApiError && historyError.code === "stream_reconnect_exhausted") {
          setDisconnectedReviewId(reviewId);
        }
        setError(historyError instanceof Error ? historyError.message : "加载审查记录失败。");
      }
    }
  }
  async function renameHistorySession(reviewId: string, title: string) {
    setError("");
    try {
      const renamed = await renameReviewSession(reviewId, title);
      setHistoryItems((current) =>
        current.map((item) => item.review_id === reviewId ? { ...item, title: renamed.title } : item),
      );
      if (session?.review_id === reviewId) setSession(renamed);
    } catch (historyError) {
      setError(historyError instanceof Error ? historyError.message : "重命名失败，请稍后重试。");
    }
  }

  async function deleteHistorySession(reviewId: string) {
    setError("");
    try {
      await deleteReviewSession(reviewId);
      setHistoryItems((current) => current.filter((item) => item.review_id !== reviewId));
      setHistoryHasMore(true);
      await openHistory(true);
      if (session?.review_id === reviewId) resetWorkspace();
    } catch (historyError) {
      setError(historyError instanceof Error ? historyError.message : "删除失败，请稍后重试。");
    }
  }


  function toFollowupPayload(context: FollowupContext) {
    return {
      kind: context.kind,
      file_id: context.fileId,
      finding_id: context.findingId,
      start_line: context.startLine,
      end_line: context.endLine,
      selected_code: context.selectedCode,
    };
  }

  function followupIdentity(reviewId: string, context: FollowupContext) {
    return `${reviewId}:${JSON.stringify(toFollowupPayload(context))}`;
  }

  async function openFollowupContext(context: FollowupContext) {
    if (!session || session.status !== "completed") return;
    const reviewId = session.review_id;
    const identity = followupIdentity(reviewId, context);
    const sequence = ++followupLoadSequenceRef.current;
    followupContextIdentityRef.current = identity;
    setFollowupContext(context);
    setFollowups([]);
    setFollowupError("");
    setFollowupBusy(false);
    try {
      const messages = await getReviewFollowups(reviewId, toFollowupPayload(context));
      if (
        sequence === followupLoadSequenceRef.current
        && followupContextIdentityRef.current === identity
        && viewedReviewIdRef.current === reviewId
      ) setFollowups(messages);
    } catch (loadError) {
      if (sequence === followupLoadSequenceRef.current && followupContextIdentityRef.current === identity) {
        setFollowupError(loadError instanceof Error ? loadError.message : "加载当前代码的追问记录失败。");
      }
    }
  }

  function closeFollowup() {
    followupLoadSequenceRef.current += 1;
    followupContextIdentityRef.current = null;
    setFollowupBusy(false);
    setFollowupError("");
    setFollowupContext(null);
  }

  async function submitFollowup(question: string): Promise<boolean> {
    if (!session || session.status !== "completed" || !followupContext) return false;
    const sourceReviewId = session.review_id;
    const sourceContext = followupContext;
    const identity = followupIdentity(sourceReviewId, sourceContext);
    if (followupContextIdentityRef.current !== identity) return false;
    setFollowupBusy(true);
    setFollowupError("");
    try {
      const payload = toFollowupPayload(sourceContext);
      const source = session.files.find((item) => item.file_id === sourceContext.fileId);
      const result = session.model.provider === "deepseek"
        ? await askReviewFollowup(
            sourceReviewId,
            question.trim(),
            payload,
            deepSeekSettings.apiKey.trim(),
            source?.sha256,
          )
        : await askReviewFollowup(sourceReviewId, question.trim(), payload, undefined, source?.sha256);
      if (viewedReviewIdRef.current === sourceReviewId && followupContextIdentityRef.current === identity) {
        if (result.action === "fix_candidate") {
          followupLoadSequenceRef.current += 1;
          followupContextIdentityRef.current = null;
          setFollowupContext(null);
          setFollowups([]);
          setFixCandidate(result.candidate);
          setStageMessage("候选修复已验证，等待确认");
          return true;
        }
        setFollowups((current) => [...current, ...result.messages]);
        return true;
      }
      return false;
    } catch (submitError) {
      if (followupContextIdentityRef.current === identity) {
        setFollowupError(repairFailureMessage(submitError, "发送追问失败。"));
      }
      return false;
    } finally {
      if (followupContextIdentityRef.current === identity) setFollowupBusy(false);
    }
  }
  async function watchReview(reviewId: string, controller: AbortController) {
    return streamReviewSession(
      reviewId,
      (streamEvent) => {
        if (controller.signal.aborted || stoppedControllersRef.current.has(controller)) return;
        if (streamEvent.event === "stage") {
          setStageMessage(String(streamEvent.data.message ?? "正在分析代码"));
        }
        if (streamEvent.event === "chunk") {
          const filename = String(streamEvent.data.target_path ?? streamEvent.data.file ?? "");
          if (filename) setStageMessage("正在审查 " + filename);
        }
        if (streamEvent.event === "finding") {
          setStageMessage("已发现问题，正在校验证据");
        }
        if (streamEvent.event === "progress") {
          const number = (key: string) => Number(streamEvent.data[key] ?? 0);
          setProgress({
            total: number("total"),
            completed: number("completed"),
            queued: number("queued"),
            running: number("running"),
            failed: number("failed"),
            coverage_percent: number("coverage_percent"),
            active_file: streamEvent.data.active_file ? String(streamEvent.data.active_file) : undefined,
          });
        }
      },
      controller.signal,
    );
  }

  async function watchRecheckReview(reviewId: string, outerController: AbortController) {
    const internalController = new AbortController();
    const propagateAbort = () => internalController.abort();
    outerController.signal.addEventListener("abort", propagateAbort, { once: true });
    const terminalStatuses: ReviewSession["status"][] = ["completed", "failed", "cancelled"];
    const waitForNextSnapshot = () => new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, 1000);
      internalController.signal.addEventListener("abort", () => {
        window.clearTimeout(timer);
        reject(new DOMException("stopped", "AbortError"));
      }, { once: true });
    });

    const streamOutcome = watchReview(reviewId, internalController)
      .then((result) => ({ source: "stream-result" as const, result }))
      .catch((error: unknown) => ({ source: "stream-error" as const, error }));
    const snapshotOutcome = (async () => {
      let deadline = Date.now() + 65_000;
      while (!internalController.signal.aborted) {
        const snapshot = await getReviewSession(reviewId, internalController.signal);
        if (terminalStatuses.includes(snapshot.status)) return snapshot;
        if (snapshot.recheck_deadline_at) {
          const serverDeadline = Date.parse(snapshot.recheck_deadline_at);
          if (Number.isFinite(serverDeadline)) deadline = serverDeadline + 3_000;
        }
        if (Date.now() >= deadline) {
          const finalSnapshot = await getReviewSession(reviewId, internalController.signal);
          if (terminalStatuses.includes(finalSnapshot.status)) return finalSnapshot;
          throw new ApiError(
            "统一复查已超过服务器门限，修改仍已保留。请稍后点击“重新复查”。",
            0,
            "recheck_snapshot_timeout",
          );
        }
        await waitForNextSnapshot();
      }
      throw new DOMException("stopped", "AbortError");
    })();

    try {
      const first = await Promise.race([streamOutcome, snapshotOutcome]);
      if ("source" in first && first.source === "stream-result") return first.result;
      if ("source" in first && first.source === "stream-error") return await snapshotOutcome;
      return first;
    } finally {
      internalController.abort();
      outerController.signal.removeEventListener("abort", propagateAbort);
    }
  }

  async function startArtifactDownload(
    key: string,
    path: string,
    fallback: string,
    label: string,
  ) {
    if (artifactBusyKey) return;
    setArtifactBusyKey(key);
    setArtifactState(null);
    try {
      const filename = await downloadArtifact(path, fallback);
      setArtifactState({ key, path, fallback, label, message: `已开始下载 ${filename}`, error: false });
    } catch (downloadError) {
      setArtifactState({
        key,
        path,
        fallback,
        label,
        message: downloadError instanceof Error ? downloadError.message : "下载失败，请重试。",
        error: true,
      });
    } finally {
      setArtifactBusyKey(null);
    }
  }

  async function reopenFinding(findingId: string) {
    if (!session || reopenBusyId) return;
    const reviewId = session.review_id;
    setReopenBusyId(findingId);
    setError("");
    try {
      const result = await reopenReviewFinding(reviewId, findingId);
      if (viewedReviewIdRef.current === reviewId) {
        displaySession(result.session);
        const reopened = result.session.findings.find((item) => item.finding_id === findingId);
        if (reopened) {
          setActiveFileId(reopened.file_id);
          setSelectedFindingId(reopened.finding_id);
        }
        if (result.revision_retained) {
          setError("问题已恢复为待处理；此前应用的代码修订仍然保留，可重新检查或生成新候选。 ");
        }
      }
    } catch (reopenError) {
      setError(reopenError instanceof Error ? reopenError.message : "重新打开问题失败，请重试。 ");
    } finally {
      setReopenBusyId(null);
    }
  }

  async function attachToReview(
    reviewId: string,
    suppliedController?: AbortController,
    initialStage = "正在连接审查进度",
    recheckSnapshotFallback = false,
  ): Promise<ReviewSession | null> {
    const controller = suppliedController ?? new AbortController();
    if (abortControllerRef.current && abortControllerRef.current !== controller) {
      abortControllerRef.current.abort();
    }
    abortControllerRef.current = controller;
    reviewIdRef.current = reviewId;
    setRunningReviewId(reviewId);
    setIsReviewing(true);
    setStageMessage(initialStage);
    setDisconnectedReviewId(null);
    try {
      const result = recheckSnapshotFallback
        ? await watchRecheckReview(reviewId, controller)
        : await watchReview(reviewId, controller);
      if (controller.signal.aborted || stoppedControllersRef.current.has(controller)) return null;
      if (viewedReviewIdRef.current === reviewId) displaySession(result);
      return result;
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
        reviewIdRef.current = null;
        setRunningReviewId(null);
        setIsReviewing(false);
        setStageMessage("");
        setProgress(null);
      }
    }
  }

  function displaySession(result: ReviewSession) {
    setSession(result);
    setActiveFileId(result.files[0]?.file_id ?? null);
    setSelectedFindingId(result.findings[0]?.finding_id ?? null);
    if (result.status === "failed") {
      setError(result.error || "审查未完整完成，可继续未完成的部分。");
      return false;
    }
    return result.status === "completed";
  }

  async function validateDeepSeekKey() {
    const apiKey = deepSeekSettings.apiKey.trim();
    if (!apiKey) {
      setError("请先填写 DeepSeek API Key。");
      return [];
    }
    setDeepSeekValidationBusy(true);
    setError("");
    try {
      const models = await getDeepSeekModels(apiKey);
      setDeepSeekModels(models);
      setValidatedDeepSeekKey(apiKey);
      setDeepSeekSettings((current) => ({
        ...current,
        manualModel: models.some((item) => item.id === current.manualModel)
          ? current.manualModel
          : (models[0]?.id ?? ""),
      }));
      return models;
    } catch (validationError) {
      setValidatedDeepSeekKey("");
      setDeepSeekModels([]);
      setError(validationError instanceof Error ? validationError.message : "DeepSeek API Key 验证失败。");
      return [];
    } finally {
      setDeepSeekValidationBusy(false);
    }
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isReviewDetail) {
      setError("正在查看审查记录；请先点击“新建审查”再提交代码。");
      return;
    }
    const files = [];
    const unsafeDraft = draftCode ? validateSourceText(draftCode) : null;
    if (unsafeDraft) {
      setError(unsafeDraft);
      return;
    }
    const trimmedCode = draftCode.trim();
    const pastedBytes = new TextEncoder().encode(trimmedCode).length;
    if (pastedBytes > MAX_SOURCE_FILE_BYTES) {
      setError("粘贴的代码不能超过 2 MiB。");
      return;
    }
    if (pastedBytes + attachments.reduce((total, item) => total + item.size, 0)
        > MAX_REVIEW_SOURCE_BYTES) {
      setError("一次审查的代码总量不能超过 8 MiB。");
      return;
    }
    if (trimmedCode) {
      const detected = detectLanguage(trimmedCode);
      if (!detected.language) {
        setError(detected.error ?? "无法识别代码语言。");
        return;
      }
      files.push({
        filename: uniqueSnippetFilename(detected.language, attachments.map((item) => item.filename)),
        language: detected.language,
        content: trimmedCode,
      });
    }
    for (const attachment of attachments) {
      const detected = detectLanguage(attachment.content, attachment.filename);
      if (!detected.language) {
        setError(attachment.filename + "：" + (detected.error ?? "文件类型不受支持。"));
        return;
      }
      files.push({ filename: attachment.filename, language: detected.language, content: attachment.content });
    }
    if (!files.length) {
      setError("请先输入需要审查的代码，或添加 Python/C++ 文件。");
      return;
    }
    if (selectedModelProfileId === "deepseek-api") {
      const models = validatedDeepSeekKey === deepSeekSettings.apiKey.trim()
        ? deepSeekModels
        : await validateDeepSeekKey();
      if (!models.length) return;
      if (deepSeekSettings.selectionMode === "manual"
          && !models.some((item) => item.id === deepSeekSettings.manualModel)) {
        setError("请选择当前 DeepSeek 账户可用的模型。");
        return;
      }
    }
    const controller = new AbortController();
    let createdReviewId: string | null = null;
    abortControllerRef.current = controller;
    setIsReviewing(true);
    viewReview(null);
    setSession(null);
    setSelectedFindingId(null);
    setStageMessage("正在准备审查");
    setProgress(null);
    setError("");
    try {
      const mode = files.length === 1 ? (trimmedCode ? "paste" : "single") : "project";
      const common = {
        origin: reviewOrigin ?? undefined,
        ...(reviewOrigin?.type === "local_diff" ? {
          local_diff_base_files: localDiffBaseFiles,
        } : {}),
        model_profile_id: selectedModelProfileId,
        ...(selectedModelProfileId === "deepseek-api" ? {
          deepseek_selection_mode: deepSeekSettings.selectionMode,
          deepseek_model: deepSeekSettings.selectionMode === "manual"
            ? deepSeekSettings.manualModel
            : undefined,
        } : {}),
      };
      const payload: ReviewCreatePayload = mode === "project"
        ? { files, ...common }
        : {
            filename: files[0].filename,
            language: files[0].language,
            content: files[0].content,
            ...common,
          };
      const created = selectedModelProfileId === "deepseek-api"
        ? await createReviewSession(mode, payload, deepSeekSettings.apiKey.trim())
        : await createReviewSession(mode, payload);
      createdReviewId = created.review_id;
      if (stoppedControllersRef.current.has(controller)) {
        await cancelReviewSession(created.review_id);
        return;
      }
      setAttachments([]);
      setReviewOrigin(null);
      setLocalDiffBaseFiles([]);
      reviewIdRef.current = created.review_id;
      setRunningReviewId(created.review_id);
      if (viewedReviewIdRef.current === null) {
        viewReview(created.review_id);
      }
      await openHistory();
      const completed = await attachToReview(created.review_id, controller, "正在连接审查进度");
      if (controller.signal.aborted || stoppedControllersRef.current.has(controller)) return;
      if (!completed) return;
      if (completed.status === "completed") {
        setDraftCode("");
        setAttachments([]);
        setReviewOrigin(null);
        setLocalDiffBaseFiles([]);
      }
    } catch (reviewError) {
      if (reviewError instanceof DOMException && reviewError.name === "AbortError") return;
      if (reviewError instanceof ApiError && reviewError.code === "stream_reconnect_exhausted" && createdReviewId) {
        setDisconnectedReviewId(createdReviewId);
      }
      if (createdReviewId && viewedReviewIdRef.current === createdReviewId) {
        viewReview(null);
        setSession(null);
        window.history.replaceState({}, "", "/");
      }
      setError(reviewError instanceof Error ? reviewError.message : "审查请求失败，请稍后重试。");
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
        reviewIdRef.current = null;
        setIsReviewing(false);
        setStageMessage("");
      }
    }
  }

  async function resumeFailedReview() {
    if (!session || session.status !== "failed") return;
    const controller = new AbortController();
    abortControllerRef.current = controller;
    reviewIdRef.current = session.review_id;
    setIsReviewing(true);
    const pendingRevalidation = Object.values(session.finding_states ?? {})
      .some((state) => state === "fixed_pending_revalidation");
    const recheckAttemptPresent = Boolean(
      session.recheck_attempt_id
      || session.recheck_attempt_status
      || session.recheck_deadline_at,
    );
    const recheckFailureMessage = [session.error, session.summary.text]
      .filter((value): value is string => Boolean(value))
      .some((value) => value.includes("统一复查"));
    const isRecheckResume = pendingRevalidation || recheckAttemptPresent || recheckFailureMessage;
    const resumeMessage = isRecheckResume
      ? "修复已保留，正在重新复查修改后的代码"
      : "正在恢复未完成的分块";
    setStageMessage(resumeMessage);
    setProgress(null);
    setError("");
    try {
      const resumeRequest = Promise.resolve(
        session.model.provider === "deepseek"
          ? resumeReviewSession(session.review_id, deepSeekSettings.apiKey.trim())
          : resumeReviewSession(session.review_id),
      );
      void resumeRequest.catch((resumeError: unknown) => {
        if (!controller.signal.aborted) {
          setError(resumeError instanceof Error ? resumeError.message : "重新复查请求失败，请稍后重试。 ");
        }
      });
      await attachToReview(session.review_id, controller, resumeMessage, isRecheckResume);
    } catch (reviewError) {
      if (reviewError instanceof DOMException && reviewError.name === "AbortError") return;
      if (reviewError instanceof ApiError && reviewError.code === "stream_reconnect_exhausted") {
        setDisconnectedReviewId(session.review_id);
      }
      setError(reviewError instanceof Error ? reviewError.message : "恢复审查失败，请稍后重试。");
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
        reviewIdRef.current = null;
        setIsReviewing(false);
        setStageMessage("");
        setProgress(null);
      }
    }
  }

  function stopReview() {
    const reviewId = reviewIdRef.current;
    const controller = abortControllerRef.current;
    if (controller) stoppedControllersRef.current.add(controller);
    if (reviewId) controller?.abort();
    abortControllerRef.current = null;
    reviewIdRef.current = null;
    setIsReviewing(false);
    setStageMessage("");
    if (reviewId && viewedReviewIdRef.current === reviewId) {
      viewReview(null);
      setSession(null);
      window.history.replaceState({}, "", "/");
    }
    if (reviewId) {
      void cancelReviewSession(reviewId).catch((cancelError) => {
        setError(
          cancelError instanceof Error
            ? `停止请求失败：${cancelError.message}`
            : "停止请求失败，后台审查可能仍在运行。",
        );
        void openHistory();
      });
    }
  }

  async function openRevisionHistory() {
    if (!session) return;
    setRevisionDialogOpen(true);
    setError("");
    try {
      setRevisions(await getReviewRevisions(session.review_id));
    } catch (revisionError) {
      setRevisionDialogOpen(false);
      setError(revisionError instanceof Error ? revisionError.message : "加载修改历史失败。");
    }
  }

  async function undoRevision(revisionId: string) {
    if (!session || isReviewing || revisionBusyId) return;
    const sourceReviewId = session.review_id;
    const controller = new AbortController();
    setRevisionBusyId(revisionId);
    setError("");
    try {
      const restarted = session.model.provider === "deepseek"
        ? await undoReviewRevision(sourceReviewId, revisionId, deepSeekSettings.apiKey.trim())
        : await undoReviewRevision(sourceReviewId, revisionId);
      setRevisionDialogOpen(false);
      abortControllerRef.current = controller;
      reviewIdRef.current = restarted.review_id;
      setRunningReviewId(restarted.review_id);
      setIsReviewing(true);
      viewReview(restarted.review_id);
      displaySession(restarted);
      setStageMessage("修改已撤销，正在重新检查当前代码");
      setProgress(null);
      const completed = await watchReview(restarted.review_id, controller);
      if (!controller.signal.aborted && !stoppedControllersRef.current.has(controller)) {
        if (viewedReviewIdRef.current === restarted.review_id) displaySession(completed);
        await openHistory();
      }
    } catch (revisionError) {
      if (!(revisionError instanceof DOMException && revisionError.name === "AbortError")) {
        setError(revisionError instanceof Error ? revisionError.message : "撤销修改失败。");
      }
    } finally {
      setRevisionBusyId(null);
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
        reviewIdRef.current = null;
        setRunningReviewId(null);
        setIsReviewing(false);
        setStageMessage("");
        setProgress(null);
      }
    }
  }

  function resetWorkspace() {
    stopReview();
    viewReview(null);
    setGitLabDialogOpen(false);
    setLocalDiffDialogOpen(false);
    setSession(null);
    setError("");
    setSelectedFindingId(null);
    setFollowups([]);
    setFollowupContext(null);
    window.history.replaceState({}, "", "/");
  }

  function navigateToFinding(finding: SessionFinding) {
    setActiveFileId(finding.file_id);
    setSelectedFindingId(finding.finding_id);
    if (session) {
      window.history.pushState({}, "", `/reviews/${session.review_id}/findings/${finding.finding_id}`);
    }
    window.requestAnimationFrame(() => {
      document
        .getElementById(`code-line-${finding.file_id}-${finding.start_line}`)
        ?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    });
  }

  function openFindingFollowup(finding: SessionFinding) {
    if (selectedFindingId !== finding.finding_id) {
      navigateToFinding(finding);
      return;
    }
    const file = session?.files.find((item) => item.file_id === finding.file_id);
    void openFollowupContext({
      kind: "finding",
      title: finding.title,
      fileId: finding.file_id,
      findingId: finding.finding_id,
      startLine: finding.start_line,
      endLine: finding.end_line,
      selectedCode: finding.evidence,
      detail: [
        `${file?.relative_path ?? finding.file_id}:${finding.start_line}-${finding.end_line}`,
        `证据：${finding.evidence}`,
        `建议：${finding.suggestion}`,
      ].join("\n"),
    });
  }

  function openSelectionFollowup(selection: CodeSelection) {
    if (!activeFile || session?.status !== "completed") return;
    setSelectedFindingId(null);
    void openFollowupContext({
      kind: "selection",
      title: `所选代码 · ${activeFile.relative_path}`,
      fileId: activeFile.file_id,
      startLine: selection.start_line,
      endLine: selection.end_line,
      selectedCode: selection.text,
      detail: [
        selection.start_line
          ? `${activeFile.relative_path}:${selection.start_line}-${selection.end_line ?? selection.start_line}`
          : activeFile.relative_path,
        selection.text,
      ].join("\n"),
    });
  }

  async function decideFinding(finding: SessionFinding, decision: "accepted_risk" | "deferred" | "dismissed") {
    if (
      !session
      || !["completed", "failed"].includes(session.status)
      || isReviewing
    ) return;
    const sourceReviewId = session.review_id;
    setFixBusyId(finding.finding_id);
    setError("");
    try {
      const result = await decideReviewFinding(sourceReviewId, finding.finding_id, decision);
      if (viewedReviewIdRef.current === sourceReviewId) {
        setSession(result.session);
      }
      await openHistory();
      if (result.revised_review) {
        const revised = result.revised_review;
        const controller = new AbortController();
        abortControllerRef.current = controller;
        reviewIdRef.current = revised.review_id;
        setRunningReviewId(revised.review_id);
        setIsReviewing(true);
        viewReview(revised.review_id);
        displaySession(revised);
        setStageMessage("所有当前问题已决策，正在统一复查代码");
        const completed = await watchReview(revised.review_id, controller);
        if (!controller.signal.aborted && !stoppedControllersRef.current.has(controller)) {
          displaySession(completed);
          await openHistory();
        }
      }
    } catch (decisionError) {
      if (!(decisionError instanceof DOMException && decisionError.name === "AbortError")) {
        setError(decisionError instanceof Error ? decisionError.message : "处理修复建议失败。");
      }
    } finally {
      setFixBusyId(null);
      abortControllerRef.current = null;
      reviewIdRef.current = null;
      setRunningReviewId(null);
      setIsReviewing(false);
      setProgress(null);
    }
  }

  async function generateFixPreview(finding: SessionFinding) {
    if (!session || !["completed", "failed"].includes(session.status) || isReviewing || repairIntent) return;
    setFixBusyId(finding.finding_id);
    setStageMessage("正在生成候选修复并执行静态验证");
    setError("");
    const controller = new AbortController();
    fixPreviewControllerRef.current = controller;
    try {
      const candidate = session.model.provider === "deepseek"
        ? await previewReviewFix(session.review_id, finding.finding_id, deepSeekSettings.apiKey.trim(), controller.signal)
        : await previewReviewFix(session.review_id, finding.finding_id, undefined, controller.signal);
      setFixCandidate(candidate);
      setStageMessage("候选修复已验证，等待确认");
    } catch (previewError) {
      if (previewError instanceof DOMException && previewError.name === "AbortError") {
        setError("已停止等待候选生成；服务端任务可能继续到结束，但结果不会写入审查会话。可稍后重新生成。");
      } else if (
        previewError instanceof ApiError
        && ["needs_intent", "ambiguous_symbol"].includes(previewError.code ?? "")
      ) {
        const evidence = (
          previewError.details?.use_def_evidence ?? finding.use_def_evidence
        ) as UseDefEvidence | undefined;
        const source = session.files.find((item) => item.file_id === finding.file_id);
        const baseSha = typeof previewError.details?.base_sha === "string"
          ? previewError.details.base_sha
          : source?.sha256;
        if (evidence && baseSha) {
          setRepairIntent({ finding, evidence, baseSha });
          setError("");
        } else {
          setError(previewError.message);
        }
      } else {
        setError(repairFailureMessage(previewError, "生成候选修复失败，会话未改变。"));
      }
      setStageMessage("");
    } finally {
      if (fixPreviewControllerRef.current === controller) fixPreviewControllerRef.current = null;
      setFixBusyId(null);
    }
  }

  async function generateIntentFix(option: RepairIntentOption, value?: string) {
    if (!session || !repairIntent) return;
    if (option.kind === "defer") {
      const finding = repairIntent.finding;
      setRepairIntent(null);
      await decideFinding(finding, "deferred");
      return;
    }
    const controller = new AbortController();
    setFixBusyId(repairIntent.finding.finding_id);
    setError("");
    try {
      const candidate = await previewReviewFixWithIntent(
        session.review_id,
        repairIntent.finding.finding_id,
        {
          review_id: session.review_id,
          finding_id: repairIntent.finding.finding_id,
          base_sha: repairIntent.baseSha,
          option_id: option.option_id,
          intent_kind: option.kind,
          selected_symbol: option.kind === "import_symbol"
            ? repairIntent.evidence.unresolved_name
            : option.symbol ?? (option.kind === "declare_parameter" ? repairIntent.evidence.unresolved_name : null),
          import_source: option.module ?? null,
          initializer: option.kind === "declare_local" ? value ?? null : null,
          user_intent: option.kind === "custom_behavior" ? value ?? null : null,
        },
        controller.signal,
      );
      setRepairIntent(null);
      setFixCandidate(candidate);
      setStageMessage("候选修复已验证，等待确认");
    } catch (intentError) {
      setError(repairFailureMessage(intentError, "无法按所选意图生成候选。"));
    } finally {
      setFixBusyId(null);
    }
  }

  function stopFixPreviewGeneration() {
    fixPreviewControllerRef.current?.abort();
  }

  async function cancelFixPreview() {
    if (!fixCandidate || fixConfirmationBusy) return;
    setFixConfirmationBusy(true);
    try {
      await cancelReviewFix(fixCandidate.review_id, fixCandidate.candidate_id);
      setFixCandidate(null);
      setStageMessage("");
    } catch (cancelError) {
      setError(cancelError instanceof Error ? cancelError.message : "取消候选修复失败。");
    } finally {
      setFixConfirmationBusy(false);
    }
  }

  async function applyFixCandidate() {
    if (!fixCandidate || fixConfirmationBusy) return;
    const sourceReviewId = fixCandidate.review_id;
    let fixWasApplied = false;
    setFixConfirmationBusy(true);
    setStageMessage("正在确认写入修复并准备统一复查");
    setError("");
    try {
      const result = await confirmReviewFix(sourceReviewId, fixCandidate.candidate_id);
      fixWasApplied = true;
      if (viewedReviewIdRef.current === sourceReviewId) displaySession(result.session);
      await openHistory();
      if (!result.revised_review) {
        setFixCandidate(null);
        setStageMessage("修复已应用；可继续处理其它问题");
        return;
      }
      const revised = result.revised_review;
      const controller = new AbortController();
      abortControllerRef.current = controller;
      reviewIdRef.current = revised.review_id;
      setRunningReviewId(revised.review_id);
      setIsReviewing(true);
      viewReview(revised.review_id);
      displaySession(revised);
      setStageMessage("所有当前问题已决策，正在统一复查代码");
      const completed = await watchReview(revised.review_id, controller);
      if (controller.signal.aborted || stoppedControllersRef.current.has(controller)) {
        throw new Error("统一复查已中止，请从审查记录查看当前修订状态。");
      }
      displaySession(completed);
      if (completed.status !== "completed") {
        throw new Error(completed.error || "统一复查未能完整完成。");
      }
      setFixCandidate(null);
      await openHistory();
    } catch (confirmError) {
      if (fixWasApplied) setFixCandidate(null);
      const detail = confirmError instanceof Error ? confirmError.message : "统一复查未能完成。";
      setError(
        fixWasApplied
          ? `修复已应用，但统一复查失败：${detail}`
          : detail || "应用候选修复失败，会话未改变。",
      );
    } finally {
      setFixConfirmationBusy(false);
      abortControllerRef.current = null;
      reviewIdRef.current = null;
      setRunningReviewId(null);
      setIsReviewing(false);
      setProgress(null);
    }
  }

  function closeAccountSettings() {
    authSession?.closeAccountSettings();
    if (resumeGitLabImportAfterAccount) { setResumeGitLabImportAfterAccount(false); setGitLabDialogOpen(true); }
  }


  return (
    <>
    <main className="app-shell" hidden={Boolean(authSession?.accountSettingsOpen)}>
      <aside className="sidebar sidebar-left">
        <div className="brand-area">
          <ProductBrand subtitle="星鉴" />
          <label className="model-switcher">
            <span>审查模型</span>
            <select
              aria-label="审查模型"
              value={selectedModelProfileId}
              disabled={isReviewing}
              onChange={(event) => {
                const profileId = event.target.value;
                setSelectedModelProfileId(profileId);
                setDeepSeekSettings((current) => ({ ...current, preferredProfileId: profileId }));
                setError("");
              }}
            >
              {modelProfiles.map((profile) => (
                <option
                  key={profile.profile_id}
                  value={profile.profile_id}
                  disabled={!profile.available}
                >
                  {profile.display_name}{profile.available ? "" : "（未配置）"}
                </option>
              ))}
            </select>
            <small>
              {selectedModelProfile.available
                ? selectedModelProfile.model + " · 新审查将全程使用此模型"
                : selectedModelProfile.unavailable_reason}
            </small>
          </label>
          {selectedModelProfileId === "deepseek-api" ? (
            <section className="deepseek-settings" aria-label="DeepSeek API 设置">
              <label>
                <span>API Key（保存在当前浏览器，并按账号隔离）</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={deepSeekSettings.apiKey}
                  disabled={isReviewing}
                  onChange={(event) => setDeepSeekSettings((current) => ({ ...current, apiKey: event.target.value }))}
                  placeholder="sk-..."
                />
              </label>
              <div className="deepseek-mode" role="group" aria-label="DeepSeek 模型选择方式">
                <button type="button" className={deepSeekSettings.selectionMode === "auto" ? "active" : ""} disabled={isReviewing} onClick={() => setDeepSeekSettings((current) => ({ ...current, selectionMode: "auto" }))}>自动选择</button>
                <button type="button" className={deepSeekSettings.selectionMode === "manual" ? "active" : ""} disabled={isReviewing} onClick={() => setDeepSeekSettings((current) => ({ ...current, selectionMode: "manual" }))}>手动选择</button>
              </div>
              {deepSeekSettings.selectionMode === "manual" ? (
                <select aria-label="DeepSeek 模型" value={deepSeekSettings.manualModel} disabled={isReviewing || !deepSeekModels.length} onChange={(event) => setDeepSeekSettings((current) => ({ ...current, manualModel: event.target.value }))}>
                  {!deepSeekModels.length ? <option value="">请先验证密钥</option> : null}
                  {deepSeekModels.map((model) => <option key={model.id} value={model.id}>{model.display_name}</option>)}
                </select>
              ) : <small>根据代码规模自动选择，创建后该审查固定使用同一模型。</small>}
              <button type="button" className="deepseek-verify" disabled={isReviewing || deepSeekValidationBusy || !deepSeekSettings.apiKey.trim()} onClick={() => void validateDeepSeekKey()}>
                {deepSeekValidationBusy ? "验证中…" : validatedDeepSeekKey === deepSeekSettings.apiKey.trim() && validatedDeepSeekKey ? "已验证" : "验证并加载模型"}
              </button>
            </section>
          ) : null}
        </div>
        <HistorySidebar
          items={historyItems}
          activeReviewId={viewedReviewId}
          onNewReview={resetWorkspace}
          onOpen={openHistorySession}
          onRename={renameHistorySession}
          onDelete={deleteHistorySession}
          hasMore={historyHasMore}
          loadingMore={historyLoading}
          loadError={historyError}
          onLoadMore={() => openHistory(historyError ? historyRetryReset : false)}
        />
        {authSession ? <SidebarAccountMenu user={authSession.user} onOpenSettings={authSession.openAccountSettings} onOpenAdmin={authSession.openAdminManagement} onSignOut={authSession.signOut} /> : null}
      </aside>

      <section className="workspace" aria-label="代码审查操作">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">CodeAstra · 星鉴</p>
            <h2>以可验证证据为中心的代码审查</h2>
            <p className="workspace-description">由 {activeModel.display_name} 提供审查支持</p>
          </div>
          <div className="toolbar">
            <div className="model-health-summary" aria-label="模型健康状态">
              <span className={`status-dot status-${healthSummary}`} />
              <span>{healthSummary}</span>
              {localHealthDetail ? <span className="model-health-detail" role="status">{localHealthDetail}</span> : null}
            </div>
            {session?.revisions?.length ? (
              <>
                <button type="button" className="icon-text-button" disabled={isReviewing} onClick={() => void openRevisionHistory()}><HistoryIcon size={16} />修改历史</button>
                <button
                  type="button"
                  className="icon-text-button"
                  disabled={artifactBusyKey !== null}
                  onClick={() => void startArtifactDownload(
                    "patch",
                    `/v1/reviews/${session.review_id}/fixes.patch`,
                    `${session.review_id}.patch`,
                    "下载 .patch",
                  )}
                ><FileDiff size={16} />{artifactBusyKey === "patch" ? "下载中…" : artifactState?.key === "patch" && artifactState.error ? "重试下载 .patch" : "下载 .patch"}</button>
                <button
                  type="button"
                  className="icon-text-button"
                  disabled={artifactBusyKey !== null}
                  onClick={() => void startArtifactDownload(
                    "all-fixed",
                    `/v1/reviews/${session.review_id}/fixed-files.zip`,
                    `${session.review_id}-fixed-files.zip`,
                    "全部修复文件",
                  )}
                ><FileCode2 size={16} />{artifactBusyKey === "all-fixed" ? "下载中…" : artifactState?.key === "all-fixed" && artifactState.error ? "重试全部文件" : "全部修复文件"}</button>
                {activeFile && session.revisions.some(item => !item.undone_at && item.file_id === activeFile.file_id) ? (
                  <button
                    type="button"
                    className="icon-text-button"
                    disabled={artifactBusyKey !== null}
                    title={activeFile.relative_path}
                    onClick={() => void startArtifactDownload(
                      `file:${activeFile.file_id}`,
                      `/v1/reviews/${session.review_id}/fixed-files/${activeFile.file_id}`,
                      activeFile.relative_path.split("/").pop() || "fixed-file.txt",
                      "当前修复文件",
                    )}
                  ><FileCode2 size={16} />{artifactBusyKey === `file:${activeFile.file_id}` ? "下载中…" : artifactState?.key === `file:${activeFile.file_id}` && artifactState.error ? "重试当前文件" : "当前修复文件"}</button>
                ) : null}
              </>
            ) : null}
            {session?.status === "completed" ? (
              <button
                type="button"
                className="icon-text-button"
                disabled={artifactBusyKey !== null}
                onClick={() => void startArtifactDownload(
                  "report",
                  `/v1/reviews/${session.review_id}/report`,
                  `${session.review_id}-report.md`,
                  "导出报告",
                )}
              >
                <ArrowRight size={16} />{artifactBusyKey === "report" ? "下载中…" : artifactState?.key === "report" && artifactState.error ? "重试导出报告" : "导出报告"}
              </button>
            ) : (
              <button type="button" className="icon-text-button" disabled><ArrowRight size={16} />导出报告</button>
            )}
          </div>
        </header>
        {artifactState ? (
          <div
            className={artifactState.error ? "artifact-download-status error" : "artifact-download-status"}
            role={artifactState.error ? "alert" : "status"}
          >
            <span>{artifactState.message}</span>
            {artifactState.error ? (
              <button
                type="button"
                disabled={artifactBusyKey !== null}
                onClick={() => void startArtifactDownload(
                  artifactState.key,
                  artifactState.path,
                  artifactState.fallback,
                  artifactState.label,
                )}
              >重试</button>
            ) : null}
          </div>
        ) : null}

        {session ? (
          <div className="result-toolbar">
            <div><p className="eyebrow">审查摘要</p><p>{session.summary.text}</p></div>
            {session.status === "failed" ? (
              <div className="result-actions">
                <button type="button" className="icon-text-button resume-button" onClick={resumeFailedReview}>
                  {failedReviewUsesRecheck ? "重新复查" : "继续未完成审查"}
                </button>
              </div>
            ) : null}
          </div>
        ) : null}

        {error ? (
          <div className="message message-error" role="alert">
            <AlertTriangle size={17} />
            <span>{error}</span>
            {disconnectedReviewId ? (
              <button
                type="button"
                className="inline-retry-button"
                disabled={isReviewing}
                onClick={() => void openHistorySession(disconnectedReviewId, undefined, false)}
              >
                重新连接
              </button>
            ) : null}
          </div>
        ) : null}

        <section className="result-section" aria-label="审查结果">
          {isReviewing && (!viewedReviewId || viewedReviewId === runningReviewId) ? (
            <section className="assistant-stream" aria-label="模型正在生成">
              <div className="assistant-avatar"><ShieldCheck size={18} /></div>
              <div className="assistant-response">
                <p className="assistant-name">{assistantName}</p>
                <div className="thinking-indicator" role="status" aria-label={stageMessage || "正在生成审查结果"}>
                  <span /><span /><span />
                </div>
                <p className="assistant-subtle">{stageMessage}</p>
                {progress ? (
                  <div className="review-progress" aria-label="审查进度">
                    <div className="review-progress-track">
                      <span style={{ width: Math.min(100, Math.max(0, progress.coverage_percent)) + "%" }} />
                    </div>
                    <p>
                      已完成 {progress.completed}/{progress.total} · 排队 {progress.queued} ·
                      运行 {progress.running} · 覆盖 {progress.coverage_percent.toFixed(0)}%
                    </p>
                  </div>
                ) : null}
              </div>
            </section>
          ) : session && activeFile ? (
            <>
              {session.files.length > 1 ? (
                <nav className="result-file-tabs" aria-label="源文件">
                  {session.files.map((file) => (
                    <button
                      type="button"
                      className={file.file_id === activeFile.file_id ? "active" : ""}
                      key={file.file_id}
                      onClick={() => setActiveFileId(file.file_id)}
                    >
                      <FileCode2 size={14} aria-hidden="true" />{file.relative_path}
                    </button>
                  ))}
                </nav>
              ) : null}
              <CodeViewer
                file={activeFile}
                findings={activeFindings}
                selectedFindingId={selectedFindingId}
                onFindingClick={(findingId) => {
                  const finding = session.findings.find((item) => item.finding_id === findingId);
                  if (finding) openFindingFollowup(finding);
                }}
                onTextSelection={openSelectionFollowup}
              />
              {session.coverage.some((item) => !item.available) ? (
                <div className="coverage-warning">
                  {session.coverage.filter((item) => !item.available).map((item) => <p key={item.language}>{item.message}</p>)}
                </div>
              ) : null}
            </>
          ) : (
            <div className="empty-state"><ListChecks size={24} /><p>审查结果会显示完整代码，并高亮可验证的问题。</p></div>
          )}
        </section>

        <form className="chat-composer" onSubmit={submitReview}>
          {!isReviewDetail && attachments.length ? (
            <div className="attachment-list" aria-label="已添加文件">
              {attachments.map((attachment) => {
                const detected = detectLanguage(attachment.content, attachment.filename);
                return (
                  <article
                    className={attachment.comparison ? "attachment-card attachment-card-comparison" : "attachment-card"}
                    key={attachment.id}
                  >
                    {attachment.comparison
                      ? <FileDiff size={18} aria-hidden="true" />
                      : <FileCode2 size={18} aria-hidden="true" />}
                    <div>
                      <strong>{attachment.filename}</strong>
                      {attachment.comparison ? (
                        <>
                          <span className="attachment-comparison-kind">
                            版本对比 · {attachment.comparison.changedRangeCount} 处变更
                          </span>
                          <span title={attachment.comparison.oldLabel + " → " + attachment.comparison.newLabel}>
                            {attachment.comparison.oldLabel} → {attachment.comparison.newLabel} · 仅新版本送审
                          </span>
                        </>
                      ) : (
                        <span>{detected.language === "python" ? "Python" : detected.language === "cpp" ? "C++" : "不支持"}{" · "}{Math.max(1, Math.ceil(attachment.size / 1024))} KB</span>
                      )}
                    </div>
                    <button type="button" className="attachment-remove" aria-label={"删除 " + attachment.filename} onClick={() => removeAttachment(attachment.id)}>
                      <X size={15} aria-hidden="true" />
                    </button>
                  </article>
                );
              })}
            </div>
          ) : null}
          <label className="sr-only" htmlFor="review-composer">输入需要审查的代码</label>
          <textarea
            id="review-composer"
            aria-label="输入需要审查的代码"
            value={isReviewDetail ? "" : draftCode}
            onChange={(event) => setDraftCode(event.target.value)}
            placeholder={isReviewDetail ? "正在查看审查记录；点击“新建审查”后可继续未提交草稿" : "粘贴 Python 或 C++ 代码，也可以添加一个或多个文件"}
            spellCheck={false}
            disabled={isReviewing || isReviewDetail}
            aria-invalid={Boolean(draftTextError)}
          />
          {isReviewDetail ? (
            <p className="composer-inline-status" role="status">正在查看审查记录；点击“新建审查”后可继续未提交草稿。</p>
          ) : draftTextError ? (
            <p className="composer-inline-status error" role="alert">{draftTextError}</p>
          ) : null}
          <div className="composer-toolbar">
            <input ref={fileInputRef} className="hidden-file-input" type="file" multiple accept=".py,.pyw,.cc,.cpp,.cxx,.hh,.hpp,.hxx" aria-label="添加代码文件" onChange={handleFiles} disabled={isReviewing || isReviewDetail} />
            <AttachmentMenu
              disabled={isReviewing || isReviewDetail}
              onSelectLocalFiles={() => fileInputRef.current?.click()}
              onSelectLocalDiff={() => setLocalDiffDialogOpen(true)}
              onSelectGitLab={() => setGitLabDialogOpen(true)}
            />
            {isReviewing ? (
              <button type="button" className="composer-send stop-button" onClick={stopReview}>
                <Square size={13} fill="currentColor" /><span>停止生成</span>
              </button>
            ) : (
              <button type="submit" className="composer-send" aria-label="发送审查" disabled={isReviewDetail || Boolean(draftTextError) || (!draftCode.trim() && attachments.length === 0)}>
                <Send size={17} aria-hidden="true" /><span>发送审查</span>
              </button>
            )}
          </div>
        </form>
      </section>

      <aside className="sidebar sidebar-right" aria-label="问题导航">
        <div className="right-heading"><p className="eyebrow">Finding map</p><h2>问题导航</h2></div>
        <div className="finding-scroll-surface">
          <ol className="finding-nav">
          {session?.findings.map((finding) => {
            const expanded = selectedFindingId === finding.finding_id;
            const panelId = `finding-accordion-panel-${finding.finding_id}`;
            return (
              <li
                className={expanded ? "finding-nav-item active" : "finding-nav-item"}
                key={finding.finding_id}
              >
                <button
                  className={expanded ? "finding-nav-link active" : "finding-nav-link"}
                  type="button"
                  aria-label={`${severityLabels[finding.severity]}：${finding.title}`}
                  aria-expanded={expanded}
                  aria-controls={panelId}
                  onClick={() => navigateToFinding(finding)}
                >
                  <span className={`severity-dot severity-dot-${finding.severity}`} />
                  <span title={finding.title}>{finding.title}</span>
                </button>
                {expanded ? (
                  <section
                    id={panelId}
                    className="finding-accordion-panel"
                    aria-label={`问题详情：${finding.title}`}
                  >
                    <span className={`severity severity-${finding.severity}`}>
                      {severityLabels[finding.severity]}
                    </span>
                    <h3>{finding.title}</h3>
                    <p>{finding.detail}</p>
                    <dl>
                      <dt>证据</dt><dd>{finding.evidence}</dd>
                      <dt>建议</dt><dd>{finding.suggestion}</dd>
                    </dl>
                    <div className="finding-decision-card">
                      <div className="finding-decision-copy">
                        <strong>处理这条建议</strong>
                         <p>修复先生成候选 Diff，确认后才写入；接受风险会保留在报告中。</p>
                      </div>
                      {session?.finding_decisions?.[finding.finding_id] ? (
                        <span className={`decision-state decision-state-${session.finding_decisions[finding.finding_id]}`}>
                           {session.finding_decisions[finding.finding_id] === "fixed"
                             ? "已修复"
                             : session.finding_decisions[finding.finding_id] === "accepted_risk"
                               ? "已接受风险"
                               : session.finding_decisions[finding.finding_id] === "deferred"
                                 ? "稍后处理"
                                 : "判定不成立"}
                        </span>
                      ) : null}
                      <div className="finding-decision-actions">
                        <button
                          type="button"
                          className="decision-action finding-decision-action decision-action-primary"
                          aria-label={fixBusyId === finding.finding_id ? "停止等待生成" : "应用修复"}
                          title={fixBusyId === finding.finding_id ? "停止等待候选生成" : "先生成、验证并预览候选修复"}
                          disabled={isReviewing}
                          onClick={() => fixBusyId === finding.finding_id
                            ? stopFixPreviewGeneration()
                            : void generateFixPreview(finding)}
                        >
                          <Check size={17} aria-hidden="true" />
                          <span>应用修复</span>
                        </button>
                        <button
                          type="button"
                          className="decision-action finding-decision-action decision-action-secondary"
                           aria-label="接受风险"
                           title="明确记录接受风险，问题仍在报告中可见"
                          disabled={fixBusyId === finding.finding_id || isReviewing}
                           onClick={() => void decideFinding(finding, "accepted_risk")}
                        >
                           <ShieldCheck size={17} aria-hidden="true" />
                           <span>接受风险</span>
                         </button>
                      </div>
                    </div>
                  </section>
                ) : null}
              </li>
            );
          })}
          </ol>
          {!session?.findings.length ? <p className="muted">当前没有活动问题。</p> : null}
          {processedFindings.length ? (
            <details className="processed-findings">
              <summary>已处理问题（{processedFindings.length}）</summary>
              <div className="processed-finding-list">
                {processedFindings.map(({ finding, decision, audit }) => {
                  const revisionRetained = session?.revisions?.some(
                    (item) => item.finding_id === finding.finding_id && !item.undone_at,
                  ) ?? false;
                  return (
                    <article className="processed-finding" key={finding.finding_id}>
                      <div>
                        <span className={`severity severity-${finding.severity}`}>{severityLabels[finding.severity]}</span>
                        <strong title={finding.title}>{finding.title}</strong>
                      </div>
                      <p>{decisionLabels[decision]}</p>
                      <p title={audit?.reason ?? "历史记录未提供原因"}>{audit?.reason ?? "历史记录未提供原因"}</p>
                      <time dateTime={audit?.created_at}>{audit?.created_at ? new Date(audit.created_at).toLocaleString("zh-CN") : "历史时间未知"}</time>
                      {revisionRetained ? <small>重新检查不会撤销已经应用的代码修订。</small> : null}
                      <button
                        type="button"
                        className="processed-reopen-button"
                        disabled={reopenBusyId !== null || isReviewing}
                        onClick={() => void reopenFinding(finding.finding_id)}
                      >{reopenBusyId === finding.finding_id ? "处理中…" : revisionRetained ? "重新检查" : "重新打开"}</button>
                    </article>
                  );
                })}
              </div>
            </details>
          ) : null}
        </div>
      </aside>
      <FixPreviewDialog
        candidate={fixCandidate}
        busy={fixConfirmationBusy}
        onCancel={() => void cancelFixPreview()}
        onConfirm={() => void applyFixCandidate()}
      />
      {repairIntent ? (
        <RepairIntentDialog
          evidence={repairIntent.evidence}
          busy={fixBusyId === repairIntent.finding.finding_id}
          error={error}
          onCancel={() => {
            setRepairIntent(null);
            setError("");
          }}
          onSubmit={(option, value) => void generateIntentFix(option, value)}
        />
      ) : null}
      <RevisionHistoryDialog
        open={revisionDialogOpen}
        items={revisions}
        busyRevisionId={revisionBusyId}
        onClose={() => {
          if (!revisionBusyId) setRevisionDialogOpen(false);
        }}
        onUndo={(revisionId) => void undoRevision(revisionId)}
      />
      <LocalDiffDialog
        open={localDiffDialogOpen}
        onClose={() => setLocalDiffDialogOpen(false)}
        onImport={importLocalDiff}
      />
      <GitLabImportDialog
        open={gitLabDialogOpen}
        accounts={gitLabAccounts}
        activeAccountId={activeGitLabAccountId}
        onClose={() => setGitLabDialogOpen(false)}
        onConnectAccount={() => {
          setGitLabDialogOpen(false);
          setResumeGitLabImportAfterAccount(true);
          authSession?.openAccountSettings();
        }}
        oauthToken={gitLabOAuthToken}
        oauthError={gitLabOAuthError}
        onOAuthConnect={() => {
          void beginGitLabOAuth(defaultGitLabRedirectUri()).catch((oauthError) => setGitLabOAuthError(oauthError instanceof Error ? oauthError.message : "GitLab OAuth 尚未配置。"));
        }}
        onImport={importGitLabFiles}
      />
      {followupContext && session?.status === "completed" ? (
        <FollowupDialog
          context={followupContext}
          messages={followups}
          assistantName={session.model.display_name}
          error={followupError}
          busy={followupBusy}
          onClose={closeFollowup}
          onSubmit={submitFollowup}
        />
      ) : null}
    </main>
    {authSession?.accountSettingsOpen ? <div className="auth-center auth-settings-surface"><AccountSecurityPanel user={authSession.user} onClose={closeAccountSettings} onSignedOut={authSession.signOut} initialSection={resumeGitLabImportAfterAccount ? "gitlab" : "profile"} gitLabConnections={<GitLabAccountManagement accounts={gitLabAccounts} activeAccountId={activeGitLabAccountId} onSave={saveGitLabAccount} onActivate={activateGitLabAccount} onDelete={deleteGitLabAccount} />} /></div> : null}
    </>
  );
}

export default App;
