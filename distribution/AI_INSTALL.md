# Instructions for the installation AI

Install and verify this ecommerce crawler on the user's Windows computer.

1. Extract the ZIP to a normal writable folder. Do not run it from inside the ZIP.
2. Open PowerShell in the extracted folder.
3. Confirm Python 3.10 or newer is available. If it is missing, install Python 3.10 or 3.11 using the user's normal package manager, then reopen PowerShell.
4. Run `Set-ExecutionPolicy -Scope Process Bypass` for this PowerShell process only.
5. Run `.\install.ps1 -InstallCodex -RunTests`.
6. If Node.js/npm is missing, install Node.js LTS, then rerun the command.
7. If Codex authentication is required, ask the user to complete `codex login`. Never request or copy their token.
8. Start the service with `.\start.ps1`. Wait until it prints the local address.
9. Open `http://127.0.0.1:8773` and confirm the page loads.
10. Report the Python version, test result, Codex availability, and local URL.

For access from another computer on the same trusted LAN, start with `.\start.ps1 -Lan`. Tell the user that Windows Firewall may ask for permission. Do not expose the service to the public internet.

AI fallback requires an authenticated Codex CLI. Deterministic storefront adapters continue to work without it. A successful installation does not imply every unknown website or full-site crawl is supported.
