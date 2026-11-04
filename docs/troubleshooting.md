# Troubleshooting & Operations Runbook

## 1. Common Operational Scenarios

### Scenario A: `GET /api/Client` Returns `HTTP 401` During Connection Test
- **Cause**: Warehouse-scoped or client-scoped Mintsoft API keys frequently restrict access to `/api/Client` and `/api/Client/SubClients` while granting full access to `/api/Warehouse`, `/api/Product/List`, `/api/Order/List`, and `/api/Product/StockLevels`.
- **Resolution**: This is **expected and non-fatal**. As long as the Warehouse, Product, Order, and Stock probes return `HTTP 200`, the connection status is `Connected`. Do not replace a working API key solely due to a `401` on `/api/Client`.

---

### Scenario B: Order Export Status Is `Unknown Outcome` (`unknown_outcome`)
- **Cause**: A network timeout or HTTP 502/503/504 gateway error occurred after `PUT /api/Order` was transmitted. Because Mintsoft may have created the order before the connection dropped, blind retry is automatically blocked to prevent duplicate physical shipments.
- **Resolution**:
  1. Wait for the **Existing Order Reconciliation** scheduled job (runs every 10 minutes), OR
  2. Open the Kays Order in **Kays Logistics → Orders → Failed** and click **Reconcile Unknown Outcome**.
  3. Odoo will query `GET /api/Order/Search` and `GET /api/Order/GetOrderId`. If the order exists in Mintsoft, Odoo links the `Mintsoft Order ID` and marks it `Sent`; if confirmed absent, Odoo safely re-enables export.

---

### Scenario C: Order Blocked Due to Unmapped SKU
- **Cause**: One or more storable lines on the Sales Order do not have a mapped SKU in **Kays Logistics → Products → Product Mappings**, and the connection's **Unmapped SKU Policy** is set to `Block Order Export`.
- **Resolution**:
  1. Open **Kays Logistics → Products → Missing SKUs**.
  2. Click **Auto-Match by Odoo SKU** or manually set the **Mintsoft SKU** and **Mintsoft Product ID**, then click **Mark Mapped**.
  3. Return to the Sales Order or **Kays Logistics → Orders → Failed** and click **Retry Export**.

---

### Scenario D: Stock Sync Blocked by Discrepancy Threshold
- **Cause**: The difference between Mintsoft available stock and Odoo available stock exceeded the connection's **Max Auto-Adjust Discrepancy (Units)** or **Max Auto-Adjust Discrepancy (%)**.
- **Resolution**:
  1. Navigate to **Kays Logistics → Inventory → Stock Differences**.
  2. Review the open discrepancy record showing `Odoo Available`, `Mintsoft Available`, and `Difference`.
  3. Click **Approve & Apply Adjustment** to update Odoo inventory, or **Mark Resolved (Ignore)** if the variance is expected.

---

### Scenario E: Order Cancellation Refused
- **Cause**: Mintsoft has already moved the order to `Packed`, `Despatched`, or `Delivered`.
- **Resolution**: Once physical dispatch has occurred at Kays Logistics, the order cannot be cancelled via API. Instead, create a return request under **Kays Logistics → Returns → Return Requests** (`kays.mintsoft.return`) and click **Submit Return to Mintsoft**.
