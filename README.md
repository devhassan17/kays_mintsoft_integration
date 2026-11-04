# Kays Logistics Integration (`kays_mintsoft_integration`)

**Odoo 19 Enterprise × Kays Logistics (Mintsoft REST API V1) — Complete Production Warehouse Fulfilment Integration**

- **Technical Name**: `kays_mintsoft_integration`
- **Display Name**: Kays Logistics Integration
- **Odoo Version**: 19.0 Enterprise (`Odoo.sh` & On-Premise compatible)
- **Python Version**: 3.12+
- **Author**: Managemyweb.co
- **Development Owner**: Ali Hassan
- **Initial Customer**: Moyee Coffee United Kingdom (Main Warehouse — Mintsoft Warehouse ID `3`)
- **License**: LGPL-3

---

## Overview

`kays_mintsoft_integration` is a complete, multi-company warehouse fulfilment management application inside Odoo 19 Enterprise that connects Odoo Sales, Inventory, and Delivery workflows with **Kays Logistics** via the **Mintsoft REST API V1** (`https://api.mintsoft.co.uk/api`).

### Key Capabilities

1. **Dedicated Kays Logistics Application & OWL Dashboard**:
   - Real-time KPI cards (Total Orders, Pending Export, Exported Today, Processing, Awaiting Dispatch, Dispatched Today, Delivered, Cancelled, Failed Orders, Awaiting Retry, Last Stock Sync, Stock Differences, API Connection Status).
   - Orders by status distribution chart, daily order export volume chart, API latency chart, recent failed requests, latest shipments, stock discrepancies, and scheduled sync telemetry.
   - Multi-company, connection, environment, warehouse, carrier, and date-range filters.
2. **Multi-Company & Multi-Connection Isolation**:
   - Explicitly enable or disable Kays Logistics per Odoo company (e.g., *Moyee Coffee United Kingdom* enabled; *Moyee Coffee Ireland* and *Moyee Coffee Netherlands* disabled).
   - Orders belonging to a disabled company are **never** exported automatically.
   - Ambiguous multi-connection configurations are automatically detected and prevented.
3. **Three Safe Execution Modes**:
   - **Dry Run Mode** (Default): Validates orders, builds JSON payloads, and executes read-only diagnostics (`GET /api/Warehouse`, `/api/Product/List`, `/api/Order/List`, `/api/Product/StockLevels`) while blocking all mutating write calls (including mutating Mintsoft `GET` endpoints such as `/api/Order/{id}/Cancel`).
   - **Test Client Mode**: Submits orders with `TEST-` prefixes and `TEST_ORDER` tags to a designated Mintsoft sandbox/test client.
   - **Production Mode**: Protected by an explicit Administrator confirmation wizard (`kays.production.mode.wizard`).
4. **16 Backend-Enforced Feature Switches**:
   - Master integration switch plus granular backend-enforced switches for Automatic Order Export, Manual Order Export, Product Import, Product Export, Inventory Sync, Order Status Sync, Tracking Sync, Shipment Sync, Cancellation Sync, Order Amendments, Returns Sync, Webhooks, Scheduled Polling, Automatic Retry, Customer Notification, and API Debug Logging.
5. **Idempotent Order Fulfilment & Split Order Support**:
   - 14-point pre-submission order validation.
   - Deterministic `ExternalOrderReference` and `idempotency_key` with PostgreSQL `SELECT ... FOR UPDATE NOWAIT` concurrency locking.
   - Pre-export duplicate checks (`GET /api/Order/Search` and `GET /api/Order/GetOrderId`) and unknown-outcome timeout reconciliation.
   - Full handling of `PUT /api/Order` split-order responses (`Array<NewOrderResult>`).
6. **Inventory Synchronization & Threshold Safeguards**:
   - Synchronizes warehouse stock via `GET /api/Product/StockLevels` with UoM conversion factors and reservation-aware available stock math.
   - Configurable unit and percentage discrepancy thresholds (`stock_threshold_units`, `stock_threshold_percent`) block unsafe bulk inventory adjustments and queue them for manager review.
7. **Shipments, Tracking, Returns, Cancellations & Amendments**:
   - Synchronizes `GET /api/Order/{id}/Shipments` and `GET /api/Order/Shipments/TrackingEvents/List` onto `kays.mintsoft.shipment` and `stock.picking`.
   - Safe cancellation workflow (`GET /api/Order/{id}` status verification followed by `GET /api/Order/{id}/Cancel`).
   - Returns & RMA workflow (`POST /api/Return/CreateReturn/{OrderId}`, `POST /api/Return/{id}/AddItem`, `GET /api/Return/List`).
8. **Authenticated HTTPS Webhook Controller**:
   - Endpoint: `POST /kays/mintsoft/webhook/<connection_identifier>`
   - Constant-time HMAC token verification (`X-Kays-Webhook-Token`), SHA-256 deduplication, IP rate limiting, and out-of-order status protection.

---

## Documentation Index

- [Installation & Deployment Guide](docs/installation.md)
- [Configuration & Multi-Company Setup Guide](docs/configuration.md)
- [System Architecture & State Machines](docs/architecture.md)
- [Mintsoft V1 API Mapping Reference](docs/api_mapping.md)
- [Test Plan & Verification Scenarios](docs/test_plan.md)
- [Troubleshooting & Operations Runbook](docs/troubleshooting.md)
