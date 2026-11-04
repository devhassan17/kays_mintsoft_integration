from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.mintsoft_client import MintsoftClient


class KaysMintsoftWarehouseMapping(models.Model):
    _name = 'kays.mintsoft.warehouse.mapping'
    _description = 'Kays Logistics Warehouse & Location Mapping'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'company_id, is_default desc, mintsoft_warehouse_id'
    _check_company_auto = True

    _unique_warehouse_per_instance = models.Constraint(
        'UNIQUE(instance_id, mintsoft_warehouse_id)',
        'Each Mintsoft Warehouse ID can only be mapped once per connection!',
    )

    name = fields.Char(string='Mapping Name', required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    is_default = fields.Boolean(string='Default Warehouse Mapping', default=False, tracking=True)
    company_id = fields.Many2one(
        'res.company',
        string='Odoo Company',
        required=True,
        default=lambda self: self.env.company,
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
    mintsoft_warehouse_id = fields.Integer(
        string='Mintsoft Warehouse ID',
        required=True,
        default=3,
        index=True,
        tracking=True,
    )
    mintsoft_warehouse_name = fields.Char(
        string='Mintsoft Warehouse Name',
        required=True,
        default='Main Warehouse',
        tracking=True,
    )
    mintsoft_warehouse_code = fields.Char(string='Mintsoft Warehouse Code')
    odoo_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Odoo Warehouse',
        required=True,
        check_company=True,
        tracking=True,
    )
    odoo_location_id = fields.Many2one(
        'stock.location',
        string='Odoo Stock Location',
        required=True,
        domain="[('usage', '=', 'internal')]",
        check_company=True,
        tracking=True,
        help='Exact Odoo internal stock location updated during Mintsoft stock synchronization.',
    )
    last_sync_at = fields.Datetime(string='Last Stock Sync', readonly=True)

    @api.onchange('odoo_warehouse_id')
    def _onchange_odoo_warehouse_id(self):
        for rec in self:
            if rec.odoo_warehouse_id and not rec.odoo_location_id:
                rec.odoo_location_id = rec.odoo_warehouse_id.lot_stock_id

    def action_verify_warehouse(self):
        """Verify warehouse against GET /api/Warehouse."""
        self.ensure_one()
        client = MintsoftClient(self.instance_id)
        warehouses = client.request('GET', '/api/Warehouse', operation='verify_warehouse')
        matched = None
        if isinstance(warehouses, list):
            for wh in warehouses:
                if isinstance(wh, dict) and int(wh.get('ID') or 0) == self.mintsoft_warehouse_id:
                    matched = wh
                    break
        if not matched:
            raise UserError(
                _("Mintsoft Warehouse ID %s was not found in the accessible warehouses for this API key.")
                % self.mintsoft_warehouse_id
            )
        self.write({
            'mintsoft_warehouse_name': matched.get('Name') or self.mintsoft_warehouse_name,
            'mintsoft_warehouse_code': matched.get('Code') or '',
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Warehouse Verified'),
                'message': _('Verified Mintsoft Warehouse "%s" (ID %s).') % (self.mintsoft_warehouse_name, self.mintsoft_warehouse_id),
                'type': 'success',
            },
        }

    def action_sync_warehouse_stock_now(self):
        self.ensure_one()
        from ..services.inventory_service import InventoryService
        snap = InventoryService(self.env).run_stock_sync(
            self.instance_id,
            sync_mode='full',
            warehouse_mapping=self,
            is_manual=True,
        )
        return {
            'type': 'ir.actions.act_window',
            'name': _('Stock Snapshot — %s') % snap.name,
            'res_model': 'kays.mintsoft.stock.snapshot',
            'res_id': snap.id,
            'view_mode': 'form',
            'target': 'current',
        }
