// Uses vary. Covered by test/cache.test.js.
const vary = require("vary");

// Adds the request headers a cached response depends on to its Vary header.
function varyOn(existing, fields) {
  return vary.append(existing || "", fields);
}

module.exports = { varyOn };
