# APIBOBO integration

Easel uses the official `@apibobo-ai/cli` package and exposes the shared
`apibobo` skill to the OpenClaw workspace through an `AGENT_VAULT` junction.

## Credential lifecycle

The real key is never stored in the repository or Agent Vault:

1. `scripts/easel-services.ps1 import-1password` reads the `apibobo` and
   `minimax api` items from 1Password in memory.
2. Both values are encrypted with Windows DPAPI CurrentUser and written to
   `%LOCALAPPDATA%\Easel\secrets.dpapi.json`.
3. `start`, `self-test`, and `exec` decrypt them only into the process
   environment. Child services inherit `APIBOBO_KEY`; the launcher then clears
   its own copy.

Do not run `bobo keys add`: the launcher deliberately avoids a second plaintext
credential store. Validate the integration with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/easel-services.ps1 self-test
```

The self-test performs a read-only balance request and never prints the balance
payload or key.
