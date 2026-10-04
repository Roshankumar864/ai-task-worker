# Accounts payable procedure (AP-SOP-04)

1. Find the invoice at its source (vendor portal or vendor e-mail). Use the vendor's own document as the source of
   truth for amount, currency, due date and PO number.
2. Check Ledgerly for an existing payable with the same vendor and invoice number. Never record an invoice twice.
3. Record the invoice in Ledgerly under *New payable*, choosing the vendor by its Ledgerly vendor ID from
   `vendors.csv`. Include the PO number when the invoice has one.
4. Payables of 10,000.00 or more (any currency) need sign-off from the AP manager before saving.
5. Recording a payable does **not** pay it. Releasing payments is done only by the treasury team; AP staff and
   automation must never press "Release payment".
6. After saving, open the saved payable and check every field against the source document.
