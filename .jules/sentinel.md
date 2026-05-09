## 2025-05-15 - [Security Hardening: Internal Servers Binding]
**Vulnerability:** Internal HTTP servers (Dashboard and Prometheus) defaulted to binding on `0.0.0.0`, exposing unauthenticated endpoints to the entire network.
**Learning:** Defaulting to `0.0.0.0` for internal monitoring tools increases the attack surface unnecessarily. Insecure CORS configuration (`allow_origins=["*"]` with `allow_credentials=True`) was also present.
**Prevention:** Always default internal/admin servers to `127.0.0.1` and provide configuration options to change it if needed. Use strict CORS policies.
