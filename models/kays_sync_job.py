import json
import logging
import time
import uuid
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.mintsoft_client import MintsoftAPIError
from ..services.order_service import OrderService
from ..services.product_service import ProductService
from ..services.inventory_service import InventoryService
from ..services.shipment_service import ShipmentService
from ..services.tracking_service import TrackingService
from ..services.return_service import ReturnService

_logger = logging.getLogger(__name__)


class KaysMintsoftJobSchedule(models.Model):
    _name = 'kays.mintsoft.job.schedule'
    _description = 'Kays Logistics Scheduled Job Task Configuration & Telemetry'
    _order = 'sequence, id'

    _unique_task_code = models.Constraint(
        'UNIQUE(task_code)',
        'Scheduled task code must be unique!',
    )

    name = fields.Char(string='Scheduled Task Name', required=True)
    sequence = fields.Integer(default=10)
    task_code = fields.Selection(
        selection=[
            ('pending_order_export', 'Pending Order Export'),
            ('order_reconciliation', 'Existing Order Reconciliation'),
            ('order_status_sync', 'Order Status Sync'),
            ('stock_sync', 'Stock Sync'),
            ('tracking_sync', 'Tracking Sync'),
            ('product_sync', 'Product Sync'),
            ('failed_job_retry', 'Failed Job Retry'),
            ('webhook_processing', 'Webhook Event Processing'),
            ('return_sync', 'Return Sync'),
            ('log_cleanup', 'Log Cleanup'),
        ],
        string='Task Type',
        required=True,
        index=True,
    )
    active = fields.Boolean(string='Enabled', default=True)
    interval_minutes = fields.Integer(string='Frequency (Minutes)', default=10, required=True)
    batch_size = fields.Integer(string='Batch Size', default=50, required=True)
    retry_policy = fields.Selection(
        selection=[
            ('exponential_backoff', 'Exponential Backoff + Retry-After'),
            ('fixed_interval', 'Fixed Interval'),
            ('manual_only', 'Manual Retry Only'),
        ],
        string='Retry Policy',
        default='exponential_backoff',
        required=True,
    )
    max_retries = fields.Integer(string='Max Retries', default=5)
    base_backoff_seconds = fields.Integer(string='Base Backoff (Seconds)', default=60)

    last_execution_at = fields.Datetime(string='Last Execution', readonly=True)
    last_success_at = fields.Datetime(string='Last Success', readonly=True)
    next_execution_at = fields.Datetime(
        string='Next Execution',
        compute='_compute_next_execution_at',
    )
    last_processed_count = fields.Integer(string='Last Processed', readonly=True)
    last_failed_count = fields.Integer(string='Last Failed', readonly=True)
    total_processed_count = fields.Integer(string='Total Processed', readonly=True)
    total_failed_count = fields.Integer(string='Total Failed', readonly=True)
    last_duration_ms = fields.Integer(string='Last Duration (ms)', readonly=True)

    @api.depends('last_execution_at', 'interval_minutes', 'active')
    def _compute_next_execution_at(self):
        for rec in self:
            if not rec.active:
                rec.next_execution_at = False
            elif rec.last_execution_at:
                rec.next_execution_at = rec.last_execution_at + timedelta(minutes=max(1, rec.interval_minutes))
            else:
                rec.next_execution_at = fields.Datetime.now()

    def action_run_now(self):
        """Manually trigger this scheduled task immediately."""
        self.ensure_one()
        self.env['kays.mintsoft.sync.job']._execute_scheduled_task(self.task_code, force=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Scheduled Task Executed'),
                'message': _('Completed task "%s" (Processed: %d, Failed: %d).') % (
                    self.name, self.last_processed_count, self.last_failed_count
                ),
                'type': 'success' if self.last_failed_count == 0 else 'warning',
            },
        }


