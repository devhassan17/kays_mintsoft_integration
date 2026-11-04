from datetime import timedelta
from odoo import api, fields, models, _


class KaysMintsoftLog(models.Model):
    _name = 'kays.mintsoft.log'
    _description = 'Kays Logistics Sanitized API & Audit Log'
    _order = 'timestamp desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Log Summary', compute='_compute_name', store=True)
    timestamp = fields.Datetime(
        string='Timestamp',
        default=fields.Datetime.now,
        required=True,
        index=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        index=True,
        readonly=True,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        ondelete='cascade',
        index=True,
        readonly=True,
    )
    environment = fields.Selection(
        selection=[
            ('dry_run', 'Dry Run'),
            ('test_client', 'Test Client'),
            ('production', 'Production'),
        ],
        string='Environment',
        index=True,
        readonly=True,
    )
    operation = fields.Char(string='Operation', required=True, index=True, readonly=True)
    http_method = fields.Char(string='HTTP Method', required=True, readonly=True)
    endpoint = fields.Char(string='Endpoint', required=True, index=True, readonly=True)
    http_status = fields.Integer(string='HTTP Status', index=True, readonly=True)
    duration_ms = fields.Integer(string='Duration (ms)', readonly=True)

    sale_order_id = fields.Many2one('sale.order', string='Related Odoo Order', index=True, readonly=True, ondelete='set null')
    kays_order_id = fields.Many2one('kays.mintsoft.order', string='Kays Order', index=True, readonly=True, ondelete='set null')
    mintsoft_order_id = fields.Integer(string='Mintsoft Order ID', index=True, readonly=True)
    sync_job_id = fields.Many2one('kays.mintsoft.sync.job', string='Job ID', index=True, readonly=True, ondelete='set null')

    status = fields.Selection(
        selection=[
            ('success', 'Success'),
            ('warning', 'Warning'),
            ('failure', 'Failure'),
        ],
        string='Outcome',
        default='success',
        required=True,
        index=True,
        readonly=True,
    )
    is_debug = fields.Boolean(string='Debug Log', default=False, index=True, readonly=True)
    error_code = fields.Char(string='Error Code', index=True, readonly=True)
    retry_count = fields.Integer(string='Retry Count', default=0, readonly=True)
    correlation_id = fields.Char(string='Correlation ID', index=True, readonly=True)

    request_summary = fields.Text(string='Sanitized Request Payload', readonly=True)
    response_summary = fields.Text(string='Sanitized Response Payload', readonly=True)
    error_message = fields.Text(string='Error Details', readonly=True)

    @api.depends('http_method', 'endpoint', 'http_status', 'status')
    def _compute_name(self):
        for rec in self:
            rec.name = f"{rec.http_method or 'API'} {rec.endpoint or ''} [{rec.http_status or rec.status}]"

    @api.model
    def _cleanup_expired_logs(self):
        """Purge expired operational and debug logs according to each connection's retention policy."""
        now = fields.Datetime.now()
        deleted_total = 0
        instances = self.env['kays.mintsoft.instance'].sudo().search(['|', ('active', '=', True), ('active', '=', False)])
        for inst in instances:
            op_days = max(1, inst.log_retention_days or 30)
            dbg_days = max(1, inst.debug_log_retention_days or 7)
            op_cutoff = now - timedelta(days=op_days)
            dbg_cutoff = now - timedelta(days=dbg_days)

            expired = self.sudo().search([
                ('instance_id', '=', inst.id),
                '|',
                '&', ('is_debug', '=', True), ('timestamp', '<', dbg_cutoff),
                '&', ('is_debug', '=', False), ('timestamp', '<', op_cutoff),
            ], limit=2000)
            if expired:
                deleted_total += len(expired)
                expired.unlink()
        return deleted_total
