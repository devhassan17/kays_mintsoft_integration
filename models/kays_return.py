from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.return_service import ReturnService


class KaysMintsoftReturn(models.Model):
    _name = 'kays.mintsoft.return'
    _description = 'Kays Logistics Return / RMA Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(
        string='Odoo Return Reference',
        required=True,
        default=lambda self: _('New'),
        index=True,
        tracking=True,
    )
    mintsoft_return_id = fields.Integer(string='Mintsoft Return ID', index=True, tracking=True, copy=False)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        ondelete='restrict',
        index=True,
        check_company=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Original Sales Order',
        index=True,
        check_company=True,
        tracking=True,
    )
    kays_order_id = fields.Many2one(
        'kays.mintsoft.order',
        string='Original Kays Order',
        index=True,
        check_company=True,
    )
    mintsoft_order_id = fields.Integer(string='Mintsoft Order ID', index=True)
    return_picking_id = fields.Many2one(
        'stock.picking',
        string='Odoo Return Picking',
        check_company=True,
    )
    reason = fields.Char(string='Return Reason', required=True, tracking=True)
    mintsoft_reason_id = fields.Integer(string='Mintsoft Reason ID', default=1)
    expected_return_date = fields.Date(string='Expected Return Date', tracking=True)

    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('submitted', 'Submitted to Mintsoft'),
            ('confirmed', 'Confirmed in Mintsoft'),
            ('partially_received', 'Partially Received'),
            ('received', 'Received'),
            ('closed', 'Closed'),
            ('failed', 'Failed'),
        ],
        string='Return Status',
        default='draft',
        required=True,
        index=True,
        tracking=True,
    )
    inventory_outcome = fields.Selection(
        selection=[
            ('pending', 'Pending Inspection'),
            ('restocked', 'Returned to Good Stock'),
            ('quarantined', 'Quarantined'),
            ('scrapped', 'Damaged / Scrapped'),
        ],
        string='Inventory Outcome',
        default='pending',
        required=True,
        tracking=True,
    )
    refund_approved = fields.Boolean(
        string='Refund Explicitly Approved by Accounting',
        default=False,
        tracking=True,
        help='Returns never issue automatic refunds unless explicitly approved.',
    )
    total_requested_qty = fields.Float(compute='_compute_totals', string='Requested Quantity', store=True)
    total_received_qty = fields.Float(compute='_compute_totals', string='Received Quantity', store=True)
    last_sync_at = fields.Datetime(string='Last Sync', readonly=True)
    last_error = fields.Text(string='Last Error', readonly=True)

    line_ids = fields.One2many(
        'kays.mintsoft.return.line',
        'return_id',
        string='Return Items',
    )

    @api.depends('line_ids.quantity', 'line_ids.received_quantity')
    def _compute_totals(self):
        for rec in self:
            rec.total_requested_qty = sum(rec.line_ids.mapped('quantity'))
            rec.total_received_qty = sum(rec.line_ids.mapped('received_quantity'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('kays.mintsoft.return') or f"RMA-{fields.Datetime.now().strftime('%Y%m%d%H%M%S')}"
        return super().create(vals_list)

    @api.onchange('kays_order_id')
    def _onchange_kays_order_id(self):
        for rec in self:
            if rec.kays_order_id:
                rec.sale_order_id = rec.kays_order_id.sale_order_id
                rec.instance_id = rec.kays_order_id.instance_id
                rec.company_id = rec.kays_order_id.company_id
                rec.mintsoft_order_id = rec.kays_order_id.mintsoft_order_id

    # -------------------------------------------------------------------------
    # Return Actions (Part K - Section 19)
    # -------------------------------------------------------------------------
    def action_create_mintsoft_return(self):
        """Submit return request to Mintsoft."""
        self.ensure_one()
        ReturnService(self.env).create_return_in_mintsoft(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Return Submitted'),
                'message': _('Return "%s" submitted to Mintsoft.') % self.name,
                'type': 'success',
            },
        }

    def action_sync_return_status(self):
        """Refresh return status and received quantities from Mintsoft."""
        svc = ReturnService(self.env)
        for rec in self:
            svc.sync_return_status(rec)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Return Status Synced'),
                'message': _('Synchronized return status with Mintsoft.'),
                'type': 'success',
            },
        }

    def action_retry_failed_return(self):
        """Retry submitting a failed return request."""
        self.ensure_one()
        if not self.mintsoft_return_id:
            return self.action_create_mintsoft_return()
        return self.action_sync_return_status()

    def action_view_return_history(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Return Logs — %s') % self.name,
            'res_model': 'kays.mintsoft.log',
            'view_mode': 'list,form',
            'domain': [
                ('instance_id', '=', self.instance_id.id),
                ('endpoint', 'ilike', '/api/Return'),
            ],
        }


class KaysMintsoftReturnLine(models.Model):
    _name = 'kays.mintsoft.return.line'
    _description = 'Kays Logistics Return Item Line'
    _order = 'id'

    return_id = fields.Many2one(
        'kays.mintsoft.return',
        string='Return Request',
        required=True,
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        related='return_id.company_id',
        store=True,
    )
    product_id = fields.Many2one('product.product', string='Product', required=True)
    product_mapping_id = fields.Many2one('kays.mintsoft.product.mapping', string='Product Mapping')
    sku = fields.Char(string='Mintsoft SKU', required=True)
    mintsoft_product_id = fields.Integer(string='Mintsoft Product ID')
    quantity = fields.Float(string='Expected Return Qty', default=1.0, required=True)
    received_quantity = fields.Float(string='Received Qty', default=0.0, readonly=True)
    mintsoft_reason_id = fields.Integer(string='Mintsoft Reason ID', default=1)
    requested_action = fields.Selection(
        selection=[
            ('Refund', 'Inspection / Credit Review'),
            ('Exchange', 'Replacement / Exchange'),
            ('ReturnToStock', 'Restock Only'),
        ],
        string='Requested Action',
        default='Refund',
    )
    returned_to_stock = fields.Boolean(string='Restocked in Mintsoft', default=False, readonly=True)
    stock_action = fields.Char(string='Warehouse Stock Outcome', readonly=True)
    notes = fields.Char(string='Comments')

    @api.onchange('product_id')
    def _onchange_product_id(self):
        for rec in self:
            if rec.product_id:
                rec.sku = rec.product_id.default_code or ''
                if rec.return_id.instance_id:
                    mapping = self.env['kays.mintsoft.product.mapping'].search([
                        ('instance_id', '=', rec.return_id.instance_id.id),
                        ('product_id', '=', rec.product_id.id),
                    ], limit=1)
                    if mapping:
                        rec.product_mapping_id = mapping.id
                        rec.sku = mapping.mintsoft_sku
                        rec.mintsoft_product_id = mapping.mintsoft_product_id
