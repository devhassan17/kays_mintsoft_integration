# Installation & Odoo.sh Deployment Guide

## 1. Prerequisites

- **Odoo Version**: Odoo 19.0 Enterprise
- **Python**: 3.12+ (with standard `requests` library installed)
- **PostgreSQL**: 14+
- **Required Odoo Standard Modules** (automatically installed as dependencies):
  - `base`, `web`, `mail`, `sale_management`, `stock`, `delivery`, `account`

---

## 2. Deploying on Odoo.sh

1. **Commit & Push to Staging Branch**:
   Place the `kays_mintsoft_integration` directory inside your Odoo.sh repository root (or configured addons path) and push to your **Staging** branch:
   ```bash
   git add kays_mintsoft_integration
   git commit -m "Add Kays Logistics (Mintsoft V1) integration module for Odoo 19 Enterprise"
   git push origin staging
   ```
2. **Update Apps List in Odoo 19**:
   - Log into Odoo 19 as an Administrator.
   - Enable **Developer Mode** (*Settings → Activate the developer mode*).
   - Navigate to **Apps → Update Apps List** and click **Update**.
3. **Install the Module**:
   - Search for `Kays Logistics Integration` or `kays_mintsoft_integration`.
   - Click **Activate / Install**.

---

## 3. Post-Installation Verification Checklist

After installing `kays_mintsoft_integration`:

1. **Verify Application Menu**:
   - Confirm **Kays Logistics** appears in the main Odoo home menu with its warehouse icon and all 8 top-level sections (*Dashboard, Orders, Products, Inventory, Shipments, Returns, Operations, Configuration*).
2. **Assign User Security Roles**:
   - Go to **Settings → Users & Companies → Users**.
   - Under the **Kays Logistics** privilege selector, assign:
     - **Integration Viewer**: Customer service or warehouse staff who need read-only visibility.
     - **Integration Manager**: Operations managers who export orders, resolve SKU/stock discrepancies, and retry jobs.
     - **Integration Administrator**: System administrators authorized to configure API keys, webhooks, and Production mode.
3. **Configure Moyee Coffee United Kingdom Connection in Dry Run Mode**:
   - Follow the [Configuration Guide](configuration.md) to create the connection profile in **Dry Run** mode first and run **Test Connection** before enabling live exports.
