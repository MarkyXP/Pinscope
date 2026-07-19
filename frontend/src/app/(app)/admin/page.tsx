"use client";

import { useCallback, useEffect, useState } from "react";
import { useOptionalUser } from "@/hooks/use-optional-auth";
import { useRouter } from "next/navigation";
import {
  Database,
  Users,
  Shield,
  Loader2,
  Check,
  Package,
  Cpu,
  Zap,
  Trash2,
  DollarSign,
  ChevronDown,
  ChevronRight,
  FolderOpen,
  Activity,
  ExternalLink,
  Clock,
  RotateCw,
  MoreVertical,
  Gauge,
  Copy,
  Settings,
  SlidersHorizontal,
  MessageSquareWarning,
  Search,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  adminAdjustCredits,
  fetchAdminComponents,
  fetchAdminComponentJson,
  fetchAdminUsers,
  searchAdminUsers,
  fetchAdminUsage,
  fetchAdminProjects,
  fetchAdminRuns,
  fetchAdminSettings,
  setMinModelVersion,
  restartPipeline,
  regenPipeline,
  adminMarkProjectComplete,
  deleteAdminComponent,
  deleteAdminFinding,
  fetchAdminFeedback,
  updateAdminFeedback,
  type AdminComponents,
  type AdminUser,
  type AdminUsage,
  type AdminUsageUser,
  type AdminProject,
  type AdminPipelineRun,
  type AdminSettings,
  type FeedbackTicket,
} from "@/lib/api";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

type Tab = "projects" | "runs" | "components" | "users" | "usage" | "overrides" | "feedback" | "settings";

const TABS: { id: Tab; label: string; icon: typeof Database }[] = [
  { id: "projects", label: "Projects", icon: FolderOpen },
  { id: "runs", label: "Pipeline Runs", icon: Activity },
  { id: "components", label: "Components", icon: Database },
  { id: "users", label: "Users & Credits", icon: Users },
  { id: "usage", label: "API Usage", icon: DollarSign },
  { id: "overrides", label: "Overrides", icon: SlidersHorizontal },
  { id: "feedback", label: "Feedback", icon: MessageSquareWarning },
  { id: "settings", label: "Settings", icon: Settings },
];

export default function AdminPage() {
  const { user, isLoaded } = useOptionalUser();
  const router = useRouter();
  const [activeTab, setActiveTab] = useState<Tab>("projects");

  const isAdmin = user?.isAdmin ?? false;

  // Redirect non-admins once Clerk has loaded
  useEffect(() => {
    if (isLoaded && !isAdmin) {
      router.replace("/dashboard");
    }
  }, [isLoaded, isAdmin, router]);

  if (!isLoaded) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!isAdmin) return null;

  return (
    <div className="flex-1 flex flex-col">
      {/* Header */}
      <div className="border-b border-border px-6 py-4">
        <div className="flex items-center gap-2">
          <Shield className="h-5 w-5 text-blue-500" />
          <h1 className="text-lg font-semibold">Admin</h1>
        </div>
        <p className="text-sm text-muted-foreground mt-0.5">
          Manage library components and user settings
        </p>
      </div>

      {/* Body: sidebar tabs + content */}
      <div className="flex-1 flex min-h-0">
        {/* Left tab navigation */}
        <nav className="w-48 shrink-0 border-r border-border p-2 space-y-0.5">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={cn(
                "flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                activeTab === id
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
              )}
            >
              <Icon className="h-4 w-4" />
              {label}
            </button>
          ))}
        </nav>

        {/* Content panel */}
        <div className="flex-1 overflow-auto p-6">
          {activeTab === "projects" && <ProjectsPanel />}
          {activeTab === "runs" && <RunsPanel />}
          {activeTab === "components" && <ComponentsPanel />}
          {activeTab === "users" && <UsersPanel />}
          {activeTab === "usage" && <UsagePanel />}
          {activeTab === "overrides" && <OverridesPanel />}
          {activeTab === "feedback" && <FeedbackPanel />}
          {activeTab === "settings" && <SettingsPanel />}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Projects Panel
// ---------------------------------------------------------------------------

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
  complete: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300",
  error: "bg-rose-100 text-rose-700 dark:bg-rose-900 dark:text-rose-300",
  cancelled: "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300",
  paused_insufficient_credits: "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300",
  paused_by_user: "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300",
};

