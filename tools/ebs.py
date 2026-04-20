from __future__ import annotations
import re
from .base import BaseTool
from .oracle import _get_conn, _safe_val

EBS_TABLES = {
    "PO": {
        "desc": "Purchasing — Purchase Orders, Requisitions",
        "tables": {
            "po_headers_all": "po_header_id, segment1, vendor_id, org_id, authorization_status, approved_flag, type_lookup_code",
            "po_lines_all": "po_line_id, po_header_id, item_id, unit_price, quantity, line_num, item_description",
            "po_distributions_all": "po_distribution_id, po_line_id, code_combination_id, quantity_ordered, quantity_delivered",
            "po_line_locations_all": "line_location_id, po_line_id, quantity, quantity_received, need_by_date",
            "po_requisition_headers_all": "requisition_header_id, segment1, authorization_status",
            "po_requisition_lines_all": "requisition_line_id, requisition_header_id, item_id, unit_price, quantity",
        },
        "joins": [
            "po_headers_all h JOIN po_lines_all l ON h.po_header_id = l.po_header_id",
            "po_headers_all h JOIN ap_suppliers s ON h.vendor_id = s.vendor_id",
        ],
    },
    "AP": {
        "desc": "Accounts Payable — Invoices, Payments, Suppliers",
        "tables": {
            "ap_invoices_all": "invoice_id, invoice_num, vendor_id, invoice_amount, payment_status_flag, org_id",
            "ap_invoice_lines_all": "invoice_id, line_number, amount, line_type_lookup_code",
            "ap_invoice_distributions_all": "invoice_id, distribution_line_number, dist_code_combination_id, amount",
            "ap_payment_schedules_all": "invoice_id, payment_num, due_date, amount_remaining",
            "ap_checks_all": "check_id, check_number, amount, check_date, vendor_id",
            "ap_suppliers": "vendor_id, vendor_name, segment1, enabled_flag",
            "ap_supplier_sites_all": "vendor_site_id, vendor_id, vendor_site_code, org_id",
        },
        "joins": [
            "ap_invoices_all i JOIN ap_suppliers s ON i.vendor_id = s.vendor_id",
            "ap_invoices_all i JOIN ap_invoice_lines_all il ON i.invoice_id = il.invoice_id",
        ],
    },
    "AR": {
        "desc": "Accounts Receivable — Customer Transactions, Receipts",
        "tables": {
            "ra_customer_trx_all": "customer_trx_id, trx_number, bill_to_customer_id, org_id, trx_date",
            "ra_customer_trx_lines_all": "customer_trx_line_id, customer_trx_id, line_number, unit_selling_price",
            "hz_parties": "party_id, party_name, party_type",
            "hz_cust_accounts": "cust_account_id, party_id, account_number, status",
        },
        "joins": [
            "ra_customer_trx_all t JOIN hz_cust_accounts ca ON t.bill_to_customer_id = ca.cust_account_id JOIN hz_parties p ON ca.party_id = p.party_id",
        ],
    },
    "GL": {
        "desc": "General Ledger — Journals, Balances",
        "tables": {
            "gl_je_headers": "je_header_id, period_name, status, name, je_source, je_category",
            "gl_je_lines": "je_header_id, je_line_num, code_combination_id, entered_dr, entered_cr",
            "gl_code_combinations": "code_combination_id, segment1..segmentN, enabled_flag",
            "gl_balances": "code_combination_id, period_name, currency_code, period_net_dr, period_net_cr",
        },
        "joins": [
            "gl_je_headers h JOIN gl_je_lines l ON h.je_header_id = l.je_header_id",
            "gl_je_lines l JOIN gl_code_combinations cc ON l.code_combination_id = cc.code_combination_id",
        ],
    },
    "INV": {
        "desc": "Inventory — Items, On-hand, Transactions",
        "tables": {
            "mtl_system_items_b": "inventory_item_id, organization_id, segment1, description, primary_uom_code",
            "mtl_onhand_quantities": "inventory_item_id, organization_id, subinventory_code, transaction_quantity",
            "mtl_material_transactions": "transaction_id, inventory_item_id, transaction_type_id, transaction_quantity",
        },
        "joins": [
            "mtl_system_items_b i JOIN mtl_onhand_quantities oh ON i.inventory_item_id = oh.inventory_item_id AND i.organization_id = oh.organization_id",
        ],
    },
    "COMMON": {
        "desc": "Shared/Foundation tables",
        "tables": {
            "fnd_user": "user_id, user_name, email_address",
            "hr_all_organization_units": "organization_id, name, type",
            "per_all_people_f": "person_id, full_name, employee_number",
            "fnd_lookup_values": "lookup_type, lookup_code, meaning, enabled_flag",
        },
        "joins": [],
    },
}


