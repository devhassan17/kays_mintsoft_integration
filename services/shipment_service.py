import logging
from odoo import fields, _

from .mintsoft_client import MintsoftClient, MintsoftAPIError

_logger = logging.getLogger(__name__)


class ShipmentService:
    """Domain service for Mintsoft OrderShipments and Odoo stock.picking dispatch synchronization."""

    def __init__(self, env):
        self.env = env

    def sync_shipments_for_order(self, kays_order, order_payload=None):
        """
        Retrieve OrderShipments for a kays.mintsoft.order (GET /api/Order/{id}/Shipments),
        create/update kays.mintsoft.shipment records, and synchronize Odoo stock.picking safely.
        """
        kays_order.ensure_one()
        instance = kays_order.instance_id
        if not instance.active or not instance.enable_integration or not instance.enable_shipment_sync:
            return False
        if not kays_order.mintsoft_order_id or kays_order.mintsoft_order_id <= 0:
            return False

        client = MintsoftClient(instance)
        shipments_data = []
        try:
            res = client.request(
                'GET',
                f'/api/Order/{kays_order.mintsoft_order_id}/Shipments',
                operation='get_order_shipments',
                sale_order=kays_order.sale_order_id,
                kays_order=kays_order,
                mintsoft_order_id=kays_order.mintsoft_order_id,
                allow_404=True,
            )
            if isinstance(res, list):
                shipments_data = res
        except MintsoftAPIError as exc:
            _logger.info("GET /api/Order/%s/Shipments returned: %s", kays_order.mintsoft_order_id, exc.message)

        if not order_payload:
            try:
                order_payload = client.request(
                    'GET',
                    f'/api/Order/{kays_order.mintsoft_order_id}',
                    operation='get_order_for_shipment',
                    sale_order=kays_order.sale_order_id,
                    kays_order=kays_order,
                    mintsoft_order_id=kays_order.mintsoft_order_id,
                    allow_404=True,
                )
            except MintsoftAPIError:
                order_payload = {}

        order_payload = order_payload if isinstance(order_payload, dict) else {}
        Shipment = self.env['kays.mintsoft.shipment']
        synced_shipments = Shipment

        # 1. Process explicit OrderShipment records from /api/Order/{id}/Shipments
        for sh in shipments_data:
            if not isinstance(sh, dict):
                continue
            ms_ship_id = int(sh.get('ID') or 0)
            tracking_no = (
                sh.get('Number')
                or (sh.get('ParcelNumbers')[0] if isinstance(sh.get('ParcelNumbers'), list) and sh.get('ParcelNumbers') else '')
                or order_payload.get('TrackingNumber')
                or kays_order.tracking_number
                or ''
            ).strip()
            parcel_nums = ', '.join(str(p) for p in (sh.get('ParcelNumbers') or []) if p)

            existing = Shipment.search([
                ('instance_id', '=', instance.id),
                '|',
                '&', ('mintsoft_shipment_id', '>', 0), ('mintsoft_shipment_id', '=', ms_ship_id),
                '&', ('kays_order_id', '=', kays_order.id), ('tracking_number', '=', tracking_no or f'MS-SH-{ms_ship_id}'),
            ], limit=1)

            ship_date = self._parse_dt(sh.get('ShipmentDate') or order_payload.get('DespatchDate')) or kays_order.despatch_date or fields.Datetime.now()
            deliv_status = self._determine_delivery_status(kays_order, tracking_no)

            vals = {
                'name': tracking_no or f"SHIP-{kays_order.name}-{ms_ship_id or 1}",
                'mintsoft_shipment_id': ms_ship_id,
                'kays_order_id': kays_order.id,
                'sale_order_id': kays_order.sale_order_id.id,
                'company_id': kays_order.company_id.id,
                'instance_id': instance.id,
                'courier_type': sh.get('CourierType') or order_payload.get('CourierServiceName') or kays_order.mintsoft_courier_service_name or '',
                'courier_service_id': int(order_payload.get('CourierServiceId') or kays_order.mintsoft_courier_service_id or 0),
                'courier_service_name': order_payload.get('CourierServiceName') or kays_order.mintsoft_courier_service_name or sh.get('CourierType') or '',
                'tracking_number': tracking_no,
                'tracking_url': order_payload.get('TrackingURL') or kays_order.tracking_url or '',
                'label_url': sh.get('LabelURL') or '',
                'parcel_numbers': parcel_nums,
                'manifested': bool(sh.get('Manifested')),
                'shipment_date': ship_date,
                'delivery_status': deliv_status,
                'last_tracking_update': fields.Datetime.now(),
            }
            if existing:
                existing.write(vals)
                synced_shipments |= existing
            else:
                synced_shipments |= Shipment.create(vals)

        # 2. If /Shipments returned empty but order itself is Despatched or has TrackingNumber, create/update header shipment
        header_tracking = (order_payload.get('TrackingNumber') or kays_order.tracking_number or '').strip()
        is_dispatched_status = kays_order.fulfilment_status in ('dispatched', 'partially_dispatched', 'delivered')
        if not synced_shipments and (header_tracking or is_dispatched_status):
            existing_hdr = Shipment.search([
                ('kays_order_id', '=', kays_order.id),
            ], limit=1)
            ship_date = self._parse_dt(order_payload.get('DespatchDate')) or kays_order.despatch_date or fields.Datetime.now()
            deliv_status = self._determine_delivery_status(kays_order, header_tracking)
            vals = {
                'name': header_tracking or f"SHIP-{kays_order.name}",
                'kays_order_id': kays_order.id,
                'sale_order_id': kays_order.sale_order_id.id,
                'company_id': kays_order.company_id.id,
                'instance_id': instance.id,
                'courier_type': order_payload.get('CourierServiceName') or kays_order.mintsoft_courier_service_name or '',
                'courier_service_id': int(order_payload.get('CourierServiceId') or kays_order.mintsoft_courier_service_id or 0),
                'courier_service_name': order_payload.get('CourierServiceName') or kays_order.mintsoft_courier_service_name or '',
                'tracking_number': header_tracking,
                'tracking_url': order_payload.get('TrackingURL') or kays_order.tracking_url or '',
                'shipment_date': ship_date,
                'delivery_status': deliv_status,
                'last_tracking_update': fields.Datetime.now(),
            }
            if existing_hdr:
                existing_hdr.write(vals)
                synced_shipments |= existing_hdr
            else:
                synced_shipments |= Shipment.create(vals)

        # Update tracking summary on kays_order
        all_trackings = [s.tracking_number for s in kays_order.shipment_ids if s.tracking_number]
        if all_trackings:
            kays_order.tracking_number = ', '.join(dict.fromkeys(all_trackings))

        # 3. Link and synchronize Odoo stock.picking safely
        self._sync_odoo_pickings(kays_order, synced_shipments, order_payload)
        return synced_shipments

    def _determine_delivery_status(self, kays_order, tracking_no):
        st = kays_order.fulfilment_status
        if st == 'delivered':
            return 'delivered'
        if st == 'cancelled':
            return 'cancelled'
        if st == 'partially_dispatched':
            return 'partial_dispatch'
        if st == 'dispatched':
            return 'tracking_available' if tracking_no else 'dispatched'
        if tracking_no:
            return 'tracking_available'
        if st in ('processing', 'picked', 'packed'):
            return 'processing'
        return 'awaiting_dispatch'

    def _sync_odoo_pickings(self, kays_order, shipments, order_payload):
        """
        Safely update Odoo stock.picking records:
        - NEVER validates a picking on intermediate statuses (received, processing, picked, packed).
        - Validates outgoing pickings ONLY when verified dispatch evidence exists.
        - Supports partial shipments (splitting backorders via native Odoo stock workflow)
          and already-completed pickings (updating tracking metadata only).
        """
        sale_order = kays_order.sale_order_id
        if not sale_order:
            return

        outgoing_pickings = sale_order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state != 'cancel'
        )
        if not outgoing_pickings:
            return

        all_tracking_nums = [s.tracking_number for s in shipments if s.tracking_number]
        if not all_tracking_nums and kays_order.tracking_number:
            all_tracking_nums = [kays_order.tracking_number]
        combined_tracking = ', '.join(dict.fromkeys(all_tracking_nums))
        primary_shipment = shipments[:1]
        tracking_url = (primary_shipment.tracking_url if primary_shipment else '') or kays_order.tracking_url or ''
        courier_name = (primary_shipment.courier_service_name if primary_shipment else '') or kays_order.mintsoft_courier_service_name or ''
        dispatch_dt = (primary_shipment.shipment_date if primary_shipment else False) or kays_order.despatch_date
        deliv_status = (primary_shipment.delivery_status if primary_shipment else False) or self._determine_delivery_status(kays_order, combined_tracking)

        open_pickings = outgoing_pickings.filtered(lambda p: p.state not in ('done', 'cancel'))
        done_pickings = outgoing_pickings.filtered(lambda p: p.state == 'done')

        target_picking = kays_order.picking_id or open_pickings[:1] or done_pickings[:1]
        if not target_picking:
            return

        if primary_shipment:
            shipments.write({'picking_id': target_picking.id})

        picking_meta = {
            'kays_order_id': kays_order.id,
            'mintsoft_shipment_id': primary_shipment.mintsoft_shipment_id if primary_shipment else 0,
            'kays_tracking_number': combined_tracking,
            'kays_tracking_url': tracking_url,
            'kays_courier_name': courier_name,
            'kays_dispatch_date': dispatch_dt,
            'kays_delivery_status': deliv_status,
            'kays_last_tracking_update': fields.Datetime.now(),
        }
        if combined_tracking:
            picking_meta['carrier_tracking_ref'] = combined_tracking

        target_picking.write(picking_meta)

        # CRITICAL RULE: Never validate an Odoo picking unless verified dispatch has occurred!
        is_verified_dispatch = kays_order.fulfilment_status in ('dispatched', 'partially_dispatched', 'delivered')
        if not is_verified_dispatch or target_picking.state == 'done':
            return

        if kays_order.instance_id.environment == 'dry_run':
            return

        # Determine shipped quantities per product from Mintsoft OrderItems or kays_order.line_ids
        shipped_qty_by_product = {}
        for line in kays_order.line_ids:
            if not line.product_id:
                continue
            mapping = line.product_mapping_id
            uom_factor = mapping.mintsoft_uom_factor if (mapping and mapping.mintsoft_uom_factor > 0) else 1.0
            # If Mintsoft reported committed/allocated qty > 0, use that; otherwise full line quantity on full dispatch
            if kays_order.fulfilment_status == 'partially_dispatched':
                ms_done = line.committed_qty or line.allocated_qty or 0
            else:
                ms_done = max(0, line.quantity - line.backorder_qty) if line.backorder_qty else line.quantity
            odoo_qty = float(ms_done) / uom_factor
            shipped_qty_by_product[line.product_id.id] = shipped_qty_by_product.get(line.product_id.id, 0.0) + odoo_qty

        # Prepare picking moves with verified quantities
        ctx = dict(
            self.env.context,
            skip_backorder=True,
            picking_ids_not_to_backorder=[] if kays_order.fulfilment_status == 'partially_dispatched' else [target_picking.id],
            mail_notrack=not kays_order.instance_id.enable_customer_notification,
            tracking_disable=not kays_order.instance_id.enable_customer_notification,
            skip_sms=not kays_order.instance_id.enable_customer_notification,
        )
        picking_sudo = target_picking.with_company(kays_order.company_id).with_context(ctx)

        if picking_sudo.state == 'draft':
            picking_sudo.action_confirm()
        if picking_sudo.state in ('confirmed', 'waiting'):
            picking_sudo.action_assign()

        any_move_picked = False
        for move in picking_sudo.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            prod_id = move.product_id.id
            demand = move.product_uom_qty
            if prod_id in shipped_qty_by_product:
                qty_to_ship = min(demand, shipped_qty_by_product[prod_id])
                shipped_qty_by_product[prod_id] = max(0.0, shipped_qty_by_product[prod_id] - qty_to_ship)
            elif kays_order.fulfilment_status in ('dispatched', 'delivered') and not kays_order.is_split_order:
                qty_to_ship = demand
            else:
                qty_to_ship = 0.0

            if qty_to_ship > 0:
                move.quantity = qty_to_ship
                if hasattr(move, 'picked'):
                    move.picked = True
                any_move_picked = True
            else:
                move.quantity = 0.0
                if hasattr(move, 'picked'):
                    move.picked = False

        if any_move_picked:
            try:
                res = picking_sudo.button_validate()
                # If button_validate returns a backorder wizard action dict, process backorder automatically
                if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                    wiz = self.env['stock.backorder.confirmation'].with_context(
                        res.get('context') or ctx
                    ).browse(res.get('res_id'))
                    if wiz.exists():
                        wiz.process()
            except Exception as val_exc:
                _logger.warning("Could not auto-validate picking %s: %s", target_picking.name, val_exc)

    @staticmethod
    def _parse_dt(dt_str):
        if not dt_str or not isinstance(dt_str, str):
            return False
        cleaned = dt_str.strip().replace('T', ' ').split('.')[0].split('+')[0].rstrip('Z')
        try:
            return fields.Datetime.to_datetime(cleaned[:19])
        except Exception:
            return False
