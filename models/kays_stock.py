from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.inventory_service import InventoryService


class KaysMintsoftStockSnapshot(models.Model):
    _name = 'kays.mintsoft.stock.snapshot'
    _description = 'Kays Logistics Stock Synchronization Snapshot'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'started_at desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Snapshot Reference', required=True, index=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        index=True,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    sync_mode = fields.Selection(
        selection=[
            ('full', 'Full Reconciliation'),
            ('incremental', 'Incremental Sync'),
        ],
        string='Sync Mode',
        default='full',
        required=True,
    )
    authority_mode = fields.Selection(
        selection=[
            ('mintsoft', 'Mintsoft Authoritative'),
            ('monitoring_only', 'Monitoring Only'),
            ('disabled', 'Disabled'),
        ],
        string='Authority Mode',
        default='mintsoft',
    )
    dry_run = fields.Boolean(string='Dry Run', default=False)
    state = fields.Selection(
        selection=[
            ('running', 'Running'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        string='Status',
        default='running',
        required=True,
        index=True,
        tracking=True,
    )
    started_at = fields.Datetime(string='Started At', default=fields.Datetime.now, index=True)
    completed_at = fields.Datetime(string='Completed At')
    total_skus_checked = fields.Integer(string='Total SKUs Checked', default=0)
    matched_count = fields.Integer(string='Matched SKUs', default=0)
    discrepancy_count = fields.Integer(string='Discrepancies Found', default=0)
    adjusted_count = fields.Integer(string='Auto-Adjusted SKUs', default=0)
    skipped_count = fields.Integer(string='Skipped SKUs', default=0)
    error_count = fields.Integer(string='Errors', default=0)
    notes = fields.Text(string='Summary Notes')

    discrepancy_ids = fields.One2many(
        'kays.mintsoft.stock.discrepancy',
        'snapshot_id',
        string='Stock Discrepancies',
    )


class KaysMintsoftStockDiscrepancy(models.Model):
    _name = 'kays.mintsoft.stock.discrepancy'
    _description = 'Kays Logistics Stock Difference & Reconciliation Line'
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(string='Reference', compute='_compute_name', store=True)
    snapshot_id = fields.Many2one(
        'kays.mintsoft.stock.snapshot',
        string='Stock Snapshot',
        ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        index=True,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        index=True,
    )
    warehouse_mapping_id = fields.Many2one(
        'kays.mintsoft.warehouse.mapping',
        string='Warehouse Mapping',
    )
    product_mapping_id = fields.Many2one(
        'kays.mintsoft.product.mapping',
        string='Product Mapping',
        index=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Odoo Product',
        index=True,
    )
    sku = fields.Char(string='SKU', required=True, index=True)
    mintsoft_product_id = fields.Integer(string='Mintsoft Product ID')
    location_id = fields.Many2one(
        'stock.location',
        string='Odoo Stock Location',
        index=True,
    )

    odoo_on_hand_qty = fields.Float(string='Odoo On Hand', default=0.0)
    odoo_reserved_qty = fields.Float(string='Odoo Reserved', default=0.0)
    odoo_available_qty = fields.Float(string='Odoo Available', default=0.0)

    mintsoft_on_hand_qty = fields.Float(string='Mintsoft On Hand', default=0.0)
    mintsoft_allocated_qty = fields.Float(string='Mintsoft Allocated', default=0.0)
    mintsoft_available_qty = fields.Float(string='Mintsoft Available', default=0.0)

    difference_qty = fields.Float(string='Difference', default=0.0, index=True)

    status = fields.Selection(
        selection=[
            ('matched', 'Matched'),
            ('review', 'Review'),
            ('pending_approval', 'Pending Approval'),
            ('approved', 'Approved'),
            ('adjusted', 'Adjusted'),
            ('skipped', 'Skipped'),
            ('error', 'Error'),
        ],
        string='Status',
        default='review',
        required=True,
        index=True,
    )
    reason = fields.Text(string='Details / Reason')
    adjusted_at = fields.Datetime(string='Adjusted At', readonly=True)
    adjusted_by = fields.Many2one('res.users', string='Adjusted By', readonly=True)

    @api.depends('sku', 'difference_qty', 'status')
    def _compute_name(self):
        for rec in self:
            rec.name = f"{rec.sku} ({rec.difference_qty:+.2f}) [{rec.status}]"

    def action_approve_and_apply(self):
        """Approve an unusual or review stock difference and apply it to Odoo stock.quant."""
        svc = InventoryService(self.env)
        applied = 0
        for rec in self:
            if rec.status in ('adjusted', 'matched'):
                continue
            if not rec.product_id or not rec.location_id:
                raise UserError(_("Discrepancy for SKU '%s' is missing a linked Odoo Product or Stock Location.") % rec.sku)
            svc.apply_discrepancy_adjustment(rec)
            applied += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Stock Adjustment Applied'),
                'message': _('Applied inventory adjustment for %d SKU(s).') % applied,
                'type': 'success',
            },
        }

    def action_skip_discrepancy(self):
        self.write({
            'status': 'skipped',
            'reason': _('Manually skipped by user %s.') % self.env.user.name,
        })
