import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConnectionFindings } from "./ConnectionFindings";
import type { components } from "@/api/schema";

type Findings = components["schemas"]["Findings"];

describe("ConnectionFindings", () => {
  it("renders nothing when both lists are empty", () => {
    const findings: Findings = {
      secret_files: [],
      unsupported_files: [],
      truncated: false,
    };
    const { container } = render(<ConnectionFindings findings={findings} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders secret files and unsupported files with path and reason, secret in alert", () => {
    const findings: Findings = {
      secret_files: [
        { path: ".env", reason: "matches a secret file name" },
        { path: "sub/id_rsa", reason: "matches a secret file name" },
      ],
      unsupported_files: [
        { path: "notes.docx", reason: "not a supported format" },
        { path: "symlink_dir", reason: "a link" },
      ],
      truncated: false,
    };

    render(<ConnectionFindings findings={findings} />);

    // Secret files in an alert
    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(
      screen.getByText(
        "The agents will be able to read these files; remove the connection to stop that.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(".env")).toBeInTheDocument();
    expect(screen.getByText("sub/id_rsa")).toBeInTheDocument();

    // Unsupported files in a plain list
    expect(screen.getByText("notes.docx")).toBeInTheDocument();
    expect(screen.getByText("not a supported format")).toBeInTheDocument();
    expect(screen.getByText("symlink_dir")).toBeInTheDocument();
    expect(screen.getByText("a link")).toBeInTheDocument();

    // Not truncated
    expect(screen.queryByText(/and more/i)).toBeNull();
  });

  it("renders 'and more' when truncated is true", () => {
    const findings: Findings = {
      secret_files: [{ path: "key.pem", reason: "matches a secret file name" }],
      unsupported_files: [],
      truncated: true,
    };

    render(<ConnectionFindings findings={findings} />);
    expect(screen.getByText(/and more/i)).toBeInTheDocument();
  });

  it("renders only secret files when unsupported files is empty", () => {
    const findings: Findings = {
      secret_files: [{ path: ".env", reason: "matches a secret file name" }],
      unsupported_files: [],
      truncated: false,
    };

    render(<ConnectionFindings findings={findings} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(".env")).toBeInTheDocument();
    expect(screen.queryByText("Unsupported files")).toBeNull();
  });

  it("renders only unsupported files when secret files is empty", () => {
    const findings: Findings = {
      secret_files: [],
      unsupported_files: [{ path: "archive.zip", reason: "not a supported format" }],
      truncated: false,
    };

    render(<ConnectionFindings findings={findings} />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("archive.zip")).toBeInTheDocument();
    expect(screen.getByText("not a supported format")).toBeInTheDocument();
  });
});
