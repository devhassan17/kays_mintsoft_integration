import json
import logging
import math
from odoo import fields, _
from odoo.exceptions import UserError

from .mintsoft_client import (
    MintsoftClient,
    MintsoftAPIError,
    MintsoftDryRunBlockedError,
    sanitize_payload,
)

_logger = logging.getLogger(__name__)

# Mintsoft statuses where order cancellation or amendment is no longer allowed
NON_AMENDABLE_STATUS_KEYWORDS = (
    'despatched', 'dispatched', 'shipped', 'delivered', 'cancelled', 'canceled', 'picked', 'packed',
)
NON_CANCELLABLE_STATUS_KEYWORDS = (
    'despatched', 'dispatched', 'shipped', 'delivered', 'cancelled', 'canceled',
)


class OrderService:
    """Domain service for Odoo sale.order <-> Mintsoft Order operations."""

    def __init__(self, env):
        self.env = env

    # -------------------------------------------------------------------------
    # Reference & Address Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def split_person_name(full_name):
        """Split a full recipient name into (FirstName, LastName) as required by Mintsoft."""
        cleaned = (full_name or '').strip()
        if not cleaned:
            return ('Customer', '-')
        parts = cleaned.split(None, 1)
        if len(parts) == 1:
            return (parts[0][:50], '-')
        return (parts[0][:50], parts[1][:50])

    @staticmethod
    def compute_external_reference(sale_order, instance):
        """
        Compute a deterministic, company-aware ExternalOrderReference.
        Never generates random values so retries remain strictly idempotent.
        """
        base_name = (sale_order.name or '').strip()
        strategy = instance.reference_strategy or 'order_name'
        prefix = ''

        if strategy == 'company_prefixed':
            comp_code = (
                sale_order.company_id.kays_reference_prefix
                or ''.join(w[0] for w in (sale_order.company_id.name or 'CO').split() if w).upper()
                or f"C{sale_order.company_id.id}"
            )
            prefix = f"{comp_code.rstrip('-')}-"
        elif strategy == 'custom_prefix' and instance.reference_prefix:
            prefix = f"{instance.reference_prefix.strip().rstrip('-')}-"

        ref = f"{prefix}{base_name}" if prefix and not base_name.startswith(prefix) else base_name

        if instance.environment == 'test_client':
            test_pref = (instance.test_order_prefix or 'TEST-').strip()
            if test_pref and not ref.startswith(test_pref):
                ref = f"{test_pref}{ref}"

        return ref[:50]

    @staticmethod
    def classify_destination_region(country_code, postcode):
        """Classify shipping destination into uk_mainland, northern_ireland, ireland, or international."""
        code = (country_code or '').upper().strip()
        pc = (postcode or '').upper().strip()
        if code in ('GB', 'UK'):
            if pc.startswith('BT'):
                return 'northern_ireland'
            return 'uk_mainland'
        if code == 'IE':
            return 'ireland'
        return 'international'

    # -------------------------------------------------------------------------
    # Payment & Order Eligibility Validation
    # -------------------------------------------------------------------------
    def check_payment_eligibility(self, sale_order, instance):
        """
        Verify payment eligibility according to instance.payment_eligibility_policy.
        Does NOT assume an order is paid merely because state == 'sale'.
        """
        policy = instance.payment_eligibility_policy or 'confirmed'
        if policy == 'confirmed':
            return True, ''

        if policy == 'manual_approval':
            if sale_order.kays_payment_approved:
                return True, ''
            return False, _("Order requires manual Kays payment/export approval before submission.")

        txs = sale_order.sudo().transaction_ids if hasattr(sale_order, 'transaction_ids') else self.env['payment.transaction']
        done_txs = txs.filtered(lambda t: t.state == 'done')
        auth_txs = txs.filtered(lambda t: t.state in ('authorized', 'done'))

        invoices = sale_order.sudo().invoice_ids.filtered(lambda inv: inv.state == 'posted' and inv.move_type == 'out_invoice')
        paid_invoices = invoices.filtered(lambda inv: inv.payment_state in ('in_payment', 'paid'))

        if policy == 'confirmed_paid':
            if done_txs or paid_invoices or sale_order.kays_payment_approved:
                return True, ''
            return False, _("Order is confirmed but has no completed payment transaction or paid invoice.")

        if policy == 'confirmed_authorized':
            if auth_txs or paid_invoices or sale_order.kays_payment_approved:
                return True, ''
            return False, _("Order has no authorized or completed payment transaction.")

        if policy == 'custom_providers':
            allowed_providers = instance.allowed_payment_provider_ids
            if not allowed_providers:
                return False, _("Custom payment provider policy is active, but no allowed payment providers are configured.")
            matching_txs = auth_txs.filtered(lambda t: t.provider_id in allowed_providers)
            if matching_txs or sale_order.kays_payment_approved:
                return True, ''
            return False, _("Order does not have a valid transaction from an allowed payment provider.")

        return True, ''

    def validate_sale_order(self, sale_order, instance, is_manual=False):
        """
        Run the complete 14-point validation checklist on a sale.order for the target instance.
        Returns dict: {'valid': bool, 'errors': list[str], 'warnings': list[str], 'resolved': dict}
        """
        errors = []
        warnings = []

        # 1. Company & Multi-company isolation check
        company = sale_order.company_id
        if not company.kays_enabled:
            errors.append(_("Kays Logistics integration is disabled for company '%s'.") % company.display_name)

        if instance.company_id != company:
            errors.append(
                _("Connection '%(conn)s' belongs to company '%(icomp)s', not order company '%(ocomp)s'.")
                % {
                    'conn': instance.display_name,
                    'icomp': instance.company_id.display_name,
                    'ocomp': company.display_name,
                }
            )

        # 2. Master & feature switches
        if not instance.active or not instance.enable_integration:
            errors.append(_("Kays integration is disabled on connection '%s'.") % instance.display_name)

        if is_manual and not instance.enable_manual_order_export:
            errors.append(_("Manual Order Export is disabled on connection '%s'.") % instance.display_name)
        elif not is_manual and not instance.enable_auto_order_export:
            errors.append(_("Automatic Order Export is disabled on connection '%s'.") % instance.display_name)

        # 3. Order state check
        if sale_order.state not in ('sale',):
            errors.append(
                _("Sales Order '%(name)s' is in state '%(state)s'. Only confirmed Sales Orders can be exported.")
                % {'name': sale_order.name, 'state': sale_order.state}
            )

        # 4. Payment eligibility
        pay_ok, pay_reason = self.check_payment_eligibility(sale_order, instance)
        if not pay_ok:
            errors.append(pay_reason)

        # 5. Shipping address validation
        ship_partner = sale_order.partner_shipping_id or sale_order.partner_id
        if not ship_partner:
            errors.append(_("Shipping partner is missing on the Sales Order."))
        else:
            if not (ship_partner.name or ship_partner.commercial_company_name):
                errors.append(_("Recipient name is missing on the shipping address."))
            if not (ship_partner.street or '').strip():
                errors.append(_("Street address (Address1) is missing on the shipping address."))
            if not (ship_partner.city or '').strip():
                errors.append(_("City/Town is missing on the shipping address."))
            if not (ship_partner.zip or '').strip():
                errors.append(_("Postcode/ZIP is missing on the shipping address."))
            if not ship_partner.country_id or not ship_partner.country_id.code:
                errors.append(_("Country with valid ISO code is required on the shipping address."))

        # 6. Warehouse resolution
        warehouse_id = instance.mintsoft_warehouse_id
        wh_mapping = self.env['kays.mintsoft.warehouse.mapping'].search([
            ('instance_id', '=', instance.id),
            ('odoo_warehouse_id', '=', sale_order.warehouse_id.id),
            ('active', '=', True),
        ], limit=1)
        if wh_mapping:
            warehouse_id = wh_mapping.mintsoft_warehouse_id
        if not warehouse_id or warehouse_id <= 0:
            errors.append(_("No valid Mintsoft Warehouse ID is mapped or configured on the connection."))

        # 7. Courier mapping resolution
        country_code = ship_partner.country_id.code if ship_partner and ship_partner.country_id else ''
        postcode = ship_partner.zip if ship_partner else ''
        region = self.classify_destination_region(country_code, postcode)

        courier_service_id = 0
        courier_service_name = ''
        carrier_mapping = self.env['kays.mintsoft.carrier.mapping']

        if sale_order.carrier_id:
            mappings = self.env['kays.mintsoft.carrier.mapping'].search([
                ('instance_id', '=', instance.id),
                ('delivery_carrier_id', '=', sale_order.carrier_id.id),
                ('active', '=', True),
            ])
            # Prefer exact region or country match first, then 'all'
            for m in mappings:
                if m.country_ids and ship_partner.country_id in m.country_ids:
                    if m.region_policy in ('all', region):
                        carrier_mapping = m
                        break
                elif m.region_policy == region:
                    carrier_mapping = m
                    break
            if not carrier_mapping:
                carrier_mapping = mappings.filtered(lambda m: m.region_policy == 'all')[:1]

        if carrier_mapping:
            courier_service_id = carrier_mapping.mintsoft_service_id
            courier_service_name = carrier_mapping.mintsoft_service_name
        else:
            policy = instance.unmapped_carrier_policy or 'block'
            if policy == 'block':
                errors.append(
                    _("Shipping carrier '%(carrier)s' (region: %(region)s) is not mapped to a Mintsoft Courier Service.")
                    % {'carrier': sale_order.carrier_id.display_name if sale_order.carrier_id else _('None'), 'region': region}
                )
            elif policy == 'warn_fallback':
                if instance.default_mintsoft_courier_service_id > 0:
                    courier_service_id = instance.default_mintsoft_courier_service_id
                    courier_service_name = instance.default_mintsoft_courier_service_name or ''
                    warnings.append(
                        _("Used default Mintsoft Courier Service ID %s because order carrier was unmapped.")
                        % courier_service_id
                    )
                else:
                    errors.append(_("Fallback courier policy selected, but no default Mintsoft Courier Service ID is configured."))
            elif policy == 'allow_auto':
                courier_service_name = instance.default_mintsoft_courier_service_name or 'AUTO'
                warnings.append(_("No explicit courier mapping found; using '%s'.") % courier_service_name)

        # 8. Order lines, SKUs, UoMs, and delivery charges separation
        physical_items = []
        shipping_ex_vat = 0.0
        shipping_vat = 0.0
        discount_ex_vat = 0.0

        for line in sale_order.order_line:
            if line.display_type:
                continue
            if getattr(line, 'is_delivery', False):
                shipping_ex_vat += float(line.price_subtotal or 0.0)
                shipping_vat += float(line.price_tax or 0.0)
                continue
            product = line.product_id
            if not product:
                continue
            if product.type == 'service':
                # Exclude non-physical service lines from warehouse fulfilment
                continue

            line_uom = getattr(line, 'product_uom_id', None) or getattr(line, 'product_uom', None)
            base_uom = product.uom_id
            raw_qty = float(line.product_uom_qty or 0.0)
            if raw_qty <= 0:
                continue

            # Convert from sale line UoM to product base UoM using Odoo 19 _compute_quantity
            try:
                if line_uom and base_uom and line_uom != base_uom:
                    converted_qty = line_uom._compute_quantity(raw_qty, base_uom, round=False)
                else:
                    converted_qty = raw_qty
            except Exception as uom_err:
                errors.append(
                    _("Cannot convert UoM for product '%(prod)s': %(err)s")
                    % {'prod': product.display_name, 'err': uom_err}
                )
                continue

            mapping = self.env['kays.mintsoft.product.mapping'].search([
                ('instance_id', '=', instance.id),
                ('product_id', '=', product.id),
                ('active', '=', True),
            ], limit=1)

            if mapping and mapping.mapping_status == 'disabled':
                errors.append(_("Product mapping for '%s' is disabled.") % product.display_name)
                continue

            uom_factor = mapping.mintsoft_uom_factor if (mapping and mapping.mintsoft_uom_factor > 0) else 1.0
            final_qty_float = converted_qty * uom_factor

            # Mintsoft NewOrderItem.Quantity requires an integer
            if not math.isclose(final_qty_float, round(final_qty_float), rel_tol=1e-5, abs_tol=1e-5):
                errors.append(
                    _("Product '%(prod)s' quantity %(qty)s converts to non-integer Mintsoft quantity %(mqty)s.")
                    % {'prod': product.display_name, 'qty': raw_qty, 'mqty': final_qty_float}
                )
                continue

            mintsoft_qty = int(round(final_qty_float))
            if mintsoft_qty <= 0:
                errors.append(_("Product '%s' has zero or negative Mintsoft quantity.") % product.display_name)
                continue

            sku = (mapping.mintsoft_sku if mapping and mapping.mintsoft_sku else (product.default_code or '')).strip()
            if not sku:
                errors.append(
                    _("Missing SKU on product '%s'. Configure an Internal Reference (SKU) or Kays Product Mapping.")
                    % product.display_name
                )
                continue

            unit_price_ex_vat = round(float(line.price_subtotal or 0.0) / mintsoft_qty, 4) if mintsoft_qty else 0.0
            unit_vat = round(float(line.price_tax or 0.0) / mintsoft_qty, 4) if mintsoft_qty else 0.0
            line_discount = round(
                float(line.price_unit or 0.0) * raw_qty * (float(line.discount or 0.0) / 100.0), 2
            )
            discount_ex_vat += line_discount

            item_dict = {
                'sale_line_id': line.id,
                'product_id': product.id,
                'product_mapping_id': mapping.id if mapping else False,
                'SKU': sku,
                'ProductId': mapping.mintsoft_product_id if (mapping and mapping.mintsoft_product_id > 0) else 0,
                'Quantity': mintsoft_qty,
                'UnitPrice': unit_price_ex_vat,
                'UnitPriceVat': unit_vat,
                'Discount': line_discount,
                'Details': (line.name or product.name or sku)[:250],
            }
            physical_items.append(item_dict)

        if not physical_items:
            errors.append(_("Order '%s' contains no eligible physical product lines for warehouse fulfilment.") % sale_order.name)

        # 9. Currency validation
        currency = sale_order.currency_id or instance.currency_id or sale_order.company_id.currency_id
        if not currency or not currency.name:
            errors.append(_("Order currency is missing."))

        external_ref = self.compute_external_reference(sale_order, instance)

        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
            'resolved': {
                'external_reference': external_ref,
                'warehouse_id': warehouse_id,
                'warehouse_mapping_id': wh_mapping.id if wh_mapping else False,
                'carrier_mapping_id': carrier_mapping.id if carrier_mapping else False,
                'courier_service_id': courier_service_id,
                'courier_service_name': courier_service_name,
                'region': region,
                'shipping_ex_vat': round(shipping_ex_vat, 2),
                'shipping_vat': round(shipping_vat, 2),
                'discount_ex_vat': round(discount_ex_vat, 2),
                'currency_code': currency.name if currency else 'GBP',
                'items': physical_items,
            },
        }

    def validate_subscription_delivery(self, picking, instance, is_manual=False, external_ref=None):
        """
        Validate an Odoo stock.picking representing a subscription delivery against Mintsoft rules.
        Validates company, feature switches, shipping address, mapped warehouse, mapped courier,
        and converts picking stock.move quantities and SKUs.
        """
        errors = []
        warnings = []

        sale_order = picking.sale_id
        if not sale_order:
            errors.append(_("Delivery '%s' has no linked Sales Order / Subscription.") % picking.name)
            return {'valid': False, 'errors': errors, 'warnings': warnings, 'resolved': {}}

        # 1. Company & multi-company isolation
        company = picking.company_id or sale_order.company_id
        if not company.kays_enabled:
            errors.append(_("Kays Logistics integration is disabled for company '%s'.") % company.display_name)
        if instance.company_id != company:
            errors.append(
                _("Connection '%(conn)s' belongs to company '%(icomp)s', not delivery company '%(ocomp)s'.")
                % {'conn': instance.display_name, 'icomp': instance.company_id.display_name, 'ocomp': company.display_name}
            )

        # 2. Master & feature switches
        if not instance.active or not instance.enable_integration:
            errors.append(_("Kays integration is disabled on connection '%s'.") % instance.display_name)
        if is_manual and not instance.enable_manual_order_export:
            errors.append(_("Manual Order Export is disabled on connection '%s'.") % instance.display_name)
        elif not is_manual and not instance.enable_subscription_export:
            errors.append(_("Subscription Delivery Export is disabled on connection '%s'.") % instance.display_name)

        # 3. State check
        if picking.state in ('draft', 'cancel'):
            errors.append(_("Delivery '%(name)s' is in state '%(state)s'. Cannot export draft or cancelled delivery.") % {'name': picking.name, 'state': picking.state})
        if picking.picking_type_id.code != 'outgoing':
            errors.append(_("Delivery '%(name)s' is not an outgoing delivery order.") % {'name': picking.name})

        # 4. Payment eligibility check
        if not is_manual:
            pay_ok, pay_reason = self.check_payment_eligibility(sale_order, instance)
            if not pay_ok:
                errors.append(pay_reason)

        # 5. Shipping address validation
        ship_partner = picking.partner_id or sale_order.partner_shipping_id or sale_order.partner_id
        if not ship_partner:
            errors.append(_("Shipping partner is missing on delivery and sales order."))
        else:
            if not (ship_partner.name or ship_partner.commercial_company_name):
                errors.append(_("Recipient name is missing on the shipping address."))
            if not (ship_partner.street or '').strip():
                errors.append(_("Street address (Address1) is missing on the shipping address."))
            if not (ship_partner.city or '').strip():
                errors.append(_("City/Town is missing on the shipping address."))
            if not (ship_partner.zip or '').strip():
                errors.append(_("Postcode/ZIP is missing on the shipping address."))
            if not ship_partner.country_id or not ship_partner.country_id.code:
                errors.append(_("Country with valid ISO code is required on the shipping address."))

        # 6. Warehouse resolution
        warehouse_id = instance.mintsoft_warehouse_id
        odoo_wh = picking.picking_type_id.warehouse_id or sale_order.warehouse_id
        wh_mapping = self.env['kays.mintsoft.warehouse.mapping'].search([
            ('instance_id', '=', instance.id),
            ('odoo_warehouse_id', '=', odoo_wh.id),
            ('active', '=', True),
        ], limit=1) if odoo_wh else False
        if wh_mapping:
            warehouse_id = wh_mapping.mintsoft_warehouse_id
        if not warehouse_id or warehouse_id <= 0:
            errors.append(_("No valid Mintsoft Warehouse ID is mapped or configured on the connection."))

        # 7. Courier mapping resolution
        country_code = ship_partner.country_id.code if ship_partner and ship_partner.country_id else ''
        postcode = ship_partner.zip if ship_partner else ''
        region = self.classify_destination_region(country_code, postcode)

        courier_service_id = 0
        courier_service_name = ''
        carrier_mapping = self.env['kays.mintsoft.carrier.mapping']

        carrier = picking.carrier_id or sale_order.carrier_id
        if carrier:
            mappings = self.env['kays.mintsoft.carrier.mapping'].search([
                ('instance_id', '=', instance.id),
                ('delivery_carrier_id', '=', carrier.id),
                ('active', '=', True),
            ])
            for m in mappings:
                if m.country_ids and ship_partner.country_id in m.country_ids:
                    if m.region_policy in ('all', region):
                        carrier_mapping = m
                        break
                elif m.region_policy == region:
                    carrier_mapping = m
                    break
            if not carrier_mapping:
                carrier_mapping = mappings.filtered(lambda m: m.region_policy == 'all')[:1]

        if carrier_mapping:
            courier_service_id = carrier_mapping.mintsoft_service_id
            courier_service_name = carrier_mapping.mintsoft_service_name
        else:
            policy = instance.unmapped_carrier_policy or 'block'
            if policy == 'block':
                errors.append(
                    _("Shipping carrier '%(carrier)s' (region: %(region)s) is not mapped to a Mintsoft Courier Service.")
                    % {'carrier': carrier.display_name if carrier else _('None'), 'region': region}
                )
            elif policy == 'warn_fallback':
                if instance.default_mintsoft_courier_service_id > 0:
                    courier_service_id = instance.default_mintsoft_courier_service_id
                    courier_service_name = instance.default_mintsoft_courier_service_name or ''
                    warnings.append(
                        _("Used default Mintsoft Courier Service ID %s because delivery carrier was unmapped.")
                        % courier_service_id
                    )
                else:
                    errors.append(_("Fallback courier policy selected, but no default Mintsoft Courier Service ID is configured."))
            elif policy == 'allow_auto':
                courier_service_name = instance.default_mintsoft_courier_service_name or 'AUTO'
                warnings.append(_("No explicit courier mapping found; using '%s'.") % courier_service_name)

        # 8. Physical moves from picking.move_ids
        physical_items = []
        total_order_val = 0.0
        moves = picking.move_ids.filtered(lambda m: m.state not in ('draft', 'cancel'))

        for move in moves:
            product = move.product_id
            if not product or product.type == 'service':
                continue

            raw_qty = float(move.product_uom_qty or 0.0)
            if raw_qty <= 0:
                continue

            line_uom = move.product_uom
            base_uom = product.uom_id
            try:
                if line_uom and base_uom and line_uom != base_uom:
                    converted_qty = line_uom._compute_quantity(raw_qty, base_uom, round=False)
                else:
                    converted_qty = raw_qty
            except Exception as uom_err:
                errors.append(
                    _("Cannot convert UoM for product '%(prod)s': %(err)s")
                    % {'prod': product.display_name, 'err': uom_err}
                )
                continue

            mapping = self.env['kays.mintsoft.product.mapping'].search([
                ('instance_id', '=', instance.id),
                ('product_id', '=', product.id),
                ('active', '=', True),
            ], limit=1)

            if mapping and mapping.mapping_status == 'disabled':
                errors.append(_("Product mapping for '%s' is disabled.") % product.display_name)
                continue

            uom_factor = mapping.mintsoft_uom_factor if (mapping and mapping.mintsoft_uom_factor > 0) else 1.0
            final_qty_float = converted_qty * uom_factor

            if not math.isclose(final_qty_float, round(final_qty_float), rel_tol=1e-5, abs_tol=1e-5):
                errors.append(
                    _("Product '%(prod)s' quantity %(qty)s converts to non-integer Mintsoft quantity %(mqty)s.")
                    % {'prod': product.display_name, 'qty': raw_qty, 'mqty': final_qty_float}
                )
                continue

            mintsoft_qty = int(round(final_qty_float))
            if mintsoft_qty <= 0:
                errors.append(_("Product '%s' has zero or negative Mintsoft quantity.") % product.display_name)
                continue

            sku = (mapping.mintsoft_sku if mapping and mapping.mintsoft_sku else (product.default_code or '')).strip()
            if not sku:
                errors.append(
                    _("Missing SKU on product '%s'. Configure an Internal Reference (SKU) or Kays Product Mapping.")
                    % product.display_name
                )
                continue

            sale_line = move.sale_line_id
            unit_price_ex_vat = round(float(sale_line.price_unit or 0.0), 4) if sale_line else round(float(product.lst_price or 0.0), 4)
            unit_vat = round(float(sale_line.price_tax or 0.0) / mintsoft_qty, 4) if (sale_line and mintsoft_qty) else 0.0
            line_discount = round(
                float(sale_line.price_unit or 0.0) * raw_qty * (float(sale_line.discount or 0.0) / 100.0), 2
            ) if sale_line else 0.0

            total_order_val += unit_price_ex_vat * mintsoft_qty

            item_dict = {
                'move_id': move.id,
                'sale_line_id': sale_line.id if sale_line else False,
                'product_id': product.id,
                'product_mapping_id': mapping.id if mapping else False,
                'SKU': sku,
                'ProductId': mapping.mintsoft_product_id if (mapping and mapping.mintsoft_product_id > 0) else 0,
                'Quantity': mintsoft_qty,
                'UnitPrice': unit_price_ex_vat,
                'UnitPriceVat': unit_vat,
                'Discount': line_discount,
                'Details': (sale_line.name or move.name or product.name or sku)[:250] if sale_line else (product.name or sku)[:250],
            }
            physical_items.append(item_dict)

        if not physical_items:
            errors.append(_("Delivery '%s' contains no eligible physical product lines for warehouse fulfilment.") % picking.name)

        # 9. Currency
        currency = sale_order.currency_id or instance.currency_id or picking.company_id.currency_id
        if not currency or not currency.name:
            errors.append(_("Order currency is missing."))

        if not external_ref:
            base_ref = self.compute_external_reference(sale_order, instance)
            external_ref = f"{base_ref}-{picking.name.split('/')[-1]}"[:50]

        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
            'resolved': {
                'external_reference': external_ref,
                'warehouse_id': warehouse_id,
                'warehouse_mapping_id': wh_mapping.id if wh_mapping else False,
                'carrier_mapping_id': carrier_mapping.id if carrier_mapping else False,
                'courier_service_id': courier_service_id,
                'courier_service_name': courier_service_name,
                'region': region,
                'order_total': round(total_order_val, 2),
                'shipping_ex_vat': 0.0,
                'shipping_vat': 0.0,
                'discount_ex_vat': 0.0,
                'currency_code': currency.name if currency else 'GBP',
                'items': physical_items,
            },
        }

    # -------------------------------------------------------------------------
    # Payload Construction
    # -------------------------------------------------------------------------
    def build_mintsoft_order_payload(self, sale_order, instance, validation_result=None):
        """Build the exact Mintsoft PUT /api/Order (NewOrderWithItems) payload."""
        if not validation_result:
            validation_result = self.validate_sale_order(sale_order, instance, is_manual=True)

        resolved = validation_result['resolved']
        ship_partner = sale_order.partner_shipping_id or sale_order.partner_id
        first_name, last_name = self.split_person_name(ship_partner.name or ship_partner.commercial_company_name)

        order_items_payload = []
        for item in resolved['items']:
            line_payload = {
                'SKU': item['SKU'],
                'Quantity': item['Quantity'],
                'UnitPrice': item['UnitPrice'],
                'UnitPriceVat': item['UnitPriceVat'],
                'Discount': item['Discount'],
                'Details': item['Details'],
            }
            if item.get('ProductId') and item['ProductId'] > 0:
                line_payload['ProductId'] = item['ProductId']
            order_items_payload.append(line_payload)

        tags = ['ODOO19']
        if instance.environment == 'test_client':
            tags.append('TEST_ORDER')

        payload = {
            'OrderNumber': resolved['external_reference'],
            'ExternalOrderReference': (sale_order.client_order_ref or resolved['external_reference'])[:50],
            'FirstName': first_name,
            'LastName': last_name,
            'CompanyName': (ship_partner.commercial_company_name or '')[:100] if ship_partner.is_company or ship_partner.parent_id else '',
            'Address1': (ship_partner.street or '')[:100],
            'Address2': (ship_partner.street2 or '')[:100],
            'Town': (ship_partner.city or '')[:100],
            'County': (ship_partner.state_id.name if ship_partner.state_id else '')[:100],
            'PostCode': (ship_partner.zip or '')[:20],
            'Country': (ship_partner.country_id.code if ship_partner.country_id else 'GB')[:2],
            'Email': (ship_partner.email or sale_order.partner_id.email or '')[:100],
            'Phone': (ship_partner.phone or sale_order.partner_id.phone or '')[:50],
            'WarehouseId': resolved['warehouse_id'],
            'Currency': resolved['currency_code'],
            'OrderValue': round(float(sale_order.amount_total or 0.0), 2),
            'TotalVat': round(float(sale_order.amount_tax or 0.0), 2),
            'ShippingTotalExVat': resolved['shipping_ex_vat'],
            'ShippingTotalVat': resolved['shipping_vat'],
            'DiscountTotalExVat': resolved['discount_ex_vat'],
            'DeliveryNotes': (sale_order.kays_delivery_notes or '')[:500],
            'Tags': ','.join(tags),
            'OrderItems': order_items_payload,
        }

        if instance.mintsoft_client_id and instance.mintsoft_client_id > 0:
            payload['ClientId'] = instance.mintsoft_client_id

        if resolved['courier_service_id'] and resolved['courier_service_id'] > 0:
            payload['CourierServiceId'] = resolved['courier_service_id']
        if resolved['courier_service_name']:
            payload['CourierService'] = resolved['courier_service_name']

        if instance.mintsoft_channel_id and instance.mintsoft_channel_id > 0:
            payload['ChannelId'] = instance.mintsoft_channel_id
        elif instance.mintsoft_channel_name:
            payload['Channel'] = instance.mintsoft_channel_name

        if sale_order.commitment_date:
            payload['RequiredDeliveryDate'] = sale_order.commitment_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Register ConnectAction callback on order creation when webhooks are enabled
        if instance.enable_webhooks and instance.register_connect_action_on_order and instance.webhook_url:
            connect_action = {
                'Type': 'Despatch',
                'SourceOrderId': resolved['external_reference'],
                'Complete': False,
                'ExtraCode1': instance.webhook_url,
            }
            if instance.connect_action_account_id and instance.connect_action_account_id > 0:
                connect_action['AccountId'] = instance.connect_action_account_id
            payload['ConnectAction'] = connect_action

        return payload

    def build_mintsoft_subscription_delivery_payload(self, picking, instance, validation_result=None):
        """Build the exact Mintsoft PUT /api/Order payload for a subscription delivery picking."""
        if not validation_result:
            validation_result = self.validate_subscription_delivery(picking, instance, is_manual=True)

        resolved = validation_result['resolved']
        sale_order = picking.sale_id
        ship_partner = picking.partner_id or sale_order.partner_shipping_id or sale_order.partner_id
        first_name, last_name = self.split_person_name(ship_partner.name or ship_partner.commercial_company_name)

        order_items_payload = []
        for item in resolved['items']:
            line_payload = {
                'SKU': item['SKU'],
                'Quantity': item['Quantity'],
                'UnitPrice': item['UnitPrice'],
                'UnitPriceVat': item['UnitPriceVat'],
                'Discount': item['Discount'],
                'Details': item['Details'],
            }
            if item.get('ProductId') and item['ProductId'] > 0:
                line_payload['ProductId'] = item['ProductId']
            order_items_payload.append(line_payload)

        tags = ['ODOO19', 'SUBSCRIPTION']
        if instance.environment == 'test_client':
            tags.append('TEST_ORDER')

        payload = {
            'OrderNumber': resolved['external_reference'],
            'ExternalOrderReference': resolved['external_reference'],
            'FirstName': first_name,
            'LastName': last_name,
            'CompanyName': (ship_partner.commercial_company_name or '')[:100] if ship_partner.is_company or ship_partner.parent_id else '',
            'Address1': (ship_partner.street or '')[:100],
            'Address2': (ship_partner.street2 or '')[:100],
            'Town': (ship_partner.city or '')[:100],
            'County': (ship_partner.state_id.name if ship_partner.state_id else '')[:100],
            'PostCode': (ship_partner.zip or '')[:20],
            'Country': (ship_partner.country_id.code if ship_partner.country_id else 'GB')[:2],
            'Email': (ship_partner.email or sale_order.partner_id.email or '')[:100],
            'Phone': (ship_partner.phone or sale_order.partner_id.phone or '')[:50],
            'WarehouseId': resolved['warehouse_id'],
            'Currency': resolved['currency_code'],
            'OrderValue': resolved['order_total'],
            'TotalVat': 0.0,
            'ShippingTotalExVat': 0.0,
            'ShippingTotalVat': 0.0,
            'DiscountTotalExVat': 0.0,
            'DeliveryNotes': (picking.note or sale_order.kays_delivery_notes or '')[:500],
            'Tags': ','.join(tags),
            'OrderItems': order_items_payload,
        }

        if instance.mintsoft_client_id and instance.mintsoft_client_id > 0:
            payload['ClientId'] = instance.mintsoft_client_id

        if resolved['courier_service_id'] and resolved['courier_service_id'] > 0:
            payload['CourierServiceId'] = resolved['courier_service_id']
        if resolved['courier_service_name']:
            payload['CourierService'] = resolved['courier_service_name']

        if instance.mintsoft_channel_id and instance.mintsoft_channel_id > 0:
            payload['ChannelId'] = instance.mintsoft_channel_id
        elif instance.mintsoft_channel_name:
            payload['Channel'] = instance.mintsoft_channel_name

        if picking.scheduled_date:
            payload['RequiredDeliveryDate'] = picking.scheduled_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        if instance.enable_webhooks and instance.register_connect_action_on_order and instance.webhook_url:
            connect_action = {
                'Type': 'Despatch',
                'SourceOrderId': resolved['external_reference'],
                'Complete': False,
                'ExtraCode1': instance.webhook_url,
            }
            if instance.connect_action_account_id and instance.connect_action_account_id > 0:
                connect_action['AccountId'] = instance.connect_action_account_id
            payload['ConnectAction'] = connect_action

        return payload

    # -------------------------------------------------------------------------
    # Pre-Export Duplicate Detection & Reconciliation
    # -------------------------------------------------------------------------
    def check_remote_duplicate(self, client, instance, external_ref):
        """
        Check Mintsoft for an existing order matching external_ref using documented endpoints:
        1. GET /api/Order/Search?OrderNumber=<ref>&exactMatch=true&includeOrderItems=true
        2. GET /api/Order/GetOrderId?orderNumber=<ref>&warehouseId=<wid>
        Returns list of matched Mintsoft Order dicts (or minimal OrderId dicts).
        """
        matches = []
        try:
            search_res = client.request(
                'GET',
                '/api/Order/Search',
                params={
                    'OrderNumber': external_ref,
                    'exactMatch': True,
                    'includeOrderItems': True,
                },
                operation='check_duplicate_search',
                allow_404=True,
            )
            if isinstance(search_res, list):
                for ord_obj in search_res:
                    if isinstance(ord_obj, dict):
                        ord_num = (ord_obj.get('OrderNumber') or '').strip()
                        ext_ref = (ord_obj.get('ExternalOrderReference') or '').strip()
                        if ord_num == external_ref or ext_ref == external_ref:
                            matches.append(ord_obj)
        except MintsoftAPIError as exc:
            _logger.info("Order/Search lookup during duplicate check returned: %s", exc.message)

        if not matches:
            params = {'orderNumber': external_ref}
            if instance.mintsoft_warehouse_id and instance.mintsoft_warehouse_id > 0:
                params['warehouseId'] = instance.mintsoft_warehouse_id
            if instance.mintsoft_client_id and instance.mintsoft_client_id > 0:
                params['clientId'] = instance.mintsoft_client_id
            try:
                id_res = client.request(
                    'GET',
                    '/api/Order/GetOrderId',
                    params=params,
                    operation='check_duplicate_get_id',
                    allow_404=True,
                )
                if isinstance(id_res, int) and id_res > 0:
                    matches.append({'ID': id_res, 'OrderNumber': external_ref})
                elif isinstance(id_res, dict) and (id_res.get('ID') or id_res.get('OrderId')):
                    matches.append({
                        'ID': id_res.get('ID') or id_res.get('OrderId'),
                        'OrderNumber': external_ref,
                    })
            except MintsoftAPIError as exc:
                if exc.http_status != 404:
                    _logger.info("Order/GetOrderId lookup returned: %s", exc.message)

        return matches

    # -------------------------------------------------------------------------
    # Full Order Submission Workflow
    # -------------------------------------------------------------------------
    def export_sale_order(self, sale_order, instance=None, is_manual=False):
        """
        Validate, deduplicate, and export an eligible Odoo sale.order to Mintsoft via PUT /api/Order.
        Handles Dry Run mode, split-order responses, and unknown-outcome timeouts safely.
        """
        sale_order.ensure_one()
        instance = instance or sale_order.kays_instance_id or sale_order.company_id._get_default_kays_instance(sale_order=sale_order)
        if not instance:
            raise UserError(_("No Kays Logistics connection is configured for company '%s'.") % sale_order.company_id.display_name)

        # Acquire row-level lock on sale_order to prevent concurrent duplicate exports
        try:
            self.env.cr.execute(
                "SELECT id FROM sale_order WHERE id = %s FOR UPDATE NOWAIT",
                [sale_order.id],
            )
        except Exception as lock_exc:
            raise UserError(
                _("Order '%s' is currently being processed by another synchronization worker.") % sale_order.name
            ) from lock_exc

        # Check existing kays.mintsoft.order record
        existing_kays_orders = self.env['kays.mintsoft.order'].search([
            ('sale_order_id', '=', sale_order.id),
            ('instance_id', '=', instance.id),
            ('is_split_order', '=', False),
        ], limit=1)

        if existing_kays_orders:
            primary_ko = existing_kays_orders[0]
            if primary_ko.export_status == 'sent' and primary_ko.mintsoft_order_id > 0:
                if is_manual:
                    raise UserError(
                        _("Order '%(order)s' has already been exported to Mintsoft (Mintsoft Order ID: %(ms_id)s).")
                        % {'order': sale_order.name, 'ms_id': primary_ko.mintsoft_order_id}
                    )
                return primary_ko

            if primary_ko.export_status == 'unknown_outcome':
                # Must reconcile before any retry! Never blindly resend an unknown_outcome order.
                reconciled = self.reconcile_unknown_outcome_order(primary_ko)
                if reconciled:
                    return primary_ko
                # If reconciliation failed due to network error, primary_ko remains in unknown_outcome
                if primary_ko.export_status == 'unknown_outcome':
                    return primary_ko

        # Run 14-point validation
        val_res = self.validate_sale_order(sale_order, instance, is_manual=is_manual)
        resolved = val_res['resolved']
        external_ref = resolved['external_reference']

        # Ensure or create the kays.mintsoft.order record
        if existing_kays_orders:
            kays_order = existing_kays_orders[0]
            kays_order.write({
                'export_status': 'validating',
                'mintsoft_warehouse_id': resolved['warehouse_id'],
                'warehouse_mapping_id': resolved['warehouse_mapping_id'],
                'carrier_mapping_id': resolved['carrier_mapping_id'],
                'mintsoft_courier_service_id': resolved['courier_service_id'],
                'mintsoft_courier_service_name': resolved['courier_service_name'],
            })
        else:
            kays_order = self.env['kays.mintsoft.order'].create({
                'name': external_ref,
                'external_order_reference': external_ref,
                'idempotency_key': f"{instance.id}:{external_ref}",
                'sale_order_id': sale_order.id,
                'company_id': sale_order.company_id.id,
                'instance_id': instance.id,
                'partner_id': sale_order.partner_id.id,
                'shipping_partner_id': (sale_order.partner_shipping_id or sale_order.partner_id).id,
                'order_date': sale_order.date_order or fields.Datetime.now(),
                'currency_id': sale_order.currency_id.id,
                'order_total': sale_order.amount_total,
                'export_status': 'validating',
                'fulfilment_status': 'unknown',
                'mintsoft_client_id': instance.mintsoft_client_id,
                'mintsoft_warehouse_id': resolved['warehouse_id'],
                'warehouse_mapping_id': resolved['warehouse_mapping_id'],
                'carrier_mapping_id': resolved['carrier_mapping_id'],
                'mintsoft_courier_service_id': resolved['courier_service_id'],
                'mintsoft_courier_service_name': resolved['courier_service_name'],
            })

        sale_order.kays_instance_id = instance.id

        if not val_res['valid']:
            err_msg = "\n".join(val_res['errors'])
            kays_order.write({
                'export_status': 'failed',
                'last_error': err_msg,
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        payload = self.build_mintsoft_order_payload(sale_order, instance, validation_result=val_res)
        sanitized_preview = json.dumps(sanitize_payload(payload), indent=2)

        # Populate kays.mintsoft.order.line items
        kays_order.line_ids.unlink()
        line_vals = []
        for item in resolved['items']:
            line_vals.append({
                'kays_order_id': kays_order.id,
                'sale_line_id': item['sale_line_id'],
                'product_id': item['product_id'],
                'product_mapping_id': item['product_mapping_id'],
                'sku': item['SKU'],
                'mintsoft_product_id': item['ProductId'],
                'quantity': item['Quantity'],
                'unit_price': item['UnitPrice'],
                'unit_vat': item['UnitPriceVat'],
                'discount': item['Discount'],
            })
        if line_vals:
            self.env['kays.mintsoft.order.line'].create(line_vals)

        kays_order.payload_preview = sanitized_preview

        # Dry Run mode: never perform a write request; store validated payload & result locally
        if instance.environment == 'dry_run':
            dry_summary = {
                'mode': 'dry_run',
                'valid': True,
                'warnings': val_res['warnings'],
                'message': 'Dry Run validation succeeded. No write request was sent to Mintsoft.',
                'external_reference': external_ref,
                'item_count': len(resolved['items']),
            }
            kays_order.write({
                'export_status': 'pending',
                'response_summary': json.dumps(dry_summary, indent=2),
                'last_error': False,
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        client = MintsoftClient(instance)

        # Step 15: Check Mintsoft for a previously created order before submitting
        remote_matches = self.check_remote_duplicate(client, instance, external_ref)
        if remote_matches:
            first_match = remote_matches[0]
            ms_id = first_match.get('ID') or first_match.get('OrderId')
            kays_order.write({
                'mintsoft_order_id': ms_id,
                'mintsoft_order_number': first_match.get('OrderNumber') or external_ref,
                'export_status': 'sent',
                'fulfilment_status': 'received',
                'response_summary': json.dumps(sanitize_payload(remote_matches), indent=2),
                'last_error': False,
                'last_sync_at': fields.Datetime.now(),
            })
            self.sync_order_status(kays_order)
            return kays_order

        # Step 17: Submit through PUT /api/Order
        kays_order.export_status = 'sending'
        try:
            response_data = client.request(
                'PUT',
                '/api/Order',
                json_payload=payload,
                operation='create_order',
                sale_order=sale_order,
                kays_order=kays_order,
            )
        except MintsoftAPIError as api_err:
            if api_err.unknown_outcome:
                kays_order.write({
                    'export_status': 'unknown_outcome',
                    'last_error': _(
                        "Order submission outcome is unknown after timeout/server error: %s. "
                        "Reconciliation will check Mintsoft before any retry."
                    ) % api_err.message,
                    'last_sync_at': fields.Datetime.now(),
                })
            else:
                kays_order.write({
                    'export_status': 'failed',
                    'last_error': api_err.message,
                    'retry_count': kays_order.retry_count + 1,
                    'last_sync_at': fields.Datetime.now(),
                })
            return kays_order

        # Step 18 & 19: Parse response (Array<NewOrderResult> or single NewOrderResult dict)
        results_list = response_data if isinstance(response_data, list) else ([response_data] if isinstance(response_data, dict) else [])
        if not results_list:
            kays_order.write({
                'export_status': 'failed',
                'last_error': _("Empty or unexpected response from Mintsoft PUT /api/Order."),
                'response_summary': json.dumps(sanitize_payload(response_data), indent=2),
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        successful_results = [
            r for r in results_list
            if isinstance(r, dict) and (r.get('Success') is True or (r.get('OrderId') and int(r.get('OrderId') or 0) > 0))
        ]

        if not successful_results:
            err_messages = [
                r.get('Message') or r.get('OrderStatus') or 'Order creation rejected by Mintsoft'
                for r in results_list if isinstance(r, dict)
            ]
            kays_order.write({
                'export_status': 'failed',
                'last_error': "; ".join(err_messages),
                'response_summary': json.dumps(sanitize_payload(results_list), indent=2),
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        # Primary order result
        primary_res = successful_results[0]
        primary_ms_id = int(primary_res.get('OrderId') or 0)
        status_id = int(primary_res.get('OrderStatusId') or 0)
        status_name = primary_res.get('OrderStatus') or 'New'
        mapped_status = self.env['kays.mintsoft.status.mapping'].map_mintsoft_status(
            instance, status_id, status_name
        )

        kays_order.write({
            'mintsoft_order_id': primary_ms_id,
            'dropship_order_id': int(primary_res.get('DropShipOrderId') or 0),
            'mintsoft_order_number': primary_res.get('OrderNumber') or external_ref,
            'export_status': 'sent',
            'fulfilment_status': mapped_status,
            'mintsoft_status_id': status_id,
            'mintsoft_status_name': status_name,
            'part_number': 1,
            'total_parts': len(successful_results),
            'response_summary': json.dumps(sanitize_payload(results_list), indent=2),
            'last_error': False,
            'last_sync_at': fields.Datetime.now(),
        })
        self._update_order_line_item_ids(kays_order, primary_res.get('OrderItems') or [])

        # Handle Split Orders returned during order creation (never overwrite earlier Mintsoft IDs!)
        if len(successful_results) > 1:
            for idx, split_res in enumerate(successful_results[1:], start=2):
                split_ms_id = int(split_res.get('OrderId') or 0)
                if split_ms_id <= 0:
                    continue
                split_status_id = int(split_res.get('OrderStatusId') or 0)
                split_status_name = split_res.get('OrderStatus') or 'New'
                split_mapped_status = self.env['kays.mintsoft.status.mapping'].map_mintsoft_status(
                    instance, split_status_id, split_status_name
                )
                split_order_num = split_res.get('OrderNumber') or f"{external_ref}-P{idx}"
                existing_split = self.env['kays.mintsoft.order'].search([
                    ('instance_id', '=', instance.id),
                    ('mintsoft_order_id', '=', split_ms_id),
                ], limit=1)
                if not existing_split:
                    self.env['kays.mintsoft.order'].create({
                        'name': split_order_num,
                        'external_order_reference': external_ref,
                        'idempotency_key': f"{instance.id}:{external_ref}:split:{split_ms_id}",
                        'sale_order_id': sale_order.id,
                        'company_id': sale_order.company_id.id,
                        'instance_id': instance.id,
                        'partner_id': sale_order.partner_id.id,
                        'shipping_partner_id': (sale_order.partner_shipping_id or sale_order.partner_id).id,
                        'mintsoft_order_id': split_ms_id,
                        'dropship_order_id': int(split_res.get('DropShipOrderId') or 0),
                        'mintsoft_order_number': split_order_num,
                        'is_split_order': True,
                        'parent_kays_order_id': kays_order.id,
                        'part_number': idx,
                        'total_parts': len(successful_results),
                        'order_date': sale_order.date_order or fields.Datetime.now(),
                        'currency_id': sale_order.currency_id.id,
                        'order_total': sale_order.amount_total,
                        'export_status': 'sent',
                        'fulfilment_status': split_mapped_status,
                        'mintsoft_status_id': split_status_id,
                        'mintsoft_status_name': split_status_name,
                        'mintsoft_client_id': instance.mintsoft_client_id,
                        'mintsoft_warehouse_id': resolved['warehouse_id'],
                        'warehouse_mapping_id': resolved['warehouse_mapping_id'],
                        'carrier_mapping_id': resolved['carrier_mapping_id'],
                        'mintsoft_courier_service_id': resolved['courier_service_id'],
                        'mintsoft_courier_service_name': resolved['courier_service_name'],
                        'response_summary': json.dumps(sanitize_payload(split_res), indent=2),
                        'last_sync_at': fields.Datetime.now(),
                    })

        return kays_order

    @staticmethod
    def _update_order_line_item_ids(kays_order, result_items):
        if not isinstance(result_items, list):
            return
        sku_to_id = {}
        for ri in result_items:
            if isinstance(ri, dict) and ri.get('SKU') and ri.get('ID'):
                sku_to_id[str(ri['SKU']).strip()] = int(ri['ID'])
        for line in kays_order.line_ids:
            if line.sku in sku_to_id:
                line.mintsoft_order_item_id = sku_to_id[line.sku]

    def export_subscription_delivery(self, picking, instance=None, is_manual=False):
        """
        Validate, deduplicate, and export an eligible subscription delivery (stock.picking) to Mintsoft.
        Assigns a deterministic external reference with cycle suffix (e.g. S10327-R2)
        and creates/updates a kays.mintsoft.order record linked directly to this picking.
        """
        picking.ensure_one()
        sale_order = picking.sale_id
        if not sale_order:
            if is_manual:
                raise UserError(_("Delivery '%s' is not linked to a Sales Order / Subscription.") % picking.name)
            return False

        instance = instance or sale_order.kays_instance_id or picking.company_id._get_default_kays_instance(sale_order=sale_order)
        if not instance:
            if is_manual:
                raise UserError(_("No Kays Logistics connection is configured for company '%s'.") % picking.company_id.display_name)
            return False

        # Row-level lock on picking
        try:
            self.env.cr.execute(
                "SELECT id FROM stock_picking WHERE id = %s FOR UPDATE NOWAIT",
                [picking.id],
            )
        except Exception as lock_exc:
            raise UserError(
                _("Delivery '%s' is currently being processed by another worker.") % picking.name
            ) from lock_exc

        kays_order = picking.kays_order_id or self.env['kays.mintsoft.order'].search([
            ('picking_id', '=', picking.id),
            ('instance_id', '=', instance.id),
        ], limit=1)

        if kays_order:
            if kays_order.export_status == 'sent' and kays_order.mintsoft_order_id > 0:
                if is_manual:
                    raise UserError(
                        _("Delivery '%(pick)s' has already been exported to Mintsoft (ID: %(id)s).")
                        % {'pick': picking.name, 'id': kays_order.mintsoft_order_id}
                    )
                return kays_order

            if kays_order.export_status == 'unknown_outcome':
                reconciled = self.reconcile_unknown_outcome_order(kays_order)
                if reconciled or kays_order.export_status == 'unknown_outcome':
                    return kays_order

        # Calculate cycle number and external reference
        outgoing_pickings = sale_order.picking_ids.filtered(
            lambda p: p.picking_type_id.code == 'outgoing' and p.state != 'cancel'
        ).sorted('id')
        try:
            cycle_num = list(outgoing_pickings.ids).index(picking.id) + 1
        except ValueError:
            cycle_num = len(outgoing_pickings) or 1

        base_ref = self.compute_external_reference(sale_order, instance)
        if kays_order and kays_order.external_order_reference:
            external_ref = kays_order.external_order_reference
        else:
            if cycle_num > 1:
                external_ref = f"{base_ref}-R{cycle_num}"[:50]
            else:
                existing_base = self.env['kays.mintsoft.order'].search([
                    ('name', '=', base_ref),
                    ('instance_id', '=', instance.id),
                    ('picking_id', '!=', picking.id),
                ], limit=1)
                if existing_base or getattr(sale_order, 'is_subscription', False):
                    external_ref = f"{base_ref}-R1"[:50]
                else:
                    external_ref = base_ref[:50]

        val_res = self.validate_subscription_delivery(picking, instance, is_manual=is_manual, external_ref=external_ref)
        resolved = val_res['resolved']

        if kays_order:
            kays_order.write({
                'export_status': 'validating',
                'name': external_ref,
                'external_order_reference': external_ref,
                'is_subscription_delivery': True,
                'subscription_cycle': cycle_num,
                'picking_id': picking.id,
                'mintsoft_warehouse_id': resolved.get('warehouse_id', instance.mintsoft_warehouse_id),
                'warehouse_mapping_id': resolved.get('warehouse_mapping_id', False),
                'carrier_mapping_id': resolved.get('carrier_mapping_id', False),
                'mintsoft_courier_service_id': resolved.get('courier_service_id', 0),
                'mintsoft_courier_service_name': resolved.get('courier_service_name', ''),
            })
        else:
            kays_order = self.env['kays.mintsoft.order'].create({
                'name': external_ref,
                'external_order_reference': external_ref,
                'idempotency_key': f"{instance.id}:{external_ref}",
                'sale_order_id': sale_order.id,
                'picking_id': picking.id,
                'is_subscription_delivery': True,
                'subscription_cycle': cycle_num,
                'company_id': picking.company_id.id,
                'instance_id': instance.id,
                'partner_id': sale_order.partner_id.id,
                'shipping_partner_id': (picking.partner_id or sale_order.partner_shipping_id or sale_order.partner_id).id,
                'order_date': picking.scheduled_date or fields.Datetime.now(),
                'currency_id': sale_order.currency_id.id,
                'order_total': resolved.get('order_total', 0.0),
                'export_status': 'validating',
                'fulfilment_status': 'unknown',
                'mintsoft_client_id': instance.mintsoft_client_id,
                'mintsoft_warehouse_id': resolved.get('warehouse_id', instance.mintsoft_warehouse_id),
                'warehouse_mapping_id': resolved.get('warehouse_mapping_id', False),
                'carrier_mapping_id': resolved.get('carrier_mapping_id', False),
                'mintsoft_courier_service_id': resolved.get('courier_service_id', 0),
                'mintsoft_courier_service_name': resolved.get('courier_service_name', ''),
            })

        picking.kays_order_id = kays_order.id
        sale_order.kays_instance_id = instance.id

        if not val_res['valid']:
            err_msg = "\n".join(val_res['errors'])
            kays_order.write({
                'export_status': 'failed',
                'last_error': err_msg,
                'last_sync_at': fields.Datetime.now(),
            })
            if is_manual:
                raise UserError(_("Validation failed for delivery '%(pick)s':\n%(err)s") % {'pick': picking.name, 'err': err_msg})
            return kays_order

        payload = self.build_mintsoft_subscription_delivery_payload(picking, instance, validation_result=val_res)
        sanitized_preview = json.dumps(sanitize_payload(payload), indent=2)

        kays_order.line_ids.unlink()
        line_vals = []
        for item in resolved['items']:
            line_vals.append({
                'kays_order_id': kays_order.id,
                'sale_line_id': item.get('sale_line_id'),
                'product_id': item['product_id'],
                'product_mapping_id': item['product_mapping_id'],
                'sku': item['SKU'],
                'mintsoft_product_id': item['ProductId'],
                'quantity': item['Quantity'],
                'unit_price': item['UnitPrice'],
                'unit_vat': item['UnitPriceVat'],
                'discount': item['Discount'],
            })
        if line_vals:
            self.env['kays.mintsoft.order.line'].create(line_vals)

        kays_order.payload_preview = sanitized_preview

        if instance.environment == 'dry_run':
            dry_summary = {
                'mode': 'dry_run',
                'valid': True,
                'warnings': val_res['warnings'],
                'message': 'Dry Run validation succeeded for subscription delivery. No write request was sent to Mintsoft.',
                'external_reference': external_ref,
                'item_count': len(resolved['items']),
            }
            kays_order.write({
                'export_status': 'pending',
                'response_summary': json.dumps(dry_summary, indent=2),
                'last_error': False,
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        client = MintsoftClient(instance)

        remote_matches = self.check_remote_duplicate(client, instance, external_ref)
        if remote_matches:
            first_match = remote_matches[0]
            ms_id = first_match.get('ID') or first_match.get('OrderId')
            kays_order.write({
                'mintsoft_order_id': ms_id,
                'mintsoft_order_number': first_match.get('OrderNumber') or external_ref,
                'export_status': 'sent',
                'fulfilment_status': 'received',
                'response_summary': json.dumps(sanitize_payload(remote_matches), indent=2),
                'last_error': False,
                'last_sync_at': fields.Datetime.now(),
            })
            self.sync_order_status(kays_order)
            return kays_order

        kays_order.export_status = 'sending'
        try:
            response_data = client.request(
                'PUT',
                '/api/Order',
                json_payload=payload,
                operation='create_subscription_delivery',
                sale_order=sale_order,
                kays_order=kays_order,
            )
        except MintsoftAPIError as api_err:
            if api_err.unknown_outcome:
                kays_order.write({
                    'export_status': 'unknown_outcome',
                    'last_error': _(
                        "Subscription delivery submission outcome unknown after timeout/server error: %s."
                    ) % api_err.message,
                    'last_sync_at': fields.Datetime.now(),
                })
            else:
                kays_order.write({
                    'export_status': 'failed',
                    'last_error': api_err.message,
                    'retry_count': kays_order.retry_count + 1,
                    'last_sync_at': fields.Datetime.now(),
                })
            if is_manual:
                raise UserError(_("Mintsoft export failed: %s") % api_err.message) from api_err
            return kays_order
        except Exception as exc:
            kays_order.write({
                'export_status': 'failed',
                'last_error': str(exc),
                'last_sync_at': fields.Datetime.now(),
            })
            if is_manual:
                raise
            return kays_order

        results_list = response_data if isinstance(response_data, list) else ([response_data] if isinstance(response_data, dict) else [])
        if not results_list:
            kays_order.write({
                'export_status': 'failed',
                'last_error': _("Empty or unexpected response from Mintsoft PUT /api/Order."),
                'response_summary': json.dumps(sanitize_payload(response_data), indent=2),
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        successful_results = [
            r for r in results_list
            if isinstance(r, dict) and (r.get('Success') is True or (r.get('OrderId') and int(r.get('OrderId') or 0) > 0))
        ]

        if not successful_results:
            err_messages = [
                r.get('Message') or r.get('OrderStatus') or 'Subscription delivery creation rejected by Mintsoft'
                for r in results_list if isinstance(r, dict)
            ]
            kays_order.write({
                'export_status': 'failed',
                'last_error': "; ".join(err_messages),
                'response_summary': json.dumps(sanitize_payload(results_list), indent=2),
                'last_sync_at': fields.Datetime.now(),
            })
            return kays_order

        primary_res = successful_results[0]
        primary_ms_id = int(primary_res.get('OrderId') or 0)
        status_id = int(primary_res.get('OrderStatusId') or 0)
        status_name = primary_res.get('OrderStatus') or 'New'
        mapped_status = self.env['kays.mintsoft.status.mapping'].map_mintsoft_status(
            instance, status_id, status_name
        )

        kays_order.write({
            'mintsoft_order_id': primary_ms_id,
            'dropship_order_id': int(primary_res.get('DropShipOrderId') or 0),
            'mintsoft_order_number': primary_res.get('OrderNumber') or external_ref,
            'export_status': 'sent',
            'fulfilment_status': mapped_status,
            'mintsoft_status_id': status_id,
            'mintsoft_status_name': status_name,
            'part_number': 1,
            'total_parts': len(successful_results),
            'response_summary': json.dumps(sanitize_payload(results_list), indent=2),
            'last_error': False,
            'last_sync_at': fields.Datetime.now(),
        })
        self._update_order_line_item_ids(kays_order, primary_res.get('OrderItems') or [])
        return kays_order

    # -------------------------------------------------------------------------
    # Unknown Outcome Reconciliation
    # -------------------------------------------------------------------------
    def reconcile_unknown_outcome_order(self, kays_order):
        """
        Reconcile an order in 'unknown_outcome' state by querying Mintsoft for its deterministic reference.
        Returns True if order was found in Mintsoft (marked 'sent') or confirmed absent (marked 'failed' for retry).
        """
        instance = kays_order.instance_id
        if not instance.active or not instance.enable_integration:
            return False

        client = MintsoftClient(instance)
        try:
            matches = self.check_remote_duplicate(client, instance, kays_order.external_order_reference)
        except MintsoftAPIError as err:
            kays_order.write({
                'last_error': _("Reconciliation lookup failed: %s") % err.message,
                'last_sync_at': fields.Datetime.now(),
            })
            return False

        if matches:
            primary = matches[0]
            ms_id = int(primary.get('ID') or primary.get('OrderId') or 0)
            kays_order.write({
                'mintsoft_order_id': ms_id,
                'mintsoft_order_number': primary.get('OrderNumber') or kays_order.external_order_reference,
                'export_status': 'sent',
                'fulfilment_status': 'received',
                'last_error': False,
                'last_sync_at': fields.Datetime.now(),
            })
            self.sync_order_status(kays_order)
            return True

        # Confirmed absent in Mintsoft: safe to transition from unknown_outcome to failed so retry can proceed
        kays_order.write({
            'export_status': 'failed',
            'last_error': _("Reconciled with Mintsoft: order was not created during previous timeout. Ready for retry."),
            'last_sync_at': fields.Datetime.now(),
        })
        return False

    # -------------------------------------------------------------------------
    # Order Status & Split Synchronization
    # -------------------------------------------------------------------------
    def sync_order_status(self, kays_order):
        """Fetch latest order details and splits from Mintsoft and update Odoo status & shipments."""
        instance = kays_order.instance_id
        if not instance.active or not instance.enable_integration or not instance.enable_order_status_sync:
            return False
        if not kays_order.mintsoft_order_id or kays_order.mintsoft_order_id <= 0:
            return False

        client = MintsoftClient(instance)
        ord_data = client.request(
            'GET',
            f'/api/Order/{kays_order.mintsoft_order_id}',
            operation='get_order_status',
            sale_order=kays_order.sale_order_id,
            kays_order=kays_order,
            mintsoft_order_id=kays_order.mintsoft_order_id,
            allow_404=True,
        )
        if not isinstance(ord_data, dict):
            return False

        status_id = int(ord_data.get('OrderStatusId') or 0)
        status_rec = self.env['kays.mintsoft.status.mapping'].search([
            '|', ('instance_id', '=', instance.id), ('instance_id', '=', False),
            ('mintsoft_status_id', '=', status_id),
        ], limit=1)
        status_name = (
            ord_data.get('OrderStatus')
            or (status_rec.mintsoft_status_name if status_rec else '')
            or f"Status {status_id}"
        )
        mapped_status = self.env['kays.mintsoft.status.mapping'].map_mintsoft_status(
            instance, status_id, status_name
        )

        vals = {
            'mintsoft_status_id': status_id,
            'mintsoft_status_name': status_name,
            'fulfilment_status': mapped_status,
            'tracking_number': ord_data.get('TrackingNumber') or kays_order.tracking_number or '',
            'tracking_url': ord_data.get('TrackingURL') or kays_order.tracking_url or '',
            'mintsoft_courier_service_name': ord_data.get('CourierServiceName') or kays_order.mintsoft_courier_service_name or '',
            'part_number': int(ord_data.get('Part') or kays_order.part_number or 1),
            'total_parts': int(ord_data.get('NumberOfParts') or kays_order.total_parts or 1),
            'last_sync_at': fields.Datetime.now(),
        }
        if ord_data.get('DespatchDate'):
            vals['despatch_date'] = self._parse_mintsoft_datetime(ord_data.get('DespatchDate'))

        kays_order.write(vals)

        # Update line allocations if OrderItems returned
        if isinstance(ord_data.get('OrderItems'), list):
            self._sync_order_lines_from_mintsoft(kays_order, ord_data['OrderItems'])

        # Check for Order Splits via GET /api/Order/{id}/Splits
        if not kays_order.is_split_order:
            self._sync_order_splits(client, kays_order)

        # If order has tracking or is dispatched/partially dispatched, trigger shipment sync
        if instance.enable_shipment_sync and (
            mapped_status in ('dispatched', 'partially_dispatched', 'delivered')
            or vals.get('tracking_number')
        ):
            from .shipment_service import ShipmentService
            ShipmentService(self.env).sync_shipments_for_order(kays_order, order_payload=ord_data)

        return True

    def _sync_order_lines_from_mintsoft(self, kays_order, ms_items):
        for ms_item in ms_items:
            if not isinstance(ms_item, dict):
                continue
            sku = (ms_item.get('SKU') or '').strip()
            ms_item_id = int(ms_item.get('ID') or 0)
            line = kays_order.line_ids.filtered(
                lambda l: (ms_item_id and l.mintsoft_order_item_id == ms_item_id) or (sku and l.sku == sku)
            )[:1]
            if line:
                line.write({
                    'mintsoft_order_item_id': ms_item_id or line.mintsoft_order_item_id,
                    'allocated_qty': int(ms_item.get('Allocated') or 0),
                    'committed_qty': int(ms_item.get('Commited') or 0),
                    'backorder_qty': int(ms_item.get('OnBackOrder') or 0),
                })

    def _sync_order_splits(self, client, kays_order):
        try:
            splits = client.request(
                'GET',
                f'/api/Order/{kays_order.mintsoft_order_id}/Splits',
                operation='get_order_splits',
                sale_order=kays_order.sale_order_id,
                kays_order=kays_order,
                mintsoft_order_id=kays_order.mintsoft_order_id,
                allow_404=True,
            )
        except MintsoftAPIError:
            return

        if not isinstance(splits, list) or not splits:
            return

        for idx, sp in enumerate(splits, start=2):
            if not isinstance(sp, dict):
                continue
            split_ms_id = int(sp.get('SplitOrderId') or 0)
            if split_ms_id <= 0 or split_ms_id == kays_order.mintsoft_order_id:
                continue
            existing = self.env['kays.mintsoft.order'].search([
                ('instance_id', '=', kays_order.instance_id.id),
                ('mintsoft_order_id', '=', split_ms_id),
            ], limit=1)
            split_ord_obj = sp.get('SplitOrder') or {}
            split_num = split_ord_obj.get('OrderNumber') or f"{kays_order.external_order_reference}-SPLIT-{split_ms_id}"
            status_id = int(split_ord_obj.get('OrderStatusId') or 0)
            mapped_status = self.env['kays.mintsoft.status.mapping'].map_mintsoft_status(
                kays_order.instance_id, status_id, ''
            )
            if not existing:
                new_split = self.env['kays.mintsoft.order'].create({
                    'name': split_num,
                    'external_order_reference': kays_order.external_order_reference,
                    'idempotency_key': f"{kays_order.instance_id.id}:{kays_order.external_order_reference}:split:{split_ms_id}",
                    'sale_order_id': kays_order.sale_order_id.id,
                    'company_id': kays_order.company_id.id,
                    'instance_id': kays_order.instance_id.id,
                    'partner_id': kays_order.partner_id.id,
                    'shipping_partner_id': kays_order.shipping_partner_id.id,
                    'mintsoft_order_id': split_ms_id,
                    'mintsoft_order_number': split_num,
                    'is_split_order': True,
                    'parent_kays_order_id': kays_order.id,
                    'part_number': int(split_ord_obj.get('Part') or idx),
                    'total_parts': int(split_ord_obj.get('NumberOfParts') or (len(splits) + 1)),
                    'order_date': kays_order.order_date,
                    'currency_id': kays_order.currency_id.id,
                    'order_total': float(split_ord_obj.get('OrderValue') or 0.0),
                    'export_status': 'sent',
                    'fulfilment_status': mapped_status,
                    'mintsoft_status_id': status_id,
                    'mintsoft_client_id': kays_order.mintsoft_client_id,
                    'mintsoft_warehouse_id': kays_order.mintsoft_warehouse_id,
                    'tracking_number': split_ord_obj.get('TrackingNumber') or '',
                    'tracking_url': split_ord_obj.get('TrackingURL') or '',
                    'last_sync_at': fields.Datetime.now(),
                })
                self.sync_order_status(new_split)

    # -------------------------------------------------------------------------
    # Cancellations & Amendments (Part K)
    # -------------------------------------------------------------------------
    def request_order_cancellation(self, kays_order, user=None):
        """
        Safely request order cancellation in Mintsoft via GET /api/Order/{id}/Cancel.
        Fetches current live status first and blocks if already dispatched or cancelled.
        """
        instance = kays_order.instance_id
        if not instance.active or not instance.enable_integration:
            raise UserError(_("Integration is disabled on connection '%s'.") % instance.display_name)
        if not instance.enable_cancellation_sync:
            raise UserError(_("Cancellation Sync is disabled on connection '%s'.") % instance.display_name)
        if not kays_order.mintsoft_order_id or kays_order.mintsoft_order_id <= 0:
            raise UserError(_("Order has no Mintsoft Order ID to cancel."))
        if kays_order.cancellation_requested and kays_order.fulfilment_status == 'cancelled':
            raise UserError(_("Order '%s' is already cancelled in Mintsoft.") % kays_order.name)

        # Fetch live Mintsoft status first
        self.sync_order_status(kays_order)

        status_lower = (kays_order.mintsoft_status_name or '').lower()
        if (
            kays_order.fulfilment_status in ('dispatched', 'partially_dispatched', 'delivered', 'cancelled')
            or any(kw in status_lower for kw in NON_CANCELLABLE_STATUS_KEYWORDS)
        ):
            raise UserError(
                _("Order '%(name)s' cannot be cancelled because its current Mintsoft status is '%(status)s'.")
                % {'name': kays_order.name, 'status': kays_order.mintsoft_status_name or kays_order.fulfilment_status}
            )

        if instance.environment == 'dry_run':
            kays_order.write({
                'cancellation_requested': True,
                'cancellation_requested_at': fields.Datetime.now(),
                'cancellation_requested_by': (user or self.env.user).id,
                'cancellation_response': 'Dry Run mode: Cancellation validated locally; no mutating request sent.',
            })
            return True

        client = MintsoftClient(instance)
        res = client.request(
            'GET',
            f'/api/Order/{kays_order.mintsoft_order_id}/Cancel',
            operation='cancel_order',
            sale_order=kays_order.sale_order_id,
            kays_order=kays_order,
            mintsoft_order_id=kays_order.mintsoft_order_id,
        )

        success = isinstance(res, dict) and bool(res.get('Success', True))
        msg = res.get('Message') if isinstance(res, dict) else str(res)

        kays_order.write({
            'cancellation_requested': True,
            'cancellation_requested_at': fields.Datetime.now(),
            'cancellation_requested_by': (user or self.env.user).id,
            'cancellation_response': json.dumps(sanitize_payload(res), indent=2),
            'fulfilment_status': 'cancelled' if success else kays_order.fulfilment_status,
            'last_error': False if success else (msg or _('Cancellation rejected by Mintsoft.')),
            'last_sync_at': fields.Datetime.now(),
        })
        if not success:
            raise UserError(_("Mintsoft rejected order cancellation: %s") % (msg or _('Unknown reason')))
        return True

    def amend_order_in_mintsoft(self, kays_order, update_address=True, update_items=False):
        """
        Push delivery address / order header amendments (POST /api/Order/{id})
        and optional item line updates (PUT/POST/DELETE /api/Order/{id}/Items) when in valid fulfilment state.
        """
        instance = kays_order.instance_id
        if not instance.active or not instance.enable_integration:
            raise UserError(_("Integration is disabled on connection '%s'.") % instance.display_name)
        if not instance.enable_order_amendments:
            raise UserError(_("Order Amendments feature is disabled on connection '%s'.") % instance.display_name)
        if not kays_order.mintsoft_order_id or kays_order.mintsoft_order_id <= 0:
            raise UserError(_("Order has not been exported to Mintsoft yet."))

        # Check live Mintsoft status first
        self.sync_order_status(kays_order)
        status_lower = (kays_order.mintsoft_status_name or '').lower()
        if (
            kays_order.fulfilment_status in ('picked', 'packed', 'partially_dispatched', 'dispatched', 'delivered', 'cancelled')
            or any(kw in status_lower for kw in NON_AMENDABLE_STATUS_KEYWORDS)
        ):
            raise UserError(
                _("Order '%(name)s' cannot be amended in Mintsoft because its fulfilment status is '%(status)s'.")
                % {'name': kays_order.name, 'status': kays_order.mintsoft_status_name or kays_order.fulfilment_status}
            )

        val_res = self.validate_sale_order(kays_order.sale_order_id, instance, is_manual=True)
        if not val_res['valid']:
            raise UserError(_("Order amendment validation failed:\n%s") % "\n".join(val_res['errors']))

        full_payload = self.build_mintsoft_order_payload(kays_order.sale_order_id, instance, validation_result=val_res)
        header_payload = {
            k: v for k, v in full_payload.items()
            if k not in ('OrderItems', 'OrderNameValues', 'Tags', 'ConnectAction')
        }

        if instance.environment == 'dry_run':
            kays_order.write({
                'payload_preview': json.dumps(sanitize_payload(full_payload), indent=2),
                'response_summary': 'Dry Run mode: Amendment payload validated locally; no write request sent.',
                'last_sync_at': fields.Datetime.now(),
            })
            return True

        client = MintsoftClient(instance)
        if update_address:
            res = client.request(
                'POST',
                f'/api/Order/{kays_order.mintsoft_order_id}',
                json_payload=header_payload,
                operation='amend_order_header',
                sale_order=kays_order.sale_order_id,
                kays_order=kays_order,
                mintsoft_order_id=kays_order.mintsoft_order_id,
            )
            if isinstance(res, dict) and res.get('Success') is False:
                raise UserError(_("Mintsoft rejected order details amendment: %s") % (res.get('Message') or ''))

        if update_items:
            self._amend_order_items(client, kays_order, val_res['resolved']['items'])

        kays_order.write({
            'payload_preview': json.dumps(sanitize_payload(full_payload), indent=2),
            'last_sync_at': fields.Datetime.now(),
        })
        return True

    def _amend_order_items(self, client, kays_order, desired_items):
        remote_items = client.request(
            'GET',
            f'/api/Order/{kays_order.mintsoft_order_id}/Items',
            operation='get_order_items_for_amendment',
            sale_order=kays_order.sale_order_id,
            kays_order=kays_order,
            mintsoft_order_id=kays_order.mintsoft_order_id,
        ) or []

        remote_by_sku = {str(item.get('SKU') or '').strip(): item for item in remote_items if isinstance(item, dict)}
        desired_by_sku = {item['SKU']: item for item in desired_items}

        # Update existing or add new items
        for sku, d_item in desired_by_sku.items():
            item_body = {
                'SKU': d_item['SKU'],
                'Quantity': d_item['Quantity'],
                'UnitPrice': d_item['UnitPrice'],
                'UnitPriceVat': d_item['UnitPriceVat'],
                'Discount': d_item['Discount'],
                'Details': d_item['Details'],
            }
            if d_item.get('ProductId') and d_item['ProductId'] > 0:
                item_body['ProductId'] = d_item['ProductId']

            if sku in remote_by_sku:
                ms_item_id = int(remote_by_sku[sku].get('ID') or 0)
                if int(remote_by_sku[sku].get('Quantity') or 0) != d_item['Quantity']:
                    client.request(
                        'POST',
                        f'/api/Order/{kays_order.mintsoft_order_id}/Items/{ms_item_id}',
                        json_payload=item_body,
                        operation='amend_update_order_item',
                        sale_order=kays_order.sale_order_id,
                        kays_order=kays_order,
                        mintsoft_order_id=kays_order.mintsoft_order_id,
                    )
            else:
                client.request(
                    'PUT',
                    f'/api/Order/{kays_order.mintsoft_order_id}/Items',
                    json_payload=item_body,
                    operation='amend_add_order_item',
                    sale_order=kays_order.sale_order_id,
                    kays_order=kays_order,
                    mintsoft_order_id=kays_order.mintsoft_order_id,
                )

        # Delete removed items
        for sku, r_item in remote_by_sku.items():
            if sku not in desired_by_sku:
                ms_item_id = int(r_item.get('ID') or 0)
                if ms_item_id > 0:
                    client.request(
                        'DELETE',
                        f'/api/Order/{kays_order.mintsoft_order_id}/Items/{ms_item_id}',
                        operation='amend_delete_order_item',
                        sale_order=kays_order.sale_order_id,
                        kays_order=kays_order,
                        mintsoft_order_id=kays_order.mintsoft_order_id,
                    )

    @staticmethod
    def _parse_mintsoft_datetime(dt_str):
        if not dt_str or not isinstance(dt_str, str):
            return False
        cleaned = dt_str.strip().replace('T', ' ').split('.')[0].split('+')[0].rstrip('Z')
        try:
            return fields.Datetime.to_datetime(cleaned[:19])
        except Exception:
            return False
