import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { APP_NAME } from "./config";
import { queryClient } from "./lib/query";

test("renders the app with APP_NAME and welcome message", () => {
  queryClient.setQueryData(["auth"], { mode: "local", signed_in: true });
  render(<App />);
  expect(screen.getByRole("heading", { name: APP_NAME })).toBeInTheDocument();
  expect(screen.getByText("No pages yet.")).toBeInTheDocument();
});
