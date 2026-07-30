"use client";

import {
  BadgeCheck,
  Carrot,
  CircleAlert,
  Clock3,
  Coins,
  Cpu,
  FileCheck2,
  Fingerprint,
  History,
  ListChecks,
  LoaderCircle,
  LockKeyhole,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sprout,
  Users,
  Wheat,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8010";

type CredentialStatus =
  | "ACTIVE"
  | "PENDING"
  | "REVOKED"
  | "EXPIRED"
  | "REJECTED"
  | "MISSING";

type OwnerSummary = {
  id: string;
  nickname: string;
  phoneMasked: string;
  did: string;
  accent: string;
  agent: {
    id: string;
    name: string;
    automationEnabled: boolean;
    credentialStatus: CredentialStatus;
  };
};

type Plot = {
  id: string;
  position: number;
  cropType: "CARROT" | "TOMATO" | "CORN" | null;
  cropName: string | null;
  stage: "EMPTY" | "SPROUT" | "LEAF" | "MATURE";
  progress: number;
  yieldRemaining: number;
  yieldTotal: number;
  maturesAt: string | null;
};

type Farm = {
  id: string;
  name: string;
  coins: number;
  plots: Plot[];
  inventory: Array<{
    cropType: string;
    cropName: string;
    quantity: number;
  }>;
};

type Action = {
  id: number;
  traceId: string;
  agentName: string;
  targetOwnerName: string;
  actionType: string;
  status: string;
  reason: string;
  credentialStatus: CredentialStatus;
  cropName: string | null;
  quantity: number;
  source: string;
  executionMode: "LLM" | "RULES";
  admissionMode: "REAL_ACTIVE" | "DEMO_CONNECTED" | "DENIED" | "LOCAL";
  admissionUpstreamStatus: string;
  agentRunId: string | null;
  isIncoming: boolean;
  createdAt: string;
};

type Dashboard = {
  owner: OwnerSummary;
  agent: {
    id: string;
    name: string;
    clawId: string;
    platformName: string;
    description: string;
    automationEnabled: boolean;
    lastRunAt: string | null;
  };
  runtime: {
    mode: "llm" | "rules";
    provider: string;
    model: string;
    configured: boolean;
    maxToolRounds: number;
  };
  recentRuns: Array<{
    id: string;
    triggerSource: string;
    runtimeMode: "LLM" | "RULES";
    provider: string;
    model: string;
    status: string;
    credentialStatus: CredentialStatus;
    toolCallCount: number;
    decisionSummary: string;
    errorMessage: string;
    startedAt: string;
    completedAt: string | null;
  }>;
  credential: null | {
    provider: string;
    templateId: string;
    aic: string;
    vcRecordId: string;
    issueMode: string;
    status: CredentialStatus;
    issuedAt: string | null;
    expiresAt: string | null;
    lastVerifiedAt: string | null;
  };
  credentialEvents: Array<{
    id: number;
    step: string;
    status: string;
    detail: string;
    createdAt: string;
  }>;
  farm: Farm;
  neighbors: Array<{ owner: OwnerSummary; farm: Farm }>;
  actions: Action[];
  serverTime: string;
};

type Admission = {
  agentId: string;
  externalAgentName: string;
  allowed: boolean;
  mode: "REAL_ACTIVE" | "DEMO_CONNECTED" | "DENIED" | "LOCAL";
  upstreamStatus: string;
  message: string;
  checkedAt: string | null;
  cached: boolean;
  environment: string;
  templateId: string;
  recordCount: number;
};

type TabKey = "farm" | "neighbors" | "credential" | "actions";
type ActionFilter = "all" | "outgoing" | "incoming" | "blocked";

const statusLabels: Record<CredentialStatus, string> = {
  ACTIVE: "凭证有效",
  PENDING: "签发中",
  REVOKED: "已吊销",
  EXPIRED: "已过期",
  REJECTED: "签发拒绝",
  MISSING: "未申领",
};

const actionLabels: Record<string, string> = {
  ACCESS: "准入校验",
  PLANT: "种植",
  HARVEST: "收获",
  STEAL: "邻居采摘",
  OBSERVE: "巡田",
  MODEL_DECISION: "模型决策",
  AGENT_ERROR: "模型运行异常",
};

const admissionLabels: Record<Admission["mode"], string> = {
  REAL_ACTIVE: "中移真实凭证有效",
  DEMO_CONNECTED: "中移接口准入有效",
  DENIED: "中移准入未通过",
  LOCAL: "本地 Demo 门禁",
};

const admissionShortLabels: Record<Admission["mode"], string> = {
  REAL_ACTIVE: "中移真实",
  DEMO_CONNECTED: "中移联调",
  DENIED: "准入拒绝",
  LOCAL: "本地门禁",
};

const stepLabels: Record<string, string> = {
  OWNER_DID_VERIFIED: "主人实名 DID",
  AGENT_CREATED: "创建智能体 AIC",
  APPLICATION_ACCEPTED: "提交凭证申领",
  CREDENTIAL_ACTIVE: "身份凭证生效",
  STATUS_ACTIVE: "凭证恢复有效",
  STATUS_REVOKED: "凭证已吊销",
  STATUS_EXPIRED: "凭证已过期",
  STATUS_PENDING: "凭证转为待签发",
  STATUS_REJECTED: "凭证签发拒绝",
};

const tabs: Array<{
  key: TabKey;
  label: string;
  icon: typeof Sprout;
}> = [
  { key: "farm", label: "我的农场", icon: Sprout },
  { key: "neighbors", label: "邻居农场", icon: Users },
  { key: "credential", label: "智能体身份", icon: BadgeCheck },
  { key: "actions", label: "行为记录", icon: ListChecks },
];

function formatTime(value: string | null, includeDate = false) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: includeDate ? "2-digit" : undefined,
    day: includeDate ? "2-digit" : undefined,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function shortId(value: string) {
  if (value.length < 23) return value;
  return `${value.slice(0, 12)}...${value.slice(-6)}`;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail ?? `请求失败：${response.status}`);
  }
  return response.json();
}

