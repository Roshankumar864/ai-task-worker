# OurCo systems directory

All paths are relative to the intranet base URL.

| System | Path | What it is | Credentials (get_credentials key) |
|---|---|---|---|
| Corp Mail | `/mail/` | Shared accounts-payable inbox (ap@ourco.example). Vendors e-mail invoice notices here. Supports search (`/mail/?q=...`). | none |
| Acme Supplier Portal | `/acme/` | Acme Corp's external vendor portal. Acme invoices are only published here, not attached to e-mail. | `acme_portal` |
| Ledgerly ERP (web) | `/erp/` | Internal accounts-payable system of record. Vendor invoices are recorded as "payables". | `ledgerly_erp` |
| Ledgerly ERP (API) | `/erp/api/payables?invoice_number=...` | Read-only JSON API. Send the key in the `X-API-Key` header. | `ledgerly_api` |

Vendor master data (vendor names -> Ledgerly vendor IDs) is in `vendors.csv`.
The accounts-payable procedure is in `ap_procedure.md`.
