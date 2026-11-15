# media45 Security Policy — active development

**Do not expose this commit as a production service**. This is not a penetration-tested or ASVS-certified release.

## Included controls
- Django CSRF middleware, server-side sessions, Secure/HttpOnly/SameSite=Strict session cookies.
- Argon2id-first password hashing; >=15 characters for Django password validation.
- No open registration. No third-party JavaScript, trackers, ads or email.
- Metadata provider addresses are hard-coded (SSRF mitigation).
- Requests require login; externally verified search hits are signed and expire in 15 minutes.
- Per-user search budget is persisted in PostgreSQL; metadata lookups use TMDB.
- Gotify notifications are queued and retried without exposing an Application token to clients.
- No media-write mounts or file execution.

## Blocking risks still to resolve before public release
- No automated administrative account recovery. WebAuthn passkey/YubiKey registration and second-factor assertions are available as an *additional method after password login* and as username-first passwordless login with mandatory authenticator user verification. Both are subject to signature, RP, origin, challenge and sign-counter verification. Password remains available for recovery. TOTP now enforced for staff and optional for users, with one-use recovery codes and anti-replay checks. Enrollment secrets are currently stored in PostgreSQL without application-layer encryption and require protected backups.
- Password login is now DB-throttled for both the main and Django admin login (8 failures per address+username and 60 per source in a 15-minute window). Raw IPs and usernames are not retained in rate-limit records. It uses REMOTE_ADDR, not spoofable forwarding headers; shared reverse-proxy source addresses can share the source quota. Broader abuse handling and independent security review are still pending.
- No complete trusted proxy/TLS configuration, HTTPS redirects or HSTS.
- Static assets production delivery not configured (admin styling might not load).
- No automated integration/security test suite or independently reviewed release.
- Pending security headers, content controls and deployment-level recovery verification.
- Notification worker currently holds DB row locks during network delivery; low traffic only.

Treat all accounts and metadata sources as untrusted; never place secrets in commits, issue bodies or logs.
