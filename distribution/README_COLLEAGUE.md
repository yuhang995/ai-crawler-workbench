# AI Ecommerce Crawler Workbench

## Install

Open PowerShell in this folder and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1 -InstallCodex -RunTests
```

The `-InstallCodex` option installs the optional AI fallback when Node.js/npm is available. Complete `codex login` once if prompted.

## Start

```powershell
.\start.ps1
```

Open `http://127.0.0.1:8773`.

For a trusted local network:

```powershell
.\start.ps1 -Lan
```

## Verify later

```powershell
.\verify.ps1
```

The normal platform task extracts the full category directory, collects one selected category's complete product list, and validates the first three parent products with their real variants. Full-site product collection must be selected explicitly and support varies by storefront.

Generated jobs are stored under `runs/`. Do not share that directory without reviewing its evidence and responses.
