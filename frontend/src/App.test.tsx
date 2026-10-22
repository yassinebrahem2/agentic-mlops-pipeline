import { screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { APP_NAME } from "./config";
import { renderPage } from "./test/render";

test("renders the app with APP_NAME and the Projects page at root", async () => {
  renderPage("/");
  expect(screen.getByRole("heading", { name: APP_NAME })).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument();
});
