#!/bin/bash
# Automatisering för order-status, product desc, kundsupport
# Eskalering till Oscar vid kritiskt
PYTHONPATH=skills python -m ecom_ops.order_status_update --site azom
# + product_desc_gen + support_handler
if [[ "$1" == "critical" ]]; then
  echo "Eskalering till Oscar: $2"
fi