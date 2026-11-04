# Test Plan & Verification Scenarios

## 1. Automated Unit & Integration Test Suite (`tests/`)

The module includes 30 automated test scenarios across 7 test modules using mocked Mintsoft HTTP responses (`unittest.mock.patch('requests.Session.request')`) so that **zero live external API calls** are made during test execution.

### Running Tests on Odoo.sh or Odoo CLI

```bash
odoo-bin -d <test_database> -i kays_mintsoft_integration --test-enable --stop-after-init --test-tags=kays_mintsoft
```

### Coverage Matrix (30 Required Scenarios)

| # | Test File | Test Method | Verified Behavior |
|---|---|---|---|
| 1 | `test_api_client.py` | `test_01_api_authentication_header_and_sanitization` | `ms-apikey` header injection & redaction in logs |
| 2 | `test_api_client.py` | `test_02_request_formatting_and_url_building` | URL normalization, JSON serialization, audit log creation |
| 3 | `test_api_client.py` | `test_03_401_403_handling_and_5_probe_diagnostics` | `/api/Client` HTTP 401 treated as non-fatal when Warehouse/Product/Order/Stock return HTTP 200 |
| 4 | `test_api_client.py` | `test_04_429_rate_limit_handling` | HTTP 429 `Retry-After` header parsing and retry classification |
| 5 | `test_api_client.py` | `test_05_timeout_handling_read_vs_mutating_write` | Read timeout (`retryable=True`) vs Write timeout (`unknown_outcome=True`) |
| 6 | `test_api_client.py` | `test_06_connection_error_handling` | Network `ConnectionError` handling and logging |
| 7 | `test_api_client.py` | `test_07_json_parse_error_handling_and_ssrf_protection` | Malformed JSON handling, SSRF/HTTPS validation, mutating-GET detection |
| 8 | `test_order_export.py` | `test_08_order_submission_success` | `PUT /api/Order` submission and `OrderId` / `OrderItemId` linking |
| 9 | `test_order_export.py` | `test_09_order_validation_failure_missing_address` | 14-point pre-submission validation catches missing address fields |
| 10 | `test_order_export.py` | `test_10_duplicate_order_prevention_remote_and_local` | Pre-export `/api/Order/Search` links existing order without duplicate `PUT` |
| 11 | `test_order_export.py` | `test_11_unknown_outcome_reconciliation` | Write timeout transitions to `unknown_outcome` and reconciles via `/api/Order/Search` |
| 12 | `test_order_export.py` | `test_12_split_order_responses` | Multiple `NewOrderResult` objects create linked split child orders |
| 13 | `test_order_export.py` | `test_13_missing_sku_failure` | Unmapped SKU blocks export and records missing SKU entry |
| 14 | `test_multi_company.py` | `test_14_wrong_company_blocked` | Cross-company order/connection mismatch blocked |
| 15 | `test_multi_company.py` | `test_15_disabled_company_never_exports` | Disabled company (`Moyee Coffee Ireland`) never exports on confirm |
| 16 | `test_order_export.py` | `test_16_disabled_connection_never_exports` | Disabled connection blocks export |
| 17 | `test_order_export.py` | `test_17_disabled_auto_export_allows_manual_export` | `enable_auto_order_export=False` stops auto-export while allowing manual export |
| 18 | `test_order_export.py` | `test_18_draft_order_blocked` | Draft quotations blocked from export |
| 19 | `test_order_export.py` | `test_19_unpaid_order_blocked_when_payment_required` | Payment eligibility policies (`invoice_paid`, `payment_flag`) enforced |
| 20 | `test_stock_sync.py` | `test_20_stock_sync_uom_factor_and_inventory_adjustment` | `GET /api/Product/StockLevels`, UoM conversion, and `stock.quant` update |
| 21 | `test_stock_sync.py` | `test_21_stock_discrepancy_threshold_blocks_unsafe_adjustment` | Discrepancy exceeding threshold blocks auto-adjustment and queues manager review |
| 22 | `test_webhooks.py` | `test_22_tracking_and_shipment_updates` | Shipment and tracking event sync updates `kays.mintsoft.shipment` & `stock.picking` |
| 23 | `test_webhooks.py` | `test_23_partial_shipment_handling` | Partial shipment updates line quantities without premature full dispatch |
| 24 | `test_webhooks.py` | `test_24_cancellation_safety_blocks_dispatched_and_cancels_new` | Live `GET /api/Order/{id}` check before calling `GET /api/Order/{id}/Cancel` |
| 25 | `test_webhooks.py` | `test_25_webhook_deduplication_and_out_of_order_protection` | SHA-256 deduplication and out-of-order status rank protection |
| 26 | `test_webhooks.py` | `test_26_webhook_token_authentication_verification` | Constant-time HMAC verification of `X-Kays-Webhook-Token` |
| 27 | `test_retries.py` | `test_27_retry_logic_exponential_backoff_and_dead_letter` | Exponential backoff, `Retry-After` override, and `dead_letter` transition |
| 28 | `test_multi_company.py` | `test_28_multi_company_isolation_and_ambiguous_routing_detection` | Multi-company isolation & ambiguous multi-connection routing detection |
| 29 | `test_security.py` | `test_29_unauthorized_settings_and_credential_access_blocked` | Viewer and Manager blocked from credential changes and Production Mode Wizard |
| 30 | `test_security.py` | `test_30_module_installation_integrity_and_dashboard_telemetry` | All 17 models, 10 job schedules, status mappings, and dashboard KPIs verified |
