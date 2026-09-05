# Screenshots

Captured from a locally-running console (`node resource-server/server.js` plus the
facilitator on :8402) with headless Chrome, at 1400px wide.

| File | What it shows |
| --- | --- |
| `dashboard-accrued-vs-collected.png` | The Phase 1 correction, with real traffic: **₹18.00 accrued, ₹0.00 collected, ₹18.00 outstanding** after nine requests and one created Payment Link. The link exists; the money has not arrived. The old dashboard showed a single tile labelled "earned" over the ₹18.00. |
| `security-profile-dark.png` | The Phase 2/3 disclosure card, dark theme — every security control this deployment runs open, each paired with what the production-like default is. |
| `security-profile-light.png` | The same card in the light theme. |

The profile card's first screenshot had an **invisible heading**: the card pins a light
amber background in both themes, but `.card__head h3` inherits `var(--text)`, which goes
light in dark mode. Caught by looking at the image rather than by any test. The fix pins
the foreground wherever the background is pinned, and the comment in `styles.css` says so.
