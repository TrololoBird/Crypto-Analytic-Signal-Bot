## 2025-05-15 - Dashboard CORS and Security Headers
**Vulnerability:** Overly permissive CORS policy (wildcard) and missing security headers (X-Frame-Options, X-Content-Type-Options) in the FastAPI dashboard.
**Learning:** The dashboard was initially built for local use but used a wildcard `allow_origins=["*"]` for convenience, which could allow malicious websites to interact with the local API if a user is running the bot.
**Prevention:** Use restricted, configurable CORS origins by default (local only) and always apply standard security middlewares to FastAPI/web components to provide defense-in-depth.
