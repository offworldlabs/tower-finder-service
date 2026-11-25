"""Cell site source modules for scripts/build_cell_db.py."""

# Widest single carrier per radio, MHz: a database row is one carrier.
CARRIER_MAX_MHZ = {"NR": 100.0, "LTE": 20.0, "UMTS": 5.0, "CDMA": 1.25, "GSM": 0.2}
