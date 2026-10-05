# SQL Server enterprise validation gate

Reusable promotion gate for SQL Server, T-SQL, Azure SQL, drivers and tooling.

## Evidence header
Record change/KB, environment, SQL build before/after, compatibility level, driver versions, Query Store baseline, workload ID, run/correlation ID, rollback point, and final PASS/FAIL/BLOCKED.

## Matrix
| Gate | Validation | Acceptance |
|---|---|---|
| Build/servicing | Inventory product version and CU/GDR branch. Re-read the target Microsoft KB immediately before rollout. | Exact build and servicing branch match the approved target. |
| CU known issues | Exercise workload-specific known issues. For 2025 CU9/2022 CU27 include SESSION_CONTEXT with parallel execution/session reuse and applicable linked-server/recovery-monitoring paths. | Correct results; no crash/dump; integrations behave as baseline. |
| JSON correctness | Test ISJSON, OPENJSON WITH explicit schema, malformed/missing/null/type cases, idempotent ingestion and quarantine. | Exact expected rows/types; invalid input controlled; replay creates no duplicate effect. |
| Native JSON | A/B nvarchar(max)+OPENJSON vs json+OPENJSON on identical representative data. | CPU, duration, logical reads, log volume, storage, throughput and P50/P95/P99 recorded; no material regression. |
| JSON index | Compare CREATE JSON INDEX with current indexing, including write cost and maintenance blocking. | Benefit and operational cost measured; maintenance fits SLO. |
| Compatibility 170 | Upgrade engine while retaining prior compatibility first; baseline Query Store; test CL170 separately. | Critical queries have no unexplained regression in plans, CPU, reads, duration or P95/P99. |
| Parameter sensitivity | At CL170 test NULL, selective, non-selective and skewed parameter values and relevant DML. | Correct results and stable/improved representative latency. |
| Concurrency | Test hot rows, long transactions, retry/deadlock behavior and version-store pressure; include ADR/RCSI dependencies when optimized locking is used. | Throughput, LCK waits, deadlocks and functional invariants remain within agreed limits. |
| Security | Test TLS/certificates, authentication, least privilege and compatibility-related crypto behavior. | Authorized paths succeed and prohibited operations fail. |
| Drivers | Inventory direct/transitive clients; test supported target driver. | E2E covers auth, TLS, pooling/session reset, timeout/retry, transactions, bulk copy where used, and JSON serialization/metadata. |
| Integrations | Exercise used Agent, linked server, Database Mail, replication, log shipping, Always On, ETL/BI/Power Platform paths. | Destination effect is independently read back; retry/failover tested where relevant. |
| Azure SQL | Benchmark representative workload after SKU/hardware/update-policy/platform change. | CPU, I/O latency, tempdb, workers, throughput, P95/P99 and cost recorded with no SLO regression. |
| Recovery/HA | Restore to an independent target and test HA failover where applicable. | Restored data independently queried; RPO/RTO measured; application reconnect verified. |
| Tooling | Validate SSMS/agent/MCP tooling under least-privilege identity and controlled integrations. | Required operations work without expanding the SQL principal's authority. |
| Full E2E | Execute a representative business transaction through SQL and downstream readback twice. | Same business result on both runs, no duplicate side effect, telemetry tied to tested build/configuration. |
| Rollback | Rehearse the supported rollback/recovery path before production where applicable. | Baseline behavior independently revalidated inside the agreed window. |

## Promotion sequence
1. Capture inventory and workload/Query Store baseline.
2. Apply one change dimension only.
3. Run correctness and security controls.
4. Run performance/regression benchmark.
5. Run integrations and recovery/HA checks.
6. Run full E2E and independently read back the effect.
7. Record PASS/FAIL/BLOCKED.
8. Promote only PASS.

Installation success, a successful command, or green CI alone does not prove production readiness.

## Performance rules
Use the same representative data, query mix, concurrency and measurement window before/after. Keep warm-up outside measured runs. Averages alone are insufficient; include tail latency. Improvements in mean latency do not compensate for material regressions in correctness, blocking, recovery or security.

## Current baseline — 2026-10-05
Microsoft currently lists SQL Server 2025 CU9 (17.0.5005.3) and SQL Server 2022 CU27 (16.0.4295.3) as latest CUs. Their KB pages document known issues including SESSION_CONTEXT with parallel plans, so the KB must be re-read before rollout.

SQL Server 2025 documents native json and enhancements including CREATE JSON INDEX and JSON_CONTAINS as generally available. Adoption still requires workload benchmark and E2E.

Compatibility level 170 changes optimizer behavior and key-material encryption behavior. Treat engine upgrade and compatibility-level elevation as separate changes.

## Authoritative references
- Microsoft Support KB321185 — latest SQL Server updates/version history
- Microsoft Support KB5122048 — SQL Server 2025 CU9
- Microsoft Support KB5104824 — SQL Server 2022 CU27
- Microsoft Learn — JSON data in SQL Server; JSON data type
- Microsoft Learn — ALTER DATABASE compatibility level
- Microsoft Learn — Parameter Sensitive Plan optimization
- Microsoft Learn — Optimized locking
- Microsoft Learn — What's new in SQL Server 2025
