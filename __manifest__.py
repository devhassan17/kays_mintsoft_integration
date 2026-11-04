{
    'name': 'Kays Logistics Integration',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Delivery',
    'summary': 'Production 3PL Fulfilment Integration between Odoo 19 Enterprise and Kays Logistics (Mintsoft REST API V1)',
    'description': """
Kays Logistics (Mintsoft) Integration for Odoo 19 Enterprise
============================================================
Complete warehouse fulfilment management system integrating Odoo 19 Enterprise
with Kays Logistics via the Mintsoft REST API V1.

Key Capabilities:
-----------------
* Multi-company & multi-instance connection management with strict company isolation
* Three-tier execution environments: Dry Run, Test Client, and Production mode
* Configurable HTTPS API endpoints with host allowlisting & TLS verification
* Secure `ms-apikey` credential management restricted to Integration Administrators
* 16 independent backend-enforced feature switches
* Outbound order validation, idempotency, split-order support & duplicate protection
* Dedicated Kays Orders, Shipments, Tracking, Stock Snapshots, Discrepancies & Returns UI
* Full & incremental stock synchronization with UoM support and threshold safeguards
* Courier service & regional destination mapping (UK, NI, IE, International)
* Real-time HTTPS webhook controller with deduplication & asynchronous job queueing
* Durable scheduled jobs with exponential backoff, HTTP 429 Retry-After & unknown-outcome reconciliation
* Interactive OWL Dashboard with live KPIs, charts, health checks & quick actions
    """,
    'author': 'Managemyweb.co',
    'maintainer': 'Ali Hassan',
    'website': 'https://managemyweb.co',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'web',
        'mail',
        'sale_management',
        'stock',
        'delivery',
        'payment',
    ],
    'data': [
        # Security
        'security/security.xml',
        'security/ir.model.access.csv',
        # Reference Data & Cron Jobs
        'data/status_mappings.xml',
        'data/ir_cron.xml',
        # Wizards
        'wizard/test_order_wizard_views.xml',
        'wizard/manual_sync_wizard_views.xml',
        'wizard/retry_wizard_views.xml',
        # Views
        'views/dashboard_views.xml',
        'views/connection_views.xml',
        'views/settings_views.xml',
        'views/order_views.xml',
        'views/product_mapping_views.xml',
        'views/stock_views.xml',
        'views/shipping_views.xml',
        'views/tracking_views.xml',
        'views/return_views.xml',
        'views/log_views.xml',
        'views/sale_order_views.xml',
        'views/stock_picking_views.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'kays_mintsoft_integration/static/src/scss/dashboard.scss',
            'kays_mintsoft_integration/static/src/xml/dashboard.xml',
            'kays_mintsoft_integration/static/src/js/dashboard.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
