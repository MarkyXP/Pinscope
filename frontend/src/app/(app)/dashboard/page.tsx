"use client";

import { Suspense, useState, useEffect, useMemo } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowRight, CheckCircle2, LayoutGrid, List, Loader2, X } from "lucide-react";
import { ProjectCard } from "@/components/dashboard/project-card";
import { ProjectsTable } from "@/components/dashboard/projects-table";
import { CreateProjectDialog } from "@/components/dashboard/create-project-dialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  fetchProject,
  fetchProjects,
  reconcileCheckoutSession,
} from "@/lib/api";
import type { CreditSnapshot, Project } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useCredits } from "@/components/billing/credits-context";
// import { OnboardingSurvey } from "@/components/dashboard/onboarding-survey";

type ViewMode = "cards" | "table";
const VIEW_STORAGE_KEY = "pinscopex:dashboard:view";

export default function DashboardPage() {
  return (
    <Suspense>
      <DashboardContent />
    </Suspense>
  );
}

type CheckoutState = "pending" | "activated" | "timeout" | "dismissed";

function DashboardContent() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [rerunProject, setRerunProject] = useState<Project | null>(null);
  const [cloneAsNewProject, setCloneAsNewProject] = useState<Project | null>(null);
  const { credits, refresh: refreshCredits } = useCredits();
  const [showCheckoutBanner, setShowCheckoutBanner] = useState(false);
  const [checkoutState, setCheckoutState] =
    useState<CheckoutState>("pending");
  const [activatedDetail, setActivatedDetail] = useState<string | null>(null);
  const [view, setView] = useState<ViewMode>("cards");
  const router = useRouter();
  const searchParams = useSearchParams();

  // Restore persisted UI preferences once on mount.
  useEffect(() => {
    try {
      const v = localStorage.getItem(VIEW_STORAGE_KEY);
      if (v === "cards" || v === "table") setView(v);
    } catch {
      // ignore
    }
  }, []);

  function updateView(next: ViewMode) {
    setView(next);
    try { localStorage.setItem(VIEW_STORAGE_KEY, next); } catch { /* ignore */ }
  }

  const sortedProjects = useMemo(
    () =>
      [...projects].sort((a, b) => {
        const ta = new Date(a.created).getTime();
        const tb = new Date(b.created).getTime();
        return (Number.isFinite(tb) ? tb : 0) - (Number.isFinite(ta) ? ta : 0);
      }),
    [projects],
  );

  useEffect(() => {
    fetchProjects()
      .then(setProjects)
      .finally(() => setLoading(false));

    // Admin handoff: "Rerun as new project" stashes a project ID here.
    const cloneId =
      typeof window !== "undefined"
        ? window.sessionStorage.getItem("pinscopex:cloneAsNewProjectId")
        : null;
    if (cloneId) {
      window.sessionStorage.removeItem("pinscopex:cloneAsNewProjectId");
      fetchProject(cloneId)
        .then(setCloneAsNewProject)
        .catch(() => {
          // Source project unreachable — silently no-op; dialog stays closed.
        });
    }

    const topupSuccess = searchParams.get("topup") === "success";
    if (!topupSuccess) return;

    setShowCheckoutBanner(true);
    setCheckoutState("pending");

    const sessionId = searchParams.get("session_id");
    let cancelled = false;
    let poll: ReturnType<typeof setInterval> | null = null;

    const activate = (balance: number) => {
      if (cancelled) return;
      setActivatedDetail(`Balance is now ${balance.toFixed(2)} credits.`);
      setCheckoutState("activated");
    };

    (async () => {
      const initial = await refreshCredits();
      const initialBalance: number | null = initial?.balance ?? null;

      // Reconcile is idempotent — a confirmed paid top-up session means
      // the grant is already in the ledger, so we skip the balance-delta
      // poll (which would hang after a refresh since the credits are
      // already applied).
      if (sessionId) {
        try {
          const r = await reconcileCheckoutSession(sessionId);
          if (!cancelled && r.ok && r.kind === "topup" && r.payment_status === "paid") {
            const c = await refreshCredits();
            activate(c?.balance ?? initialBalance ?? 0);
            return;
          }
        } catch {
          /* fall through to polling */
        }
      }

      let attempts = 0;
      const MAX_ATTEMPTS = 8; // ~16s — after that we drop to a Refresh CTA
      poll = setInterval(async () => {
        if (cancelled) return;
        attempts++;
        const c = await refreshCredits();

        const balanceIncreased =
          c != null &&
          initialBalance != null &&
          c.balance > initialBalance + 0.001;

        if (balanceIncreased && c) {
          activate(c.balance);
          if (poll) clearInterval(poll);
        } else if (attempts >= MAX_ATTEMPTS) {
          setCheckoutState("timeout");
          if (poll) clearInterval(poll);
        }
      }, 2000);
    })();

    return () => {
      cancelled = true;
      if (poll) clearInterval(poll);
    };
  }, [searchParams, refreshCredits]);

  useEffect(() => {
    if (checkoutState !== "activated") return;
    const url = new URL(window.location.href);
    if (url.searchParams.has("topup") || url.searchParams.has("session_id")) {
      url.searchParams.delete("topup");
      url.searchParams.delete("session_id");
      router.replace(url.pathname + (url.search || ""));
    }
    const t = setTimeout(() => {
      setCheckoutState("dismissed");
      setShowCheckoutBanner(false);
    }, 4000);
    return () => clearTimeout(t);
  }, [checkoutState, router]);

  function dismissCheckout() {
    setCheckoutState("dismissed");
    setShowCheckoutBanner(false);
    const url = new URL(window.location.href);
    url.searchParams.delete("topup");
    url.searchParams.delete("session_id");
    router.replace(url.pathname + (url.search || ""));
  }

  return (
    <div className="flex-1 p-6 max-w-5xl mx-auto w-full">
      {/* <OnboardingSurvey /> */}
      {showCheckoutBanner && checkoutState !== "dismissed" && (
        <CheckoutSuccessBanner
          state={checkoutState}
          detail={activatedDetail}
          onDismiss={dismissCheckout}
        />
      )}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold">Projects</h1>
          <BalanceSubtitle credits={credits} />
        </div>
        <CreateProjectDialog
          rerunProject={rerunProject}
          onRerunDone={() => setRerunProject(null)}
          cloneAsNewProject={cloneAsNewProject}
          onCloneAsNewDone={() => setCloneAsNewProject(null)}
          onCreateProject={(p) => {
            setProjects((prev) => {
              const existing = prev.find((x) => x.id === p.id);
              if (existing) {
                return prev.map((x) => (x.id === p.id ? p : x));
              }
              return [p, ...prev];
            });
            router.push(`/project/${p.id}/progress`);
          }}
        />
      </div>

      {!loading && projects.length > 0 && (
        <div className="flex items-center justify-end gap-2 mb-4">
          <div className="inline-flex rounded-lg border border-input p-0.5">
            <button
              type="button"
              onClick={() => updateView("cards")}
              aria-pressed={view === "cards"}
              title="Card view"
              className={cn(
                "p-1 rounded-md transition-colors",
                view === "cards"
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <LayoutGrid className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => updateView("table")}
              aria-pressed={view === "table"}
              title="Table view"
              className={cn(
                "p-1 rounded-md transition-colors",
                view === "table"
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <List className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-36 rounded-lg" />
          ))}
        </div>
      ) : projects.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-12">
          No projects yet. Create one to get started.
        </p>
      ) : view === "table" ? (
        <ProjectsTable
          projects={sortedProjects}
          onDeleted={(id) => setProjects((prev) => prev.filter((x) => x.id !== id))}
          onRerun={setRerunProject}
        />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {sortedProjects.map((p) => (
            <ProjectCard
              key={p.id}
              project={p}
              onDeleted={() => setProjects((prev) => prev.filter((x) => x.id !== p.id))}
              onRerun={setRerunProject}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function BalanceSubtitle({ credits }: { credits: CreditSnapshot | null }) {
  if (!credits) {
    return (
      <p className="text-sm text-muted-foreground">
        Schematic validation projects
      </p>
    );
  }
  return (
    <p className="text-sm text-muted-foreground">
      {credits.balance.toFixed(2)} credits
      {" · "}
      <Link
        href="/billing"
        className="underline underline-offset-2 hover:text-foreground"
      >
        buy more
      </Link>
      {" · "}
      <Link
        href="/credits"
        className="underline underline-offset-2 hover:text-foreground"
      >
        ledger
      </Link>
    </p>
  );
}

function CheckoutSuccessBanner({
  state,
  detail,
  onDismiss,
}: {
  state: CheckoutState;
  detail: string | null;
  onDismiss: () => void;
}) {
  const tone =
    state === "timeout"
      ? "border-amber-500/30 bg-amber-500/5 text-amber-600 dark:text-amber-400"
      : "border-emerald-500/30 bg-emerald-500/5 text-emerald-600 dark:text-emerald-400";

  let icon;
  let heading;
  let body;
  let action;

  if (state === "pending") {
    icon = <Loader2 className="h-4 w-4 shrink-0 animate-spin" />;
    heading = "Payment received";
    body = "Applying your top-up to your balance…";
  } else if (state === "activated") {
    icon = <CheckCircle2 className="h-4 w-4 shrink-0" />;
    heading = "Top-up complete";
    body = detail;
    action = (
      <Link href="/credits">
        <Button size="sm" variant="outline" className="h-7 text-xs">
          View ledger
          <ArrowRight className="h-3 w-3 ml-1" />
        </Button>
      </Link>
    );
  } else {
    // timeout
    icon = <Loader2 className="h-4 w-4 shrink-0" />;
    heading = "Still processing";
    body =
      "Stripe is taking longer than usual. Refresh the page in a moment — your credits will appear automatically once the webhook completes.";
    action = (
      <Button
        size="sm"
        variant="outline"
        className="h-7 text-xs"
        onClick={() => window.location.reload()}
      >
        Refresh
      </Button>
    );
  }

  return (
    <div className={`rounded-lg border px-4 py-3 text-sm mb-4 flex items-start gap-3 ${tone}`}>
      {icon}
      <div className="flex-1 min-w-0">
        <div className="font-medium">{heading}</div>
        {body && (
          <div className="text-xs text-muted-foreground mt-0.5">{body}</div>
        )}
      </div>
      {action}
      <button
        type="button"
        onClick={onDismiss}
        className="text-muted-foreground hover:text-foreground transition-colors"
        aria-label="Dismiss"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
