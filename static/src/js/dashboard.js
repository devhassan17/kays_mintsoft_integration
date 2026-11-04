/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

export class KaysDashboard extends Component {
    static template = "kays_mintsoft_integration.KaysDashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        this.state = useState({
            filters: {
                company_id: "",
                instance_id: "",
                environment: "",
                fulfilment_status: "",
                date_from: "",
                date_to: "",
            },
            data: {
                kpis: {
                    total_orders: 0,
                    pending_export: 0,
                    exported_today: 0,
                    processing: 0,
                    awaiting_dispatch: 0,
                    dispatched_today: 0,
                    delivered: 0,
                    cancelled: 0,
                    failed_orders: 0,
                    awaiting_retry: 0,
                    last_stock_sync: "Never",
                    next_stock_sync: "Not Scheduled",
                    stock_diff_count: 0,
                    connection_status: "untested",
                },
                orders_by_status: [],
                daily_exports: [],
                response_times: [],
                recent_failures: [],
                latest_shipments: [],
                stock_discrepancies: [],
                connection_health: [],
                filter_options: {
                    companies: [],
                    instances: [],
                },
            },
        });

        onWillStart(async () => {
            await this.loadDashboardData();
        });
    }

    async loadDashboardData() {
        const result = await this.orm.call(
            "kays.mintsoft.instance",
            "get_dashboard_data",
            [this.state.filters]
        );
        if (result) {
            this.state.data = result;
        }
    }

    async onFilterChange() {
        await this.loadDashboardData();
    }

    openOrders(extraDomain = []) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Kays Orders",
            res_model: "kays.mintsoft.order",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain: extraDomain,
        });
    }

    onViewFailedOrders() {
        this.openOrders([["export_status", "in", ["failed", "unknown_outcome"]]]);
    }

    onOpenFailedJobs() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Failed & Retry Sync Jobs",
            res_model: "kays.mintsoft.sync.job",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
            domain: [["state", "in", ["failed", "retry_scheduled", "unknown_outcome", "dead_letter"]]],
        });
    }

    onOpenDiscrepancies() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Stock Differences",
            res_model: "kays.mintsoft.stock.discrepancy",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    onTestConnection() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Kays Connections",
            res_model: "kays.mintsoft.instance",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }

    onSyncOrdersNow() {
        this.action.doAction("kays_mintsoft_integration.action_kays_manual_sync_wizard", {
            additionalContext: { default_sync_operation: "orders_export" },
        });
    }

    onSyncStockNow() {
        this.action.doAction("kays_mintsoft_integration.action_kays_manual_sync_wizard", {
            additionalContext: { default_sync_operation: "stock_full" },
        });
    }

    onRefreshTracking() {
        this.action.doAction("kays_mintsoft_integration.action_kays_manual_sync_wizard", {
            additionalContext: { default_sync_operation: "tracking" },
        });
    }

    onRetryFailedJobs() {
        this.action.doAction("kays_mintsoft_integration.action_kays_retry_wizard");
    }

    onOpenConfiguration() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Kays Connections",
            res_model: "kays.mintsoft.instance",
            view_mode: "list,form",
            views: [[false, "list"], [false, "form"]],
        });
    }
}

registry.category("actions").add("kays_mintsoft_integration.KaysDashboard", KaysDashboard);
