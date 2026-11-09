# Gate testbed

A tiny Node app with deliberately old dependencies. Dependabot opens a PR for each one, and the self-test workflow runs the gate from this repository on it. Each dependency is chosen to produce a different verdict:

| Dependency | Update | Used in | Tests | Expected verdict | Why |
|---|---|---|---|---|---|
| `chalk` | 4.1.2 → 6.x | [src/status.js](src/status.js) | yes | ❌ fail | chalk 5+ is ESM-only, so `require("chalk")` no longer returns the chalk API and the tests fail. |
| `escape-html` | 1.0.2 → 1.0.3 | [src/render.js](src/render.js) | yes | ✅ pass | A small patch release, and the escaping is covered by a test. |
| `ms` | 2.0.0 → 2.1.x | [src/timeouts.js](src/timeouts.js) | **no** | 👀 needs-human | It's used in code with no tests, so nothing proves the update is safe. |
| `vary` | 1.1.1 → 1.1.2 | [src/cache.js](src/cache.js) | yes | ✅ pass, auto-merged | A patch release that only speeds up header parsing, covered by a test. The self-test allows auto-merge for patch updates, so the gate merges it by itself. |

The point is to check the gate's judgement, so don't merge the other PRs. Close them, or comment `@dependabot ignore this major version` once you've seen the result. Once a PR is closed, Dependabot won't open it again for the same version, so to re-run a case, pin a different dependency one release behind its latest.

```bash
cd testbed && npm ci && npm test
```
