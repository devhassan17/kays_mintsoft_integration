import logging
from odoo import fields, _
from odoo.exceptions import UserError

from .mintsoft_client import MintsoftClient, MintsoftAPIError

_logger = logging.getLogger(__name__)


class InventoryService:
    """Domain service for Mintsoft <-> Odoo Stock Synchronization and Reconciliation."""

    def __init__(self, env):
        self.env = env

    def run_stock_sync(self, instance, sync_mode='full', warehouse_mapping=None, is_manual=False):
        """
        Execute a Full Reconciliation or Incremental Stock Synchronization for a connection.
        Creates a kays.mintsoft.stock.snapshot and per-SKU kays.mintsoft.stock.discrepancy records.
        """
        instance.ensure_one()
        if not instance.company_id.kays_enabled:
            raise UserError(_("Kays Logistics integration is disabled for company '%s'.") % instance.company_id.display_name)
        if not instance.active or not instance.enable_integration:
            raise UserError(_("Integration is disabled on connection '%s'.") % instance.display_name)
        if not instance.enable_inventory_sync:
            raise UserError(_("Inventory Sync is disabled on connection '%s'.") % instance.display_name)
        if instance.inventory_authority == 'disabled':
            raise UserError(_("Inventory Authority is set to Disabled on connection '%s'.") % instance.display_name)

        # Resolve target warehouse mapping(s)
        if warehouse_mapping:
            wh_mappings = warehouse_mapping
        else:
            wh_mappings = self.env['kays.mintsoft.warehouse.mapping'].search([
                ('instance_id', '=', instance.id),
                ('active', '=', True),
            ])

        client = MintsoftClient(instance)
        is_dry_run = bool(instance.environment == 'dry_run' or instance.dry_run_stock_adjustments)
        authority = instance.inventory_authority or 'mintsoft'

        snapshot = self.env['kays.mintsoft.stock.snapshot'].create({
            'name': self.env['ir.sequence'].next_by_code('kays.mintsoft.stock.snapshot') or f"SNAP-{fields.Datetime.now()}",
            'company_id': instance.company_id.id,
            'instance_id': instance.id,
            'sync_mode': sync_mode,
            'authority_mode': authority,
            'dry_run': is_dry_run,
            'state': 'running',
            'started_at': fields.Datetime.now(),
        })

        # Determine target warehouse IDs and corresponding Odoo locations
        targets = []
        if wh_mappings:
            for wm in wh_mappings:
                targets.append({
                    'warehouse_mapping': wm,
                    'mintsoft_warehouse_id': wm.mintsoft_warehouse_id,
                    'location': wm.odoo_location_id,
                })
        else:
            default_loc = instance.default_location_id or (
                instance.default_warehouse_id.lot_stock_id if instance.default_warehouse_id else False
            )
            if not default_loc:
                wh = self.env['stock.warehouse'].with_company(instance.company_id).search([
                    ('company_id', '=', instance.company_id.id)
                ], limit=1)
                default_loc = wh.lot_stock_id if wh else False
            if not default_loc:
                snapshot.write({
                    'state': 'failed',
                    'notes': _("No mapped or default Odoo Stock Location found for company '%s'.") % instance.company_id.display_name,
                    'completed_at': fields.Datetime.now(),
                })
                raise UserError(_("Configure a Default Odoo Stock Location or Warehouse Mapping before syncing stock."))
            targets.append({
                'warehouse_mapping': self.env['kays.mintsoft.warehouse.mapping'],
                'mintsoft_warehouse_id': instance.mintsoft_warehouse_id or 3,
                'location': default_loc,
            })

        # Optional incremental filter using /api/Product/StockLevels/UpdatedSince
        updated_product_ids = None
        if sync_mode == 'incremental' and instance.last_stock_sync_at:
            from_date_str = instance.last_stock_sync_at.strftime('%Y-%m-%dT%H:%M:%SZ')
            try:
                upd_res = client.request(
                    'GET',
                    '/api/Product/StockLevels/UpdatedSince',
                    params={'FromDate': from_date_str},
                    operation='stock_levels_updated_since',
                )
                if isinstance(upd_res, list):
                    updated_product_ids = {int(pid) for pid in upd_res if str(pid).isdigit()}
            except MintsoftAPIError as exc:
                _logger.info("UpdatedSince fallback to full StockLevels: %s", exc.message)

        total_checked = 0
        matched_count = 0
        discrepancy_count = 0
        adjusted_count = 0
        skipped_count = 0
        error_count = 0
        large_discrepancies = []

        for target in targets:
            ms_wh_id = target['mintsoft_warehouse_id']
            location = target['location']
            wm_rec = target['warehouse_mapping']

            params = {'WarehouseId': ms_wh_id, 'Breakdown': False}
            if instance.mintsoft_client_id and instance.mintsoft_client_id > 0:
                params['ClientId'] = instance.mintsoft_client_id

            stock_rows = client.request(
                'GET',
                '/api/Product/StockLevels',
                params=params,
                operation='get_stock_levels',
            )
            if not isinstance(stock_rows, list):
                continue

            for row in stock_rows:
                if not isinstance(row, dict):
                    continue
                ms_prod_id = int(row.get('ProductId') or 0)
                if updated_product_ids is not None and ms_prod_id not in updated_product_ids:
                    continue

                sku = (row.get('SKU') or '').strip()
                if not sku:
                    continue

                # Skip Mintsoft virtual bundles if configured
                if row.get('Bundle'):
                    skipped_count += 1
                    continue

                total_checked += 1
                res_info = self._process_single_stock_row(
                    instance=instance,
                    snapshot=snapshot,
                    warehouse_mapping=wm_rec,
                    location=location,
                    ms_wh_id=ms_wh_id,
                    row=row,
                    authority=authority,
                    is_dry_run=is_dry_run,
                )
                st = res_info['outcome']
                if st == 'matched':
                    matched_count += 1
                elif st == 'adjusted':
                    discrepancy_count += 1
                    adjusted_count += 1
                elif st in ('review', 'pending_approval'):
                    discrepancy_count += 1
                elif st == 'skipped':
                    skipped_count += 1
                elif st == 'error':
                    error_count += 1

                if res_info.get('is_large_discrepancy') and res_info.get('discrepancy'):
                    large_discrepancies.append(res_info['discrepancy'])

            if wm_rec:
                wm_rec.last_sync_at = fields.Datetime.now()

        now = fields.Datetime.now()
        snapshot.write({
            'total_skus_checked': total_checked,
            'matched_count': matched_count,
            'discrepancy_count': discrepancy_count,
            'adjusted_count': adjusted_count,
            'skipped_count': skipped_count,
            'error_count': error_count,
            'state': 'completed',
            'completed_at': now,
        })
        instance.sudo().write({
            'last_stock_sync_at': now,
        })

        if large_discrepancies and instance.notify_large_discrepancies:
            self._notify_large_discrepancies(instance, snapshot, large_discrepancies)

        return snapshot

    def _process_single_stock_row(
        self,
        instance,
        snapshot,
        warehouse_mapping,
        location,
        ms_wh_id,
        row,
        authority,
        is_dry_run,
    ):
        sku = (row.get('SKU') or '').strip()
        ms_prod_id = int(row.get('ProductId') or 0)
        ms_level = float(row.get('Level') or 0.0)
        ms_total = float(row.get('TotalStockLevel') if row.get('TotalStockLevel') is not None else ms_level)
        ms_allocated = max(0.0, ms_total - ms_level)

        Mapping = self.env['kays.mintsoft.product.mapping']
        mapping = Mapping.search([
            ('instance_id', '=', instance.id),
            '|',
            ('mintsoft_sku', '=', sku),
            ('mintsoft_product_id', '=', ms_prod_id),
        ], limit=1)

        if not mapping:
            # Try auto-matching against company-scoped product.product by default_code
            Product = self.env['product.product'].with_company(instance.company_id)
            prods = Product.search([
                ('default_code', '=', sku),
                ('company_id', 'in', [False, instance.company_id.id]),
                ('active', '=', True),
            ])
            if len(prods) == 1:
                mapping = Mapping.create({
                    'instance_id': instance.id,
                    'company_id': instance.company_id.id,
                    'product_id': prods[0].id,
                    'odoo_sku': prods[0].default_code,
                    'mintsoft_product_id': ms_prod_id,
                    'mintsoft_sku': sku,
                    'mintsoft_name': sku,
                    'mapping_status': 'matched',
                    'last_sync_at': fields.Datetime.now(),
                })
            elif len(prods) > 1:
                mapping = Mapping.create({
                    'instance_id': instance.id,
                    'company_id': instance.company_id.id,
                    'product_id': prods[0].id,
                    'odoo_sku': sku,
                    'mintsoft_product_id': ms_prod_id,
                    'mintsoft_sku': sku,
                    'mintsoft_name': sku,
                    'mapping_status': 'duplicate_sku',
                    'last_error': _("Duplicate Odoo products with SKU '%s'.") % sku,
                    'last_sync_at': fields.Datetime.now(),
                })
            else:
                mapping = Mapping.create({
                    'instance_id': instance.id,
                    'company_id': instance.company_id.id,
                    'mintsoft_product_id': ms_prod_id,
                    'mintsoft_sku': sku,
                    'mintsoft_name': sku,
                    'mapping_status': 'missing_in_odoo',
                    'last_error': _("SKU '%s' not found in Odoo.") % sku,
                    'last_sync_at': fields.Datetime.now(),
                })

        # Safeguard: skip products with mapping errors if configured
        if mapping.mapping_status != 'matched' or not mapping.product_id:
            if instance.skip_mapping_error_products:
                disc = self.env['kays.mintsoft.stock.discrepancy'].create({
                    'snapshot_id': snapshot.id,
                    'company_id': instance.company_id.id,
                    'instance_id': instance.id,
                    'warehouse_mapping_id': warehouse_mapping.id if warehouse_mapping else False,
                    'product_mapping_id': mapping.id,
                    'sku': sku,
                    'mintsoft_product_id': ms_prod_id,
                    'location_id': location.id,
                    'mintsoft_on_hand_qty': ms_total,
                    'mintsoft_allocated_qty': ms_allocated,
                    'mintsoft_available_qty': ms_level,
                    'status': 'skipped',
                    'reason': _("Skipped: Mapping status is '%s'.") % mapping.mapping_status,
                })
                return {'outcome': 'skipped', 'discrepancy': disc}
            return {'outcome': 'error', 'discrepancy': None}

        product = mapping.product_id.with_company(instance.company_id)

        # Safeguard: Sync selected products only
        if instance.sync_selected_products_only and not mapping.sync_stock_enabled:
            return {'outcome': 'skipped', 'discrepancy': None}

        # Safeguard: Exclude specified product categories
        if instance.excluded_product_category_ids and product.categ_id in instance.excluded_product_category_ids:
            return {'outcome': 'skipped', 'discrepancy': None}

        # Skip non-storable products
        is_storable = getattr(product, 'is_storable', product.type == 'product')
        if not is_storable:
            return {'outcome': 'skipped', 'discrepancy': None}

        # Convert Mintsoft quantities to Odoo product base UoM using mintsoft_uom_factor
        uom_factor = mapping.mintsoft_uom_factor if (mapping.mintsoft_uom_factor and mapping.mintsoft_uom_factor > 0) else 1.0
        ms_avail_odoo_uom = ms_level / uom_factor
        ms_on_hand_odoo_uom = ms_total / uom_factor
        ms_alloc_odoo_uom = ms_allocated / uom_factor

        # Compute current Odoo stock strictly inside the mapped location (and its children)
        quants = self.env['stock.quant'].with_company(instance.company_id).search([
            ('product_id', '=', product.id),
            ('location_id', 'child_of', location.id),
        ])
        odoo_on_hand = sum(quants.mapped('quantity'))
        odoo_reserved = sum(quants.mapped('reserved_quantity'))
        odoo_available = odoo_on_hand - odoo_reserved

        diff_qty = round(ms_avail_odoo_uom - odoo_available, 4)
        mapping.write({
            'last_mintsoft_stock': ms_avail_odoo_uom,
            'last_odoo_stock': odoo_available,
            'last_sync_at': fields.Datetime.now(),
        })

        if abs(diff_qty) < 1e-4:
            return {'outcome': 'matched', 'discrepancy': None}

        is_large = bool(
            instance.large_discrepancy_threshold > 0
            and abs(diff_qty) >= instance.large_discrepancy_threshold
        )

        # Determine status & whether to apply inventory adjustment
        status = 'review'
        reason = _("Stock discrepancy detected between Odoo and Mintsoft.")
        should_apply = False

        if authority == 'monitoring_only':
            status = 'review'
            reason = _("Monitoring Only mode: stock difference logged without modifying Odoo inventory.")
        elif is_dry_run:
            status = 'review'
            reason = _("Dry Run mode: stock adjustment simulated without modifying Odoo inventory.")
        elif not instance.allow_zero_stock_updates and ms_avail_odoo_uom <= 0:
            status = 'skipped'
            reason = _("Zero-stock updates are disabled by connection safeguard settings.")
        elif (
            instance.max_stock_adjustment_threshold > 0
            and abs(diff_qty) > instance.max_stock_adjustment_threshold
        ):
            status = 'pending_approval' if instance.require_manual_approval_unusual else 'review'
            reason = _(
                "Adjustment of %(diff)s exceeds maximum threshold (%(max)s). Manual approval required."
            ) % {'diff': diff_qty, 'max': instance.max_stock_adjustment_threshold}
        else:
            should_apply = True

        disc = self.env['kays.mintsoft.stock.discrepancy'].create({
            'snapshot_id': snapshot.id,
            'company_id': instance.company_id.id,
            'instance_id': instance.id,
            'warehouse_mapping_id': warehouse_mapping.id if warehouse_mapping else False,
            'product_mapping_id': mapping.id,
            'product_id': product.id,
            'sku': sku,
            'mintsoft_product_id': ms_prod_id,
            'location_id': location.id,
            'odoo_on_hand_qty': odoo_on_hand,
            'odoo_reserved_qty': odoo_reserved,
            'odoo_available_qty': odoo_available,
            'mintsoft_on_hand_qty': ms_on_hand_odoo_uom,
            'mintsoft_allocated_qty': ms_alloc_odoo_uom,
            'mintsoft_available_qty': ms_avail_odoo_uom,
            'difference_qty': diff_qty,
            'status': status,
            'reason': reason,
        })

        if should_apply:
            try:
                self.apply_discrepancy_adjustment(disc)
                return {'outcome': 'adjusted', 'discrepancy': disc, 'is_large_discrepancy': is_large}
            except Exception as adj_err:
                disc.write({
                    'status': 'error',
                    'reason': _("Inventory adjustment error: %s") % adj_err,
                })
                return {'outcome': 'error', 'discrepancy': disc, 'is_large_discrepancy': is_large}

        return {'outcome': status, 'discrepancy': disc, 'is_large_discrepancy': is_large}

    def apply_discrepancy_adjustment(self, discrepancy):
        """
        Apply a single stock discrepancy to the exact mapped Odoo stock location
        using Odoo 19's native stock.quant inventory adjustment workflow.
        Never uses raw SQL updates on stock_quant!
        """
        discrepancy.ensure_one()
        instance = discrepancy.instance_id
        product = discrepancy.product_id.with_company(instance.company_id)
        location = discrepancy.location_id

        if not product or not location:
            raise UserError(_("Product and Stock Location are required to apply a stock adjustment."))

        # Acquire lock on quant rows for this product & location
        Quant = self.env['stock.quant'].with_company(instance.company_id).sudo().with_context(inventory_mode=True)
        existing_quant = Quant.search([
            ('product_id', '=', product.id),
            ('location_id', '=', location.id),
            ('lot_id', '=', False),
            ('package_id', '=', False),
            ('owner_id', '=', False),
        ], limit=1)

        # Target on-hand at this specific location = current on-hand at this location + difference_qty
        # This guarantees that across the location hierarchy, available quantity equals Mintsoft available
        # without ever double-subtracting Odoo reservations!
        current_loc_on_hand = existing_quant.quantity if existing_quant else 0.0
        target_loc_on_hand = max(0.0, round(current_loc_on_hand + discrepancy.difference_qty, 4))

        inv_name = _(
            "Kays Mintsoft Stock Sync (%(conn)s - %(sku)s)"
        ) % {'conn': instance.name, 'sku': discrepancy.sku}

        if existing_quant:
            existing_quant.with_context(inventory_name=inv_name).write({
                'inventory_quantity': target_loc_on_hand,
            })
            existing_quant.with_context(inventory_name=inv_name).action_apply_inventory()
        else:
            new_quant = Quant.with_context(inventory_name=inv_name).create({
                'product_id': product.id,
                'location_id': location.id,
                'inventory_quantity': target_loc_on_hand,
            })
            new_quant.with_context(inventory_name=inv_name).action_apply_inventory()

        discrepancy.write({
            'status': 'adjusted',
            'reason': _("Stock adjusted in location '%(loc)s' to match Mintsoft available quantity (%(avail)s).")
            % {'loc': location.display_name, 'avail': discrepancy.mintsoft_available_qty},
            'adjusted_at': fields.Datetime.now(),
            'adjusted_by': self.env.user.id,
        })
        return True

    def _notify_large_discrepancies(self, instance, snapshot, discrepancies):
        try:
            body_lines = [
                _("Large stock discrepancies detected on connection <b>%s</b> (Snapshot: %s):")
                % (instance.display_name, snapshot.name),
                "<ul>",
            ]
            for d in discrepancies[:20]:
                body_lines.append(
                    f"<li><b>{d.sku}</b>: Odoo Available={d.odoo_available_qty}, "
                    f"Mintsoft Available={d.mintsoft_available_qty} (Diff: {d.difference_qty:+.2f}) — {d.status}</li>"
                )
            body_lines.append("</ul>")
            instance.send_admin_alert(
                subject=_("Kays Logistics: Large Stock Discrepancies Detected (%d SKUs)") % len(discrepancies),
                body="".join(body_lines),
            )
        except Exception as exc:
            _logger.warning("Failed to send large discrepancy notification: %s", exc)
