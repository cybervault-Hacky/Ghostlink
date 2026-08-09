import type { ButtonHTMLAttributes, CSSProperties, ReactNode } from "react";

export function Button({
  variant,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "danger" | "ghost" }) {
  const cls = ["btn", variant === "primary" ? "btn-primary" : variant === "danger" ? "btn-danger" : variant === "ghost" ? "btn-ghost" : ""]
    .join(" ");
  return (
    <button className={cls} {...props}>
      {children}
    </button>
  );
}

export function Card({ children, className = "", style }: { children: ReactNode; className?: string; style?: CSSProperties }) {
  return (
    <div className={`card ${className}`} style={style}>
      {children}
    </div>
  );
}

export function GlassCard({ children, strong = false, className = "", style }: { children: ReactNode; strong?: boolean; className?: string; style?: CSSProperties }) {
  return (
    <div className={`${strong ? "glass-strong" : "glass"} ${className}`} style={style}>
      {children}
    </div>
  );
}

export function Badge({ tone = "active", children }: { tone?: "active" | "revoked" | "warn" | "accent"; children: ReactNode }) {
  const cls = `badge badge-${tone}`;
  return <span className={cls}>{children}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  if (status === "active") return <Badge tone="active">Active</Badge>;
  if (status === "revoked") return <Badge tone="revoked">Revoked</Badge>;
  if (status === "archived") return <Badge tone="revoked">Archived</Badge>;
  return <Badge tone="warn">{status}</Badge>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
    </div>
  );
}

export function EmptyState({ title, cta }: { title: string; cta?: ReactNode }) {
  return (
    <div className="empty">
      <p>{title}</p>
      {cta}
    </div>
  );
}
