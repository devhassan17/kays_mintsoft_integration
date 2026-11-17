import json
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.mintsoft_client import sanitize_payload
from ..services.order_service import OrderService

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    kays_enabled = fields.Boolean(
        related='company_id.kays_enabled',
        string='Kays Integration Enabled',
        readonly=True,
    )
    kays_instance_id = fields.Many2one(
        'kays.mintsoft.instance',
        string='Kays Connection',
        domain="[('company_id', '=', company_id)]",
        check_company=True,
        copy=False,
        tracking=True,
    )
    kays_payment_approved = fields.Boolean(
        string='Kays Payment / Export Approved',
        default=False,
        copy=False,
        tracking=True,
    )
    kays_delivery_notes = fields.Text(
        string='Kays Warehouse / Delivery Notes',
        copy=False,
    )
    is_subscription_order = fields.Boolean(
        compute='_compute_is_subscription_order',
        string='Is Subscription Order',
    )

    def _compute_is_subscription_order(self):
        for order in self:
            is_sub = bool(
                getattr(order, 'is_subscription', False)
                or getattr(order, 'recurring_plan_id', False)
                or order.order_line.filtered(lambda l: getattr(l, 'recurring_invoice', False))
            )
            order.is_subscription_order = is_sub

    kays_order_ids = fields.One2many(
        'kays.mintsoft.order',
        'sale_order_id',
        string='Related Mintsoft Orders',
        copy=False,
    )
    kays_order_count = fields.Integer(
        compute='_compute_kays_summary',
        string='Kays Orders Count',
    )
    kays_primary_order_id = fields.Many2one(
        'kays.mintsoft.order',
        compute='_compute_kays_summary',
        string='Primary Kays Order',
        store=True,
    )

    # Summary fields displayed on the dedicated Kays Logistics tab (Part D - Section 9)
    kays_mintsoft_client_id = fields.Integer(
        compute='_compute_kays_summary',
        string='Mintsoft Client ID',
    )
    kays_mintsoft_warehouse = fields.Char(
        compute='_compute_kays_summary',
        string='Mintsoft Warehouse',
    )
    kays_mintsoft_order_id = fields.Integer(
        compute='_compute_kays_summary',
        string='Mintsoft Order ID',
        store=True,
    )
    kays_external_reference = fields.Char(
        compute='_compute_kays_summary',
        string='External Order Reference',
        store=True,
    )
    kays_export_status = fields.Selection(
        selection=[
            ('not_applicable', 'Not Applicable'),
            ('pending', 'Pending'),
            ('validating', 'Validating'),
            ('queued', 'Queued'),
            ('sending', 'Sending'),
            ('sent', 'Sent'),
            ('failed', 'Failed'),
            ('unknown_outcome', 'Unknown Outcome'),
        ],
        compute='_compute_kays_summary',
        string='Export Status',
        store=True,
    )
    kays_fulfilment_status = fields.Selection(
        selection=[
            ('unknown', 'Unknown'),
            ('received', 'Received'),
            ('awaiting_confirmation', 'Awaiting Confirmation'),
            ('awaiting_processing', 'Awaiting Processing'),
            ('processing', 'Processing'),
            ('picked', 'Picked'),
            ('packed', 'Packed'),
            ('partially_dispatched', 'Partially Dispatched'),
            ('dispatched', 'Dispatched'),
            ('delivered', 'Delivered'),
            ('cancelled', 'Cancelled'),
            ('on_hold', 'On Hold'),
            ('other', 'Other'),
        ],
        compute='_compute_kays_summary',
        string='Fulfilment Status',
        store=True,
    )
    kays_shipment_status = fields.Selection(
        selection=[
            ('awaiting_dispatch', 'Awaiting Dispatch'),
            ('processing', 'Processing'),
            ('partial_dispatch', 'Partial Dispatch'),
            ('dispatched', 'Full Dispatch'),
            ('tracking_available', 'Tracking Available'),
            ('in_transit', 'In Transit'),
            ('delivered', 'Delivered'),
            ('cancelled', 'Cancelled'),
            ('returned', 'Returned'),
        ],
        compute='_compute_kays_summary',
        string='Shipment Status',
    )
    kays_tracking_number = fields.Char(
        compute='_compute_kays_summary',
        string='Tracking Number',
        store=True,
    )
    kays_courier_name = fields.Char(
        compute='_compute_kays_summary',
        string='Courier',
    )
    kays_last_sync_at = fields.Datetime(
        compute='_compute_kays_summary',
        string='Last Sync',
    )
    kays_next_retry_at = fields.Datetime(
        compute='_compute_kays_summary',
        string='Next Retry',
    )
    kays_retry_count = fields.Integer(
        compute='_compute_kays_summary',
        string='Retry Count',
    )
    kays_last_error = fields.Text(
        compute='_compute_kays_summary',
        string='Last Error',
    )
    kays_payload_preview = fields.Text(
        compute='_compute_kays_summary',
        string='Payload Preview',
    )

    @api.depends(
        'company_id.kays_enabled',
        'kays_instance_id',
        'kays_order_ids',
        'kays_order_ids.mintsoft_order_id',
        'kays_order_ids.external_order_reference',
        'kays_order_ids.export_status',
        'kays_order_ids.fulfilment_status',
        'kays_order_ids.tracking_number',
        'kays_order_ids.mintsoft_courier_service_name',
        'kays_order_ids.last_sync_at',
        'kays_order_ids.next_retry_at',
        'kays_order_ids.retry_count',
        'kays_order_ids.last_error',
        'kays_order_ids.payload_preview',
        'kays_order_ids.shipment_ids.delivery_status',
    )
    def _compute_kays_summary(self):
        for order in self:
            order.kays_order_count = len(order.kays_order_ids)
            primary = order.kays_order_ids.filtered(lambda o: not o.is_split_order)[:1] or order.kays_order_ids[:1]
            order.kays_primary_order_id = primary

            inst = primary.instance_id if primary else order.kays_instance_id
            if not inst and order.company_id.kays_enabled:
                try:
                    inst = order.company_id._get_default_kays_instance(sale_order=order)
                except UserError:
                    inst = self.env['kays.mintsoft.instance']
            if not order.company_id.kays_enabled:
                order.kays_mintsoft_client_id = 0
                order.kays_mintsoft_warehouse = '-'
                order.kays_mintsoft_order_id = 0
                order.kays_external_reference = ''
                order.kays_export_status = 'not_applicable'
                order.kays_fulfilment_status = 'unknown'
                order.kays_shipment_status = False
                order.kays_tracking_number = ''
                order.kays_courier_name = ''
                order.kays_last_sync_at = False
                order.kays_next_retry_at = False
                order.kays_retry_count = 0
                order.kays_last_error = False
                order.kays_payload_preview = False
                continue

            order.kays_mintsoft_client_id = primary.mintsoft_client_id if primary else (inst.mintsoft_client_id if inst else 0)
            order.kays_mintsoft_warehouse = primary.warehouse_display if primary else (
                f"{inst.mintsoft_warehouse_name} (#{inst.mintsoft_warehouse_id})" if inst else '-'
            )
            order.kays_mintsoft_order_id = primary.mintsoft_order_id if primary else 0
            order.kays_external_reference = primary.external_order_reference if primary else (
                OrderService.compute_external_reference(order, inst) if inst else order.name
            )
            order.kays_export_status = primary.export_status if primary else 'pending'
            order.kays_fulfilment_status = primary.fulfilment_status if primary else 'unknown'
            first_ship = primary.shipment_ids[:1] if primary else False
            order.kays_shipment_status = first_ship.delivery_status if first_ship else False
            order.kays_tracking_number = ', '.join(
                dict.fromkeys(o.tracking_number for o in order.kays_order_ids if o.tracking_number)
            )
            order.kays_courier_name = primary.mintsoft_courier_service_name if primary else ''
            order.kays_last_sync_at = primary.last_sync_at if primary else False
            order.kays_next_retry_at = primary.next_retry_at if primary else False
            order.kays_retry_count = primary.retry_count if primary else 0
            order.kays_last_error = primary.last_error if primary else False
            order.kays_payload_preview = primary.payload_preview if primary else False

    # -------------------------------------------------------------------------
    # Lifecycle Hooks: Automatic Export & Safe Cancellation
    # -------------------------------------------------------------------------
    def action_confirm(self):
        res = super().action_confirm()
        for order in self:
            order._kays_trigger_auto_export_on_confirm()
        return res

    def _kays_trigger_auto_export_on_confirm(self):
        """
        Evaluate whether this confirmed Sales Order should be exported or queued for Kays Logistics.
        Enforces strict multi-company isolation: disabled companies NEVER export orders automatically.
        """
        self.ensure_one()
        if not self.company_id.kays_enabled:
            return False

        try:
            instance = self.kays_instance_id or self.company_id._get_default_kays_instance(sale_order=self)
        except UserError as routing_err:
            _logger.warning("Kays connection routing skipped for %s: %s", self.name, routing_err)
            return False

        if not instance or not instance.active or not instance.enable_integration:
            return False

        self.kays_instance_id = instance.id
        if not instance.enable_auto_order_export:
            return False

        # Export immediately if payment eligibility is met; otherwise create a pending kays.mintsoft.order record
        svc = OrderService(self.env)
        pay_ok, _reason = svc.check_payment_eligibility(self, instance)
        if pay_ok:
            try:
                svc.export_sale_order(self, instance=instance, is_manual=False)
            except Exception as exc:
                _logger.warning("Auto-export on confirm for %s encountered error: %s", self.name, exc)
        else:
            existing = self.env['kays.mintsoft.order'].search([
                ('sale_order_id', '=', self.id),
                ('instance_id', '=', instance.id),
                ('is_split_order', '=', False),
            ], limit=1)
            if not existing:
                ext_ref = svc.compute_external_reference(self, instance)
                self.env['kays.mintsoft.order'].create({
                    'name': ext_ref,
                    'external_order_reference': ext_ref,
                    'idempotency_key': f"{instance.id}:{ext_ref}",
                    'sale_order_id': self.id,
                    'company_id': self.company_id.id,
                    'instance_id': instance.id,
                    'partner_id': self.partner_id.id,
                    'shipping_partner_id': (self.partner_shipping_id or self.partner_id).id,
                    'order_date': self.date_order or fields.Datetime.now(),
                    'currency_id': self.currency_id.id,
                    'order_total': self.amount_total,
                    'export_status': 'pending',
                    'fulfilment_status': 'awaiting_confirmation',
                    'mintsoft_client_id': instance.mintsoft_client_id,
                    'mintsoft_warehouse_id': instance.mintsoft_warehouse_id,
                })
        return True

    # -------------------------------------------------------------------------
    # Sales Order Kays Tab Buttons (Part D - Section 9)
    # -------------------------------------------------------------------------
    def _ensure_kays_instance(self):
        self.ensure_one()
        if not self.company_id.kays_enabled:
            raise UserError(_("Kays Logistics integration is disabled for company '%s'.") % self.company_id.display_name)
        inst = self.kays_instance_id or self.company_id._get_default_kays_instance(sale_order=self)
        if not inst:
            raise UserError(_("No active Kays connection found for company '%s'.") % self.company_id.display_name)
        if not self.kays_instance_id:
            self.kays_instance_id = inst.id
        return inst

    def action_kays_send_order(self):
        """Manual 'Send to Kays' button on sale.order."""
        self.ensure_one()
        inst = self._ensure_kays_instance()
        ko = OrderService(self.env).export_sale_order(self, instance=inst, is_manual=True)
        notif_type = 'success' if ko.export_status in ('sent', 'pending') and not ko.last_error else 'danger'
        msg = (
            _('Order exported to Mintsoft (Mintsoft Order ID: %s).') % ko.mintsoft_order_id
            if ko.export_status == 'sent'
            else (ko.last_error or _('Dry run validation completed.'))
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Kays Order Submission'),
                'message': msg,
                'type': notif_type,
                'sticky': notif_type == 'danger',
            },
        }

    def action_kays_validate_order(self):
        """Validate Order button on sale.order."""
        self.ensure_one()
        inst = self._ensure_kays_instance()
        svc = OrderService(self.env)
        val_res = svc.validate_sale_order(self, inst, is_manual=True)
        payload = svc.build_mintsoft_order_payload(self, inst, validation_result=val_res)
        preview_str = json.dumps(sanitize_payload(payload), indent=2)

        if self.kays_primary_order_id:
            self.kays_primary_order_id.write({
                'payload_preview': preview_str,
                'last_error': '\n'.join(val_res['errors']) if not val_res['valid'] else False,
            })

        msg = (
            _('All 14 order validation checks passed.')
            if val_res['valid']
            else '\n'.join(val_res['errors'])
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Order Validation'),
                'message': msg,
                'type': 'success' if val_res['valid'] else 'danger',
                'sticky': not val_res['valid'],
            },
        }

    def action_kays_preview_payload(self):
        """Preview Payload button on sale.order."""
        self.ensure_one()
        inst = self._ensure_kays_instance()
        svc = OrderService(self.env)
        val_res = svc.validate_sale_order(self, inst, is_manual=True)
        payload = svc.build_mintsoft_order_payload(self, inst, validation_result=val_res)
        preview_str = json.dumps(sanitize_payload(payload), indent=2)

        if not self.kays_primary_order_id:
            ext_ref = val_res['resolved']['external_reference']
            ko = self.env['kays.mintsoft.order'].create({
                'name': ext_ref,
                'external_order_reference': ext_ref,
                'idempotency_key': f"{inst.id}:{ext_ref}",
                'sale_order_id': self.id,
                'company_id': self.company_id.id,
                'instance_id': inst.id,
                'partner_id': self.partner_id.id,
                'shipping_partner_id': (self.partner_shipping_id or self.partner_id).id,
                'order_date': self.date_order or fields.Datetime.now(),
                'currency_id': self.currency_id.id,
                'order_total': self.amount_total,
                'export_status': 'pending',
                'payload_preview': preview_str,
            })
        else:
            ko = self.kays_primary_order_id
            ko.payload_preview = preview_str

        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays Order Payload Preview — %s') % ko.name,
            'res_model': 'kays.mintsoft.order',
            'res_id': ko.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_kays_check_status(self):
        """Check Mintsoft Status button on sale.order."""
        self.ensure_one()
        if not self.kays_order_ids:
            raise UserError(_("This order has no Kays Logistics record yet."))
        return self.kays_order_ids.action_check_mintsoft_status()

    def action_kays_refresh_tracking(self):
        """Refresh Tracking button on sale.order."""
        self.ensure_one()
        if not self.kays_order_ids:
            raise UserError(_("This order has no Kays Logistics record yet."))
        return self.kays_order_ids.action_refresh_tracking()

    def action_kays_view_sync_logs(self):
        """View Sync Logs button on sale.order."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays Sync Logs — %s') % self.name,
            'res_model': 'kays.mintsoft.log',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
        }

    def action_kays_retry_export(self):
        """Retry Export button on sale.order."""
        self.ensure_one()
        if not self.kays_order_ids:
            return self.action_kays_send_order()
        return self.kays_order_ids.action_retry_export()

    def action_kays_request_cancellation(self):
        """Request Cancellation button on sale.order."""
        self.ensure_one()
        if not self.kays_primary_order_id:
            raise UserError(_("Order has not been exported to Kays Logistics."))
        return self.kays_primary_order_id.action_request_cancellation()

    def action_kays_view_mintsoft_order(self):
        """View Mintsoft Order button on sale.order."""
        self.ensure_one()
        if not self.kays_order_ids:
            raise UserError(_("No Kays Order record exists for this Sales Order yet."))
        if len(self.kays_order_ids) == 1:
            return {
                'type': 'ir.actions.act_window',
                'name': _('Kays Order — %s') % self.kays_order_ids[0].name,
                'res_model': 'kays.mintsoft.order',
                'res_id': self.kays_order_ids[0].id,
                'view_mode': 'form',
                'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _('Kays Orders — %s') % self.name,
            'res_model': 'kays.mintsoft.order',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
        }

    def _create_subscription_renewal_picking(self, cycle_num=None):
        """
        Create a new outgoing stock.picking and stock.moves for this subscription sale.order,
        representing a recurring delivery cycle (e.g. bi-weekly coffee replenishment).
        """
        self.ensure_one()
        warehouse = self.warehouse_id
        if not warehouse:
            warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.company_id.id)], limit=1)
        if not warehouse:
            raise UserError(_("No warehouse found for company '%s'.") % self.company_id.display_name)

        picking_type = warehouse.out_type_id
        if not picking_type:
            raise UserError(_("No outgoing delivery picking type found for warehouse '%s'.") % warehouse.display_name)

        customer_loc = (self.partner_shipping_id or self.partner_id).property_stock_customer
        if not customer_loc:
            customer_loc = self.env.ref('stock.stock_location_customers', raise_if_not_found=False)
        source_loc = picking_type.default_location_src_id or warehouse.lot_stock_id

        # Compute cycle number if not provided
        existing_outgoing = self.picking_ids.filtered(
            lambda p: p.picking_type_id.code == 'outgoing' and p.state != 'cancel'
        )
        if not cycle_num:
            cycle_num = len(existing_outgoing) + 1

        carrier = self.carrier_id or (existing_outgoing[:1].carrier_id if existing_outgoing else False)

        picking_vals = {
            'picking_type_id': picking_type.id,
            'partner_id': (self.partner_shipping_id or self.partner_id).id,
            'sale_id': self.id,
            'origin': f"{self.name} (Renewal #{cycle_num})",
            'company_id': self.company_id.id,
            'location_id': source_loc.id,
            'location_dest_id': customer_loc.id if customer_loc else picking_type.default_location_dest_id.id,
            'carrier_id': carrier.id if carrier else False,
        }
        if hasattr(self, 'procurement_group_id') and self.procurement_group_id:
            picking_vals['group_id'] = self.procurement_group_id.id

        picking = self.env['stock.picking'].create(picking_vals)

        # Create stock.move for each physical product line on the subscription
        physical_lines = self.order_line.filtered(
            lambda l: not l.display_type
            and not getattr(l, 'is_delivery', False)
            and l.product_id
            and l.product_id.type != 'service'
            and l.product_uom_qty > 0
        )
        if not physical_lines:
            raise UserError(_("Subscription '%s' has no physical product lines to deliver.") % self.name)

        for line in physical_lines:
            line_uom = getattr(line, 'product_uom_id', None) or getattr(line, 'product_uom', None) or line.product_id.uom_id
            move_vals = {
                'name': line.name or line.product_id.name,
                'product_id': line.product_id.id,
                'product_uom_qty': line.product_uom_qty,
                'product_uom': line_uom.id,
                'picking_id': picking.id,
                'sale_line_id': line.id,
                'company_id': self.company_id.id,
                'location_id': source_loc.id,
                'location_dest_id': customer_loc.id if customer_loc else picking_type.default_location_dest_id.id,
                'warehouse_id': warehouse.id,
                'origin': f"{self.name} - R{cycle_num}",
                'description_picking': line.name or line.product_id.name,
            }
            if hasattr(self, 'procurement_group_id') and self.procurement_group_id:
                move_vals['group_id'] = self.procurement_group_id.id
            self.env['stock.move'].create(move_vals)

        picking.action_confirm()
        picking.action_assign()
        return picking

    def action_kays_create_renewal_delivery(self):
        """Manually generate the next recurring subscription delivery picking and export it to Kays."""
        self.ensure_one()
        instance = self.kays_instance_id or self.company_id._get_default_kays_instance(sale_order=self)
        if not instance:
            raise UserError(_("No Kays connection configured for this company."))

        existing_pickings = self.picking_ids.filtered(
            lambda p: p.picking_type_id.code == 'outgoing' and p.state != 'cancel'
        )
        paid_invoices = self.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_invoice'
            and inv.state == 'posted'
            and inv.payment_state in ('in_payment', 'paid')
        )
        missing_count = max(len(paid_invoices) - len(existing_pickings), 1)

        created_pickings = self.env['stock.picking']
        svc = OrderService(self.env)
        for _ in range(missing_count):
            picking = self._create_subscription_renewal_picking()
            created_pickings |= picking
            try:
                svc.export_subscription_delivery(picking, instance=instance, is_manual=True)
            except Exception as exc:
                _logger.warning("Manual renewal export failed for picking %s: %s", picking.name, exc)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Renewal Delivery Created'),
                'message': _('Created %d delivery order(s) for subscription %s and sent to Kays.') % (len(created_pickings), self.name),
                'type': 'success',
            }
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    kays_order_line_ids = fields.One2many(
        'kays.mintsoft.order.line',
        'sale_line_id',
        string='Kays Fulfilment Lines',
        copy=False,
    )
