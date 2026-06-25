# Incidents

Postmortems for production incidents. One file per incident, named
`YYYY-MM-DD-short-description.md`. Treat them as records, not
adversarial reviews.

## Conventions

- File the postmortem after the incident is resolved, not during.
- Lead with the resolved-status header so future readers can tell
  at a glance whether the issue is still live.
- Include a real timeline. Wall-clock matters because detection
  latency is itself a finding.
- Distinguish *root cause* (what permitted the failure) from
  *trigger* (what tipped the system into failure). Both belong in
  the doc.
- "Lessons" should be specific enough to inform PR review going
  forward. Generic platitudes don't help.
- Cross-link to follow-up issues filed.

The 2026-06-19 toshi incident is the first entry and serves as
the template.
