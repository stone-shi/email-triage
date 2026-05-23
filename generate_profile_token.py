#!/usr/bin/env python3
"""
Secure token generation utility for MCP Multi-Tenant Profile Mapping.
Generates a secure random hex token and saves it under the target profile's .env file.
"""

import argparse
import secrets
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description="Generate and attach a secure profile token for the MCP server")
    parser.add_argument("--profile", type=str, default="default", help="Target profile name (default: root .env)")
    args = parser.parse_args()

    token = secrets.token_hex(16)
    workspace_root = Path(__file__).parent.resolve()

    if args.profile == "default" or not args.profile:
        env_file = workspace_root / ".env"
        profile_display = "default (root)"
    else:
        profile_dir = workspace_root / "profiles" / args.profile
        if not profile_dir.exists():
            print(f"Profile directory '{args.profile}' does not exist under profiles/. Creating it now...")
            profile_dir.mkdir(parents=True, exist_ok=True)
        env_file = profile_dir / ".env"
        profile_display = args.profile

    # Read existing lines to update or append the token key
    lines = []
    token_updated = False
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("EMAIL_TRIAGE_PROFILE_TOKEN="):
                    lines.append(f"EMAIL_TRIAGE_PROFILE_TOKEN={token}\n")
                    token_updated = True
                else:
                    lines.append(line)

    if not token_updated:
        # Append to the end of the file
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"EMAIL_TRIAGE_PROFILE_TOKEN={token}\n")

    # Write the updated configuration back to the environment file
    with open(env_file, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"Success! Generated secure token for profile '{profile_display}':")
    print(f"👉 {token}")
    print(f"\nSaved directly to: {env_file}")
    print("\nHow to pass this token in MCP SSE Client requests:")
    print("  Header option A:  Authorization: Bearer <token>")
    print("  Header option B:  X-Profile-Token: <token>")
    print("  Query Param fallback: ?token=<token>")

if __name__ == "__main__":
    main()