function AgentAvatar({
  ownerId,
  label,
  small = false,
}: {
  ownerId: string;
  label: string;
  small?: boolean;
}) {
  const index =
    ownerId === "owner-lin" ? 0 : ownerId === "owner-zhou" ? 1 : 2;
  return (
    <div
      aria-label={label}
      className={`agent-avatar avatar-${index} ${small ? "avatar-small" : ""}`}
      role="img"
    />
  );
}

function CredentialBadge({ status }: { status: CredentialStatus }) {
  return (
    <span className={`status-badge status-${status.toLowerCase()}`}>
      {status === "ACTIVE" ? <ShieldCheck size={14} /> : <LockKeyhole size={14} />}
      {statusLabels[status]}
    </span>
  );
}

function CropIcon({ plot, compact = false }: { plot: Plot; compact?: boolean }) {
  if (!plot.cropType) {
    return (
      <div className={`crop-visual crop-empty ${compact ? "crop-compact" : ""}`}>
        <span />
      </div>
    );
  }

  return (
    <div
      aria-label={`${plot.cropName}${plot.stage === "MATURE" ? "成熟" : "生长中"}`}
      className={`crop-visual crop-${plot.cropType.toLowerCase()} crop-${plot.stage.toLowerCase()} ${
        compact ? "crop-compact" : ""
      }`}
      role="img"
    />
  );
}

function FarmPlot({ plot }: { plot: Plot }) {
  return (
    <article className={`farm-plot plot-${plot.stage.toLowerCase()}`}>
      <div className="plot-topline">
        <span>地块 {plot.position + 1}</span>
        <span>{plot.cropName ?? "空地"}</span>
      </div>
      <CropIcon plot={plot} />
      {plot.cropType ? (
        <>
          <div className="growth-track" aria-label={`生长进度 ${plot.progress}%`}>
            <span style={{ width: `${plot.progress}%` }} />
          </div>
          <div className="plot-meta">
            <span>{plot.stage === "MATURE" ? "可收获" : `${plot.progress}%`}</span>
            <span>余量 {plot.yieldRemaining}</span>
          </div>
        </>
      ) : (
        <div className="plot-empty-label">等待智能体补种</div>
      )}
    </article>
  );
}

