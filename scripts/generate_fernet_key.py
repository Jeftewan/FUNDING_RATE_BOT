#!/usr/bin/env python3
"""Generate a Fernet encryption key for FERNET_KEY env var."""
from cryptography.fernet import Fernet

key = Fernet.generate_key().decode()
print(f"FERNET_KEY={key}")
print("\nAdd this to your Railway environment variables.")