class EBSModuleGuideTool(BaseTool):
    name = "ebs_module_guide"
    description = "Get Oracle EBS module knowledge: table names, key columns, common JOINs. Use BEFORE writing any EBS SQL."
    parameters = {
        "type": "object",
        "properties": {
            "module": {
                "type": "string",
                "enum": ["PO", "AP", "AR", "GL", "INV", "COMMON", "ALL"],
                "description": "EBS module code",
            },
        },
        "required": ["module"],
    }

    async def execute(self, module: str) -> str:
        module = module.upper()
        if module == "ALL":
            modules = EBS_TABLES.keys()
        elif module in EBS_TABLES:
            modules = [module]
        else:
            return f"Unknown module: {module}. Valid: PO, AP, AR, GL, INV, COMMON, ALL"

        lines = []
        for mod in modules:
            info = EBS_TABLES[mod]
            lines.append(f"## {mod} — {info['desc']}")
            for tbl, cols in info["tables"].items():
                lines.append(f"  {tbl}: {cols}")
            if info["joins"]:
                lines.append("  Joins:")
                for j in info["joins"]:
                    lines.append(f"    {j}")
            lines.append("")
        return "\n".join(lines)


class EBSConcurrentStatusTool(BaseTool):
    name = "ebs_concurrent_status"
    description = (
        "Show Oracle EBS concurrent requests with phase/status filters "
        "(pending/running/completed/error), program filter, and lookback window."
    )
    parameters = {
        "type": "object",
        "properties": {
            "db": {
                "type": "string",
                "description": "Connection name from config (e.g. dev, prod). Defaults to config default.",
            },
            "phase": {
                "type": "string",
                "enum": ["ALL", "PENDING", "RUNNING", "COMPLETED", "INACTIVE"],
                "description": "High-level phase filter.",
            },
            "status": {
                "type": "string",
                "description": "Optional exact status code filter (e.g. Q, I, R, C, E, X).",
            },
            "program_like": {
                "type": "string",
                "description": "Optional program name contains filter.",
            },
            "requested_by": {
                "type": "string",
                "description": "Optional EBS username contains filter.",
            },
            "last_hours": {
                "type": "integer",
                "description": "Only include requests newer than this many hours.",
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return (1-500).",
            },
        },
    }

    PHASE_CODE = {
        "ALL": None,
        "PENDING": "P",
        "RUNNING": "R",
        "COMPLETED": "C",
        "INACTIVE": "I",
    }

    PHASE_NAME = {
        "P": "Pending",
        "R": "Running",
        "C": "Completed",
        "I": "Inactive",
    }

    STATUS_NAME = {
        "A": "Waiting",
        "B": "Resuming",
        "C": "Normal",
        "D": "Cancelled",
        "E": "Error",
        "F": "Scheduled",
        "G": "Warning",
        "H": "On Hold",
        "I": "Normal",
        "M": "No Manager",
        "Q": "Standby",
        "R": "Normal",
        "S": "Suspended",
        "T": "Terminating",
        "U": "Disabled",
        "W": "Paused",
        "X": "Terminated",
        "Z": "Waiting",
    }

    async def execute(
        self,
        db: str = "",
        phase: str = "ALL",
        status: str = "",
        program_like: str = "",
        requested_by: str = "",
        last_hours: int = 24,
        limit: int = 100,
    ) -> str:
        phase_key = (phase or "ALL").strip().upper()
        if phase_key not in self.PHASE_CODE:
            return "Invalid phase. Use: ALL, PENDING, RUNNING, COMPLETED, INACTIVE"

        if not isinstance(limit, int):
            try:
                limit = int(limit)
            except Exception:
                return "Invalid limit. Must be integer."
        limit = max(1, min(limit, 500))

        if not isinstance(last_hours, int):
            try:
                last_hours = int(last_hours)
            except Exception:
                return "Invalid last_hours. Must be integer."
        last_hours = max(1, min(last_hours, 24 * 90))

        status_code = (status or "").strip().upper()
        if status_code and not re.match(r"^[A-Z]$", status_code):
            return "Invalid status. Use a one-letter EBS status code (e.g. Q, I, R, C, E, X)."

        params: dict[str, object] = {
            "last_hours": last_hours,
            "row_limit": limit,
        }
        where = [
            "r.requested_start_date >= (SYSDATE - (:last_hours / 24))",
        ]

        phase_code = self.PHASE_CODE[phase_key]
        if phase_code:
            where.append("r.phase_code = :phase_code")
            params["phase_code"] = phase_code

        if status_code:
            where.append("r.status_code = :status_code")
            params["status_code"] = status_code

        if program_like.strip():
            where.append("LOWER(p.user_concurrent_program_name) LIKE :program_like")
            params["program_like"] = f"%{program_like.strip().lower()}%"

        if requested_by.strip():
            where.append("LOWER(u.user_name) LIKE :requested_by")
            params["requested_by"] = f"%{requested_by.strip().lower()}%"

        sql = f"""
            SELECT * FROM (
                SELECT
                    r.request_id,
                    p.user_concurrent_program_name AS program_name,
                    u.user_name AS requested_by,
                    r.phase_code,
                    r.status_code,
                    r.requested_start_date,
                    r.actual_start_date,
                    r.actual_completion_date,
                    r.logfile_name,
                    r.outfile_name
                FROM apps.fnd_concurrent_requests r
                LEFT JOIN apps.fnd_concurrent_programs_vl p
                  ON p.concurrent_program_id = r.concurrent_program_id
                 AND p.application_id = r.program_application_id
                LEFT JOIN apps.fnd_user u
                  ON u.user_id = r.requested_by
                WHERE {" AND ".join(where)}
                ORDER BY r.request_id DESC
            ) WHERE ROWNUM <= :row_limit
        """

        try:
            conn = _get_conn(db)
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [[_safe_val(c) for c in r] for r in cur.fetchall()]
            cur.close()
            conn.close()
        except Exception as e:
            return f"EBS concurrent status error: {e}"

        if not rows:
            return (
                f"No concurrent requests found for filters: "
                f"phase={phase_key}, status={status_code or 'ANY'}, last_hours={last_hours}."
            )

        idx = {c.lower(): i for i, c in enumerate(cols)}
        out = [
            f"Rows: {len(rows)} (limit={limit}, last_hours={last_hours}, phase={phase_key}, status={status_code or 'ANY'})",
            "",
            "request_id | phase | status | requested_by | program_name | requested_start | started | completed",
            "-" * 120,
        ]

        for r in rows:
            req_id = r[idx["request_id"]]
            phase_c = str(r[idx["phase_code"]] or "")
            status_c = str(r[idx["status_code"]] or "")
            phase_n = self.PHASE_NAME.get(phase_c, phase_c)
            status_n = self.STATUS_NAME.get(status_c, status_c)
            req_by = str(r[idx["requested_by"]] or "-")
            prog = str(r[idx["program_name"]] or "-")
            req_start = str(r[idx["requested_start_date"]] or "-")
            act_start = str(r[idx["actual_start_date"]] or "-")
            done = str(r[idx["actual_completion_date"]] or "-")

            if len(prog) > 46:
                prog = prog[:43] + "..."

            out.append(
                f"{req_id} | {phase_n}({phase_c}) | {status_n}({status_c}) | "
                f"{req_by} | {prog} | {req_start} | {act_start} | {done}"
            )

        return "\n".join(out)
