import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["cdk.out"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
);
