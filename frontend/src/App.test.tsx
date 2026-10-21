import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { APP_NAME } from "./config";

test("renders the app with APP_NAME and welcome message", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: APP_NAME })).toBeInTheDocument();
  expect(screen.getByText("No pages yet.")).toBeInTheDocument();
});
