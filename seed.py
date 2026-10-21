"""Create the initial admin, agent and employee accounts.

Passwords are read from SEED_ADMIN_PASSWORD, SEED_AGENT_PASSWORD and SEED_EMPLOYEE_PASSWORD (environment or .env).
Existing users are left unchanged, so the script is safe to run more than once.
"""
import os
import sys

from app import create_app
from models import User, db

ACCOUNTS = [
    {
        "username": "admin",
        "full_name": "Facility Admin",
        "role": "admin",
        "department": "",
        "team": None,
        "email_env": "SEED_ADMIN_EMAIL",
        "default_email": "admin@example.com",
        "password_env": "SEED_ADMIN_PASSWORD",
    },
    {
        "username": "agent",
        "full_name": "Facility Agent",
        "role": "agent",
        "department": "",
        "team": "MAINTENANCE",
        "email_env": "SEED_AGENT_EMAIL",
        "default_email": "agent@example.com",
        "password_env": "SEED_AGENT_PASSWORD",
    },
    {
        "username": "employee",
        "full_name": "Sample Employee",
        "role": "employee",
        "department": "DPS",
        "team": None,
        "email_env": "SEED_EMPLOYEE_EMAIL",
        "default_email": "employee@example.com",
        "password_env": "SEED_EMPLOYEE_PASSWORD",
    },
]

MIN_PASSWORD_LENGTH = 8


def main():
    app = create_app()
    exit_code = 0
    with app.app_context():
        for account in ACCOUNTS:
            username = account["username"]
            if User.query.filter_by(username=username).first():
                print(f"User '{username}' already exists; skipped.")
                continue
            password = os.environ.get(account["password_env"], "")
            if len(password) < MIN_PASSWORD_LENGTH:
                print(
                    f"Set {account['password_env']} (at least {MIN_PASSWORD_LENGTH} characters) "
                    f"to create '{username}'.",
                    file=sys.stderr,
                )
                exit_code = 1
                continue
            user = User(
                username=username,
                full_name=account["full_name"],
                role=account["role"],
                department=account["department"],
                team=account["team"],
                email=os.environ.get(account["email_env"]) or account["default_email"],
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            print(f"Created {account['role']} user '{username}'.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
