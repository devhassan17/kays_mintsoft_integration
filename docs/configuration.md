# Configuration & Multi-Company Setup Guide

## 1. Enabling Companies for Kays Logistics

By default, all Odoo companies have Kays Logistics **disabled** (`kays_enabled = False`) to prevent accidental cross-company exports.

1. Navigate to **Kays Logistics → Configuration → Companies** (or **Settings → Kays Logistics**).
2. Enable **Enable Kays Logistics Integration** only for the intended company, e.g.:
   - **Moyee Coffee United Kingdom** → `Enabled`
   - **Moyee Coffee Ireland** → `Disabled`
   - **Moyee Coffee Netherlands** → `Disabled`
3. Orders belonging to any disabled company will show `Export Status = Not Applicable` and will never be exported automatically.

---

## 2. Creating a Mintsoft Connection Profile

1. Navigate to **Kays Logistics → Configuration → Connections** and click **New**.
2. Fill in the **General** settings:
   - **Connection Name**: `Moyee UK — Kays Main Warehouse`
   - **Odoo Company**: `Moyee Coffee United Kingdom`
   - **Execution Mode**: Start in `Dry Run Mode (Validate & Log Only — No Live Writes)`
   - **API Base URL**: `https://api.mintsoft.co.uk/api`
   - **API Version**: `V1`
   - **Mintsoft Warehouse ID**: `3`
   - **Mintsoft Warehouse Name**: `Main Warehouse`
   - **Default Odoo Warehouse**: Select the UK Odoo warehouse
   - **Default Odoo Stock Location**: Select `WH/Stock` for the UK warehouse
3. Fill in the **Authentication** tab (requires **Integration Administrator** role):
   - **Mintsoft API Key (`ms-apikey`)**: Paste the API key provided by Kays Logistics.
   - Click **Save**, then click **Test Connection** in the header.
4. Inspect the **Connection Diagnostics** output:
   - `Warehouse API: HTTP 200 (1 warehouse(s), ID: 3 - Main Warehouse)`
   - `Products API: HTTP 200 (34 record(s))`
   - `Orders API: HTTP 200 (100 record(s))`
   - `Stock API: HTTP 200 (34 stock level record(s))`
   - `Client API: HTTP 401 — permission or authentication issue for this endpoint (non-fatal; Warehouse/Product/Order/Stock endpoints are operational)`

---

## 3. Synchronizing Warehouses, Couriers, and Products

1. Click **Fetch Warehouses** on the connection form to populate **Kays Logistics → Configuration → Warehouses** (`kays.mintsoft.warehouse.mapping`). Map Mintsoft Warehouse `3 (Main Warehouse)` to your Odoo UK Warehouse.
2. Click **Fetch Courier Services** to import Mintsoft courier services into **Kays Logistics → Shipments → Courier Mappings** (`kays.mintsoft.carrier.mapping`). Map each Odoo `delivery.carrier` to the corresponding Mintsoft `CourierServiceId` / `CourierServiceName` and set a **Default Courier Service** on the connection.
3. Click **Fetch Products** to pull the Mintsoft product catalogue (`GET /api/Product/List`) and automatically match SKUs against Odoo `product.product.default_code`.
4. Check **Kays Logistics → Products → Missing SKUs** to resolve any unmapped or duplicate SKUs before enabling order export.

---

## 4. Configuring Webhooks

1. Open the **Webhooks & ConnectAction** tab on the Connection form.
2. Copy the auto-generated **Odoo Webhook Endpoint URL**:
   `https://<your-odoo-domain>/kays/mintsoft/webhook/<connection_identifier>`
3. Copy the **Webhook Secret Token** (`X-Kays-Webhook-Token`).
4. In Mintsoft (or via automatic `ConnectAction` registration on order creation), configure despatch/status callbacks to POST to the endpoint with header `X-Kays-Webhook-Token` (or query parameter `?token=<secret>`).

---

## 5. Promoting from Dry Run to Production Mode

1. Test an order end-to-end using **Kays Logistics → Orders → Create Test Order** in **Dry Run** mode to inspect the generated JSON payload and validation report.
2. When ready to go live, an **Integration Administrator** clicks **Enable Production Mode** in the Connection header, completes all 3 safety checkboxes, and types `ENABLE PRODUCTION`.
