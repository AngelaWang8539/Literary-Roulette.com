// Concise badge tests. Run: node tests/js/badge_tests.mjs
import assert from "node:assert/strict";
import { badgeText } from "../../static/js/badge.js";

const cases = [
  [{ tier: "jackpot", label: "x", top_percent: 1 }, "Top 1%"],
  [{ tier: "lucky", label: "x", top_percent: 10 }, "Top 10%"],
  [{ tier: "jackpot", label: "x", rank: 4 }, "Top 10 this month"],
  [{ tier: "lucky", label: "x", rank: 40 }, "Top 100 this month"],
  [{ tier: "hidden-gem", label: "x", top_percent: 95, rank: 900 }, "Last 10%"],
  [{ tier: "hidden-gem", label: "x" }, "Off the list"],
  [{ tier: "offlist", label: "x" }, "Off the list"],
  [{ tier: "discovery", label: "x", top_percent: 50 }, "Discovery"],
  [{ tier: "something-new", label: "x" }, "Discovery"],
];
for (const [data, short] of cases) assert.equal(badgeText(data).short, short, JSON.stringify(data));
assert.equal(badgeText(null), null);
assert.equal(badgeText({ tier: "jackpot" }), null);        // no label from the server: no badge, never an invented one
assert.match(badgeText({ tier: "jackpot", label: "x", top_percent: 1 }).long, /Top 1% most popular/);
console.log("  ok   badge tests (" + (cases.length + 3) + " checks)");
