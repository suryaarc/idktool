# Finding: CSRF Token Logged in Plaintext to Local File

## Location
[extension/lsClient.js:8, 18-24](extension/lsClient.js#L8)

## What happen

Antigravity IDE language server write own CSRF token to plaintext log file on disk:

```
%APPDATA%\Antigravity IDE\logs\<session>\ls-main.log
```

`lsClient.js` read this file to steal token:

```js
const LOGS_DIR = path.join(os.homedir(), 'AppData', 'Roaming', 'Antigravity IDE', 'logs');
...
const text = fs.readFileSync(logPath, 'utf8');
const tokenMatch = text.match(/--csrf_token (\S+)/);
const portMatch = text.match(/listening on random port at (\d+) for HTTPS/);
```

Token used direct in HTTPS request header to authenticate against local RPC service:

```js
headers: { 'x-codeium-csrf-token': token }
```

## Why this bad

CSRF token meant guard local HTTPS RPC port (`SendUserCascadeMessage` etc) from unauthorized local process access — same origin/process-boundary defense. But since:

1. Token write plaintext to log file
2. Log file world-readable by any process running as same user (no ACL restriction, no permission lockdown)
3. `rejectUnauthorized: false` also set — TLS cert validation off too

...any local process (malicious script, other extension, malware) can read log dir, grab token + port, then make authenticated calls to language server RPC — same as extension does here. Defeats whole purpose of CSRF protection. Log-based secret storage = broken secret storage.

## Attack surface

Whole `lsClient.js` built ON TOP of this hole — it IS proof-of-concept exploit code, functional, working:
- Scans all session log dirs, newest first (`mtime` sort)
- Regex-extract token + port
- Full RPC client using stolen token (`rpcCall`, `sendUserCascadeMessage`, `listConversations`)
- Also reads separate SQLite state DB (`state.vscdb`) for conversation data — different attack surface, no auth needed there at all (direct file read)

## Root cause

Antigravity IDE (Google-based, VS Code fork) dev/debug logging left in production build. `--csrf_token` flag value dumped straight into log during process startup — likely for local dev debugging, never stripped before ship.

## Fix (vendor side, not yours to patch)

Google/Antigravity team need:
- Stop logging `--csrf_token` value to disk (log token presence, not value, or redact)
- If must persist for local IPC handshake, use OS-level secret store (Credential Manager / keychain) not flat log file
- Restrict log dir ACL to prevent cross-process read within same user context (partial mitigation only)
- Fix `rejectUnauthorized: false` too — separate but related weakening

## Your exposure

Since this repo (`idktool`) contain `lsClient.js` that exploit this hole intentionally (per file comment: "TLS-decrypted via SSLKEYLOGFILE + Wireshark" — reverse-engineered on purpose) — flag: this code itself functions as local-privilege-boundary bypass tool against Antigravity IDE. Legit if for own IDE automation/interop on own machine. Not legit if shipped to others' machines without disclosure, since it silently reads another app's auth secrets from disk.
