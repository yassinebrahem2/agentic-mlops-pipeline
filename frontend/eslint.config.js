import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "dist",
      "src/api/schema.d.ts",
      "src/components/ui/**",
      "playwright-report",
      "test-results",
    ],
  },
  {
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true, allowExportNames: ["route"] },
      ],
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    files: ["src/pages/agents/MessageView.tsx"],
    rules: {
      // ReactMarkdown's AST node must be removed before spreading props onto the DOM.
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_node$" }],
    },
  },
  {
    files: ["src/**/testing.tsx"],
    rules: {
      "react-refresh/only-export-components": "off",
    },
  },
  prettier,
);