class KaysMintsoftSyncJob(models.Model):
    _name = 'kays.mintsoft.sync.job'
    _description = 'Kays Logistics Durable Synchronization Job & Retry Queue'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'priority desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Job Reference', required=True, index=True)
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
        ondelete='cascade',
        index=True,
        check_company=True,
    )
    job_type = fields.Selection(
        selection=[
            ('order_export', 'Order Export'),
            ('order_reconcile', 'Unknown Outcome Reconciliation'),
            ('order_status_sync', 'Order Status Sync'),
            ('stock_sync', 'Stock Sync'),
            ('tracking_sync', 'Tracking & Shipment Sync'),
            ('product_sync', 'Product & SKU Sync'),
            ('webhook_process', 'Webhook Event Processing'),
            ('return_sync', 'Return Sync'),
            ('order_cancel', 'Order Cancellation'),
            ('order_amend', 'Order Amendment'),
        ],
        string='Job Type',
        required=True,
        index=True,
        tracking=True,
    )
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('completed', 'Completed'),
            ('retry_scheduled', 'Awaiting Retry'),
            ('unknown_outcome', 'Unknown Outcome (Needs Reconciliation)'),
            ('failed', 'Failed'),
            ('dead_letter', 'Dead-Letter (Max Retries Reached)'),
            ('cancelled', 'Cancelled'),
        ],
        string='Status',
        default='pending',
        required=True,
        index=True,
        tracking=True,
    )
    priority = fields.Integer(string='Priority', default=10, index=True)

    kays_order_id = fields.Many2one('kays.mintsoft.order', string='Kays Order', ondelete='set null', index=True)
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', ondelete='set null', index=True)
    webhook_event_id = fields.Many2one('kays.mintsoft.webhook.event', string='Webhook Event', ondelete='set null', index=True)
    return_id = fields.Many2one('kays.mintsoft.return', string='Return Record', ondelete='set null', index=True)

    payload_json = fields.Text(string='Job Parameters (JSON)')
    result_json = fields.Text(string='Execution Result (JSON)')

    retry_count = fields.Integer(string='Retry Count', default=0)
    max_retries = fields.Integer(string='Max Retries', default=5)
    base_backoff_seconds = fields.Integer(string='Base Backoff (s)', default=60)
    retry_after_seconds = fields.Integer(string='Retry-After Header (s)', default=0)
    next_retry_at = fields.Datetime(string='Next Retry At', index=True)
    is_retryable = fields.Boolean(string='Retryable', default=True)
    error_classification = fields.Selection(
        selection=[
            ('none', 'None'),
            ('transient', 'Transient Network / Server Error'),
            ('rate_limited', 'HTTP 429 Rate Limited'),
            ('timeout_unknown', 'Timeout with Unknown Outcome'),
            ('auth_error', 'Authentication / Permission Error'),
            ('validation_error', 'Validation / Business Rule Error'),
            ('permanent', 'Permanent Failure'),
        ],
        string='Error Classification',
        default='none',
    )
    last_error = fields.Text(string='Last Error')
    started_at = fields.Datetime(string='Started At')
    completed_at = fields.Datetime(string='Completed At')
    duration_ms = fields.Integer(string='Duration (ms)', default=0)
    correlation_id = fields.Char(string='Correlation ID', default=lambda self: str(uuid.uuid4()), index=True)

    # -------------------------------------------------------------------------
    # Queue Creation & Execution Engine
    # -------------------------------------------------------------------------
    @api.model
    def enqueue_job(
        self,
        instance,
        job_type,
        kays_order=None,
        sale_order=None,
        webhook_event=None,
        kays_return=None,
        payload=None,
        priority=10,
    ):
        """Idempotently enqueue a durable synchronization job, avoiding duplicate active jobs."""
        domain = [
            ('instance_id', '=', instance.id),
            ('job_type', '=', job_type),
            ('state', 'in', ['pending', 'running', 'retry_scheduled']),
        ]
        if kays_order:
            domain.append(('kays_order_id', '=', kays_order.id))
        elif sale_order:
            domain.append(('sale_order_id', '=', sale_order.id))
        elif webhook_event:
            domain.append(('webhook_event_id', '=', webhook_event.id))
        elif kays_return:
            domain.append(('return_id', '=', kays_return.id))

        existing = self.search(domain, limit=1)
        if existing:
            return existing

        ref_suffix = (
            kays_order.name if kays_order
            else (sale_order.name if sale_order else (webhook_event.name if webhook_event else job_type.upper()))
        )
        return self.create({
            'name': f"JOB/{job_type.upper()}/{ref_suffix}",
            'company_id': instance.company_id.id,
            'instance_id': instance.id,
            'job_type': job_type,
            'state': 'pending',
            'priority': priority,
            'kays_order_id': kays_order.id if kays_order else False,
            'sale_order_id': sale_order.id if sale_order else (kays_order.sale_order_id.id if kays_order else False),
            'webhook_event_id': webhook_event.id if webhook_event else False,
            'return_id': kays_return.id if kays_return else False,
            'payload_json': json.dumps(payload or {}, default=str),
        })

    def compute_backoff_seconds(self):
        """Calculate exponential backoff duration, respecting HTTP 429 Retry-After header."""
        self.ensure_one()
        if self.retry_after_seconds and self.retry_after_seconds > 0:
            return max(self.retry_after_seconds, 10)
        base = max(10, self.base_backoff_seconds or 60)
        exp = min(self.retry_count, 8)
        return min(base * (2 ** exp), 7200)

    def action_execute_job(self, is_manual=False):
        """Execute one or more queued sync jobs with full error classification and backoff scheduling."""
        for job in self:
            job._run_single_job(is_manual=is_manual)
        return True

    def action_retry_job_manually(self):
        """Manual retry action from UI."""
        for job in self:
            if job.state == 'completed':
                continue
            job.write({
                'state': 'pending',
                'is_retryable': True,
                'next_retry_at': fields.Datetime.now(),
            })
            job._run_single_job(is_manual=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Job Execution Triggered'),
                'message': _('Processed %d job(s).') % len(self),
                'type': 'info',
            },
        }

    def action_cancel_job(self):
        self.filtered(lambda j: j.state != 'completed').write({'state': 'cancelled'})

    def _run_single_job(self, is_manual=False):
        self.ensure_one()
        instance = self.instance_id.with_company(self.company_id)

        if not instance.company_id.kays_enabled or not instance.active or not instance.enable_integration:
            self.write({
                'state': 'failed',
                'error_classification': 'validation_error',
                'is_retryable': False,
                'last_error': _("Company or connection integration is disabled."),
            })
            return False

        # Acquire row-level lock on this job
        try:
            self.env.cr.execute(
                "SELECT id FROM kays_mintsoft_sync_job WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Exception:
            return False

        start_ts = time.monotonic()
        self.write({
            'state': 'running',
            'started_at': fields.Datetime.now(),
        })

        try:
            result = self._dispatch_job_handler(instance, is_manual=is_manual)
            duration_ms = int((time.monotonic() - start_ts) * 1000)

            # Check if an order export resulted in unknown_outcome or failed state on kays_order
            if self.job_type == 'order_export' and self.kays_order_id:
                if self.kays_order_id.export_status == 'unknown_outcome':
                    self.write({
                        'state': 'unknown_outcome',
                        'error_classification': 'timeout_unknown',
                        'is_retryable': False,
                        'last_error': self.kays_order_id.last_error,
                        'duration_ms': duration_ms,
                    })
                    return False
                if self.kays_order_id.export_status == 'failed':
                    raise MintsoftAPIError(
                        self.kays_order_id.last_error or _("Order export validation/API failure."),
                        error_classification='validation_error',
                        is_retryable=False,
                    )

            self.write({
                'state': 'completed',
                'error_classification': 'none',
                'last_error': False,
                'result_json': json.dumps(result or {'ok': True}, default=str),
                'completed_at': fields.Datetime.now(),
                'duration_ms': duration_ms,
            })
            return True

        except MintsoftAPIError as api_err:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            self._handle_job_failure(
                error_msg=api_err.message,
                classification=api_err.error_classification,
                is_retryable=api_err.is_retryable,
                retry_after=api_err.retry_after,
                unknown_outcome=api_err.unknown_outcome,
                duration_ms=duration_ms,
            )
            return False

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            self._handle_job_failure(
                error_msg=str(exc),
                classification='validation_error' if isinstance(exc, UserError) else 'transient',
                is_retryable=not isinstance(exc, UserError),
                retry_after=0,
                unknown_outcome=False,
                duration_ms=duration_ms,
            )
            return False

    def _dispatch_job_handler(self, instance, is_manual=False):
        if self.job_type == 'order_export':
            so = self.sale_order_id or (self.kays_order_id.sale_order_id if self.kays_order_id else False)
            if not so:
                raise UserError(_("Job has no linked Sales Order."))
            ko = OrderService(self.env).export_sale_order(so, instance=instance, is_manual=is_manual)
            self.kays_order_id = ko.id
            return {'kays_order_id': ko.id, 'export_status': ko.export_status, 'mintsoft_order_id': ko.mintsoft_order_id}

        if self.job_type == 'order_reconcile':
            if not self.kays_order_id:
                raise UserError(_("Reconciliation job requires a linked Kays Order."))
            found = OrderService(self.env).reconcile_unknown_outcome_order(self.kays_order_id)
            return {'reconciled': found, 'export_status': self.kays_order_id.export_status}

        if self.job_type == 'order_status_sync':
            if self.kays_order_id:
                ok = OrderService(self.env).sync_order_status(self.kays_order_id)
                return {'synced': ok, 'fulfilment_status': self.kays_order_id.fulfilment_status}
            return {'synced': False}

        if self.job_type == 'stock_sync':
            snap = InventoryService(self.env).run_stock_sync(instance, sync_mode='full', is_manual=is_manual)
            return {'snapshot_id': snap.id, 'checked': snap.total_skus_checked, 'adjusted': snap.adjusted_count}

        if self.job_type == 'tracking_sync':
            if self.kays_order_id:
                ShipmentService(self.env).sync_shipments_for_order(self.kays_order_id)
            cnt = TrackingService(self.env).sync_tracking_events(instance)
            return {'events_processed': cnt}

        if self.job_type == 'product_sync':
            stats = ProductService(self.env).fetch_and_map_products(instance)
            return stats

        if self.job_type == 'webhook_process':
            if not self.webhook_event_id:
                raise UserError(_("Webhook processing job has no linked Webhook Event."))
            self.webhook_event_id._process_webhook_payload()
            return {'webhook_state': self.webhook_event_id.state}

        if self.job_type == 'return_sync':
            if self.return_id:
                if not self.return_id.mintsoft_return_id:
                    ReturnService(self.env).create_return_in_mintsoft(self.return_id)
                else:
                    ReturnService(self.env).sync_return_status(self.return_id)
                return {'return_id': self.return_id.id, 'state': self.return_id.state}
            cnt = ReturnService(self.env).poll_returns_list(instance)
            return {'returns_synced': cnt}

        if self.job_type == 'order_cancel':
            if not self.kays_order_id:
                raise UserError(_("Cancellation job requires a linked Kays Order."))
            OrderService(self.env).request_order_cancellation(self.kays_order_id)
            return {'cancelled': True}

        if self.job_type == 'order_amend':
            if not self.kays_order_id:
                raise UserError(_("Amendment job requires a linked Kays Order."))
            OrderService(self.env).amend_order_in_mintsoft(self.kays_order_id, update_address=True, update_items=True)
            return {'amended': True}

        return {'ok': True}

    def _handle_job_failure(
        self,
        error_msg,
        classification,
        is_retryable,
        retry_after,
        unknown_outcome,
        duration_ms,
    ):
        new_retry_count = self.retry_count + 1
        self.retry_after_seconds = retry_after or 0

        # CRITICAL SAFETY RULE: Never automatically retry order creation after an unknown timeout!
        if unknown_outcome or classification == 'timeout_unknown':
            self.write({
                'state': 'unknown_outcome',
                'error_classification': 'timeout_unknown',
                'is_retryable': False,
                'retry_count': new_retry_count,
                'last_error': error_msg,
                'duration_ms': duration_ms,
            })
            return

        can_auto_retry = (
            is_retryable
            and self.instance_id.enable_auto_retry
            and new_retry_count <= self.max_retries
        )

        if can_auto_retry:
            backoff_sec = self.compute_backoff_seconds()
            next_dt = fields.Datetime.now() + timedelta(seconds=backoff_sec)
            self.write({
                'state': 'retry_scheduled',
                'error_classification': classification,
                'is_retryable': True,
                'retry_count': new_retry_count,
                'next_retry_at': next_dt,
                'last_error': error_msg,
                'duration_ms': duration_ms,
            })
            if self.kays_order_id:
                self.kays_order_id.next_retry_at = next_dt
        else:
            final_state = 'dead_letter' if (is_retryable and new_retry_count > self.max_retries) else 'failed'
            self.write({
                'state': final_state,
                'error_classification': classification,
                'is_retryable': is_retryable,
                'retry_count': new_retry_count,
                'last_error': error_msg,
                'duration_ms': duration_ms,
            })

    # -------------------------------------------------------------------------
    # Odoo 19 Scheduled Cron Entrypoints (Part L - Section 20)
    # -------------------------------------------------------------------------
    @api.model
    def _get_active_polling_instances(self):
        """Return active connections where both company Kays integration and scheduled polling are enabled."""
        return self.env['kays.mintsoft.instance'].search([
            ('active', '=', True),
            ('enable_integration', '=', True),
            ('enable_scheduled_polling', '=', True),
            ('company_id.kays_enabled', '=', True),
        ])

    @api.model
    def _execute_scheduled_task(self, task_code, force=False):
        sched = self.env['kays.mintsoft.job.schedule'].search([('task_code', '=', task_code)], limit=1)
        if sched and not sched.active and not force:
            return

        batch_size = sched.batch_size if sched else 50
        start_time = time.monotonic()
        processed = 0
        failed = 0

        try:
            processed, failed = self._run_task_by_code(task_code, batch_size=batch_size)
        except Exception as exc:
            _logger.error("Scheduled task %s failed: %s", task_code, exc)
            failed += 1

        duration_ms = int((time.monotonic() - start_time) * 1000)
        if sched:
            now = fields.Datetime.now()
            vals = {
                'last_execution_at': now,
                'last_processed_count': processed,
                'last_failed_count': failed,
                'total_processed_count': sched.total_processed_count + processed,
                'total_failed_count': sched.total_failed_count + failed,
                'last_duration_ms': duration_ms,
            }
            if failed == 0:
                vals['last_success_at'] = now
            sched.write(vals)

    @api.model
    def _run_task_by_code(self, task_code, batch_size=50):
        processed = 0
        failed = 0
        instances = self._get_active_polling_instances()

        if task_code == 'pending_order_export':
            for inst in instances.filtered('enable_auto_order_export'):
                inst_ctx = inst.with_company(inst.company_id)
                pending_orders = self.env['kays.mintsoft.order'].with_company(inst.company_id).search([
                    ('instance_id', '=', inst.id),
                    ('export_status', 'in', ['pending', 'queued']),
                    ('is_split_order', '=', False),
                ], limit=batch_size, order='id asc')
                svc = OrderService(self.env)
                for ko in pending_orders:
                    try:
                        svc.export_sale_order(ko.sale_order_id, instance=inst_ctx, is_manual=False)
                        if ko.export_status == 'sent':
                            processed += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1

        elif task_code == 'order_reconciliation':
            for inst in instances:
                unknown_orders = self.env['kays.mintsoft.order'].with_company(inst.company_id).search([
                    ('instance_id', '=', inst.id),
                    ('export_status', '=', 'unknown_outcome'),
                ], limit=batch_size)
                svc = OrderService(self.env)
                for ko in unknown_orders:
                    try:
                        svc.reconcile_unknown_outcome_order(ko)
                        processed += 1
                    except Exception:
                        failed += 1

        elif task_code == 'order_status_sync':
            for inst in instances.filtered('enable_order_status_sync'):
                active_orders = self.env['kays.mintsoft.order'].with_company(inst.company_id).search([
                    ('instance_id', '=', inst.id),
                    ('export_status', '=', 'sent'),
                    ('mintsoft_order_id', '>', 0),
                    ('fulfilment_status', 'not in', ['delivered', 'cancelled']),
                ], limit=batch_size, order='last_sync_at asc nulls first')
                svc = OrderService(self.env)
                for ko in active_orders:
                    try:
                        if svc.sync_order_status(ko):
                            processed += 1
                    except Exception:
                        failed += 1

        elif task_code == 'stock_sync':
            for inst in instances.filtered(lambda i: i.enable_inventory_sync and i.inventory_authority != 'disabled'):
                try:
                    InventoryService(self.env).run_stock_sync(inst, sync_mode='full', is_manual=False)
                    processed += 1
                except Exception:
                    failed += 1

        elif task_code == 'tracking_sync':
            for inst in instances.filtered('enable_tracking_sync'):
                try:
                    cnt = TrackingService(self.env).sync_tracking_events(inst)
                    processed += max(1, cnt)
                except Exception:
                    failed += 1

        elif task_code == 'product_sync':
            for inst in instances.filtered('enable_product_import'):
                try:
                    ProductService(self.env).fetch_and_map_products(inst)
                    processed += 1
                except Exception:
                    failed += 1

        elif task_code == 'failed_job_retry':
            now = fields.Datetime.now()
            due_jobs = self.search([
                ('state', 'in', ['pending', 'retry_scheduled']),
                ('is_retryable', '=', True),
                '|',
                ('next_retry_at', '=', False),
                ('next_retry_at', '<=', now),
            ], limit=batch_size, order='priority desc, id asc')
            for job in due_jobs:
                if job._run_single_job(is_manual=False):
                    processed += 1
                else:
                    failed += 1

        elif task_code == 'webhook_processing':
            events = self.env['kays.mintsoft.webhook.event'].search([
                ('state', 'in', ['received', 'queued']),
            ], limit=batch_size, order='received_at asc')
            for ev in events:
                try:
                    ev._process_webhook_payload()
                    if ev.state == 'processed':
                        processed += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1

        elif task_code == 'return_sync':
            for inst in instances.filtered('enable_returns_sync'):
                try:
                    cnt = ReturnService(self.env).poll_returns_list(inst)
                    processed += max(1, cnt)
                except Exception:
                    failed += 1

        elif task_code == 'log_cleanup':
            processed = self.env['kays.mintsoft.log']._cleanup_expired_logs()

        return processed, failed

    # Cron Entrypoint Methods referenced in data/ir_cron.xml
    @api.model
    def _cron_pending_order_export(self):
        self._execute_scheduled_task('pending_order_export')

    @api.model
    def _cron_order_reconciliation(self):
        self._execute_scheduled_task('order_reconciliation')

    @api.model
    def _cron_order_status_sync(self):
        self._execute_scheduled_task('order_status_sync')

    @api.model
    def _cron_stock_sync(self):
        self._execute_scheduled_task('stock_sync')

    @api.model
    def _cron_tracking_sync(self):
        self._execute_scheduled_task('tracking_sync')

    @api.model
    def _cron_product_sync(self):
        self._execute_scheduled_task('product_sync')

    @api.model
    def _cron_failed_job_retry(self):
        self._execute_scheduled_task('failed_job_retry')

    @api.model
    def _cron_webhook_processing(self):
        self._execute_scheduled_task('webhook_processing')

    @api.model
    def _cron_return_sync(self):
        self._execute_scheduled_task('return_sync')

    @api.model
    def _cron_log_cleanup(self):
        self._execute_scheduled_task('log_cleanup')
