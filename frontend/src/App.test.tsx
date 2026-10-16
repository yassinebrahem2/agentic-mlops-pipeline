import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { APP_NAME } from "./config";

test("shows the app name", () => {
  render(<App />);
  expect(screen.getByRole("heading", { name: APP_NAME })).toBeInTheDocument();
});
