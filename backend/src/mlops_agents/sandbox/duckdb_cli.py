"""Per-architecture release asset names and sha256 digests for DuckDB CLI."""

from __future__ import annotations

DUCKDB_CLI_SHA256_AMD64 = "c6507206baf7793c6013dbd82368469a45f20dc0e17a6f92f204602f652ed2bc"
DUCKDB_CLI_SHA256_ARM64 = "859e1875d2a9515fbb4bf8871061437a14af82320da68c753f094c4b776348fe"

DUCKDB_CLI_ASSETS: dict[str, str] = {
    "amd64": "duckdb-cli-linux-amd64.tar.gz",
    "arm64": "duckdb-cli-linux-arm64.tar.gz",
}

DUCKDB_CLI_SHA256: dict[str, str] = {
    "amd64": DUCKDB_CLI_SHA256_AMD64,
    "arm64": DUCKDB_CLI_SHA256_ARM64,
}
