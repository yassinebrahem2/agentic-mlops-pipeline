import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelCards } from "./ModelCards";

describe("ModelCards", () => {
  it("renders model card with formatted metrics (3 significant digits, integers as integers)", () => {
    render(
      <ModelCards
        models={[
          {
            name: "churn",
            production_version: "2",
            latest_metrics: {
              auc: 0.8538461538461538,
              samples: 1234,
            },
            open_incidents: 1,
          },
        ]}
      />,
    );

    const card = screen.getByTestId("model-churn");
    expect(within(card).getByText("Production version 2")).toBeInTheDocument();
    expect(within(card).getByText("auc")).toBeInTheDocument();
    expect(within(card).getByText("0.854")).toBeInTheDocument();
    expect(within(card).getByText("samples")).toBeInTheDocument();
    expect(within(card).getByText("1,234")).toBeInTheDocument();
    expect(within(card).getByText("1 open incident")).toBeInTheDocument();
  });
});
