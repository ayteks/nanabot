# SoCandyShop / Vuth's Homelab — Port Map
> Last updated: 2026-04-28
> Server: `socandyshopfr` (Tailscale) / local WSL

## Theme Groups

| Port | Service | Purpose | Status |
|------|---------|---------|--------|
| | **🔧 LLM / AI** | | |
| 11434 | Ollama | Local LLM inference (deepseek, ministral, glm, nomic-embed) | ✅ Active |
| 18789 | OpenClaw Gateway | OpenClaw agent gateway (Telegram bot) | ✅ Active |
| - | Hermes Gateway | Hermes Agent gateway (Telegram bot) | ✅ Active (no fixed port) |
| | **🛒 E-Commerce (SoCandyShop)** | | |
| **3100** | **TikTok Backend** | **FastAPI + TikTok-Api** — live status, user data, videos, trending | 🚧 Deploying |
| - | Shopify Store | Hosted by Shopify (external) | ✅ Active |
| | **🌐 Web / Infra** | | |
| 53 | systemd-resolved | DNS (local) | ✅ Active |
| - | Tailscale | Mesh VPN (socandyshopfr, socandyshop.local) | ✅ Active |
| - | WSLg | WSL GUI integration | ✅ Active |

## Port Allocation (Reserved Range)

| Range | Purpose | Assigned |
|-------|---------|----------|
| **3000-3099** | E-Commerce / Shopify tools | 💡 Free |
| **3100-3199** | E-Commerce backends | **3100** ← TikTok Backend |
| **8000-8099** | Dev servers / testing | 💡 Free |
| **11434** | Ollama (fixed) | Inference |
| **18000-18999** | Gateway services | **18789** ← OpenClaw Gateway |

## Design Principles

1. **Group ports by theme** — e-commerce in 3xxx, gateways in 18xxx, LLM in 11xxx
2. **Always configure via env var** — never hardcode in code (use `PORT=3100` or `os.getenv("PORT", "3100")`)
3. **Document every new port** — add to this file at deploy time
4. **Don't reuse** — if a service is retired, mark it `🔴 Retired` not removed (to prevent accidental reuse)
5. **Run on localhost** (127.0.0.1) unless it needs to be reachable from other machines

## Future Ports (Planned)

| Port | Service | When |
|------|---------|------|
| 3000 | Shopify QA agent (agent-browser test runner) | TBD |
| 8080 | browser-use autonomous agent UI | TBD |
| 11435 | Ollama test/alt instance | If needed |

---

> **Rule:** Before deploying anything new, check this file for conflicts. Update it after assigning.
