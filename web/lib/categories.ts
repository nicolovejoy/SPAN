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

/** True if `v` names one of the configured categories (rules + the default).
 *  Guards the `drill` param on the API routes and in the URL state. */
export function isCategory(v: string): boolean {
  return (CATEGORIES as readonly string[]).includes(v);
}

export function categorize(name: string): string {
  for (const { category, re } of compiledRules) {
    if (re.test(name)) return category;
  }
  return cfg.default;
}

const fluxStr = (s: string) => `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;

// Flux regex literals are /.../ — patterns can't contain a literal slash. We
// don't currently use any, but assert just so a future rule update fails loud.
function fluxRegex(pattern: string): string {
  if (pattern.includes("/")) {
    throw new Error(`Pattern contains "/", needs escaping for Flux: ${pattern}`);
  }
  return `/(?i)${pattern}/`;
}

/**
 * Flux fragment that synthesizes a `category` column from `r.name` using the
 * same rules as the Python collector. Lets historical rows (written before
 * `category` was tagged at write time) be grouped correctly.
 */
export function categoryFromNameFlux(): string {
  const branches = compiledRules
    .map(({ category }, i) => {
      const pattern = cfg.rules[i].pattern;
      const head = i === 0 ? "if" : "else if";
      return `      ${head} r.name =~ ${fluxRegex(pattern)} then ${fluxStr(category)}`;
    })
    .join("\n");
  return `map(fn: (r) => ({ r with category:
${branches}
      else ${fluxStr(cfg.default)}
    }))`;
}

/**
 * Flux predicate matching circuits whose `name` falls into any of the given
 * categories. Used in the initial filter so we don't pull series we'll
 * discard. For the default ("Other") we negate the union of all rule patterns.
 */
export function nameMatchesCategoriesFlux(categories: string[]): string {
  const ruleCats = new Set(cfg.rules.map((r) => r.category));
  const patternsForCat = (cat: string): string[] => {
    if (cat === cfg.default) return [];
    return cfg.rules.filter((r) => r.category === cat).map((r) => r.pattern);
  };
  const wantsDefault = categories.includes(cfg.default);
  const wantsRule = categories.filter((c) => ruleCats.has(c));

  const ruleParts = wantsRule.flatMap(patternsForCat);
  const allRulePatterns = cfg.rules.map((r) => r.pattern);

  const clauses: string[] = [];
  if (ruleParts.length) {
    clauses.push(`r.name =~ ${fluxRegex(ruleParts.join("|"))}`);
  }
  if (wantsDefault) {
    clauses.push(`not (r.name =~ ${fluxRegex(allRulePatterns.join("|"))})`);
  }
  return clauses.length ? `(${clauses.join(" or ")})` : "true";
}
