const test = require("node:test");
const assert = require("node:assert");
const { varyOn } = require("../src/cache");

test("varyOn adds fields without duplicating existing ones", () => {
  assert.strictEqual(varyOn("", "Accept"), "Accept");
  assert.strictEqual(varyOn("Accept", ["accept", "Origin"]), "Accept, Origin");
  assert.strictEqual(varyOn("Accept, Origin", "Accept-Encoding"), "Accept, Origin, Accept-Encoding");
});

test("varyOn treats * as matching everything", () => {
  assert.strictEqual(varyOn("*", "Accept"), "*");
  assert.strictEqual(varyOn("Accept", "*"), "*");
});

test("varyOn rejects invalid header names", () => {
  assert.throws(() => varyOn("", "bad header"), TypeError);
});
