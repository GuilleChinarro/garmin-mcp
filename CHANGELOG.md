# Changelog

## Unreleased

- curl_cffi DNS fallback: when curl_cffi's built-in resolver fails ("Could not resolve
  host ... Misformatted DNS reply") the name is resolved through the OS, pinned with
  CURLOPT_RESOLVE for 30 minutes and the call is retried once. Covers data calls and
  garminconnect's token refresh/login. `garmin_status` reports when it was used.

## 0.1.0 - 2026-10-07

First release.

- 39 tools covering every feature of Taxuspt/garmin_mcp (see PARITY.md): activities,
  wellness, training, trends, body, devices, gear, achievements, workouts and calendar,
  nutrition, courses, women's health, downloads and deletes.
- `garmin-mcp-auth` logs in once with MFA; the server only reuses tokens. Failed logins
  start a cooldown.
- Windows 11 Cloudflare 403 fallback through curl_cffi.
- FIT analysis with Garmin's official FIT SDK: climbs, grades, decoupling, power curve,
  NP/IF/TSS, R-R HRV, running and cycling dynamics, Di2.
- A validated step language for structured workouts, and checks for raw workout JSON.
- Deletes always preview first; `GARMIN_MCP_READ_ONLY=1` removes every write.
- Unit conversions checked against Garmin Connect on a real account.
