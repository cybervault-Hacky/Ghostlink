import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QrCodeView } from "./QrCodeView";
import { ConnectDeviceModal } from "./ConnectDeviceModal";

describe("QrCodeView", () => {
  it("renders SVG QR code correctly", () => {
    render(<QrCodeView value="gl://dev-pair?code=GL-TEST-1234" size={150} />);
    const svg = screen.getByRole("img", { name: "QR Code" });
    expect(svg).toBeDefined();
    expect(svg.getAttribute("width")).toBe("150");
  });
});

describe("ConnectDeviceModal", () => {
  it("renders nothing when isOpen is false", () => {
    const { container } = render(
      <ConnectDeviceModal isOpen={false} onClose={vi.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders modal dialog when isOpen is true", () => {
    render(<ConnectDeviceModal isOpen={true} onClose={vi.fn()} />);
    expect(screen.getByText("Connect this Termux Device")).toBeDefined();
  });
});
