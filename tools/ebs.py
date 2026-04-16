from __future__ import annotations
from .base import BaseTool

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
