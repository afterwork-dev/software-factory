Could this change let someone do or see something they shouldn't?

- **Access**: every new or changed entry point (route, handler, command, query,
  background job) checks who is calling and whether they may touch that
  particular record, not only whether they are signed in.
- **Untrusted input**: input from users, files, URLs, headers or other services
  is validated, and never reaches SQL, shell commands, file paths, templates,
  `eval`, deserializers or redirects without parameterising or escaping.
- **Output**: user-controlled text rendered as HTML or markdown, or placed in
  attributes, URLs or scripts, is escaped. No new raw-HTML sinks.
- **Secrets and data**: no keys, tokens or credentials in code, tests, fixtures
  or logs. Responses, errors and logs don't leak personal data, internal ids or
  stack traces.
- **Sessions and identity**: sign-in, tokens, cookies, password resets and
  webhooks keep their checks (signatures, expiry, CSRF, cookie flags).
- **Abuse**: new public actions that cost money, send messages or do heavy work
  have limits.
- **Dependencies**: new packages are needed, maintained and come from the
  expected source; lockfiles change only as intended.
- **Configuration**: CORS, CSP, file permissions, and storage or database access
  rules aren't loosened.

Use CHANGES only for real, exploitable problems this branch introduces or makes
worse, and name the attack in one sentence. Problems that were already there go
under Notes.
