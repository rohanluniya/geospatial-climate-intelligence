# ADR-005: Content-Addressed PNG Map Tile Caching and Header-Based RBAC

## Status
Accepted

## Context
Rendering colormapped PNG map tiles directly from GeoTIFF rasters on every HTTP request burns CPU on GDAL reads and PNG encoding, while naive URL-only caching serves stale tiles after radiometric recalibration (`v1 -> v2`).

## Decision
1. **Cache Key & Invalidation:** Key every cached 256×256 RGBA PNG tile by `(region_id, layer, z, x, y, stac_snapshot, scene_version)` across both in-memory and disk tiers, emit a strong SHA-256 `ETag`, and explicitly invalidate affected regions during `POST /v1/backfill`.
2. **Provenance Headers:** Attach `X-STAC-Snapshot`, `X-Valid-Pixel-Pct`, `X-CRS`, `X-Cache-Status`, `X-Scene-Version`, and `X-Render-Ms` to every PNG response.
3. **Header-Based RBAC:** Require `X-API-Key` or `Authorization: Bearer` on all `/v1/*` routes (`viewer` for reads/tiles, `admin` for `/v1/backfill`). The Control Tower dashboard fetches tile PNGs via `fetch()` with `X-API-Key` headers + `URL.createObjectURL()` rather than putting secrets in URL query parameters.

## Tradeoffs & Consequences
- **Benefits:** Eliminates credential leakage in server access logs, guarantees cache coherence on recalibration backfills, and delivers sub-millisecond warm tile latency.
- **Costs:** Requires CORS/header support for browser clients consuming tiles via JavaScript `fetch()`.
