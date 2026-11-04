# Accounts and roles


Use delegate_configure only for changes requested by the user. Example new role:

```json
{"section":"agents","name":"quant-validator","value":{"provider":"codex","enabled":true,
 "automatic":true,"workspace":"read-only","instructions":"Validate data contracts and research claims against actual evidence."}}
```

Optional role `models`/`efforts` arrays narrow choices; omit them for per-task freedom.
Providers support `codex`, `claude`, or `anthropic` (an isolated Claude CLI worker against an Anthropic-compatible API).
DeepSeek can reference a Claude Switch Bridge account through bridge_state_dir and bridge_account, or use api_key_env.
Use only the source explicitly chosen by the user. Keep company and official providers/roles distinct. Resolve the
current private snapshot at dispatch, validate its fingerprint and pinned endpoint, and pass authentication only to
that child. Do not copy keys into config/prompts or run claude-switch select/use to launch a worker.
Company metadata uses its own origin and X-Provider header; never send company credentials to the official balance API.
Official balances are API money, not Claude/Codex subscription quota. Company costs/balance may remain unknown.
Model lists are intersected with configured allowlists; respect server effort metadata when supplied. A catalogue entry
alone does not prove inference availability. CLI modelUsage.costUSD is not a verified company/provider bill.
Metadata (models/balance) 401 is not proof that inference is unavailable. Fall back to the configured model allowlist
when discovery fails and report balance as unknown. Test actual inference before diagnosing an invalid account.
If inference itself reports authentication failure, ask the user to check its Bridge account outside chat;
do not silently substitute the other account.
For env-only accounts, ask the user to set the named variable outside chat. Only change child environment variables;
preserve the main Claude subscription route. API-worker histories are isolated under plugin backend-state by provider.
No key values in config, task prompts, logs, or chat. Use api_key_env only. Claude/Anthropic read-only workers have no shell;
Claude/Anthropic write workers have scoped Read/Glob/Grep/Edit/Write tools and report tests for the parent to execute.
Arbitrary OpenAI-compatible providers require another adapter and are not currently supported.
