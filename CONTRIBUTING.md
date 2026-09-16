# Contributing

VARIANT-1 is an early Windows source preview. Small, reproducible issues and
focused pull requests are welcome. Discuss major architecture changes before
implementing them.

1. Read the setup and architecture guides.
2. Work on a short-lived branch and keep one concern per pull request.
3. Run the relevant maintained tests. Describe exactly what passed and what was
   not tested; do not substitute a development run for installer qualification.
4. Preserve the canonical Python/proxy/broker/service path and documented state lifetimes.
5. Include necessary synthetic fixtures, not captured user data or credentials.

Never commit live configuration, API/OAuth tokens, browser profiles, transcripts,
model weights, generated binaries, or private test artifacts. Redact diagnostic
logs before posting them. Native desktop tests can create windows or processes;
run them in a dedicated test environment.

Contributions to original project code are under the repository's MIT license.
Retain third-party notices and disclose the provenance/license of imported code
or assets. Maintainers review changes; a pull request is not a promise of a merge
or a release date.
