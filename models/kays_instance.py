import logging
import secrets
from datetime import timedelta
from urllib.parse import urljoin

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..services.mintsoft_client import (
    MintsoftClient,
    MintsoftAPIError,
    MintsoftSecurityError,
    validate_and_normalize_api_url,
)

_logger = logging.getLogger(__name__)


class KaysMintsoftInstance(models.Model):
    _name = 'kays.mintsoft.instance'
    _description = 'Kays Logistics (Mintsoft) Connection Instance'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, company_id, name'
    _check_company_auto = True

    _unique_name_company = models.Constraint(
        'UNIQUE(name, company_id)',
        'Connection name must be unique per Odoo Company!',
    )
    _unique_webhook_identifier = models.Constraint(
        'UNIQUE(webhook_identifier)',
        'Webhook identifier must be globally unique!',
    )

    # -------------------------------------------------------------------------
    # General Configuration
    # -------------------------------------------------------------------------
    name = fields.Char(
        string='Connection Name',
        required=True,
        tracking=True,
        help='Descriptive name for this Kays Logistics / Mintsoft connection profile.',
    )
    sequence = fields.Integer(string='Priority / Sequence', default=10)
    active = fields.Boolean(string='Active', default=True, tracking=True)
    is_default = fields.Boolean(
        string='Default Connection for Company',
        default=True,
        tracking=True,
        help='Used by default when a company has a single or primary Mintsoft connection.',
    )
    company_id = fields.Many2one(
        'res.company',
        string='Odoo Company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
        tracking=True,
    )
    environment = fields.Selection(
        selection=[
            ('dry_run', 'Dry Run (Read-Only / Payload Validation)'),
            ('test_client', 'Test Client (Approved Kays Test Account)'),
            ('production', 'Production (Live Fulfilment)'),
        ],
        string='Environment Mode',
        required=True,
        default='dry_run',
        tracking=True,
    )
    environment_name = fields.Char(
        string='Environment Label',
        compute='_compute_active_api_url',
        store=True,
    )
    production_api_url = fields.Char(
        string='Production API Base URL',
        default='https://api.mintsoft.co.uk/api',
        required=True,
        tracking=True,
    )
    test_api_url = fields.Char(
        string='Test API Base URL',
        default='https://api.mintsoft.co.uk/api',
        required=True,
        tracking=True,
        help='Mintsoft uses the live API endpoint with a separate test client unless Kays provides a dedicated URL.',
    )
    custom_api_url = fields.Char(
        string='Custom API Base URL',
        tracking=True,
        help='Optional custom HTTPS endpoint. Hostname must be listed in Approved API Hosts.',
    )
    allowed_hosts = fields.Char(
        string='Approved API Hosts (Allowlist)',
        default='api.mintsoft.co.uk',
        required=True,
        groups='kays_mintsoft_integration.group_kays_admin',
        help='Comma-separated list of approved HTTPS hostnames allowed to receive Mintsoft API requests.',
    )
    active_api_url = fields.Char(
        string='Active API Base URL',
        compute='_compute_active_api_url',
        store=True,
    )
    api_version = fields.Selection(
        selection=[('V1', 'REST API V1')],
        string='API Version',
        default='V1',
        required=True,
    )
    mintsoft_warehouse_id = fields.Integer(
        string='Mintsoft Warehouse ID',
        default=3,
        required=True,
        tracking=True,
        help='Primary Mintsoft Warehouse ID (e.g., 3 for Main Warehouse).',
    )
    mintsoft_warehouse_name = fields.Char(
        string='Mintsoft Warehouse Name',
        default='Main Warehouse',
        tracking=True,
    )
    mintsoft_client_id = fields.Integer(
        string='Mintsoft Client ID',
        default=0,
        tracking=True,
        help='Mintsoft Client ID (0 if determined automatically by client-scoped API key).',
    )
    mintsoft_client_name = fields.Char(string='Mintsoft Client Name')
    default_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Default Odoo Warehouse',
        check_company=True,
        tracking=True,
    )
    default_location_id = fields.Many2one(
        'stock.location',
        string='Default Odoo Stock Location',
        domain="[('usage', '=', 'internal')]",
        check_company=True,
        tracking=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Default Currency',
        default=lambda self: self.env.company.currency_id,
    )
    routing_domain = fields.Char(
        string='Order Routing Domain',
        default='[]',
        help='Optional Odoo domain filter when multiple connections exist for one company.',
    )
    notes = fields.Text(string='Connection Notes')

    # Test & Production Safety Controls
    test_order_prefix = fields.Char(
        string='Test Order Prefix',
        default='TEST-',
        help='Prefix prepended to order references when running in Test Client mode.',
    )
    test_write_acknowledged = fields.Boolean(
        string='Acknowledge Test Client Writes Operate on Live Mintsoft Infrastructure',
        default=False,
        tracking=True,
    )
    production_approved = fields.Boolean(
        string='Production Mode Approved',
        default=False,
        copy=False,
        tracking=True,
        groups='kays_mintsoft_integration.group_kays_admin',
    )
    production_approved_by = fields.Many2one(
        'res.users',
        string='Production Approved By',
        readonly=True,
        copy=False,
    )
    production_approved_date = fields.Datetime(
        string='Production Approved On',
        readonly=True,
        copy=False,
    )

    # -------------------------------------------------------------------------
    # Authentication & Connection Health
    # -------------------------------------------------------------------------
    # Never track api_key in mail.thread chatter! Restricted strictly to Integration Administrators.
    api_key = fields.Char(
        string='Mintsoft API Key (ms-apikey)',
        groups='kays_mintsoft_integration.group_kays_admin',
        copy=False,
        tracking=False,
    )
    api_key_masked = fields.Char(
        string='API Key (Masked)',
        compute='_compute_api_key_masked',
    )
    auth_type = fields.Selection(
        selection=[('ms_apikey', 'HTTP Header (ms-apikey)')],
        string='Authentication Type',
        default='ms_apikey',
        required=True,
    )
    api_key_expiry_date = fields.Date(
        string='API Key Expiration Date',
        tracking=True,
        help='Optional known expiration date for credential rotation warnings.',
    )
    api_key_expiry_warning = fields.Boolean(
        string='API Key Expiring Soon / Expired',
        compute='_compute_api_key_expiry_warning',
    )
    last_auth_check = fields.Datetime(string='Last Authentication Check', readonly=True, copy=False)
    last_successful_request = fields.Datetime(string='Last Successful Request', readonly=True, copy=False)
    last_failed_request = fields.Datetime(string='Last Failed Request', readonly=True, copy=False)
    avg_response_time_ms = fields.Float(string='Avg Response Time (ms)', readonly=True, copy=False)
    connection_status = fields.Selection(
        selection=[
            ('untested', 'Untested'),
            ('connected', 'Connected'),
            ('partial', 'Partial (Client Endpoint Restricted)'),
            ('failed', 'Failed'),
            ('disabled', 'Disabled'),
        ],
        string='Connection Status',
        default='untested',
        tracking=True,
        copy=False,
    )
    health_summary = fields.Text(string='Diagnostic Summary', readonly=True, copy=False)

    # -------------------------------------------------------------------------
    # Feature Enable / Disable Controls (Part B - Section 7)
    # -------------------------------------------------------------------------
    enable_integration = fields.Boolean(
        string='Enable Kays Integration',
        default=True,
        tracking=True,
        help='Master switch for this connection.',
    )
    enable_auto_order_export = fields.Boolean(
        string='Automatic Order Export',
        default=True,
        tracking=True,
    )
    enable_subscription_export = fields.Boolean(
        string='Subscription Delivery Export',
        default=True,
        tracking=True,
        help='Automatically export recurring subscription deliveries to Kays upon renewal payment.',
    )
    enable_manual_order_export = fields.Boolean(
        string='Manual Order Export',
        default=True,
        tracking=True,
    )
    enable_product_import = fields.Boolean(
        string='Product Import & SKU Sync',
        default=True,
        tracking=True,
    )
    enable_product_export = fields.Boolean(
        string='Product Export to Mintsoft',
        default=False,
        tracking=True,
        help='Disabled by default to prevent accidental product creation in Mintsoft.',
    )
    enable_inventory_sync = fields.Boolean(
        string='Inventory / Stock Sync',
        default=True,
        tracking=True,
    )
    enable_order_status_sync = fields.Boolean(
        string='Order Status Sync',
        default=True,
        tracking=True,
    )
    enable_tracking_sync = fields.Boolean(
        string='Tracking Sync',
        default=True,
        tracking=True,
    )
    enable_shipment_sync = fields.Boolean(
        string='Shipment Sync',
        default=True,
        tracking=True,
    )
    enable_cancellation_sync = fields.Boolean(
        string='Cancellation Sync',
        default=True,
        tracking=True,
    )
    enable_order_amendments = fields.Boolean(
        string='Order Amendments',
        default=True,
        tracking=True,
    )
    enable_returns_sync = fields.Boolean(
        string='Returns Sync',
        default=True,
        tracking=True,
    )
    enable_webhooks = fields.Boolean(
        string='Webhooks Processing',
        default=True,
        tracking=True,
    )
    enable_scheduled_polling = fields.Boolean(
        string='Scheduled Polling',
        default=True,
        tracking=True,
    )
    enable_auto_retry = fields.Boolean(
        string='Automatic Job Retry',
        default=True,
        tracking=True,
    )
    enable_customer_notification = fields.Boolean(
        string='Customer Dispatch Notification',
        default=False,
        tracking=True,
        help='Send Odoo delivery email on dispatch validation. Disabled by default to avoid duplicate courier/Mintsoft emails.',
    )
    enable_debug_logging = fields.Boolean(
        string='API Debug Logging',
        default=False,
        tracking=True,
    )

    # -------------------------------------------------------------------------
    # Order & Shipping Rules (Part D & Part I)
    # -------------------------------------------------------------------------
    payment_eligibility_policy = fields.Selection(
        selection=[
            ('confirmed', 'Confirmed Order'),
            ('confirmed_paid', 'Confirmed and Paid'),
            ('confirmed_authorized', 'Confirmed and Authorized Payment'),
            ('manual_approval', 'Manual Approval Required'),
            ('custom_providers', 'Custom Allowed Payment Providers'),
        ],
        string='Order Payment Eligibility Policy',
        default='confirmed',
        required=True,
        tracking=True,
    )
    allowed_payment_provider_ids = fields.Many2many(
        'payment.provider',
        'kays_instance_payment_provider_rel',
        'instance_id',
        'provider_id',
        string='Allowed Payment Providers',
    )
    reference_strategy = fields.Selection(
        selection=[
            ('order_name', 'Odoo Order Number (e.g. S10216)'),
            ('company_prefixed', 'Company-Prefixed Reference (e.g. MCUK-S10216)'),
            ('custom_prefix', 'Custom Prefix + Order Number'),
        ],
        string='External Order Reference Strategy',
        default='order_name',
        required=True,
        tracking=True,
    )
    reference_prefix = fields.Char(string='Custom Order Reference Prefix')
    unmapped_carrier_policy = fields.Selection(
        selection=[
            ('block', 'Block Export & Flag Error'),
            ('warn_fallback', 'Warn & Use Default Mintsoft Courier Service'),
            ('allow_auto', 'Allow AUTO Courier Selection (Explicit Opt-In)'),
        ],
        string='Unmapped Carrier Policy',
        default='block',
        required=True,
        tracking=True,
    )
    default_mintsoft_courier_service_id = fields.Integer(
        string='Default Mintsoft Courier Service ID',
        default=0,
    )
    default_mintsoft_courier_service_name = fields.Char(
        string='Default Mintsoft Courier Service Name',
    )
    mintsoft_channel_id = fields.Integer(string='Mintsoft Channel ID', default=0)
    mintsoft_channel_name = fields.Char(string='Mintsoft Channel Name')
    bundle_mapping_policy = fields.Selection(
        selection=[
            ('mintsoft_bundle_sku', 'Send Bundle Parent SKU to Mintsoft'),
            ('explode_kit_lines', 'Send Component Lines from Odoo Kit'),
            ('block_unmapped', 'Block Unmapped Bundles'),
        ],
        string='Bundle / Kit Mapping Policy',
        default='explode_kit_lines',
        required=True,
    )

    # -------------------------------------------------------------------------
    # Inventory & Stock Safeguard Settings (Part H)
    # -------------------------------------------------------------------------
    inventory_authority = fields.Selection(
        selection=[
            ('mintsoft', 'Mintsoft is Authoritative Stock Source'),
            ('monitoring_only', 'Monitoring Only (No Odoo Stock Adjustments)'),
            ('disabled', 'Disabled'),
        ],
        string='Inventory Authority',
        default='mintsoft',
        required=True,
        tracking=True,
    )
    stock_sync_interval = fields.Selection(
        selection=[
            ('5', 'Every 5 Minutes'),
            ('10', 'Every 10 Minutes'),
            ('15', 'Every 15 Minutes'),
            ('30', 'Every 30 Minutes'),
            ('60', 'Every 60 Minutes'),
            ('custom', 'Custom Interval (Minutes)'),
        ],
        string='Stock Sync Frequency',
        default='15',
        required=True,
    )
    stock_sync_custom_minutes = fields.Integer(string='Custom Sync Interval (Minutes)', default=15)
    max_stock_adjustment_threshold = fields.Float(
        string='Max Automatic Stock Adjustment Threshold',
        default=500.0,
        help='Adjustments exceeding this unit delta require manual approval.',
    )
    allow_zero_stock_updates = fields.Boolean(
        string='Allow Zero-Stock Updates',
        default=True,
    )
    skip_mapping_error_products = fields.Boolean(
        string='Skip Products with Mapping Errors',
        default=True,
    )
    notify_large_discrepancies = fields.Boolean(
        string='Notify on Large Stock Discrepancies',
        default=True,
    )
    large_discrepancy_threshold = fields.Float(
        string='Large Discrepancy Notification Threshold',
        default=50.0,
    )
    dry_run_stock_adjustments = fields.Boolean(
        string='Dry-Run Stock Adjustments Only',
        default=False,
    )
    require_manual_approval_unusual = fields.Boolean(
        string='Require Manual Approval for Threshold Breaches',
        default=True,
    )
    sync_selected_products_only = fields.Boolean(
        string='Sync Only Explicitly Selected Products',
        default=False,
    )
    excluded_product_category_ids = fields.Many2many(
        'product.category',
        'kays_instance_excl_prod_categ_rel',
        'instance_id',
        'categ_id',
        string='Excluded Product Categories',
    )
    last_stock_sync_at = fields.Datetime(string='Last Successful Stock Sync', readonly=True, copy=False)
    next_stock_sync_at = fields.Datetime(
        string='Next Scheduled Stock Sync',
        compute='_compute_next_stock_sync_at',
    )

    # -------------------------------------------------------------------------
    # Webhook Configuration (Part J)
    # -------------------------------------------------------------------------
    webhook_identifier = fields.Char(
        string='Webhook Endpoint Identifier',
        default=lambda self: secrets.token_urlsafe(24),
        required=True,
        copy=False,
        readonly=True,
    )
    webhook_secret = fields.Char(
        string='Webhook Verification Token',
        default=lambda self: secrets.token_urlsafe(32),
        groups='kays_mintsoft_integration.group_kays_admin',
        copy=False,
        tracking=False,
    )
    webhook_url = fields.Char(
        string='Webhook Callback URL',
        compute='_compute_webhook_url',
    )
    webhook_rate_limit_per_min = fields.Integer(
        string='Webhook Rate Limit (Requests / Minute)',
        default=120,
    )
    webhook_max_payload_bytes = fields.Integer(
        string='Max Webhook Payload Size (Bytes)',
        default=262144,
    )
    register_connect_action_on_order = fields.Boolean(
        string='Register ConnectAction Callback on Order Creation',
        default=True,
    )
    connect_action_account_id = fields.Integer(
        string='Mintsoft ConnectAction Account ID',
        default=0,
    )

    # -------------------------------------------------------------------------
    # Logging, Retention & Alerts (Part M)
    # -------------------------------------------------------------------------
    log_retention_days = fields.Integer(string='Operational Log Retention (Days)', default=30)
    debug_log_retention_days = fields.Integer(string='Debug Log Retention (Days)', default=7)
    alert_user_ids = fields.Many2many(
        'res.users',
        'kays_instance_alert_user_rel',
        'instance_id',
        'user_id',
        string='Designated Alert Recipients',
    )
    alert_cooldown_minutes = fields.Integer(
        string='Alert Cooldown (Minutes)',
        default=60,
        help='Minimum minutes between duplicate error alerts to prevent alert flooding.',
    )
    last_alert_sent_at = fields.Datetime(string='Last Alert Sent At', readonly=True, copy=False)
    enable_auto_refund_on_return = fields.Boolean(
        string='Enable Approved Return Credit Note Automation',
        default=False,
        groups='kays_mintsoft_integration.group_kays_admin',
        tracking=True,
    )

    # Relational Counts
    order_count = fields.Integer(compute='_compute_record_counts', string='Orders Count')
    product_mapping_count = fields.Integer(compute='_compute_record_counts', string='Mapped Products')
    discrepancy_count = fields.Integer(compute='_compute_record_counts', string='Stock Discrepancies')
    failed_job_count = fields.Integer(compute='_compute_record_counts', string='Failed Jobs')
    log_count = fields.Integer(compute='_compute_record_counts', string='API Logs')

    # -------------------------------------------------------------------------
    # Computed Fields & Constraints
    # -------------------------------------------------------------------------
    @api.depends('environment', 'production_api_url', 'test_api_url', 'custom_api_url')
    def _compute_active_api_url(self):
        env_labels = {
            'dry_run': _('Dry Run'),
            'test_client': _('Test Client'),
            'production': _('Production'),
        }
        for rec in self:
            if rec.custom_api_url and rec.custom_api_url.strip():
                url = rec.custom_api_url.strip()
            elif rec.environment == 'test_client':
                url = (rec.test_api_url or 'https://api.mintsoft.co.uk/api').strip()
            else:
                url = (rec.production_api_url or 'https://api.mintsoft.co.uk/api').strip()
            rec.active_api_url = url.rstrip('/')
            rec.environment_name = env_labels.get(rec.environment, rec.environment)

    def _compute_api_key_masked(self):
        for rec in self:
            raw_key = rec.sudo().api_key
            if not raw_key:
                rec.api_key_masked = _('Not Configured')
            elif len(raw_key) <= 8:
                rec.api_key_masked = '********'
            else:
                rec.api_key_masked = f"{raw_key[:4]}{'*' * 12}{raw_key[-4:]}"

    @api.depends('api_key_expiry_date')
    def _compute_api_key_expiry_warning(self):
        today = fields.Date.context_today(self)
        warn_horizon = today + timedelta(days=14)
        for rec in self:
            rec.api_key_expiry_warning = bool(
                rec.api_key_expiry_date and rec.api_key_expiry_date <= warn_horizon
            )

    @api.depends('last_stock_sync_at', 'stock_sync_interval', 'stock_sync_custom_minutes')
    def _compute_next_stock_sync_at(self):
        for rec in self:
            if not rec.last_stock_sync_at or not rec.enable_inventory_sync:
                rec.next_stock_sync_at = False
                continue
            mins = (
                rec.stock_sync_custom_minutes
                if rec.stock_sync_interval == 'custom'
                else int(rec.stock_sync_interval or 15)
            )
            rec.next_stock_sync_at = rec.last_stock_sync_at + timedelta(minutes=max(1, mins))

    @api.depends('webhook_identifier')
    def _compute_webhook_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', 'https://localhost')
        if base_url.startswith('http://') and 'localhost' not in base_url:
            base_url = 'https://' + base_url[len('http://'):]
        for rec in self:
            if rec.webhook_identifier:
                path = f"/kays/mintsoft/webhook/{rec.webhook_identifier}"
                rec.webhook_url = urljoin(base_url.rstrip('/') + '/', path.lstrip('/'))
            else:
                rec.webhook_url = False

    def _compute_record_counts(self):
        for rec in self:
            rec.order_count = self.env['kays.mintsoft.order'].search_count([('instance_id', '=', rec.id)])
            rec.product_mapping_count = self.env['kays.mintsoft.product.mapping'].search_count([('instance_id', '=', rec.id)])
            rec.discrepancy_count = self.env['kays.mintsoft.stock.discrepancy'].search_count([
                ('instance_id', '=', rec.id),
                ('status', 'in', ['review', 'pending_approval', 'error']),
            ])
            rec.failed_job_count = self.env['kays.mintsoft.sync.job'].search_count([
                ('instance_id', '=', rec.id),
                ('state', 'in', ['failed', 'dead_letter', 'unknown_outcome']),
            ])
            rec.log_count = self.env['kays.mintsoft.log'].search_count([('instance_id', '=', rec.id)])

    @api.constrains('production_api_url', 'test_api_url', 'custom_api_url', 'allowed_hosts')
    def _check_api_urls_security(self):
        for rec in self:
            allowed = rec.sudo().allowed_hosts or 'api.mintsoft.co.uk'
            for url_val in (rec.production_api_url, rec.test_api_url, rec.custom_api_url):
                if url_val and url_val.strip():
                    try:
                        validate_and_normalize_api_url(url_val, allowed)
                    except MintsoftSecurityError as err:
                        raise ValidationError(err.message) from err

    @api.constrains('environment', 'production_approved')
    def _check_production_mode_authorization(self):
        for rec in self:
            if rec.environment == 'production':
                if not self.env.user.has_group('kays_mintsoft_integration.group_kays_admin'):
                    raise ValidationError(_("Only Kays Integration Administrators may enable Production mode."))
                if not rec.sudo().production_approved:
                    raise ValidationError(
                        _("Use the 'Switch to Production Mode' confirmation wizard to authorize Production mode.")
                    )

    # -------------------------------------------------------------------------
    # Company Activation Auto-Synchronization
    # -------------------------------------------------------------------------
    def _sync_company_kays_activation(self):
        """
        Automatically enable Kays Integration on the connection's Odoo Company
        when an active connection with Enable Kays Integration is created or enabled.
        """
        for company in self.mapped('company_id').sudo():
            active_enabled = self.sudo().search([
                ('company_id', '=', company.id),
                ('active', '=', True),
                ('enable_integration', '=', True),
            ], order='is_default desc, sequence asc, id asc')
            if active_enabled:
                vals = {}
                if not company.kays_enabled:
                    vals['kays_enabled'] = True
                defaults = active_enabled.filtered('is_default')
                if not company.kays_default_instance_id or company.kays_default_instance_id not in active_enabled:
                    if len(active_enabled) == 1:
                        vals['kays_default_instance_id'] = active_enabled[0].id
                    elif len(defaults) == 1:
                        vals['kays_default_instance_id'] = defaults[0].id
                if vals:
                    company.write(vals)
                orders = self.env['sale.order'].sudo().search([
                    ('company_id', '=', company.id),
                    '|',
                    ('kays_export_status', '=', 'not_applicable'),
                    ('kays_instance_id', '=', False),
                ])
                if orders:
                    if len(active_enabled) == 1 or len(defaults) == 1:
                        target_inst = active_enabled[0] if len(active_enabled) == 1 else defaults[0]
                        orders.filtered(lambda o: not o.kays_instance_id).write({'kays_instance_id': target_inst.id})
                    orders._compute_kays_summary()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_company_kays_activation()
        return records

    def write(self, vals):
        res = super().write(vals)
        if any(k in vals for k in ('active', 'enable_integration', 'is_default', 'company_id', 'environment')):
            self._sync_company_kays_activation()
        return res

    def _register_hook(self):
        super()._register_hook()
        try:
            active_instances = self.sudo().search([
                ('active', '=', True),
                ('enable_integration', '=', True),
            ])
            if active_instances:
                active_instances._sync_company_kays_activation()
        except Exception as exc:
            _logger.debug("Kays instance register hook skipped: %s", exc)

    # -------------------------------------------------------------------------
    # Connection Action Buttons (Part B - Section 5)
    # -------------------------------------------------------------------------
    def action_test_connection(self):
        """Run safe read-only diagnostics across Warehouse, Products, Orders, Stock, and Client endpoints."""
        self.ensure_one()
        client = MintsoftClient(self)
        diag = client.run_connection_diagnostics()
        notif_type = 'success' if diag['overall_status'] in ('connected', 'partial') else 'danger'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': diag['headline'],
                'message': diag['summary_text'],
                'type': notif_type,
                'sticky': True,
            },
        }

    def action_validate_credentials(self):
        """Validate API key using safe read-only GET /api/Warehouse endpoint."""
        self.ensure_one()
        client = MintsoftClient(self)
        warehouses = client.request('GET', '/api/Warehouse', operation='validate_credentials')
        self.sudo().write({
            'last_auth_check': fields.Datetime.now(),
            'connection_status': 'connected',
        })
        count = len(warehouses) if isinstance(warehouses, list) else 0
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Credentials Validated'),
                'message': _('API Key authenticated successfully (HTTP 200, %d accessible warehouse(s)).') % count,
                'type': 'success',
            },
        }

    def action_fetch_warehouses(self):
        """Fetch warehouses from GET /api/Warehouse and create/update kays.mintsoft.warehouse.mapping."""
        self.ensure_one()
        client = MintsoftClient(self)
        warehouses = client.request('GET', '/api/Warehouse', operation='fetch_warehouses')
        if not isinstance(warehouses, list):
            raise UserError(_("Unexpected response from /api/Warehouse."))

        WhMapping = self.env['kays.mintsoft.warehouse.mapping']
        odoo_wh = self.default_warehouse_id or self.env['stock.warehouse'].with_company(self.company_id).search([
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        odoo_loc = self.default_location_id or (odoo_wh.lot_stock_id if odoo_wh else False)

        synced = 0
        for wh in warehouses:
            if not isinstance(wh, dict) or not wh.get('ID'):
                continue
            ms_wh_id = int(wh['ID'])
            ms_wh_name = (wh.get('Name') or f"Warehouse {ms_wh_id}").strip()
            ms_wh_code = (wh.get('Code') or '').strip()

            if synced == 0:
                self.write({
                    'mintsoft_warehouse_id': ms_wh_id,
                    'mintsoft_warehouse_name': ms_wh_name,
                })

            existing = WhMapping.search([
                ('instance_id', '=', self.id),
                ('mintsoft_warehouse_id', '=', ms_wh_id),
            ], limit=1)
            if existing:
                existing.write({
                    'mintsoft_warehouse_name': ms_wh_name,
                    'mintsoft_warehouse_code': ms_wh_code,
                    'last_sync_at': fields.Datetime.now(),
                })
            elif odoo_wh and odoo_loc:
                WhMapping.create({
                    'name': f"{ms_wh_name} ({ms_wh_id}) -> {odoo_wh.name}",
                    'company_id': self.company_id.id,
                    'instance_id': self.id,
                    'mintsoft_warehouse_id': ms_wh_id,
                    'mintsoft_warehouse_name': ms_wh_name,
                    'mintsoft_warehouse_code': ms_wh_code,
                    'odoo_warehouse_id': odoo_wh.id,
                    'odoo_location_id': odoo_loc.id,
                    'is_default': synced == 0,
                    'last_sync_at': fields.Datetime.now(),
                })
            synced += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Warehouses Fetched'),
                'message': _('Synchronized %d warehouse(s) from Mintsoft.') % synced,
                'type': 'success',
            },
        }

    def action_fetch_courier_services(self):
        """Fetch courier services from GET /api/Courier/Services."""
        self.ensure_one()
        from ..services.tracking_service import TrackingService
        count = TrackingService(self.env).fetch_and_sync_couriers(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Courier Services Fetched'),
                'message': _('Synchronized %d Mintsoft courier service(s).') % count,
                'type': 'success',
            },
        }

    def action_fetch_client_info(self):
        """Fetch client information from GET /api/Client if permitted (handles 401 gracefully)."""
        self.ensure_one()
        client = MintsoftClient(self)
        try:
            clients = client.request('GET', '/api/Client', params={'pageNo': 1, 'limit': 20}, operation='fetch_clients')
            if isinstance(clients, list) and clients:
                first = clients[0]
                self.write({
                    'mintsoft_client_id': int(first.get('ID') or self.mintsoft_client_id or 0),
                    'mintsoft_client_name': first.get('Name') or first.get('ShortName') or '',
                })
                msg = _('Fetched %d client record(s). Primary Client: %s (ID %s).') % (
                    len(clients), self.mintsoft_client_name, self.mintsoft_client_id
                )
                notif_type = 'success'
            else:
                msg = _('Client endpoint returned 0 records.')
                notif_type = 'info'
        except MintsoftAPIError as err:
            if err.http_status in (401, 403):
                msg = _(
                    'GET /api/Client returned HTTP %s (Admin-only endpoint). '
                    'This is normal for client-scoped API keys; Warehouse, Product, Order, and Stock APIs remain active.'
                ) % err.http_status
                notif_type = 'warning'
            else:
                raise UserError(err.message) from err

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Client API Check'),
                'message': msg,
                'type': notif_type,
                'sticky': True,
            },
        }

    def action_fetch_products(self):
        """Fetch products and update SKU mappings from GET /api/Product/List."""
        self.ensure_one()
        from ..services.product_service import ProductService
        stats = ProductService(self.env).fetch_and_map_products(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Products Fetched & Mapped'),
                'message': _(
                    'Matched: %(matched)d | Missing in Odoo: %(missing_in_odoo)d | '
                    'Missing in Mintsoft: %(missing_in_mintsoft)d | Duplicate SKUs: %(duplicate_sku)d'
                ) % stats,
                'type': 'success',
                'sticky': True,
            },
        }

    def action_fetch_order_statuses(self):
        """Fetch Mintsoft order statuses from GET /api/Order/Statuses and update mappings."""
        self.ensure_one()
        client = MintsoftClient(self)
        statuses = client.request('GET', '/api/Order/Statuses', operation='fetch_order_statuses')
        count = self.env['kays.mintsoft.status.mapping'].sync_from_api_payload(self, statuses)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Order Statuses Synced'),
                'message': _('Synchronized %d Mintsoft order status mappings.') % count,
                'type': 'success',
            },
        }

    def action_check_api_permissions(self):
        """Run full read-only permission diagnostics and update health_summary."""
        return self.action_test_connection()

    def action_refresh_connection_status(self):
        """Refresh connection status and fetch order statuses."""
        self.ensure_one()
        res = self.action_test_connection()
        try:
            self.action_fetch_order_statuses()
        except Exception as exc:
            _logger.debug("Order statuses refresh skipped: %s", exc)
        return res

    def action_view_api_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('API Logs — %s') % self.name,
            'res_model': 'kays.mintsoft.log',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id)],
            'context': {'default_instance_id': self.id, 'default_company_id': self.company_id.id},
        }

    def action_disable_connection(self):
        """Safely disable outbound writes and scheduled sync without deleting history."""
        self.ensure_one()
        self.write({
            'enable_integration': False,
            'enable_auto_order_export': False,
            'enable_scheduled_polling': False,
            'connection_status': 'disabled',
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Connection Disabled'),
                'message': _('Connection "%s" has been disabled safely. All historical records and logs are preserved.') % self.name,
                'type': 'warning',
            },
        }

    def action_open_test_order_wizard(self):
        """Open the Test Order Wizard (Part F - Section 11)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Create Test Order — %s') % self.name,
            'res_model': 'kays.test.order.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_instance_id': self.id,
                'default_company_id': self.company_id.id,
                'default_execution_mode': 'dry_run' if self.environment == 'dry_run' else 'test_client',
                'default_mintsoft_warehouse_id': self.mintsoft_warehouse_id or 3,
                'default_mintsoft_client_id': self.mintsoft_client_id or 0,
            },
        }

    def action_open_production_mode_wizard(self):
        """Open the confirmation wizard required before switching to Production mode."""
        self.ensure_one()
        if not self.env.user.has_group('kays_mintsoft_integration.group_kays_admin'):
            raise UserError(_("Only Kays Integration Administrators may activate Production mode."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Confirm Production Mode Activation'),
            'res_model': 'kays.production.mode.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_instance_id': self.id},
        }

    def action_regenerate_webhook_secret(self):
        self.ensure_one()
        if not self.env.user.has_group('kays_mintsoft_integration.group_kays_admin'):
            raise UserError(_("Only Kays Integration Administrators can rotate webhook credentials."))
        self.sudo().write({
            'webhook_identifier': secrets.token_urlsafe(24),
            'webhook_secret': secrets.token_urlsafe(32),
        })

    # -------------------------------------------------------------------------
    # Alert Notification Helper with Cooldown Deduplication
    # -------------------------------------------------------------------------
    def send_admin_alert(self, subject, body, force=False):
        """Post an internal notification / activity to designated integration administrators with cooldown."""
        self.ensure_one()
        now = fields.Datetime.now()
        if not force and self.last_alert_sent_at and self.alert_cooldown_minutes > 0:
            if now < self.last_alert_sent_at + timedelta(minutes=self.alert_cooldown_minutes):
                return False

        recipients = self.alert_user_ids
        if not recipients:
            admin_group = self.env.ref('kays_mintsoft_integration.group_kays_admin', raise_if_not_found=False)
            if admin_group:
                recipients = admin_group.users.filtered(lambda u: self.company_id in u.company_ids)

        partner_ids = recipients.mapped('partner_id').ids
        self.sudo().message_post(
            subject=subject,
            body=body,
            partner_ids=partner_ids,
            subtype_xmlid='mail.mt_comment',
        )
        self.sudo().last_alert_sent_at = now
        return True

    # -------------------------------------------------------------------------
    # Dashboard Data Endpoint (Part A - Section 4)
    # -------------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self, filters=None):
        """
        Return real-time multi-company KPI cards, charts, tables, and connection health
        for the Kays Logistics OWL Dashboard.
        """
        filters = filters or {}
        allowed_company_ids = self.env.companies.ids

        domain_order = [('company_id', 'in', allowed_company_ids)]
        domain_instance = [('company_id', 'in', allowed_company_ids)]
        domain_log = [('company_id', 'in', allowed_company_ids)]
        domain_disc = [('company_id', 'in', allowed_company_ids)]
        domain_job = [('company_id', 'in', allowed_company_ids)]
        domain_ship = [('company_id', 'in', allowed_company_ids)]

        if filters.get('company_id'):
            cid = int(filters['company_id'])
            if cid in allowed_company_ids:
                for d in (domain_order, domain_instance, domain_log, domain_disc, domain_job, domain_ship):
                    d.append(('company_id', '=', cid))

        if filters.get('instance_id'):
            iid = int(filters['instance_id'])
            domain_instance.append(('id', '=', iid))
            for d in (domain_order, domain_log, domain_disc, domain_job, domain_ship):
                d.append(('instance_id', '=', iid))

        if filters.get('environment'):
            env_val = filters['environment']
            domain_instance.append(('environment', '=', env_val))
            domain_order.append(('environment', '=', env_val))
            domain_log.append(('environment', '=', env_val))

        if filters.get('warehouse_id'):
            wh_val = int(filters['warehouse_id'])
            domain_order.append(('mintsoft_warehouse_id', '=', wh_val))

        if filters.get('fulfilment_status'):
            domain_order.append(('fulfilment_status', '=', filters['fulfilment_status']))

        if filters.get('date_from'):
            domain_order.append(('order_date', '>=', filters['date_from']))
            domain_log.append(('timestamp', '>=', filters['date_from']))

        if filters.get('date_to'):
            domain_order.append(('order_date', '<=', filters['date_to']))
            domain_log.append(('timestamp', '<=', filters['date_to']))

        Order = self.env['kays.mintsoft.order']
        Job = self.env['kays.mintsoft.sync.job']
        Disc = self.env['kays.mintsoft.stock.discrepancy']
        Log = self.env['kays.mintsoft.log']
        Ship = self.env['kays.mintsoft.shipment']

        today_start = fields.Datetime.to_string(
            fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        )

        # KPI Counts from actual records
        total_orders = Order.search_count(domain_order)
        pending_export = Order.search_count(domain_order + [('export_status', 'in', ['pending', 'validating', 'queued'])])
        exported_today = Order.search_count(domain_order + [('export_status', '=', 'sent'), ('last_sync_at', '>=', today_start)])
        processing_orders = Order.search_count(domain_order + [('fulfilment_status', 'in', ['received', 'awaiting_processing', 'processing', 'picked', 'packed'])])
        awaiting_dispatch = Order.search_count(domain_order + [('fulfilment_status', 'in', ['picked', 'packed', 'awaiting_processing'])])
        dispatched_today = Order.search_count(domain_order + [('fulfilment_status', 'in', ['dispatched', 'partially_dispatched']), ('despatch_date', '>=', today_start)])
        delivered_orders = Order.search_count(domain_order + [('fulfilment_status', '=', 'delivered')])
        cancelled_orders = Order.search_count(domain_order + [('fulfilment_status', '=', 'cancelled')])
        failed_orders = Order.search_count(domain_order + [('export_status', 'in', ['failed', 'unknown_outcome'])])
        awaiting_retry = Job.search_count(domain_job + [('state', 'in', ['retry_scheduled', 'failed', 'unknown_outcome'])])
        stock_diff_count = Disc.search_count(domain_disc + [('status', 'in', ['review', 'pending_approval', 'error'])])

        instances = self.search(domain_instance)
        primary_inst = instances[:1]
        last_stock_sync = (
            fields.Datetime.to_string(primary_inst.last_stock_sync_at)
            if primary_inst and primary_inst.last_stock_sync_at
            else _('Never')
        )
        next_stock_sync = (
            fields.Datetime.to_string(primary_inst.next_stock_sync_at)
            if primary_inst and primary_inst.next_stock_sync_at
            else _('Not Scheduled')
        )
        conn_status = primary_inst.connection_status if primary_inst else 'untested'

        # Orders by Status Chart
        status_buckets = [
            ('received', _('Received')),
            ('processing', _('Processing')),
            ('picked', _('Picked')),
            ('packed', _('Packed')),
            ('partially_dispatched', _('Part Dispatched')),
            ('dispatched', _('Dispatched')),
            ('delivered', _('Delivered')),
            ('cancelled', _('Cancelled')),
            ('on_hold', _('On Hold')),
        ]
        orders_by_status = []
        for code, label in status_buckets:
            cnt = Order.search_count(domain_order + [('fulfilment_status', '=', code)])
            orders_by_status.append({'status': code, 'label': label, 'count': cnt})

        # Daily Order Export Chart (last 7 days)
        daily_exports = []
        now_dt = fields.Datetime.now()
        for day_offset in range(6, -1, -1):
            d_start = (now_dt - timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
            d_end = d_start + timedelta(days=1)
            cnt = Order.search_count(
                domain_order + [
                    ('export_status', '=', 'sent'),
                    ('create_date', '>=', fields.Datetime.to_string(d_start)),
                    ('create_date', '<', fields.Datetime.to_string(d_end)),
                ]
            )
            daily_exports.append({
                'date': d_start.strftime('%d %b'),
                'count': cnt,
            })

        # API Response Time Chart (recent 10 API calls)
        recent_logs = Log.search(domain_log + [('http_status', '>', 0)], limit=10, order='timestamp desc')
        response_times = [
            {
                'id': lg.id,
                'endpoint': lg.endpoint,
                'duration_ms': lg.duration_ms,
                'http_status': lg.http_status,
                'timestamp': fields.Datetime.to_string(lg.timestamp),
            }
            for lg in reversed(recent_logs)
        ]

        # Recent Failed Requests
        failed_logs = Log.search(domain_log + [('status', '=', 'failure')], limit=8, order='timestamp desc')
        recent_failures = [
            {
                'id': lg.id,
                'timestamp': fields.Datetime.to_string(lg.timestamp),
                'instance': lg.instance_id.name,
                'operation': lg.operation,
                'endpoint': lg.endpoint,
                'http_status': lg.http_status,
                'error_message': (lg.error_message or '')[:140],
            }
            for lg in failed_logs
        ]

        # Latest Shipments
        latest_shipments_recs = Ship.search(domain_ship, limit=8, order='shipment_date desc, id desc')
        latest_shipments = [
            {
                'id': sh.id,
                'order_name': sh.kays_order_id.name,
                'courier': sh.courier_service_name or sh.courier_type or '-',
                'tracking_number': sh.tracking_number or '-',
                'delivery_status': sh.delivery_status,
                'shipment_date': fields.Datetime.to_string(sh.shipment_date) if sh.shipment_date else '-',
            }
            for sh in latest_shipments_recs
        ]

        # Stock Discrepancy Summary
        disc_recs = Disc.search(
            domain_disc + [('status', 'in', ['review', 'pending_approval', 'error', 'adjusted'])],
            limit=8,
            order='id desc',
        )
        stock_discrepancies = [
            {
                'id': d.id,
                'sku': d.sku,
                'odoo_available': d.odoo_available_qty,
                'mintsoft_available': d.mintsoft_available_qty,
                'difference': d.difference_qty,
                'status': d.status,
            }
            for d in disc_recs
        ]

        # Connection Health Cards
        connection_health = [
            {
                'id': inst.id,
                'name': inst.name,
                'company': inst.company_id.name,
                'environment': inst.environment,
                'environment_label': inst.environment_name,
                'status': inst.connection_status,
                'warehouse_id': inst.mintsoft_warehouse_id,
                'warehouse_name': inst.mintsoft_warehouse_name,
                'avg_response_ms': inst.avg_response_time_ms,
                'last_success': fields.Datetime.to_string(inst.last_successful_request) if inst.last_successful_request else _('Never'),
                'last_stock_sync': fields.Datetime.to_string(inst.last_stock_sync_at) if inst.last_stock_sync_at else _('Never'),
            }
            for inst in instances
        ]

        # Filter option lists
        all_companies = [
            {'id': c.id, 'name': c.name}
            for c in self.env.companies
        ]
        all_instances = [
            {'id': i.id, 'name': i.name, 'company_id': i.company_id.id}
            for i in self.search([('company_id', 'in', allowed_company_ids)])
        ]

        return {
            'kpis': {
                'total_orders': total_orders,
                'pending_export': pending_export,
                'exported_today': exported_today,
                'processing': processing_orders,
                'awaiting_dispatch': awaiting_dispatch,
                'dispatched_today': dispatched_today,
                'delivered': delivered_orders,
                'cancelled': cancelled_orders,
                'failed_orders': failed_orders,
                'awaiting_retry': awaiting_retry,
                'last_stock_sync': last_stock_sync,
                'next_stock_sync': next_stock_sync,
                'stock_diff_count': stock_diff_count,
                'connection_status': conn_status,
            },
            'orders_by_status': orders_by_status,
            'daily_exports': daily_exports,
            'response_times': response_times,
            'recent_failures': recent_failures,
            'latest_shipments': latest_shipments,
            'stock_discrepancies': stock_discrepancies,
            'connection_health': connection_health,
            'filter_options': {
                'companies': all_companies,
                'instances': all_instances,
            },
        }
