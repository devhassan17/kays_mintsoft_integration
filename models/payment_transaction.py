import logging
from odoo import models
from ..services.order_service import OrderService

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _set_done(self, **kwargs):
        not_done_ids = set(self.filtered(lambda t: t.state != 'done').ids)
        super()._set_done(**kwargs)
        newly_done = self.filtered(lambda t: t.id in not_done_ids and t.state == 'done')
        if newly_done:
            newly_done._kays_trigger_post_payment_export()

    def write(self, vals):
        not_done_ids = set(self.filtered(lambda t: t.state != 'done').ids) if vals.get('state') == 'done' else set()
        res = super().write(vals)
        if vals.get('state') == 'done' and not_done_ids:
            newly_done = self.filtered(lambda t: t.id in not_done_ids and t.state == 'done')
            if newly_done:
                newly_done._kays_trigger_post_payment_export()
        return res

    def _kays_trigger_post_payment_export(self):
        """
        When a payment transaction succeeds (e.g. subscription renewal charge or customer invoice payment),
        find all associated Sales Orders and automatically export:
        1. Any subscription renewal deliveries (stock.picking) waiting for payment.
        2. Any confirmed Sales Orders that were pending payment.
        """
        if self.env.context.get('kays_in_post_payment_export'):
            return
        self = self.with_context(kays_in_post_payment_export=True, kays_skip_picking_auto_export=True)

        for tx in self.filtered(lambda t: t.state == 'done'):
            orders = tx.sale_order_ids
            if not orders and tx.invoice_ids:
                invoices = tx.invoice_ids.filtered(lambda inv: inv.move_type == 'out_invoice')
                orders = invoices.line_ids.sale_line_ids.order_id
            if not orders:
                continue

            for order in orders.filtered(lambda o: o.state == 'sale'):
                if not order.company_id.kays_enabled:
                    continue
                try:
                    instance = order.kays_instance_id or order.company_id._get_default_kays_instance(sale_order=order)
                except Exception:
                    continue
                if not instance or not instance.active or not instance.enable_integration:
                    continue

                svc = OrderService(self.env)

                # 1. Export any subscription renewal delivery pickings
                if instance.enable_subscription_export and order.is_subscription_order:
                    _existing_cycles, target_cycles, missing_count = order._get_subscription_cycle_counts()
                    # Only auto-create renewal pickings on cycle 2+ (cycle 1 is the initial order)
                    if target_cycles >= 2 and missing_count > 0:
                        for _ in range(missing_count):
                            try:
                                with self.env.cr.savepoint():
                                    new_picking = order._create_subscription_renewal_picking()
                                    svc.export_subscription_delivery(new_picking, instance=instance, is_manual=False)
                            except Exception as exc:
                                _logger.warning("Auto-create renewal delivery failed for %s on payment: %s", order.name, exc)

                    pickings = order.picking_ids.filtered(
                        lambda p: p.picking_type_id.code == 'outgoing'
                        and p.state != 'cancel'
                        and ('Renewal #' in (p.origin or '') or not order.kays_order_ids.filtered(lambda o: not o.is_subscription_delivery and o.export_status == 'sent'))
                        and (not p.kays_order_id or p.kays_order_id.export_status != 'sent')
                    )
                    for picking in pickings:
                        try:
                            with self.env.cr.savepoint():
                                svc.export_subscription_delivery(picking, instance=instance, is_manual=False)
                        except Exception as exc:
                            _logger.warning(
                                "Payment transaction subscription export failed for picking %s (Order %s): %s",
                                picking.name, order.name, exc
                            )
                elif instance.enable_subscription_export:
                    pickings = order.picking_ids.filtered(
                        lambda p: p.picking_type_id.code == 'outgoing'
                        and p.state not in ('draft', 'cancel')
                        and (not p.kays_order_id or p.kays_order_id.export_status != 'sent')
                    )
                    for picking in pickings:
                        try:
                            with self.env.cr.savepoint():
                                svc.export_subscription_delivery(picking, instance=instance, is_manual=False)
                        except Exception as exc:
                            _logger.warning(
                                "Payment transaction subscription export failed for picking %s (Order %s): %s",
                                picking.name, order.name, exc
                            )

                # 2. Export pending primary/split Kays orders
                if instance.enable_auto_order_export:
                    pending_kos = order.kays_order_ids.filtered(lambda ko: ko.export_status == 'pending')
                    if pending_kos:
                        for ko in pending_kos:
                            try:
                                with self.env.cr.savepoint():
                                    if ko.picking_id:
                                        svc.export_subscription_delivery(ko.picking_id, instance=instance, is_manual=False)
                                    else:
                                        svc.export_sale_order(order, instance=instance, is_manual=False)
                            except Exception as exc:
                                _logger.warning("Payment transaction order export failed for order %s: %s", order.name, exc)
                    elif order.state == 'sale' and not order.kays_order_ids:
                        try:
                            with self.env.cr.savepoint():
                                svc.export_sale_order(order, instance=instance, is_manual=False)
                        except Exception as exc:
                            _logger.warning("Payment transaction auto-export failed for %s: %s", order.name, exc)

