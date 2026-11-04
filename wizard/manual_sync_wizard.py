from odoo import fields, models, _
from odoo.exceptions import UserError

from ..services.order_service import OrderService
from ..services.product_service import ProductService
from ..services.inventory_service import InventoryService
from ..services.tracking_service import TrackingService
from ..services.return_service import ReturnService


class KaysManualSyncWizard(models.TransientModel):
    _name = 'kays.manual.sync.wizard'
    _description = 'Kays Logistics Manual Synchronization Wizard'

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
    )
    instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Connection',
        required=True,
        domain="[('company_id', '=', company_id)]",
    )
    sync_operation = fields.Selection(
        selection=[
            ('orders_export', 'Export Pending Orders'),
            ('orders_status', 'Sync Active Order Statuses'),
            ('stock_full', 'Full Stock Reconciliation'),
            ('stock_incremental', 'Incremental Stock Sync'),
            ('products', 'Fetch & Map Products / SKUs'),
            ('couriers', 'Fetch Courier Services'),
            ('tracking', 'Sync Shipment Tracking Events'),
            ('returns', 'Poll & Sync Returns'),
        ],
        string='Operation to Execute',
        default='stock_full',
        required=True,
    )

    def action_execute_sync(self):
        self.ensure_one()
        inst = self.instance_id.with_company(self.company_id)
        if not inst.company_id.kays_enabled:
            raise UserError(_("Kays Logistics is disabled for company '%s'.") % inst.company_id.display_name)

        if self.sync_operation == 'orders_export':
            self.env['kays.mintsoft.sync.job']._execute_scheduled_task('pending_order_export', force=True)
            msg = _('Executed pending order exports.')
        elif self.sync_operation == 'orders_status':
            self.env['kays.mintsoft.sync.job']._execute_scheduled_task('order_status_sync', force=True)
            msg = _('Synchronized active order statuses.')
        elif self.sync_operation == 'stock_full':
            snap = InventoryService(self.env).run_stock_sync(inst, sync_mode='full', is_manual=True)
            return {
                'type': 'ir.actions.act_window',
                'name': _('Stock Snapshot — %s') % snap.name,
                'res_model': 'kays.mintsoft.stock.snapshot',
                'res_id': snap.id,
                'view_mode': 'form',
                'target': 'current',
            }
        elif self.sync_operation == 'stock_incremental':
            snap = InventoryService(self.env).run_stock_sync(inst, sync_mode='incremental', is_manual=True)
            return {
                'type': 'ir.actions.act_window',
                'name': _('Stock Snapshot — %s') % snap.name,
                'res_model': 'kays.mintsoft.stock.snapshot',
                'res_id': snap.id,
                'view_mode': 'form',
                'target': 'current',
            }
        elif self.sync_operation == 'products':
            stats = ProductService(self.env).fetch_and_map_products(inst)
            msg = _('Product sync complete (Matched: %d, Missing in Odoo: %d).') % (stats['matched'], stats['missing_in_odoo'])
        elif self.sync_operation == 'couriers':
            cnt = TrackingService(self.env).fetch_and_sync_couriers(inst)
            msg = _('Synchronized %d courier service(s).') % cnt
        elif self.sync_operation == 'tracking':
            cnt = TrackingService(self.env).sync_tracking_events(inst)
            msg = _('Processed %d tracking event(s).') % cnt
        elif self.sync_operation == 'returns':
            cnt = ReturnService(self.env).poll_returns_list(inst)
            msg = _('Synchronized %d return record(s).') % cnt
        else:
            msg = _('Sync completed.')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Manual Sync Completed'),
                'message': msg,
                'type': 'success',
            },
        }
