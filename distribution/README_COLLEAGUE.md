# AI Ecommerce Crawler Workbench

## Install

Clone the private GitHub repository, open PowerShell in the repository root and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\distribution\install.ps1 -InstallCodex -RunTests
```

The `-InstallCodex` option installs the optional AI fallback when Node.js/npm is available. Complete `codex login` once if prompted.

## Start

```powershell
.\distribution\start.ps1
```

Open `http://127.0.0.1:8773`.

For a trusted local network:

```powershell
.\distribution\start.ps1 -Lan
```

## Verify later

```powershell
.\distribution\verify.ps1
```

The normal platform task extracts the full category directory, collects one selected category's complete product list, and validates the first three parent products with their real variants. Full-site product collection must be selected explicitly and support varies by storefront.

Generated jobs are stored under `runs/`. Do not share that directory without reviewing its evidence and responses.

## Updates

Version 1.0 supports up to 20 sites per submission and runs four sites concurrently. Use the **检查更新** button in the workbench. Updates are fast-forward only and never overwrite local code changes. Because the repository is private, each colleague must have repository access and authenticate Git once on that computer.
