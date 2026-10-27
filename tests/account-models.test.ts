import assert from "node:assert/strict";
import test from "node:test";
import { groupAccountModels, type AccountModel } from "../src/lib/account-models.ts";

const model = (id: string, name = id, remaining_fraction: number | null = 0.6,
  reset_at: number | null = 100): AccountModel => ({ id, name, remaining_fraction, reset_at });

test("reasoning variants consume one base model balance, never summed", () => {
  const rows = ["high", "low", "medium"].map((effort) =>
    model(`gemini-3.8-flash-${effort}`, `Gemini 3.8 Flash (${effort})`));
  const [group] = groupAccountModels(rows);
  assert.equal(groupAccountModels(rows).length, 1);
  assert.equal(group.name, "Gemini 3.8 Flash");
  assert.equal(group.remaining_fraction, 0.6);
  assert.equal(group.reset_at, 100);
  assert.deepEqual(group.variants, ["low", "medium", "high"]);
});

test("raw tiered entries resolve to the named family regardless of input order", () => {
  const rows = [model("gemini-3.8-flash-tiered"),
    model("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)")];
  assert.deepEqual(groupAccountModels(rows), groupAccountModels([...rows].reverse()));
  assert.equal(groupAccountModels(rows).length, 1);
});

test("legacy IDs use upstream names and current effort labels", () => {
  const groups = groupAccountModels([
    model("gemini-3-flash-agent", "Gemini 3.5 Flash (High)"),
    model("gemini-3.5-flash-extra-low", "Gemini 3.5 Flash (Low)"),
    model("gemini-3.5-flash-low", "Gemini 3.5 Flash (Medium)"),
    model("gemini-2.5-flash", "Gemini 3.5 Flash Lite"),
    model("gemini-2.5-flash-thinking", "Gemini 3.5 Flash Lite"),
    model("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite"),
  ]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].variants, ["low", "medium", "high"]);
  assert.equal(groups[1].name, "Gemini 3.5 Flash Lite");
});

test("different models with identical balances remain separate", () => {
  assert.equal(groupAccountModels([
    model("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    model("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    model("gemini-3.1-flash-image", "Gemini 3.1 Flash Image"),
    model("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite"),
  ]).length, 4);
});

test("raw IDs group without friendly names and keep unknown quota unknown", () => {
  const groups = groupAccountModels([
    model("future-model-low", undefined, null, null),
    model("future-model-xhigh", undefined, null, null),
    model("future-model-max", undefined, null, null),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].name, "future-model");
  assert.equal(groups[0].remaining_fraction, null);
  assert.equal(groups[0].reset_at, null);
  assert.deepEqual(groupAccountModels([]), []);
});

test("inconsistent snapshots use the lowest known balance with its reset, without mutating input", () => {
  const rows = [model("model-low", undefined, null, 999),
    model("model-medium", undefined, 0.8, 800), model("model-high", undefined, 0, null)];
  const before = structuredClone(rows);
  const [group] = groupAccountModels(rows);
  assert.equal(group.remaining_fraction, 0);
  assert.equal(group.reset_at, null);
  assert.deepEqual(rows, before);
});
