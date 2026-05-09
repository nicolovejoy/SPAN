import categoriesConfig from "../categories.generated.json";

type Rule = { category: string; pattern: string };
type Config = { version: number; default: string; rules: Rule[] };

const cfg = categoriesConfig as Config;

const compiledRules: Array<{ category: string; re: RegExp }> = cfg.rules.map(
  (r) => ({ category: r.category, re: new RegExp(r.pattern, "i") })
);

export const CATEGORIES = [
  ...cfg.rules.map((r) => r.category),
  cfg.default,
] as const;

export type Category = (typeof CATEGORIES)[number];

export function categorize(name: string): string {
  for (const { category, re } of compiledRules) {
    if (re.test(name)) return category;
  }
  return cfg.default;
}
