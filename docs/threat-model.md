# Threat Model & Security Controls

## Assets & Trust Boundaries

1. **Raw Satellite & Weather Inputs:** Untrusted external feeds that may contain corrupted GeoTIFF headers, tampered checksums, malformed Avro payloads, or physically impossible sensor values.
2. **Apache Iceberg V2 Warehouse & STAC Catalog:** Authoritative historical state powering scientific zonal metrics and map tiles.
3. **FastAPI Spatial & Tile API (`:8070`):** Network-facing serving boundary handling read queries, map tile rendering, and privileged backfill mutations.
4. **Credentials & Audit Logs:** Bearer/API-key tokens and security audit trails (`security_audit.jsonl`).

## STRIDE Threat Analysis & Implemented Mitigations

| Threat | Vector | Mitigation Implemented | Residual / Production Gap |
| --- | --- | --- | --- |
| **Spoofing** | Anonymous or forged caller accessing `/v1/*` | Mandatory `X-API-Key` or `Authorization: Bearer` validation on all `/v1/*` endpoints; returns `401` and logs audit event | Local dev uses static env tokens; production requires OIDC/JWT + short-lived IAM credentials |
| **Elevation of Privilege** | Read-only `viewer` triggering `POST /v1/backfill` | Hierarchical RBAC (`viewer < analyst < admin`) rejects non-admin mutations with `403 Forbidden` | No per-region row-level policy engine yet |
| **Tampering (Data)** | Corrupted GeoTIFF bytes or silent bit-rot | SHA-256 digest verification + GDAL/rasterio structural check before Bronze commit; failures routed to `bronze.quarantine_dlq` | No cryptographic GPG/Sigstore signature verification on STAC manifests yet |
| **Information Disclosure** | Tokens or internal paths leaked in logs or URLs | `redact_sensitive()` strips bearer tokens, PATs, and passwords in JSON logs and audit logs; dashboard passes API key in headers (not query strings) | TLS termination assumed at ingress/reverse proxy outside local `127.0.0.1` |
| **Denial of Service** | Tile zoom bomb (`z=30`) or huge query limits | Tile zoom bounded to `0..6`; API limits capped (`limit <= 500`); Kubernetes manifests enforce CPU/memory limits & NetworkPolicy | Rate limiting should be enforced at API gateway / Envoy in production |
| **Supply Chain Risk** | Unpinned `:l-a-t-e-s-t` containers or copyleft weights | `scripts/static_audit.py` enforces pinned container tags; `scripts/check_licenses.py` & `scripts/generate_sbom.py` generate CycloneDX 1.5 SBOM; zero external ML weights downloaded | Container vulnerability scanning (Trivy/Grype) runs in CI once images are built |

## Explicit Dev-vs-Production Boundaries

- Default local tokens in `.env.example` are strictly for local loopback (`127.0.0.1`) demonstration.
- Kubernetes deployment manifests (`infra/kubernetes/deployment-api.yaml`) read tokens exclusively from `secretKeyRef`, enforce `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, and drop `ALL` Linux capabilities.
