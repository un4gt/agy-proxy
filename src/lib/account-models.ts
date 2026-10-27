export type AccountModel = {
  id: string;
  name: string;
  remaining_fraction: number | null;
  reset_at: number | null;
};

type AccountModelGroup = AccountModel & { variants: string[] };
const variantOrder = ["extra-low", "low", "medium", "high", "xhigh", "max", "tiered"];

function variant(model: AccountModel) {
  // Friendly names take priority: some legacy IDs use an older effort name.
  return (model.name.match(/\((extra[- ]low|low|medium|high|xhigh|max)\)$/i)?.[1]
    ?? model.id.match(/-(extra-low|low|medium|high|xhigh|max|tiered)$/i)?.[1])
    ?.toLowerCase().replace(" ", "-");
}

// Account catalogs include reasoning variants and legacy IDs. These are choices
// for calling a model, not separate quota balances.
function baseId(id: string) {
  return id.replace(/-(?:extra-low|low|medium|high|xhigh|max|tiered)$/i, "");
}

function baseName(model: AccountModel) {
  const name = model.name.trim();
  if (!name || name.toLowerCase() === model.id.toLowerCase()) return null;
  return name.replace(/\s*\((?:extra[- ]low|low|medium|high|xhigh|max)\)$/i, "").trim();
}

export function groupAccountModels(models: readonly AccountModel[]): AccountModelGroup[] {
  // Resolve raw IDs (including tiered) through the named entries in the same
  // family. Upstream display names also identify aliases with older model IDs.
  const names = new Map<string, string>();
  for (const model of models) {
    const name = baseName(model);
    if (name) names.set(baseId(model.id).toLowerCase(), name);
  }

  const groups = new Map<string, AccountModelGroup>();
  for (const model of models) {
    const family = baseId(model.id).toLowerCase();
    const name = names.get(family) ?? family;
    const key = name.toLowerCase().replace(/\s+/g, " ");
    const effort = variant(model);
    const group = groups.get(key);
    if (!group) {
      groups.set(key, { ...model, id: key, name, variants: effort ? [effort] : [] });
      continue;
    }
    if (effort && !group.variants.includes(effort)) group.variants.push(effort);

    // Never add shared balances. If upstream snapshots disagree, use the
    // lowest known balance and its reset time; missing data stays unknown.
    if (model.remaining_fraction !== null &&
        (group.remaining_fraction === null || model.remaining_fraction < group.remaining_fraction)) {
      group.remaining_fraction = model.remaining_fraction;
      group.reset_at = model.reset_at;
    } else if (model.remaining_fraction === group.remaining_fraction && model.reset_at !== null) {
      group.reset_at = Math.max(group.reset_at ?? model.reset_at, model.reset_at);
    }
  }
  return [...groups.values()].map((group) => ({
    ...group,
    variants: group.variants.sort((a, b) => variantOrder.indexOf(a) - variantOrder.indexOf(b)),
  }));
}
