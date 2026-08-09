import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Loading, ErrorState } from "./ui";

describe("Loading", () => {
  it("announces loading via role=status", () => {
    render(<Loading label="Loading devices…" />);
    const status = screen.getByRole("status");
    expect(status.textContent).toContain("Loading devices…");
  });
});

describe("ErrorState", () => {
  it("is an alert with a retry action", () => {
    const onRetry = vi.fn();
    render(<ErrorState title="Could not load" message="boom" onRetry={onRetry} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Could not load");
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("renders without a retry button when none is provided", () => {
    render(<ErrorState message="quiet failure" />);
    expect(screen.getByRole("alert").textContent).toContain("Something went wrong");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
