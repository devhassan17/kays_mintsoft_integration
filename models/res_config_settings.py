from odoo import api, fields, models, _


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Company-level settings
    kays_enabled = fields.Boolean(
        related='company_id.kays_enabled',
        readonly=False,
        string='Enable Kays Logistics for Company',
    )
    kays_reference_prefix = fields.Char(
        related='company_id.kays_reference_prefix',
        readonly=False,
        string='Company Order Reference Prefix',
    )
    kays_default_instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        related='company_id.kays_default_instance_id',
        readonly=False,
        string='Default Kays Connection',
    )

    # Instance-linked quick configuration fields
    kays_environment = fields.Selection(
        related='kays_default_instance_id.environment',
        readonly=False,
        string='Execution Environment',
    )
    kays_active_api_url = fields.Char(
        related='kays_default_instance_id.active_api_url',
        readonly=True,
        string='Active API Base URL',
    )
    kays_mintsoft_warehouse_id = fields.Integer(
        related='kays_default_instance_id.mintsoft_warehouse_id',
        readonly=False,
        string='Mintsoft Warehouse ID',
    )
    kays_mintsoft_client_id = fields.Integer(
        related='kays_default_instance_id.mintsoft_client_id',
        readonly=False,
        string='Mintsoft Client ID',
    )

    # Feature Switches on Default Connection
    kays_enable_integration = fields.Boolean(
        related='kays_default_instance_id.enable_integration',
        readonly=False,
        string='Enable Kays Integration',
    )
    kays_enable_auto_order_export = fields.Boolean(
        related='kays_default_instance_id.enable_auto_order_export',
        readonly=False,
        string='Automatic Order Export',
    )
    kays_enable_subscription_export = fields.Boolean(
        related='kays_default_instance_id.enable_subscription_export',
        readonly=False,
        string='Subscription Delivery Export',
    )
    kays_enable_manual_order_export = fields.Boolean(
        related='kays_default_instance_id.enable_manual_order_export',
        readonly=False,
        string='Manual Order Export',
    )
    kays_enable_product_import = fields.Boolean(
        related='kays_default_instance_id.enable_product_import',
        readonly=False,
        string='Product Import',
    )
    kays_enable_product_export = fields.Boolean(
        related='kays_default_instance_id.enable_product_export',
        readonly=False,
        string='Product Export',
    )
    kays_enable_inventory_sync = fields.Boolean(
        related='kays_default_instance_id.enable_inventory_sync',
        readonly=False,
        string='Inventory Sync',
    )
    kays_enable_order_status_sync = fields.Boolean(
        related='kays_default_instance_id.enable_order_status_sync',
        readonly=False,
        string='Order Status Sync',
    )
    kays_enable_tracking_sync = fields.Boolean(
        related='kays_default_instance_id.enable_tracking_sync',
        readonly=False,
        string='Tracking Sync',
    )
    kays_enable_shipment_sync = fields.Boolean(
        related='kays_default_instance_id.enable_shipment_sync',
        readonly=False,
        string='Shipment Sync',
    )
    kays_enable_cancellation_sync = fields.Boolean(
        related='kays_default_instance_id.enable_cancellation_sync',
        readonly=False,
        string='Cancellation Sync',
    )
    kays_enable_order_amendments = fields.Boolean(
        related='kays_default_instance_id.enable_order_amendments',
        readonly=False,
        string='Order Amendments',
    )
    kays_enable_returns_sync = fields.Boolean(
        related='kays_default_instance_id.enable_returns_sync',
        readonly=False,
        string='Returns Sync',
    )
    kays_enable_webhooks = fields.Boolean(
        related='kays_default_instance_id.enable_webhooks',
        readonly=False,
        string='Webhooks',
    )
    kays_enable_scheduled_polling = fields.Boolean(
        related='kays_default_instance_id.enable_scheduled_polling',
        readonly=False,
        string='Scheduled Polling',
    )
    kays_enable_auto_retry = fields.Boolean(
        related='kays_default_instance_id.enable_auto_retry',
        readonly=False,
        string='Automatic Retry',
    )
    kays_enable_customer_notification = fields.Boolean(
        related='kays_default_instance_id.enable_customer_notification',
        readonly=False,
        string='Customer Notification',
    )
    kays_enable_debug_logging = fields.Boolean(
        related='kays_default_instance_id.enable_debug_logging',
        readonly=False,
        string='API Debug Logging',
    )

    # Order & Inventory Policies
    kays_payment_eligibility_policy = fields.Selection(
        related='kays_default_instance_id.payment_eligibility_policy',
        readonly=False,
        string='Payment Eligibility Policy',
    )
    kays_unmapped_carrier_policy = fields.Selection(
        related='kays_default_instance_id.unmapped_carrier_policy',
        readonly=False,
        string='Unmapped Carrier Policy',
    )
    kays_inventory_authority = fields.Selection(
        related='kays_default_instance_id.inventory_authority',
        readonly=False,
        string='Inventory Authority',
    )
    kays_stock_sync_interval = fields.Selection(
        related='kays_default_instance_id.stock_sync_interval',
        readonly=False,
        string='Stock Sync Interval',
    )
    kays_max_stock_adjustment_threshold = fields.Float(
        related='kays_default_instance_id.max_stock_adjustment_threshold',
        readonly=False,
        string='Max Stock Adjustment Threshold',
    )
    kays_webhook_url = fields.Char(
        related='kays_default_instance_id.webhook_url',
        readonly=True,
        string='Webhook Callback URL',
    )
    kays_log_retention_days = fields.Integer(
        related='kays_default_instance_id.log_retention_days',
        readonly=False,
        string='Log Retention (Days)',
    )

    def action_open_kays_connections(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays Logistics Connections'),
            'res_model': 'kays.mintsoft.instance',
            'view_mode': 'list,form',
            'domain': [('company_id', '=', self.company_id.id)],
            'context': {'default_company_id': self.company_id.id},
        }