function formatCostShort(usd: number | null): string {
  if (!usd || usd === 0) return "-";
  if (usd < 0.01) return "<$0.01";
  return "$" + usd.toFixed(2);
}

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function ProjectsPanel() {
  const router = useRouter();
  const [projects, setProjects] = useState<AdminProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [restarting, setRestarting] = useState<string | null>(null);

  async function handleRestart(projectId: string) {
    setRestarting(projectId);
    try {
      await restartPipeline(projectId);
      setProjects((prev) =>
        prev.map((p) => (p.id === projectId ? { ...p, status: "running", pipeline_state: null } : p)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to restart pipeline");
    } finally {
      setRestarting(null);
    }
  }

  async function handleRegen(projectId: string, stages: string[]) {
    setRestarting(projectId);
    try {
      await regenPipeline(projectId, stages);
      setProjects((prev) =>
        prev.map((p) => (p.id === projectId ? { ...p, status: "running", pipeline_state: null } : p)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start regen");
    } finally {
      setRestarting(null);
    }
  }

  function handleCloneAsNew(projectId: string) {
    // Stash the source project id and hand off to the dashboard, which
    // mounts the create-project dialog. The dialog fetches the full
    // Project on its end so we don't have to pass the entire object here.
    window.sessionStorage.setItem("pinscopex:cloneAsNewProjectId", projectId);
    router.push("/dashboard");
  }

  async function handleMarkComplete(projectId: string) {
    setRestarting(projectId);
    try {
      await adminMarkProjectComplete(projectId);
      setProjects((prev) =>
        prev.map((p) => (p.id === projectId ? { ...p, status: "complete" } : p)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to mark complete");
    } finally {
      setRestarting(null);
    }
  }

  useEffect(() => {
    fetchAdminProjects()
      .then(setProjects)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading)
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading projects...
      </div>
    );
  if (error)
    return <p className="text-sm text-destructive">Error: {error}</p>;

  const lf = filter.toLowerCase();
  const filtered = projects
    .filter(
      (p) =>
        p.name.toLowerCase().includes(lf) ||
        (p.owner_name ?? "").toLowerCase().includes(lf) ||
        (p.owner_email ?? "").toLowerCase().includes(lf) ||
        p.status.toLowerCase().includes(lf),
    )
    .sort((a, b) => new Date(b.created).getTime() - new Date(a.created).getTime());

  const statusCounts = projects.reduce<Record<string, number>>((acc, p) => {
    acc[p.status] = (acc[p.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-6">
      {/* Stats bar */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5 text-sm">
          <FolderOpen className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          <span className="font-medium">{projects.length}</span>
          <span className="text-muted-foreground">total</span>
        </div>
        {Object.entries(statusCounts).map(([status, count]) => (
          <div key={status} className="flex items-center gap-1.5 text-sm">
            <span className={cn("inline-block h-2 w-2 rounded-full", {
              "bg-zinc-500 dark:bg-zinc-400": status === "draft",
              "bg-blue-500": status === "running",
              "bg-emerald-500": status === "complete",
              "bg-rose-500": status === "error",
              "bg-amber-500": status === "cancelled",
            })} />
            <span className="font-medium">{count}</span>
            <span className="text-muted-foreground capitalize">{status}</span>
          </div>
        ))}
        <div className="flex-1" />
        <Input
          placeholder="Filter by name, owner, or status..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="w-72"
        />
      </div>

      {filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <FolderOpen className="h-8 w-8 mb-2" />
          <p className="text-sm">
            {filter ? "No projects match your filter" : "No projects yet"}
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-muted/50 text-muted-foreground">
                <th className="text-left px-3 py-2 font-medium">Project</th>
                <th className="text-left px-3 py-2 font-medium">Owner</th>
                <th className="text-center px-3 py-2 font-medium">Status</th>
                <th className="text-right px-3 py-2 font-medium">Cost</th>
                <th className="text-right px-3 py-2 font-medium">Created</th>
                <th className="text-left px-3 py-2 font-medium">Error</th>
                <th className="text-center px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filtered.map((p) => {
                const errorMsg = p.status === "error" && p.pipeline_state
                  ? (p.pipeline_state as Record<string, string>).error
                  : null;
                return (
                  <tr
                    key={p.id}
                    className="hover:bg-muted/30 cursor-pointer"
                    onClick={() => router.push(`/project/${p.id}`)}
                  >
                    <td className="px-3 py-2">
                      <div className="font-medium">{p.name}</div>
                      <div className="text-xs text-muted-foreground font-mono">{p.id.slice(0, 8)}</div>
                    </td>
                    <td className="px-3 py-2">
                      <div className="min-w-0">
                        {p.owner_name ? (
                          <div className="text-sm truncate">{p.owner_name}</div>
                        ) : null}
                        <div className="text-xs text-muted-foreground truncate">
                          {p.owner_email || p.user_id.slice(0, 12)}
                        </div>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-center">
                      <span className={cn(
                        "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium capitalize",
                        STATUS_COLORS[p.status] || STATUS_COLORS.draft,
                      )}>
                        {p.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-xs">
                      {formatCostShort(p.total_cost_usd)}
                    </td>
                    <td className="px-3 py-2 text-right text-xs text-muted-foreground">
                      {formatRelativeTime(p.created)}
                    </td>
                    <td className="px-3 py-2 max-w-xs">
                      {errorMsg ? (
                        <span className="text-xs text-rose-600 dark:text-rose-500 line-clamp-2">{errorMsg}</span>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-center" onClick={(e) => e.stopPropagation()}>
                      {p.has_bom && p.has_netlist && (
                        restarting === p.id ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin mx-auto text-muted-foreground" />
                        ) : (
                          <DropdownMenu>
                            <DropdownMenuTrigger>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 w-7 p-0"
                                title="Pipeline actions"
                              >
                                <MoreVertical className="h-3.5 w-3.5" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end" side="bottom">
                              {(p.status === "paused_insufficient_credits" ||
                                p.status === "paused_by_user") && (
                                <>
                                  <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                                    Paused run
                                  </div>
                                  <DropdownMenuItem onClick={() => handleMarkComplete(p.id)}>
                                    <Check className="h-3.5 w-3.5" />
                                    Mark as complete
                                  </DropdownMenuItem>
                                  <DropdownMenuSeparator />
                                </>
                              )}
                              <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                                Free — not charged to user
                              </div>
                              <DropdownMenuItem onClick={() => handleRestart(p.id)}>
                                <RotateCw className="h-3.5 w-3.5" />
                                Rerun full pipeline
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                                Regen only
                              </div>
                              <DropdownMenuItem onClick={() => handleRegen(p.id, ["derating"])}>
                                <Gauge className="h-3.5 w-3.5" />
                                Regen derating
                              </DropdownMenuItem>
                              <DropdownMenuSeparator />
                              <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                                Clone
                              </div>
                              <DropdownMenuItem onClick={() => handleCloneAsNew(p.id)}>
                                <Copy className="h-3.5 w-3.5" />
                                Rerun as new project
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        )
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pipeline Runs Panel
// ---------------------------------------------------------------------------

const STAGE_LABELS: Record<string, string> = {
  bom_parse: "Parsing BOM",
  ic_extraction: "IC Extraction",
  simple_extraction: "Specs Extraction",
  passive_extraction: "Passive Extraction",
  digikey_resolve: "DigiKey Resolve",
  graph_build: "Building Graph",
  bom_summary: "BOM Summary",
  derating: "Derating",
  validation: "Validation",
};

function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}m ${s}s`;
}

function RunsPanel() {
  const router = useRouter();
  const [runs, setRuns] = useState<AdminPipelineRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(() => {
    fetchAdminRuns()
      .then(setRuns)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchRuns();
    const interval = setInterval(fetchRuns, 5000);
    return () => clearInterval(interval);
  }, [fetchRuns]);

  if (loading)
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading pipeline runs...
      </div>
    );
  if (error)
    return <p className="text-sm text-destructive">Error: {error}</p>;

  if (runs.length === 0) {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Activity className="h-4 w-4" />
          <span>Auto-refreshes every 5 seconds</span>
        </div>
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <Activity className="h-8 w-8 mb-2" />
          <p className="text-sm">No pipelines currently running</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5 text-sm">
          <Activity className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          <span className="font-medium">{runs.length}</span>
          <span className="text-muted-foreground">running</span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Clock className="h-3.5 w-3.5" />
          Auto-refreshes every 5s
        </div>
      </div>

      <div className="rounded-lg border border-border overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-muted/50 text-muted-foreground">
              <th className="text-left px-3 py-2 font-medium">Project</th>
              <th className="text-left px-3 py-2 font-medium">Owner</th>
              <th className="text-left px-3 py-2 font-medium">Stage</th>
              <th className="text-left px-3 py-2 font-medium">Substep</th>
              <th className="text-right px-3 py-2 font-medium">Duration</th>
              <th className="w-8 px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {runs.map((run) => (
              <tr
                key={run.project_id}
                className="hover:bg-muted/30 cursor-pointer"
                onClick={() => router.push(`/project/${run.project_id}/progress`)}
              >
                <td className="px-3 py-2 font-medium">{run.project_name}</td>
                <td className="px-3 py-2">
                  <div className="min-w-0">
                    {run.owner_name ? (
                      <div className="text-sm truncate">{run.owner_name}</div>
                    ) : null}
                    <div className="text-xs text-muted-foreground truncate">
                      {run.owner_email || run.user_id.slice(0, 12)}
                    </div>
                  </div>
                </td>
                <td className="px-3 py-2">
                  {run.current_stage ? (
                    <span className={cn(
                      "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium",
                      "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
                    )}>
                      {STAGE_LABELS[run.current_stage] || run.current_stage}
                    </span>
                  ) : (
                    <span className="text-muted-foreground text-xs">Starting...</span>
                  )}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-muted-foreground max-w-xs truncate">
                  {run.current_substep || "-"}
                </td>
                <td className="px-3 py-2 text-right font-mono text-xs">
                  {formatDuration(run.duration_seconds)}
                </td>
                <td className="px-3 py-2">
                  <ExternalLink className="h-3.5 w-3.5 text-muted-foreground" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Components Panel
// ---------------------------------------------------------------------------

function ComponentsPanel() {
  const [data, setData] = useState<AdminComponents | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);
  const [selectedComponent, setSelectedComponent] = useState<{
    type: "ic" | "passive" | "simple";
    name: string;
  } | null>(null);
  const [componentJson, setComponentJson] = useState<Record<string, unknown> | null>(null);
  const [jsonLoading, setJsonLoading] = useState(false);

  function reload() {
    setError(null);
    fetchAdminComponents()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    reload();
  }, []);

  async function handleDelete(type: "ic" | "passive" | "simple", name: string) {
    const label = type === "ic" ? "IC extraction" : type === "passive" ? "passive pattern" : "component specs";
    if (!confirm(`Delete ${label} "${name}"? This cannot be undone.`)) return;
    setDeleting(name);
    setError(null);
    try {
      await deleteAdminComponent(type, name);
      // Remove from local state immediately, then refresh from server
      setData((prev) => {
        if (!prev) return prev;
        if (type === "ic") return { ...prev, ics: prev.ics.filter((c) => c.mpn !== name) };
        if (type === "passive") return { ...prev, passives: prev.passives.filter((c) => c.mpn !== name) };
        return { ...prev, simple: prev.simple.filter((c) => c.mpn !== name) };
      });
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setDeleting(null);
    }
  }

  async function handleRowClick(type: "ic" | "passive" | "simple", name: string) {
    setSelectedComponent({ type, name });
    setComponentJson(null);
    setJsonLoading(true);
    try {
      const json = await fetchAdminComponentJson(type, name);
      setComponentJson(json);
    } catch {
      setComponentJson({ error: "Failed to load component JSON" });
    } finally {
      setJsonLoading(false);
    }
  }

  if (loading)
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading components...
      </div>
    );
  if (error)
    return <p className="text-sm text-destructive">Error: {error}</p>;
  if (!data) return null;

  const lf = filter.toLowerCase();
  const filteredICs = data.ics.filter(
    (c) =>
      c.mpn.toLowerCase().includes(lf) ||
      c.subtype.toLowerCase().includes(lf),
  );
  const filteredPassives = data.passives.filter(
    (c) =>
      c.mpn.toLowerCase().includes(lf) ||
      c.subtype.toLowerCase().includes(lf) ||
      c.description.toLowerCase().includes(lf),
  );
  const filteredSimple = (data.simple ?? []).filter(
    (c) =>
      c.mpn.toLowerCase().includes(lf) ||
      c.subtype.toLowerCase().includes(lf) ||
      c.specs_type.toLowerCase().includes(lf),
  );

  return (
    <div className="space-y-6">
      {/* Stats bar */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5 text-sm">
          <Cpu className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          <span className="font-medium">{data.ics.length}</span>
          <span className="text-muted-foreground">ICs</span>
        </div>
        <div className="flex items-center gap-1.5 text-sm">
          <Zap className="h-4 w-4 text-amber-600 dark:text-amber-400" />
          <span className="font-medium">{data.passives.length}</span>
          <span className="text-muted-foreground">Passive Patterns</span>
        </div>
        {(data.simple?.length ?? 0) > 0 && (
          <div className="flex items-center gap-1.5 text-sm">
            <Zap className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
            <span className="font-medium">{data.simple.length}</span>
            <span className="text-muted-foreground">Component Specs</span>
          </div>
        )}
        <div className="flex-1" />
        <Input
          placeholder="Filter by MPN or type..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="w-64"
        />
      </div>

      {/* IC table */}
      {filteredICs.length > 0 && (
        <div>
          <h2 className="text-sm font-medium mb-2">IC Extractions</h2>
          <div className="rounded-lg border border-border overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-muted/50 text-muted-foreground">
                  <th className="text-left px-3 py-2 font-medium">MPN</th>
                  <th className="text-left px-3 py-2 font-medium">Subtype</th>
                  <th className="text-center px-3 py-2 font-medium">Pins</th>
                  <th className="text-center px-3 py-2 font-medium">
                    Abs Max
                  </th>
                  <th className="w-10 px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredICs.map((ic) => (
                  <tr key={ic.mpn} className="hover:bg-muted/30 cursor-pointer" onClick={() => handleRowClick("ic", ic.mpn)}>
                    <td className="px-3 py-2 font-mono text-xs">{ic.mpn}</td>
                    <td className="px-3 py-2">
                      {ic.subtype ? (
                        <Badge variant="secondary">{ic.subtype}</Badge>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-center">{ic.pin_count}</td>
                    <td className="px-3 py-2 text-center">
                      {ic.has_ratings ? (
                        <Check className="h-4 w-4 text-emerald-500 mx-auto" />
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-center">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete("ic", ic.mpn); }}
                        disabled={deleting === ic.mpn}
                        className="text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50"
                      >
                        {deleting === ic.mpn ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Passive patterns table */}
      {filteredPassives.length > 0 && (
        <div>
          <h2 className="text-sm font-medium mb-2">Passive Patterns</h2>
          <div className="rounded-lg border border-border overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-muted/50 text-muted-foreground">
                  <th className="text-left px-3 py-2 font-medium">Name</th>
                  <th className="text-left px-3 py-2 font-medium">Type</th>
                  <th className="text-left px-3 py-2 font-medium">
                    Description
                  </th>
                  <th className="text-left px-3 py-2 font-medium">Regex</th>
                  <th className="w-10 px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredPassives.map((p) => (
                  <tr key={p.mpn} className="hover:bg-muted/30 cursor-pointer" onClick={() => handleRowClick("passive", p.mpn)}>
                    <td className="px-3 py-2 font-mono text-xs">{p.mpn}</td>
                    <td className="px-3 py-2">
                      {p.subtype ? (
                        <Badge variant="secondary">{p.subtype}</Badge>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground max-w-xs truncate">
                      {p.description || "-"}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs max-w-xs truncate text-muted-foreground">
                      {p.regex || "-"}
                    </td>
                    <td className="px-3 py-2 text-center">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete("passive", p.mpn); }}
                        disabled={deleting === p.mpn}
                        className="text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50"
                      >
                        {deleting === p.mpn ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Simple component specs table */}
      {filteredSimple.length > 0 && (
        <div>
          <h2 className="text-sm font-medium mb-2">Component Specs</h2>
          <div className="rounded-lg border border-border overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-muted/50 text-muted-foreground">
                  <th className="text-left px-3 py-2 font-medium">MPN</th>
                  <th className="text-left px-3 py-2 font-medium">Type</th>
                  <th className="text-left px-3 py-2 font-medium">Subtype</th>
                  <th className="text-center px-3 py-2 font-medium">Params</th>
                  <th className="w-10 px-3 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredSimple.map((s) => (
                  <tr key={s.mpn} className="hover:bg-muted/30 cursor-pointer" onClick={() => handleRowClick("simple", s.mpn)}>
                    <td className="px-3 py-2 font-mono text-xs">{s.mpn}</td>
                    <td className="px-3 py-2">
                      <Badge variant="secondary">{s.specs_type}</Badge>
                    </td>
                    <td className="px-3 py-2">
                      {s.subtype ? (
                        <Badge variant="outline">{s.subtype}</Badge>
                      ) : (
                        <span className="text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-center font-mono">{s.param_count}</td>
                    <td className="px-3 py-2 text-center">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete("simple", s.mpn); }}
                        disabled={deleting === s.mpn}
                        className="text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50"
                      >
                        {deleting === s.mpn ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {filteredICs.length === 0 && filteredPassives.length === 0 && filteredSimple.length === 0 && (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <Package className="h-8 w-8 mb-2" />
          <p className="text-sm">
            {filter ? "No components match your filter" : "No components in the library yet"}
          </p>
        </div>
      )}

      {/* Component JSON viewer dialog */}
      <Dialog
        open={selectedComponent !== null}
        onOpenChange={(open) => { if (!open) setSelectedComponent(null); }}
      >
        <DialogContent className="sm:max-w-2xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="font-mono text-sm">
              {selectedComponent?.name}
            </DialogTitle>
          </DialogHeader>
          <div className="flex-1 min-h-0 overflow-auto">
            {jsonLoading ? (
              <div className="flex items-center gap-2 text-muted-foreground text-sm py-8 justify-center">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading...
              </div>
            ) : componentJson ? (
              <pre className="text-xs font-mono bg-muted/50 rounded-lg p-4 overflow-auto whitespace-pre-wrap break-all">
                {JSON.stringify(componentJson, null, 2)}
              </pre>
            ) : null}
          </div>
          <DialogFooter showCloseButton />
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users Panel
// ---------------------------------------------------------------------------

function UsersTable({
  users,
  onAdjust,
}: {
  users: AdminUser[];
  onAdjust: (u: AdminUser) => void;
}) {
  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-muted/50 text-muted-foreground">
            <th className="text-left px-3 py-2 font-medium">User</th>
            <th className="text-center px-3 py-2 font-medium">Projects</th>
            <th className="text-right px-3 py-2 font-medium">Balance</th>
            <th className="text-right px-3 py-2 font-medium" />
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {users.map((u) => (
            <tr key={u.user_id} className="hover:bg-muted/30">
              <td className="px-3 py-2">
                <div className="flex items-center gap-2.5">
                  {u.image_url ? (
                    <img
                      src={u.image_url}
                      alt=""
                      className="h-7 w-7 rounded-full shrink-0"
                    />
                  ) : (
                    <div className="h-7 w-7 rounded-full bg-muted flex items-center justify-center shrink-0">
                      <Users className="h-3.5 w-3.5 text-muted-foreground" />
                    </div>
                  )}
                  <div className="min-w-0">
                    {u.name ? (
                      <>
                        <div className="text-sm font-medium truncate">{u.name}</div>
                        {u.email && (
                          <div className="text-xs text-muted-foreground truncate">{u.email}</div>
                        )}
                      </>
                    ) : (
                      <div className="font-mono text-xs text-muted-foreground truncate">
                        {u.email || u.user_id}
                      </div>
                    )}
                  </div>
                </div>
              </td>
              <td className="px-3 py-2 text-center">{u.project_count}</td>
              <td className="px-3 py-2 text-right font-mono text-xs tabular-nums">
                {u.balance.toFixed(2)}
              </td>
              <td className="px-3 py-2 text-right">
                <Button variant="outline" size="sm" onClick={() => onAdjust(u)}>
                  Adjust
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UsersPanel() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adjusting, setAdjusting] = useState<AdminUser | null>(null);

  // Email search — finds any registered user via Clerk, including those
  // with no project and no credit activity yet.
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [results, setResults] = useState<AdminUser[] | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    fetchAdminUsers()
      .then(setUsers)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const runSearch = useCallback(async () => {
    const email = query.trim();
    if (!email) {
      setResults(null);
      setSearchError(null);
      return;
    }
    setSearching(true);
    setSearchError(null);
    try {
      setResults(await searchAdminUsers(email));
    } catch (e) {
      setSearchError(e instanceof Error ? e.message : "Search failed");
      setResults(null);
    } finally {
      setSearching(false);
    }
  }, [query]);

  const clearSearch = useCallback(() => {
    setQuery("");
    setResults(null);
    setSearchError(null);
  }, []);

  const afterAdjust = useCallback(() => {
    setAdjusting(null);
    reload();
    if (results !== null) runSearch();
  }, [reload, results, runSearch]);

  if (loading)
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading users...
      </div>
    );
  if (error)
    return <p className="text-sm text-destructive">Error: {error}</p>;

  const showingSearch = results !== null;
  const list = showingSearch ? results : users;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Input
          type="email"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") runSearch();
          }}
          placeholder="Find any user by email…"
          className="max-w-xs"
        />
        <Button
          variant="outline"
          size="sm"
          onClick={runSearch}
          disabled={searching || !query.trim()}
        >
          {searching ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Search className="h-3.5 w-3.5" />
          )}
          <span className="ml-1.5">Search</span>
        </Button>
        {showingSearch && (
          <Button variant="ghost" size="sm" onClick={clearSearch}>
            Clear
          </Button>
        )}
      </div>
      {searchError && <p className="text-xs text-destructive">{searchError}</p>}

      <h2 className="text-sm font-medium">
        {showingSearch ? `Search results (${list.length})` : `Users (${list.length})`}
      </h2>

      {list.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <Users className="h-8 w-8 mb-2" />
          <p className="text-sm">
            {showingSearch ? "No user found with that email" : "No users yet"}
          </p>
        </div>
      ) : (
        <UsersTable users={list} onAdjust={setAdjusting} />
      )}

      {adjusting && (
        <AdjustCreditsDialog
          user={adjusting}
          onClose={() => setAdjusting(null)}
          onAdjusted={afterAdjust}
        />
      )}
    </div>
  );
}

function AdjustCreditsDialog({
  user,
  onClose,
  onAdjusted,
}: {
  user: AdminUser;
  onClose: () => void;
  onAdjusted: () => void;
}) {
  const [delta, setDelta] = useState("0");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const parsed = parseFloat(delta);
  const valid = !Number.isNaN(parsed) && parsed !== 0 && note.trim().length > 0;

  async function handleSave() {
    if (!valid) return;
    setSaving(true);
    setError(null);
    try {
      await adminAdjustCredits(user.user_id, parsed, note.trim());
      onAdjusted();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            Adjust credits — {user.name || user.email || user.user_id}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div>
            <div className="text-xs text-muted-foreground mb-1">Current balance</div>
            <div className="font-mono text-lg">{user.balance.toFixed(2)}</div>
          </div>
          <div>
            <label className="text-xs font-medium" htmlFor="delta">
              Delta (credits, positive or negative)
            </label>
            <Input
              id="delta"
              type="number"
              step="0.01"
              value={delta}
              onChange={(e) => setDelta(e.target.value)}
              className="font-mono mt-1"
            />
          </div>
          <div>
            <label className="text-xs font-medium" htmlFor="note">
              Note (required)
            </label>
            <Input
              id="note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. goodwill / correction"
              className="mt-1"
            />
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!valid || saving}>
            {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : "Apply"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Usage Panel
// ---------------------------------------------------------------------------

function formatCost(usd: number): string {
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return "<$0.01";
  return "$" + usd.toFixed(2);
}

function UsagePanel() {
  const [data, setData] = useState<AdminUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedUser, setExpandedUser] = useState<string | null>(null);

  useEffect(() => {
    fetchAdminUsage()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading)
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading usage data...
      </div>
    );
  if (error)
    return <p className="text-sm text-destructive">Error: {error}</p>;
  if (!data) return null;

  const sortedUsers = [...data.users].sort(
    (a, b) => b.total_cost_usd - a.total_cost_usd,
  );

  return (
    <div className="space-y-6">
      {/* Grand total */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-1.5 text-sm">
          <DollarSign className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
          <span className="font-medium">{formatCost(data.grand_total_usd)}</span>
          <span className="text-muted-foreground">total spend</span>
        </div>
        <div className="flex items-center gap-1.5 text-sm">
          <Users className="h-4 w-4 text-blue-600 dark:text-blue-400" />
          <span className="font-medium">{data.users.length}</span>
          <span className="text-muted-foreground">users</span>
        </div>
      </div>

      {sortedUsers.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
          <DollarSign className="h-8 w-8 mb-2" />
          <p className="text-sm">No usage data yet</p>
        </div>
      ) : (
        <div>
          <h2 className="text-sm font-medium mb-2">Usage by User</h2>
          <div className="rounded-lg border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-muted/50 text-muted-foreground">
                  <th className="w-8 px-3 py-2" />
                  <th className="text-left px-3 py-2 font-medium">User</th>
                  <th className="text-center px-3 py-2 font-medium">Projects</th>
                  <th className="text-right px-3 py-2 font-medium">Cost</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {sortedUsers.map((u) => (
                  <UserUsageRow
                    key={u.user_id}
                    user={u}
                    expanded={expandedUser === u.user_id}
                    onToggle={() =>
                      setExpandedUser(
                        expandedUser === u.user_id ? null : u.user_id,
                      )
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function UserUsageRow({
  user,
  expanded,
  onToggle,
}: {
  user: AdminUsageUser;
  expanded: boolean;
  onToggle: () => void;
}) {
  const sortedProjects = [...user.projects].sort(
    (a, b) => b.cost_usd - a.cost_usd,
  );

  return (
    <>
      <tr
        className="hover:bg-muted/30 cursor-pointer"
        onClick={onToggle}
      >
        <td className="px-3 py-2 text-muted-foreground">
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
        </td>
        <td className="px-3 py-2">
          <div className="min-w-0">
            {user.name ? (
              <>
                <div className="text-sm font-medium truncate">{user.name}</div>
                {user.email && (
                  <div className="text-xs text-muted-foreground truncate">
                    {user.email}
                  </div>
                )}
              </>
            ) : (
              <div className="font-mono text-xs text-muted-foreground truncate">
                {user.email || user.user_id}
              </div>
            )}
          </div>
        </td>
        <td className="px-3 py-2 text-center">{user.project_count}</td>
        <td className="px-3 py-2 text-right font-mono text-xs">
          {formatCost(user.total_cost_usd)}
        </td>
      </tr>
      {expanded &&
        sortedProjects.map((p) => (
          <tr key={p.id} className="bg-muted/20">
            <td className="px-3 py-1.5" />
            <td className="px-3 py-1.5 pl-8 text-xs text-muted-foreground">
              {p.name}
            </td>
            <td className="px-3 py-1.5 text-center">
              <Badge variant="outline" className="text-[10px] px-1.5 py-0 capitalize">
                {p.status}
              </Badge>
            </td>
            <td className="px-3 py-1.5 text-right font-mono text-xs text-muted-foreground">
              {formatCost(p.cost_usd)}
            </td>
          </tr>
        ))}
    </>
  );
}

// ---------------------------------------------------------------------------
// Settings Panel
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Overrides Panel
// ---------------------------------------------------------------------------

function OverridesPanel() {
  const [projectId, setProjectId] = useState("");
  const [ruleId, setRuleId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    deleted: string;
    project_id: string;
    remaining: number;
  } | null>(null);

  async function handleDelete() {
    if (!projectId.trim() || !ruleId.trim()) return;
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const res = await deleteAdminFinding(projectId.trim(), ruleId.trim());
      setResult({
        deleted: res.deleted,
        project_id: res.project_id,
        remaining: res.remaining,
      });
      setRuleId("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete rule");
    } finally {
      setSubmitting(false);
    }
  }

  const canSubmit = projectId.trim() && ruleId.trim() && !submitting;

  return (
    <div className="space-y-6 max-w-lg">
      <div>
        <h2 className="text-sm font-medium mb-1">Overrides</h2>
        <p className="text-xs text-muted-foreground">
          Manual corrections to pipeline outputs. More override tools will land
          here over time.
        </p>
      </div>

      <div className="rounded-lg border border-border p-4 space-y-4">
        <div>
          <div className="text-sm font-medium">Delete rule from report</div>
          <div className="text-xs text-muted-foreground">
            Removes a single finding from the project&apos;s report.json and
            refreshes the summary counts. No-op if the rule is not in the
            report.
          </div>
        </div>

        <div className="space-y-3">
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Project ID
            </label>
            <Input
              value={projectId}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                setProjectId(e.target.value)
              }
              placeholder="e.g. 7f3a2c1e-..."
              className="font-mono"
            />
          </div>

          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              Rule ID
            </label>
            <Input
              value={ruleId}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                setRuleId(e.target.value)
              }
              placeholder="e.g. U3-001"
              className="font-mono"
            />
          </div>

          <div className="flex items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={!canSubmit}
              onClick={handleDelete}
            >
              {submitting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <>
                  <Trash2 className="h-3 w-3 mr-1" />
                  Delete rule
                </>
              )}
            </Button>
          </div>
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        {result && (
          <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
            <div className="text-emerald-700 dark:text-emerald-300">
              Deleted rule{" "}
              <span className="font-mono">{result.deleted}</span> from project{" "}
              <span className="font-mono">{result.project_id}</span>.
            </div>
            <div className="text-xs text-muted-foreground mt-1">
              {result.remaining} finding{result.remaining === 1 ? "" : "s"}{" "}
              remaining in report.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function SettingsPanel() {
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editVersion, setEditVersion] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetchAdminSettings()
      .then((data) => {
        setSettings(data);
        setEditVersion(data.min_model_version);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleSave() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await setMinModelVersion(editVersion);
      setSettings((prev) =>
        prev ? { ...prev, min_model_version: editVersion } : prev,
      );
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  const hasChanged = settings && editVersion !== settings.min_model_version;
  const isValidSemver = /^\d+\.\d+\.\d+$/.test(editVersion);

  if (loading)
    return (
      <div className="flex items-center gap-2 text-muted-foreground text-sm">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading settings...
      </div>
    );

  if (!settings) return null;

  return (
    <div className="space-y-6 max-w-lg">
      <div>
        <h2 className="text-sm font-medium mb-1">Model Version</h2>
        <p className="text-xs text-muted-foreground">
          Control when cached library extractions are re-extracted with newer
          skills.
        </p>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="space-y-4">
        {/* Read-only: current default */}
        <div className="flex items-center justify-between rounded-lg border border-border p-3">
          <div>
            <div className="text-sm font-medium">
              Current extraction version
            </div>
            <div className="text-xs text-muted-foreground">
              Hardcoded in backend config (read-only)
            </div>
          </div>
          <Badge variant="secondary" className="font-mono">
            {settings.default_model_version}
          </Badge>
        </div>

        {/* Editable: min version threshold */}
        <div className="rounded-lg border border-border p-3 space-y-3">
          <div>
            <div className="text-sm font-medium">
              Force refresh if older than
            </div>
            <div className="text-xs text-muted-foreground">
              Library components with a model_version below this will be
              re-extracted on next pipeline run. To refresh all existing
              extractions, set this to the current extraction version above.
              Set to 0.0.0 to disable.
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Input
              value={editVersion}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                setEditVersion(e.target.value)
              }
              placeholder="e.g. 1.0.1"
              className="w-32 font-mono"
            />
            <Button
              variant="outline"
              size="sm"
              disabled={!hasChanged || !isValidSemver || saving}
              onClick={handleSave}
            >
              {saving ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : saved ? (
                <Check className="h-3 w-3" />
              ) : (
                "Save"
              )}
            </Button>
          </div>
          {editVersion && !isValidSemver && (
            <p className="text-xs text-destructive">
              Must be a valid semver (e.g. 1.0.1)
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Feedback Panel
// ---------------------------------------------------------------------------

const FEEDBACK_STATUS_STYLES: Record<string, string> = {
  open: "bg-blue-500/15 text-blue-600 dark:text-blue-400 border-blue-500/30",
  acknowledged: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30",
  resolved: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
};


function FeedbackPanel() {
  const [tickets, setTickets] = useState<FeedbackTicket[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selected, setSelected] = useState<FeedbackTicket | null>(null);
  const [editStatus, setEditStatus] = useState("");
  const [editNotes, setEditNotes] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await fetchAdminFeedback();
      setTickets(data);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = tickets.filter((t) => {
    if (statusFilter !== "all" && t.status !== statusFilter) return false;
    if (filter) {
      const q = filter.toLowerCase();
      const searchable = [t.user_name, t.user_email, t.project_name, t.finding_id, t.message]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      if (!searchable.includes(q)) return false;
    }
    return true;
  });

  const counts = {
    total: tickets.length,
    open: tickets.filter((t) => t.status === "open").length,
    acknowledged: tickets.filter((t) => t.status === "acknowledged").length,
    resolved: tickets.filter((t) => t.status === "resolved").length,
  };

  function openDetail(t: FeedbackTicket) {
    setSelected(t);
    setEditStatus(t.status);
    setEditNotes(t.admin_notes ?? "");
  }

  async function handleSave() {
    if (!selected) return;
    setSaving(true);
    try {
      const updated = await updateAdminFeedback(selected.ticket_id, {
        status: editStatus,
        admin_notes: editNotes || undefined,
      });
      setTickets((prev) => prev.map((t) => (t.ticket_id === updated.ticket_id ? updated : t)));
      setSelected(null);
    } catch {
      // ignore
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Stats */}
      <div className="flex gap-4 text-sm">
        <span className="text-muted-foreground">
          Total: <span className="text-foreground font-medium">{counts.total}</span>
        </span>
        <span className="text-blue-600 dark:text-blue-400">
          Open: {counts.open}
        </span>
        <span className="text-amber-600 dark:text-amber-400">
          Acknowledged: {counts.acknowledged}
        </span>
        <span className="text-emerald-600 dark:text-emerald-400">
          Resolved: {counts.resolved}
        </span>
      </div>

      {/* Filters */}
      <div className="flex gap-3">
        <Input
          placeholder="Search by user, project, finding..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-xs"
        />
        <div className="flex gap-1">
          {["all", "open", "acknowledged", "resolved"].map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatusFilter(s)}
              className={cn(
                "px-2.5 py-1 rounded-md text-xs font-medium transition-colors",
                statusFilter === s
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:text-foreground hover:bg-accent/50"
              )}
            >
              {s === "all" ? "All" : s.charAt(0).toUpperCase() + s.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      {filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-12">
          {tickets.length === 0 ? "No feedback tickets yet." : "No tickets match your filters."}
        </p>
      ) : (
        <div className="border border-border rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-muted/50 text-muted-foreground text-xs">
                <th className="text-left px-3 py-2 font-medium">User</th>
                <th className="text-left px-3 py-2 font-medium">Finding</th>
                <th className="text-left px-3 py-2 font-medium">Project</th>
                <th className="text-left px-3 py-2 font-medium">Message</th>
                <th className="text-left px-3 py-2 font-medium">Status</th>
                <th className="text-left px-3 py-2 font-medium">Created</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t) => (
                <tr
                  key={t.ticket_id}
                  onClick={() => openDetail(t)}
                  className="border-b border-border last:border-0 hover:bg-accent/30 cursor-pointer transition-colors"
                >
                  <td className="px-3 py-2.5">
                    <div className="text-sm truncate max-w-[140px]">{t.user_name ?? "—"}</div>
                    <div className="text-xs text-muted-foreground truncate max-w-[140px]">{t.user_email ?? ""}</div>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-xs text-muted-foreground">
                    {t.finding_id ?? "—"}
                  </td>
                  <td className="px-3 py-2.5 text-xs text-muted-foreground truncate max-w-[120px]">
                    {t.project_name ?? "—"}
                  </td>
                  <td className="px-3 py-2.5 text-xs text-muted-foreground">
                    <span className="line-clamp-2 max-w-[200px]">{t.message}</span>
                  </td>
                  <td className="px-3 py-2.5">
                    <Badge variant="outline" className={`text-xs ${FEEDBACK_STATUS_STYLES[t.status] ?? ""}`}>
                      {t.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                    {formatRelativeTime(t.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Detail dialog */}
      <Dialog open={!!selected} onOpenChange={(open) => { if (!open) setSelected(null); }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Feedback Detail</DialogTitle>
          </DialogHeader>
          {selected && (
            <div className="space-y-4">
              {/* Meta */}
              <div className="flex items-center gap-2 flex-wrap">
                <Badge variant="outline" className={`text-xs ${FEEDBACK_STATUS_STYLES[selected.status] ?? ""}`}>
                  {selected.status}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  by {selected.user_name ?? selected.user_email ?? selected.user_id}
                </span>
                <span className="text-xs text-muted-foreground ml-auto">
                  {formatRelativeTime(selected.created_at)}
                </span>
              </div>

              {/* Finding context */}
              {selected.finding_id && (
                <div className="rounded-lg border border-border bg-muted/30 p-3 space-y-1.5">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="font-mono font-medium">{selected.finding_id}</span>
                    {selected.finding_designator && (
                      <span className="font-mono text-muted-foreground">{selected.finding_designator}</span>
                    )}
                    {selected.finding_mpn && (
                      <span className="font-mono text-muted-foreground">{selected.finding_mpn}</span>
                    )}
                    {selected.finding_status && (
                      <Badge variant="outline" className="text-xs">
                        {selected.finding_status}
                      </Badge>
                    )}
                  </div>
                  {selected.finding_text && (
                    <p className="text-sm">{selected.finding_text}</p>
                  )}
                </div>
              )}

              {/* Project */}
              {selected.project_name && (
                <p className="text-xs text-muted-foreground">
                  Project: <span className="text-foreground">{selected.project_name}</span>
                </p>
              )}

              {/* Message */}
              <div className="rounded-lg border border-border p-3">
                <p className="text-sm whitespace-pre-wrap">{selected.message}</p>
              </div>

              {/* Status update */}
              <div className="space-y-2">
                <label className="text-xs font-medium text-muted-foreground">Status</label>
                <div className="flex gap-1">
                  {(["open", "acknowledged", "resolved"] as const).map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => setEditStatus(s)}
                      className={cn(
                        "px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                        editStatus === s
                          ? "bg-accent text-accent-foreground"
                          : "text-muted-foreground hover:text-foreground hover:bg-accent/50"
                      )}
                    >
                      {s.charAt(0).toUpperCase() + s.slice(1)}
                    </button>
                  ))}
                </div>
              </div>

              {/* Admin notes */}
              <div className="space-y-2">
                <label className="text-xs font-medium text-muted-foreground">Admin Notes</label>
                <Textarea
                  value={editNotes}
                  onChange={(e) => setEditNotes(e.target.value)}
                  placeholder="Internal notes or response to the user..."
                  className="min-h-[80px]"
                />
              </div>
            </div>
          )}
          <DialogFooter>
            <Button onClick={handleSave} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
