#!/usr/bin/env python3
"""seed.py — Initialize coin database with admin and seed users."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coin

coin.init_db()

# Ensure admin exists with initial funding
conn = coin.get_db()
coin.ensure_account(conn, "admin")
admin_bal = coin.get_balance(conn, "admin")
if admin_bal == 0:
    conn.execute("UPDATE accounts SET balance = 100000, updated_at = datetime('now') WHERE username = 'admin'")
    conn.commit()
    admin_bal = 100000
    print("admin funded with 100000 coins")
else:
    print(f"admin balance: {admin_bal}")

# Create seed users from HONEY_LEDGER if available
honey_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "HONEY_LEDGER.json")
if os.path.isfile(honey_path):
    import json
    with open(honey_path) as f:
        ledger = json.load(f)
    for name, data in ledger.items():
        coin.ensure_account(conn, name)
        honey = data.get("HONEY", 0)
        if honey > 0 and coin.get_balance(conn, name) < honey:
            # Transfer from admin via atomic guarded update
            cur = conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE username = 'admin' AND balance >= ?",
                (honey, honey),
            )
            if cur.rowcount:
                conn.execute("UPDATE accounts SET balance = balance + ? WHERE username = ?", (honey, name))
                conn.commit()
                print(f"  Seeded {name}: {honey} coins")
            else:
                print(f"  SKIP {name}: admin balance insufficient")

conn.close()
print("Seed complete")
