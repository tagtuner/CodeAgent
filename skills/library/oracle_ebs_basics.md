---
name: oracle_ebs_basics
description: Oracle EBS fundamentals and common patterns
tags: [oracle, ebs, sql]
triggers: [ebs, oracle, po_headers, ap_invoices, purchase order, invoice]
---

# Oracle EBS SQL Patterns

## Key Rules
- Always use _all suffix tables (po_headers_all, not po_headers)
- Always filter by org_id for multi-org tables
- Use authorization_status = 'APPROVED' for approved POs
- Use NVL() for nullable numeric columns
- Date columns: use TRUNC() for date-only comparisons

## Common Patterns
- Pending POs: authorization_status IN ('IN PROCESS', 'INCOMPLETE', 'REQUIRES REAPPROVAL')
- Active suppliers: enabled_flag = 'Y' AND NVL(end_date_active, SYSDATE+1) > SYSDATE
- Unpaid invoices: payment_status_flag != 'Y'
