"use client";

import { useState } from "react";
import Link from "next/link";
import { RotateCcw, Trash2, Users } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useOptionalUser } from "@/hooks/use-optional-auth";
import { deleteProject } from "@/lib/api";
import type { Project } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useReviewedCount } from "@/hooks/use-reviewed-count";

const STATUS_STYLES: Record<string, string> = {
  draft: "bg-muted text-muted-foreground",
  running: "bg-blue-500/15 text-blue-600 dark:text-blue-400 border-blue-500/30",
  complete: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
  error: "bg-rose-500/15 text-rose-600 dark:text-rose-400 border-rose-500/30",
  cancelled: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30",
};

export function ProjectCard({
  project,
  onDeleted,
  onRerun,
}: {
  project: Project;
  onDeleted?: () => void;
  onRerun?: (project: Project) => void;
}) {
  const { user } = useOptionalUser();
  const [deleting, setDeleting] = useState(false);
  const { summary } = project;
  const total = summary?.total ?? 0;
  const isShared = project.userId != null && user?.id != null && project.userId !== user.id;
  const checkedCount = useReviewedCount(project.id);
  const isCancelled = project.status === "cancelled";
  const isDraft = project.status === "draft";
  const opensModalOnClick = isDraft && !isShared && onRerun != null;

  async function handleDelete(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm(`Delete "${project.name}"?`)) return;
    setDeleting(true);
    try {
      await deleteProject(project.id);
      onDeleted?.();
    } catch {
      setDeleting(false);
    }
  }

  function handleRerun(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    onRerun?.(project);
  }

  const card = (
    <Card
      className={cn(
        "h-full transition-colors",
        isCancelled
          ? "opacity-70"
          : "hover:border-foreground/20 cursor-pointer",
      )}
    >
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-semibold">{project.name}</CardTitle>
            <div className="flex items-center gap-1.5">
              {isShared && (
                <Badge variant="outline" className="text-[10px] px-1.5 py-0 text-blue-600 dark:text-blue-400 border-blue-500/40">
                  <Users className="h-3 w-3 mr-0.5" />
                  Shared
                </Badge>
              )}
              <Badge variant="outline" className={cn("text-xs capitalize", STATUS_STYLES[project.status])}>
                {project.status}
              </Badge>
              {isCancelled && !isShared && onRerun && (
                <button
                  onClick={handleRerun}
                  title="Rerun project"
                  className="p-1 rounded-md text-muted-foreground hover:text-emerald-600 dark:hover:text-emerald-400 hover:bg-emerald-500/10 transition-colors"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                </button>
              )}
              {!isShared && (
                <button
                  onClick={handleDelete}
                  disabled={deleting}
                  className="p-1 rounded-md text-muted-foreground hover:text-rose-600 dark:hover:text-rose-400 hover:bg-rose-500/10 transition-colors disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            {new Date(project.created).toLocaleDateString()}
          </p>
        </CardHeader>
        <CardContent>
          {summary && total > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-3 text-xs font-mono tabular-nums">
                <span className="text-rose-600 dark:text-rose-400">{summary.ERROR ?? 0} err</span>
                <span className="text-amber-600 dark:text-amber-400">{summary.WARNING ?? 0} warn</span>
                <span className="text-blue-600 dark:text-blue-400">{summary.INFO ?? 0} info</span>
                {checkedCount > 0 && (
                  <span className="text-emerald-600 dark:text-emerald-400">{checkedCount} checked</span>
                )}
              </div>
              <div className="flex h-1.5 rounded-full overflow-hidden bg-muted">
                {(summary.ERROR ?? 0) > 0 && (
                  <div className="h-full bg-rose-500" style={{ width: `${((summary.ERROR ?? 0) / total) * 100}%` }} />
                )}
                {(summary.WARNING ?? 0) > 0 && (
                  <div className="h-full bg-amber-500" style={{ width: `${((summary.WARNING ?? 0) / total) * 100}%` }} />
                )}
                {(summary.INFO ?? 0) > 0 && (
                  <div className="h-full bg-blue-500" style={{ width: `${((summary.INFO ?? 0) / total) * 100}%` }} />
                )}
              </div>
            </div>
          )}
          {(!summary || total === 0) && (
            <p className="text-xs text-muted-foreground">No report yet</p>
          )}
        </CardContent>
      </Card>
  );

  if (isCancelled) return card;

  if (opensModalOnClick) {
    return (
      <div
        role="button"
        tabIndex={0}
        onClick={() => onRerun?.(project)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRerun?.(project); } }}
        className="text-left w-full cursor-pointer"
      >
        {card}
      </div>
    );
  }

  // Open the report by default — that's what users want to see for a finished
  // project. In-flight or paused runs go to the progress page where the SSE
  // stepper and resume controls live.
  const inFlight =
    project.status === "running"
    || project.status === "paused_insufficient_credits"
    || project.status === "paused_by_user";
  const href = inFlight
    ? `/project/${project.id}/progress`
    : `/project/${project.id}/report`;
  return <Link href={href}>{card}</Link>;
}