function ActionIcon({ type }: { type: string }) {
  if (type === "PLANT") return <Sprout size={17} />;
  if (type === "HARVEST") return <Wheat size={17} />;
  if (type === "STEAL") return <Carrot size={17} />;
  if (type === "ACCESS") return <LockKeyhole size={17} />;
  if (type === "MODEL_DECISION") return <Cpu size={17} />;
  if (type === "AGENT_ERROR") return <CircleAlert size={17} />;
  return <History size={17} />;
}

function actionResultLabel(status: string) {
  if (status === "SUCCESS") return "已完成";
  if (status === "BLOCKED") return "已拦截";
  if (status === "REJECTED") return "已拒绝";
  return "执行失败";
}

export default function FarmDemo() {
  const [owners, setOwners] = useState<OwnerSummary[]>([]);
  const [selectedOwnerId, setSelectedOwnerId] = useState("owner-lin");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [admission, setAdmission] = useState<Admission | null>(null);
  const [activeTab, setActiveTab] = useState<TabKey>("farm");
  const [actionFilter, setActionFilter] = useState<ActionFilter>("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectedOwnerRef = useRef(selectedOwnerId);
  const dashboardRequestRef = useRef<{
    controller: AbortController;
    ownerId: string;
    requestId: number;
  } | null>(null);
  const dashboardRequestIdRef = useRef(0);

  const loadOwners = useCallback(async () => {
    const data = await api<OwnerSummary[]>("/api/owners");
    setOwners(data);
  }, []);

  const loadDashboard = useCallback(
    async (quiet = false, ownerId = selectedOwnerId) => {
      const activeRequest = dashboardRequestRef.current;
      if (quiet && activeRequest?.ownerId === ownerId) return;
      activeRequest?.controller.abort();

      const controller = new AbortController();
      const requestId = ++dashboardRequestIdRef.current;
      dashboardRequestRef.current = { controller, ownerId, requestId };
      try {
        const data = await api<Dashboard>(
          `/api/owners/${ownerId}/dashboard`,
          { signal: controller.signal },
        );
        const isCurrent =
          dashboardRequestRef.current?.requestId === requestId &&
          selectedOwnerRef.current === ownerId;
        if (isCurrent) {
          setDashboard(data);
          if (!quiet) setError(null);
        }
      } catch (requestError) {
        if (
          !controller.signal.aborted &&
          !quiet &&
          selectedOwnerRef.current === ownerId
        ) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "无法连接后端服务",
          );
        }
      } finally {
        if (dashboardRequestRef.current?.requestId === requestId) {
          dashboardRequestRef.current = null;
        }
      }
    },
    [selectedOwnerId],
  );

  useEffect(() => {
    const initialLoad = window.setTimeout(() => {
      void loadOwners().catch(() => setError("无法读取演示账号"));
    }, 0);
    return () => window.clearTimeout(initialLoad);
  }, [loadOwners]);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => {
      void loadDashboard();
    }, 0);
    const timer = window.setInterval(() => loadDashboard(true), 2000);
    return () => {
      window.clearTimeout(initialLoad);
      window.clearInterval(timer);
      if (dashboardRequestRef.current?.ownerId === selectedOwnerId) {
        dashboardRequestRef.current.controller.abort();
        dashboardRequestRef.current = null;
      }
    };
  }, [loadDashboard, selectedOwnerId]);

  const dashboardAgentId = dashboard?.agent.id;
  useEffect(() => {
    if (!dashboardAgentId) return;
    let cancelled = false;
    const loadAdmission = async () => {
      try {
        const data = await api<Admission>(
          `/api/agents/${dashboardAgentId}/admission`,
        );
        if (!cancelled && selectedOwnerRef.current === dashboard?.owner.id) {
          setAdmission(data);
        }
      } catch {
        if (!cancelled) setAdmission(null);
      }
    };
    void loadAdmission();
    const timer = window.setInterval(loadAdmission, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [dashboardAgentId, dashboard?.owner.id]);

  const selectOwner = (ownerId: string) => {
    dashboardRequestRef.current?.controller.abort();
    dashboardRequestRef.current = null;
    dashboardRequestIdRef.current += 1;
    selectedOwnerRef.current = ownerId;
    setSelectedOwnerId(ownerId);
    setDashboard((current) => (current?.owner.id === ownerId ? current : null));
    setAdmission(null);
    setError(null);
  };

  const mutate = async (
    key: string,
    path: string,
    init: RequestInit = { method: "POST" },
  ) => {
    setBusy(key);
    setError(null);
    try {
      await api(path, init);
      await Promise.all([
        loadDashboard(false, selectedOwnerRef.current),
        loadOwners(),
      ]);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "操作失败",
      );
    } finally {
      setBusy(null);
    }
  };

  const filteredActions = useMemo(() => {
    if (!dashboard) return [];
    if (actionFilter === "incoming") {
      return dashboard.actions.filter((action) => action.isIncoming);
    }
    if (actionFilter === "outgoing") {
      return dashboard.actions.filter((action) => !action.isIncoming);
    }
    if (actionFilter === "blocked") {
      return dashboard.actions.filter((action) => action.status === "BLOCKED");
    }
    return dashboard.actions;
  }, [dashboard, actionFilter]);

  const latestOutgoingAction = useMemo(
    () => dashboard?.actions.find((action) => !action.isIncoming),
    [dashboard],
  );

  const resetDemo = async () => {
    setBusy("reset");
    try {
      await api("/api/demo/reset", { method: "POST" });
      selectedOwnerRef.current = "owner-lin";
      setSelectedOwnerId("owner-lin");
      setAdmission(null);
      setActiveTab("farm");
      await Promise.all([loadOwners(), loadDashboard(false, "owner-lin")]);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "重置失败",
      );
    } finally {
      setBusy(null);
    }
  };

  const verifyAdmission = async () => {
    if (!dashboard) return;
    const ownerId = dashboard.owner.id;
    setBusy("admission");
    setError(null);
    try {
      const data = await api<Admission>(
        `/api/agents/${dashboard.agent.id}/admission/verify`,
        { method: "POST" },
      );
      if (selectedOwnerRef.current === ownerId) setAdmission(data);
    } catch (requestError) {
      if (selectedOwnerRef.current === ownerId) {
        setError(
          requestError instanceof Error ? requestError.message : "准入校验失败",
        );
      }
    } finally {
      setBusy(null);
    }
  };

  const applyCredential = async () => {
    if (!dashboard) return;
    const ownerId = dashboard.owner.id;
    const agentId = dashboard.agent.id;
    setBusy("apply");
    setError(null);
    try {
      const nextDashboard = await api<Dashboard>(
        `/api/owners/${ownerId}/agent/credential/apply`,
        { method: "POST" },
      );
      const nextAdmission = await api<Admission>(
        `/api/agents/${agentId}/admission`,
      );
      if (selectedOwnerRef.current === ownerId) {
        setDashboard(nextDashboard);
        setAdmission(nextAdmission);
      }
      await loadOwners();
    } catch (requestError) {
      if (selectedOwnerRef.current === ownerId) {
        setError(
          requestError instanceof Error ? requestError.message : "申领失败",
        );
      }
    } finally {
      setBusy(null);
    }
  };

  const awaitingCmccCredential = Boolean(
    dashboard &&
      admission?.externalAgentName &&
      !dashboard.credential,
  );

  return (
    <main className="app-shell">
      <header className="app-header">
        <div className="brand-block">
          <div className="brand-mark">
            <Sprout size={22} />
          </div>
          <div>
            <h1>智耕凭证农场</h1>
            <p>中移互联网智能体身份凭证 Demo</p>
          </div>
        </div>

        <div className="owner-switcher" aria-label="切换演示主人">
          {owners.map((owner) => (
            <button
              className={owner.id === selectedOwnerId ? "selected" : ""}
              key={owner.id}
              onClick={() => selectOwner(owner.id)}
              type="button"
            >
              <AgentAvatar
                label={`${owner.agent.name}头像`}
                ownerId={owner.id}
                small
              />
              <span>
                <strong>{owner.nickname}</strong>
                <small>{owner.agent.name}</small>
              </span>
            </button>
          ))}
        </div>

        <button
          aria-label="重置演示数据"
          className="icon-button"
          disabled={busy === "reset"}
          onClick={resetDemo}
          title="重置演示数据"
          type="button"
        >
          {busy === "reset" ? (
            <LoaderCircle className="spin" size={19} />
          ) : (
            <RotateCcw size={19} />
          )}
        </button>
      </header>

      <nav className="tab-bar" aria-label="主功能">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button
            aria-current={activeTab === key ? "page" : undefined}
            className={activeTab === key ? "active" : ""}
            key={key}
            onClick={() => setActiveTab(key)}
            type="button"
          >
            <Icon size={18} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      {error && (
        <div className="error-banner" role="alert">
          <CircleAlert size={18} />
          <span>{error}</span>
          <button onClick={() => loadDashboard()} type="button">
            <RefreshCw size={16} />
            重试
          </button>
        </div>
      )}

      {!dashboard ? (
        <section className="loading-state">
          <LoaderCircle className="spin" size={28} />
          <span>正在读取可信农场状态</span>
        </section>
      ) : (
        <>
          <section className="identity-strip">
            <div className="agent-summary">
              <AgentAvatar
                label={`${dashboard.agent.name}头像`}
                ownerId={dashboard.owner.id}
              />
              <div>
                <div className="agent-title-row">
                  <h2>{dashboard.agent.name}</h2>
                  <CredentialBadge
                    status={dashboard.credential?.status ?? "MISSING"}
                  />
                </div>
                <p>{dashboard.agent.description}</p>
              </div>
            </div>
            <div className="identity-metrics">
              <div>
                <Fingerprint size={17} />
                <span>AIC</span>
                <strong>
                  {dashboard.credential
                    ? shortId(dashboard.credential.aic)
                    : "等待申领"}
                </strong>
              </div>
              <div>
                <Clock3 size={17} />
                <span>最近执行</span>
                <strong>{formatTime(dashboard.agent.lastRunAt)}</strong>
              </div>
              <div>
                <Coins size={17} />
                <span>农场资产</span>
                <strong>{dashboard.farm.coins}</strong>
              </div>
            </div>
          </section>

          {activeTab === "farm" && (
            <section className="farm-view">
              <div className="farm-scene">
                <div className="farm-scene-heading">
                  <div>
                    <span className="section-kicker">MY TRUSTED FARM</span>
                    <h2>{dashboard.farm.name}</h2>
                  </div>
                  <div className="inventory-list">
                    {dashboard.farm.inventory.map((item) => (
                      <span key={item.cropType}>
                        {item.cropName}
                        <strong>{item.quantity}</strong>
                      </span>
                    ))}
                  </div>
                </div>
                <div className="farm-grid">
                  {dashboard.farm.plots.map((plot) => (
                    <FarmPlot key={plot.id} plot={plot} />
                  ))}
                </div>
              </div>

              <aside className="agent-console">
                <div className="console-heading">
                  <div>
                    <span className="section-kicker">AGENT CONSOLE</span>
                    <h3>自主运行</h3>
                  </div>
                  <span
                    className={`live-dot ${
                      dashboard.agent.automationEnabled ? "is-live" : ""
                    }`}
                  >
                    {dashboard.agent.automationEnabled ? "运行中" : "已暂停"}
                  </span>
                </div>
                <div className="strategy-order">
                  <div className="runtime-state">
                    <Cpu size={16} />
                    <div>
                      <strong>
                        {dashboard.runtime.mode === "llm"
                          ? "模型 Skill 模式"
                          : "确定性规则模式"}
                      </strong>
                      <span>
                        {dashboard.runtime.mode === "llm"
                          ? `${dashboard.runtime.provider} · ${dashboard.runtime.model}`
                          : "不调用外部模型 API"}
                      </span>
                    </div>
                    {!dashboard.runtime.configured && (
                      <small>等待 API Key</small>
                    )}
                  </div>
                  {dashboard.runtime.mode === "llm" ? (
                    <>
                      <div>
                        <strong>01</strong>
                        <span>观察农场、邻居与近期行为</span>
                      </div>
                      <div>
                        <strong>02</strong>
                        <span>调用受限种植与收获 Skills</span>
                      </div>
                      <div>
                        <strong>03</strong>
                        <span>按额度完成一次社交采摘</span>
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <strong>01</strong>
                        <span>收获成熟作物</span>
                      </div>
                      <div>
                        <strong>02</strong>
                        <span>补种空闲地块</span>
                      </div>
                      <div>
                        <strong>03</strong>
                        <span>寻找邻居成熟作物</span>
                      </div>
                    </>
                  )}
                </div>
                <div className="console-actions">
                  <button
                    className="primary-button"
                    disabled={busy === "run"}
                    onClick={() =>
                      mutate("run", `/api/agents/${dashboard.agent.id}/run`)
                    }
                    type="button"
                  >
                    {busy === "run" ? (
                      <LoaderCircle className="spin" size={17} />
                    ) : (
                      <Play size={17} />
                    )}
                    运行一次
                  </button>
                  <button
                    className="secondary-button"
                    disabled={busy === "automation"}
                    onClick={() =>
                      mutate(
                        "automation",
                        `/api/agents/${dashboard.agent.id}/automation`,
                        {
                          method: "PATCH",
                          body: JSON.stringify({
                            enabled: !dashboard.agent.automationEnabled,
                          }),
                        },
                      )
                    }
                    type="button"
                  >
                    {dashboard.agent.automationEnabled ? (
                      <Pause size={17} />
                    ) : (
                      <Play size={17} />
                    )}
                    {dashboard.agent.automationEnabled ? "暂停" : "自动运行"}
                  </button>
                </div>
                <div className="latest-action">
                  <span>最新决策</span>
                  {latestOutgoingAction ? (
                    <>
                      <strong>
                        {actionLabels[latestOutgoingAction.actionType] ??
                          "状态检查"}
                      </strong>
                      <p>{latestOutgoingAction.reason}</p>
                    </>
                  ) : (
                    <p>等待智能体首次运行</p>
                  )}
                  {dashboard.recentRuns[0] && (
                    <small className="latest-run-summary">
                      {dashboard.recentRuns[0].runtimeMode === "LLM"
                        ? `${dashboard.recentRuns[0].provider} · ${dashboard.recentRuns[0].model}`
                        : "本地规则"}
                      {" · "}
                      {dashboard.recentRuns[0].toolCallCount} 次 Skill 调用
                    </small>
                  )}
                </div>
              </aside>
            </section>
          )}

          {activeTab === "neighbors" && (
            <section className="content-view">
              <div className="view-heading">
                <div>
                  <span className="section-kicker">SOCIAL FARMS</span>
                  <h2>邻居农场</h2>
                </div>
                <p>成熟作物将进入智能体的社交采摘候选集。</p>
              </div>
              <div className="neighbor-list">
                {dashboard.neighbors.map(({ owner, farm }) => {
                  const matureCount = farm.plots.filter(
                    (plot) => plot.stage === "MATURE" && plot.yieldRemaining > 1,
                  ).length;
                  return (
                    <article className="neighbor-row" key={owner.id}>
                      <div className="neighbor-owner">
                        <AgentAvatar
                          label={`${owner.agent.name}头像`}
                          ownerId={owner.id}
                        />
                        <div>
                          <h3>{farm.name}</h3>
                          <p>
                            主人 {owner.nickname} · 智能体 {owner.agent.name}
                          </p>
                        </div>
                        <CredentialBadge
                          status={owner.agent.credentialStatus}
                        />
                      </div>
                      <div className="neighbor-plots">
                        {farm.plots.map((plot) => (
                          <div className="mini-plot" key={plot.id}>
                            <CropIcon compact plot={plot} />
                            <span>{plot.cropName ?? "空地"}</span>
                            <small>
                              {plot.stage === "MATURE"
                                ? `成熟 · ${plot.yieldRemaining}`
                                : `${plot.progress}%`}
                            </small>
                          </div>
                        ))}
                      </div>
                      <div className="neighbor-ready">
                        <span>可采摘地块</span>
                        <strong>{matureCount}</strong>
                      </div>
                    </article>
                  );
                })}
              </div>
            </section>
          )}

          {activeTab === "credential" && (
            <section className="credential-view">
              <div className="credential-identity">
                <div className="view-heading">
                  <div>
                    <span className="section-kicker">CREDENTIAL IDENTITY</span>
                    <h2>智能体身份凭证</h2>
                  </div>
                  <CredentialBadge
                    status={dashboard.credential?.status ?? "MISSING"}
                  />
                </div>

                <div className="identity-fields">
                  <div>
                    <span>主人实名 DID</span>
                    <strong>{dashboard.owner.did}</strong>
                  </div>
                  <div>
                    <span>智能体 AIC</span>
                    <strong>
                      {dashboard.credential?.aic ?? "尚未创建"}
                    </strong>
                  </div>
                  <div>
                    <span>凭证记录 ID</span>
                    <strong>
                      {dashboard.credential?.vcRecordId ?? "尚未签发"}
                    </strong>
                  </div>
                  <div>
                    <span>有效期</span>
                    <strong>
                      {dashboard.credential?.expiresAt
                        ? formatTime(dashboard.credential.expiresAt, true)
                        : "尚未生成"}
                    </strong>
                  </div>
                </div>

                {admission && (
                  <div className={`admission-status admission-${admission.mode.toLowerCase()}`}>
                    <div className="admission-heading">
                      <div>
                        <span>中移接口准入</span>
                        <strong>
                          {admission.externalAgentName
                            ? `${dashboard.agent.name} → ${admission.externalAgentName}`
                            : dashboard.agent.name}
                        </strong>
                      </div>
                      <span className="admission-badge">
                        {awaitingCmccCredential
                          ? "等待申领"
                          : admissionLabels[admission.mode]}
                      </span>
                    </div>
                    <div className="admission-details">
                      <div>
                        <span>环境</span>
                        <strong>{admission.environment || "本地"}</strong>
                      </div>
                      {awaitingCmccCredential ? (
                        <div>
                          <span>接入状态</span>
                          <strong>等待凭证申领</strong>
                        </div>
                      ) : admission.mode === "DEMO_CONNECTED" ? (
                        <div>
                          <span>校验结果</span>
                          <strong>准入校验通过</strong>
                        </div>
                      ) : (
                        <>
                          <div>
                            <span>上游状态</span>
                            <strong>{admission.upstreamStatus}</strong>
                          </div>
                          <div>
                            <span>记录数量</span>
                            <strong>{admission.recordCount}</strong>
                          </div>
                        </>
                      )}
                      <div>
                        <span>校验时间</span>
                        <strong>
                          {admission.checkedAt
                            ? formatTime(admission.checkedAt, true)
                            : "尚未校验"}
                        </strong>
                      </div>
                    </div>
                    <p>
                      {awaitingCmccCredential
                        ? "完成智能体身份凭证申领后，将进行中移接口准入校验。"
                        : admission.mode === "DEMO_CONNECTED"
                          ? "中移接口联调校验已完成，当前智能体已获得农场协作准入。"
                          : admission.message}
                    </p>
                    {admission.templateId && (
                      <small>模板：{shortId(admission.templateId)}</small>
                    )}
                  </div>
                )}

                <div className="credential-actions">
                  {admission?.externalAgentName &&
                    admission.mode !== "LOCAL" &&
                    dashboard.credential?.status === "ACTIVE" && (
                      <button
                        className="secondary-button"
                        disabled={busy === "admission"}
                        onClick={verifyAdmission}
                        type="button"
                      >
                        {busy === "admission" ? (
                          <LoaderCircle className="spin" size={17} />
                        ) : (
                          <RefreshCw size={17} />
                        )}
                        重新校验中移接口
                      </button>
                    )}
                  {!dashboard.credential ? (
                    <button
                      className="primary-button"
                      disabled={busy === "apply"}
                      onClick={applyCredential}
                      type="button"
                    >
                      {busy === "apply" ? (
                        <LoaderCircle className="spin" size={17} />
                      ) : (
                        <FileCheck2 size={17} />
                      )}
                      {admission?.externalAgentName
                        ? "申领凭证并加入协作"
                        : "申领智能体身份凭证"}
                    </button>
                  ) : dashboard.credential.status === "ACTIVE" ? (
                    <button
                      className="danger-button"
                      disabled={busy === "status"}
                      onClick={() =>
                        mutate(
                          "status",
                          `/api/agents/${dashboard.agent.id}/credential/simulate-status`,
                          {
                            method: "POST",
                            body: JSON.stringify({ status: "REVOKED" }),
                          },
                        )
                      }
                      type="button"
                    >
                      <LockKeyhole size={17} />
                      模拟吊销
                    </button>
                  ) : (
                    <button
                      className="primary-button"
                      disabled={busy === "status"}
                      onClick={() =>
                        mutate(
                          "status",
                          `/api/agents/${dashboard.agent.id}/credential/simulate-status`,
                          {
                            method: "POST",
                            body: JSON.stringify({ status: "ACTIVE" }),
                          },
                        )
                      }
                      type="button"
                    >
                      <ShieldCheck size={17} />
                      恢复有效
                    </button>
                  )}
                </div>
              </div>

              <div className="credential-timeline">
                <div className="view-heading compact-heading">
                  <div>
                    <span className="section-kicker">ISSUANCE TRACE</span>
                    <h2>申领与签发轨迹</h2>
                  </div>
                </div>
                {dashboard.credentialEvents.length ? (
                  <ol>
                    {dashboard.credentialEvents.map((event, index) => (
                      <li key={event.id}>
                        <span className="timeline-index">
                          {String(index + 1).padStart(2, "0")}
                        </span>
                        <div>
                          <strong>
                            {stepLabels[event.step] ?? event.step}
                          </strong>
                          <p>{event.detail}</p>
                          <time>{formatTime(event.createdAt, true)}</time>
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <div className="empty-timeline">
                    <Fingerprint size={28} />
                    <strong>等待凭证申领</strong>
                    <span>主人 DID 已就绪，尚未创建 AIC 和 VC。</span>
                  </div>
                )}
              </div>
            </section>
          )}

          {activeTab === "actions" && (
            <section className="content-view action-view">
              <div className="view-heading">
                <div>
                  <span className="section-kicker">AUDIT TRAIL</span>
                  <h2>智能体行为记录</h2>
                </div>
                <p>共 {dashboard.actions.length} 条可追溯记录</p>
              </div>
              <div className="segmented-control" aria-label="筛选行为记录">
                {[
                  ["all", "全部"],
                  ["outgoing", "我的智能体"],
                  ["incoming", "影响我的农场"],
                  ["blocked", "准入拦截"],
                ].map(([key, label]) => (
                  <button
                    className={actionFilter === key ? "active" : ""}
                    key={key}
                    onClick={() => setActionFilter(key as ActionFilter)}
                    type="button"
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="action-table">
                <div className="action-table-head">
                  <span>时间与行为</span>
                  <span>目标</span>
                  <span>凭证校验</span>
                  <span>结果与追踪号</span>
                </div>
                {filteredActions.length ? (
                  filteredActions.map((action) => (
                    <article
                      className={`action-row ${
                        action.status !== "SUCCESS" ? "action-blocked" : ""
                      }`}
                      key={action.id}
                    >
                      <div className="action-main">
                        <span className="action-icon">
                          <ActionIcon type={action.actionType} />
                        </span>
                        <div>
                          <strong>
                            {action.isIncoming
                              ? `${action.agentName}影响了我的农场`
                              : (actionLabels[action.actionType] ??
                                action.actionType)}
                          </strong>
                          <time>{formatTime(action.createdAt, true)}</time>
                        </div>
                      </div>
                      <div>
                        <span className="mobile-label">目标</span>
                        <strong>{action.targetOwnerName}</strong>
                        <small>
                          {action.cropName
                            ? `${action.cropName} × ${action.quantity}`
                            : "身份与状态检查"}
                        </small>
                      </div>
                      <div>
                        <span className="mobile-label">凭证校验</span>
                        <CredentialBadge status={action.credentialStatus} />
                        <span
                          className={`source-chip source-${action.executionMode.toLowerCase()}`}
                        >
                          {action.executionMode === "LLM" ? "模型 Skill" : "规则引擎"}
                        </span>
                        <span
                          className={`admission-chip admission-${action.admissionMode.toLowerCase()}`}
                        >
                          {admissionShortLabels[action.admissionMode]}
                        </span>
                        <small>{action.reason}</small>
                      </div>
                      <div>
                        <span className="mobile-label">结果</span>
                        <strong
                          className={
                            action.status === "SUCCESS"
                              ? "result-success"
                              : "result-blocked"
                          }
                        >
                          {actionResultLabel(action.status)}
                        </strong>
                        <small>{action.traceId}</small>
                      </div>
                    </article>
                  ))
                ) : (
                  <div className="empty-actions">
                    <History size={26} />
                    <span>当前筛选条件下暂无行为记录</span>
                  </div>
                )}
              </div>
            </section>
          )}
        </>
      )}
    </main>
  );
}
